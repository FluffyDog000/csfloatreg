"""Привязки аккаунт → прокси и состояние пула.

Порядок строк в proxies.txt больше ничего не значит: это пул. Кто с каким
прокси работает, хранится здесь и переживает перезапуск, добавление новых
строк и перемешивание файла.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path


class BindingStore:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {"accounts": {}, "bad_proxies": [], "bad_mails": []}
        self.load()

    # ── чтение и запись ──────────────────────────────────────
    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(raw, dict):
            self.data = {
                "accounts": raw.get("accounts") or {},
                "bad_proxies": list(raw.get("bad_proxies") or []),
                "bad_mails": list(raw.get("bad_mails") or []),
            }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".bindings-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    # ── аккаунты ─────────────────────────────────────────────
    def entry(self, login: str) -> dict:
        entry = self.data["accounts"].setdefault(
            login,
            {"proxy": None, "mail": "", "status": "new", "note": "", "trade_url": "", "history": []},
        )
        for field in ("trade_url", "mail"):   # для записей, сделанных до появления поля
            entry.setdefault(field, "")
        return entry

    def proxy_of(self, login: str) -> str | None:
        return self.entry(login).get("proxy")

    def bind(self, login: str, proxy_raw: str) -> None:
        entry = self.entry(login)
        entry["proxy"] = proxy_raw
        entry["bound_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.save()

    def set_status(self, login: str, status: str, note: str = "") -> None:
        entry = self.entry(login)
        entry["status"] = status
        if note:
            entry["note"] = note
        self.save()

    def set_field(self, login: str, name: str, value: str) -> None:
        """Произвольное поле аккаунта: трейд-ссылка, заметка и всё, что добавится."""
        self.entry(login)[name] = value
        self.save()

    # ── почтовые ящики ───────────────────────────────────────
    def mail_of(self, login: str) -> str:
        return self.entry(login).get("mail") or ""

    def bind_mail(self, login: str, address: str) -> None:
        entry = self.entry(login)
        entry["mail"] = address
        entry["mail_bound_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.save()

    def used_mails(self) -> set[str]:
        return {e["mail"].lower() for e in self.data["accounts"].values() if e.get("mail")}

    def is_bad_mail(self, address: str) -> bool:
        return address.lower() in {m.lower() for m in self.data["bad_mails"]}

    def mark_bad_mail(self, address: str) -> None:
        if address and not self.is_bad_mail(address):
            self.data["bad_mails"].append(address)
            self.save()

    def free_mail(self, pool: list[str]) -> str | None:
        """Первый ящик из пула, который никому не выдан и не помечен плохим."""
        used = self.used_mails()
        for address in pool:
            if address.lower() not in used and not self.is_bad_mail(address):
                return address
        return None

    def used_proxies(self) -> set[str]:
        return {e["proxy"] for e in self.data["accounts"].values() if e.get("proxy")}

    # ── пул прокси ───────────────────────────────────────────
    def is_bad(self, proxy_raw: str) -> bool:
        return proxy_raw in self.data["bad_proxies"]

    def mark_bad(self, proxy_raw: str) -> None:
        if proxy_raw and proxy_raw not in self.data["bad_proxies"]:
            self.data["bad_proxies"].append(proxy_raw)
            self.save()

    def unmark_bad(self, proxy_raw: str) -> None:
        if proxy_raw in self.data["bad_proxies"]:
            self.data["bad_proxies"].remove(proxy_raw)
            self.save()

    def free_proxy(self, pool: list[str]) -> str | None:
        """Первый прокси из пула, который никому не выдан и не помечен плохим."""
        used = self.used_proxies()
        for raw in pool:
            if raw not in used and not self.is_bad(raw):
                return raw
        return None

    def remember_history(self, login: str, proxy_raw: str, reason: str) -> None:
        entry = self.entry(login)
        entry.setdefault("history", []).append(
            {"proxy": proxy_raw, "reason": reason, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        )
        self.save()
