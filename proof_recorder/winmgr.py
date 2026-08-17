from __future__ import annotations

import ctypes
import time


import bot
from ._compat import WIN32_OK, PSUTIL_OK, win32gui, win32con, win32process, win32api, psutil
from .config import CAPTURE_TASKBAR, CONTENT_SIZE, LANGUAGE_LAUNCH_ARGS, _attach_mode, _browser_hwnd, load_proof_settings
from .mobile import MOBILE_PROOF_VIEWPORT
def _chrome_pids() -> set:
    if not PSUTIL_OK:
        return set()
    pids = set()
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if "chrome" in name or name in ("browser.exe", "msedge.exe"):
                pids.add(proc.info["pid"])
        except Exception:
            pass
    return pids


def _expand_pids(pids: set) -> set:
    if not PSUTIL_OK or not pids:
        return set(pids or ())
    out = set(pids)
    for pid in list(pids):
        try:
            out.update(c.pid for c in psutil.Process(pid).children(recursive=True))
        except Exception:
            pass
    return out


def _bring_browser_to_front(log_fn, allowed_pids: set = None, max_wait: float = 12.0) -> bool:
    if not WIN32_OK:
        log_fn("[!] pywin32 не установлен (pip install pywin32) — не могу "
               "вывести окно браузера на передний план")
        return False

    allowed_pids = _expand_pids(allowed_pids or set())
    target = {"hwnd": None, "area": 0}
    seen = {}

    def _enum_cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        cls = win32gui.GetClassName(hwnd)
        if allowed_pids:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid not in allowed_pids:
                return True
            seen[cls] = seen.get(cls, 0) + 1
        else:
            if cls != "Chrome_WidgetWin_1":
                return True
            left, top, _, _ = win32gui.GetWindowRect(hwnd)
            if abs(left) > 20 or abs(top) > 20:
                return True
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        area = (right - left) * (bottom - top)
        if area < 200 * 200:
            return True
        if area > target["area"]:
            target["hwnd"] = hwnd
            target["area"] = area
        return True

    deadline = time.time() + max_wait
    while time.time() < deadline and not target["hwnd"]:
        try:
            win32gui.EnumWindows(_enum_cb, None)
        except Exception:
            pass
        if not target["hwnd"]:
            time.sleep(0.2)

    hwnd = target["hwnd"]
    if not hwnd:
        log_fn(f"[!] Не нашёл окно браузера за {max_wait:.0f} сек "
               f"(процессов в поиске: {len(allowed_pids)}"
               + (f", их видимые окна: {seen}" if seen else ", видимых окон у них нет")
               + ")")
        return False
    _browser_hwnd["v"] = hwnd     # пригодится для точных экранных координат
    log_fn(f"[i] Окно браузера найдено (hwnd={hwnd}), вывожу на передний план")

    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        cur_thread = win32api.GetCurrentThreadId()
        fg_hwnd = win32gui.GetForegroundWindow()
        fg_thread = win32process.GetWindowThreadProcessId(fg_hwnd)[0] if fg_hwnd else 0
        attached = False
        if fg_thread and fg_thread != cur_thread:
            win32process.AttachThreadInput(fg_thread, cur_thread, True)
            attached = True
        try:
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
        finally:
            if attached:
                win32process.AttachThreadInput(fg_thread, cur_thread, False)
        return True
    except Exception as e:
        log_fn(f"[!] Не удалось вывести окно браузера на передний план: {e}")
        return False


class _WinRect(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _MonitorInfo(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", _WinRect),
                ("rcWork", _WinRect), ("dwFlags", ctypes.c_ulong)]


MONITOR_AUTO = -1   # как раньше: основной монитор Windows

_MONITORENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                                      ctypes.POINTER(_WinRect), ctypes.c_void_p)


def list_monitors() -> list:
    found = []
    try:
        MONITORINFOF_PRIMARY = 1

        def _collect(hmon, hdc, lprc, lparam):
            mi = _MonitorInfo()
            mi.cbSize = ctypes.sizeof(_MonitorInfo)
            if ctypes.windll.user32.GetMonitorInfoW(ctypes.c_void_p(hmon),
                                                    ctypes.byref(mi)):
                m, w = mi.rcMonitor, mi.rcWork
                found.append({
                    "rect": (int(m.left), int(m.top), int(m.right), int(m.bottom)),
                    "work": (int(w.left), int(w.top), int(w.right), int(w.bottom)),
                    "primary": bool(mi.dwFlags & MONITORINFOF_PRIMARY),
                })
            return 1

        ctypes.windll.user32.EnumDisplayMonitors(
            None, None, _MONITORENUMPROC(_collect), 0)
    except Exception:
        return []
    found.sort(key=lambda d: (d["rect"][0], d["rect"][1]))
    for i, mon in enumerate(found):
        left, top, right, bottom = mon["rect"]
        mon["index"] = i
        mon["title"] = (f"{i + 1} — {right - left}x{bottom - top}"
                        + (" (основной)" if mon["primary"] else "")
                        + (f", смещение {left},{top}" if (left or top) else ""))
    return found


def _selected_monitor():
    try:
        idx = int(load_proof_settings("monitor_index", MONITOR_AUTO))
    except (TypeError, ValueError):
        return None
    if idx < 0:
        return None
    for mon in list_monitors():
        if mon["index"] == idx:
            return mon["rect"], mon["work"]
    return None


def _screen_size():
    mon = _selected_monitor()
    if mon:
        left, top, right, bottom = mon[0]
        return right - left, bottom - top
    try:
        u = ctypes.windll.user32
        w, h = int(u.GetSystemMetrics(0)), int(u.GetSystemMetrics(1))
        return (w, h) if w > 0 and h > 0 else None
    except Exception:
        return None


def _display_scale() -> float:
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)
    except Exception:
        pass
    for getter in ("GetDpiForSystem",):
        try:
            dpi = getattr(ctypes.windll.user32, getter)()
            if dpi and dpi > 0:
                return dpi / 96.0
        except Exception:
            continue
    return 1.0


def _work_area_logical():
    work = _work_area()
    if not work:
        return None
    k = _display_scale() or 1.0
    if abs(k - 1.0) < 0.01:
        return work
    return tuple(int(round(v / k)) for v in work)


def _work_area():
    mon = _selected_monitor()
    if mon:
        return mon[1]
    try:
        r = _WinRect()
        SPI_GETWORKAREA = 0x0030
        if not ctypes.windll.user32.SystemParametersInfoW(
                SPI_GETWORKAREA, 0, ctypes.byref(r), 0):
            return None
        if r.right <= r.left or r.bottom <= r.top:
            return None
        return int(r.left), int(r.top), int(r.right), int(r.bottom)
    except Exception:
        return None


def _monitor_of_window(hwnd):
    if not hwnd:
        return None
    try:
        MONITOR_DEFAULTTONEAREST = 2
        hmon = ctypes.windll.user32.MonitorFromWindow(
            ctypes.c_void_p(int(hwnd)), MONITOR_DEFAULTTONEAREST)
        if not hmon:
            return None
        mi = _MonitorInfo()
        mi.cbSize = ctypes.sizeof(_MonitorInfo)
        if not ctypes.windll.user32.GetMonitorInfoW(ctypes.c_void_p(hmon),
                                                    ctypes.byref(mi)):
            return None
        m, w = mi.rcMonitor, mi.rcWork
        return ((int(m.left), int(m.top), int(m.right), int(m.bottom)),
                (int(w.left), int(w.top), int(w.right), int(w.bottom)))
    except Exception:
        return None


def _keep_window_inside_work_area(context, page, log_fn, tries: int = 2) -> None:
    work = _work_area_logical()
    if not work:
        return
    for _ in range(max(1, tries)):
        try:
            cdp = context.new_cdp_session(page)
            window_id = cdp.send("Browser.getWindowForTarget")["windowId"]
            b = (cdp.send("Browser.getWindowBounds",
                          {"windowId": window_id}) or {}).get("bounds") or {}
            top, height = int(b.get("top") or 0), int(b.get("height") or 0)
        except Exception:
            return
        overflow = (top + height) - work[3]
        if overflow <= 0:
            return
        try:
            vp = page.viewport_size or {}
            new_h = int(vp.get("height", 0)) - overflow
            if new_h < 200:
                log_fn(f"[!] Окно вылезает за рабочую область на {overflow} px, "
                       f"но ужимать дальше некуда.")
                return
            log_fn(f"[i] Окно на {overflow} px ниже рабочей области — ужимаю экран "
                   f"до {vp.get('width')}x{new_h}, чтобы панель задач осталась видна.")
            page.set_viewport_size({"width": int(vp.get("width")), "height": new_h})
        except Exception as e:
            log_fn(f"[!] Не удалось ужать окно: {e}")
            return
        _position_browser_window(context, page, log_fn)


def _position_browser_window(context, page, log_fn) -> bool:
    work = _work_area_logical()
    try:
        cdp = context.new_cdp_session(page)
        window_id = cdp.send("Browser.getWindowForTarget")["windowId"]
        bounds = (cdp.send("Browser.getWindowBounds",
                           {"windowId": window_id}) or {}).get("bounds") or {}
        win_w, win_h = int(bounds.get("width") or 0), int(bounds.get("height") or 0)
    except Exception as e:
        log_fn(f"[!] Не удалось прочитать границы окна через CDP: {e}")
        return False

    if not work or win_w <= 0 or win_h <= 0:
        left, top = 0, 0   # не смогли посчитать — хотя бы в угол, как раньше
    else:
        left = max(work[0], work[2] - win_w)
        top = max(work[1], work[3] - win_h)
    try:
        cdp.send("Browser.setWindowBounds", {
            "windowId": window_id,
            "bounds": {"left": left, "top": top},
        })
    except Exception as e:
        log_fn(f"[!] Не удалось спозиционировать окно через CDP: {e}")
        return False
    if work:
        log_fn(f"[i] Окно {win_w}x{win_h} — в правый нижний угол рабочей области "
               f"{work[2] - work[0]}x{work[3] - work[1]}, в ({left}, {top})")
    return True


def _resize_window_cdp(page, log_fn, target: dict = None) -> None:
    work = _work_area()
    if not work:
        log_fn("[!] Не удалось узнать рабочую область — оставляю окно как есть.")
        return
    avail_w, avail_h = work[2] - work[0], work[3] - work[1]
    try:
        m = page.evaluate("""() => ({
            outerW: window.outerWidth, outerH: window.outerHeight,
            innerW: window.innerWidth, innerH: window.innerHeight,
        })""")
    except Exception as e:
        log_fn(f"[!] Не удалось измерить окно: {e}")
        return
    chrome_w = max(0, m["outerW"] - m["innerW"])
    chrome_h = max(0, m["outerH"] - m["innerH"])
    size = target or CONTENT_SIZE
    want_w = min(size["width"] + chrome_w, avail_w)
    want_h = min(size["height"] + chrome_h, avail_h)
    if abs(m["outerW"] - want_w) <= 2 and abs(m["outerH"] - want_h) <= 2:
        return
    try:
        cdp = page.context.new_cdp_session(page)
        window_id = cdp.send("Browser.getWindowForTarget")["windowId"]
        try:
            cdp.send("Browser.setWindowBounds",
                     {"windowId": window_id, "bounds": {"windowState": "normal"}})
        except Exception:
            pass
        cdp.send("Browser.setWindowBounds", {
            "windowId": window_id,
            "bounds": {"width": want_w, "height": want_h},
        })
        page.wait_for_timeout(250)
    except Exception as e:
        log_fn(f"[!] Не удалось изменить размер окна: {e} — пруф может выйти "
               f"с окном не того размера.")
        return
    log_fn(f"[i] Окно {want_w}x{want_h} (без эмуляции вьюпорта, рабочая область "
           f"{avail_w}x{avail_h})")


def _fit_mobile_window(page, log_fn) -> None:
    want = dict(MOBILE_PROOF_VIEWPORT)
    work = _work_area_logical()
    if not work:
        log_fn("[!] Не удалось узнать рабочую область — оставляю размеры как есть.")
        return
    avail_w, avail_h = work[2] - work[0], work[3] - work[1]

    try:
        page.context.new_cdp_session(page).send("Emulation.clearDeviceMetricsOverride")
        m = page.evaluate("""() => ({
            outerW: window.outerWidth, outerH: window.outerHeight,
            innerW: window.innerWidth, innerH: window.innerHeight,
        })""")
    except Exception as e:
        log_fn(f"[!] Не удалось померить окно для подгонки ({e}) — "
               f"оставляю экран {want['width']}x{want['height']}.")
        return

    chrome_w = max(0, m["outerW"] - m["innerW"])   # рамки по бокам, обычно 0
    chrome_h = max(0, m["outerH"] - m["innerH"])   # вкладки + адресная строка

    view_w = min(want["width"], avail_w - chrome_w)
    view_h = min(want["height"], avail_h - chrome_h)
    if view_w >= 768:
        view_w = 760
    if view_w < 200 or view_h < 200:
        log_fn(f"[!] Рабочая область слишком мала ({avail_w}x{avail_h}) — "
               f"оставляю экран {want['width']}x{want['height']}.")
        view_w, view_h = want["width"], want["height"]

    try:
        page.set_viewport_size({"width": view_w, "height": view_h})
    except Exception as e:
        log_fn(f"[!] Не удалось задать экран {view_w}x{view_h} ({e})")
        return
    log_fn(f"[i] Экран телефона {view_w}x{view_h}; окно с рамкой "
           f"{view_w + chrome_w}x{view_h + chrome_h} при рабочей области "
           f"{avail_w}x{avail_h} — панель задач не перекрывается.")


def _fit_mobile_window_cdp(page, log_fn) -> dict:
    want = dict(MOBILE_PROOF_VIEWPORT)
    _resize_window_cdp(page, log_fn, target=want)
    try:
        m = page.evaluate("""() => ({
            innerW: window.innerWidth, innerH: window.innerHeight,
        })""")
        view = {"width": int(m["innerW"]), "height": int(m["innerH"])}
    except Exception as e:
        log_fn(f"[!] Не удалось померить окно после подгонки ({e}) — "
               f"эмулирую экран {want['width']}x{want['height']}.")
        return want
    if view["width"] < 200 or view["height"] < 200:
        return want
    if view["width"] >= 768:
        log_fn(f"[!] Окно осталось шириной {view['width']} — эмулирую экран "
               f"{want['width']} точек, по бокам страницы будут поля.")
        view["width"] = want["width"]
    log_fn(f"[i] Экран телефона {view['width']}x{view['height']} — по размеру "
           f"содержимого окна (свой запуск).")
    return view


def _content_size_cdp(page, log_fn) -> dict:
    try:
        m = page.evaluate("""() => ({
            innerW: window.innerWidth, innerH: window.innerHeight,
        })""")
        view = {"width": int(m["innerW"]), "height": int(m["innerH"])}
    except Exception as e:
        log_fn(f"[!] Не удалось померить содержимое окна ({e}) — эмулирую "
               f"{CONTENT_SIZE['width']}x{CONTENT_SIZE['height']}.")
        return dict(CONTENT_SIZE)
    if view["width"] < 200 or view["height"] < 200:
        return dict(CONTENT_SIZE)
    log_fn(f"[i] Мобильный режим с десктопным окном: эмулируемый экран "
           f"{view['width']}x{view['height']} — по размеру содержимого окна.")
    return view


def _fit_window_to_screen(page, log_fn) -> None:
    if _attach_mode():
        _resize_window_cdp(page, log_fn)
        return
    work = _work_area()
    if not work:
        log_fn("[!] Не удалось узнать рабочую область экрана — оставляю размер как есть.")
        return
    avail_w, avail_h = work[2] - work[0], work[3] - work[1]
    try:
        m = page.evaluate("""() => ({
            outerW: window.outerWidth, outerH: window.outerHeight,
            innerW: window.innerWidth, innerH: window.innerHeight,
        })""")
    except Exception as e:
        log_fn(f"[!] Не удалось измерить окно относительно экрана: {e}")
        return

    chrome_w = m["outerW"] - m["innerW"]   # рамки по бокам, обычно 0
    chrome_h = m["outerH"] - m["innerH"]   # вкладки + адресная строка
    fit_w = min(m["innerW"], avail_w - chrome_w)
    fit_h = min(m["innerH"], avail_h - chrome_h)
    if fit_w < 800 or fit_h < 600:
        log_fn(f"[!] Рабочая область {avail_w}x{avail_h} слишком мала для окна с "
               f"содержимым {CONTENT_SIZE['width']}x{CONTENT_SIZE['height']} — "
               f"часть окна не поместится в кадр.")
        return
    if fit_w >= m["innerW"] and fit_h >= m["innerH"]:
        return   # и так помещается

    log_fn(f"[i] Окно {m['outerW']}x{m['outerH']} не помещается в рабочую область "
           f"{avail_w}x{avail_h} — ужимаю содержимое до {fit_w}x{fit_h}")
    try:
        page.set_viewport_size({"width": fit_w, "height": fit_h})
        page.wait_for_timeout(150)
    except Exception as e:
        log_fn(f"[!] Не удалось подогнать размер окна: {e}")


_DWMWA_EXTENDED_FRAME_BOUNDS = 9


def _visible_window_rect(hwnd):
    if not hwnd:
        return None
    try:
        r = _WinRect()
        hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            ctypes.c_void_p(int(hwnd)), ctypes.c_uint(_DWMWA_EXTENDED_FRAME_BOUNDS),
            ctypes.byref(r), ctypes.sizeof(r))
        if hr != 0 or r.right <= r.left or r.bottom <= r.top:
            return None
        return r.left, r.top, r.right, r.bottom
    except Exception:
        return None


def _window_capture_rect(page) -> dict:
    rect = _visible_window_rect(_browser_hwnd.get("v"))
    if rect:
        left, top, right, bottom = rect
    else:
        m = _get_page_metrics(page)
        left, top = int(m["screenX"]), int(m["screenY"])
        right, bottom = left + int(m["outerWidth"]), top + int(m["outerHeight"])

    mon = _monitor_of_window(_browser_hwnd.get("v")) if CAPTURE_TASKBAR else None
    if mon is None and CAPTURE_TASKBAR:
        mon = _selected_monitor()
    if mon is None and CAPTURE_TASKBAR:
        work, scr = _work_area(), _screen_size()
        if work and scr:
            mon = ((0, 0, scr[0], scr[1]), work)
    if mon:
        (mon_l, mon_t, mon_r, mon_b), (work_l, work_t, work_r, work_b) = mon
        if mon_b > work_b:
            bottom = max(bottom, mon_b)
        if mon_r > work_r:
            right = max(right, mon_r)
        if bottom > work_b:
            right = max(right, mon_r)
        right, bottom = min(right, mon_r), min(bottom, mon_b)
        left, top = max(left, mon_l), max(top, mon_t)

    width, height = right - left, bottom - top
    return {"left": left, "top": top,
            "width": max(2, width - width % 2), "height": max(2, height - height % 2)}


def _get_page_metrics(page) -> dict:
    return page.evaluate("""() => ({
        screenX: window.screenX, screenY: window.screenY,
        outerWidth: window.outerWidth, outerHeight: window.outerHeight,
        innerWidth: window.innerWidth, innerHeight: window.innerHeight,
        dpr: window.devicePixelRatio || 1
    })""")


def _content_origin_win32(metrics: dict):
    hwnd = _browser_hwnd.get("v")
    if not WIN32_OK or not hwnd:
        return None
    found = []

    def _kid(k, _):
        try:
            if win32gui.GetClassName(k) == "Chrome_RenderWidgetHostHWND":
                found.append(win32gui.GetWindowRect(k))
        except Exception:
            pass
        return True

    try:
        win32gui.EnumChildWindows(hwnd, _kid, None)
    except Exception:
        return None
    for left, top, right, bottom in found:
        if (abs((right - left) - metrics["innerWidth"]) <= 2 and
                abs((bottom - top) - metrics["innerHeight"]) <= 2):
            return left, top
    return None


def _content_origin(metrics: dict):
    origin = _content_origin_win32(metrics)
    if origin:
        return origin
    left = metrics["screenX"] + (metrics["outerWidth"] - metrics["innerWidth"]) / 2
    top = metrics["screenY"] + (metrics["outerHeight"] - metrics["innerHeight"])
    return left, top


def _click_and_await_transition(page, context, do_click, log_fn, retry_click=None,
                                wait_s: float = 6, retry_wait_s: float = 5,
                                dup_grace_ms: int = 400, on_tick=None):
    popups = []
    nav_started = {"v": False}

    def _on_new_page(pg):
        popups.append(pg)

    def _on_request(req):
        try:
            if req.is_navigation_request() and req.frame is page.main_frame:
                nav_started["v"] = True
        except Exception:
            pass

    def _on_framenavigated(frame):
        try:
            if frame is page.main_frame:
                nav_started["v"] = True
        except Exception:
            pass

    context.on("page", _on_new_page)
    page.on("request", _on_request)
    page.on("framenavigated", _on_framenavigated)
    try:
        url_before = page.url
    except Exception:
        url_before = ""

    def _seen() -> bool:
        if popups or nav_started["v"]:
            return True
        try:
            return page.url != url_before
        except Exception:
            return True

    def _tick():
        if on_tick is None:
            return
        try:
            on_tick()
        except Exception:
            pass

    def _wait(seconds: float):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not _seen():
            _tick()
            try:
                page.wait_for_timeout(80)
            except Exception:
                return
        _tick()

    try:
        clicked_xy = do_click()
        _tick()
        _wait(wait_s)

        if not _seen() and retry_click is not None:
            log_fn("[!] После клика ничего не произошло (навигация даже не началась) — "
                   "пробую ещё раз.")
            clicked_xy = retry_click() or clicked_xy
            _wait(retry_wait_s)

        if popups:
            try:
                page.wait_for_timeout(dup_grace_ms)
            except Exception:
                pass
    finally:
        for target, event, handler in ((context, "page", _on_new_page),
                                       (page, "request", _on_request),
                                       (page, "framenavigated", _on_framenavigated)):
            try:
                target.remove_listener(event, handler)
            except Exception:
                pass

    popped_page = popups[0] if popups else None
    if len(popups) > 1:
        log_fn(f"[!] Сайт открыл вкладок сразу: {len(popups)} — оставляю первую, "
               f"остальные закрываю.")
        for extra in popups[1:]:
            try:
                log_fn(f"[i] Закрываю лишнюю вкладку: {extra.url}")
                extra.close()
            except Exception:
                pass
        try:
            popped_page.bring_to_front()  # вернуть в кадр именно нашу вкладку
        except Exception:
            pass
    return popped_page, clicked_xy


_HEADLESS_ONLY_ARGS = ("--disable-gpu", "--disable-software-rasterizer", "--no-sandbox")


_NO_FLAG_WARNING_ARG = "--test-type"


def _visible_launch_args() -> list:
    return ([a for a in bot.PLAYWRIGHT_LAUNCH_ARGS
             if not a.startswith(_HEADLESS_ONLY_ARGS)]
            + LANGUAGE_LAUNCH_ARGS + [_NO_FLAG_WARNING_ARG])


def _page_to_screen_xy(page_x: int, page_y: int, metrics: dict):
    left, top = _content_origin(metrics)
    return round(page_x + left), round(page_y + top)


