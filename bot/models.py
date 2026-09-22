"""Датаклассы входных данных и статусы."""
from __future__ import annotations

import dataclasses
from pathlib import Path


#: Пометки аккаунта, которые ставит человек в интерфейсе.
STATUSES = ("new", "in_work", "done", "bad")


@dataclasses.dataclass(slots=True)
class Account:
    login: str
    password: str = dataclasses.field(repr=False)
    mail: str = ""
    mail_password: str = dataclasses.field(default="", repr=False)
    line_no: int = 0

    def __str__(self) -> str:  # чтобы пароль не утёк в f-строку
        return self.login


@dataclasses.dataclass(slots=True)
class Proxy:
    scheme: str
    host: str
    port: int
    username: str | None = None
    password: str | None = dataclasses.field(default=None, repr=False)
    raw_line_no: int = 0
    raw: str = dataclasses.field(default="", repr=False)   # строка из proxies.txt = ключ в пуле

    @property
    def needs_socks_relay(self) -> bool:
        """Playwright/Firefox не умеют авторизацию в SOCKS — нужен локальный релей."""
        return self.scheme.startswith("socks") and bool(self.username)

    @property
    def server(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"

    def playwright(self) -> dict:
        cfg: dict = {"server": self.server}
        if self.username:
            cfg["username"] = self.username
            cfg["password"] = self.password or ""
        return cfg

    def safe(self) -> str:
        """Строка для логов: без пароля."""
        user = f"{self.username}@" if self.username else ""
        return f"{self.scheme}://{user}{self.host}:{self.port}"

    def __str__(self) -> str:
        return self.safe()


@dataclasses.dataclass(slots=True)
class MaFile:
    account_name: str
    shared_secret: str = dataclasses.field(repr=False)
    identity_secret: str = dataclasses.field(default="", repr=False)
    steam_id: str = ""
    path: Path | None = None


