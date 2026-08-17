from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime


import bot
from .capture import _grab_screenshot
from .classify import _same_site
from .config import APP_DIR, load_proof_settings
from typing import TYPE_CHECKING
if TYPE_CHECKING:                       # только аннотации; в рантайме импорт дал бы цикл
    from .pipeline import _Run
_TRANSITION_PROBE_JS = """() => {
    const b = document.body;
    return {
        url: location.href,
        ready: document.readyState,
        text: b ? (b.innerText || '').trim().length : 0,
        nodes: b ? b.getElementsByTagName('*').length : 0,
    };
}"""

TRANSITION_HOLD_MS = 900
TRANSITION_SETTLE_MS = 450
TRANSITION_SHOW_MS = 250

TRANSITION_NAV_TIMEOUT_MS = 30000
TRANSITION_BLANK_TIMEOUT_MS = 5000

TRANSITION_FALLBACK_BUDGET_S = 8.0

TRANSITION_DEBUG_DEFAULT = False
TRANSITION_DEBUG_BUDGET_S = 4.0
TRANSITION_DEBUG_EVERY_MS = 100
TRANSITION_DEBUG_DIR = os.path.join(APP_DIR, "_debug_transition")


def _transition_debug_on() -> bool:
    return bool(load_proof_settings("transition_debug", TRANSITION_DEBUG_DEFAULT))


def _safe_name(url: str, limit: int = 60) -> str:
    name = re.sub(r"[^\w.\-]+", "_", (url or "пусто")).strip("_")
    return name[:limit] or "пусто"


class _TransitionBurst:

    def __init__(self, run: _Run, rect: dict):
        self.run = run
        self.rect = rect
        self.dir = os.path.join(TRANSITION_DEBUG_DIR,
                                f"{datetime.now():%Y-%m-%d_%H-%M-%S}_{_safe_name(run.url, 40)}")
        self.t0 = time.monotonic()
        self.n = 0
        self._last = 0.0
        self._context = None
        self._watched = []
        self._pages = []

    def _ms(self) -> int:
        return int((time.monotonic() - self.t0) * 1000)

    def arm(self, context, page):
        os.makedirs(self.dir, exist_ok=True)
        self.run.log(f"[debug] Диагностика перехода включена, кадры: {self.dir}")
        self._context = context
        try:
            context.on("page", self._on_page)
        except Exception:
            pass
        self._watch(page)

    def _on_page(self, pg):
        self.run.log(f"[debug] +{self._ms():>5} мс  открылась вкладка")
        self._pages.append(pg)
        self._watch(pg)

    def _watch(self, pg):
        try:
            pg.on("framenavigated", self._on_nav)
            self._watched.append(pg)
        except Exception:
            pass

    def _on_nav(self, frame):
        try:
            main = frame.parent_frame is None
            url = frame.url or ""
        except Exception:
            return
        self.run.log(f"[debug] +{self._ms():>5} мс  коммит "
                     f"{'главного фрейма' if main else 'вложенного фрейма'}: {url}")

    def start(self):
        self.t0 = time.monotonic()
        self._last = 0.0

    def tick(self, page=None):
        now = time.monotonic()
        if (now - self._last) * 1000 < TRANSITION_DEBUG_EVERY_MS:
            return
        if (now - self.t0) > TRANSITION_DEBUG_BUDGET_S:
            return
        self._last = now
        pg = self._pages[-1] if self._pages else page
        try:
            url = pg.url if pg else ""
        except Exception:
            url = ""
        self.n += 1
        path = os.path.join(self.dir, f"{self.n:03d}_{self._ms():05d}ms_{_safe_name(url)}.png")
        _grab_screenshot(path, self.rect, lambda *a: None)

    def run_out(self, page):
        while (time.monotonic() - self.t0) <= TRANSITION_DEBUG_BUDGET_S:
            self.tick(page)
            time.sleep(TRANSITION_DEBUG_EVERY_MS / 2000)

    def finish(self):
        for target, event, handler in (
                (self._context, "page", self._on_page),
                *[(pg, "framenavigated", self._on_nav) for pg in self._watched]):
            try:
                if target is not None:
                    target.remove_listener(event, handler)
            except Exception:
                pass
        self._watched, self._context = [], None
        self.run.log(f"[debug] Снято кадров: {self.n}. Смотри {self.dir} — "
                     f"в имени файла время от клика и адрес, который в этот момент "
                     f"видит Playwright.")


def _is_yandex_domain(domain: str) -> bool:
    d = (domain or "").lower()
    return d == "ya.ru" or "yandex." in d


class _TransitionHold:

    def __init__(self, run: _Run, url: str, path: str, rect: dict, referer: str = ""):
        self.run = run
        self.url = url
        self.domain = bot.extract_domain(url)
        self.path = path
        self.rect = rect
        self.referer = referer
        self.context = None
        self.ok = False
        self.popup = None           # вкладка, открывшаяся от клика
        self._done = False
        self._watched = []

    def takeover(self, popped, page) -> bool:
        if self.ok:
            return True             # запасной путь уже успел снять кадр
        pg = popped or self.popup or page
        if pg is None:
            return False
        landed = self._landed_url(pg)
        self.run.log(f"[*] Веду вкладку на {self.url} — чтобы адрес встал в "
                     f"строку, и снимаю кадр перехода.")
        try:
            pg.goto("about:blank", timeout=TRANSITION_BLANK_TIMEOUT_MS)
        except Exception as e:
            self.run.log(f"[i] Очистить вкладку не вышло: "
                         f"{str(e).splitlines()[0][:80]}")
        grab = threading.Thread(target=self._delayed_grab, daemon=True,
                                name="transition-shot")
        grab.start()
        try:
            pg.goto(self.url, referer=self.referer or None, wait_until="commit",
                    timeout=TRANSITION_SETTLE_MS + TRANSITION_SHOW_MS)
        except Exception:
            pass
        grab.join(timeout=TRANSITION_SETTLE_MS / 1000 + 5)
        self.ok = os.path.isfile(self.path) and os.path.getsize(self.path) > 0
        self.run.log(f"[+] {os.path.basename(self.path)} снят на переходе"
                     if self.ok else "[!] Кадр перехода снять не удалось")

        if landed:
            self.run.log("[*] Кадр готов — возвращаю вкладку на зеркало, чтобы "
                         "пустая страница не висела.")
            try:
                pg.goto(landed, wait_until="commit", timeout=TRANSITION_NAV_TIMEOUT_MS)
            except Exception as e:
                self.run.log(f"[i] Возврат на зеркало: "
                             f"{str(e).splitlines()[0][:120]}")
        return self.ok

    def _landed_url(self, pg) -> str:
        try:
            url = pg.url or ""
        except Exception:
            return ""
        if not url.startswith("http"):
            return ""
        domain = bot.extract_domain(url)
        if not domain or _same_site(domain, self.domain) or _is_yandex_domain(domain):
            return ""
        return url

    def _delayed_grab(self):
        time.sleep(TRANSITION_SETTLE_MS / 1000)
        try:
            _grab_screenshot(self.path, self.rect, self.run.log)
        except Exception as e:
            self.run.log(f"[!] Кадр перехода не снялся: {e}")

    def _on_page(self, pg):
        if self.popup is None:
            self.popup = pg
        self._watch(pg)

    def _watch(self, pg):
        try:
            pg.on("framenavigated", self._on_nav)
            self._watched.append(pg)
        except Exception:
            pass

    def _on_nav(self, frame):
        if self._done or self.ok:
            return
        try:
            if frame.parent_frame is not None:   # только главный фрейм
                return
            url = frame.url or ""
        except Exception:
            return
        domain = bot.extract_domain(url)
        if not domain or not _same_site(domain, self.domain):
            return
        self._done = True   # снимаем ровно один раз
        try:
            self._shoot(url)
        except Exception as e:
            self.run.log(f"[!] Кадр перехода не снялся: {e}")

    def _shoot(self, url: str):
        self.run.log(f"[*] Открылся документ {url} — снимаю кадр перехода, пока "
                     f"страница пустая (держу {TRANSITION_HOLD_MS} мс).")
        time.sleep(TRANSITION_SETTLE_MS / 1000)
        _grab_screenshot(self.path, self.rect, self.run.log)
        self.ok = os.path.isfile(self.path) and os.path.getsize(self.path) > 0
        self.run.log(f"[+] {os.path.basename(self.path)} снят на переходе"
                     if self.ok else "[!] Кадр перехода снять не удалось")
        rest = (TRANSITION_HOLD_MS - TRANSITION_SETTLE_MS) / 1000
        if rest > 0:
            time.sleep(rest)

    def arm(self, context, page) -> bool:
        if not self.domain:
            self.run.log("[!] Из ссылки не выделился домен — кадр перехода не подготовить")
            return False
        try:
            context.on("page", self._on_page)
            self._watch(page)          # на случай перехода в той же вкладке
        except Exception as e:
            self.run.log(f"[!] Не удалось подготовить кадр перехода: {e}")
            return False
        self.context = context
        return True

    def disarm(self):
        if self.context is None:
            return
        for target, event, handler in (
                (self.context, "page", self._on_page),
                *[(pg, "framenavigated", self._on_nav) for pg in self._watched]):
            try:
                target.remove_listener(event, handler)
            except Exception:
                pass
        self._watched = []
        self.context = None


def _shoot_transition_fallback(run: _Run, page, popped, name: str) -> bool:
    want = bot.extract_domain(run.url)
    deadline = time.monotonic() + TRANSITION_FALLBACK_BUDGET_S
    while time.monotonic() < deadline:
        pg = popped or page
        try:
            info = pg.evaluate(_TRANSITION_PROBE_JS)
        except Exception:
            info = None
        url = (info or {}).get("url") or ""
        if not url or url.startswith("about:"):
            try:
                url = pg.url
            except Exception:
                url = ""
        domain = bot.extract_domain(url)
        if domain and _same_site(domain, want):
            if run.shot(name, pg):
                text_len = (info or {}).get("text", 0)
                state = ("страница ещё пустая" if text_len <= 40
                         else f"страница уже отрисовалась, {text_len} символов")
                run.log(f"[i] Кадр перехода снят запасным путём на {domain} ({state})")
                return True
        try:
            pg.wait_for_timeout(60)
        except Exception:
            time.sleep(0.06)
    run.log(f"[!] Кадр перехода снять не удалось: {want} в адресной строке так и не появился")
    return False


