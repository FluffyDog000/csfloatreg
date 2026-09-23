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
import shutil
import time
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from bot import logging_setup
from bot.config import load_selectors
from bot.confirmations import ConfirmationError
from bot.errors import LoaderError
from bot.events import HubLogHandler, hub
from bot.loader import load_all
from bot.manager import ProfileManager
from bot.runner import Runner
from bot.steam_guard import SteamTime

STATIC = Path(__file__).resolve().parent / "static"


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
    manager = ProfileManager(cfg, steam_time)
    # одно хранилище привязок на процесс: два экземпляра затирали бы записи друг друга
    state = AppState(cfg, selectors, selectors_path, bindings=manager.bindings)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        hub.bind_loop(asyncio.get_running_loop())
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

        only = payload.get("only") or None
        limit = payload.get("limit") or None

        async def _job():
            try:
                state.last_summary = await runner.run(only=only, limit=limit)
            except Exception as exc:  # noqa: BLE001
                logging_setup.get_logger().exception("Прогон упал: %s", exc)
                hub.publish("run", state="failed", error=str(exc))

        state.task = asyncio.create_task(_job())
        return {"started": True}

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

    # ── конфиги ──────────────────────────────────────────────
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

    # ── сброс статуса ────────────────────────────────────────
    @app.post("/api/reset")
    async def api_reset(payload: dict = Body(...)):
        login = payload.get("login")
        if not login:
            raise HTTPException(400, "не указан login")
        results = state.results_store()
        for module in cfg.get("run.modules") or ["registration"]:
            await results.update(login, module, status="new", stage="", error="", attempts=0)
        if payload.get("forget_cookies"):
            from bot.storage import StateStore

            StateStore(cfg.path_for("state"), cfg.path_for("profiles")).forget(login)
        hub.publish("log", level="INFO", text=f"[{login}] статус сброшен")
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

    @app.post("/api/m/reset/{login}")
    async def m_reset(login: str):
        if login in manager.sessions:
            raise HTTPException(400, "сначала закрой профиль")
        manager.state.forget(login)          # отпечаток сохраняется
        hub.publish("log", level="INFO", text=f"[{login}] профиль и cookies стёрты")
        return {"ok": True}

    return app
