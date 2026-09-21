"""Иерархия ошибок.

Правило простое: RetryableError -> повторяем попытку, FatalError -> сразу
фиксируем статус и переходим к следующему аккаунту.
"""
from __future__ import annotations


class BotError(Exception):
    """База. status попадает в results.csv."""

    status = "error"
    retryable = False

    def __init__(self, message: str = "", *, stage: str | None = None):
        super().__init__(message or self.__class__.__name__)
        self.stage = stage


# ── Повторяемые ──────────────────────────────────────────────
class RetryableError(BotError):
    retryable = True


class NetworkError(RetryableError):
    """Прокси отвалился, DNS, ERR_CONNECTION_*, таймаут загрузки."""


class StepTimeout(RetryableError):
    """Ожидаемый элемент/состояние не появились за отведённое время."""


class UnexpectedState(RetryableError):
    """Страница не там, где ждали. Часто лечится перезапуском."""


# ── Фатальные ────────────────────────────────────────────────
class FatalError(BotError):
    retryable = False


class BadCredentials(FatalError):
    status = "bad_credentials"


class SteamLocked(FatalError):
    status = "steam_locked"


class SteamRateLimited(FatalError):
    status = "steam_rate_limited"


class SteamEmailCodeRequired(FatalError):
    status = "steam_email_code_required"


class SteamMobileConfirmRequired(FatalError):
    status = "steam_mobile_confirm_required"


class CaptchaDetected(FatalError):
    status = "captcha"


class MaFileMissing(FatalError):
    status = "no_mafile"


class MaFileEncrypted(FatalError):
    status = "no_mafile"


class MailBadCredentials(FatalError):
    status = "mail_bad_credentials"


class MailBlocked(FatalError):
    status = "mail_blocked"


class MailVerifyRequired(FatalError):
    status = "mail_verify_required"


class MailNotReceived(FatalError):
    status = "mail_not_received"


class BrowserNotInstalled(FatalError):
    """Движок браузера не скачан. Ретраить бессмысленно — нужен `camoufox fetch`."""

    status = "browser_missing"


class NotImplementedYet(FatalError):
    status = "not_implemented"


# ── Ошибки конфигурации/загрузки (до старта прогона) ─────────
class ConfigError(Exception):
    pass


class LoaderError(Exception):
    pass
