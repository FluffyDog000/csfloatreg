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
    TRADE_HOLD_DAYS,
    SessionExpired,
    TradeError,
    accept_offer,
    escrow_days,
    fetch_inventory,
    item_counts,
    locked_until,
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

    async def _ensure_session(self, login: str, *, headful, force: bool = False) -> bool:
        """Steam должен помнить аккаунт: без этого ни отправить, ни принять.

        Не помнит — бот входит сам. Не смог войти — это ошибка именно этого
        аккаунта, а не повод ронять всю рассылку.
        """
        try:
            result = await self.manager.ensure_steam_login(login, headful=headful, force=force)
        except Exception as exc:  # noqa: BLE001 — причину покажем в статусе аккаунта
            raise SessionExpired(f"не удалось войти в Steam под {login}: {exc}") from None
        return bool(result.get("relogin"))

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
        await self._ensure_session(sender, headful=headful)
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

            await self._ensure_session(sender, headful=headful)
            context = await self.manager.request_for(sender, headful=headful)
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
                        sender=sender, mafile=mafile, login=login,
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

    async def _one(self, *, sender, mafile, login, items, item, per_account, used, message, headful) -> dict:
        trade_url = self.manager.bindings.entry(login).get("trade_url")
        if not trade_url:
            raise TradeError("у аккаунта нет трейд-ссылки")
        partner = parse_trade_url(trade_url)

        context = await self.manager.sessions[sender].context("main")
        request = context.request

        chosen = pick_items(items, item, per_account, exclude=used)
        days = await escrow_days(request, partner)
        if days:
            self.log.warning("[%s] Steam обещает заморозку на %d дн.", login, days)

        try:
            offer = await send_offer(
                request, sessionid=await session_id(context), partner=partner,
                items=chosen, message=message,
            )
        except SessionExpired as exc:
            # cookies отправителя протухли посреди рассылки — входим и повторяем,
            # уже без проверки страницы: состояние сессии мы только что выяснили сами
            self.log.warning("[%s] %s — вхожу в Steam заново", login, exc)
            await self._ensure_session(sender, headful=headful, force=True)
            context = await self.manager.sessions[sender].context("main")
            request = context.request
            offer = await send_offer(
                request, sessionid=await session_id(context), partner=partner,
                items=chosen, message=message, precheck=False,
            )
        used.update(offer["items"])
        self.log.info("[%s] обмен создан: %s", login, offer["offer_id"])
        self._record(login, status="sent", offer=offer["offer_id"], items=per_account,
                     note=f"заморозка {days} дн." if days else "")

        if offer["needs_confirmation"]:
            await self._confirm(sender, mafile, offer["offer_id"])
            self._record(login, status="confirmed", offer=offer["offer_id"], items=per_account)

        result = await self._accept(
            login, offer["offer_id"], mafile.steam_id,
            headful=headful, partner=partner, item=item, escrow=days,
        )
        status = "escrow" if (days or result.get("escrow")) else "accepted"
        self.log.info("[%s] обмен %s: %s", login, offer["offer_id"], status)
        return self._record(
            login, status=status, offer=offer["offer_id"], items=per_account,
            note=f"заморозка {days} дн." if days else "",
            **result.get("lock", {}),
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

    async def _accept(self, login: str, offer_id: str, sender_steam_id: str, *,
                      headful, partner, item: str, escrow) -> dict:
        """Приём на стороне получателя: его профиль, его прокси, его cookies."""
        opened_here = login not in self.manager.sessions
        try:
            try:
                # заодно будит cookie sessionid: она живёт до закрытия браузера,
                # и в свежем профиле её нет, пока не открыта страница Steam
                await self._ensure_session(login, headful=headful)
                result = await self._accept_once(login, offer_id, sender_steam_id, headful=headful)
            except SessionExpired as exc:
                self.log.warning("[%s] %s — вхожу в Steam заново", login, exc)
                await self._ensure_session(login, headful=headful, force=True)
                result = await self._accept_once(login, offer_id, sender_steam_id, headful=headful)

            held = int(escrow or 0) or (15 if result.get("escrow") else 0)
            # пока профиль получателя открыт — самое время спросить про трейд-бан
            result["lock"] = await self._trade_lock(login, partner, item, held, headful=headful)
            return result
        finally:
            if opened_here:
                await self.manager.close_profile(login)

    async def _trade_lock(self, login: str, partner, item: str, held_days: int, *,
                          headful, attempts: int = 3) -> dict:
        """Когда предмет выйдет из трейд-бана.

        Дату называет сам Steam — она лежит в инвентаре получателя и видна
        только ему. Инвентарь после обмена обновляется не мгновенно, поэтому
        пробуем несколько раз, а если Steam так и промолчал, считаем сами:
        семь дней после обмена — правило CS2.
        """
        due = time.time() + (TRADE_HOLD_DAYS + held_days) * 86400
        guess = {
            "unlock_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(due)),
            "unlock_text": "", "unlock_source": "расчёт",
        }
        if held_days:
            return guess                      # предмет ещё в заморозке, в инвентаре его нет

        for attempt in range(1, attempts + 1):
            try:
                context = await self.manager.request_for(login, headful=headful)
                items = await fetch_inventory(context.request, partner.steam_id)
            except Exception as exc:  # noqa: BLE001 — дата не повод считать приём неудачным
                self.log.debug("[%s] инвентарь для даты разблокировки не прочитался: %s", login, exc)
                return guess
            lock = locked_until(items, item)
            if lock:
                self.log.info("[%s] предмет заперт до %s", login, lock["unlock_at"] or lock["unlock_text"])
                return {**lock, "unlock_source": "Steam"}
            if attempt < attempts:
                await asyncio.sleep(2.5)

        self.log.debug("[%s] Steam не назвал дату разблокировки — считаю сам", login)
        return guess

    async def _accept_once(self, login: str, offer_id: str, sender_steam_id: str, *, headful) -> dict:
        context = await self.manager.request_for(login, headful=headful)
        return await accept_offer(
            context.request, offer_id,
            sessionid=await session_id(context), partner_steam_id=sender_steam_id,
        )
