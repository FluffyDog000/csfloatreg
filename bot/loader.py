"""Чтение входных файлов: accounts.txt, proxies.txt, mafiles/."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .errors import LoaderError
from .logging_setup import register_secret
from .models import Account, Bundle, MaFile, Proxy

_KNOWN_SCHEMES = ("http", "https", "socks5", "socks5h", "socks4")


def _clean_lines(path: Path) -> list[tuple[int, str]]:
    if not path.exists():
        raise LoaderError(f"Не найден файл: {path}")
    out: list[tuple[int, str]] = []
    with path.open("r", encoding="utf-8-sig", errors="replace") as fh:
        for i, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("//"):
                continue
            out.append((i, line))
    return out


# ── accounts.txt ─────────────────────────────────────────────
def load_accounts(path: Path, *, delimiter: str = ":") -> list[Account]:
    accounts: list[Account] = []
    seen: set[str] = set()
    for line_no, line in _clean_lines(path):
        parts = line.split(delimiter)
        if len(parts) != 4:
            raise LoaderError(
                f"{path.name}:{line_no}: ожидается login{delimiter}pass{delimiter}mail"
                f"{delimiter}mailpassword, получено полей: {len(parts)}"
            )
        login, password, mail, mail_password = (p.strip() for p in parts)
        if not login or not password:
            raise LoaderError(f"{path.name}:{line_no}: пустой логин или пароль")
        if login.lower() in seen:
            raise LoaderError(f"{path.name}:{line_no}: дубликат логина {login}")
        seen.add(login.lower())
        register_secret(password, mail_password)
        accounts.append(Account(login, password, mail, mail_password, line_no))
    if not accounts:
        raise LoaderError(f"{path}: не найдено ни одного аккаунта")
    return accounts


# ── proxies.txt ──────────────────────────────────────────────
def parse_proxy(line: str, *, default_scheme: str = "http", line_no: int = 0) -> Proxy:
    """Автодетект формата.

    Понимает:
      scheme://user:pass@host:port | scheme://host:port | scheme://host:port:user:pass
      user:pass@host:port | host:port:user:pass | host:port
    """
    raw = line.strip()
    scheme = default_scheme
    if "://" in raw:
        scheme, raw = raw.split("://", 1)
        scheme = scheme.lower()
        if scheme not in _KNOWN_SCHEMES:
            raise LoaderError(f"строка {line_no}: неизвестная схема прокси '{scheme}'")

    user = password = None
    if "@" in raw:
        creds, hostpart = raw.rsplit("@", 1)
        if ":" in creds:
            user, password = creds.split(":", 1)
        else:
            user = creds
        parts = hostpart.split(":")
    else:
        parts = raw.split(":")
        if len(parts) == 4:
            parts, user, password = parts[:2], parts[2], parts[3]
        elif len(parts) != 2:
            raise LoaderError(
                f"строка {line_no}: не разобрал прокси '{line}'. "
                f"Ожидается host:port[:user:pass] или scheme://user:pass@host:port"
            )

    if len(parts) != 2:
        raise LoaderError(f"строка {line_no}: не разобрал host:port в '{line}'")
    host, port_raw = parts[0].strip(), parts[1].strip()
    if not host:
        raise LoaderError(f"строка {line_no}: пустой хост в '{line}'")
    if not port_raw.isdigit() or not (0 < int(port_raw) < 65536):
        raise LoaderError(f"строка {line_no}: некорректный порт '{port_raw}' в '{line}'")

    register_secret(password)
    return Proxy(scheme, host, int(port_raw), user or None, password, line_no, raw=line.strip())


def load_proxies(path: Path, *, default_scheme: str = "http") -> list[Proxy]:
    """Список прокси.

    Для очереди регистрации это строки по порядку (1 прокси = 1 аккаунт), для
    менеджера профилей — пул, из которого раздаются привязки. Дубликаты строк
    отбрасываем: в пуле они только мешают, а в очереди означают один и тот же
    выход в сеть у двух аккаунтов.
    """
    proxies: list[Proxy] = []
    seen: set[str] = set()
    for no, line in _clean_lines(path):
        proxy = parse_proxy(line, default_scheme=default_scheme, line_no=no)
        if proxy.raw in seen:
            continue
        seen.add(proxy.raw)
        proxies.append(proxy)
    if not proxies:
        raise LoaderError(f"{path}: не найдено ни одного прокси")
    return proxies


# ── mafiles/ ─────────────────────────────────────────────────
def load_mafiles(directory: Path) -> dict[str, MaFile]:
    """Индекс по account_name ВНУТРИ файла, а не по имени файла."""
    if not directory.exists():
        raise LoaderError(f"Не найдена папка maFile'ов: {directory}")

    index: dict[str, MaFile] = {}
    candidates = [p for p in sorted(directory.rglob("*")) if p.is_file()]
    for p in candidates:
        if p.suffix.lower() not in (".mafile", ".json", ""):
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig", errors="replace"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue

        session = data.get("Session") or data.get("session") or {}
        account_name = (
            data.get("account_name")
            or data.get("AccountName")
            or (session.get("AccountName") if isinstance(session, dict) else None)
        )
        shared = data.get("shared_secret") or data.get("SharedSecret")
        if not account_name:
            continue
        if not shared:
            # зашифрованный maFile (SDA с паролем) — отличаем от битого
            if data.get("encryption_iv") or data.get("encryption_salt"):
                index[str(account_name).lower()] = MaFile(str(account_name), "", "", "", p)
            continue

        steam_id = str(
            data.get("steamid")
            or data.get("SteamID")
            or (session.get("SteamID") if isinstance(session, dict) else "")
            or ""
        )
        mafile = MaFile(
            account_name=str(account_name),
            shared_secret=str(shared),
            identity_secret=str(data.get("identity_secret") or data.get("IdentitySecret") or ""),
            steam_id=steam_id,
            device_id=str(data.get("device_id") or data.get("DeviceID") or ""),
            path=p,
        )
        register_secret(mafile.shared_secret, mafile.identity_secret)
        index[mafile.account_name.lower()] = mafile
    return index


# ── связывание ───────────────────────────────────────────────
def build_bundles(
    accounts: list[Account], proxies: list[Proxy], mafiles: dict[str, MaFile]
) -> list[Bundle]:
    """1 прокси = 1 аккаунт, привязка по порядку строк."""
    if len(proxies) < len(accounts):
        raise LoaderError(
            f"Прокси меньше, чем аккаунтов: {len(proxies)} < {len(accounts)}. "
            f"Правило '1 прокси = 1 аккаунт' нарушено — добавьте "
            f"{len(accounts) - len(proxies)} строк в proxies.txt."
        )

    bundles: list[Bundle] = []
    for account, proxy in zip(accounts, proxies):
        mafile = mafiles.get(account.login.lower())
        error = None
        if mafile is None:
            error = f"maFile с account_name='{account.login}' не найден"
        elif not mafile.shared_secret:
            error = f"maFile {mafile.path.name if mafile.path else '?'} зашифрован (нет shared_secret)"
        bundles.append(Bundle(account=account, proxy=proxy, mafile=mafile, error=error))
    return bundles


def load_all(cfg) -> list[Bundle]:
    accounts = load_accounts(cfg.path_for("accounts"))
    proxies = load_proxies(
        cfg.path_for("proxies"), default_scheme=cfg.get("proxy.default_scheme", "http")
    )
    mafiles = load_mafiles(cfg.path_for("mafiles"))
    return build_bundles(accounts, proxies, mafiles)
