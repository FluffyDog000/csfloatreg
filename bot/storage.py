"""results.csv, cookies-state и дампы ошибок."""
from __future__ import annotations

import asyncio
import csv
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .logging_setup import redact
from .models import Status

FIELDS = ["login", "module", "status", "stage", "error", "attempts", "updated_at"]


@dataclass(slots=True)
class ResultRow:
    login: str
    module: str
    status: str = Status.NEW
    stage: str = ""
    error: str = ""
    attempts: int = 0
    updated_at: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


class ResultsStore:
    """Одна строка на пару (логин, модуль).

    Такой формат переживает добавление модуля 2: у аккаунта просто появится
    вторая строка, а формат колонок не изменится.
    """

    def __init__(self, path: Path):
        self.path = path
        self._rows: dict[tuple[str, str], ResultRow] = {}
        self._lock = asyncio.Lock()
        self.load()

    # ── чтение ───────────────────────────────────────────────
    def load(self) -> None:
        self._rows.clear()
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8", newline="") as fh:
            for raw in csv.DictReader(fh):
                row = ResultRow(
                    login=raw.get("login", ""),
                    module=raw.get("module", "registration"),
                    status=raw.get("status", Status.NEW),
                    stage=raw.get("stage", ""),
                    error=raw.get("error", ""),
                    attempts=int(raw.get("attempts") or 0),
                    updated_at=raw.get("updated_at", ""),
                )
                if row.login:
                    self._rows[(row.login.lower(), row.module)] = row

    def get(self, login: str, module: str) -> ResultRow:
        return self._rows.get((login.lower(), module)) or ResultRow(login=login, module=module)

    def rows(self) -> list[ResultRow]:
        return list(self._rows.values())

    def summary(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for row in self._rows.values():
            out[row.status] = out.get(row.status, 0) + 1
        return out

    # ── запись ───────────────────────────────────────────────
    async def update(
        self,
        login: str,
        module: str,
        *,
        status: str,
        stage: str = "",
        error: str = "",
        attempts: int | None = None,
    ) -> ResultRow:
        async with self._lock:
            key = (login.lower(), module)
            row = self._rows.get(key) or ResultRow(login=login, module=module)
            row.status = status
            row.stage = stage
            row.error = redact(str(error or ""))[:500].replace("\n", " ")
            if attempts is not None:
                row.attempts = attempts
            row.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._rows[key] = row
            self._flush()
            return row

    def _flush(self) -> None:
        """Атомарная перезапись: прогон можно убить в любой момент."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".results-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=FIELDS)
                writer.writeheader()
                for row in sorted(self._rows.values(), key=lambda r: (r.login.lower(), r.module)):
                    writer.writerow(row.as_dict())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


class StateStore:
    """Пути к cookies-файлам браузерных контекстов."""

    def __init__(self, directory: Path):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)

    def path(self, login: str, name: str = "csfloat") -> Path:
        safe = _safe_name(login)
        # основной контекст живёт в state/<login>.json, как в ТЗ
        return self.dir / (f"{safe}.json" if name == "csfloat" else f"{safe}.{name}.json")

    def forget(self, login: str) -> None:
        for p in self.dir.glob(f"{_safe_name(login)}*.json"):
            p.unlink(missing_ok=True)


class ArtifactStore:
    """Скриншот + HTML при любой ошибке: errors/<login>/."""

    def __init__(self, errors_dir: Path, debug_dir: Path):
        self.errors_dir = errors_dir
        self.debug_dir = debug_dir

    def dir_for(self, login: str, *, debug: bool = False) -> Path:
        base = self.debug_dir if debug else self.errors_dir
        path = base / _safe_name(login)
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def dump(self, page, login: str, tag: str, *, debug: bool = False, note: str = "") -> list[Path]:
        """Сохраняет png + html (+ txt с пояснением). Никогда не бросает наружу."""
        if page is None:
            return []
        stamp = time.strftime("%H%M%S")
        folder = self.dir_for(login, debug=debug)
        prefix = f"{time.strftime('%Y%m%d')}_{stamp}_{_safe_name(tag)}"
        saved: list[Path] = []

        try:
            shot = folder / f"{prefix}.png"
            await page.screenshot(path=str(shot), full_page=True)
            saved.append(shot)
        except Exception:  # noqa: BLE001
            pass

        try:
            html = folder / f"{prefix}.html"
            content = await page.content()
            html.write_text(redact(content), encoding="utf-8")
            saved.append(html)
        except Exception:  # noqa: BLE001
            pass

        if note:
            try:
                txt = folder / f"{prefix}.txt"
                url = ""
                try:
                    url = page.url
                except Exception:  # noqa: BLE001
                    pass
                txt.write_text(redact(f"url: {url}\n\n{note}\n"), encoding="utf-8")
                saved.append(txt)
            except Exception:  # noqa: BLE001
                pass
        return saved

    def list_for(self, login: str) -> list[Path]:
        out: list[Path] = []
        for base in (self.errors_dir, self.debug_dir):
            folder = base / _safe_name(login)
            if folder.exists():
                out.extend(sorted(folder.iterdir(), reverse=True))
        return out


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value) or "unknown"
