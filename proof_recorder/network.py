from __future__ import annotations

import json
import os
import re
import time

import requests
from playwright.sync_api import sync_playwright

import bot
from . import config
from .config import APP_DIR, YANDEX_DIRECT_HOSTS, current_profile_dir, load_proof_settings, save_proof_settings
def _proxy_dict_for(proxy_raw: str, log_fn=None):
    proxy_dict = bot._playwright_proxy_dict(proxy_raw)
    if proxy_dict and config.YANDEX_BYPASS_PROXY:
        proxy_dict["bypass"] = ",".join(YANDEX_DIRECT_HOSTS)
        if log_fn:
            log_fn("[i] Яндекс идёт мимо прокси, напрямую с этой машины; "
                   "сайты — через прокси.")
    return proxy_dict


COOKIES_FILE = os.path.join(APP_DIR, "cookies.json")

_SAMESITE_MAP = {"no_restriction": "None", "none": "None", "lax": "Lax", "strict": "Strict"}


def _convert_cookie(c: dict):
    name, value = c.get("name"), c.get("value")
    domain, path = c.get("domain"), c.get("path") or "/"
    if not name or value is None or not domain:
        return None
    out = {"name": str(name), "value": str(value), "domain": str(domain), "path": str(path),
           "httpOnly": bool(c.get("httpOnly")), "secure": bool(c.get("secure"))}
    if c.get("hostOnly") and out["domain"].startswith("."):
        out["domain"] = out["domain"][1:]
    exp = c.get("expires", c.get("expirationDate"))
    if exp and not c.get("session"):
        try:
            out["expires"] = int(float(exp))
        except (TypeError, ValueError):
            pass
    ss = _SAMESITE_MAP.get(str(c.get("sameSite", "")).lower())
    if ss:
        if ss != "None" or out["secure"]:
            out["sameSite"] = ss
    return out


def _inject_saved_cookies(context, log_fn) -> int:
    if not os.path.isfile(COOKIES_FILE):
        return 0
    try:
        with open(COOKIES_FILE, encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception as e:
        log_fn(f"[!] Не удалось прочитать {os.path.basename(COOKIES_FILE)}: {e}")
        return 0
    if isinstance(data, dict):
        data = data.get("cookies") or []
    if not isinstance(data, list):
        log_fn(f"[!] {os.path.basename(COOKIES_FILE)}: ожидался список кук — пропускаю.")
        return 0
    cookies = [x for x in (_convert_cookie(c) for c in data if isinstance(c, dict)) if x]
    if not cookies:
        log_fn(f"[!] {os.path.basename(COOKIES_FILE)}: годных записей не нашлось.")
        return 0
    try:
        context.add_cookies(cookies)
    except Exception as e:
        log_fn(f"[!] Куки применить не удалось: {e}")
        return 0
    domains = sorted({c["domain"].lstrip(".") for c in cookies})
    log_fn(f"[i] Подложил {len(cookies)} кук из {os.path.basename(COOKIES_FILE)} "
           f"для доменов: {', '.join(domains[:6])}"
           + (" и др." if len(domains) > 6 else ""))
    return len(cookies)


GUARD_COOKIE_PREFIXES = ("__ddg", "ddg", "cf_clearance", "__cf_bm", "_cfuvid",
                         "__cflb", "cf_chl", "__cf_")

_GUARD_PROXY_KEY = "guard_cookie_proxy"


def _is_guard_cookie(name: str) -> bool:
    n = (name or "").lower()
    return any(n.startswith(pref) for pref in GUARD_COOKIE_PREFIXES)


def _host_matches(cookie_domain: str, host: str) -> bool:
    cd = (cookie_domain or "").lstrip(".").lower()
    h = (host or "").lstrip(".").lower()
    if not cd or not h:
        return False
    return cd == h or cd.endswith("." + h) or h.endswith("." + cd)


def _drop_cookies(context, log_fn, host: str = "", guard_only: bool = True) -> int:
    try:
        cookies = context.cookies()
    except Exception as e:
        log_fn(f"[!] Не удалось прочитать куки профиля: {e}")
        return 0
    doomed, keep = [], []
    for c in cookies:
        hit = (not host or _host_matches(c.get("domain", ""), host)) and \
              (not guard_only or _is_guard_cookie(c.get("name", "")))
        (doomed if hit else keep).append(c)
    if not doomed:
        return 0
    try:
        context.clear_cookies()
        if keep:
            context.add_cookies(keep)
    except Exception as e:
        log_fn(f"[!] Не удалось почистить куки: {e}")
        return 0
    names = sorted({c.get("name", "?") for c in doomed})
    log_fn(f"[i] Выбросил {len(doomed)} кук: {', '.join(names[:8])}"
           + (" и др." if len(names) > 8 else ""))
    return len(doomed)


def log_site_cookies(context, host: str, log_fn) -> None:
    try:
        mine = [c for c in context.cookies() if _host_matches(c.get("domain", ""), host)]
    except Exception:
        return
    if not mine:
        log_fn(f"[i] Кук {host} в профиле нет — придём как чистый браузер.")
        return
    guard = sorted(c.get("name", "?") for c in mine if _is_guard_cookie(c.get("name", "")))
    log_fn(f"[i] Кук {host} в профиле: {len(mine)}"
           + (f", из них проверка защиты: {', '.join(guard)}" if guard else ""))


def _site_origins(host: str) -> list:
    host = (host or "").strip().lstrip(".").lower()
    if not host:
        return []
    hosts = {host}
    hosts.add(host[4:] if host.startswith("www.") else "www." + host)
    return [f"{scheme}://{h}" for h in sorted(hosts) for scheme in ("https", "http")]


def _clear_site_data(context, host: str, log_fn) -> bool:
    removed = _drop_cookies(context, log_fn, host=host, guard_only=False)
    try:
        page = context.pages[0] if context.pages else context.new_page()
        cdp = context.new_cdp_session(page)
    except Exception as e:
        log_fn(f"[!] Кэш и service worker почистить не удалось ({e}) — куки убраны, "
               f"но сайт может по-прежнему отвечать из своего хранилища.")
        return bool(removed)
    for origin in _site_origins(host):
        try:
            cdp.send("Storage.clearDataForOrigin",
                     {"origin": origin, "storageTypes": "all"})
        except Exception:
            pass
    try:
        cdp.send("Network.clearBrowserCache")
    except Exception:
        pass
    log_fn(f"[i] {host}: убраны куки ({removed}), кэш, service worker и хранилища.")
    return True


_MY_IP_TTL_S = 300
_my_ip_cache = {"ip": "", "at": 0.0}


def _network_mark(proxy_raw: str, log_fn) -> str:
    if proxy_raw:
        return bot._proxy_label(proxy_raw)
    if _my_ip_cache["ip"] and time.time() - _my_ip_cache["at"] < _MY_IP_TTL_S:
        return f"прямой {_my_ip_cache['ip']}"
    try:
        ip = (requests.get(bot.PROXY_HEALTH_CHECK_URL, timeout=6).json() or {}).get("ip", "")
    except Exception as e:
        log_fn(f"[i] Внешний IP узнать не удалось ({str(e).splitlines()[0][:60]}) — "
               f"куки защиты оставляю как есть.")
        return ""
    if not ip:
        return ""
    _my_ip_cache.update(ip=ip, at=time.time())
    return f"прямой {ip}"


def _drop_stale_guard_cookies(context, proxy_raw: str, log_fn) -> int:
    mark = _network_mark(proxy_raw, log_fn)
    if not mark:
        return 0
    was = load_proof_settings(_GUARD_PROXY_KEY, "")
    if was == mark:
        return 0
    if was:
        log_fn(f"[i] Адрес сменился ({was} -> {mark}) — куки проверки "
               f"DDoS-Guard/Cloudflare привязаны к прежнему адресу и сейчас "
               f"только мешают, выбрасываю их.")
    n = _drop_cookies(context, log_fn, guard_only=True)
    if n:
        try:
            page = context.pages[0] if context.pages else context.new_page()
            context.new_cdp_session(page).send("Network.clearBrowserCache")
        except Exception:
            pass
    save_proof_settings(**{_GUARD_PROXY_KEY: mark})
    return n


def reset_site_data(domain: str, log_fn) -> int:
    from .browser import _close_browser, _launch_browser
    domain = (domain or "").strip().lower()
    domain = re.sub(r"^\w+://", "", domain).split("/")[0].lstrip(".")
    if not domain:
        log_fn("[!] Не указан домен — нечего чистить.")
        return 0
    log_fn(f"[*] Чищу {domain} в профиле {os.path.basename(current_profile_dir())}: "
           f"куки, кэш, service worker, хранилища...")
    browser = context = None
    ok = False
    with sync_playwright() as p:
        try:
            browser, context = _launch_browser(p, None, log_fn)
            ok = _clear_site_data(context, domain, log_fn)
        except Exception as e:
            log_fn(f"[!] Ошибка: {str(e).splitlines()[0][:90]}")
        finally:
            _close_browser(browser, context)
    log_fn(f"[{'✓' if ok else '!'}] {domain}: "
           + ("профиль по этому сайту чист — следующий заход будет как с пустого "
              "браузера." if ok else "почистить не удалось."))
    return 1 if ok else 0


