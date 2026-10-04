"""Реестр модулей.

Модуль — это класс с именем и async run(ctx). Runner прогоняет их по порядку
из config.yaml (run.modules), фиксируя статус каждого отдельно в results.csv.
Добавление модуля 2 = новый файл + @register('api_key') + строка в конфиге.
"""
from __future__ import annotations

from typing import Protocol

from ..errors import ConfigError


class Module(Protocol):
    name: str

    async def run(self, ctx) -> None:
        ...


MODULES: dict[str, type] = {}


def register(name: str):
    def wrapper(cls):
        cls.name = name
        MODULES[name] = cls
        return cls

    return wrapper


def build_modules(names: list[str]) -> list[Module]:
    from . import api_key, registration  # noqa: F401 — регистрация

    modules = []
    for name in names:
        cls = MODULES.get(name)
        if cls is None:
            raise ConfigError(f"Неизвестный модуль '{name}'. Доступны: {sorted(MODULES)}")
        modules.append(cls())
    return modules
