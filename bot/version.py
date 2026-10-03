"""Какой код сейчас запущен. Нужно, чтобы не чинить то, что уже починено."""
from __future__ import annotations


def code_version(root) -> str:
    """Короткий идентификатор запущенного кода: ветка и последний коммит."""
    import subprocess

    try:
        # только хеш и дата: заголовок коммита ломается в консоли Windows
        out = subprocess.run(
            ["git", "log", "-1", "--format=%h %ad", "--date=short"], cwd=str(root),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5, check=True,
        ).stdout.strip()
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(root),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5, check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "config.yaml", "selectors.yaml"], cwd=str(root),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5, check=True,
        ).stdout.strip()
        suffix = f" | ЛОКАЛЬНО ИЗМЕНЕНЫ: {dirty.replace(chr(10), ', ')}" if dirty else ""
        return f"{branch} @ {out}{suffix}"
    except Exception:  # noqa: BLE001 — без git тоже должно работать
        return "версия неизвестна (git недоступен)"
