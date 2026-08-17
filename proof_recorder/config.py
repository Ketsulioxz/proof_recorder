from __future__ import annotations

import json
import os
import sys


import bot


def _app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = _app_dir()

bot.PROXY_CONFIG_FILE = os.path.join(APP_DIR, "proxy_config.json")
CONTENT_SIZE = {"width": 1920, "height": 1080}
RECORD_FPS = 30

CAPTURE_TASKBAR = True

ACCEPT_LANGUAGE = "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"
LANGUAGE_LAUNCH_ARGS = ["--lang=ru-RU", f"--accept-lang={ACCEPT_LANGUAGE}"]
_browser_hwnd = {"v": None}
BROWSER_PROFILE_DIR = os.path.join(APP_DIR, ".browser_profile")


YANDEX_DIRECT_HOSTS = ["ya.ru", "*.ya.ru", "yandex.ru", "*.yandex.ru",
                       "yandex.net", "*.yandex.net", "yastatic.net", "*.yastatic.net",
                       "dzen.ru", "*.dzen.ru"]

PROOF_SETTINGS_FILE = os.path.join(APP_DIR, "proof_settings.json")

YANDEX_BYPASS_PROXY = True


def _read_proof_settings() -> dict:
    try:
        with open(PROOF_SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_proof_settings(key: str = None, default=None):
    global YANDEX_BYPASS_PROXY
    data = _read_proof_settings()
    if key is None:
        YANDEX_BYPASS_PROXY = bool(data.get("yandex_bypass_proxy", True))
        return YANDEX_BYPASS_PROXY
    value = data.get(key, default)
    return default if value is None else value


def save_proof_settings(yandex_bypass_proxy: bool = None, **fields):
    global YANDEX_BYPASS_PROXY
    data = _read_proof_settings()
    if yandex_bypass_proxy is not None:
        YANDEX_BYPASS_PROXY = bool(yandex_bypass_proxy)
        data["yandex_bypass_proxy"] = YANDEX_BYPASS_PROXY
    for k, v in fields.items():
        data[k] = v
    try:
        with open(PROOF_SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


_BROWSERS = [
    {"key": "chrome", "title": "Google Chrome",
     "exe": [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
             r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
             r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"],
     "data": r"%LOCALAPPDATA%\Google\Chrome\User Data"},
    {"key": "yandex", "title": "Яндекс Браузер",
     "exe": [r"%LOCALAPPDATA%\Yandex\YandexBrowser\Application\browser.exe",
             r"C:\Program Files\Yandex\YandexBrowser\Application\browser.exe",
             r"C:\Program Files (x86)\Yandex\YandexBrowser\Application\browser.exe"],
     "data": r"%LOCALAPPDATA%\Yandex\YandexBrowser\User Data"},
    {"key": "edge", "title": "Microsoft Edge",
     "exe": [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
             r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"],
     "data": r"%LOCALAPPDATA%\Microsoft\Edge\User Data"},
]


def detect_browsers() -> list:
    found = []
    for b in _BROWSERS:
        for raw in b["exe"]:
            path = os.path.expandvars(raw)
            if os.path.isfile(path):
                found.append({"key": b["key"], "title": b["title"], "exe": path,
                              "data": os.path.expandvars(b["data"])})
                break
    return found


def _browser_choice() -> dict:
    found = detect_browsers()
    if not found:
        return {}
    key = load_proof_settings("browser_key", "chrome")
    for b in found:
        if b["key"] == key:
            return b
    return found[0]


def _attach_mode() -> bool:
    return str(load_proof_settings("browser_mode", "playwright")) == "attach"


def current_profile_dir() -> str:
    key = (_browser_choice() or {}).get("key", "chrome")
    return BROWSER_PROFILE_DIR if key == "chrome" else f"{BROWSER_PROFILE_DIR}-{key}"


_attached = {"proc": None, "port": 0, "exe": "", "version": ""}


SHOT_1 = "Screenshot_1.PNG"
SHOT_2 = "Screenshot_2.PNG"
VIDEO_1 = "Video_1.mp4"
_PROOF_FILES = (SHOT_1, SHOT_2, VIDEO_1)
