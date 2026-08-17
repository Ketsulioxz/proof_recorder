from __future__ import annotations

import re
import urllib.parse


import bot
from .config import ACCEPT_LANGUAGE, CONTENT_SIZE, _attach_mode, _attached, _browser_choice, load_proof_settings
from .pageutil import _normalize_url, brand_for_url
MOBILE_UA_DEFAULT = True          # включено по умолчанию; ключ настройки "mobile_ua"

MOBILE_KEEP_DESKTOP_VIEWPORT = True

MOBILE_PROOF_VIEWPORT = {"width": 540, "height": 960}


MOBILE_BROWSERS = ("chrome", "yandex")

MOBILE_ATTACH_BROWSERS = MOBILE_BROWSERS


def _mobile_supported() -> bool:
    key = (_browser_choice() or {}).get("key", "chrome")
    if _attach_mode():
        return key in MOBILE_ATTACH_BROWSERS
    return key in MOBILE_BROWSERS


DESKTOP_ONLY_BRANDS = {"Pinco", "Vavada"}

DESKTOP_HOST_TAIL_LEN = (3, 4)


def _host_tail(url: str) -> str:
    try:
        host = (urllib.parse.urlparse(_normalize_url(url)).hostname or "").lower()
    except Exception:
        host = ""
    host = host.removeprefix("www.")
    parts = [p for p in host.split(".") if p]
    if len(parts) < 2:
        return ""
    return parts[-2].rsplit("-", 1)[-1]


def desktop_url_reason(url: str) -> str:
    brand = brand_for_url(url)
    if brand in DESKTOP_ONLY_BRANDS:
        return f"бренд {brand} снимается только десктопом"
    tail = _host_tail(url)
    if tail and len(tail) in DESKTOP_HOST_TAIL_LEN:
        return f"короткий хвост домена «{tail}» ({len(tail)} знака)"
    return ""


def mobile_for_url(url: str) -> bool:
    return not desktop_url_reason(url)


_mobile_run = {"on": None}   # None — ссылка не задана, режим по умолчанию


def set_mobile_run(on) -> None:
    _mobile_run["on"] = None if on is None else bool(on)


def set_mobile_run_for_url(url: str) -> None:
    set_mobile_run(mobile_for_url(url))


class _desktop_for_run:

    def __init__(self, on: bool = False):
        self.on = on
        self.prev = None

    def __enter__(self):
        self.prev = _mobile_run.get("on")
        set_mobile_run(self.on)
        return self

    def __exit__(self, *exc):
        _mobile_run["on"] = self.prev
        return False


def _mobile_ua_enabled() -> bool:
    if not bool(load_proof_settings("mobile_ua", MOBILE_UA_DEFAULT)):
        return False
    if not _mobile_supported():
        return False
    flag = _mobile_run.get("on")
    return True if flag is None else bool(flag)


_mobile_device_pin = {"device": None}


def reset_mobile_device() -> None:
    _mobile_device_pin["device"] = None


def _mobile_device() -> dict:
    device = _mobile_device_pin["device"]
    if device is None:
        device = dict(bot._context_device_kwargs(mobile=True))
        if MOBILE_KEEP_DESKTOP_VIEWPORT:
            device["viewport"] = dict(CONTENT_SIZE)
            device.pop("device_scale_factor", None)
        else:
            device["viewport"] = dict(MOBILE_PROOF_VIEWPORT)
            device["device_scale_factor"] = 1
        _mobile_device_pin["device"] = device
    out = dict(device)
    out["viewport"] = dict(device.get("viewport") or {})
    return out


_ANDROID_UA_RE = re.compile(r"Android\s+([\d.]+)\s*;\s*([^);]+)", re.I)


def _android_identity_from_ua(ua: str) -> tuple:
    match = _ANDROID_UA_RE.search(ua or "")
    if not match:
        return "14.0.0", "Pixel 8"
    version = match.group(1).strip(".")
    parts = [p for p in version.split(".") if p]
    while len(parts) < 3:
        parts.append("0")
    model = match.group(2).strip()
    return ".".join(parts[:3]), model


def _attached_browser_version() -> str:
    match = re.search(r"Chrome/([\d.]+)", str(_attached.get("version") or ""))
    return match.group(1) if match else ""


def _mobile_identity(page):
    version = _attached_browser_version()
    if not version:
        real_ua = page.evaluate("navigator.userAgent") or ""
        match = re.search(r"Chrome/([\d.]+)", real_ua)
        if match:
            version = match.group(1)
    major = version.split(".")[0] if version else ""

    try:
        brands = page.evaluate("navigator.userAgentData && navigator.userAgentData.brands") or []
    except Exception:
        brands = []
    if not brands and major:
        brands = [{"brand": "Chromium", "version": major},
                  {"brand": "Google Chrome", "version": major},
                  {"brand": "Not)A;Brand", "version": "24"}]

    device_ua = _mobile_device().get("user_agent", "")
    mobile_ua = device_ua
    if version and "Chrome/" in device_ua:
        mobile_ua = re.sub(r"Chrome/[\d.]+", f"Chrome/{version}", device_ua)
    platform_version, model = _android_identity_from_ua(mobile_ua)
    return {
        "userAgent": mobile_ua,
        "acceptLanguage": ACCEPT_LANGUAGE,
        "platform": "Linux armv8l",
        "userAgentMetadata": {
            "brands": brands,
            "fullVersion": version or "131.0.0.0",
            "platform": "Android",
            "platformVersion": platform_version,
            "architecture": "",
            "model": model,
            "mobile": True,
            "bitness": "",
            "wow64": False,
        },
    }


def _apply_mobile_cdp(context, page, log_fn=print, viewport: dict = None) -> bool:
    if not _mobile_ua_enabled():
        return False
    device = _mobile_device()
    viewport = viewport or device.get("viewport") or CONTENT_SIZE

    def _apply_to(pg) -> None:
        cdp = pg.context.new_cdp_session(pg)
        cdp.send("Emulation.setUserAgentOverride", _mobile_identity(pg))
        cdp.send("Emulation.setDeviceMetricsOverride", {
            "width": viewport["width"],
            "height": viewport["height"],
            "deviceScaleFactor": device.get("device_scale_factor", 1),
            "mobile": True,
        })
        cdp.send("Emulation.setTouchEmulationEnabled",
                 {"enabled": True, "maxTouchPoints": 5})

    try:
        _apply_to(page)
    except Exception as e:
        log_fn(f"[!] Мобильную эмуляцию включить не удалось ({e}) — "
               f"иду как обычный десктоп; прокладка может не отдать зеркало.")
        return False

    def _on_popup(pg):
        try:
            _apply_to(pg)
        except Exception:
            pass

    try:
        context.on("page", _on_popup)
    except Exception:
        pass
    _, model = _android_identity_from_ua(device.get("user_agent", ""))
    log_fn(f"[i] Представляюсь телефоном {model} (экран "
           f"{viewport['width']}x{viewport['height']}, тач включён) — эмуляция "
           f"через CDP, окно своё. Этот же аппарат в ключе запуска и в подсказках.")
    return True
