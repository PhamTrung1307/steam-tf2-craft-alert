from datetime import datetime
import sys

try:
    from colorama import Fore, Style, just_fix_windows_console
except ImportError:  # Keep Render/local runs alive even before dependencies install.
    Fore = Style = None

    def just_fix_windows_console() -> None:
        return None


just_fix_windows_console()


def timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def safe_print(message: str) -> None:
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        fallback = (
            message.replace("✅", "OK")
            .replace("❌", "NO")
            .replace("❔", "??")
            .replace("⚠️", "!!")
        )
        print(fallback.encode("ascii", errors="replace").decode("ascii"), flush=True)


def log(message: str) -> None:
    safe_print(f"[{timestamp()}] {message}")


def log_warning(message: str) -> None:
    log(f"WARNING | {message}")


def log_error(message: str) -> None:
    log(f"ERROR | {message}")


def color_text(message: str, color: object) -> str:
    if not Fore or not Style or not color or not sys.stdout.isatty():
        return message
    return f"{color}{message}{Style.RESET_ALL}"


def log_scan_header(started_at: str, item_count: int) -> None:
    print(f"===== Scan started {started_at} | {item_count} items =====", flush=True)


def log_item_status(status: str, item_name: str, parse_method: str = "") -> None:
    icons = {
        "CRAFTABLE": "✅",
        "NOT_CRAFTABLE": "❌",
        "UNKNOWN": "❔",
        "ERROR": "⚠️",
    }
    colors = {
        "CRAFTABLE": getattr(Fore, "GREEN", None),
        "NOT_CRAFTABLE": getattr(Fore, "RED", None),
        "UNKNOWN": getattr(Fore, "YELLOW", None),
        "ERROR": getattr(Fore, "YELLOW", None),
    }
    suffix = f" | {parse_method}" if parse_method else ""
    line = f"{icons.get(status, '??')} {status:<13} | {item_name}{suffix}"
    safe_print(color_text(line, colors.get(status)))


def log_scan_summary(
    craftable: int,
    not_craftable: int,
    unknown: int,
    errors: int,
    elapsed_seconds: float,
    next_scan_seconds: int,
) -> None:
    safe_print(
        "Summary: "
        f"{craftable} craftable, "
        f"{not_craftable} not craftable, "
        f"{unknown} unknown, "
        f"{errors} errors. "
        f"Duration: {elapsed_seconds:.1f}s. "
        f"Next scan in {next_scan_seconds}s."
    )
