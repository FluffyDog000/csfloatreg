"""Автоподтверждение: бот сам принимает подтверждения отмеченных аккаунтов.

Зачем: продажа на CSFloat превращается в обмен Steam, который надо подтвердить
мобильно, иначе он висит. Руками по полусотне аккаунтов это не делается.

Как устроено. Подтверждения читаются из cookies профиля, поэтому дёшево
обходятся только открытые профили — их бот проверяет часто. Закрытые профили
приходится поднимать (браузер на аккаунт, свой прокси), и это долго, поэтому
их обход идёт редко и в несколько потоков. Аккаунт участвует, только если на
нём включена галочка: случайных подтверждений быть не должно.
"""
from __future__ import annotations

import asyncio
import time

from .confirmations import ConfirmationError
from .events import hub as default_hub
from .logging_setup import get_logger

FIELD = "auto_confirm"


class AutoConfirm:
    def __init__(self, manager, *, hub=None, busy=None):
        self.manager = manager
        self.cfg = manager.cfg
        self.hub = hub or default_hub
        self.busy = busy or (lambda: False)     # не мешаем прогону и рассылке
        self.log = get_logger()
        self._task: asyncio.Task | None = None
        self._last_sweep = 0.0
        self.accepted: dict[str, dict] = {}     # что и когда подтвердили

    # ── настройки ────────────────────────────────────────────
    @property
    def poll_s(self) -> float:
        return float(self.cfg.get("confirm.poll_s", 45))

    @property
    def sweep_s(self) -> float:
        return float(self.cfg.get("confirm.sweep_s", 900))

    @property
    def workers(self) -> int:
        return max(1, min(int(self.cfg.get("confirm.workers", 2)), 8))

    def logins(self) -> list[str]:
        return [
            login for login in self.manager.accounts
            if self.manager.bindings.entry(login).get(FIELD)
        ]

    def enabled(self, login: str) -> bool:
        return bool(self.manager.bindings.entry(login).get(FIELD))

    def switch(self, logins: list[str], on: bool) -> list[str]:
        changed = [login for login in logins if login in self.manager.accounts]
        for login in changed:
            self.manager.bindings.set_field(login, FIELD, bool(on))
        self.log.info("Автоподтверждение %s: %s", "включено" if on else "выключено", ", ".join(changed) or "—")
        return changed

    # ── жизненный цикл ───────────────────────────────────────
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 — гасим молча
            pass

    async def _loop(self) -> None:
        self.log.info("Автоподтверждение следит за аккаунтами (опрос %.0f c, полный обход %.0f c)",
                      self.poll_s, self.sweep_s)
        while True:
            await asyncio.sleep(self.poll_s)
            if self.busy():
                continue
            full = time.monotonic() - self._last_sweep >= self.sweep_s
            try:
                await self.sweep(include_closed=full)
            except Exception:  # noqa: BLE001 — слежка не имеет права умирать
                self.log.exception("Автоподтверждение: непредвиденная ошибка обхода")
            if full:
                self._last_sweep = time.monotonic()

    # ── обход ────────────────────────────────────────────────
    async def sweep(self, *, include_closed: bool = False) -> dict:
        """Проверяет отмеченные аккаунты. Закрытые профили — только при include_closed."""
        # аккаунту с токенами в maFile браузер не нужен вовсе — его проверяем всегда
        logins = [
            login for login in self.logins()
            if include_closed or login in self.manager.sessions
            or self.manager.can_confirm_offline(login)
        ]
        if not logins:
            return {"checked": 0, "accepted": 0}

        gate = asyncio.Semaphore(self.workers)
        done = {"checked": 0, "accepted": 0, "failed": 0}

        async def handle(login: str) -> None:
            async with gate:
                try:
                    done["accepted"] += await self._one(login)
                    done["checked"] += 1
                except ConfirmationError as exc:
                    done["failed"] += 1
                    self.log.warning("[%s] автоподтверждение: %s", login, exc)
                except Exception as exc:  # noqa: BLE001 — один аккаунт не ломает обход
                    done["failed"] += 1
                    self.log.warning("[%s] автоподтверждение не удалось: %s", login, exc)

        await asyncio.gather(*(handle(login) for login in logins))
        if done["accepted"]:
            self.log.info("Автоподтверждение: принято %d на %d аккаунт(ах)", done["accepted"], done["checked"])
        return done

    async def _one(self, login: str) -> int:
        """Один аккаунт: по возможности без браузера, иначе открыть и закрыть за собой."""
        offline = self.manager.can_confirm_offline(login)
        opened_here = not offline and login not in self.manager.sessions
        if opened_here:
            await self.manager.open_profile(login, headful=bool(self.cfg.get("confirm.headful", False)))
        try:
            items = await self.manager.confirmations(login)
            if not items:
                return 0
            ids = [item["id"] for item in items]
            await self.manager.respond_confirmation(login, ids, accept=True)
            self.accepted[login] = {
                "count": len(ids), "at": time.strftime("%H:%M:%S"),
                "what": "; ".join(i.get("headline") or "" for i in items).strip("; ")[:120],
            }
            self.log.info("[%s] автоподтверждение: принято %d", login, len(ids))
            self.hub.publish("auto-confirm", login=login, **self.accepted[login])
            return len(ids)
        finally:
            if opened_here:
                await self.manager.close_profile(login)
