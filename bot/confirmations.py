"""Мобильные подтверждения Steam — то же, что делает SDA.

Подтверждения живут на steamcommunity.com/mobileconf. Каждый запрос подписан
HMAC-ключом из identity_secret, а авторизация берётся из cookies уже открытого
профиля: отдельный мобильный логин мы не делаем. Если Steam для этой сессии
подтверждения не отдаёт, это видно по тексту ошибки, а не по пустому списку.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
from urllib.parse import urlencode

from .logging_setup import get_logger
from .models import MaFile
from .steam_guard import device_id as make_device_id

BASE = "https://steamcommunity.com/mobileconf"
COMMUNITY = "https://steamcommunity.com"

#: Заголовки мобильного клиента Steam: без них часть ответов приходит как HTML.
HEADERS = {
    "X-Requested-With": "com.valvesoftware.android.steam.community",
    "Referer": f"{BASE}/conf",
}


def _headers(params: dict) -> dict:
    """Referer — полный адрес страницы подтверждений с подписью, как шлёт SDA.

    В SteamDesktopAuthenticator (GenerateConfirmationURL) заголовок Referer
    собирается из тех же параметров, что и запрос. Голый /mobileconf/conf —
    наша вольность, а Steam к мелочам мобильного клиента придирчив.
    """
    query = urlencode({**params, "tag": "conf"})
    return {**HEADERS, "Referer": f"{BASE}/conf?{query}"}

#: Сначала пробуем современный клиент (react), потом старый (android).
CLIENTS = ("react", "android")

#: Тег участвует в подписи, и Steam ждёт на операции ровно «allow»/«cancel» —
#: те же слова, что и в op. На собственные выдумки вроде «accept» он отвечает
#: голым {"success": false} без единого слова объяснения. Старые теги остаются
#: вторым заходом: вдруг где-то ещё принимаются.
OP_TAGS = {True: ("allow", "accept"), False: ("cancel", "reject")}

#: Признаки мобильного клиента. Список Steam отдаёт и так, а вот операцию над
#: подтверждением от «не мобильной» сессии умеет отклонять молча — ровно тем
#: самым {"success": false} без единого слова. SDA и steampy ставят эти cookies
#: при входе; у нас сессия браузерная, поэтому выставляем их сами.
MOBILE_COOKIES = (
    ("mobileClientVersion", "777777 3.6.4"),
    ("mobileClient", "android"),
    ("Steam_Language", "english"),
)


async def prepare(context) -> None:
    """Добавляет в профиль cookies мобильного клиента. Без них Steam капризничает."""
    cookies = [
        {"name": name, "value": value, "domain": ".steamcommunity.com", "path": "/"}
        for name, value in MOBILE_COOKIES
    ]
    try:
        await context.add_cookies(cookies)
    except Exception as exc:  # noqa: BLE001 — не повод отказываться от попытки
        get_logger().debug("Не удалось поставить cookies мобильного клиента: %s", exc)


class ConfirmationError(RuntimeError):
    """Steam не отдал список или отказал в операции — причина в тексте."""


class RateLimited(ConfirmationError):
    """Steam придерживает запросы с этого IP. Долбиться дальше — только хуже."""


class OfferGone(ConfirmationError):
    """Обмена, к которому относится подтверждение, больше нет."""


@dataclasses.dataclass(slots=True)
class Confirmation:
    id: str
    nonce: str
    creator_id: str = ""        # id обмена, к которому относится подтверждение
    type: int = 0
    type_name: str = ""
    headline: str = ""
    summary: list[str] = dataclasses.field(default_factory=list)
    icon: str = ""
    creation_time: int = 0
    accept: str = "Подтвердить"
    cancel: str = "Отклонить"

    @classmethod
    def from_json(cls, raw: dict) -> "Confirmation":
        summary = raw.get("summary") or []
        if isinstance(summary, str):
            summary = [summary]
        return cls(
            id=str(raw.get("id") or ""),
            # nonce в новом API, key — в старом
            nonce=str(raw.get("nonce") or raw.get("key") or ""),
            creator_id=str(raw.get("creator_id") or raw.get("creator") or ""),
            type=int(raw.get("type") or 0),
            type_name=str(raw.get("type_name") or ""),
            headline=str(raw.get("headline") or ""),
            summary=[str(s) for s in summary],
            icon=str(raw.get("icon") or ""),
            creation_time=int(raw.get("creation_time") or 0),
            accept=str(raw.get("accept") or "Подтвердить"),
            cancel=str(raw.get("cancel") or "Отклонить"),
        )

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def _identity(mafile: MaFile | None) -> tuple[str, str, str]:
    if mafile is None:
        raise ConfirmationError("нет maFile для этого аккаунта")
    if not mafile.identity_secret:
        raise ConfirmationError("в maFile нет identity_secret — подтверждения недоступны")
    if not mafile.steam_id:
        raise ConfirmationError("в maFile нет steamid")
    return mafile.identity_secret, str(mafile.steam_id), mafile.device_id or make_device_id(mafile.steam_id)


def _params(mafile: MaFile, steam_time, tag: str, client: str) -> dict:
    secret, steam_id, device = _identity(mafile)
    key, moment = steam_time.confirmation_key(secret, tag)
    return {"p": device, "a": steam_id, "k": key, "t": moment, "m": client, "tag": tag}


def _payload(body: str) -> dict | None:
    try:
        data = json.loads(body)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


#: Steam кладёт настоящую причину отказа в заголовок x-eresult, а в теле
#: оставляет голое {"success": false}. Имена кодов — из перечисления EResult.
ERESULT = {
    "1": "OK",
    "2": "Fail — общий отказ",
    "5": "InvalidPassword — не приняты данные входа",
    "8": "InvalidParam — Steam не понял параметры запроса",
    "9": "FileNotFound — подтверждения или обмена уже нет",
    "10": "Busy — Steam занят, попробуй позже",
    "11": "InvalidState — операция сейчас невозможна (состояние аккаунта или обмена)",
    "15": "AccessDenied — аккаунту это действие запрещено",
    "16": "Timeout",
    "20": "ServiceUnavailable — служба Steam недоступна",
    "24": "InsufficientPrivilege — недостаточно прав (обмены аккаунту закрыты)",
    "25": "LimitExceeded — превышен предел",
    "26": "Revoked — отозвано",
    "27": "Expired — истекло",
    "28": "AlreadyRedeemed — уже выполнено",
    "29": "DuplicateRequest — такой запрос уже был",
    "42": "NoMatch — не найдено",
    "84": "RateLimitExceeded — Steam придерживает запросы",
    "88": "TwoFactorCodeMismatch — не сошёлся код аутентификатора (часы или секрет)",
    "101": "NeedCaptcha — Steam просит капчу",
    "108": "TooManyPending — слишком много ожидающих операций",
}


def eresult_of(response) -> str:
    """Код отказа из заголовков ответа. Пустая строка — Steam его не прислал."""
    headers = {}
    try:
        headers = {str(k).lower(): str(v) for k, v in dict(response.headers).items()}
    except Exception:  # noqa: BLE001 — заголовки не обязаны быть
        return ""
    code = headers.get("x-eresult", "").strip()
    message = headers.get("x-error_message", "").strip()
    if not code and not message:
        return ""
    known = ERESULT.get(code, f"код {code}" if code else "")
    return " | ".join(part for part in (known, message) if part)


#: Пауза между попытками: Steam считает частые обращения и начинает придерживать.
RETRY_PAUSE_S = 2.5


def _rate_limited(status: int, body: str) -> bool:
    return status == 429 or "too many requests" in body.lower()


def _explain(status: int, body: str) -> str:
    """HTML вместо JSON означает конкретные вещи — не заставляем гадать."""
    low = body.lower()
    if _rate_limited(status, body):
        return "Steam придерживает запросы с этого IP (429): подожди или смени прокси"
    if "steam guard mobile authenticator" in low and "add" in low:
        return "Steam не считает эту сессию мобильной: подтверждения доступны только мобильному входу"
    if "login" in low and ("sign in" in low or "steamcommunity.com/login" in low):
        return "в браузере нет активной сессии Steam — залогинься в профиле и повтори"
    if status >= 400:
        return f"Steam ответил {status}"
    return f"Steam ответил не JSON ({status}, {len(body)} символов)"


def _failure(payload: dict) -> str:
    for key in ("message", "detail", "error"):
        if payload.get(key):
            return str(payload[key])
    if payload.get("needauth"):
        return "Steam требует мобильный вход (needauth): cookies браузера ему не подходят"
    if set(payload) <= {"success"}:
        return (
            "Steam отказал без объяснения. Так он отвечает, когда придерживает запросы "
            "с этого IP (смени прокси и подожди), когда обмена уже нет или когда "
            "список подтверждений устарел"
        )
    return json.dumps(payload, ensure_ascii=False)[:200]


async def fetch(request, mafile: MaFile, steam_time, *, timeout_ms: int = 20000) -> list[Confirmation]:
    """Список ожидающих подтверждений. request — APIRequestContext открытого профиля."""
    last_error = ""
    for client in CLIENTS:
        response = await request.get(
            f"{BASE}/getlist",
            params=(listing := _params(mafile, steam_time, "conf", client)),
            headers=_headers(listing),
            timeout=timeout_ms,
        )
        body = await response.text()
        payload = _payload(body)
        if payload is None:
            last_error = _explain(response.status, body)
            continue
        if payload.get("success"):
            items = payload.get("conf") or payload.get("confirmations") or []
            return [Confirmation.from_json(item) for item in items if item.get("id")]
        last_error = _failure(payload)
    raise ConfirmationError(last_error or "Steam не ответил")


async def details(request, mafile: MaFile, steam_time, conf_id: str, *, timeout_ms: int = 20000) -> str:
    """Подробности подтверждения — то же, что показывает приложение по тапу.

    Нужны, когда операция отклонена без объяснения: подробности иногда прямо
    говорят, что обмена уже нет.
    """
    last = ""
    for client in CLIENTS:
        response = await request.get(
            f"{BASE}/details/{conf_id}",
            params=(detail := _params(mafile, steam_time, f"details{conf_id}", client)),
            headers=_headers(detail), timeout=timeout_ms,
        )
        body = await response.text()
        payload = _payload(body)
        if payload is None:
            last = _explain(response.status, body)
            continue
        if payload.get("success"):
            return str(payload.get("html") or "")
        last = _failure(payload)
    raise ConfirmationError(last or "Steam не отдал подробности")


def plans(accept: bool, count: int) -> list[tuple[str, str, str]]:
    """Способы отправить операцию: (адрес, клиент, тег), от обычного к запасным.

    Steam на отказ не объясняется, поэтому вместо гадания пробуем все рабочие
    сочетания, какие знают SDA, steampy и node-steamcommunity.
    """
    op_tag, _legacy = OP_TAGS[bool(accept)]
    # Запрос сверен с SDA и NebulaAuth — он правильный, поэтому перебирать
    # экзотику незачем: лишние попытки только злят Steam, который считает
    # обращения. Оставляем канонический способ и один запасной.
    if count > 1:
        return [("multiajaxop", "react", op_tag), ("multiajaxop", "android", op_tag)]
    return [("ajaxop", "react", op_tag), ("multiajaxop", "react", op_tag)]


async def respond(
    request,
    mafile: MaFile,
    steam_time,
    items: list[Confirmation],
    *,
    accept: bool,
    timeout_ms: int = 20000,
) -> dict:
    """Подтвердить или отклонить. Перебирает способы, пока Steam не согласится."""
    if not items:
        raise ConfirmationError("нечего подтверждать")
    op = "allow" if accept else "cancel"
    log = get_logger()

    last_error = ""
    tried = []
    for endpoint, client, tag in plans(accept, len(items)):
        params = _params(mafile, steam_time, tag, client)
        if endpoint == "ajaxop":
            single = dict(params, op=op, cid=items[0].id, ck=items[0].nonce)
            response = await request.get(
                f"{BASE}/ajaxop", params=single, headers=_headers(params), timeout=timeout_ms
            )
        else:
            fields = [(k, str(v)) for k, v in params.items()] + [("op", op)]
            for item in items:
                fields.append(("cid[]", item.id))
                fields.append(("ck[]", item.nonce))
            response = await request.post(
                f"{BASE}/multiajaxop",
                data=urlencode(fields),
                headers={**_headers(params),
                         "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
                timeout=timeout_ms,
            )

        body = await response.text()
        tried.append(f"{endpoint}/{client}/{tag}")
        payload = _payload(body)
        if payload is not None and payload.get("success"):
            log.info("Подтверждение прошло: %s/%s, тег %s", endpoint, client, tag)
            return {"done": len(items), "accept": accept}
        reason = eresult_of(response)
        if reason:
            last_error = f"Steam: {reason}"
        if _rate_limited(response.status, body) or reason.startswith("RateLimitExceeded"):
            raise RateLimited(
                "Steam придерживает запросы с этого IP (429). Подожди несколько минут "
                "или смени прокси аккаунта: перебирать способы сейчас бессмысленно"
            )

        if not reason:
            last_error = _explain(response.status, body) if payload is None else _failure(payload)
        # сырой ответ и код отказа: без них отказ Steam не отличить от нашей ошибки
        log.warning("Подтверждение не принято (%s/%s, тег %s): HTTP %s%s %s",
                    endpoint, client, tag, response.status,
                    f", x-eresult {reason}" if reason else " (x-eresult не прислан)",
                    " ".join(body.split())[:120])
        await asyncio.sleep(RETRY_PAUSE_S)      # частить нельзя: Steam считает обращения

    raise ConfirmationError(f"{last_error or 'Steam не ответил'} [перебрано: {', '.join(tried)}]")
