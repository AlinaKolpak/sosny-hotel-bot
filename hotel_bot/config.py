"""Настройки: секреты читаются локально, а не хранятся в коде."""

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


def load_env(path: str = ".env") -> None:
    if not Path(path).is_file():
        return
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not key.strip().replace("_", "").isalnum():
            raise ValueError("Некорректная строка в .env; ожидается NAME=value")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


@dataclass
class Settings:
    db_path: str = "data/hotel.db"
    provider: str = "openai"
    model: str = "gpt-4.1-mini"
    ollama_url: str = "http://127.0.0.1:11434"
    api_key: str = ""
    api_url: str = "https://api.openai.com/v1"
    timeout: int = 120
    telegram_token: str = ""
    manager_id: str = ""

    @classmethod
    def from_env(cls):
        load_env()
        return cls(
            db_path=os.getenv("DB_PATH", "data/hotel.db"),
            provider=os.getenv("LLM_PROVIDER", "openai"),
            model=os.getenv("LLM_MODEL", "gpt-4.1-mini"),
            ollama_url=os.getenv("OLLAMA_URL", "http://127.0.0.1:11434"),
            api_key=os.getenv("OPENAI_API_KEY", ""),
            api_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            timeout=int(os.getenv("LLM_TIMEOUT", "120")),
            telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            manager_id=os.getenv("MANAGER_ID", ""),
        )

    def validate_ai(self):
        if self.provider not in {"ollama", "openai"}:
            raise ValueError("LLM_PROVIDER должен быть ollama или openai")
        if not self.model or not 1 <= self.timeout <= 300:
            raise ValueError("Укажите LLM_MODEL и LLM_TIMEOUT от 1 до 300 секунд")
        url = urlparse(self.ollama_url if self.provider == "ollama" else self.api_url)
        if self.provider == "ollama":
            if url.scheme not in {"http", "https"} or url.hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError("Локальный режим Ollama разрешает только localhost")
        elif not self.api_key or url.scheme != "https":
            raise ValueError("Для внешнего ИИ нужны OPENAI_API_KEY и HTTPS URL")
