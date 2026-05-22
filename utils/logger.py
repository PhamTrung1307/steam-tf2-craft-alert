from datetime import datetime


def timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(message: str) -> None:
    print(f"[{timestamp()}] {message}", flush=True)


def log_warning(message: str) -> None:
    log(f"WARNING | {message}")


def log_error(message: str) -> None:
    log(f"ERROR | {message}")
