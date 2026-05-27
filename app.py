import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext
from typing import Dict, Optional

from dotenv import load_dotenv

import main as bot
from utils.logger import log, log_error, log_item_status, log_scan_header, log_scan_summary, log_warning
from utils.state_manager import load_state, save_state
from utils.steam_checker import create_session, check_market_item
from utils.telegram_notifier import TelegramNotifier


APP_DIR = Path(__file__).resolve().parent
ENV_FILE = APP_DIR / ".env"
LINK_FILE = APP_DIR / bot.LINK_FILE
STATE_FILE = APP_DIR / bot.STATE_FILE

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


class CraftAlertApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("TF2 Craft Alert")
        self.geometry("820x620")
        self.minsize(720, 520)

        self.log_queue: "queue.Queue[str]" = queue.Queue()
        sys.stdout = QueueWriter(self.log_queue)

        self.stop_event = threading.Event()
        self.monitor_thread: Optional[threading.Thread] = None
        self.test_thread: Optional[threading.Thread] = None

        self.vars = {name: tk.StringVar(value=value) for name, value in DEFAULTS.items()}
        self.status_var = tk.StringVar(value="Stopped")

        self._build_ui()
        self._load_config_into_form()
        self.after(150, self._drain_logs)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        config_frame = tk.LabelFrame(self, text="Config", padx=12, pady=10)
        config_frame.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 8))
        config_frame.columnconfigure(1, weight=1)

        fields = [
            ("Telegram Bot Token", "TELEGRAM_BOT_TOKEN", True),
            ("Telegram Chat ID", "TELEGRAM_CHAT_ID", False),
            ("Check Interval", "CHECK_INTERVAL", False),
            ("Request Delay", "REQUEST_DELAY", False),
            ("Request Timeout", "REQUEST_TIMEOUT", False),
        ]
        for row, (label, key, secret) in enumerate(fields):
            tk.Label(config_frame, text=label).grid(row=row, column=0, sticky="w", pady=3)
            show = "*" if secret else ""
            tk.Entry(config_frame, textvariable=self.vars[key], show=show).grid(
                row=row, column=1, sticky="ew", padx=(10, 0), pady=3
            )

        buttons = tk.Frame(self)
        buttons.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 8))
        buttons.columnconfigure(6, weight=1)

        self.start_button = tk.Button(buttons, text="Start Bot", width=14, command=self.start_bot)
        self.start_button.grid(row=0, column=0, padx=(0, 8))
        self.stop_button = tk.Button(buttons, text="Stop Bot", width=14, command=self.stop_bot, state=tk.DISABLED)
        self.stop_button.grid(row=0, column=1, padx=(0, 8))
        tk.Button(buttons, text="Test Telegram", width=14, command=self.test_telegram).grid(row=0, column=2, padx=(0, 8))
        tk.Button(buttons, text="Open Link.txt", width=14, command=self.open_links).grid(row=0, column=3, padx=(0, 8))
        tk.Button(buttons, text="Save Config", width=14, command=self.save_config).grid(row=0, column=4, padx=(0, 8))
        tk.Label(buttons, textvariable=self.status_var, anchor="e").grid(row=0, column=6, sticky="e")

        log_frame = tk.LabelFrame(self, text="Log", padx=8, pady=8)
        log_frame.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 12))
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)

        self.log_text = scrolledtext.ScrolledText(log_frame, wrap=tk.WORD, state=tk.DISABLED, height=18)
        self.log_text.grid(row=0, column=0, sticky="nsew")

    def _load_config_into_form(self) -> None:
        load_dotenv(ENV_FILE, override=True)
        for key, default in DEFAULTS.items():
            self.vars[key].set(os.getenv(key, default))

    def _config_from_form(self) -> Dict[str, object]:
        for key, var in self.vars.items():
            os.environ[key] = var.get().strip()
        return bot.load_config()

    def save_config(self) -> None:
        lines = []
        for key in DEFAULTS:
            value = self.vars[key].get().strip()
            lines.append(f"{key}={value}")
            os.environ[key] = value
        ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        log(f"Saved config to {ENV_FILE.name}")

    def start_bot(self) -> None:
        if self.monitor_thread and self.monitor_thread.is_alive():
            log_warning("Bot is already running.")
            return

        self.save_config()
        self.stop_event.clear()
        bot.shutdown_event.clear()
        self.monitor_thread = threading.Thread(target=self._monitor_loop, name="tf2craft-gui-monitor", daemon=True)
        self.monitor_thread.start()
        self.status_var.set("Running")
        self.start_button.config(state=tk.DISABLED)
        self.stop_button.config(state=tk.NORMAL)
        log("GUI monitor started")

    def stop_bot(self) -> None:
        self.stop_event.set()
        bot.shutdown_event.set()
        self.status_var.set("Stopping...")
        self.stop_button.config(state=tk.DISABLED)
        log("Stop requested. Current request or scan will finish first.")

    def test_telegram(self) -> None:
        if self.test_thread and self.test_thread.is_alive():
            log_warning("Telegram test is already running.")
            return
        self.save_config()
        self.test_thread = threading.Thread(target=bot.test_telegram, args=(self._config_from_form(),), daemon=True)
        self.test_thread.start()

    def open_links(self) -> None:
        if not LINK_FILE.exists():
            LINK_FILE.write_text("", encoding="utf-8")
        try:
            os.startfile(str(LINK_FILE))
        except OSError:
            subprocess.Popen(["notepad.exe", str(LINK_FILE)])
        log(f"Opened {LINK_FILE.name}")

    def _monitor_loop(self) -> None:
        config = self._config_from_form()
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

            if index < len(links) - 1 and not self.stop_event.wait(request_delay):
                continue

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
        self.after(0, self._set_stopped)

    def _set_stopped(self) -> None:
        self.status_var.set("Stopped")
        self.start_button.config(state=tk.NORMAL)
        self.stop_button.config(state=tk.DISABLED)

    def _drain_logs(self) -> None:
        while True:
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self._append_log(line)
        self.after(150, self._drain_logs)

    def _append_log(self, line: str) -> None:
        if "<html" in line.lower() or "<div" in line.lower() or "<script" in line.lower():
            return
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, line + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _on_close(self) -> None:
        if self.monitor_thread and self.monitor_thread.is_alive():
            self.stop_bot()
            messagebox.showinfo("TF2 Craft Alert", "Bot is stopping. Close the app again in a moment.")
            return
        self.destroy()


if __name__ == "__main__":
    os.chdir(APP_DIR)
    CraftAlertApp().mainloop()
