from __future__ import annotations

import re

import requests

import bot
from .pageutil import _normalize_url
LINK_LANDING = "лендинг"
LINK_REDIRECT = "редирект"

_META_REFRESH_RE = re.compile(
    r'<meta[^>]+http-equiv\s*=\s*["\']?refresh["\']?[^>]*content\s*=\s*["\'][^"\']*url\s*=\s*([^"\'\s>]+)',
    re.I)
_JS_REDIRECT_RE = re.compile(
    r'(?:location\s*\.\s*(?:href|replace|assign)\s*(?:=|\()\s*|location\s*=\s*)["\'](https?://[^"\']+)["\']',
    re.I)


def _same_site(a: str, b: str) -> bool:
    a = (a or "").removeprefix("www.").lower()
    b = (b or "").removeprefix("www.").lower()
    if not a or not b:
        return False
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def _redirect_in_body(html: str, base_domain: str) -> str:
    head = (html or "")[:8000]
    for rx in (_META_REFRESH_RE, _JS_REDIRECT_RE):
        m = rx.search(head)
        if not m:
            continue
        target = bot.extract_domain(_normalize_url(m.group(1).strip("'\" ")))
        if target and not _same_site(target, base_domain):
            return target
    return ""


def _classify_link(url: str, proxy_attempts: list, log_fn) -> tuple:
    domain = bot.extract_domain(url)
    headers = bot._build_headers(bot._realistic_referer(bot.ALIVE_REFERERS[0], domain))
    last_err = ""
    for proxy in proxy_attempts:
        proxies = {"http": proxy, "https": proxy} if proxy else None
        try:
            resp = requests.get(url, headers=headers, proxies=proxies, verify=False,
                                allow_redirects=True, stream=True,
                                timeout=(8, bot.ALIVE_TIMEOUT))
        except requests.exceptions.RequestException as e:
            last_err = type(e).__name__
            log_fn(f"[i] Проба типа ссылки не прошла через "
                   f"{bot._proxy_label(proxy) if proxy else 'прямое соединение'}: {last_err}")
            continue

        try:
            status = resp.status_code
            if status in (404, 410) or status >= 500:
                return None, f"сервер отдал {status}", resp.url
            if status in bot.BLOCK_STATUS_CODES:
                log_fn(f"[i] Тип ссылки: РЕДИРЕКТ (проба получила {status} — напрямую "
                       f"защита не пускает, пойдём через выдачу Яндекса)")
                return LINK_REDIRECT, "", resp.url

            final_domain = bot.extract_domain(resp.url)
            if final_domain and not _same_site(final_domain, domain):
                log_fn(f"[i] Тип ссылки: РЕДИРЕКТ ({domain} → {final_domain}, "
                       f"цепочка из {len(resp.history)} переходов)")
                return LINK_REDIRECT, "", resp.url

            try:
                head = resp.raw.read(8192, decode_content=True).decode(
                    resp.encoding or "utf-8", errors="ignore")
            except Exception:
                head = ""
            body_target = _redirect_in_body(head, domain)
            if body_target:
                log_fn(f"[i] Тип ссылки: РЕДИРЕКТ (страница сама уводит на {body_target})")
                return LINK_REDIRECT, "", resp.url

            log_fn(f"[i] Тип ссылки: ЛЕНДИНГ (домен {domain} остался своим, ответ {status})")
            return LINK_LANDING, "", resp.url
        finally:
            resp.close()

    return None, f"сайт не отвечает ни через один прокси ({last_err or 'нет ответа'})", ""


