"""Раздача почтовых ящиков аккаунтам.

Почта живёт в отдельном файле (`mails.txt`, формат `mail:password`), а не в
accounts.txt: ящики покупаются пачкой и к Steam-аккаунтам отношения не имеют.
Кому какой ящик достался, помнит data/bindings.json — та же память, что и для
прокси, поэтому привязка переживает перезапуск и перемешивание файла.
"""
from __future__ import annotations

from .bindings import BindingStore
from .errors import LoaderError
from .logging_setup import get_logger
from .models import Account, Mailbox


def mail_source(cfg) -> str:
    """`mails_file` (по умолчанию) или `accounts` — почта из старого формата."""
    return str(cfg.get("mail.source") or "mails_file").lower()


def attach_mailboxes(
    cfg,
    accounts: list[Account],
    *,
    bindings: BindingStore | None = None,
    pool: list[Mailbox] | None = None,
    log=None,
) -> dict:
    """Проставляет каждому аккаунту его ящик. Возвращает сводку для интерфейса."""
    log = log or get_logger()
    if mail_source(cfg) == "accounts":
        return {"source": "accounts", "assigned": 0, "missing": [], "total": 0}

    from .loader import load_mails

    if pool is None:
        pool = load_mails(cfg.path_for("mails"))
    if bindings is None:
        bindings = BindingStore(cfg.path_for("data") / "bindings.json")

    by_address = {box.address.lower(): box for box in pool}
    addresses = [box.address for box in pool]
    assigned, missing = 0, []

    for account in accounts:
        current = bindings.mail_of(account.login)
        box = by_address.get(current.lower()) if current else None
        if box is not None and bindings.is_bad_mail(box.address):
            box = None
        if box is None:
            free = bindings.free_mail(addresses)
            if free is None:
                missing.append(account.login)
                account.mail = ""
                account.mail_password = ""
                continue
            box = by_address[free.lower()]
            bindings.bind_mail(account.login, box.address)
            assigned += 1
        # почта из accounts.txt намеренно игнорируется: источник — mails.txt
        account.mail = box.address
        account.mail_password = box.password

    if assigned:
        log.info("Выдано почт: %d", assigned)
    if missing:
        log.warning(
            "Почт не хватило на %d аккаунт(ов): %s%s",
            len(missing), ", ".join(missing[:5]), " …" if len(missing) > 5 else "",
        )
    return {"source": "mails_file", "assigned": assigned, "missing": missing, "total": len(pool)}


def mail_stats(bindings: BindingStore, pool: list[Mailbox]) -> dict:
    addresses = {box.address.lower() for box in pool}
    used = bindings.used_mails() & addresses
    bad = {m.lower() for m in bindings.data.get("bad_mails") or []} & addresses
    return {
        "total": len(addresses),
        "used": len(used),
        "bad": len(bad),
        "free": len(addresses - used - bad),
    }


def replace_mailbox(login: str, bindings: BindingStore, pool: list[Mailbox], *, mark_bad: bool = True) -> Mailbox:
    """Выдать аккаунту другой ящик. Старый уходит в плохие и больше не выдаётся."""
    # сначала убеждаемся, что замена есть: иначе неудачная попытка испортила бы
    # текущий ящик — пометила плохим и оставила аккаунт с ним же
    free = bindings.free_mail([box.address for box in pool])
    if free is None:
        raise LoaderError("в пуле не осталось свободных почт")

    current = bindings.mail_of(login)
    if current and mark_bad:
        bindings.mark_bad_mail(current)
        bindings.remember_history(login, current, "почта заменена вручную")
    bindings.bind_mail(login, free)
    return next(box for box in pool if box.address.lower() == free.lower())
