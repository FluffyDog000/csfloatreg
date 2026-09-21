"""Очередь аккаунтов: параллельность, ретраи, статусы."""
from __future__ import annotations

import asyncio
import random
import time

from .browser import BrowserSession
from .captcha import build_solver
from .context import AccountContext
from .debug import Debugger
from .errors import BotError, MaFileMissing, RetryableError
from .events import hub as default_hub
from .logging_setup import get_logger
from .models import Bundle, Status
from .modules.base import build_modules
from .steam_guard import SteamTime
from .storage import ArtifactStore, ResultsStore, StateStore


class Runner:
    def __init__(self, cfg, selectors, bundles: list[Bundle], *, hub=None):
        self.cfg = cfg
        self.selectors = selectors
        self.bundles = bundles
        self.hub = hub or default_hub
        self.log = get_logger()

        cfg.ensure_dirs()
        self.results = ResultsStore(cfg.path_for("results"))
        self.state = StateStore(cfg.path_for("state"), cfg.path_for("profiles"))
        self.artifacts = ArtifactStore(cfg.path_for("errors"), cfg.path_for("debug_dumps"))
        self.solver = build_solver(cfg)
        self.steam_time = SteamTime(
            cfg.get("steam.time_sync_url"), enabled=bool(cfg.get("steam.time_sync", True))
        )
        self.modules = build_modules(cfg.get("run.modules") or ["registration"])
        self.debug = bool(cfg.get("run.debug", False))

        self._stop = asyncio.Event()
        self._consecutive_failures = 0
        self.stats: dict[str, int] = {}

    # ── управление ───────────────────────────────────────────
    def stop(self) -> None:
        self.log.warning("Получен сигнал остановки — новые аккаунты не стартуют")
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    # ── прогон ───────────────────────────────────────────────
    async def run(self, *, only: str | None = None, limit: int | None = None) -> dict:
        queue = self._select(only, limit)
        if not queue:
            self.log.warning("Нечего делать: очередь пуста")
            return {"total": 0}

        threads = max(1, int(self.cfg.get("run.threads", 3)))
        self.log.info(
            "Старт: %d аккаунт(ов), потоков %d, модули %s",
            len(queue), threads, [m.name for m in self.modules],
        )
        self.hub.publish("run", state="started", total=len(queue), threads=threads)
        await self.steam_time.sync(self.log)

        semaphore = asyncio.Semaphore(threads)
        started = time.monotonic()
        tasks = [asyncio.create_task(self._guarded(bundle, semaphore)) for bundle in queue]
        await asyncio.gather(*tasks, return_exceptions=True)

        self.stats = self.results.summary()
        elapsed = time.monotonic() - started
        self.log.info("Готово за %.0f c. Итог: %s", elapsed, self.stats or "—")
        self.hub.publish("run", state="finished", elapsed=elapsed, stats=self.stats)
        return {"total": len(queue), "elapsed": elapsed, "stats": self.stats}

    def _select(self, only: str | None, limit: int | None) -> list[Bundle]:
        skip = set(self.cfg.get("run.skip_statuses") or ["done"])
        queue: list[Bundle] = []
        for bundle in self.bundles:
            login = bundle.account.login
            if only and login.lower() != only.lower():
                continue
            pending = [
                module.name
                for module in self.modules
                if self.results.get(login, module.name).status not in skip
            ]
            if not pending:
                self.log.debug("[%s] пропуск: все модули уже выполнены", login)
                continue
            queue.append(bundle)
            if limit and len(queue) >= limit:
                break
        return queue

    async def _guarded(self, bundle: Bundle, semaphore: asyncio.Semaphore) -> None:
        async with semaphore:
            if self.stopping:
                return
            try:
                await self._process(bundle)
            except Exception:  # noqa: BLE001 — одна упавшая задача не должна ронять прогон
                self.log.exception("[%s] аварийное завершение задачи", bundle.account.login)

    # ── один аккаунт ─────────────────────────────────────────
    async def _process(self, bundle: Bundle) -> None:
        login = bundle.account.login
        log = get_logger(login)
        jitter = self.cfg.get("run.start_jitter") or [0, 0]
        await asyncio.sleep(random.uniform(float(jitter[0]), float(jitter[1])))

        self.hub.publish("account", login=login, state="started")
        skip = set(self.cfg.get("run.skip_statuses") or ["done"])

        for module in self.modules:
            if self.stopping:
                return
            current = self.results.get(login, module.name)
            if current.status in skip:
                log.info("Модуль %s уже в статусе '%s' — пропускаю", module.name, current.status)
                continue

            if bundle.error:
                await self._record(login, module.name, "no_mafile", "bind", bundle.error, 0)
                log.error("%s", bundle.error)
                continue

            await self._run_module(bundle, module, log)

    async def _run_module(self, bundle: Bundle, module, log) -> None:
        login = bundle.account.login
        attempts = max(1, int(self.cfg.get("retries.attempts", 3)))
        base_delay = float(self.cfg.get("retries.base_delay_s", 5))
        max_delay = float(self.cfg.get("retries.max_delay_s", 60))

        for attempt in range(1, attempts + 1):
            if self.stopping:
                return
            await self._record(login, module.name, Status.IN_PROGRESS, "", "", attempt)
            session = BrowserSession(login, bundle.proxy, self.cfg, self.state, log)
            ctx = AccountContext(
                bundle=bundle,
                cfg=self.cfg,
                selectors=self.selectors,
                log=log,
                session=session,
                results=self.results,
                artifacts=self.artifacts,
                steam_time=self.steam_time,
                solver=self.solver,
                debugger=Debugger(self.cfg, log, self.artifacts) if self.debug else None,
                module=module.name,
                attempt=attempt,
            )
            try:
                await session.start()
                if self.debug:
                    # в отладке модуль стоит на паузах — общий таймаут его бы убил
                    await module.run(ctx)
                else:
                    await asyncio.wait_for(module.run(ctx), timeout=float(self.cfg.get("timeouts.module_s", 900)))
            except ImportError as exc:
                # движок браузера не установлен — повторять бессмысленно, останавливаем прогон
                log.error("%s", exc)
                await self._record(login, module.name, Status.ERROR, "browser", str(exc), attempt)
                self.hub.publish("run", state="failed", error=str(exc))
                self.stop()
                return
            except MaFileMissing as exc:
                await self._finish(login, module.name, exc.status, ctx.stage, str(exc), attempt, fatal=True)
                return
            except RetryableError as exc:
                log.warning("Повторяемая ошибка на этапе '%s': %s", ctx.stage, exc)
                if attempt >= attempts:
                    await self._finish(login, module.name, Status.ERROR, ctx.stage, str(exc), attempt, fatal=True)
                    return
                delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                log.info("Повтор через %.0f c (попытка %d из %d)", delay, attempt + 1, attempts)
                await asyncio.sleep(delay)
            except BotError as exc:
                await self._finish(login, module.name, exc.status, exc.stage or ctx.stage, str(exc), attempt, fatal=True)
                return
            except asyncio.TimeoutError:
                await self._finish(
                    login, module.name, Status.ERROR, ctx.stage, "превышен общий таймаут модуля", attempt, fatal=True
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("Непредвиденная ошибка на этапе '%s'", ctx.stage)
                if attempt >= attempts:
                    await self._finish(
                        login, module.name, Status.ERROR, ctx.stage, f"{type(exc).__name__}: {exc}", attempt, fatal=True
                    )
                    return
                await asyncio.sleep(min(base_delay * attempt, max_delay))
            else:
                await self._finish(login, module.name, Status.DONE, ctx.stage, "", attempt, fatal=False)
                return
            finally:
                await session.close()

    # ── статусы и предохранитель ─────────────────────────────
    async def _record(self, login: str, module: str, status: str, stage: str, error: str, attempts: int) -> None:
        row = await self.results.update(
            login, module, status=status, stage=stage, error=error, attempts=attempts
        )
        self.hub.publish(
            "result",
            login=row.login,
            module=row.module,
            status=row.status,
            stage=row.stage,
            error=row.error,
            attempts=row.attempts,
            updated_at=row.updated_at,
        )

    async def _finish(
        self, login: str, module: str, status: str, stage: str, error: str, attempts: int, *, fatal: bool
    ) -> None:
        await self._record(login, module, status, stage, error, attempts)
        if fatal and status != Status.DONE:
            self._consecutive_failures += 1
            self._check_breaker(status)
        else:
            self._consecutive_failures = 0

    def _check_breaker(self, status: str) -> None:
        breaker = self.cfg.section("run").get("circuit_breaker") or {}
        if not breaker.get("enabled", True):
            return
        limit = int(breaker.get("consecutive_failures", 10))
        if self._consecutive_failures >= limit:
            self.log.error(
                "Предохранитель: %d фатальных подряд (последний статус '%s') — останавливаю прогон, "
                "чтобы не сжечь остальные аккаунты",
                self._consecutive_failures, status,
            )
            self.hub.publish("run", state="circuit_breaker", failures=self._consecutive_failures)
            self.stop()
