"""Логи: консоль + файл, префикс [login], вырезание секретов.

register_secret() пополняет глобальный реестр. Фильтр стоит на всех хендлерах,
поэтому пароль не попадёт ни в лог, ни в дамп HTML (см. redact()).
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable

_SECRETS: set[str] = set()
_MASK = "***"

LOGGER_NAME = "bot"


def register_secret(*values: str | None) -> None:
    for value in values:
        if value and isinstance(value, str) and len(value) >= 3:
            _SECRETS.add(value)


def register_secrets(values: Iterable[str | None]) -> None:
    register_secret(*values)


def redact(text: str) -> str:
    """Убирает известные секреты из произвольного текста (логи, HTML-дампы)."""
    if not text or not _SECRETS:
        return text
    for secret in sorted(_SECRETS, key=len, reverse=True):
        if secret in text:
            text = text.replace(secret, _MASK)
    return text


class _RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: redact(v) if isinstance(v, str) else v for k, v in record.args.items()}
            else:
                record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


class AccountLogger(logging.LoggerAdapter):
    """Добавляет [login] в начало каждой строки."""

    def process(self, msg, kwargs):
        return f"[{self.extra['login']}] {msg}", kwargs


_configured = False


def setup(log_dir: Path, *, level: str = "INFO", run_name: str | None = None) -> Path:
    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    log_dir.mkdir(parents=True, exist_ok=True)
    run_name = run_name or datetime.now().strftime("run_%Y%m%d_%H%M%S")
    log_path = log_dir / f"{run_name}.log"

    if _configured:
        return log_path

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(getattr(logging, level.upper(), logging.INFO))
    console.setFormatter(fmt)
    console.addFilter(_RedactFilter())

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    file_handler.addFilter(_RedactFilter())

    logger.addHandler(console)
    logger.addHandler(file_handler)
    logger.propagate = False
    _configured = True
    return log_path


def add_handler(handler: logging.Handler) -> None:
    """Подключить дополнительный приёмник (например, SSE-хаб веб-интерфейса)."""
    handler.addFilter(_RedactFilter())
    logging.getLogger(LOGGER_NAME).addHandler(handler)


def get_logger(login: str | None = None) -> logging.Logger | AccountLogger:
    base = logging.getLogger(LOGGER_NAME)
    if login is None:
        return base
    return AccountLogger(base, {"login": login})
