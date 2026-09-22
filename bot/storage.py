"""Хранилище профилей браузера, cookies и закреплённых отпечатков."""
from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path


class StateStore:
    """Пути к cookies-файлам, закреплённым отпечаткам и профилям браузера."""

    def __init__(self, directory: Path, profiles_dir: Path | None = None):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self.profiles_dir = profiles_dir
        if profiles_dir is not None:
            profiles_dir.mkdir(parents=True, exist_ok=True)

    def profile(self, login: str) -> Path:
        """Папка профиля Firefox для аккаунта (persistent context)."""
        if self.profiles_dir is None:
            raise ValueError("profiles_dir не задан")
        path = self.profiles_dir / _safe_name(login)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def path(self, login: str, name: str = "csfloat") -> Path:
        safe = _safe_name(login)
        # основной контекст живёт в state/<login>.json, как в ТЗ
        return self.dir / (f"{safe}.json" if name == "csfloat" else f"{safe}.{name}.json")

    def forget(self, login: str, *, keep_fingerprint: bool = True) -> None:
        """Сбрасывает сессию аккаунта. Отпечаток по умолчанию сохраняем:
        он должен пережить сброс cookies, иначе аккаунт снова станет новым устройством."""
        for p in self.dir.glob(f"{_safe_name(login)}*.json"):
            if keep_fingerprint and p.name.endswith(".fp.json"):
                continue
            p.unlink(missing_ok=True)
        if self.profiles_dir is not None:
            shutil.rmtree(self.profiles_dir / _safe_name(login), ignore_errors=True)


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value) or "unknown"
