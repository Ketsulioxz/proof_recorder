from __future__ import annotations

import math
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time


from ._compat import FFMPEG_OK, MSS_OK, OXIPNG_OK, WIN32_OK, imageio_ffmpeg, mss, oxipng, win32gui, win32con, win32api
from .config import APP_DIR, RECORD_FPS
from .config import SHOT_1, SHOT_2, VIDEO_1
from .winmgr import _get_page_metrics, _page_to_screen_xy
CURSOR_OVERLAY_SCRIPT = """
(function install() {
    if (window.__cursorOverlayInstalled) return;
    if (window.top !== window.self) return;
    if (!document.documentElement) {
        return requestAnimationFrame(install);
    }
    window.__cursorOverlayInstalled = true;
    const stale = document.getElementById('__auto_cursor');
    if (stale) stale.remove();

    const style = document.createElement('style');
    style.textContent = `
        #__auto_cursor {
            position: fixed;
            top: 0; left: 0;
            width: 22px; height: 22px;
            pointer-events: none;
            z-index: 2147483647;
            transform: translate(-2px, -2px);
            transition: left 45ms linear, top 45ms linear;
            opacity: 0;
        }
        #__auto_cursor.visible { opacity: 1; }
        #__auto_cursor svg { display: block; filter: drop-shadow(0 1px 2px rgba(0,0,0,.5)); }
        #__auto_cursor.clicking svg { animation: __cursor_click 260ms ease; }
        @keyframes __cursor_click {
            0% { transform: scale(1); }
            40% { transform: scale(0.75); }
            100% { transform: scale(1); }
        }
    `;
    document.documentElement.appendChild(style);

    const el = document.createElement('div');
    el.id = '__auto_cursor';
    el.innerHTML = '<svg width="22" height="22" viewBox="0 0 22 22" xmlns="http://www.w3.org/2000/svg">' +
        '<path d="M1 1 L1 16 L5 12.5 L7.5 18.5 L10 17.5 L7.5 11.5 L13 11.5 Z" fill="white" stroke="black" stroke-width="1"/>' +
        '</svg>';
    document.documentElement.appendChild(el);

    window.__cursorX = 0;
    window.__cursorY = 0;
    let placed = false;
    window.__setCursorPos = function(x, y) {
        if (!placed) {
            placed = true;
            const t = el.style.transition;
            el.style.transition = 'none';
            el.style.left = x + 'px';
            el.style.top = y + 'px';
            void el.offsetWidth;          // применить сразу, до возврата плавности
            el.style.transition = t;
        } else {
            el.style.left = x + 'px';
            el.style.top = y + 'px';
        }
        el.classList.add('visible');
        window.__cursorX = x;
        window.__cursorY = y;
    };
    window.__cursorClickPulse = function() {
        el.classList.remove('clicking');
        void el.offsetWidth;  // reflow — иначе повторное добавление класса не перезапустит анимацию
        el.classList.add('clicking');
    };
    window.__setCursorHidden = function(hidden) {
        el.style.visibility = hidden ? 'hidden' : '';
    };
})();
"""


_NATIVE_CURSOR_CLASS = "ProofRecorderCursorOverlay"
_NATIVE_CURSOR_COLOR_KEY = 0x00FF00FF  # COLORREF 0x00bbggrr — пурпурная затравка под LWA_COLORKEY


class _NativeCursorOverlay:
    def __init__(self):
        self._hwnd = None
        self._thread = None
        self._ready = threading.Event()

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == win32con.WM_PAINT:
            hdc, ps = win32gui.BeginPaint(hwnd)
            try:
                rect = win32gui.GetClientRect(hwnd)
                key_brush = win32gui.CreateSolidBrush(_NATIVE_CURSOR_COLOR_KEY)
                win32gui.FillRect(hdc, rect, key_brush)
                win32gui.DeleteObject(key_brush)
                pts = [(1, 1), (1, 16), (5, 13), (8, 19), (10, 18), (8, 12), (13, 12)]
                white_brush = win32gui.CreateSolidBrush(win32api.RGB(255, 255, 255))
                black_pen = win32gui.CreatePen(win32con.PS_SOLID, 1, win32api.RGB(0, 0, 0))
                old_brush = win32gui.SelectObject(hdc, white_brush)
                old_pen = win32gui.SelectObject(hdc, black_pen)
                win32gui.Polygon(hdc, pts)
                win32gui.SelectObject(hdc, old_brush)
                win32gui.SelectObject(hdc, old_pen)
                win32gui.DeleteObject(white_brush)
                win32gui.DeleteObject(black_pen)
            finally:
                win32gui.EndPaint(hwnd, ps)
            return 0
        if msg == win32con.WM_DESTROY:
            win32gui.PostQuitMessage(0)
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _run(self):
        try:
            hinst = win32api.GetModuleHandle(None)
            wc = win32gui.WNDCLASS()
            wc.lpfnWndProc = self._wndproc
            wc.hInstance = hinst
            wc.lpszClassName = _NATIVE_CURSOR_CLASS
            wc.hbrBackground = 0
            try:
                win32gui.RegisterClass(wc)
            except Exception:
                pass  # уже зарегистрирован (повторный запуск в том же процессе)
            ex_style = (win32con.WS_EX_LAYERED | win32con.WS_EX_TOPMOST |
                        win32con.WS_EX_TOOLWINDOW | win32con.WS_EX_TRANSPARENT |
                        win32con.WS_EX_NOACTIVATE)
            self._hwnd = win32gui.CreateWindowEx(
                ex_style, _NATIVE_CURSOR_CLASS, None, win32con.WS_POPUP,
                0, 0, 22, 22, 0, 0, hinst, None)
            win32gui.SetLayeredWindowAttributes(self._hwnd, _NATIVE_CURSOR_COLOR_KEY, 255, win32con.LWA_COLORKEY)
        except Exception:
            self._hwnd = None
        finally:
            self._ready.set()
        if self._hwnd:
            try:
                win32gui.PumpMessages()  # блокирует поток, обрабатывает WM_PAINT и т.п. до WM_QUIT
            except Exception:
                pass

    def _ensure(self):
        if not WIN32_OK:
            return None
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            self._ready.wait(timeout=2)
        return self._hwnd

    def show(self, screen_x: int, screen_y: int):
        hwnd = self._ensure()
        if not hwnd:
            return
        try:
            win32gui.SetWindowPos(
                hwnd, win32con.HWND_TOPMOST, int(screen_x) - 2, int(screen_y) - 2, 0, 0,
                win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW)
        except Exception:
            pass

    def hide(self):
        if self._hwnd:
            try:
                win32gui.ShowWindow(self._hwnd, win32con.SW_HIDE)
            except Exception:
                pass

    def destroy(self):
        hwnd, self._hwnd = self._hwnd, None
        if hwnd:
            try:
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            except Exception:
                pass
        self._thread = None
        self._ready.clear()


_native_cursor = _NativeCursorOverlay()

def _set_cursor_overlay(page, x: int, y: int):
    try:
        page.evaluate("([x, y]) => { if (window.__setCursorPos) window.__setCursorPos(x, y); }", [x, y])
    except Exception:
        pass


def _seed_cursor_asap(page, x: int, y: int, timeout_ms: int = 4000) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        try:
            ok = page.evaluate(
                "([x, y]) => { if (window.__setCursorPos) { window.__setCursorPos(x, y); return true; } return false; }",
                [x, y])
        except Exception:
            ok = False
        if ok:
            return True
        try:
            page.wait_for_timeout(15)
        except Exception:
            return False
    return False


def _handoff_cursor_to_page(page, x: int, y: int, log_fn) -> bool:
    try:
        sx, sy = _page_to_screen_xy(x, y, _get_page_metrics(page))
        _native_cursor.show(sx, sy)
    except Exception:
        pass
    ok = _seed_cursor_asap(page, x, y)
    _native_cursor.hide()
    if not ok:
        log_fn("[!] Нарисованный курсор на зеркале не встал — дальше он появится "
               "при первом же движении мыши.")
    return ok


def _cursor_click_pulse(page):
    try:
        page.evaluate("() => { if (window.__cursorClickPulse) window.__cursorClickPulse(); }")
    except Exception:
        pass


def _get_cursor_pos(page):
    try:
        pos = page.evaluate("() => [window.__cursorX || 0, window.__cursorY || 0]")
        return int(pos[0]), int(pos[1])
    except Exception:
        return None


def _bezier_point(t: float, p0, p1, p2, p3):
    mt = 1 - t
    x = mt ** 3 * p0[0] + 3 * mt ** 2 * t * p1[0] + 3 * mt * t ** 2 * p2[0] + t ** 3 * p3[0]
    y = mt ** 3 * p0[1] + 3 * mt ** 2 * t * p1[1] + 3 * mt * t ** 2 * p2[1] + t ** 3 * p3[1]
    return x, y


def _ease_in_out(t: float) -> float:
    return t * t * (3 - 2 * t)  # smoothstep


def _bezier_path(start, end, min_points: int = 14, max_points: int = 36):
    sx, sy = start
    ex, ey = end
    dx, dy = ex - sx, ey - sy
    dist = math.hypot(dx, dy)
    if dist < 1:
        return [end]
    nx, ny = -dy / dist, dx / dist  # единичный перпендикуляр к линии старт->финиш
    spread = min(dist * 0.35, 160)
    off1 = random.uniform(-spread, spread)
    off2 = random.uniform(-spread, spread)
    cp1 = (sx + dx * 0.25 + nx * off1, sy + dy * 0.25 + ny * off1)
    cp2 = (sx + dx * 0.75 + nx * off2, sy + dy * 0.75 + ny * off2)
    steps = int(max(min_points, min(max_points, dist / 10)))
    return [_bezier_point(_ease_in_out(i / steps), start, cp1, cp2, end)
            for i in range(1, steps + 1)]


def _ghost_move(page, start, end, log_fn=None):
    try:
        dist = math.hypot(end[0] - start[0], end[1] - start[1])
        overshoot = dist > 120 and random.random() < 0.6
        target = end
        if overshoot:
            length = max(dist, 1e-6)
            ux, uy = (end[0] - start[0]) / length, (end[1] - start[1]) / length
            over_dist = random.uniform(8, 22)
            target = (end[0] + ux * over_dist, end[1] + uy * over_dist)

        for px, py in _bezier_path(start, target, min_points=8, max_points=20):
            ix, iy = round(px), round(py)
            _set_cursor_overlay(page, ix, iy)
            page.mouse.move(ix, iy)
            page.wait_for_timeout(random.randint(3, 8))

        if overshoot:
            page.wait_for_timeout(random.randint(30, 70))
            for px, py in _bezier_path(target, end, min_points=4, max_points=8):
                ix, iy = round(px), round(py)
                _set_cursor_overlay(page, ix, iy)
                page.mouse.move(ix, iy)
                page.wait_for_timeout(random.randint(4, 8))
    except Exception as e:
        if log_fn:
            log_fn(f"[!] _ghost_move: {e}")
        try:
            _set_cursor_overlay(page, *end)
            page.mouse.move(*end)
        except Exception:
            pass


def _scroll_scan_down(page, width: int, height: int, log_fn=None):
    x = int(width * random.uniform(0.4, 0.6))
    y = int(height * random.uniform(0.2, 0.4))
    _set_cursor_overlay(page, x, y)
    try:
        for _ in range(random.randint(10, 18)):
            page.mouse.wheel(0, random.randint(45, 85))
            x = max(0, min(width, x + random.randint(-25, 25)))
            y = max(0, min(height, y + random.randint(-15, 15)))
            _set_cursor_overlay(page, x, y)
            page.mouse.move(x, y, steps=random.randint(3, 6))
            page.wait_for_timeout(random.randint(30, 65))
        page.wait_for_timeout(random.randint(150, 250))
    except Exception as e:
        if log_fn:
            log_fn(f"[!] _scroll_scan_down: {e}")
    return x, y


def _scroll_scan_up(page, width: int, height: int, start_xy, log_fn=None, target_xy=None):
    x, y = start_xy
    ticks = random.randint(10, 18)
    path = _bezier_path(start_xy, target_xy, min_points=ticks, max_points=ticks) if target_xy else None
    try:
        for i in range(ticks):
            page.mouse.wheel(0, -random.randint(50, 95))
            if path:
                x, y = round(path[i][0]), round(path[i][1])
            else:
                x = max(0, min(width, x + random.randint(-25, 25)))
                y = max(0, min(height, y + random.randint(-15, 15)))
            _set_cursor_overlay(page, x, y)
            page.mouse.move(x, y, steps=random.randint(3, 6))
            page.wait_for_timeout(random.randint(30, 55))
        if target_xy:
            page.evaluate("() => window.scrollTo({top: 0, behavior: 'smooth'})")
            page.wait_for_timeout(random.randint(200, 350))
        page.wait_for_timeout(random.randint(150, 300))
    except Exception as e:
        if log_fn:
            log_fn(f"[!] _scroll_scan_up: {e}")
    return x, y


def _scroll_to_element_human(page, key: str, log_fn, index: int = 0, max_ticks: int = 70):
    from .dommap import _dom_query   # ленивый импорт, см. комментарий вверху модуля
    try:
        h = int(page.evaluate("window.innerHeight"))
        w = int(page.evaluate("window.innerWidth"))
    except Exception:
        return
    want = h * 0.35          # куда ставим элемент по вертикали
    cur = _get_cursor_pos(page) or (int(w * 0.5), int(h * 0.4))
    x, y = cur
    last_top = None
    stuck = 0
    for _ in range(max_ticks):
        res = _dom_query(page, log_fn, "point", key=key, index=index)
        if not res or res.get("top") is None:
            return
        delta = res["top"] - want
        if abs(delta) <= 60:
            return
        if last_top is not None and abs(res["top"] - last_top) < 2:
            stuck += 1
            if stuck >= 3:
                return
        else:
            stuck = 0
        last_top = res["top"]
        try:
            page.mouse.wheel(0, max(-120, min(120, int(delta))))
            x = max(0, min(w, x + random.randint(-18, 18)))
            y = max(0, min(h, y + random.randint(-12, 12)))
            _set_cursor_overlay(page, x, y)
            page.mouse.move(x, y, steps=random.randint(2, 4))
            page.wait_for_timeout(random.randint(28, 55))
        except Exception:
            return


def _human_move_to(page, x: int, y: int, dwell_ms=(700, 1100), log_fn=None):
    start = _get_cursor_pos(page) or (x, y)
    _ghost_move(page, start, (x, y), log_fn=log_fn)
    page.wait_for_timeout(random.randint(*dwell_ms))


def _human_click_at(page, x: int, y: int, log_fn, dwell_ms=(700, 1100)):
    _human_move_to(page, x, y, dwell_ms=dwell_ms, log_fn=log_fn)
    _cursor_click_pulse(page)
    page.mouse.click(x, y)


def _start_screen_recording(output_path: str, rect: dict, log_fn):
    if not FFMPEG_OK:
        log_fn("[!] Пакет imageio-ffmpeg не установлен — видео не будет записано")
        return None
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [
        ffmpeg_exe, "-y",
        "-f", "gdigrab", "-framerate", str(RECORD_FPS),
        "-offset_x", str(rect["left"]), "-offset_y", str(rect["top"]),
        "-video_size", f"{rect['width']}x{rect['height']}",
        "-i", "desktop",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        output_path,
    ]
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=_NO_WINDOW)
    except Exception as e:
        log_fn(f"[!] Не удалось запустить запись экрана: {e}")
        return None
    _recording_active.set()
    return proc


def _stop_screen_recording(proc, log_fn):
    if not proc:
        return
    try:
        proc.communicate(input=b"q", timeout=15)
    except Exception:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            pass
    _recording_active.clear()   # фоновое сжатие может продолжать


def _finalize_video(raw_path: str, output_path: str, log_fn):
    if not raw_path or not os.path.exists(raw_path):
        return
    try:
        os.replace(raw_path, output_path)
    except Exception as e:
        log_fn(f"[!] Не удалось сохранить видео: {e}")


def _grab_screenshot(path: str, rect: dict, log_fn):
    if not MSS_OK:
        log_fn("[!] Пакет mss не установлен — скриншот не сделан")
        return
    with mss.mss() as sct:
        img = sct.grab(rect)
        mss.tools.to_png(img.rgb, img.size, output=path)


MAX_SHOT_BYTES = 1 * 1024 * 1024      # 1 МБ на скриншот
MAX_VIDEO_BYTES = 3 * 1024 * 1024     # 3 МБ на видео

_X264_SCREEN_ARGS = [
    "-tune", "stillimage", "-g", "250", "-bf", "8", "-refs", "6",
    "-x264-params", "aq-mode=3:aq-strength=0.8:psy-rd=0.4,0.0:deblock=-1,-1",
]

_recording_active = threading.Event()

_LOW_PRIORITY = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_BG_FLAGS = _LOW_PRIORITY | _NO_WINDOW


def _find_pngquant() -> str:
    places = []
    if getattr(sys, "frozen", False):
        places.append(os.path.join(getattr(sys, "_MEIPASS", ""), "pngquant.exe"))
        places.append(os.path.join(APP_DIR, "pngquant.exe"))
    places.append(os.path.join(os.path.dirname(sys.executable), "pngquant.exe"))
    for cand in places:
        if cand and os.path.isfile(cand):
            return cand
    return shutil.which("pngquant") or ""


def _oxipng_pass(path: str) -> None:
    if not OXIPNG_OK:
        return
    try:
        oxipng.optimize(path, level=3)
    except Exception:
        pass


def _shrink_screenshot(path: str, log_fn, max_bytes: int = MAX_SHOT_BYTES) -> None:
    try:
        before = os.path.getsize(path)
    except OSError:
        return
    if before <= max_bytes:
        return

    _oxipng_pass(path)
    if os.path.getsize(path) <= max_bytes:
        log_fn(f"[i] {os.path.basename(path)}: {before // 1024} -> "
               f"{os.path.getsize(path) // 1024} КБ (без потерь)")
        return

    pngquant = _find_pngquant()
    if not pngquant:
        log_fn(f"[!] {os.path.basename(path)} весит {os.path.getsize(path) // 1024} КБ "
               f"при лимите {max_bytes // 1024} КБ, а pngquant не найден "
               f"(pip install pngquant-cli) — оставляю как есть.")
        return

    tmp = path + ".q.png"
    for extra, label in (([], "палитра 256"),
                         (["--nofs"], "палитра 256 без дизеринга"),
                         (["--nofs", "128"], "палитра 128"),
                         (["--nofs", "64"], "палитра 64")):
        try:
            subprocess.run([pngquant, "--quality", "0-95", "--speed", "1", "--strip",
                            "--force", "--output", tmp] + extra + [path],
                           capture_output=True, timeout=120,
                           creationflags=_BG_FLAGS)
        except Exception:
            break
        if not os.path.isfile(tmp):
            continue
        _oxipng_pass(tmp)
        if os.path.getsize(tmp) <= max_bytes:
            try:
                os.replace(tmp, path)
                log_fn(f"[i] {os.path.basename(path)}: {before // 1024} -> "
                       f"{os.path.getsize(path) // 1024} КБ ({label})")
            except OSError:
                pass
            return
    if os.path.isfile(tmp):
        try:
            os.replace(tmp, path)
        except OSError:
            pass
    log_fn(f"[!] {os.path.basename(path)}: {os.path.getsize(path) // 1024} КБ — "
           f"в лимит {max_bytes // 1024} КБ ужать не удалось.")


def _video_duration_s(path: str) -> float:
    try:
        r = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-i", path],
                           capture_output=True, text=True, errors="replace",
                           timeout=60, creationflags=_NO_WINDOW)
    except Exception:
        return 0.0
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", r.stderr or "")
    if not m:
        return 0.0
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def _shrink_video(path: str, log_fn, max_bytes: int = MAX_VIDEO_BYTES) -> None:
    try:
        before = os.path.getsize(path)
    except OSError:
        return
    if before <= max_bytes or not FFMPEG_OK:
        return
    duration = _video_duration_s(path)
    if duration <= 0.5:
        log_fn("[!] Длительность видео не определилась — пережимать не берусь.")
        return

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    workdir = os.path.dirname(path) or "."
    out = os.path.join(workdir, "_shrink.mp4")
    passlog = os.path.join(workdir, "_pass")
    for margin in (0.93, 0.80):
        kbits = int(max_bytes * 8 / 1000 / duration * margin)
        if kbits < 80:
            log_fn(f"[!] Видео {duration:.0f} сек — под {max_bytes // 1024} КБ "
                   f"получится каша, оставляю как есть.")
            break
        common = ([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", path,
                   "-c:v", "libx264", "-preset", "slower"] + _X264_SCREEN_ARGS +
                  ["-b:v", f"{kbits}k", "-pix_fmt", "yuv420p", "-an",
                   "-passlogfile", passlog])
        try:
            subprocess.run(common + ["-pass", "1", "-f", "mp4", os.devnull],
                           capture_output=True, timeout=900,
                           creationflags=_BG_FLAGS)
            subprocess.run(common + ["-pass", "2", out],
                           capture_output=True, timeout=900,
                           creationflags=_BG_FLAGS)
        except Exception as e:
            log_fn(f"[!] Пережать видео не удалось: {str(e).splitlines()[0][:70]}")
            break
        if os.path.isfile(out) and 0 < os.path.getsize(out) <= max_bytes:
            try:
                os.replace(out, path)
                log_fn(f"[i] {os.path.basename(path)}: {before // 1024} -> "
                       f"{os.path.getsize(path) // 1024} КБ "
                       f"(H.264, два прохода, {kbits} кбит/с)")
            except OSError:
                pass
            break
        log_fn(f"[i] {kbits} кбит/с дали "
               f"{os.path.getsize(out) // 1024 if os.path.isfile(out) else '?'} КБ — "
               f"беру битрейт ниже.")
    for junk in (out, passlog + "-0.log", passlog + "-0.log.mbtree"):
        try:
            os.remove(junk)
        except OSError:
            pass


class _CompressQueue:

    def __init__(self):
        self._jobs = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._thread = None
        self._stop = False

    def submit(self, folder: str, log_fn):
        with self._lock:
            self._jobs.append((folder, log_fn))
            self._idle.clear()
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._work, daemon=True)
                self._thread.start()
        self._wake.set()

    def _work(self):
        while True:
            with self._lock:
                if self._stop or not self._jobs:
                    self._idle.set()
                    self._thread = None
                    return
                folder, log_fn = self._jobs.pop(0)
            while _recording_active.is_set() and not self._stop:
                time.sleep(0.5)
            try:
                for name in (SHOT_1, SHOT_2):
                    path = os.path.join(folder, name)
                    if os.path.isfile(path):
                        _shrink_screenshot(path, log_fn)
                video = os.path.join(folder, VIDEO_1)
                if os.path.isfile(video):
                    _shrink_video(video, log_fn)
            except Exception as e:
                log_fn(f"[!] Сжатие папки {os.path.basename(folder)} не удалось: "
                       f"{str(e).splitlines()[0][:80]}")

    def pending(self) -> int:
        with self._lock:
            return len(self._jobs)

    def drain(self, log_fn, timeout: float = 1800) -> None:
        left = self.pending()
        if left or not self._idle.is_set():
            log_fn(f"[*] Дожимаю оставшиеся пруфы ({left} в очереди)...")
        if not self._idle.wait(timeout):
            log_fn("[!] Сжатие не успело за отведённое время — часть пруфов "
                   "осталась несжатой.")


_compress_queue = _CompressQueue()
