"""Мобильные подтверждения Steam — то же, что делает SDA.

Подтверждения живут на steamcommunity.com/mobileconf. Каждый запрос подписан
HMAC-ключом из identity_secret, а авторизация берётся из cookies уже открытого
профиля: отдельный мобильный логин мы не делаем. Если Steam для этой сессии
подтверждения не отдаёт, это видно по тексту ошибки, а не по пустому списку.
"""
from __future__ import annotations

import dataclasses
import json
from urllib.parse import urlencode

from .models import MaFile
from .steam_guard import device_id as make_device_id

BASE = "https://steamcommunity.com/mobileconf"

#: Заголовки мобильного клиента Steam: без них часть ответов приходит как HTML.
HEADERS = {
    "X-Requested-With": "com.valvesoftware.android.steam.community",
    "Referer": f"{BASE}/conf",
}

#: Сначала пробуем современный клиент (react), потом старый (android).
CLIENTS = ("react", "android")


class ConfirmationError(RuntimeError):
    """Steam не отдал список или отказал в операции — причина в тексте."""


@dataclasses.dataclass(slots=True)
class Confirmation:
    id: str
    nonce: str
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


def _explain(status: int, body: str) -> str:
    """HTML вместо JSON означает конкретные вещи — не заставляем гадать."""
    low = body.lower()
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
    return json.dumps(payload, ensure_ascii=False)[:200]


async def fetch(request, mafile: MaFile, steam_time, *, timeout_ms: int = 20000) -> list[Confirmation]:
    """Список ожидающих подтверждений. request — APIRequestContext открытого профиля."""
    last_error = ""
    for client in CLIENTS:
        response = await request.get(
            f"{BASE}/getlist",
            params=_params(mafile, steam_time, "conf", client),
            headers=HEADERS,
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


async def respond(
    request,
    mafile: MaFile,
    steam_time,
    items: list[Confirmation],
    *,
    accept: bool,
    timeout_ms: int = 20000,
) -> dict:
    """Подтвердить или отклонить. Одно — через ajaxop, пачку — через multiajaxop."""
    if not items:
        raise ConfirmationError("нечего подтверждать")
    tag = "accept" if accept else "reject"
    op = "allow" if accept else "cancel"

    last_error = ""
    for client in CLIENTS:
        params = _params(mafile, steam_time, tag, client)
        if len(items) == 1:
            single = dict(params, op=op, cid=items[0].id, ck=items[0].nonce)
            response = await request.get(f"{BASE}/ajaxop", params=single, headers=HEADERS, timeout=timeout_ms)
        else:
            fields = [(k, str(v)) for k, v in params.items()] + [("op", op)]
            for item in items:
                fields.append(("cid[]", item.id))
                fields.append(("ck[]", item.nonce))
            response = await request.post(
                f"{BASE}/multiajaxop",
                data=urlencode(fields),
                headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
                timeout=timeout_ms,
            )
        body = await response.text()
        payload = _payload(body)
        if payload is None:
            last_error = _explain(response.status, body)
            continue
        if payload.get("success"):
            return {"done": len(items), "accept": accept}
        last_error = _failure(payload)
    raise ConfirmationError(last_error or "Steam не ответил")
