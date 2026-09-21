"""Отладочный режим: пауза на каждом шаге + выгрузка кандидатов в селекторы.

    python main.py --debug --headful --only <login>

На паузе:
    Enter — дальше
    d     — выгрузить кандидатов (JSON) + HTML + скриншот в debug/<login>/
    s     — только скриншот
    p     — Playwright Inspector (page.pause())
    c     — доработать аккаунт без пауз
    q     — прервать аккаунт
"""
from __future__ import annotations

import asyncio
import json
import time

from .errors import BotError

_COLLECT_JS = """
() => {
  const out = [];
  const sels = 'input, button, a, select, textarea, [role=button], [role=option], [contenteditable=true]';
  document.querySelectorAll(sels).forEach((el, i) => {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return;
    const st = window.getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || st.opacity === '0') return;
    out.push({
      i,
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type') || '',
      id: el.id || '',
      name: el.getAttribute('name') || '',
      cls: (el.getAttribute('class') || '').slice(0, 120),
      testid: el.getAttribute('data-testid') || el.getAttribute('data-test') || '',
      aria: el.getAttribute('aria-label') || '',
      placeholder: el.getAttribute('placeholder') || '',
      href: (el.getAttribute('href') || '').slice(0, 160),
      text: (el.innerText || el.value || '').trim().slice(0, 80),
      box: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)]
    });
  });
  return {url: location.href, title: document.title, frames: window.frames.length, elements: out};
}
"""


def _suggest(el: dict) -> str:
    """Черновой селектор-кандидат для человека, который будет править selectors.yaml."""
    if el["testid"]:
        return f"[data-testid='{el['testid']}']"
    if el["id"]:
        return f"#{el['id']}"
    if el["name"]:
        return f"{el['tag']}[name='{el['name']}']"
    if el["aria"]:
        return f"{el['tag']}[aria-label='{el['aria']}']"
    if el["placeholder"]:
        return f"{el['tag']}[placeholder='{el['placeholder']}']"
    if el["text"] and el["tag"] in ("button", "a"):
        return f"{el['tag']}:has-text('{el['text'][:30]}')"
    if el["type"]:
        return f"{el['tag']}[type='{el['type']}']"
    return el["tag"]


class Debugger:
    def __init__(self, cfg, logger, artifacts, *, enabled: bool = True):
        self.cfg = cfg
        self.log = logger
        self.artifacts = artifacts
        self.enabled = enabled
        self.skip_rest = False

    async def before(self, ctx, step: str, title: str = "") -> None:
        if not self.enabled or self.skip_rest:
            return
        await self._prompt(ctx, f"ПЕРЕД шагом '{step}'" + (f" — {title}" if title else ""))

    async def after(self, ctx, step: str) -> None:
        if not self.enabled or self.skip_rest:
            return
        await self._prompt(ctx, f"ПОСЛЕ шага '{step}'")

    async def _prompt(self, ctx, headline: str) -> None:
        page = await self._active_page(ctx)
        url = getattr(page, "url", "—")
        banner = (
            f"\n{'─' * 70}\n"
            f"  [{ctx.login}] {headline}\n"
            f"  URL: {url}\n"
            f"  Enter — дальше | d — дамп кандидатов | s — скриншот | p — inspector | c — без пауз | q — прервать\n"
            f"{'─' * 70}"
        )
        print(banner, flush=True)
        while True:
            answer = (await asyncio.to_thread(input, "  > ")).strip().lower()
            if answer in ("", "n", "next"):
                return
            if answer == "c":
                self.skip_rest = True
                self.log.info("Отладка: дальше без пауз")
                return
            if answer == "q":
                raise BotError("прервано вручную в отладочном режиме", stage=ctx.stage)
            if answer == "s":
                await ctx.dump(f"debug_{ctx.stage}", debug=True)
                continue
            if answer == "d":
                await self.collect(ctx, page)
                continue
            if answer == "p":
                if page is not None:
                    self.log.info("Открываю Playwright Inspector — закрой его, чтобы продолжить")
                    await page.pause()
                continue
            print("  ? неизвестная команда", flush=True)

    async def collect(self, ctx, page=None) -> dict | None:
        """Выгружает интерактивные элементы страницы и iframe'ов в debug/<login>/*.json."""
        page = page or await self._active_page(ctx)
        if page is None:
            self.log.warning("Нет открытой страницы для дампа")
            return None

        report: dict = {"step": ctx.stage, "module": ctx.module, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
        try:
            main = await page.evaluate(_COLLECT_JS)
        except Exception as exc:  # noqa: BLE001
            self.log.warning("Не удалось собрать элементы: %s", exc)
            return None

        for el in main["elements"]:
            el["suggest"] = _suggest(el)
        report["main"] = main

        frames = []
        for frame in page.frames[1:]:
            try:
                data = await frame.evaluate(_COLLECT_JS)
            except Exception:  # noqa: BLE001 — cross-origin iframe читать нельзя, это нормально
                frames.append({"url": frame.url, "error": "недоступен (cross-origin)"})
                continue
            for el in data["elements"]:
                el["suggest"] = _suggest(el)
            data["frame_url"] = frame.url
            frames.append(data)
        report["frames"] = frames

        folder = self.artifacts.dir_for(ctx.login, debug=True)
        name = f"{time.strftime('%H%M%S')}_{ctx.module or 'step'}_{ctx.stage or 'page'}"
        path = folder / f"{name}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        await ctx.dump(f"debug_{ctx.stage}", debug=True)

        print(f"\n  Кандидаты ({len(main['elements'])} элементов) -> {path}")
        for el in main["elements"][:25]:
            label = el["text"] or el["aria"] or el["placeholder"] or el["name"] or el["id"]
            print(f"    {el['tag']:<9} {el['suggest']:<46} {label[:40]}")
        if len(main["elements"]) > 25:
            print(f"    … ещё {len(main['elements']) - 25}, полный список в JSON")
        print(flush=True)
        return report

    @staticmethod
    async def _active_page(ctx):
        pages = getattr(ctx.session, "_pages", {}) or {}
        if not pages:
            return None
        return list(pages.values())[-1]
