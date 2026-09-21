"""Загрузка config.yaml / selectors.yaml + оверрайды из CLI."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError

ROOT = Path(__file__).resolve().parent.parent


def _deep_merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Config:
    """Тонкая обёртка над dict с доступом по точке: cfg.get('timeouts.action_ms')."""

    def __init__(self, data: dict, *, path: Path | None = None, root: Path | None = None):
        self._data = data
        self.path = path
        self.root = root or ROOT

    # ── чтение ───────────────────────────────────────────────
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node if node is not None else default

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        node = self._data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def section(self, name: str) -> dict:
        return self.get(name, {}) or {}

    def as_dict(self) -> dict:
        return copy.deepcopy(self._data)

    # ── пути ─────────────────────────────────────────────────
    def path_for(self, key: str) -> Path:
        raw = self.get(f"paths.{key}")
        if not raw:
            raise ConfigError(f"paths.{key} не задан в конфиге")
        p = Path(raw)
        return p if p.is_absolute() else self.root / p

    def ensure_dirs(self) -> None:
        for key in ("state", "profiles", "errors", "logs", "debug_dumps"):
            self.path_for(key).mkdir(parents=True, exist_ok=True)

    # ── загрузка/сохранение ──────────────────────────────────
    @classmethod
    def load(cls, path: str | Path = "config.yaml", *, root: Path | None = None) -> "Config":
        root = root or ROOT
        p = Path(path)
        if not p.is_absolute():
            p = root / p
        if not p.exists():
            raise ConfigError(f"Не найден конфиг: {p}")
        with p.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            raise ConfigError(f"{p}: ожидался YAML-словарь")
        return cls(data, path=p, root=root)

    def apply_cli(self, args) -> "Config":
        """CLI перекрывает yaml. Пустые/None значения игнорируются."""
        mapping = {
            "threads": "run.threads",
            "headful": "run.headful",
            "debug": "run.debug",
            "host": "web.host",
            "port": "web.port",
        }
        for attr, dotted in mapping.items():
            value = getattr(args, attr, None)
            if value is None or value is False:
                continue
            self.set(dotted, value)
        modules = getattr(args, "modules", None)
        if modules:
            self.set("run.modules", [m.strip() for m in modules.split(",") if m.strip()])
        if getattr(args, "debug", False):
            # отладка всегда headful и в один поток — иначе паузы перемешаются
            self.set("run.headful", True)
            self.set("run.threads", 1)
        return self


def load_selectors(path: str | Path = "selectors.yaml", *, root: Path | None = None) -> dict:
    root = root or ROOT
    p = Path(path)
    if not p.is_absolute():
        p = root / p
    if not p.exists():
        raise ConfigError(f"Не найден файл селекторов: {p}")
    with p.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{p}: ожидался YAML-словарь")
    return data


def selector(selectors: dict, dotted: str, *, required: bool = True) -> list[str]:
    """Достаёт список кандидатов: selector(sel, 'outlook.email_input')."""
    node: Any = selectors
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            if required:
                raise ConfigError(f"В selectors.yaml нет ключа {dotted}")
            return []
        node = node[part]
    if isinstance(node, str):
        return [node]
    if not isinstance(node, list):
        raise ConfigError(f"selectors.yaml: {dotted} должен быть строкой или списком")
    return [str(x) for x in node]
