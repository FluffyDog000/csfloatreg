"""Расширения Firefox в каждом профиле.

Camoufox принимает не .xpi, а распакованный каталог с manifest.json. Поэтому
один раз скачиваем архив (xpi — это обычный zip), раскладываем в
data/addons/<имя>, а дальше все профили берут уже готовое.

Расширение — удобство, а не условие работы: если скачать не вышло, профиль
обязан подняться без него, с предупреждением в логе.
"""
from __future__ import annotations

import asyncio
import io
import shutil
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urlparse

#: Последняя версия расширения по его имени на addons.mozilla.org.
AMO_LATEST = "https://addons.mozilla.org/firefox/downloads/latest/{slug}/latest.xpi"

#: AMO отдаёт файл только «браузеру»: без узнаваемого User-Agent прилетает 403.
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0"


def is_unpacked(path: Path | str) -> bool:
    return (Path(path) / "manifest.json").is_file()


def source_url(entry: str) -> str:
    """«csgofloat» → ссылка на AMO. Полную ссылку берём как есть."""
    if entry.startswith(("http://", "https://")):
        return entry
    return AMO_LATEST.format(slug=entry.strip("/"))


def name_of(entry: str) -> str:
    """Имя папки: для имени с AMO — оно само, для ссылки — осмысленный кусок пути."""
    if not entry.startswith(("http://", "https://")):
        return entry.strip("/")
    parts = [p for p in urlparse(entry).path.split("/") if p and p != "latest.xpi"]
    return (parts[-1] if parts else urlparse(entry).netloc).removesuffix(".xpi")


def download(url: str, target: Path, *, timeout: float = 60.0) -> None:
    """Скачивает и распаковывает расширение. Любая беда — исключение с текстом."""
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 — адрес из конфига
        body = response.read()
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            archive.extractall(target)
    except zipfile.BadZipFile:
        raise RuntimeError(f"по адресу не расширение, а что-то другое ({len(body)} байт)") from None
    if not is_unpacked(target):
        raise RuntimeError("в архиве нет manifest.json")


async def ensure(entries, root: Path, log, *, timeout: float = 60.0) -> list[str]:
    """Пути к готовым расширениям. Чего нет — скачиваем, что не вышло — пропускаем."""
    ready: list[str] = []
    for raw in entries or []:
        entry = str(raw).strip()
        if not entry:
            continue

        local = Path(entry)
        if local.is_dir():                       # уже распакованное расширение рядом
            if is_unpacked(local):
                ready.append(str(local))
            else:
                log.warning("В папке расширения %s нет manifest.json — пропускаю", local)
            continue

        name = name_of(entry)
        target = Path(root) / name
        if is_unpacked(target):
            ready.append(str(target))
            continue

        try:
            target.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(download, source_url(entry), target, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 — профиль важнее расширения
            shutil.rmtree(target, ignore_errors=True)
            log.warning("Расширение %s не установилось (%s) — профиль поднимется без него", name, exc)
            continue
        log.info("Расширение %s скачано в %s", name, target)
        ready.append(str(target))
    return ready
