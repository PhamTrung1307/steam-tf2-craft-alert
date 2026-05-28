import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from dotenv import load_dotenv
from PySide6.QtCore import QObject, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QColor, QCloseEvent, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QPushButton,
    QPlainTextEdit,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

import main as bot
from utils.logger import log, log_error, log_item_status, log_scan_header, log_scan_summary, log_warning
from utils.state_manager import load_state, save_state
from utils.steam_checker import check_market_item, create_session
from utils.telegram_notifier import TelegramNotifier


APP_NAME = "TF2 Craft Alert"


def resource_path(relative_path: str) -> Path:
    base_path = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base_path / relative_path


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = app_dir()
ENV_FILE = APP_DIR / "config.env"
LEGACY_ENV_FILE = APP_DIR / ".env"
LINK_FILE = APP_DIR / bot.LINK_FILE
STATE_FILE = APP_DIR / bot.STATE_FILE
ICON_FILE = resource_path("icon.ico")

DEFAULTS = {
    "TELEGRAM_BOT_TOKEN": "",
    "TELEGRAM_CHAT_ID": "",
    "CHECK_INTERVAL": "60",
    "REQUEST_DELAY": "5",
    "REQUEST_TIMEOUT": "20",
}


class QueueWriter:
    def __init__(self, log_queue: "queue.Queue[str]") -> None:
        self.log_queue = log_queue

    def write(self, message: str) -> int:
        if message and message.strip():
            self.log_queue.put(message.rstrip())
        return len(message)

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False


class UiSignals(QObject):
    stopped = Signal()


class CraftAlertWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(900, 660)
        self.setMinimumSize(760, 540)

        self._ensure_user_files()

        self.log_queue: "queue.Queue[str]" = queue.Queue()
        sys.stdout = QueueWriter(self.log_queue)
        sys.stderr = QueueWriter(self.log_queue)

        self.stop_event = threading.Event()
        self.monitor_thread: Optional[threading.Thread] = None
        self.test_thread: Optional[threading.Thread] = None
        self.is_exiting = False
        self.config_valid = False
        self.signals = UiSignals()
        self.signals.stopped.connect(self._set_stopped)

        self.inputs: Dict[str, QLineEdit] = {}
        self.status_label = QLabel("Stopped")
        self.warning_label = QLabel("")
        self.warning_label.setStyleSheet("color: #a15c00;")

        self._build_ui()
        self._load_config_into_form()
        self._build_tray()
        self._validate_config(show_warning=True)

        self.log_timer = QTimer(self)
        self.log_timer.timeout.connect(self._drain_logs)
        self.log_timer.start(150)
        log(f"{APP_NAME} ready. Bot is stopped until Start Bot is pressed.")

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        config_box = QGroupBox("Config")
        form = QFormLayout(config_box)
        form.setLabelAlignment(Qt.AlignLeft)

        fields = [
            ("Telegram Bot Token", "TELEGRAM_BOT_TOKEN", True),
            ("Telegram Chat ID", "TELEGRAM_CHAT_ID", False),
            ("Check Interval", "CHECK_INTERVAL", False),
            ("Request Delay", "REQUEST_DELAY", False),
            ("Request Timeout", "REQUEST_TIMEOUT", False),
        ]
        for label, key, secret in fields:
            edit = QLineEdit()
            if secret:
                edit.setEchoMode(QLineEdit.Password)
            edit.textChanged.connect(self._validate_config)
            self.inputs[key] = edit
            form.addRow(label, edit)

        layout.addWidget(config_box)
        layout.addWidget(self.warning_label)

        button_row = QHBoxLayout()
        self.start_button = QPushButton("Start Bot")
        self.stop_button = QPushButton("Stop Bot")
        self.test_button = QPushButton("Test Telegram")
        open_button = QPushButton("Open Link.txt")
        save_button = QPushButton("Save Config")
        exit_button = QPushButton("Exit App")

        self.start_button.clicked.connect(self.start_bot)
        self.stop_button.clicked.connect(self.stop_bot)
        self.test_button.clicked.connect(self.test_telegram)
        open_button.clicked.connect(self.open_links)
        save_button.clicked.connect(self.save_config)
        exit_button.clicked.connect(self.exit_app)

        self.stop_button.setEnabled(False)
        for button in (self.start_button, self.stop_button, self.test_button, open_button, save_button, exit_button):
            button.setMinimumWidth(110)
            button_row.addWidget(button)
        button_row.addStretch(1)
        self.status_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        button_row.addWidget(self.status_label)
        layout.addLayout(button_row)

        log_box = QGroupBox("Realtime Log")
        log_layout = QGridLayout(log_box)
        self.log_text = QPlainTextEdit()
        self.log_text.setReadOnly(True)
        log_layout.addWidget(self.log_text, 0, 0)
        layout.addWidget(log_box, 1)

        separator = QFrame()
        separator.setFrameShape(QFrame.HLine)
        separator.setFrameShadow(QFrame.Sunken)
        layout.addWidget(separator)
        self.setCentralWidget(root)

    def _build_tray(self) -> None:
        icon = self._app_icon()
        self.setWindowIcon(icon)
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip(APP_NAME)

        menu = QMenu()
        self.show_action = QAction("Show", self)
        self.tray_start_action = QAction("Start Bot", self)
        self.tray_stop_action = QAction("Stop Bot", self)
        self.exit_action = QAction("Exit", self)

        self.show_action.triggered.connect(self.show_window)
        self.tray_start_action.triggered.connect(self.start_bot)
        self.tray_stop_action.triggered.connect(self.stop_bot)
        self.exit_action.triggered.connect(self.exit_app)

        menu.addAction(self.show_action)
        menu.addAction(self.tray_start_action)
        menu.addAction(self.tray_stop_action)
        menu.addSeparator()
        menu.addAction(self.exit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def _app_icon(self) -> QIcon:
        if ICON_FILE.exists():
            return QIcon(str(ICON_FILE))
        pixmap = QPixmap(256, 256)
        pixmap.fill(QColor("#1b2838"))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor("#66c0f4"))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(24, 24, 208, 208, 36, 36)
        painter.setBrush(QColor("#1b2838"))
        painter.drawRect(58, 70, 140, 22)
        painter.drawRect(58, 116, 140, 22)
        painter.drawRect(58, 162, 94, 22)
        painter.drawEllipse(166, 148, 40, 40)
        painter.end()
        return QIcon(pixmap)

    def _ensure_user_files(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        if not ENV_FILE.exists():
            if LEGACY_ENV_FILE.exists():
                ENV_FILE.write_text(LEGACY_ENV_FILE.read_text(encoding="utf-8"), encoding="utf-8")
            else:
                ENV_FILE.write_text(
                    "\n".join(f"{key}={value}" for key, value in DEFAULTS.items()) + "\n",
                    encoding="utf-8",
                )
        if not LINK_FILE.exists():
            LINK_FILE.write_text("# Add one Steam Community Market URL per line.\n", encoding="utf-8")
        if not STATE_FILE.exists():
            STATE_FILE.write_text("{}\n", encoding="utf-8")

    def _load_config_into_form(self) -> None:
        load_dotenv(ENV_FILE, override=True)
        for key, default in DEFAULTS.items():
            self.inputs[key].setText(os.getenv(key, default))

    def _config_validation_state(self) -> tuple[list[str], bool]:
        missing = []
        if not self.inputs["TELEGRAM_BOT_TOKEN"].text().strip():
            missing.append("Telegram Bot Token")
        if not self.inputs["TELEGRAM_CHAT_ID"].text().strip():
            missing.append("Telegram Chat ID")

        valid_numbers = True
        for key in ("CHECK_INTERVAL", "REQUEST_DELAY", "REQUEST_TIMEOUT"):
            try:
                if int(self.inputs[key].text().strip()) < 0:
                    valid_numbers = False
            except ValueError:
                valid_numbers = False

        return missing, valid_numbers

    def _validate_config(self, show_warning: bool = False) -> bool:
        missing, valid_numbers = self._config_validation_state()
        if missing:
            self.warning_label.setText(f"Missing {', '.join(missing)}. Start Bot is disabled until config is complete.")
        elif not valid_numbers:
            self.warning_label.setText("Interval, delay, and timeout must be valid non-negative numbers.")
        else:
            self.warning_label.setText("")

        self.config_valid = not missing and valid_numbers
        can_start = self.config_valid and not self._is_running()
        self.start_button.setEnabled(can_start)
        self.test_button.setEnabled(self.config_valid)
        if hasattr(self, "tray_start_action"):
            self.tray_start_action.setEnabled(can_start)
            self.tray_stop_action.setEnabled(self._is_running())
        if show_warning and missing:
            log_warning("Telegram token/chat id missing. Fill them in and click Save Config.")
        return can_start

    def _is_running(self) -> bool:
        return bool(self.monitor_thread and self.monitor_thread.is_alive())

    def _config_from_form(self) -> Dict[str, object]:
        for key, edit in self.inputs.items():
            os.environ[key] = edit.text().strip()
        return bot.load_config()

    def save_config(self) -> None:
        lines = []
        for key in DEFAULTS:
            value = self.inputs[key].text().strip()
            lines.append(f"{key}={value}")
            os.environ[key] = value
        ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self._validate_config()
        log(f"Saved config to {ENV_FILE.name}")

    def start_bot(self) -> None:
        if self._is_running():
            log_warning("Bot is already running.")
            return
        if not self._validate_config(show_warning=True):
            return

        self.save_config()
        config = self._config_from_form()
        self.stop_event.clear()
        bot.shutdown_event.clear()
        self.monitor_thread = threading.Thread(
            target=self._monitor_loop,
            args=(config,),
            name="tf2craft-gui-monitor",
            daemon=True,
        )
        self.monitor_thread.start()
        self.status_label.setText("Running")
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self._validate_config()
        log("GUI monitor started")

    def stop_bot(self) -> None:
        if not self._is_running():
            self._set_stopped()
            return
        self.stop_event.set()
        bot.shutdown_event.set()
        self.status_label.setText("Stopping...")
        self.stop_button.setEnabled(False)
        self._validate_config()
        log("Stop requested. Current request or scan will finish first.")

    def test_telegram(self) -> None:
        if self.test_thread and self.test_thread.is_alive():
            log_warning("Telegram test is already running.")
            return
        if not self._validate_config(show_warning=True):
            return
        self.save_config()
        self.test_thread = threading.Thread(target=bot.test_telegram, args=(self._config_from_form(),), daemon=True)
        self.test_thread.start()

    def open_links(self) -> None:
        if not LINK_FILE.exists():
            LINK_FILE.write_text("# Add one Steam Community Market URL per line.\n", encoding="utf-8")
        try:
            os.startfile(str(LINK_FILE))
        except OSError:
            subprocess.Popen(["notepad.exe", str(LINK_FILE)])
        log(f"Opened {LINK_FILE.name}")

    def _monitor_loop(self, config: Dict[str, object]) -> None:
        if not bot.has_required_telegram_env(config):
            bot.log_missing_telegram_env(config)
            self._monitor_finished()
            return

        notifier = TelegramNotifier(
            token=str(config["telegram_bot_token"]),
            chat_id=str(config["telegram_chat_id"]),
            timeout=int(config["request_timeout"]),
        )
        log("Monitor started")

        while not self.stop_event.is_set():
            try:
                self._run_check_cycle(config, notifier)
                self.stop_event.wait(int(config["check_interval"]))
            except Exception as exc:
                log_error(f"Unexpected error: {exc}")
                self.stop_event.wait(int(config["check_interval"]))

        log("Monitor stopped")
        self._monitor_finished()

    def _run_check_cycle(self, config: Dict[str, object], notifier: TelegramNotifier) -> None:
        links = bot.read_links(str(LINK_FILE))
        if not links:
            return

        started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        started_monotonic = time.monotonic()
        counts = {"CRAFTABLE": 0, "NOT_CRAFTABLE": 0, "UNKNOWN": 0, "ERROR": 0}
        log_scan_header(started_at, len(links))

        state = load_state(str(STATE_FILE))
        request_delay = int(config["request_delay"])
        request_timeout = int(config["request_timeout"])
        check_interval = int(config["check_interval"])
        session = create_session()

        for index, url in enumerate(links):
            if self.stop_event.is_set():
                break

            result = check_market_item(url, timeout=request_timeout, session=session)
            previous_status = state.get(url, {}).get("status")
            notification_result = bot.verify_craftable_three_times(
                url=url,
                timeout=request_timeout,
                session=session,
                first_result=result,
            )
            display_result = notification_result if notification_result else result
            count_status = "CRAFTABLE" if display_result.status == "CONFIRMED_CRAFTABLE" else display_result.status
            counts[count_status] = counts.get(count_status, 0) + 1

            log_item_status(count_status, display_result.item_name, display_result.parse_method)
            if display_result.error:
                log_error(f"{display_result.item_name} | {display_result.error}")

            if notification_result and bot.should_notify(previous_status, notification_result.status):
                sent = notifier.send_message(bot.build_message(notification_result))
                if sent:
                    log(f"Telegram sent | {notification_result.item_name}")
                else:
                    log_warning(f"Telegram failed | {notification_result.item_name}")

            state[url] = {
                "item_name": result.item_name,
                "status": notification_result.status if notification_result else result.status,
                "last_checked_at": result.checked_at,
                "last_error": result.error,
            }
            save_state(state, str(STATE_FILE))

            if index < len(links) - 1:
                self.stop_event.wait(request_delay)

        elapsed = time.monotonic() - started_monotonic
        log_scan_summary(
            craftable=counts.get("CRAFTABLE", 0),
            not_craftable=counts.get("NOT_CRAFTABLE", 0),
            unknown=counts.get("UNKNOWN", 0),
            errors=counts.get("ERROR", 0),
            elapsed_seconds=elapsed,
            next_scan_seconds=check_interval,
        )

    def _monitor_finished(self) -> None:
        self.signals.stopped.emit()

    def _set_stopped(self) -> None:
        self.status_label.setText("Stopped")
        self.stop_button.setEnabled(False)
        self._validate_config()

    def _drain_logs(self) -> None:
        while True:
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            if "<html" in line.lower() or "<div" in line.lower() or "<script" in line.lower():
                continue
            self.log_text.appendPlainText(line)

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.DoubleClick, QSystemTrayIcon.Trigger):
            self.show_window()

    def show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.is_exiting:
            event.accept()
            return
        event.ignore()
        self.hide()
        log("Window hidden. Use the tray icon to show or exit.")

    def exit_app(self) -> None:
        self.is_exiting = True
        self.stop_event.set()
        bot.shutdown_event.set()
        self.tray.hide()
        QApplication.quit()


def main() -> int:
    os.chdir(APP_DIR)
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    window = CraftAlertWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
