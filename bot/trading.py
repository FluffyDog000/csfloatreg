"""Обмены Steam: инвентарь, отправка предмета по трейд-ссылке, приём обмена.

Всё делается запросами из сессии открытого профиля — теми же cookies, что и у
живого человека за этим браузером. Официального API для создания обмена нет:
Steam принимает их только на steamcommunity.com, поэтому работаем с ним.

Разделение сторон важно помнить: отдающий подтверждает обмен мобильно (это
делает bot/confirmations.py), принимающий — просто жмёт «Принять», и никакого
подтверждения с его стороны не требуется, раз он ничего не отдаёт.
"""
from __future__ import annotations

import dataclasses
import json
import re
from urllib.parse import parse_qs, urlparse

BASE = "https://steamcommunity.com"

#: Steam хранит id аккаунта в двух видах; разница — эта константа.
STEAMID64_BASE = 76561197960265728

HEADERS = {
    "Origin": BASE,
    "X-Requested-With": "XMLHttpRequest",
}


class TradeError(RuntimeError):
    """Steam отказал в операции — причина в тексте."""


class SessionExpired(TradeError):
    """Cookies профиля протухли: Steam отвечает страницей логина."""


def steamid64(account_id: int | str) -> str:
    return str(int(account_id) + STEAMID64_BASE)


def account_id(steam_id: int | str) -> int:
    return int(steam_id) - STEAMID64_BASE


@dataclasses.dataclass(slots=True)
class TradePartner:
    """Кому шлём: разобранная трейд-ссылка."""

    steam_id: str
    token: str
    account_id: int

    @property
    def referer(self) -> str:
        return f"{BASE}/tradeoffer/new/?partner={self.account_id}&token={self.token}"


def parse_trade_url(url: str) -> TradePartner:
    """`…/tradeoffer/new/?partner=428817&token=x7Kd2Qa1` → steamid и токен."""
    query = parse_qs(urlparse(str(url).strip()).query)
    partner = (query.get("partner") or [""])[0]
    token = (query.get("token") or [""])[0]
    if not partner.isdigit() or not token:
        raise TradeError(f"трейд-ссылка не разбирается: {url[:80]}")
    return TradePartner(steam_id=steamid64(partner), token=token, account_id=int(partner))


@dataclasses.dataclass(slots=True)
class Item:
    asset_id: str
    class_id: str
    instance_id: str
    name: str
    app_id: int
    context_id: str
    tradable: bool

    def as_asset(self, amount: int = 1) -> dict:
        return {
            "appid": self.app_id,
            "contextid": str(self.context_id),
            "amount": amount,
            "assetid": self.asset_id,
        }


def _looks_like_login(body: str) -> bool:
    low = body.lower()
    return "steamcommunity.com/login" in low or ("login" in low and "<html" in low[:200])


def _json(body: str, what: str, *, status: int | None = None) -> dict:
    """Ответ Steam словарём. Всё остальное — ошибка с текстом, а не падение.

    Steam умеет отвечать литералом null: это валидный JSON, но не словарь, и
    обращение к .get на нём роняло рассылку трейсбеком вместо внятной причины.
    """
    try:
        payload = json.loads(body)
    except ValueError:
        if _looks_like_login(body):
            raise SessionExpired(f"{what}: Steam просит войти заново — сессия профиля истекла") from None
        raise TradeError(f"{what}: Steam ответил не JSON ({' '.join(body.split())[:160]})") from None

    if isinstance(payload, dict):
        return payload
    where = f" HTTP {status}," if status is not None else ""
    raise TradeError(
        f"{what}: Steam ответил «{json.dumps(payload)[:60]}» вместо данных ({where.strip(',')} "
        f"тело {len(body)} символов)"
    )


async def session_id(context) -> str:
    """sessionid из cookies профиля — Steam требует его в каждой форме."""
    for cookie in await context.cookies(BASE):
        if cookie.get("name") == "sessionid":
            return str(cookie.get("value") or "")
    raise SessionExpired("в профиле нет cookie sessionid — Steam не залогинен")


async def fetch_inventory(
    request, steam_id: str, *, app_id: int = 730, context_id: str = "2", timeout_ms: int = 30000
) -> list[Item]:
    """Инвентарь: только предметы, которые можно передать."""
    url = f"{BASE}/inventory/{steam_id}/{app_id}/{context_id}?l=english&count=2000"
    response = await request.get(url, headers=HEADERS, timeout=timeout_ms)
    payload = _json(await response.text(), "инвентарь", status=response.status)
    if not payload or payload.get("success") in (False, 0):
        raise TradeError(f"инвентарь недоступен: {str(payload)[:160]}")

    names = {
        (str(d.get("classid")), str(d.get("instanceid"))): d
        for d in payload.get("descriptions") or []
    }
    items: list[Item] = []
    for asset in payload.get("assets") or []:
        key = (str(asset.get("classid")), str(asset.get("instanceid")))
        description = names.get(key, {})
        items.append(
            Item(
                asset_id=str(asset.get("assetid")),
                class_id=key[0],
                instance_id=key[1],
                name=str(description.get("market_hash_name") or description.get("name") or ""),
                app_id=int(asset.get("appid") or app_id),
                context_id=str(asset.get("contextid") or context_id),
                tradable=bool(description.get("tradable", 1)),
            )
        )
    return items


def pick_items(items: list[Item], name: str, count: int, *, exclude: set[str] | None = None) -> list[Item]:
    """Выбирает count штук предмета по имени, пропуская уже занятые."""
    exclude = exclude or set()
    wanted = name.strip().lower()
    chosen = [
        item for item in items
        if item.tradable and item.asset_id not in exclude and item.name.strip().lower() == wanted
    ]
    if len(chosen) < count:
        raise TradeError(
            f"в инвентаре не хватает предметов «{name}»: нужно {count}, свободно {len(chosen)}"
        )
    return chosen[:count]


def item_counts(items: list[Item]) -> dict[str, int]:
    """Сколько чего лежит в инвентаре — для подсказки в интерфейсе."""
    counts: dict[str, int] = {}
    for item in items:
        if item.tradable and item.name:
            counts[item.name] = counts.get(item.name, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


async def escrow_days(request, partner: TradePartner, *, timeout_ms: int = 20000) -> int | None:
    """Сколько дней заморозки будет у получателя. None — Steam не ответил."""
    url = f"{BASE}/tradeoffer/new/getuserdetails/?partner={partner.account_id}&token={partner.token}"
    try:
        response = await request.get(url, headers={**HEADERS, "Referer": partner.referer}, timeout=timeout_ms)
        payload = _json(await response.text(), "проверка заморозки", status=response.status)
    except TradeError:
        return None
    them = payload.get("them") or {}
    value = them.get("escrow_days_left")
    return int(value) if isinstance(value, (int, str)) and str(value).isdigit() else None


async def send_offer(
    request,
    *,
    sessionid: str,
    partner: TradePartner,
    items: list[Item],
    message: str = "",
    timeout_ms: int = 30000,
) -> dict:
    """Создаёт обмен «отдаю предметы, не прошу ничего». Возвращает id обмена."""
    if not items:
        raise TradeError("нечего отправлять: список предметов пуст")

    offer = {
        "newversion": True,
        "version": len(items) + 1,
        "me": {"assets": [item.as_asset() for item in items], "currency": [], "ready": False},
        "them": {"assets": [], "currency": [], "ready": False},
    }
    form = {
        "sessionid": sessionid,
        "serverid": "1",
        "partner": partner.steam_id,
        "tradeoffermessage": message,
        "json_tradeoffer": json.dumps(offer, separators=(",", ":")),
        "captcha": "",
        "trade_offer_create_params": json.dumps({"trade_offer_access_token": partner.token}),
    }
    # живой человек сначала открывает страницу обмена, и Steam это учитывает:
    # без захода на неё он умеет отвечать пустым null вместо ответа
    blocker = await trade_page_problem(request, partner, timeout_ms=timeout_ms)
    if blocker:
        raise TradeError(f"страница обмена сообщает: {blocker}")

    response = await request.post(
        f"{BASE}/tradeoffer/new/send",
        form=form,
        headers={**HEADERS, "Referer": partner.referer},
        timeout=timeout_ms,
    )
    body = await response.text()
    try:
        payload = _json(body, "отправка обмена", status=response.status)
    except SessionExpired:
        raise                       # протухшую сессию не маскируем догадками
    except TradeError as exc:
        hint = await trade_page_problem(request, partner, timeout_ms=timeout_ms)
        if not hint:
            hint = (
                "обычно так отвечают, когда аккаунту закрыты обмены: торговый бан, "
                "недавняя смена пароля, мобильный аутентификатор младше 7 дней — "
                "или трейд-ссылка чужая"
            )
        raise TradeError(f"{exc}. {hint}") from None
    if payload.get("strError"):
        raise TradeError(f"Steam отказал: {payload['strError']}")
    offer_id = str(payload.get("tradeofferid") or "")
    if not offer_id:
        raise TradeError(f"Steam не вернул номер обмена: {str(payload)[:160]}")
    return {
        "offer_id": offer_id,
        "needs_confirmation": bool(payload.get("needs_mobile_confirmation")),
        "items": [item.asset_id for item in items],
    }


async def accept_offer(
    request, offer_id: str, *, sessionid: str, partner_steam_id: str, timeout_ms: int = 30000
) -> dict:
    """Принимает входящий обмен. Получатель ничего не отдаёт — подтверждение не нужно."""
    response = await request.post(
        f"{BASE}/tradeoffer/{offer_id}/accept",
        form={
            "sessionid": sessionid,
            "serverid": "1",
            "tradeofferid": str(offer_id),
            "partner": str(partner_steam_id),
            "captcha": "",
        },
        headers={**HEADERS, "Referer": f"{BASE}/tradeoffer/{offer_id}/"},
        timeout=timeout_ms,
    )
    payload = _json(await response.text(), "приём обмена", status=response.status)
    if payload.get("strError"):
        raise TradeError(f"Steam отказал в приёме: {payload['strError']}")
    return {
        "trade_id": str(payload.get("tradeid") or ""),
        # Steam ставит этот флаг, когда предметы уходят в заморозку
        "escrow": bool(payload.get("needs_mobile_confirmation") or payload.get("needs_email_confirmation")),
    }


#: Фразы, которыми страница обмена объясняет, почему обмен невозможен.
_TRADE_BLOCKERS = (
    "cannot trade",
    "is not available to trade",
    "unable to trade",
    "trade ban",
    "trade URL is no longer valid",
    "profile is private",
    "they have a trade ban",
    "you have a trade ban",
    "recently changed your password",
    "Steam Guard",
)


async def trade_page_problem(request, partner: TradePartner, *, timeout_ms: int = 20000) -> str:
    """Открывает страницу обмена и возвращает причину отказа, если она там есть.

    Пустая строка — препятствий не видно. Ошибки чтения не мешают отправке:
    это подсказка, а не проверка.
    """
    try:
        response = await request.get(partner.referer, headers=HEADERS, timeout=timeout_ms)
        text = await response.text()
    except Exception:  # noqa: BLE001 — подсказка не обязана работать
        return ""
    if _looks_like_login(text):
        return "Steam просит войти заново — сессия профиля истекла"
    flat = " ".join(text.split())
    for marker in _TRADE_BLOCKERS:
        index = flat.lower().find(marker.lower())
        if index >= 0:
            return flat[max(0, index - 90) : index + 110].strip()
    return ""


_OFFER_IN_TEXT = re.compile(r"tradeofferid[_\"':= ]+(\d{6,})")


def offer_ids_in(text: str) -> list[str]:
    """Номера обменов, встречающиеся в HTML страницы обменов."""
    return sorted(set(_OFFER_IN_TEXT.findall(text or "")))
