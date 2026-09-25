"""Рассылка предметов: один аккаунт раздаёт, остальные принимают.

Порядок на каждого получателя ровно такой, какого требует Steam:
    1) отправитель создаёт обмен по трейд-ссылке получателя,
    2) отправитель подтверждает его мобильно (он отдаёт предметы),
    3) получатель жмёт «Принять» — ему подтверждать нечего, он ничего не отдаёт.

Результат каждого шага ложится в data/bindings.json, поэтому повторный запуск
не шлёт дубликаты тем, у кого обмен уже принят.
"""
from __future__ import annotations

import asyncio
import random
import time

from .confirmations import ConfirmationError
from .confirmations import fetch as fetch_confirmations
from .confirmations import respond as respond_confirmations
from .events import hub as default_hub
from .logging_setup import get_logger
from .trading import (
    SessionExpired,
    TradeError,
    accept_offer,
    escrow_days,
    fetch_inventory,
    item_counts,
    parse_trade_url,
    pick_items,
    send_offer,
    session_id,
)

#: Статусы, при которых аккаунт в повторном прогоне пропускается.
DONE_STATUSES = ("accepted", "escrow")


class Delivery:
    """Одна рассылка: отправитель, предмет, количество, список получателей."""

    def __init__(self, manager, *, hub=None):
        self.manager = manager
        self.hub = hub or default_hub
        self.log = get_logger()
        self._stop = asyncio.Event()
        self.running = False
        self.results: dict[str, dict] = {}

    def stop(self) -> None:
        self.log.warning("Рассылка: получен сигнал остановки")
        self._stop.set()

    # ── вспомогательное ──────────────────────────────────────
    def _record(self, login: str, **fields) -> dict:
        row = {**self.results.get(login, {"login": login}), **fields, "at": time.strftime("%H:%M:%S")}
        self.results[login] = row
        self.manager.bindings.set_field(login, "delivery", {k: v for k, v in row.items() if k != "login"})
        self.hub.publish("delivery", **row)
        return row

    def previous(self, login: str) -> dict:
        return self.manager.bindings.entry(login).get("delivery") or {}

    def targets_with_links(self) -> list[str]:
        return [
            login for login in self.manager.accounts
            if self.manager.bindings.entry(login).get("trade_url")
        ]

    # ── инвентарь отправителя ────────────────────────────────
    async def inventory(self, sender: str, *, headful: bool | None = False) -> dict:
        mafile = self.manager.mafiles.get(sender.lower())
        if mafile is None or not mafile.steam_id:
            raise TradeError(f"для {sender} нет maFile со steamid — неоткуда взять инвентарь")
        context = await self.manager.request_for(sender, headful=headful)
        items = await fetch_inventory(context.request, mafile.steam_id)
        counts = item_counts(items)
        self.log.info("Инвентарь %s: видов предметов %d, всего к передаче %d",
                      sender, len(counts), sum(counts.values()))
        return {"steam_id": mafile.steam_id, "counts": counts, "total": sum(counts.values())}

    # ── сама рассылка ────────────────────────────────────────
    async def run(
        self,
        *,
        sender: str,
        item: str,
        per_account: int,
        targets: list[str],
        message: str = "",
        headful: bool | None = False,
        pause: tuple[float, float] = (4.0, 9.0),
        resume: bool = True,
    ) -> dict:
        if self.running:
            raise RuntimeError("рассылка уже идёт")
        if sender in targets:
            raise ValueError("отправитель не может быть получателем")

        self.running = True
        self._stop.clear()
        self.results = {}
        started = time.monotonic()
        used: set[str] = set()
        sent = accepted = skipped = failed = 0

        try:
            mafile = self.manager.mafiles.get(sender.lower())
            if mafile is None or not mafile.shared_secret:
                raise TradeError(f"для отправителя {sender} нет maFile")
            if not mafile.identity_secret:
                raise TradeError(
                    f"в maFile отправителя {sender} нет identity_secret: "
                    "бот не сможет подтвердить обмены, и они повиснут"
                )

            context = await self.manager.request_for(sender, headful=headful)
            sessionid = await session_id(context)
            items = await fetch_inventory(context.request, mafile.steam_id)
            available = item_counts(items).get(item, 0)
            need = per_account * len(targets)
            self.log.info(
                "Рассылка: %s -> %d аккаунт(ов), %d × «%s» каждому (в инвентаре %d)",
                sender, len(targets), per_account, item, available,
            )
            self.hub.publish("delivery-run", state="started", total=len(targets), available=available)
            if available < need:
                raise TradeError(
                    f"предметов «{item}» не хватит: нужно {need}, свободно {available}"
                )

            for login in targets:
                if self._stop.is_set():
                    self.log.warning("Рассылка остановлена, осталось аккаунтов: %d",
                                     len(targets) - sent - skipped - failed)
                    break

                previous = self.previous(login)
                if resume and previous.get("status") in DONE_STATUSES:
                    skipped += 1
                    self._record(login, status=previous["status"], note="уже сделано ранее")
                    continue

                try:
                    outcome = await self._one(
                        sender=sender, sessionid=sessionid, mafile=mafile, login=login,
                        items=items, item=item, per_account=per_account, used=used,
                        message=message, headful=headful,
                    )
                except (TradeError, ConfirmationError, SessionExpired) as exc:
                    failed += 1
                    self.log.error("[%s] рассылка: %s", login, exc)
                    self._record(login, status="error", note=str(exc)[:200])
                except Exception as exc:  # noqa: BLE001 — одна беда не должна валить всю рассылку
                    failed += 1
                    self.log.exception("[%s] непредвиденная ошибка рассылки", login)
                    self._record(login, status="error", note=f"{type(exc).__name__}: {exc}"[:200])
                else:
                    sent += 1
                    accepted += 1 if outcome.get("status") in DONE_STATUSES else 0

                await asyncio.sleep(random.uniform(*pause))

            summary = {
                "sent": sent, "accepted": accepted, "skipped": skipped, "failed": failed,
                "elapsed": round(time.monotonic() - started, 1),
            }
            self.log.info("Рассылка завершена: %s", summary)
            self.hub.publish("delivery-run", state="finished", **summary)
            return summary
        finally:
            self.running = False

    async def _one(self, *, sender, sessionid, mafile, login, items, item, per_account, used, message, headful) -> dict:
        trade_url = self.manager.bindings.entry(login).get("trade_url")
        if not trade_url:
            raise TradeError("у аккаунта нет трейд-ссылки")
        partner = parse_trade_url(trade_url)

        context = self.manager.sessions[sender]
        request = (await context.context("main")).request

        chosen = pick_items(items, item, per_account, exclude=used)
        days = await escrow_days(request, partner)
        if days:
            self.log.warning("[%s] Steam обещает заморозку на %d дн.", login, days)

        offer = await send_offer(
            request, sessionid=sessionid, partner=partner, items=chosen, message=message
        )
        used.update(offer["items"])
        self.log.info("[%s] обмен создан: %s", login, offer["offer_id"])
        self._record(login, status="sent", offer=offer["offer_id"], items=per_account,
                     note=f"заморозка {days} дн." if days else "")

        if offer["needs_confirmation"]:
            await self._confirm(sender, mafile, offer["offer_id"])
            self._record(login, status="confirmed", offer=offer["offer_id"], items=per_account)

        result = await self._accept(login, offer["offer_id"], mafile.steam_id, headful=headful)
        status = "escrow" if (days or result.get("escrow")) else "accepted"
        self.log.info("[%s] обмен %s: %s", login, offer["offer_id"], status)
        return self._record(
            login, status=status, offer=offer["offer_id"], items=per_account,
            note=f"заморозка {days} дн." if days else "",
        )

    async def _confirm(self, sender: str, mafile, offer_id: str, *, attempts: int = 6) -> None:
        """Подтверждение появляется не мгновенно — ждём именно своё."""
        request = (await self.manager.sessions[sender].context("main")).request
        for attempt in range(1, attempts + 1):
            pending = await fetch_confirmations(request, mafile, self.manager.steam_time)
            mine = [c for c in pending if c.creator_id == str(offer_id)]
            if mine:
                await respond_confirmations(request, mafile, self.manager.steam_time, mine, accept=True)
                self.log.info("Обмен %s подтверждён мобильно", offer_id)
                return
            self.log.debug("Подтверждение для %s ещё не появилось (попытка %d)", offer_id, attempt)
            await asyncio.sleep(2.5)
        raise ConfirmationError(f"подтверждение для обмена {offer_id} так и не появилось")

    async def _accept(self, login: str, offer_id: str, sender_steam_id: str, *, headful) -> dict:
        """Приём на стороне получателя: его профиль, его прокси, его cookies."""
        opened_here = login not in self.manager.sessions
        context = await self.manager.request_for(login, headful=headful)
        try:
            sessionid = await session_id(context)
            return await accept_offer(
                context.request, offer_id, sessionid=sessionid, partner_steam_id=sender_steam_id
            )
        finally:
            if opened_here:
                await self.manager.close_profile(login)
