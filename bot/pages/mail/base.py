"""Интерфейс почтового провайдера.

Модуль регистрации не знает, откуда берётся письмо. Чтобы добавить IMAP или
Graph API, достаточно написать класс с этими тремя методами и зарегистрировать
его в PROVIDERS — csfloat.py и runner.py трогать не нужно.
"""
from __future__ import annotations

import re
from html import unescape
from typing import Protocol
from urllib.parse import parse_qs, unquote, urlparse


class MailProvider(Protocol):
    name: str

    async def login(self) -> None:
        """Аутентификация в почте. Бросает MailBlocked/MailVerifyRequired/MailBadCredentials."""

    async def wait_for_link(
        self, pattern: str, *, timeout_s: float, poll_s: float, include_existing: bool = False
    ) -> str:
        """Ждёт письмо и возвращает первую ссылку, подходящую под regex.

        include_existing=True — смотреть и те письма, что лежали в ящике до начала
        ожидания: так бывает, когда CSFloat отправил письмо в прошлый заход.
        """

    async def wait_for_code(
        self, pattern: str, *, timeout_s: float, poll_s: float, include_existing: bool = False
    ) -> str:
        """Ждёт письмо и возвращает код по regex."""

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


#: Слова, которые стоят рядом с токеном, но токеном не являются.
_NOT_TOKEN = {
    "token", "code", "email", "verify", "verification", "please", "type", "below",
    "your", "this", "that", "the", "and", "for", "you", "it", "is", "to", "in",
    "spam", "trash", "folder", "promotional", "csfloat", "here", "link", "click",
}
_CANDIDATE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{3,63}")
_KEYWORD = re.compile(r"\b(?:token|code|код|токен)\b", re.I)


def _looks_like_token(value: str) -> bool:
    value = value.strip(".,;:!?)»\"'")
    if len(value) < 4 or value.lower() in _NOT_TOKEN:
        return False
    if any(ch.isdigit() for ch in value):
        return True
    # набор заглавных без цифр тоже бывает токеном
    return len(value) >= 6 and value == value.upper()


def extract_token(pattern: str | None, *sources: str) -> str | None:
    """Достаёт токен подтверждения из письма.

    Сначала пробует шаблон из конфига, затем эвристику: ищет слово token/code
    и берёт за ним первое значение, похожее на токен. Одной регуляркой это не
    решается — формулировки писем слишком разные.
    """
    regex = re.compile(pattern, re.I) if pattern else None
    for source in sources:
        if not source:
            continue
        text = unescape(source)
        if regex:
            for match in regex.finditer(text):
                candidate = (match.group(1) if match.groups() else match.group(0)).strip()
                if _looks_like_token(candidate):
                    return candidate.strip(".,;:!?)»\"'")
        for keyword in _KEYWORD.finditer(text):
            window = text[keyword.end() : keyword.end() + 120]
            for candidate in _CANDIDATE.findall(window):
                if _looks_like_token(candidate):
                    return candidate.strip(".,;:!?)»\"'")
    return None


PROVIDERS: dict[str, type] = {}


def register(name: str):
    def wrapper(cls):
        PROVIDERS[name] = cls
        cls.name = name
        return cls

    return wrapper


def build_mail_provider(ctx):
    from . import firstmail_api, outlook_web  # noqa: F401 — регистрация провайдеров

    name = (ctx.cfg.get("mail.provider") or "firstmail").lower()
    provider = PROVIDERS.get(name)
    if provider is None:
        raise NotImplementedError(
            f"mail.provider='{name}' не реализован. Доступны: {sorted(PROVIDERS)}"
        )
    return provider(ctx)
