from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import time

import requests

import bot
from ._compat import PSUTIL_OK, psutil
from .capture import CURSOR_OVERLAY_SCRIPT
from .config import CONTENT_SIZE, LANGUAGE_LAUNCH_ARGS, _attach_mode, _attached, _browser_choice, current_profile_dir
from .mobile import MOBILE_KEEP_DESKTOP_VIEWPORT, MOBILE_PROOF_VIEWPORT, _apply_mobile_cdp, _mobile_device, _mobile_ua_enabled
from .network import _clear_site_data, _drop_stale_guard_cookies, _inject_saved_cookies, _proxy_dict_for, log_site_cookies
from .winmgr import _bring_browser_to_front, _chrome_pids, _content_size_cdp, _fit_mobile_window, _fit_mobile_window_cdp, _fit_window_to_screen, _keep_window_inside_work_area, _position_browser_window, _visible_launch_args
def _pids_using_profile(profile_dir: str) -> set:
    if not PSUTIL_OK or not profile_dir:
        return set()
    want = os.path.normcase(os.path.abspath(profile_dir))
    pids = set()
    for info in psutil.process_iter(["pid", "cmdline"]):
        try:
            for arg in (info.info.get("cmdline") or []):
                if not arg.startswith("--user-data-dir="):
                    continue
                if os.path.normcase(os.path.abspath(arg.split("=", 1)[1])) == want:
                    pids.add(info.info["pid"])
                    break
        except Exception:
            pass
    return pids


def _free_profile(profile_dir: str, log_fn) -> None:
    pids = _pids_using_profile(profile_dir)
    if not pids:
        return
    log_fn(f"[!] Наш профиль уже занят браузером (процессов: {len(pids)}) — "
           f"закрываю его, иначе новый запуск просто отдаст адрес старому окну "
           f"и отладочный порт не появится.")
    victims = []
    for pid in pids:
        try:
            proc = psutil.Process(pid)
            victims.append(proc)
            victims.extend(proc.children(recursive=True))
        except Exception:
            pass
    for victim in victims:
        try:
            victim.terminate()
        except Exception:
            pass
    _, alive = psutil.wait_procs(victims, timeout=10)
    for victim in alive:
        try:
            victim.kill()
        except Exception:
            pass
    time.sleep(1.0)


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_cdp_ready(port: int, timeout_s: float = 30.0, log_fn=None) -> dict:
    deadline = time.time() + timeout_s
    url = f"http://127.0.0.1:{port}/json/version"
    said = 0
    while time.time() < deadline:
        try:
            resp = requests.get(url, timeout=2)
            if resp.status_code == 200:
                return resp.json() or {"Browser": "?"}
        except Exception:
            pass
        waited = timeout_s - (deadline - time.time())
        if log_fn and waited >= said + 5:
            said = int(waited)
            log_fn(f"[i] Жду отладочный порт браузера... {said} сек")
        time.sleep(0.3)
    return {}


def _browser_pids_on_port(port: int) -> set:
    if not PSUTIL_OK:
        return set()
    needle = f"--remote-debugging-port={port}"
    pids = set()
    for info in psutil.process_iter(["pid", "cmdline"]):
        try:
            if any(needle == a for a in (info.info.get("cmdline") or [])):
                pids.add(info.info["pid"])
        except Exception:
            pass
    return pids


def _attached_pids() -> set:
    proc = _attached.get("proc")
    pids = set(_attached.get("pids") or ())
    if proc is not None:
        pids.add(proc.pid)
    port = _attached.get("port") or 0
    if port:
        pids |= _browser_pids_on_port(port)
    return pids


def _quit_attached_politely(port: int) -> bool:
    if not port:
        return False
    try:
        info = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=3).json()
    except Exception:
        return False
    ws = (info or {}).get("webSocketDebuggerUrl", "")
    if not ws:
        return False
    try:
        tabs = requests.get(f"http://127.0.0.1:{port}/json/list", timeout=3).json() or []
    except Exception:
        return False
    closed = 0
    for tab in tabs:
        if tab.get("type") != "page":
            continue
        try:
            requests.get(f"http://127.0.0.1:{port}/json/close/{tab.get('id')}", timeout=3)
            closed += 1
        except Exception:
            pass
    return closed > 0


def _kill_attached(log_fn=None, grace_s: float = 2.5) -> None:
    proc, pids = _attached.get("proc"), set(_attached.get("pids") or ())
    port = _attached.get("port") or 0
    _attached.update(proc=None, port=0, pids=set(), version="")
    if proc is None and not pids:
        return
    victims = []
    if PSUTIL_OK:
        for pid in ({proc.pid} if proc is not None else set()) | pids:
            try:
                parent = psutil.Process(pid)
                victims.append(parent)
                victims.extend(parent.children(recursive=True))
            except Exception:
                pass

    _quit_attached_politely(port)
    alive = victims
    if victims:
        _, alive = psutil.wait_procs(victims, timeout=grace_s)
    if alive and log_fn:
        log_fn(f"[i] Браузер сам не закрылся ({len(alive)} процессов) — закрываю "
               f"принудительно.")
    for victim in alive:
        try:
            victim.terminate()
        except Exception:
            pass
    if alive:
        _, stubborn = psutil.wait_procs(alive, timeout=8)
        for victim in stubborn:
            try:
                victim.kill()
            except Exception:
                pass
    try:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _mark_profile_clean(profile_dir: str) -> None:
    path = os.path.join(profile_dir, "Default", "Preferences")
    if not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return
        profile = data.setdefault("profile", {})
        if not isinstance(profile, dict):
            return
        if profile.get("exit_type") == "Normal" and profile.get("exited_cleanly") is True:
            return
        profile["exit_type"] = "Normal"
        profile["exited_cleanly"] = True
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def _launch_attached(p, proxy_dict, log_fn, profile_dir: str):
    choice = _browser_choice()
    if not choice:
        log_fn("[!] Ни одного браузера не нашлось — беру обычный запуск.")
        return None
    exe, port = choice["exe"], _free_port()
    args = [exe,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run", "--no-default-browser-check",
            "--disable-session-crashed-bubble",
            "--hide-crash-restore-bubble"] + LANGUAGE_LAUNCH_ARGS
    if _mobile_ua_enabled():
        if not MOBILE_KEEP_DESKTOP_VIEWPORT:
            args.append(f"--window-size={MOBILE_PROOF_VIEWPORT['width']},"
                        f"{MOBILE_PROOF_VIEWPORT['height']}")
        mobile_ua = _mobile_device().get("user_agent", "")
        if mobile_ua:
            args.append(f"--user-agent={mobile_ua}")
    if proxy_dict and proxy_dict.get("server"):
        args.append("--proxy-server=" + proxy_dict["server"])
        if proxy_dict.get("bypass"):
            args.append("--proxy-bypass-list=" +
                        proxy_dict["bypass"].replace(",", ";"))
        if proxy_dict.get("username"):
            log_fn("[!] У прокси логин и пароль, а обычный браузер их из ключа "
                   "не берёт — он спросит окном. Для пакетного прогона нужен "
                   "прокси с доступом по IP, без пароля.")
    try:
        os.makedirs(profile_dir, exist_ok=True)
        _free_profile(profile_dir, log_fn)
        _mark_profile_clean(profile_dir)
        proc = subprocess.Popen(args)
    except Exception as e:
        log_fn(f"[!] {choice['title']} запустить не удалось ({e}) — беру "
               f"обычный запуск.")
        return None
    _attached.update(proc=proc, port=port, exe=exe, pids=set())
    log_fn(f"[i] {choice['title']} запущен, жду отладочный порт {port}...")
    version = _wait_cdp_ready(port, log_fn=log_fn)
    if not version:
        if proc.poll() is not None and not _browser_pids_on_port(port):
            log_fn(f"[!] {choice['title']} завершился сразу и порт не открыл: он "
                   f"передал запуск другому своему процессу — с этим браузером "
                   f"такой способ не работает.")
        else:
            log_fn(f"[!] {choice['title']} не поднял отладочный порт за 30 секунд.")
        log_fn("[!] Беру обычный запуск: браузер тот же, управляет им Playwright.")
        _kill_attached(log_fn)
        return None
    _attached["pids"] = _browser_pids_on_port(port)
    _attached["version"] = version.get("Browser", "")
    log_fn(f"[i] Подключился к {version.get('Browser', '?')} по отладочному порту "
           f"{port}. Служебных ключей Playwright в командной строке нет.")
    try:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    except Exception as e:
        log_fn(f"[!] Подключиться к {choice['title']} не удалось ({e}) — беру "
               f"обычный запуск.")
        _kill_attached(log_fn)
        return None

    context = browser.contexts[0] if browser.contexts else None
    if context is None:
        log_fn(f"[!] {choice['title']} не отдал ни одного окна для управления — "
               f"беру обычный запуск.")
        try:
            browser.close()
        except Exception:
            pass
        _kill_attached(log_fn)
        return None
    try:
        page = context.pages[0] if context.pages else context.new_page()
        page.bring_to_front()
    except Exception as e:
        log_fn(f"[!] Вкладку получить не удалось ({e}) — беру обычный запуск.")
        try:
            browser.close()
        except Exception:
            pass
        _kill_attached(log_fn)
        return None
    log_fn(f"[i] Окон у браузера: {len(browser.contexts)}, вкладок в рабочем: "
           f"{len(context.pages)} — управляю вкладкой {page.url or 'about:blank'}")
    log_fn(f"[i] Профиль браузера: {os.path.basename(profile_dir)} — куки живут "
           f"между прогонами")
    return browser, context


def browser_processes(exe: str) -> list:
    if not PSUTIL_OK or not exe:
        return []
    ours = set()
    proc = _attached.get("proc")
    if proc is not None:
        ours.add(proc.pid)
        try:
            ours.update(c.pid for c in psutil.Process(proc.pid).children(recursive=True))
        except Exception:
            pass
    name = os.path.basename(exe).lower()
    found = []
    for info in psutil.process_iter(["pid", "name", "exe"]):
        try:
            if info.info["pid"] in ours:
                continue
            if (info.info.get("name") or "").lower() != name:
                continue
            path = info.info.get("exe") or ""
            if path and os.path.normcase(path) != os.path.normcase(exe):
                continue
            found.append(psutil.Process(info.info["pid"]))
        except Exception:
            pass
    return found


def close_browser_processes(exe: str, log_fn, wait_s: float = 15.0) -> bool:
    procs = browser_processes(exe)
    if not procs:
        return True
    log_fn(f"[*] {os.path.basename(exe)}: живых процессов {len(procs)} — закрываю "
           f"(браузер продолжает работать в фоне и после закрытия окна).")
    for pr_ in procs:
        try:
            pr_.terminate()
        except Exception:
            pass
    gone, alive = psutil.wait_procs(procs, timeout=wait_s)
    for pr_ in alive:
        try:
            pr_.kill()
        except Exception:
            pass
    if alive:
        psutil.wait_procs(alive, timeout=5)
    time.sleep(1.5)
    left = browser_processes(exe)
    if left:
        log_fn(f"[!] {len(left)} процессов закрыть не удалось — возможно, браузер "
               f"запущен от другого пользователя.")
        return False
    log_fn("[+] Браузер закрыт, файлы освободились.")
    return True


def _copy_file_retry(src: str, dst: str, tries: int = 3) -> str:
    last = ""
    for attempt in range(tries):
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            return ""
        except Exception as e:
            last = str(e)
            time.sleep(1.0)
    return last


def copy_user_profile(log_fn, close_running: bool = False) -> bool:
    choice = _browser_choice()
    if not choice:
        log_fn("[!] Браузер не найден.")
        return False
    src_root = choice.get("data", "")
    if not os.path.isdir(src_root):
        log_fn(f"[!] Профиль {choice['title']} не нашёлся: {src_root}")
        return False
    pairs = [
        ("Local State", "Local State"),                       # ключ расшифровки кук
        (os.path.join("Default", "Preferences"), os.path.join("Default", "Preferences")),
        (os.path.join("Default", "Network", "Cookies"),
         os.path.join("Default", "Network", "Cookies")),
    ]
    if close_running:
        close_browser_processes(choice["exe"], log_fn)
    elif browser_processes(choice["exe"]):
        log_fn(f"[!] {choice['title']} ещё работает (в том числе в фоне, после "
               f"закрытия окна) — файлы кук заняты.")
        return False

    copied = 0
    for rel_src, rel_dst in pairs:
        src = os.path.join(src_root, rel_src)
        dst = os.path.join(current_profile_dir(), rel_dst)
        if not os.path.isfile(src):
            continue
        err = _copy_file_retry(src, dst)
        if err:
            log_fn(f"[!] {rel_src}: скопировать не удалось ({err}).")
            log_fn(f"[!] Закрой {choice['title']} полностью — проверь значок в "
                   f"области уведомлений у часов и отключи в его настройках "
                   f"«Работать в фоновом режиме».")
            return False
        copied += 1
    if not copied:
        log_fn(f"[!] В профиле {choice['title']} нечего было брать.")
        return False
    log_fn(f"[✓] Из профиля {choice['title']} перенесено файлов: {copied} "
           f"(куки, настройки, ключ расшифровки).")
    log_fn("[i] Если куки окажутся нечитаемыми — значит браузер шифрует их "
           "привязкой к себе; тогда просто прогрей зеркало вручную, это надёжнее.")
    return True


def _launch_browser(p, proxy_dict, log_fn, profile_dir: str = None):
    kw = dict(headless=False, args=_visible_launch_args(), proxy=proxy_dict,
              ignore_default_args=["--enable-automation"],
              chromium_sandbox=True)
    ctx_kw = dict(
        viewport=CONTENT_SIZE,   # окно подстраивается под размер содержимого
        timezone_id="Europe/Moscow",
        ignore_https_errors=True,
    )
    if _mobile_ua_enabled():
        ctx_kw.update(_mobile_device())
        log_fn(f"[i] Контекст создаётся телефоном: {ctx_kw.get('viewport')}, "
               f"UA {ctx_kw.get('user_agent', '')[:60]}...")
    profile_dir = profile_dir or current_profile_dir()
    if _attach_mode():
        attached = _launch_attached(p, proxy_dict, log_fn, profile_dir)
        if attached:
            return attached
        log_fn("[!] Откатываюсь на обычный запуск через Playwright.")

    choice = _browser_choice() or {}
    key = choice.get("key", "chrome")
    if key != "chrome" and choice.get("exe"):
        kw["executable_path"] = choice["exe"]
        title = choice["title"]
    else:
        kw["channel"] = "chrome"
        title = "Google Chrome"
    _free_profile(profile_dir, log_fn)
    for attempt in (1, 2):
        try:
            os.makedirs(profile_dir, exist_ok=True)
            context = p.chromium.launch_persistent_context(profile_dir, **kw, **ctx_kw)
            log_fn(f"[i] {title} под управлением Playwright, профиль "
                   f"{os.path.basename(profile_dir)} — куки живут между прогонами")
            return None, context
        except Exception as e:
            if attempt == 1:
                log_fn(f"[!] {title} с первого раза не открылся "
                       f"({str(e).splitlines()[0][:90]}) — закрываю что осталось и "
                       f"пробую ещё раз, профиль уже готов.")
                _free_profile(profile_dir, log_fn)
                time.sleep(1.5)
                continue
            log_fn(f"[!] Постоянный профиль открыть не удалось ({e}) — беру временный. "
                   f"Куки этого прогона пропадут, для защиты сайта мы снова новый гость.")
    try:
        browser = p.chromium.launch(**kw)
    except Exception as e:
        log_fn(f"[!] {title} запустить не удалось ({e}) — беру встроенный "
               f"Chromium. Отпечаток будет хуже: в sec-ch-ua не будет бренда "
               f"'Google Chrome', и антибот-защита может отдать 403.")
        kw.pop("executable_path", None)
        kw.pop("channel", None)
        browser = p.chromium.launch(**kw)
    return browser, browser.new_context(**ctx_kw)


class OpenResult:

    __slots__ = ("browser", "context", "page", "pids", "status", "proxy", "reason", "dead")

    def __init__(self, browser=None, context=None, page=None, pids=None,
                 status=None, proxy: str = "", reason: str = "", dead: bool = False):
        self.browser, self.context, self.page, self.pids = browser, context, page, pids
        self.status = status
        self.proxy = proxy
        self.reason = reason
        self.dead = dead

    @property
    def ok(self) -> bool:
        return self.context is not None

    def close(self):
        _close_browser(self.browser, self.context)


def _close_browser(browser, context):
    attached = bool(_attached.get("proc") or _attached.get("pids"))
    for obj in ((browser,) if attached else (context, browser)):
        try:
            if obj:
                obj.close()
        except Exception:
            pass
    _kill_attached()


def _open_url(p, url: str, log_fn, proxy_attempts: list, referer: str = None,
              timeout_ms: int = 20000) -> OpenResult:
    pids_before = _chrome_pids()
    attempts = proxy_attempts or [""]
    last = OpenResult(reason="ни одной попытки открыть не сделано")

    for attempt, proxy_raw in enumerate(attempts):
        proxy_dict = _proxy_dict_for(proxy_raw, log_fn)
        label = bot._proxy_label(proxy_raw) if proxy_raw else "без прокси"
        if proxy_dict:
            log_fn(f"[i] Прокси {label} (попытка {attempt + 1}/{len(attempts)})")

        browser, context = _launch_browser(p, proxy_dict, log_fn)
        _drop_stale_guard_cookies(context, proxy_raw, log_fn)
        log_site_cookies(context, bot.extract_domain(url) or "", log_fn)
        _inject_saved_cookies(context, log_fn)
        context.add_init_script(CURSOR_OVERLAY_SCRIPT)
        page = context.pages[0] if context.pages else context.new_page()
        if _attach_mode():
            if _mobile_ua_enabled() and not MOBILE_KEEP_DESKTOP_VIEWPORT:
                mobile_view = _fit_mobile_window_cdp(page, log_fn)
            else:
                _fit_window_to_screen(page, log_fn)
                mobile_view = (_content_size_cdp(page, log_fn)
                               if _mobile_ua_enabled() else None)
            _apply_mobile_cdp(context, page, log_fn, viewport=mobile_view)
        elif _mobile_ua_enabled() and not MOBILE_KEEP_DESKTOP_VIEWPORT:
            _fit_mobile_window(page, log_fn)
        else:
            _fit_window_to_screen(page, log_fn)
        _position_browser_window(context, page, log_fn)
        _keep_window_inside_work_area(context, page, log_fn)

        ref = referer if referer is not None else bot._realistic_referer(
            bot.ALIVE_REFERERS[0], bot.extract_domain(url))
        log_fn(f"[*] Открываю {url}" + (f" (referer: {ref})" if ref else ""))
        nav_error = ""
        try:
            nav_resp = page.goto(url, wait_until="domcontentloaded",
                                 timeout=timeout_ms, referer=ref or None)
        except Exception as e:
            log_fn(f"[!] Ошибка открытия страницы: {e}")
            nav_resp = None
            nav_error = str(e).splitlines()[0][:160]

        status = nav_resp.status if nav_resp is not None else None
        blocked = status in bot.BLOCK_STATUS_CODES if status else False
        failed = bool(nav_error) or blocked

        if blocked:
            log_fn(f"[!] Защита сайта ответила {status} — убираю её след из профиля, "
                   f"иначе он повторится и на следующем заходе.")
            _clear_site_data(context, bot.extract_domain(url) or "", log_fn)

        if failed and attempt + 1 < len(attempts):
            reason = f"ответ {status}" if blocked else "таймаут/ошибка соединения"
            log_fn(f"[!] {reason} — пробую другой прокси.")
            last = OpenResult(status=status, proxy=label, dead=not blocked,
                              reason=_nav_fail_reason(status, nav_error))
            _close_browser(browser, context)
            continue

        if failed:
            log_fn("[!] Открыть страницу не удалось ни с одним прокси.")
            res = OpenResult(status=status, proxy=label,
                             dead=not blocked and _looks_dead(status, nav_error),
                             reason=_nav_fail_reason(status, nav_error))
            _close_browser(browser, context)
            return res

        chrome_pids = (_chrome_pids() - pids_before) | _attached_pids()
        log_fn(f"[i] PID нашего браузера: {sorted(chrome_pids)}")
        _bring_browser_to_front(log_fn, allowed_pids=chrome_pids)
        return OpenResult(browser, context, page, chrome_pids, status=status, proxy=label)

    return last


_DEAD_NET_ERRORS = ("ERR_NAME_NOT_RESOLVED", "ERR_NAME_RESOLUTION_FAILED",
                    "ERR_CONNECTION_REFUSED", "ERR_CONNECTION_RESET",
                    "ERR_CONNECTION_CLOSED", "ERR_ADDRESS_UNREACHABLE",
                    "ERR_SSL_PROTOCOL_ERROR", "ERR_EMPTY_RESPONSE",
                    "ERR_CERT_COMMON_NAME_INVALID", "ERR_CERT_DATE_INVALID")


def _looks_dead(status, nav_error: str) -> bool:
    if status and (status in (404, 410) or status >= 500):
        return True
    return any(e in (nav_error or "") for e in _DEAD_NET_ERRORS)


def _nav_fail_reason(status, nav_error: str) -> str:
    if status in bot.BLOCK_STATUS_CODES:
        return f"защита сайта отдала {status} (блокировка IP) — ни один прокси не прошёл"
    if status and status >= 400:
        return f"сервер отдал {status}"
    for err in _DEAD_NET_ERRORS:
        if err in (nav_error or ""):
            return f"сайт не отвечает ({err})"
    if "Timeout" in (nav_error or "") or "timeout" in (nav_error or ""):
        return "страница не загрузилась: таймаут"
    return f"страница не загрузилась: {nav_error or 'неизвестная ошибка'}"


def _plan_proxies(count: int, pool: list) -> list:
    if not pool:
        return [""] * count
    start = random.randrange(len(pool))
    return [pool[(start + i) % len(pool)] for i in range(count)]


def _proxy_attempts_for(primary: str, pool: list) -> list:
    if not pool:
        return [primary or ""]
    rest = [p for p in pool if p != primary]
    idx = pool.index(primary) if primary in pool else 0
    rest.sort(key=lambda p: (pool.index(p) - idx) % len(pool))   # дальше по кругу
    return ([primary] + rest)[:max(1, min(bot.MAX_PROXY_RETRIES, len(pool)))]


