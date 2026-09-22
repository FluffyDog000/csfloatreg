"""Веб-интерфейс менеджера профилей.

Слушает 127.0.0.1: в панели показываются пароли — это её задача, но наружу
такое выставлять нельзя.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
import time
from pathlib import Path

from fastapi import Body, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from bot import logging_setup
from bot.confirmations import ConfirmationError
from bot.events import HubLogHandler, hub
from bot.manager import ProfileManager
from bot.steam_guard import SteamTime

STATIC = Path(__file__).resolve().parent / "static"


def create_app(cfg) -> FastAPI:
    steam_time = SteamTime(cfg.get("steam.time_sync_url"), enabled=bool(cfg.get("steam.time_sync", True)))
    manager = ProfileManager(cfg, steam_time)
    token = cfg.get("web.token")

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        hub.bind_loop(asyncio.get_running_loop())
        await steam_time.sync(logging_setup.get_logger())
        yield
        await manager.close_all()

    app = FastAPI(title="Профили", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.manager = manager          # чтобы до менеджера можно было добраться снаружи
    logging_setup.add_handler(HubLogHandler(hub, level=logging.INFO))

    @app.middleware("http")
    async def check_token(request: Request, call_next):
        if token and request.url.path.startswith("/api"):
            supplied = request.headers.get("x-token") or request.query_params.get("token")
            if supplied != token:
                return JSONResponse({"detail": "нужен токен"}, status_code=401)
        return await call_next(request)

    # ── страница ─────────────────────────────────────────────
    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    # ── состояние ────────────────────────────────────────────
    @app.get("/api/state")
    async def api_state():
        return {
            "accounts": manager.rows(),
            "pool": manager.pool_stats(),
            "load_error": manager.load_error,
            "engine": cfg.get("browser.engine"),
            "time_offset": round(steam_time.offset, 1),
        }

    @app.post("/api/reload")
    async def api_reload():
        manager.reload_inputs()
        hub.publish("log", level="INFO", text="Входные файлы перечитаны")
        return {"ok": True}

    # ── карточка аккаунта и код Steam Guard ──────────────────
    @app.get("/api/card/{login}")
    async def api_card(login: str):
        try:
            return manager.card(login)
        except KeyError:
            raise HTTPException(404, "аккаунт не найден") from None

    @app.get("/api/guard/{login}")
    async def api_guard(login: str):
        return manager.guard(login)

    @app.post("/api/status/{login}")
    async def api_status(login: str, payload: dict = Body(default={})):
        manager.set_status(login, payload.get("status", "new"), payload.get("note", ""))
        return {"ok": True}

    @app.post("/api/trade-url/{login}")
    async def api_trade_url(login: str, payload: dict = Body(default={})):
        try:
            return {"trade_url": manager.set_trade_url(login, payload.get("trade_url", ""))}
        except KeyError:
            raise HTTPException(404, "аккаунт не найден") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    # ── подтверждения Steam (как в SDA) ──────────────────────
    @app.get("/api/confirmations/{login}")
    async def api_confirmations(login: str):
        try:
            return {"items": await manager.confirmations(login)}
        except ConfirmationError as exc:
            raise HTTPException(400, str(exc)) from None
        except Exception as exc:  # noqa: BLE001 — текст ошибки нужен в интерфейсе
            raise HTTPException(400, f"не удалось получить подтверждения: {exc}") from None

    @app.post("/api/confirmations/{login}")
    async def api_confirm(login: str, payload: dict = Body(default={})):
        ids = [str(i) for i in (payload.get("ids") or []) if str(i)]
        if not ids:
            raise HTTPException(400, "не переданы id подтверждений")
        try:
            return await manager.respond_confirmation(login, ids, accept=bool(payload.get("accept", True)))
        except ConfirmationError as exc:
            raise HTTPException(400, str(exc)) from None
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"не удалось ответить на подтверждение: {exc}") from None

    # ── профили ──────────────────────────────────────────────
    @app.post("/api/open/{login}")
    async def api_open(login: str):
        try:
            return await manager.open_profile(login)
        except KeyError:
            raise HTTPException(404, "аккаунт не найден") from None
        except Exception as exc:  # noqa: BLE001 — текст ошибки нужен в интерфейсе
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/close/{login}")
    async def api_close(login: str):
        return await manager.close_profile(login)

    @app.post("/api/replace-proxy/{login}")
    async def api_replace_proxy(login: str):
        try:
            return manager.replace_proxy(login)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/reset/{login}")
    async def api_reset(login: str):
        if login in manager.sessions:
            raise HTTPException(400, "сначала закрой профиль")
        manager.state.forget(login)          # отпечаток сохраняется
        hub.publish("log", level="INFO", text=f"[{login}] профиль и cookies стёрты")
        return {"ok": True}

    # ── файлы ────────────────────────────────────────────────
    @app.post("/api/upload/{kind}")
    async def api_upload(kind: str, files: list[UploadFile] = File(...)):
        if kind in ("accounts", "proxies"):
            target = cfg.path_for(kind)
            target.write_bytes(await files[0].read())
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

        manager.reload_inputs()
        hub.publish("log", level="INFO", text=f"Загружено в {kind}: {len(saved)} файл(ов)")
        return {"saved": saved}

    # ── конфиг ───────────────────────────────────────────────
    @app.get("/api/config")
    async def api_config_get():
        path = _local_config()
        return {"path": str(path), "text": path.read_text(encoding="utf-8") if path.exists() else ""}

    @app.post("/api/config")
    async def api_config_set(payload: dict = Body(...)):
        import yaml

        text = payload.get("text", "")
        try:
            yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise HTTPException(400, f"YAML не разбирается: {exc}") from exc
        path = _local_config()
        if path.exists():
            shutil.copy2(path, path.with_suffix(path.suffix + f".bak{time.strftime('%H%M%S')}"))
        path.write_text(text, encoding="utf-8")
        return {"saved": True, "note": "применится после перезапуска"}

    def _local_config() -> Path:
        base = cfg.path or (cfg.root / "config.yaml")
        return base.with_name(base.stem + ".local" + base.suffix)

    # ── события ──────────────────────────────────────────────
    @app.get("/api/events")
    async def api_events(request: Request):
        queue = hub.subscribe()

        async def stream():
            try:
                for event in hub.history()[-80:]:
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
            stream(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app
