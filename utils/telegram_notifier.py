from typing import Optional

import requests


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, timeout: int = 20) -> None:
        self.token = token
        self.chat_id = chat_id
        self.timeout = timeout
        self.api_url = f"https://api.telegram.org/bot{token}/sendMessage"

    def send_message(self, text: str) -> bool:
        if not self.token or not self.chat_id:
            return False

        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": False,
        }

        try:
            response = requests.post(self.api_url, json=payload, timeout=self.timeout)
        except requests.RequestException:
            return False

        if response.status_code != 200:
            return False

        try:
            data: Optional[dict] = response.json()
        except ValueError:
            return False

        return bool(data and data.get("ok"))
