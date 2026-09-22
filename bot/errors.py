"""Иерархия ошибок.

Правило простое: RetryableError -> повторяем попытку, FatalError -> сразу
фиксируем статус и переходим к следующему аккаунту.
"""
from __future__ import annotations


class BotError(Exception):
    """База. Сообщение показывается пользователю в интерфейсе."""

    status = "error"
    retryable = False

    def __init__(self, message: str = "", *, stage: str | None = None):
        super().__init__(message or self.__class__.__name__)
        self.stage = stage


class RetryableError(BotError):
    retryable = True


class NetworkError(RetryableError):
    """Прокси отвалился, DNS, ERR_CONNECTION_*, таймаут загрузки."""


class FatalError(BotError):
    retryable = False


class ProxyAuthFailed(FatalError):
    """Прокси отверг логин/пароль."""

    status = "proxy_auth_failed"


class BrowserNotInstalled(FatalError):
    """Движок браузера не скачан: нужен `camoufox fetch`."""

    status = "browser_missing"


class ConfigError(Exception):
    pass


class LoaderError(Exception):
    pass
