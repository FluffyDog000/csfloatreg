"""Веб-интерфейс: две страницы на одном приложении.

    /          — «Запуск»:  очередь автоматической регистрации, лог, артефакты
    /profiles  — «Профили»: ручной антидетект-браузер, карточка аккаунта, SDA

Слушает 127.0.0.1 — наружу выставлять нельзя. На странице запуска паролей нет,
а вот карточка аккаунта показывает их намеренно: в этом её смысл.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import time
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from bot import logging_setup
from bot.config import load_selectors
from bot.confirmations import ConfirmationError
from bot.delivery import MAX_WORKERS, Delivery
from bot.errors import LoaderError
from bot.events import HubLogHandler, hub
from bot.loader import load_all
from bot.manager import ProfileManager
from bot.runner import Runner
from bot.steam_guard import SteamTime

STATIC = Path(__file__).resolve().parent / "static"

#: Статусы, которые человек ставит сам. Остальные пишет только прогон.
MANUAL_STATUSES = {"new", "done", "skipped", "error"}


def mask_mail(mail: str) -> str:
    if "@" not in mail:
        return mail
    name, domain = mail.split("@", 1)
    head = name[:2] if len(name) > 2 else name[:1]
    return f"{head}{'*' * max(3, len(name) - len(head))}@{domain}"


class AppState:
    def __init__(self, cfg, selectors, selectors_path: str, bindings=None):
        self.cfg = cfg
        self.bindings = bindings
        self.selectors = selectors
        self.selectors_path = selectors_path
        self.bundles: list = []
        self.load_error: str | None = None
        self.runner: Runner | None = None
        self.task: asyncio.Task | None = None
        self.last_summary: dict = {}
        self.reload_inputs()

    # ── входные данные ───────────────────────────────────────
    def reload_inputs(self) -> None:
        try:
            self.bundles = load_all(self.cfg, bindings=self.bindings)
            self.load_error = None
        except LoaderError as exc:
            self.bundles = []
            self.load_error = str(exc)

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def results_store(self):
        if self.runner is not None:
            return self.runner.results
        from bot.storage import ResultsStore

        return ResultsStore(self.cfg.path_for("results"))

    def snapshot(self) -> dict:
        results = self.results_store()
        rows = {(r.login.lower(), r.module): r for r in results.rows()}
        modules = self.cfg.get("run.modules") or ["registration"]
        accounts = []
        for bundle in self.bundles:
            login = bundle.account.login
            per_module = {}
            for module in modules:
                row = rows.get((login.lower(), module))
                per_module[module] = (
                    {
                        "status": row.status,
                        "stage": row.stage,
                        "error": row.error,
                        "attempts": row.attempts,
                        "updated_at": row.updated_at,
                    }
                    if row
                    else {"status": "new", "stage": "", "error": "", "attempts": 0, "updated_at": ""}
                )
            accounts.append(
                {
                    "login": login,
                    "mail": mask_mail(bundle.account.mail),
                    "proxy": bundle.proxy.safe(),
                    "mafile": bool(bundle.mafile and bundle.mafile.shared_secret),
                    "bind_error": bundle.error,
                    "modules": per_module,
                }
            )
        return {
            "running": self.running,
            "load_error": self.load_error,
            "modules": modules,
            "threads": self.cfg.get("run.threads"),
            "headful": self.cfg.get("run.headful"),
            "engine": self.cfg.get("browser.engine"),
            "counts": {
                "accounts": len(self.bundles),
                "ready": sum(1 for b in self.bundles if not b.error),
                "problems": sum(1 for b in self.bundles if b.error),
            },
            "summary": results.summary(),
            "last_summary": self.last_summary,
            "accounts": accounts,
        }


def create_app(cfg, selectors=None, *, selectors_path: str = "selectors.yaml") -> FastAPI:
    if selectors is None:
        selectors = load_selectors(selectors_path)
    steam_time = SteamTime(cfg.get("steam.time_sync_url"), enabled=bool(cfg.get("steam.time_sync", True)))
    manager = ProfileManager(cfg, steam_time, selectors=selectors)
    # одно хранилище привязок на процесс: два экземпляра затирали бы записи друг друга
    state = AppState(cfg, selectors, selectors_path, bindings=manager.bindings)
    delivery = Delivery(manager)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        hub.bind_loop(asyncio.get_running_loop())
        _warn_stale_local()
        await steam_time.sync(logging_setup.get_logger())
        yield
        await manager.close_all()

    app = FastAPI(title="CSFloat bot", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.manager = manager          # чтобы до менеджера можно было добраться снаружи
    token = cfg.get("web.token")

    logging_setup.add_handler(HubLogHandler(hub, level=logging.INFO))

    # ── авторизация (опциональный токен) ─────────────────────
    @app.middleware("http")
    async def check_token(request: Request, call_next):
        if token and request.url.path.startswith("/api"):
            supplied = request.headers.get("x-token") or request.query_params.get("token")
            if supplied != token:
                return JSONResponse({"detail": "нужен токен"}, status_code=401)
        return await call_next(request)

    # ── страницы ─────────────────────────────────────────────
    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/profiles")
    async def profiles_page():
        return FileResponse(STATIC / "manager.html")

    @app.get("/delivery")
    async def delivery_page():
        return FileResponse(STATIC / "delivery.html")

    # ── состояние ────────────────────────────────────────────
    @app.get("/api/state")
    async def api_state():
        return state.snapshot()

    @app.post("/api/reload")
    async def api_reload():
        state.reload_inputs()
        manager.reload_inputs()
        hub.publish("log", level="INFO", text="Входные файлы перечитаны")
        return state.snapshot()

    # ── управление прогоном ──────────────────────────────────
    @app.post("/api/start")
    async def api_start(payload: dict = Body(default={})):
        if state.running:
            raise HTTPException(409, "прогон уже идёт")
        if state.load_error:
            raise HTTPException(400, state.load_error)

        for key, dotted in (
            ("threads", "run.threads"),
            ("headful", "run.headful"),
            ("debug", "run.debug"),
        ):
            if payload.get(key) is not None:
                cfg.set(dotted, payload[key])
        if payload.get("modules"):
            cfg.set("run.modules", payload["modules"])

        state.reload_inputs()
        runner = Runner(cfg, state.selectors, state.bundles, bindings=manager.bindings)
        state.runner = runner

        # точечный запуск: галочки в таблице приходят списком logins,
        # поле «только» — строкой only. Выбранные аккаунты идут независимо
        # от статуса, иначе 'done' молча выкинул бы их из очереди.
        only = payload.get("logins") or payload.get("only") or None
        limit = payload.get("limit") or None
        force = bool(payload.get("force", bool(payload.get("logins"))))

        async def _job():
            try:
                state.last_summary = await runner.run(only=only, limit=limit, force=force)
            except Exception as exc:  # noqa: BLE001
                logging_setup.get_logger().exception("Прогон упал: %s", exc)
                hub.publish("run", state="failed", error=str(exc))

        state.task = asyncio.create_task(_job())
        return {"started": True, "logins": only if isinstance(only, list) else ([only] if only else [])}

    @app.post("/api/stop")
    async def api_stop():
        if state.runner is None or not state.running:
            raise HTTPException(409, "прогон не запущен")
        state.runner.stop()
        return {"stopping": True}

    # ── события (SSE) ────────────────────────────────────────
    @app.get("/api/events")
    async def api_events(request: Request):
        queue = hub.subscribe()

        async def stream():
            try:
                for event in hub.history()[-120:]:
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
                        continue
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            finally:
                hub.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ── файлы входных данных ─────────────────────────────────
    @app.post("/api/upload/{kind}")
    async def api_upload(kind: str, files: list[UploadFile] = File(...)):
        if kind in ("accounts", "proxies", "mails"):
            target = cfg.path_for(kind)
            content = await files[0].read()
            target.write_bytes(content)
            saved = [target.name]
        elif kind == "mafiles":
            folder = cfg.path_for("mafiles")
            folder.mkdir(parents=True, exist_ok=True)
            saved = []
            for item in files:
                name = Path(item.filename or "unknown.maFile").name
                (folder / name).write_bytes(await item.read())
                saved.append(name)
        else:
            raise HTTPException(400, f"неизвестный тип загрузки: {kind}")

        state.reload_inputs()
        manager.reload_inputs()
        hub.publish("log", level="INFO", text=f"Загружено в {kind}: {len(saved)} файл(ов)")
        return {"saved": saved, "state": state.snapshot()}

    def _stale_local():
        return cfg.stale_local(cfg.path) if cfg.path else None

    def _warn_stale_local() -> None:
        legacy = _stale_local()
        if legacy is not None:
            hub.publish(
                "log", level="WARNING",
                text=f"{legacy.name} больше не читается — перенеси настройки в config.yaml "
                     f"и удали файл, иначе будешь править то, что ни на что не влияет",
            )

    # ── конфиги ──────────────────────────────────────────────
    @app.post("/api/config/reload")
    async def api_config_reload():
        """Перечитать config.yaml в живой объект настроек.

        Без этого правка ключа или таймаутов доходила до бота только перезапуском
        процесса, а сообщение об ошибке при этом выглядело как «я же вписал».
        """
        try:
            cfg.reload()
        except Exception as exc:  # noqa: BLE001 — текст нужен в интерфейсе
            raise HTTPException(400, f"конфиг не читается: {exc}") from None
        state.reload_inputs()
        manager.reload_inputs()
        key_set = bool(cfg.get("mail.firstmail.api_key") or os.getenv("FIRSTMAIL_API_KEY"))
        sources = [str(path.name) for path in cfg.sources()]
        hub.publish(
            "log", level="INFO",
            text=f"Конфиг перечитан ({', '.join(sources)}), ключ firstmail: "
                 + ("задан" if key_set else "НЕ ЗАДАН"),
        )
        _warn_stale_local()
        return {
            "sources": [str(path) for path in cfg.sources()],
            "mail_key": key_set,
            "provider": cfg.get("mail.provider"),
            "stale_local": str(_stale_local() or ""),
            "state": state.snapshot(),
        }

    @app.get("/api/config/{name}")
    async def api_config_get(name: str):
        path = _config_path(name)
        return {"name": name, "path": str(path), "text": path.read_text(encoding="utf-8")}

    @app.post("/api/config/{name}")
    async def api_config_set(name: str, payload: dict = Body(...)):
        import yaml

        path = _config_path(name)
        text = payload.get("text", "")
        try:
            yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise HTTPException(400, f"YAML не разбирается: {exc}") from exc
        backup = path.with_suffix(path.suffix + f".bak{time.strftime('%H%M%S')}")
        shutil.copy2(path, backup)
        path.write_text(text, encoding="utf-8")

        if name == "selectors":
            state.selectors = load_selectors(state.selectors_path)
            hub.publish("log", level="INFO", text="selectors.yaml перечитан")
        else:
            hub.publish(
                "log", level="INFO",
                text="config.yaml сохранён (применится при следующем запуске процесса)",
            )
        return {"saved": True, "backup": backup.name}

    def _config_path(name: str) -> Path:
        if name == "config":
            return cfg.path or (cfg.root / "config.yaml")
        if name == "selectors":
            path = Path(state.selectors_path)
            return path if path.is_absolute() else cfg.root / path
        raise HTTPException(404, "нет такого конфига")

    # ── артефакты ошибок ─────────────────────────────────────
    @app.get("/api/artifacts/{login}")
    async def api_artifacts(login: str):
        from bot.storage import ArtifactStore

        store = ArtifactStore(cfg.path_for("errors"), cfg.path_for("debug_dumps"))
        items = [
            {"name": p.name, "path": str(p), "size": p.stat().st_size, "mtime": p.stat().st_mtime}
            for p in store.list_for(login)
            if p.is_file()
        ]
        return {"login": login, "items": items}

    @app.get("/api/artifact")
    async def api_artifact(path: str):
        target = Path(path).resolve()
        allowed = [cfg.path_for("errors").resolve(), cfg.path_for("debug_dumps").resolve()]
        if not any(str(target).startswith(str(root)) for root in allowed):
            raise HTTPException(403, "файл вне папок артефактов")
        if not target.exists():
            raise HTTPException(404, "файл не найден")
        return FileResponse(target)

    # ── ручная смена статуса ─────────────────────────────────
    @app.post("/api/status")
    async def api_set_status(payload: dict = Body(...)):
        """Статус в results.csv ставит человек: пометить done, вернуть в new и т.д."""
        logins = [str(x) for x in (payload.get("logins") or []) if str(x).strip()]
        if not logins and payload.get("login"):
            logins = [str(payload["login"])]
        if not logins:
            raise HTTPException(400, "не выбран ни один аккаунт")

        status = str(payload.get("status") or "").strip()
        if status not in MANUAL_STATUSES:
            raise HTTPException(400, f"статус '{status}' менять вручную нельзя: {sorted(MANUAL_STATUSES)}")

        modules = payload.get("modules") or cfg.get("run.modules") or ["registration"]
        note = str(payload.get("note") or "поставлено вручную")
        results = state.results_store()
        known = {b.account.login.lower(): b.account.login for b in state.bundles}

        changed = []
        for login in logins:
            real = known.get(login.lower())
            if real is None:
                continue
            for module in modules:
                await results.update(
                    real, module, status=status, stage="", error="" if status != "error" else note, attempts=0
                )
            # менеджер профилей смотрит на те же аккаунты — держим пометки в согласии
            if status in ("done", "new"):
                manager.bindings.set_status(real, status)
            changed.append(real)

        if not changed:
            raise HTTPException(404, "ни один из логинов не найден в accounts.txt")
        hub.publish(
            "log", level="INFO",
            text=f"Статус '{status}' поставлен вручную: {', '.join(changed[:8])}"
                 + (f" и ещё {len(changed) - 8}" if len(changed) > 8 else ""),
        )
        return {"changed": changed, "status": status, "state": state.snapshot()}

    # ── сброс статуса ────────────────────────────────────────
    @app.post("/api/reset")
    async def api_reset(payload: dict = Body(...)):
        logins = [str(x) for x in (payload.get("logins") or []) if str(x).strip()]
        if not logins and payload.get("login"):
            logins = [str(payload["login"])]
        if not logins:
            raise HTTPException(400, "не указан login")

        results = state.results_store()
        store = None
        if payload.get("forget_cookies"):
            from bot.storage import StateStore

            store = StateStore(cfg.path_for("state"), cfg.path_for("profiles"))

        for login in logins:
            for module in cfg.get("run.modules") or ["registration"]:
                await results.update(login, module, status="new", stage="", error="", attempts=0)
            if store is not None:
                if login in manager.sessions:
                    raise HTTPException(400, f"[{login}] открыт ручной профиль — закрой его на вкладке «Профили»")
                store.forget(login)          # отпечаток сохраняется
            manager.bindings.set_status(login, "new")
        hub.publish("log", level="INFO", text=f"Статус сброшен: {', '.join(logins[:8])}")
        return state.snapshot()

    # ═══ менеджер профилей (/profiles) ═══════════════════════
    @app.get("/api/m/state")
    async def m_state():
        return {
            "accounts": manager.rows(),
            "pool": manager.pool_stats(),
            "mails": manager.mail_stats(),
            "load_error": manager.load_error,
            "engine": cfg.get("browser.engine"),
            "time_offset": round(steam_time.offset, 1),
        }

    @app.get("/api/m/card/{login}")
    async def m_card(login: str):
        try:
            return manager.card(login)
        except KeyError:
            raise HTTPException(404, "аккаунт не найден") from None

    @app.get("/api/m/guard/{login}")
    async def m_guard(login: str):
        return manager.guard(login)

    @app.post("/api/m/status/{login}")
    async def m_status(login: str, payload: dict = Body(default={})):
        manager.set_status(login, payload.get("status", "new"), payload.get("note", ""))
        return {"ok": True}

    @app.post("/api/m/trade-url/{login}")
    async def m_trade_url(login: str, payload: dict = Body(default={})):
        try:
            return {"trade_url": manager.set_trade_url(login, payload.get("trade_url", ""))}
        except KeyError:
            raise HTTPException(404, "аккаунт не найден") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.get("/api/m/confirmations/{login}")
    async def m_confirmations(login: str):
        try:
            return {"items": await manager.confirmations(login)}
        except ConfirmationError as exc:
            raise HTTPException(400, str(exc)) from None
        except Exception as exc:  # noqa: BLE001 — текст ошибки нужен в интерфейсе
            raise HTTPException(400, f"не удалось получить подтверждения: {exc}") from None

    @app.post("/api/m/confirmations/{login}")
    async def m_confirm(login: str, payload: dict = Body(default={})):
        ids = [str(i) for i in (payload.get("ids") or []) if str(i)]
        if not ids:
            raise HTTPException(400, "не переданы id подтверждений")
        try:
            return await manager.respond_confirmation(login, ids, accept=bool(payload.get("accept", True)))
        except ConfirmationError as exc:
            raise HTTPException(400, str(exc)) from None
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"не удалось ответить на подтверждение: {exc}") from None

    @app.post("/api/m/open/{login}")
    async def m_open(login: str):
        if state.running:
            raise HTTPException(409, "идёт прогон: ручной профиль займёт тот же аккаунт")
        try:
            return await manager.open_profile(login)
        except KeyError:
            raise HTTPException(404, "аккаунт не найден") from None
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/m/close/{login}")
    async def m_close(login: str):
        return await manager.close_profile(login)

    @app.post("/api/m/replace-proxy")
    async def m_replace_proxies(payload: dict = Body(default={})):
        """Смена прокси на нескольких аккаунтах сразу."""
        logins = [str(x) for x in (payload.get("logins") or []) if str(x).strip()]
        if not logins:
            raise HTTPException(400, "не выбран ни один аккаунт")
        if state.running:
            raise HTTPException(409, "идёт прогон: смена прокси на ходу оборвала бы аккаунт")
        result = manager.replace_proxies(logins)
        state.reload_inputs()        # очередь берёт прокси из тех же привязок
        return {**result, "state": state.snapshot()}

    @app.post("/api/m/replace-proxy/{login}")
    async def m_replace_proxy(login: str):
        try:
            return manager.replace_proxy(login)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/m/replace-mail/{login}")
    async def m_replace_mail(login: str):
        try:
            return manager.replace_mail(login)
        except KeyError:
            raise HTTPException(404, "аккаунт не найден") from None
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, str(exc)) from None

    # ═══ рассылка предметов ══════════════════════════════════
    @app.get("/api/m/delivery")
    async def m_delivery_state():
        rows = []
        for login in manager.accounts:
            entry = manager.bindings.entry(login)
            mafile = manager.mafiles.get(login.lower())
            rows.append({
                "login": login,
                "trade_url": entry.get("trade_url", ""),
                "can_send": bool(mafile and mafile.identity_secret and mafile.steam_id),
                "opened": login in manager.sessions,
                "delivery": delivery.results.get(login) or entry.get("delivery") or {},
            })
        return {
            "accounts": rows, "running": delivery.running,
            "workers": max(1, min(int(cfg.get("delivery.workers", 1) or 1), MAX_WORKERS)),
            "max_workers": MAX_WORKERS,
        }

    @app.post("/api/m/delivery/inventory/{sender}")
    async def m_delivery_inventory(sender: str):
        try:
            return await delivery.inventory(sender)
        except Exception as exc:  # noqa: BLE001 — текст ошибки нужен в интерфейсе
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/m/delivery/start")
    async def m_delivery_start(payload: dict = Body(...)):
        if delivery.running:
            raise HTTPException(409, "рассылка уже идёт")
        if state.running:
            raise HTTPException(409, "идёт прогон регистрации — дождись его конца")

        sender = str(payload.get("sender") or "").strip()
        item = str(payload.get("item") or "").strip()
        targets = [str(t) for t in (payload.get("targets") or []) if str(t)]
        if not sender or not item or not targets:
            raise HTTPException(400, "нужны отправитель, название предмета и хотя бы один получатель")

        async def _job():
            try:
                await delivery.run(
                    sender=sender,
                    item=item,
                    per_account=max(1, int(payload.get("per_account") or 1)),
                    targets=targets,
                    message=str(payload.get("message") or ""),
                    headful=bool(payload.get("headful", False)),
                    resume=bool(payload.get("resume", True)),
                    workers=max(1, min(int(payload.get("workers") or 1), MAX_WORKERS)),
                )
            except Exception as exc:  # noqa: BLE001 — рассылка не должна ронять процесс
                logging_setup.get_logger().error("Рассылка прервана: %s", exc)
                hub.publish("delivery-run", state="failed", error=str(exc))

        asyncio.create_task(_job())
        return {"started": True, "targets": len(targets)}

    @app.post("/api/m/delivery/stop")
    async def m_delivery_stop():
        if not delivery.running:
            raise HTTPException(409, "рассылка не запущена")
        delivery.stop()
        return {"stopping": True}

    @app.post("/api/m/reset/{login}")
    async def m_reset(login: str):
        if login in manager.sessions:
            raise HTTPException(400, "сначала закрой профиль")
        manager.state.forget(login)          # отпечаток сохраняется
        hub.publish("log", level="INFO", text=f"[{login}] профиль и cookies стёрты")
        return {"ok": True}

    return app
