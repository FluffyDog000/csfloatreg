"""Почта firstmail через их HTTP API — вместо браузерного входа в Outlook.

Это единственный способ не зависеть от интерфейса почтовика: письмо забирается
запросом, а не кликами по OWA. Адреса и имена параметров лежат в конфиге
(mail.firstmail), а не в коде: документация API меняется чаще, чем этот файл.

Имена полей внутри ответа мы НЕ фиксируем: письмо ищется по всем строкам JSON.
Так провайдер переживает переименование полей и разницу между тарифами.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterator

from ...errors import MailBadCredentials, MailNotReceived, NetworkError
from ...logging_setup import register_secret
from .base import extract_link, extract_token, register

#: Значения по умолчанию. Всё это перекрывается секцией mail.firstmail в конфиге.
DEFAULTS = {
    # Ключ из панели (/panel/api/keys/) — это ключ ПАНЕЛЬНОГО API: он ходит
    # с заголовком Authorization: Bearer и базой firstmail.ltd/api/v1.
    # У сервиса есть и второй, «рыночный» API (api.firstmail.ltd/v1/market/…,
    # заголовок X-API-KEY) — там нужен отдельный ключ.
    "base_url": "https://firstmail.ltd/api/v1",
    "message_path": "/market/get/message",
    "messages_path": None,
    "auth_header": "Authorization",
    "auth_prefix": "Bearer ",       # для X-API-KEY поставь пустую строку
    "username_param": "username",
    "password_param": "password",
    "timeout_s": 30,
}

#: Поля, в которых API обычно держит идентификатор письма.
_ID_KEYS = ("id", "uid", "message_id", "messageId", "msg_id", "guid")

#: Ключи, под которыми может лежать список писем.
_LIST_KEYS = ("messages", "mails", "emails", "items", "data", "result", "results")


class EndpointMissing(MailBadCredentials):
    """Сервис не знает такого URL. Отдельный тип, чтобы перебрать остальные."""


def _texts(node) -> Iterator[str]:
    """Все строки ответа: имена полей у API могут поменяться, текст письма — нет."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _texts(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from _texts(value)


def messages_of(payload) -> list[dict]:
    """Приводит ответ к списку писем, каким бы ни была его обёртка."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in _LIST_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            return [value]
    return [payload]


def message_key(message: dict) -> str:
    """Стабильный ключ письма: id, если он есть, иначе отпечаток содержимого."""
    for key in _ID_KEYS:
        value = message.get(key)
        if isinstance(value, (str, int)) and str(value):
            return f"{key}:{value}"
    blob = json.dumps(message, ensure_ascii=False, sort_keys=True, default=str)
    return "sha1:" + hashlib.sha1(blob.encode("utf-8")).hexdigest()


def has_message(payload) -> bool:
    """Явный отказ API «писем нет» — чтобы не принимать его за письмо."""
    if isinstance(payload, dict):
        for key in ("has_message", "hasMessage", "success", "status"):
            value = payload.get(key)
            if value is False:
                return False
    return True


#: Где панель firstmail держит спецификацию своего API.
SPEC_URLS = (
    "https://firstmail.ltd/static/api/openapi.json",
    "https://firstmail.ltd/api/v1/openapi.json",
    "https://firstmail.ltd/api/openapi.json",
    "https://firstmail.ltd/api/schema/",          # drf-spectacular
    "https://firstmail.ltd/api/v1/schema/",
    "https://firstmail.ltd/api/v1/swagger.json",
    "https://api.firstmail.ltd/openapi.json",
)

#: Кандидаты на базовый адрес и путь — перебираем, когда настроенный отдаёт 404.
CANDIDATE_BASES = (
    "https://firstmail.ltd/api/v1",      # панельный API, ключ Bearer
    "https://api.firstmail.ltd/v1",      # рыночный API, ключ X-API-KEY
)
CANDIDATE_PATHS = (
    "/market/get/message",
    "/mail/",
    "/mails/",
    "/mailbox/",
    "/messages/",
    "/mail/messages/",
    "/mail/one",
)

#: Адрес из подсказки панели: по нему проверяется, что ключ вообще принимают.
KNOWN_OK_PATH = "/domains/"


def raw_get(url: str, headers: dict | None = None, *, timeout: float = 20.0) -> tuple[int, str]:
    """Запрос без исключений: отдаёт код и тело как есть. Нужен разведке."""
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "csfloatreg/1.0", **(headers or {})},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, f"сеть: {exc}"


def spec_summary(text: str) -> list[str]:
    """Короткая выжимка из openapi.json: путь, метод, параметры."""
    try:
        spec = json.loads(text)
    except ValueError:
        return []
    lines = []
    for path, methods in (spec.get("paths") or {}).items():
        if not isinstance(methods, dict):
            continue
        for method, body in methods.items():
            if method.lower() not in ("get", "post", "put", "patch", "delete"):
                continue
            params = [
                str(item.get("name"))
                for item in (body.get("parameters") or [])
                if isinstance(item, dict) and item.get("name")
            ]
            summary = str((body or {}).get("summary") or "")[:60]
            lines.append(f"{method.upper():<5} {path:<40} {', '.join(params) or '—':<40} {summary}")
    return sorted(lines)


@register("firstmail")
class FirstMailProvider:
    """Ящик firstmail. Браузер не открывается вообще."""

    name = "firstmail"

    def __init__(self, ctx):
        self.ctx = ctx
        self.log = ctx.log
        self.cfg = {**DEFAULTS, **(ctx.cfg.get("mail.firstmail") or {})}
        self.api_key = str(self.cfg.get("api_key") or os.getenv("FIRSTMAIL_API_KEY") or "").strip()
        self.mail = ctx.account.mail
        self.password = ctx.account.mail_password
        register_secret(self.api_key)
        self._seen: set[str] = set()
        self._path: str | None = None      # какой эндпоинт сработал

    # ── доступ ───────────────────────────────────────────────
    async def login(self) -> None:
        """Логина как такового нет: проверяем ключ и запоминаем старые письма."""
        if not self.api_key:
            raise MailBadCredentials(self._no_key_hint())
        if not self.mail or not self.password:
            raise MailBadCredentials("в accounts.txt нет почты или пароля от неё")

        messages = await self._fetch()
        self._seen = {message_key(m) for m in messages}
        self.log.info("firstmail: ящик %s доступен, писем в выдаче: %d", self.mail, len(messages))

    def _no_key_hint(self) -> str:
        """Ключ не найден — говорим, ГДЕ искали: гадать по одной строке лога невыносимо."""
        cfg = getattr(self.ctx, "cfg", None)
        files = []
        try:
            files = [str(path) for path in cfg.sources()]
        except Exception:  # noqa: BLE001 — конфиг мог прийти из теста
            pass
        where = ", ".join(files) if files else "(файл конфига неизвестен)"
        legacy = ""
        try:
            stale = cfg.stale_local(cfg.path) if cfg.path else None
            if stale is not None:
                legacy = (
                    f" ВНИМАНИЕ: рядом лежит {stale.name} — он больше не читается,"
                    " ключ должен быть в config.yaml."
                )
        except Exception:  # noqa: BLE001
            pass
        return (
            "не задан mail.firstmail.api_key (ключ из панели firstmail, /panel/api/keys/). "
            f"Прочитан: {where}.{legacy} "
            "После правки конфига нажми «Перечитать конфиг» в интерфейсе или перезапусти "
            "процесс — на лету файл не перечитывается. Ключ можно положить и в переменную "
            "окружения FIRSTMAIL_API_KEY."
        )

    async def close(self) -> None:
        return None

    # ── ожидание письма ──────────────────────────────────────
    async def wait_for_link(
        self, pattern: str, *, timeout_s: float, poll_s: float, include_existing: bool = False
    ) -> str:
        return await self._wait(
            lambda message: extract_link(pattern, *_texts(message)),
            timeout_s=timeout_s, poll_s=poll_s, what="ссылку", include_existing=include_existing,
        )

    async def wait_for_code(
        self, pattern: str, *, timeout_s: float, poll_s: float, include_existing: bool = False
    ) -> str:
        return await self._wait(
            lambda message: extract_token(pattern, *_texts(message)),
            timeout_s=timeout_s, poll_s=poll_s, what="токен", include_existing=include_existing,
        )

    async def _wait(
        self, extractor, *, timeout_s: float, poll_s: float, what: str, include_existing: bool = False
    ) -> str:
        if include_existing:
            # письмо могло прийти до того, как мы начали ждать
            self._seen.clear()
        deadline = time.monotonic() + timeout_s
        checked = 0
        while True:
            for message in await self._fetch():
                key = message_key(message)
                if key in self._seen:
                    continue
                self._seen.add(key)
                checked += 1
                value = extractor(message)
                if value:
                    return value
                self.log.debug("firstmail: письмо %s без совпадения", key[:24])
            if time.monotonic() >= deadline:
                raise MailNotReceived(
                    f"firstmail: за {timeout_s:.0f} c не пришло письмо, из которого можно взять {what} "
                    f"(новых писем просмотрено: {checked})"
                )
            await asyncio.sleep(poll_s)

    # ── транспорт ────────────────────────────────────────────
    async def _fetch(self) -> list[dict]:
        """Письма ящика. Сначала список, если его не отдают — последнее письмо."""
        paths = [self._path] if self._path else [self.cfg["messages_path"], self.cfg["message_path"]]
        last_error: Exception | None = None
        for path in paths:
            if not path:
                continue
            try:
                payload = await asyncio.to_thread(self._request, path)
            except EndpointMissing as exc:
                # такого URL у сервиса нет — пробуем следующий кандидат
                last_error = exc
                continue
            except MailBadCredentials:
                raise
            except NetworkError as exc:
                last_error = exc
                continue
            self._path = path
            if not has_message(payload):
                return []
            return messages_of(payload)
        raise last_error or NetworkError("firstmail: не удалось получить письма")

    def _request(self, path: str):
        query = urllib.parse.urlencode(
            {
                self.cfg["username_param"]: self.mail,
                self.cfg["password_param"]: self.password,
            }
        )
        url = f"{str(self.cfg['base_url']).rstrip('/')}{path}?{query}"
        request = urllib.request.Request(
            url,
            headers={
                self.cfg["auth_header"]: f"{self.cfg.get('auth_prefix') or ''}{self.api_key}",
                "Accept": "application/json",
                "User-Agent": "csfloatreg/1.0",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=float(self.cfg["timeout_s"])) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            short = " ".join(body.split())[:200]
            if exc.code in (401, 403):
                raise MailBadCredentials(
                    f"firstmail: сервис не принял ключ ({exc.code}): {short}. "
                    f"Ключ длиной {len(self.api_key)} символов, заголовок {self.cfg['auth_header']}. "
                    "Чаще всего ключ скопирован не целиком — возьми его заново в панели "
                    "(/panel/api/keys/) и проверь `python main.py --mail-probe`"
                ) from None
            if exc.code == 404:
                # HTML вместо JSON означает «нет такого адреса», а не «нет ящика»:
                # раньше мы сваливали это в одну кучу и искали проблему не там
                if not body.lstrip().startswith(("{", "[")):
                    raise EndpointMissing(
                        f"firstmail: сервер не знает адрес {url.split('?')[0]} (404, ответ не JSON). "
                        f"Проверь mail.firstmail.base_url и *_path по документации API "
                        f"(`python main.py --mail-probe` покажет, какие адреса рабочие)"
                    ) from None
                raise MailBadCredentials(f"firstmail: ящик {self.mail} не найден (404) {short}") from None
            raise NetworkError(f"firstmail: HTTP {exc.code} {short}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise NetworkError(f"firstmail: сеть недоступна ({exc})") from None

        try:
            return json.loads(body)
        except ValueError:
            raise NetworkError(f"firstmail: ответ не JSON ({body[:200]})") from None
