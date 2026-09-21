"""Интерфейс почтового провайдера.

Модуль регистрации не знает, откуда берётся письмо. Чтобы добавить IMAP или
Graph API, достаточно написать класс с этими тремя методами и зарегистрировать
его в PROVIDERS — csfloat.py и runner.py трогать не нужно.
"""
from __future__ import annotations

import re
from typing import Protocol
from urllib.parse import parse_qs, unquote, urlparse


class MailProvider(Protocol):
    name: str

    async def login(self) -> None:
        """Аутентификация в почте. Бросает MailBlocked/MailVerifyRequired/MailBadCredentials."""

    async def wait_for_link(self, pattern: str, *, timeout_s: float, poll_s: float) -> str:
        """Ждёт письмо и возвращает первую ссылку, подходящую под regex."""

    async def wait_for_code(self, pattern: str, *, timeout_s: float, poll_s: float) -> str:
        """Ждёт письмо и возвращает код по regex (задел под email-коды Steam)."""

    async def close(self) -> None:
        ...


_SAFELINK = re.compile(r"safelinks\.protection\.outlook\.com", re.I)


def unwrap_safelink(url: str) -> str:
    """Outlook иногда заворачивает ссылки в свой редирект — разворачиваем."""
    if not _SAFELINK.search(url):
        return url
    query = parse_qs(urlparse(url).query)
    target = query.get("url", [None])[0]
    return unquote(target) if target else url


_URL_RE = re.compile(r"""https?://[^\s"'<>)\]}]+""")


def extract_link(pattern: str, *sources: str) -> str | None:
    """Ищет ссылку по regex в HTML/тексте письма.

    Сначала разворачивает каждый найденный URL (safelinks, html-экранирование),
    потом сверяет с шаблоном — иначе обёрнутая ссылка не совпадёт никогда.
    """
    regex = re.compile(pattern, re.I)
    for source in sources:
        if not source:
            continue
        cleaned = source.replace("&amp;", "&")
        for raw in _URL_RE.findall(cleaned):
            candidate = unwrap_safelink(raw.rstrip("\"'<>)]},.;"))
            if regex.search(candidate):
                return candidate
    # запасной путь: шаблон может описывать не URL целиком
    for source in sources:
        if not source:
            continue
        match = regex.search(source.replace("&amp;", "&"))
        if match:
            return unwrap_safelink(match.group(0))
    return None


PROVIDERS: dict[str, type] = {}


def register(name: str):
    def wrapper(cls):
        PROVIDERS[name] = cls
        cls.name = name
        return cls

    return wrapper


def build_mail_provider(ctx):
    from . import outlook_web  # noqa: F401 — регистрация провайдера

    name = (ctx.cfg.get("mail.provider") or "outlook_web").lower()
    provider = PROVIDERS.get(name)
    if provider is None:
        raise NotImplementedError(
            f"mail.provider='{name}' не реализован. Доступны: {sorted(PROVIDERS)}"
        )
    return provider(ctx)
