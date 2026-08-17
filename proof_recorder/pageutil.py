from __future__ import annotations

import os
import time
import urllib.parse


import bot


def _normalize_url(url: str) -> str:
    url = url.strip()
    if url and "://" not in url:
        url = "https://" + url
    return url


UNKNOWN_BRAND_FOLDER = "Прочее"


def brand_for_url(url: str) -> str:
    for brand, matcher in bot.BRAND_MATCHERS.items():
        try:
            if matcher(url):
                return brand
        except Exception:
            continue
    return UNKNOWN_BRAND_FOLDER


def _next_folder_number(day_root: str) -> int:
    if not os.path.isdir(day_root):
        return 1
    nums = [int(n) for n in os.listdir(day_root) if n.isdigit()]
    return max(nums, default=0) + 1


def _wait_for_media(page, log_fn, timeout_ms: int = 4000, idle_timeout_ms: int = 1500):
    try:
        page.wait_for_load_state("load", timeout=timeout_ms)
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=idle_timeout_ms)
    except Exception:
        pass


_HEALTH_JS = """() => {
    const nav = performance.getEntriesByType('navigation')[0] || {};
    const txt = ((document.body && document.body.innerText) || '').trim();
    return {
        status: nav.responseStatus || null,
        title: document.title || '',
        textLen: txt.length,
        head: txt.slice(0, 200).replace(/\\s+/g, ' '),
        clickable: document.querySelectorAll('a,button,input').length,
        imgs: document.images.length,
        htmlLen: (document.documentElement && document.documentElement.innerHTML || '').length,
    };
}"""


def _page_health(page) -> dict:
    try:
        return page.evaluate(_HEALTH_JS) or {}
    except Exception:
        return {}


def _has_content(h: dict) -> bool:
    return bool(h) and (h.get("textLen", 0) >= 100 or h.get("clickable", 0) >= 8
                        or h.get("imgs", 0) >= 3)


def _wait_for_content(page, log_fn, timeout_ms: int = 15000) -> dict:
    deadline = time.time() + timeout_ms / 1000
    health = {}
    while True:
        health = _page_health(page)
        if _has_content(health):
            return health
        if time.time() >= deadline:
            return health
        page.wait_for_timeout(400)


URL_QUIET_MS = 2500
URL_SETTLE_MAX_MS = 20000

FORM_WAIT_MS = 10000

MIRROR_FORM_WAIT_MS = 2500        # обычное зеркало
MIRROR_FORM_WAIT_REG_MS = 8000    # адрес похож на страницу регистрации
_REG_URL_HINTS = ("registration", "register", "sign-up", "signup", "/reg")


def _mirror_form_wait_ms(url: str) -> int:
    u = (url or "").lower()
    return MIRROR_FORM_WAIT_REG_MS if any(h in u for h in _REG_URL_HINTS) else MIRROR_FORM_WAIT_MS


def _settle_url(page, log_fn, quiet_ms: int = URL_QUIET_MS,
                max_ms: int = URL_SETTLE_MAX_MS, poll_ms: int = 300) -> str:
    last = page.url
    quiet = 0
    elapsed = 0
    while elapsed < max_ms:
        try:
            page.wait_for_timeout(poll_ms)
        except Exception:
            break
        elapsed += poll_ms
        try:
            current = page.url
        except Exception:
            break
        if current == last:
            quiet += poll_ms
            if quiet >= quiet_ms:
                return last
        else:
            log_fn(f"[i] Промежуточный переход: {current[:110]}")
            quiet = 0
            last = current
    log_fn(f"[!] Адрес так и не устоялся за {max_ms // 1000} с — беру последний: "
           f"{last[:110]}")
    return last


def _await_navigation(run, page, before_url: str, what: str) -> bool:
    url = _settle_url(page, run.log)
    if _urls_same_page(url, before_url):
        return False
    run.log(f"[i] {what} увёл на другую страницу: {url[:110]} — жду её загрузки.")
    _wait_for_media(page, run.log)
    _wait_for_content(page, run.log)
    return True


def _urls_same_page(a: str, b: str) -> bool:
    def key(u):
        try:
            parts = urllib.parse.urlsplit(u or "")
        except Exception:
            return (u or "").strip("/")
        return (parts.netloc.lower().removeprefix("www."),
                parts.path.rstrip("/").lower())
    return key(a) == key(b)


def _report_empty_page(health: dict, where: str, log_fn) -> None:
    status = health.get("status")
    log_fn(f"[!] {where} не отрисовалось — страница пустая.")
    log_fn(f"[!]   HTTP-код документа: {status if status else 'неизвестен'}, "
           f"заголовок: {health.get('title', '')!r}")
    log_fn(f"[!]   текста: {health.get('textLen', 0)} символов, "
           f"кликабельных элементов: {health.get('clickable', 0)}, "
           f"картинок: {health.get('imgs', 0)}, "
           f"разметки: {health.get('htmlLen', 0)} символов")
    if health.get("head"):
        log_fn(f"[!]   начало текста: {health['head']!r}")
    if status in bot.BLOCK_STATUS_CODES:
        log_fn(f"[!]   Код {status} — это блокировка защитой сайта, а не "
               f"медленная загрузка. Меняй прокси: дело в IP.")
    elif status and status >= 400:
        log_fn(f"[!]   Код {status} — сервер отдал ошибку.")
    else:
        log_fn("[!]   Сервер ответил успешно, но содержимое не появилось: либо "
               "скрипты сайта не отработали, либо защита отдала пустой ответ.")


