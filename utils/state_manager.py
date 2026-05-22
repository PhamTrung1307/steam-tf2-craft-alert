import json
import os
from typing import Any, Dict


State = Dict[str, Dict[str, Any]]


def load_state(path: str = "state.json") -> State:
    if not os.path.exists(path):
        return {}

    try:
        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file)
    except (json.JSONDecodeError, OSError):
        return {}

    if not isinstance(data, dict):
        return {}

    return data


def save_state(state: State, path: str = "state.json") -> bool:
    temp_path = f"{path}.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as file:
            json.dump(state, file, ensure_ascii=False, indent=2)
            file.write("\n")
        os.replace(temp_path, path)
        return True
    except OSError:
        return False
