import argparse
import json
import logging
import os
import signal
import threading
import time
from typing import Dict, List, Optional

from dotenv import load_dotenv
from flask import Flask, Response
from werkzeug.serving import make_server

from utils.logger import (
    log,
    log_error,
    log_item_status,
    log_scan_header,
    log_scan_summary,
    log_warning,
    safe_print,
)
from utils.state_manager import load_state, save_state
from utils.steam_checker import (
    SteamCheckResult,
    check_market_item,
    create_session,
)
from utils.telegram_notifier import TelegramNotifier


LINK_FILE = "Link.txt"
STATE_FILE = "state.json"
HEALTH_RESPONSE = "TF2 Craft Alert Running"
shutdown_event = threading.Event()


def get_int_env(name: str, default: Optional[int] = None) -> Optional[int]:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default

    try:
        parsed = int(value)
    except ValueError:
        log_warning(f"Invalid {name}={value!r}. Using default {default}.")
        return default

    return parsed


def load_config() -> Dict[str, object]:
    load_dotenv()

    required = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
    missing = [name for name in required if not os.getenv(name)]

    config = {
        "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN", ""),
        "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID", ""),
        "check_interval": get_int_env("CHECK_INTERVAL", 60),
        "request_delay": get_int_env("REQUEST_DELAY", 5),
        "request_timeout": get_int_env("REQUEST_TIMEOUT", 20),
        "missing_env": missing,
    }

    return config


def has_required_telegram_env(config: Dict[str, object]) -> bool:
    missing = config.get("missing_env", [])
    return not missing


def log_missing_telegram_env(config: Dict[str, object]) -> None:
    missing = config.get("missing_env", [])
    if missing:
        log_error(
            "Missing Telegram environment variables: "
            f"{', '.join(str(name) for name in missing)}. "
            "Add them in Render Environment Variables or local .env."
        )


def read_links(path: str = LINK_FILE) -> List[str]:
    if not os.path.exists(path):
        log_error(f"{path} not found. Create it and add one Steam Market URL per line.")
        return []

    links: List[str] = []
    with open(path, "r", encoding="utf-8") as file:
        for line in file:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            links.append(stripped)

    if not links:
        log_error(f"{path} is empty. Add Steam Market URLs before running the monitor.")

    return links


def should_notify(previous_status: Optional[str], current_status: str) -> bool:
    if current_status != "CONFIRMED_CRAFTABLE":
        return False

    return previous_status in {"NOT_CRAFTABLE", "UNKNOWN", "ERROR", None}


def is_safe_craftable_result(result: SteamCheckResult) -> bool:
    return (
        result.status == "CRAFTABLE"
        and not result.found_not_craftable_pattern
        and not result.trusted_negative_sources
        and not result.parser_conflict_detected
        and not result.dom_visible_text_has_not_usable
        and not result.raw_html_has_not_usable
        and not result.anti_bot_detected
        and result.http_status == 200
    )


def build_message(result: SteamCheckResult) -> str:
    parser = result.used_layout if result.used_layout in {"beta", "classic"} else result.parse_method
    return (
        "✅ TF2 Craftable Item Alert\n"
        f"Item: {result.item_name}\n"
        "Status: CONFIRMED_CRAFTABLE\n"
        f"Parser: {parser}\n"
        f"URL: {result.url}\n"
        f"Time: {result.checked_at}"
    )


def log_detection_debug(result: SteamCheckResult) -> None:
    log(f"{result.item_name} | FOUND_NOT_CRAFTABLE_PATTERN={result.found_not_craftable_pattern}")
    log(f"{result.item_name} | PARSER={result.parse_method} | LAYOUT={result.used_layout}")
    log(f"{result.item_name} | FINAL_DECISION_REASON={result.final_decision_reason}")
    if result.trusted_negative_sources:
        log(f"{result.item_name} | TRUSTED_NEGATIVE_SOURCES={','.join(result.trusted_negative_sources)}")
    if result.parser_conflict_detected:
        log_warning(f"{result.item_name} | PARSER_CONFLICT_DOWNGRADE_TO_NOT_CRAFTABLE")
    if result.matched_text:
        log(f"{result.item_name} | MATCHED_TEXT={result.matched_text}")
    if result.description_text:
        log(f"{result.item_name} | DESCRIPTION_TEXT={result.description_text}")
    if result.parsed_json is not None:
        log(f"{result.item_name} | SSR_RENDER_CONTEXT_PARSED=True")
    if result.anti_bot_detected:
        log_warning(f"{result.item_name} | Possible Steam anti-bot page detected.")

    if result.description_html:
        log(f"{result.item_name} | ITEM_DESCRIPTION_HTML_BEGIN")
        safe_print(result.description_html)
        log(f"{result.item_name} | ITEM_DESCRIPTION_HTML_END")
    else:
        log_warning(f"{result.item_name} | Item description HTML block not found in response.")


def verify_craftable_three_times(
    url: str,
    timeout: int,
    session: object,
    first_result: SteamCheckResult,
) -> Optional[SteamCheckResult]:
    if not is_safe_craftable_result(first_result):
        return None

    results = [first_result]
    for _attempt in range(2):
        time.sleep(5)
        result = check_market_item(url, timeout=timeout, session=session)
        results.append(result)
        if not is_safe_craftable_result(result):
            log_warning(
                f"Craftable verification stopped | {result.item_name} | "
                f"status={result.status} parser={result.parse_method} "
                f"reason={result.final_decision_reason}"
            )
            return None

    confirmed = results[-1]
    confirmed.status = "CONFIRMED_CRAFTABLE"
    confirmed.parse_method = "confirmed_3x_no_negative_signals"
    return confirmed


def run_check_cycle(
    config: Dict[str, object],
    notifier: TelegramNotifier,
    debug: bool = False,
) -> None:
    links = read_links()
    if not links:
        return

    started_at = time.strftime("%Y-%m-%d %H:%M:%S")
    started_monotonic = time.monotonic()
    counts = {
        "CRAFTABLE": 0,
        "NOT_CRAFTABLE": 0,
        "UNKNOWN": 0,
        "ERROR": 0,
    }
    log_scan_header(started_at, len(links))

    state = load_state(STATE_FILE)
    request_delay = int(config["request_delay"])
    request_timeout = int(config["request_timeout"])
    check_interval = int(config["check_interval"])
    session = create_session()

    for index, url in enumerate(links):
        result = check_market_item(url, timeout=request_timeout, session=session)
        previous_status = state.get(url, {}).get("status")
        notification_result = verify_craftable_three_times(
            url=url,
            timeout=request_timeout,
            session=session,
            first_result=result,
        )
        display_result = notification_result if notification_result else result
        count_status = "CRAFTABLE" if display_result.status == "CONFIRMED_CRAFTABLE" else display_result.status
        counts[count_status] = counts.get(count_status, 0) + 1

        log_item_status(count_status, display_result.item_name, display_result.parse_method)
        if display_result.error and debug:
            log_error(f"{display_result.item_name} | {display_result.error}")

        if debug:
            log_detection_debug(display_result)

        if notification_result and should_notify(previous_status, notification_result.status):
            sent = notifier.send_message(build_message(notification_result))
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

        save_state(state, STATE_FILE)

        if index < len(links) - 1:
            time.sleep(request_delay)

    elapsed = time.monotonic() - started_monotonic
    log_scan_summary(
        craftable=counts.get("CRAFTABLE", 0),
        not_craftable=counts.get("NOT_CRAFTABLE", 0),
        unknown=counts.get("UNKNOWN", 0),
        errors=counts.get("ERROR", 0),
        elapsed_seconds=elapsed,
        next_scan_seconds=check_interval,
    )


def run_monitor(config: Dict[str, object], debug: bool = False) -> None:
    log("Monitor started")

    if not has_required_telegram_env(config):
        while not shutdown_event.is_set():
            log_missing_telegram_env(config)
            shutdown_event.wait(int(config["check_interval"]))
        return

    notifier = TelegramNotifier(
        token=str(config["telegram_bot_token"]),
        chat_id=str(config["telegram_chat_id"]),
        timeout=int(config["request_timeout"]),
    )

    while not shutdown_event.is_set():
        try:
            run_check_cycle(config, notifier, debug=debug)
            shutdown_event.wait(int(config["check_interval"]))
        except Exception as exc:
            log_error(f"Unexpected error: {exc}")
            shutdown_event.wait(int(config["check_interval"]))


def create_health_app() -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def health() -> Response:
        return Response(HEALTH_RESPONSE, mimetype="text/plain")

    return app


def run_web_server(port: int) -> None:
    logging.getLogger("werkzeug").disabled = True
    server = make_server("0.0.0.0", port, create_health_app(), threaded=True)
    server.timeout = 1
    log(f"Health server listening on 0.0.0.0:{port}")
    log(HEALTH_RESPONSE)

    while not shutdown_event.is_set():
        server.handle_request()

    server.server_close()


def get_port() -> int:
    value = os.getenv("PORT", "8000")
    try:
        return int(value)
    except ValueError:
        log_warning(f"Invalid PORT={value!r}. Using 8000.")
        return 8000


def test_telegram(config: Dict[str, object]) -> None:
    if not has_required_telegram_env(config):
        log_missing_telegram_env(config)
        return

    notifier = TelegramNotifier(
        token=str(config["telegram_bot_token"]),
        chat_id=str(config["telegram_chat_id"]),
        timeout=int(config["request_timeout"]),
    )
    ok = notifier.send_message("steam-tf2-craft-alert test message")
    if ok:
        log("Telegram test message sent.")
    else:
        log_error("Telegram test message failed.")


def debug_single(url: str, config: Dict[str, object]) -> None:
    session = create_session()
    result = check_market_item(
        url,
        timeout=int(config["request_timeout"]),
        session=session,
        include_html=True,
    )

    if result.raw_html is not None:
        with open("debug.html", "w", encoding="utf-8") as file:
            file.write(result.raw_html)
        log("Saved raw HTML to debug.html")
    else:
        log_warning("No raw HTML received, debug.html was not written.")

    if result.description_text is not None:
        with open("debug_extracted_text.txt", "w", encoding="utf-8") as file:
            file.write(result.description_text)
            file.write("\n")
        log("Saved extracted text to debug_extracted_text.txt")
    else:
        log_warning("No parsed description text, debug_extracted_text.txt was not written.")

    debug_result = {
        "url": url,
        "expected_item_name_from_url": result.item_name,
        "parsed_item_name": result.parsed_item_name,
        "appid": result.appid,
        "status": result.status,
        "parse_method": result.parse_method,
        "found_not_craftable": result.found_not_craftable_pattern,
        "matched_patterns": result.matched_patterns,
        "extracted_texts": result.extracted_texts,
        "request_status_code": result.http_status,
        "is_antibot_page": result.anti_bot_detected,
        "used_layout": result.used_layout,
        "parser_results": result.parser_results,
        "trusted_negative_sources": result.trusted_negative_sources,
        "parser_conflict_detected": result.parser_conflict_detected,
        "dom_visible_text_has_not_usable": result.dom_visible_text_has_not_usable,
        "raw_html_has_not_usable": result.raw_html_has_not_usable,
        "final_decision_reason": result.final_decision_reason,
    }
    with open("debug_result.json", "w", encoding="utf-8") as file:
        json.dump(debug_result, file, ensure_ascii=False, indent=2)
        file.write("\n")
    log("Saved debug result to debug_result.json")

    if result.error:
        log_error(f"{result.item_name} | {result.status} | {result.error}")
    else:
        log_item_status(result.status, result.item_name, result.parse_method)

    log_detection_debug(result)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor TF2 Steam Community Market pages and alert when items are craftable."
    )
    parser.add_argument("--once", action="store_true", help="Run one check cycle then exit.")
    parser.add_argument(
        "--test-telegram",
        action="store_true",
        help="Send a test Telegram message then exit.",
    )
    parser.add_argument(
        "--debug-single",
        metavar="URL",
        help="Debug one Steam Market URL, save raw HTML to debug.html, and print matched text.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print detection debug details during normal monitor or --once runs.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config()

    if args.test_telegram:
        test_telegram(config)
        return

    if args.debug_single:
        debug_single(args.debug_single, config)
        return

    if args.once:
        if not has_required_telegram_env(config):
            log_missing_telegram_env(config)

        notifier = TelegramNotifier(
            token=str(config["telegram_bot_token"]),
            chat_id=str(config["telegram_chat_id"]),
            timeout=int(config["request_timeout"]),
        )
        run_check_cycle(config, notifier, debug=args.debug)
        return

    def stop(_signum: int, _frame: object) -> None:
        shutdown_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    monitor_thread = threading.Thread(
        target=run_monitor,
        args=(config, args.debug),
        name="tf2craft-monitor",
    )
    web_thread = threading.Thread(
        target=run_web_server,
        args=(get_port(),),
        name="tf2craft-web",
    )

    monitor_thread.start()
    web_thread.start()

    monitor_thread.join()
    web_thread.join()


if __name__ == "__main__":
    main()
