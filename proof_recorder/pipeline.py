from __future__ import annotations

import os
import random
import re
import shutil
import subprocess
import time
from datetime import datetime

from playwright.sync_api import sync_playwright

import bot
from .config import SHOT_1, SHOT_2, VIDEO_1, _PROOF_FILES
from . import proof_xlsx
from . import config
from .browser import OpenResult, _close_browser, _free_profile, _looks_dead, _mark_profile_clean, _open_url, _proxy_attempts_for
from .capture import _compress_queue, _finalize_video, _grab_screenshot, _handoff_cursor_to_page, _human_click_at, _native_cursor, _scroll_scan_down, _scroll_scan_up, _set_cursor_overlay, _start_screen_recording, _stop_screen_recording
from .classify import LINK_REDIRECT, _classify_link, _same_site
from .config import LANGUAGE_LAUNCH_ARGS, YANDEX_DIRECT_HOSTS, _browser_choice, current_profile_dir
from .dommap import _click_through_dom_fields, _dom_find, _dom_point, _dom_point_fresh, _find_form_dom, _human_click_dom
from .mobile import _desktop_for_run, desktop_url_reason, reset_mobile_device
from .pageutil import FORM_WAIT_MS, UNKNOWN_BRAND_FOLDER, _await_navigation, _has_content, _mirror_form_wait_ms, _next_folder_number, _normalize_url, _report_empty_page, _settle_url, _urls_same_page, _wait_for_content, _wait_for_media, brand_for_url
from .transition import _TransitionBurst, _TransitionHold, _shoot_transition_fallback, _transition_debug_on
from .winmgr import _bring_browser_to_front, _click_and_await_transition, _get_page_metrics, _page_to_screen_xy, _position_browser_window, _window_capture_rect
YANDEX_HOME = "https://ya.ru/"
YANDEX_SEARCH = "https://yandex.ru/search/?text={}&lr=213"


class RunResult:

    __slots__ = ("url", "kind", "outcome", "reason", "final_url", "proxy", "folder",
                 "retry_as_redirect")

    def __init__(self, url: str, kind: str = "", outcome: str = proof_xlsx.PROBLEM,
                 reason: str = "", final_url: str = "", proxy: str = "", folder: str = ""):
        self.url = url
        self.kind = kind
        self.outcome = outcome
        self.reason = reason
        self.final_url = final_url
        self.proxy = proxy
        self.folder = folder
        self.retry_as_redirect = False

    @property
    def ok(self) -> bool:
        return self.outcome == proof_xlsx.OK


class _Run:

    def __init__(self, p, url: str, kind: str, folder: str, proxy_attempts: list, log_fn):
        self.p = p
        self.url = url
        self.kind = kind
        self.folder = folder
        self.proxy_attempts = proxy_attempts
        self.log = log_fn

        self.browser = None
        self.context = None
        self.page = None          # вкладка, с которой начали (лендинг/выдача)
        self.target_page = None   # активная вкладка (после перехода — зеркало)
        self.pids = set()
        self.proxy = ""

        self.rec_proc = None
        self.raw_video = os.path.join(folder, "_recording.mp4")
        self.video_done = False

        self.problems = []        # расхождения -> жёлтый
        self.dead_reason = ""     # мёртвый сайт -> красный
        self.final_url = ""
        self.retry_as_redirect = False
        self.click_xy = (0, 0)
        self.shot2_done = False
        self.transition_shot_ok = False

    def problem(self, reason: str) -> bool:
        self.log(f"[✗] {reason}")
        self.problems.append(reason)
        return False

    def dead(self, reason: str) -> bool:
        self.log(f"[✗] Сайт мёртв: {reason}")
        self.dead_reason = reason
        return False

    def attach(self, opened: OpenResult):
        self.browser, self.context = opened.browser, opened.context
        self.page = self.target_page = opened.page
        self.pids = opened.pids or set()
        self.proxy = opened.proxy

    def close_browser(self):
        _close_browser(self.browser, self.context)
        self.browser = self.context = None

    def start_recording(self, page):
        rect = _window_capture_rect(page)
        self.log(f"[i] Регион захвата: {rect['width']}x{rect['height']} "
                 f"от ({rect['left']}, {rect['top']})")
        if abs(_get_page_metrics(page).get("dpr", 1) - 1) > 0.01:
            self.log("[!] Масштаб дисплея Windows не 100% — регион записи/скриншотов может "
                     "не совпасть с окном браузера. Поставь масштаб 100% в параметрах экрана.")
        self.rec_proc = _start_screen_recording(self.raw_video, rect, self.log)

    def stop_recording(self):
        if self.rec_proc:
            self.log("[*] Останавливаю запись экрана...")
            _stop_screen_recording(self.rec_proc, self.log)
            self.rec_proc = None
        if not self.video_done:
            _finalize_video(self.raw_video, os.path.join(self.folder, VIDEO_1), self.log)
            self.video_done = True

    def shot(self, name: str, page=None) -> bool:
        path = os.path.join(self.folder, name)
        _grab_screenshot(path, _window_capture_rect(page or self.target_page), self.log)
        ok = os.path.isfile(path) and os.path.getsize(path) > 0
        self.log(f"[+] {name} сохранён" if ok else f"[!] {name} снять не удалось")
        return ok


def _stage_landing(run: _Run) -> bool:
    opened = _open_url(run.p, run.url, run.log, run.proxy_attempts)
    if not opened.ok:
        run.proxy = opened.proxy
        if opened.status in bot.BLOCK_STATUS_CODES:
            run.log(f"[i] Напрямую отдаёт {opened.status} — переигрываю через выдачу Яндекса.")
            run.retry_as_redirect = True
            return False
        return run.dead(opened.reason) if opened.dead else run.problem(opened.reason)
    run.attach(opened)
    page = run.page

    landed = bot.extract_domain(page.url)
    if not _same_site(landed, bot.extract_domain(run.url)):
        run.log(f"[i] Домен сразу сменился на {landed} — это редирект, а не лендинг. "
                f"Переигрываю ссылку через выдачу Яндекса.")
        run.retry_as_redirect = True
        return False

    run.log("[*] Жду догрузки картинок/баннеров/видео...")
    _wait_for_media(page, run.log)
    health = _wait_for_content(page, run.log, timeout_ms=12000)
    if not _has_content(health):
        _report_empty_page(health, "Лендинг", run.log)
        status = health.get("status")
        if status in bot.BLOCK_STATUS_CODES:
            run.log(f"[i] Лендинг отдал {status} — переигрываю через выдачу Яндекса.")
            run.retry_as_redirect = True
            return False
        if _looks_dead(status, ""):
            return run.dead(f"лендинг отдал {status}")
        return run.problem("лендинг не отрисовался — пустая страница")
    run.log(f"[i] Лендинг отрисовался: {health.get('textLen')} символов текста, "
            f"{health.get('clickable')} кликабельных элементов")

    run.log("[*] Ищу кнопку перехода в разметке страницы...")
    dom_cta = _dom_find(page, run.log, "cta", "Кнопка перехода", scroll=False)
    if not dom_cta:
        return run.problem("на лендинге не нашлась кнопка перехода на зеркало")

    run.start_recording(page)
    metrics = _get_page_metrics(page)
    if not run.shot(SHOT_1, page):
        return run.problem("не удалось снять Screenshot_1")

    run.log("[*] Пролистываю страницу целиком (как беглый просмотр)...")
    cur_xy = _scroll_scan_down(page, metrics["innerWidth"], metrics["innerHeight"], run.log)
    target_xy = dom_cta if isinstance(dom_cta, tuple) else None
    _scroll_scan_up(page, metrics["innerWidth"], metrics["innerHeight"], cur_xy,
                    run.log, target_xy=target_xy)

    point = _dom_point_fresh(page, run.log, "cta", what="кнопка перехода")
    if not point:
        return run.problem("кнопка перехода пропала со страницы до клика")
    run.log(f"[+] Кнопка в ({point[0]}, {point[1]}) на странице")
    run.log("[*] Навожу курсор на кнопку и выдерживаю паузу перед кликом...")
    return _click_to_mirror(run, page, "cta", "кнопка перехода", point, metrics)


def _yandex_captcha(page) -> bool:
    try:
        if "showcaptcha" in (page.url or "").lower():
            return True
        return bool(page.evaluate("""() => {
            if (document.querySelector('.CheckboxCaptcha,[class*="SmartCaptcha"],form[action*="checkcaptcha"]'))
                return true;
            const t = ((document.body && document.body.innerText) || '').toLowerCase();
            return t.includes('подтвердите, что запросы отправляли вы')
                || t.includes('вы не робот') || t.includes('я не робот');
        }"""))
    except Exception:
        return False


_DISMISS_BANNERS_JS = r"""() => {
    const NO_WORDS = ['нет', 'не сейчас', 'спасибо, нет', 'закрыть', 'позже', 'отмена'];
    const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
    const closed = [];
    const boxes = document.querySelectorAll(
        '[class*="DistributionPopup"],[class*="Distribution"],[class*="Popup"],[class*="popup"]');
    for (const box of boxes) {
        let s, r;
        try { s = getComputedStyle(box); r = box.getBoundingClientRect(); } catch (e) { continue; }
        if (s.display === 'none' || s.visibility === 'hidden') continue;
        if (s.position !== 'fixed' && s.position !== 'sticky') continue;
        if (r.width < 120 || r.height < 30) continue;
        let btn = null;
        for (const b of box.querySelectorAll(
                'button,a,[role="button"],[class*="button"],[class*="Button"]')) {
            const t = norm(b.innerText) || norm(b.getAttribute('aria-label'));
            if (NO_WORDS.indexOf(t) >= 0) { btn = b; break; }
        }
        if (!btn) continue;
        const label = norm(box.innerText).slice(0, 60);
        try { btn.click(); closed.push(label); } catch (e) {}
    }
    return closed;
}"""


def _dismiss_yandex_banners(page, log_fn) -> int:
    try:
        closed = page.evaluate(_DISMISS_BANNERS_JS) or []
    except Exception:
        return 0
    for label in closed:
        log_fn(f"[i] Закрыл плашку Яндекса: {label!r}")
    return len(closed)


def _yandex_no_results(page, query: str) -> str:
    try:
        info = page.evaluate("""() => {
            const body = (document.body && document.body.innerText) || '';
            const m = body.match(/Добавлены результаты по запросу\\s*[«"']([^»"']+)[»"']/);
            return {
                corrected: m ? m[1] : '',
                nothing: /ничего не нашлось|ничего не найдено|не найдено ни одного/i.test(body),
            };
        }""") or {}
    except Exception:
        return ""
    corrected = (info.get("corrected") or "").strip()
    if corrected and corrected.lower().replace(" ", "") != query.lower().replace(" ", ""):
        return f"нет в индексе Яндекса — выдача подменила запрос на «{corrected}»"
    if info.get("nothing"):
        return "нет в индексе Яндекса — по запросу ничего не нашлось"
    return ""


def _yandex_type_query(page, query: str, log_fn) -> bool:
    if not _dom_find(page, log_fn, "search_box", "Строка поиска Яндекса"):
        return False
    if not _human_click_dom(page, log_fn, "search_box", "строке поиска", dwell_ms=(400, 700)):
        return False
    page.wait_for_timeout(random.randint(250, 450))
    log_fn(f"[*] Печатаю в поиске: {query}")
    for ch in query:
        try:
            page.keyboard.type(ch)
        except Exception as e:
            log_fn(f"[!] Ввод запроса прервался: {e}")
            return False
        page.wait_for_timeout(random.randint(35, 115))
    page.wait_for_timeout(random.randint(400, 800))   # пауза перед Enter, как у человека
    page.keyboard.press("Enter")
    try:
        page.wait_for_load_state("domcontentloaded", timeout=20000)
    except Exception:
        pass
    page.wait_for_timeout(random.randint(600, 1000))
    return True


def _yandex_open_serp(run: _Run) -> bool:
    page = run.page
    if _yandex_captcha(page):
        return run.problem("Яндекс показал капчу на главной")
    _dismiss_yandex_banners(page, run.log)

    query = bot.extract_domain(run.url) or run.url

    if not _yandex_type_query(page, query, run.log):
        return run.problem("не нашлась строка поиска Яндекса — печатать запрос некуда")
    if "/search" not in (page.url or ""):
        return run.problem("после Enter выдача не открылась")

    _wait_for_media(page, run.log)
    if _yandex_captcha(page):
        return run.problem("Яндекс показал капчу вместо выдачи")
    _dismiss_yandex_banners(page, run.log)
    health = _wait_for_content(page, run.log, timeout_ms=10000)
    if not _has_content(health):
        return run.problem("страница выдачи Яндекса не отрисовалась")
    return True


def _stage_yandex(run: _Run) -> bool:
    domain = bot.extract_domain(run.url)
    if not domain:
        return run.problem("из ссылки не удалось выделить домен")

    opened = _open_url(run.p, YANDEX_HOME, run.log, run.proxy_attempts, referer="")
    if not opened.ok:
        run.proxy = opened.proxy
        return run.problem(f"не открылась главная Яндекса: {opened.reason}")
    run.attach(opened)

    if not _yandex_open_serp(run):
        return False

    page = run.page
    label = f"Ссылка на {domain} в выдаче"
    found = _dom_find(page, run.log, "serp", label, scroll=False, domain=domain)
    if not found:
        verdict = _yandex_no_results(page, domain)
        return run.problem(f"{domain}: {verdict}" if verdict
                           else f"{domain} не найден на первой странице выдачи Яндекса")

    run.start_recording(page)
    metrics = _get_page_metrics(page)

    point = found if isinstance(found, tuple) else _dom_point_fresh(
        page, run.log, "serp", what="ссылка в выдаче", refind_args={"domain": domain})
    if not point:
        return run.problem("ссылка пропала из выдачи до клика")
    run.log(f"[*] Навожу курсор на ссылку в выдаче ({point[0]}, {point[1]}) и кликаю...")
    clicked = _click_to_mirror(
        run,
        page,
        "serp",
        "ссылка в выдаче",
        point,
        metrics,
        refind_args={"domain": domain},
        transition_shot=SHOT_1,
    )

    if not clicked:
        return False

    # Сначала выясняем, куда реально пришёл браузер.
    # Это нужно сделать ДО ошибки по Screenshot_1, потому что быстрый
    # серверный редирект может сразу отправить нас на чужой сайт.
    run.log("[*] Проверяю конечный адрес после перехода...")

    try:
        run.final_url = _settle_url(run.target_page, run.log)
    except Exception as e:
        return run.problem(
            f"не удалось определить конечный адрес после перехода: "
            f"{type(e).__name__}"
        )

    if not run.final_url:
        try:
            run.final_url = run.target_page.url or ""
        except Exception:
            run.final_url = ""

    run.log(f"[i] Конечный адрес после перехода: {run.final_url}")

    src = bot.extract_domain(run.url)
    dst = bot.extract_domain(run.final_url)

    if not dst:
        return run.problem(
            "после клика не удалось определить конечный домен"
        )

    # Если вообще остались на Яндексе — это не переход на зеркало.
    if "yandex." in dst or dst == "ya.ru":
        return run.problem(
            "клик по выдаче никуда не увёл — остались на Яндексе"
        )

    # Если после стабилизации всё ещё исходный домен,
    # значит зеркало пока не получено.
    if _same_site(src, dst):
        return run.problem(
            f"перехода на зеркало не произошло — остались на {dst}"
        )

    # Ключевая новая проверка:
    # если конечный домен не относится к разрешённым брендам/зеркалам,
    # сразу прекращаем прогон.
    if not _final_url_brand_ok(run):
        return False

    # Только после проверки конечного сайта требуем Screenshot_1.
    if not run.transition_shot_ok:
        return run.problem(
            "не удалось снять Screenshot_1: наша ссылка не открылась "
            "как страница (похоже, серверный редирект сразу на зеркало)"
        )

    return True


def open_profile_chrome(log_fn, stop_event=None, on_step=None,
                        start_url: str = "") -> list:
    choice = _browser_choice()
    exe = choice.get("exe", "")
    title = choice.get("title") or "браузер"
    out = [{"profile": os.path.basename(current_profile_dir()), "ok": False,
            "uid": False, "spravka": False, "note": ""}]
    if not exe:
        log_fn("[!] Браузер на этой машине не нашёлся — открой профиль обычной "
               "кнопкой (через Playwright).")
        out[0]["note"] = "браузер не найден"
        if on_step:
            on_step(1, 1, out)
        return out

    target = (start_url or "").strip()
    if target and not re.match(r"^\w+://", target):
        target = "https://" + target

    bot.load_proxy_config()
    pool = bot._proxy_pool()
    proxy_raw = pool[0] if pool else ""
    profile_dir = current_profile_dir()
    args = [exe, f"--user-data-dir={profile_dir}",
            "--no-first-run", "--no-default-browser-check"] + LANGUAGE_LAUNCH_ARGS
    if proxy_raw:
        pd = bot._playwright_proxy_dict(proxy_raw) or {}
        server = pd.get("server", "")
        if server:
            args.append(f"--proxy-server={server}")
            if config.YANDEX_BYPASS_PROXY:
                args.append("--proxy-bypass-list=" + ";".join(YANDEX_DIRECT_HOSTS))
            log_fn(f"[i] Прокси {bot._proxy_label(proxy_raw)} задан ключом запуска.")
            if pd.get("username"):
                log_fn(f"[!] {title} спросит логин и пароль прокси окном — введи "
                       f"их РУКАМИ: логин {pd.get('username')}, "
                       f"пароль {pd.get('password')}")
    else:
        log_fn("[i] Прокси выключены — всё пойдёт напрямую, как и сам прогон.")
    if target:
        args.append(target)

    log_fn(f"[i] Профиль: {profile_dir}")
    log_fn(f"[i] {title} запускается БЕЗ Playwright: ни отладчика, ни эмуляции "
           f"размера, ни служебных ключей — обычное окно, как из ярлыка.")
    if target:
        log_fn(f"[*] Открываю {target}")
    else:
        log_fn("[!] Поле «Одна ссылка» пустое — открыл стартовую страницу. Это НЕ "
               "прогон: программа только держит окно и ждёт, пока ты его закроешь.")
    log_fn("[*] Пройди проверку, зайди куда нужно — и ПРОСТО ЗАКРОЙ ОКНО.")
    try:
        _free_profile(profile_dir, log_fn)
        _mark_profile_clean(profile_dir)
        proc = subprocess.Popen(args)
    except Exception as e:
        log_fn(f"[!] Не удалось запустить {title}: {e}")
        out[0]["note"] = "браузер не запустился"
        if on_step:
            on_step(1, 1, out)
        return out

    deadline = time.time() + 3600
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        if stop_event is not None and stop_event.is_set():
            log_fn("[!] Остановлено — окно оставляю открытым, закрой его сам.")
            break
        time.sleep(1.0)

    out[0]["ok"] = True
    out[0]["note"] = "окно закрыто, всё сохранено в профиль"
    log_fn("")
    log_fn(f"[✓] Окно закрыто. Куки и настройки остались в профиле "
           f"{os.path.basename(profile_dir)} — прогон пойдёт с ними.")
    if on_step:
        on_step(1, 1, out)
    return out


def _click_to_mirror(run: _Run, page, dom_key: str, what: str, point, metrics: dict,
                     refind_args: dict = None, transition_shot: str = "") -> bool:
    page_x, page_y = point
    screen_x, screen_y = _page_to_screen_xy(page_x, page_y, metrics)

    def _hand_over_to_native(hit_xy):
        try:
            sx, sy = _page_to_screen_xy(hit_xy[0], hit_xy[1], _get_page_metrics(page))
            _native_cursor.show(sx, sy)
        except Exception:
            _native_cursor.show(screen_x, screen_y)

    def _do_click():
        hit = _human_click_dom(page, run.log, dom_key, what, refind_args=refind_args)
        if hit is None:
            _human_click_at(page, page_x, page_y, run.log)
            hit = (page_x, page_y)
        _hand_over_to_native(hit)
        return hit

    def _do_retry():
        rx, ry = _dom_point(page, run.log, dom_key) or (page_x, page_y)
        _human_click_at(page, rx, ry, run.log, dwell_ms=(250, 400))
        _hand_over_to_native((rx, ry))
        return rx, ry

    debug = bool(transition_shot) and _transition_debug_on()
    rect = _window_capture_rect(page) if transition_shot else None

    burst = None
    hold = None
    if debug:
        burst = _TransitionBurst(run, rect)
        burst.arm(run.context, page)
    elif transition_shot:
        try:
            serp_url = page.url or ""
        except Exception:
            serp_url = ""
        hold = _TransitionHold(run, run.url, os.path.join(run.folder, transition_shot),
                               rect, referer=serp_url)
        if not hold.arm(run.context, page):
            hold = None

    try:
        if burst is not None:
            burst.start()
        popped_page, clicked_xy = _click_and_await_transition(
            page, run.context, _do_click, run.log, retry_click=_do_retry,
            on_tick=(burst.tick if burst is not None else None))
        if burst is not None:
            burst.run_out(page)
        if hold is not None:
            hold.takeover(popped_page, page)
    finally:
        if hold is not None:
            hold.disarm()
        if burst is not None:
            burst.finish()
    run.click_xy = clicked_xy or (page_x, page_y)

    if transition_shot:
        run.transition_shot_ok = bool(hold and hold.ok)
        if debug:
            run.log("[debug] Прогон диагностический — Screenshot_1 берётся запасным путём.")
        if not run.transition_shot_ok:
            run.log("[!] Коммита нашей ссылки не поймали — пробую снять кадр "
                    "перехода на лету.")
            run.transition_shot_ok = _shoot_transition_fallback(
                run, page, popped_page, transition_shot)

    if popped_page is not None:
        try:
            popped_page.wait_for_load_state("domcontentloaded", timeout=15000)
            run.target_page = popped_page
            _position_browser_window(run.context, popped_page, run.log)
            run.log(f"[i] Открылась новая вкладка: {popped_page.url}")
        except Exception:
            pass
    else:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
    return True


_MIRROR_BRAND_JS = r"""() => {
    const out = [];
    if (document.title) out.push(document.title);
    const metas = ['og:site_name', 'application-name', 'apple-mobile-web-app-title', 'og:title'];
    for (const m of metas) {
        const el = document.querySelector('meta[property="' + m + '"],meta[name="' + m + '"]');
        if (el && el.content) out.push(el.content);
    }
    const H = window.innerHeight || 800;
    let n = 0;
    const nodes = document.querySelectorAll(
        'header *, [class*="logo" i], [id*="logo" i], [class*="brand" i], a[href="/"], img');
    for (const el of nodes) {
        if (n > 60) break;
        let r;
        try { r = el.getBoundingClientRect(); } catch (e) { continue; }
        if (!r || r.width < 2 || r.top > H * 0.4) continue;
        const cls = (el.className && el.className.baseVal !== undefined) ? el.className.baseVal : (el.className || '');
        const bits = [el.getAttribute('alt'), el.getAttribute('aria-label'),
                      el.getAttribute('title'), el.id, cls];
        if (el.tagName === 'A' && el.innerText) bits.push(el.innerText);
        const t = bits.filter(Boolean).join(' ').trim();
        if (t) { out.push(t); n++; }
    }
    return out.join(' | ').slice(0, 3000);
}"""

def _final_url_brand_ok(run: _Run) -> bool:
    final_url = (run.final_url or "").strip()
    domain = bot.extract_domain(final_url)

    if not domain:
        return run.problem(
            "конечная ссылка не содержит корректного домена"
        )

    # Сначала проверяем обычные матчеры наших брендов:
    # 1xBet, Melbet, CatCasino, Mostbet, Pinco, Vavada, 1xCasino.
    brand = brand_for_url(final_url)
    if brand != UNKNOWN_BRAND_FOLDER:
        run.log(
            f"[+] Финальный домен соответствует нашему бренду: "
            f"{brand} ({domain})"
        )
        return True

    # Некоторые рабочие зеркала имеют обфусцированные домены,
    # поэтому дополнительно используем существующий allowlist зеркал из bot.py.
    try:
        approved_mirror = bool(
            bot._looks_like_real_mirror(final_url)
        )
    except Exception as e:
        run.log(
            f"[!] Не удалось проверить финальный домен "
            f"по списку зеркал ({type(e).__name__})"
        )
        approved_mirror = False

    if approved_mirror:
        run.log(
            f"[+] Финальный домен найден в списке "
            f"разрешённых зеркал: {domain}"
        )
        return True

    return run.problem(
        f"конечная ссылка ведёт на неподдерживаемый "
        f"бренд/домен: {domain}"
    )



def _mirror_brand_ok(run: _Run) -> bool:
    try:
        text = run.target_page.evaluate(_MIRROR_BRAND_JS) or ""
    except Exception as e:
        run.log(f"[i] Баннер/логотип: не смог прочитать разметку ({type(e).__name__}) — проверку пропускаю.")
        return True
    if not text.strip():
        run.log("[i] Баннер/логотип: в разметке нет текста бренда — проверку пропускаю.")
        return True
    brand = brand_for_url(text)   # матчеры брендов ищут ключевые слова в этом тексте
    if brand != UNKNOWN_BRAND_FOLDER:
        run.log(f"[+] Баннер/логотип бренда на зеркале: {brand}")
        return True
    snippet = " ".join(text.split())[:160]
    return run.problem(f"баннер/логотип зеркала не совпал ни с одним нашим брендом (нашёл: «{snippet}»)")


def _settle_on_mirror(run: _Run) -> bool:
    target_page = run.target_page
    run.log("[*] Жду стабилизации URL...")
    run.final_url = _settle_url(target_page, run.log)
    run.log(f"[+] Похоже, попали на зеркало: {run.final_url}")

    src, dst = bot.extract_domain(run.url), bot.extract_domain(run.final_url)
    if not dst:
        return run.problem("после клика адрес страницы не определился")
    if _same_site(src, dst):
        return run.problem(f"перехода на зеркало не произошло — остались на {dst}")
    if run.kind == LINK_REDIRECT and ("yandex." in dst or dst == "ya.ru"):
        return run.problem("клик по выдаче никуда не увёл — остались на Яндексе")

    _bring_browser_to_front(run.log, allowed_pids=run.pids)  # новая вкладка могла увести фокус
    _handoff_cursor_to_page(target_page, run.click_xy[0], run.click_xy[1], run.log)

    run.log("[*] Жду догрузки картинок/баннеров/видео на зеркале...")
    _wait_for_media(target_page, run.log)
    health = _wait_for_content(target_page, run.log)
    if not _has_content(health):
        _report_empty_page(health, "Зеркало", run.log)
        status = health.get("status")
        if status in bot.BLOCK_STATUS_CODES:
            return run.problem(f"зеркало отдало {status} — защита заблокировала IP")
        if _looks_dead(status, ""):
            return run.dead(f"зеркало отдало {status}")
        return run.problem("зеркало не отрисовалось — пустая страница")
    run.log(f"[i] Зеркало отрисовалось: {health.get('textLen')} символов текста, "
            f"{health.get('clickable')} кликабельных элементов")

    settled = _settle_url(target_page, run.log)
    if not _urls_same_page(settled, run.final_url):
        run.log("[i] Зеркало доехало не сразу — обновляю адрес и жду содержимое заново.")
        run.final_url = settled
        _wait_for_media(target_page, run.log)
        health = _wait_for_content(target_page, run.log)
        if not _has_content(health):
            _report_empty_page(health, "Зеркало", run.log)
            return run.problem("зеркало не отрисовалось — пустая страница")

    if not _final_url_brand_ok(run):
        return False

    if not _mirror_brand_ok(run):
        return False

    _set_cursor_overlay(target_page, run.click_xy[0], run.click_xy[1])
    _native_cursor.hide()   # подстраховка, если выше передача не состоялась
    return True


def _click_known_point(run: _Run, point, what: str, dom_key: str, dwell=(300, 500)) -> bool:
    hit = _human_click_dom(run.target_page, run.log, dom_key, what, dwell_ms=dwell)
    if hit:
        run.log(f"[+] Кликнул по {what} {hit}")
        return True
    if not isinstance(point, tuple):
        run.log(f"[!] {what}: элемент пропал со страницы")
        return False
    run.log(f"[!] {what}: элемент пропал — кликаю по последней известной точке {point}")
    _human_click_at(run.target_page, point[0], point[1], run.log, dwell_ms=dwell)
    return True


def _handle_leave_confirm(run: _Run, tries: int = 2) -> bool:
    page = run.target_page
    for attempt in range(tries):
        point = _dom_find(page, run.log, "leave_confirm",
                          "Окно подтверждения выхода", scroll=False)
        if point:
            hit = _human_click_dom(page, run.log, "leave_confirm",
                                   "кнопке «Все равно выйти»", dwell_ms=(250, 450))
            if hit is None and isinstance(point, tuple):
                _human_click_at(page, point[0], point[1], run.log, dwell_ms=(250, 450))
            run.log("[+] Подтвердил выход из регистрации")
            page.wait_for_timeout(random.randint(500, 900))
            return True
        if attempt + 1 < tries:
            page.wait_for_timeout(500)
    return False


_CASINO_LIKE = ("casino", "казино", "games", "игры")


_CASINO_CANDIDATES_JS = r"""() => {
    const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
    const words = ['казино', 'casino', 'games', 'игры'];
    const out = [];
    for (const el of document.querySelectorAll('*')) {
        const t = norm(el.innerText || el.textContent || '');
        if (!t || t.length > 24) continue;
        if (!words.some(w => t === w || t.startsWith(w + ' '))) continue;
        let child = false;
        for (const c of el.children) {
            if (norm(c.innerText || c.textContent || '') === t) { child = true; break; }
        }
        if (child) continue;
        const r = el.getBoundingClientRect();
        if (!r || r.width < 2 || r.height < 2) continue;
        out.push({tag: el.tagName.toLowerCase(), text: t,
                  x: Math.round(r.left), y: Math.round(r.top),
                  w: Math.round(r.width), h: Math.round(r.height),
                  href: (el.getAttribute && el.getAttribute('href')) || ''});
        if (out.length >= 8) break;
    }
    return out;
}"""


def _log_casino_candidates(page, log_fn) -> None:
    try:
        found = page.evaluate(_CASINO_CANDIDATES_JS) or []
    except Exception as e:
        log_fn(f"[i] Не удалось осмотреть страницу на пункты «Казино»: {e}")
        return
    if not found:
        log_fn("[i] На странице сейчас нет ни одного элемента с текстом «Казино» — "
               "список, похоже, не раскрылся.")
        return
    log_fn(f"[i] Элементы с текстом «Казино» на странице ({len(found)}):")
    for f in found:
        log_fn(f"      <{f['tag']}> '{f['text']}' в ({f['x']},{f['y']}) "
               f"{f['w']}x{f['h']}" + (f" href={f['href']}" if f['href'] else ""))


def _second_slots_click(run: _Run, before_url: str, label: str,
                        settle_ms: int = 1200) -> bool:
    page = run.target_page
    try:
        page.wait_for_timeout(settle_ms)
        now_url = page.url
    except Exception:
        return False
    if now_url != before_url:
        return False
    casino = any(w in (label or "").lower() for w in _CASINO_LIKE)
    run.log(f"[i] Кликаю по '{label or 'Слоты'}' второй раз"
            + (" — это пункт «Казино», он открывает раздел только со второго "
               "нажатия" if casino else " — от первого страница не изменилась"))
    hit = _human_click_dom(page, run.log, "slots_again",
                           f"'{label or 'Слоты'}' (пункт раскрывшегося списка)",
                           dwell_ms=(250, 450))
    if hit:
        run.log("[i] Попал в пункт раскрывшегося списка — в мобильной вёрстке "
                "раздел открывается именно им.")
    else:
        if casino:
            _log_casino_candidates(page, run.log)
        hit = _human_click_dom(page, run.log, "slots", f"'{label or 'Слоты'}' (второй клик)",
                               dwell_ms=(250, 450))
    if not hit:
        run.log("[i] Второй клик делать не по чему — пункта меню на странице уже нет.")
        return False
    run.log(f"[+] Кликнул повторно {hit}")
    try:
        page.wait_for_timeout(random.randint(600, 900))
    except Exception:
        pass
    _handle_leave_confirm(run, tries=1)
    return True


def _is_vavada(url: str) -> bool:
    return "vavada" in (url or "").lower()


def _shoot_slots_page(run: _Run) -> bool:
    target_page = run.target_page
    run.log("[*] Пролистываю страницу со слотами...")
    m = _get_page_metrics(target_page)
    scan_xy = _scroll_scan_down(target_page, m["innerWidth"], m["innerHeight"], run.log)
    _scroll_scan_up(target_page, m["innerWidth"], m["innerHeight"], scan_xy, run.log)
    _wait_for_media(target_page, run.log)

    if not run.shot(SHOT_2):
        return run.problem("не удалось снять Screenshot_2")
    run.shot2_done = True
    run.log("[+] Screenshot_2 снят на странице со слотами")
    return True


def _open_slots(run: _Run) -> bool:
    target_page = run.target_page

    if _is_vavada(run.url):
        run.log("[i] Vavada: главная и есть страница со слотами — кнопку 'Слоты' "
                "не ищу.")
        if "register" in (target_page.url or "").lower():
            run.log("[i] Стоим на регистрации — ухожу на главную по логотипу бренда.")
            brand = _dom_find(target_page, run.log, "brand", "Логотип бренда")
            if brand and _click_known_point(run, brand, "логотипу бренда", "brand"):
                _handle_leave_confirm(run)
                _wait_for_media(target_page, run.log)
                target_page.wait_for_timeout(random.randint(800, 1300))
                _wait_for_content(target_page, run.log, timeout_ms=8000)
        return _shoot_slots_page(run)

    found = {}
    point = _dom_find(target_page, run.log, "slots", "'Слоты'", out=found)
    if not point:
        run.log("[i] Кнопки 'Слоты' нет — пробую уйти на главную по логотипу бренда.")
        brand = _dom_find(target_page, run.log, "brand", "Логотип бренда")
        before_brand = target_page.url
        if brand and _click_known_point(run, brand, "логотипу бренда", "brand"):
            _handle_leave_confirm(run)
            if not _await_navigation(run, target_page, before_brand, "Клик по логотипу"):
                _wait_for_media(target_page, run.log)
                _wait_for_content(target_page, run.log, timeout_ms=8000)
            target_page.wait_for_timeout(random.randint(800, 1300))
            point = _dom_find(target_page, run.log, "slots", "'Слоты'", out=found)
        if not point:
            return run.problem("на зеркале не нашлась кнопка 'Слоты'")

    label = (found.get("text") or "").strip()
    before_url = target_page.url
    if not _click_known_point(run, point, f"'{label or 'Слоты'}'", "slots"):
        return run.problem("не удалось кликнуть по 'Слотам'")
    _handle_leave_confirm(run)
    _second_slots_click(run, before_url, label)
    _wait_for_media(target_page, run.log)
    target_page.wait_for_timeout(random.randint(800, 1300))
    return _shoot_slots_page(run)


def _registration_still_open(run: _Run, form_ctx) -> bool:
    res, _ = _find_form_dom(run.target_page, lambda m: None)
    return bool(res and res.get("open"))


def _leave_registration(run: _Run, dom_form: dict, form_ctx) -> bool:
    target_page = run.target_page

    brand = _dom_find(target_page, run.log, "brand", "Баннер бренда над формой")
    if brand:
        before = target_page.url
        if _click_known_point(run, brand, "баннеру бренда", "brand"):
            _handle_leave_confirm(run)
            if not _await_navigation(run, target_page, before, "Клик по баннеру бренда"):
                target_page.wait_for_timeout(random.randint(600, 1000))
            if not _registration_still_open(run, form_ctx):
                run.log("[+] Ушёл с формы регистрации по баннеру бренда")
                return True
            run.log("[i] После клика по баннеру форма всё ещё открыта — закрываю крестиком.")

    close_pt = _dom_point(target_page, run.log, "close", what="Крестик формы", ctx=form_ctx)
    if close_pt:
        before = target_page.url
        if _click_known_point(run, close_pt, "крестику формы", "close"):
            _handle_leave_confirm(run)
            target_page.wait_for_timeout(random.randint(600, 1000))
            if not _registration_still_open(run, form_ctx):
                run.log("[+] Форма регистрации закрыта")
                return True

    run.log("[!] Уйти с формы регистрации не получилось — пробую искать 'Слоты' как есть.")
    return False


def _do_registration(run: _Run, dom_form: dict, form_ctx) -> bool:
    fields = (dom_form or {}).get("fields") or []
    if not fields:
        return run.problem("форма регистрации открылась, но полей в ней нет")
    _click_through_dom_fields(run.target_page, dom_form, run.log, ctx=form_ctx)
    run.log("[i] Поля формы регистрации прокликаны.")
    return True


def _stage_mirror(run: _Run) -> bool:
    target_page = run.target_page
    run.log("[*] Разбираю зеркало по разметке...")
    wait_ms = _mirror_form_wait_ms(getattr(target_page, "url", ""))
    dom_form, form_ctx = _find_form_dom(target_page, run.log, timeout_ms=wait_ms)
    dom_form = dom_form or {}

    if dom_form.get("open"):
        run.log("[i] Сразу попали в форму регистрации — прокликиваю поля, потом ищу 'Слоты'.")
        reg_ok = _do_registration(run, dom_form, form_ctx)
        _leave_registration(run, dom_form, form_ctx)
        slots_ok = _open_slots(run)
        return reg_ok and slots_ok

    run.log("[i] Форма регистрации не открыта — иду по 'Слоты' → 'Регистрация'.")
    slots_ok = _open_slots(run)

    reg_point = _dom_find(target_page, run.log, "register", "Кнопка регистрации")
    if not reg_point:
        return run.problem("на зеркале не нашлась кнопка регистрации")
    before_reg = target_page.url
    if not _click_known_point(run, reg_point, "кнопке регистрации", "register"):
        return run.problem("не удалось кликнуть по кнопке регистрации")
    target_page.wait_for_timeout(random.randint(500, 800))  # дать модалке открыться/анимироваться

    _await_navigation(run, target_page, before_reg, "Клик по регистрации")

    opened, opened_ctx = _find_form_dom(target_page, run.log, timeout_ms=FORM_WAIT_MS)
    if not opened:
        return run.problem("форма регистрации не открылась после клика")
    return _do_registration(run, opened, opened_ctx) and slots_ok


def _missing_proof_files(folder: str) -> list:
    out = []
    for name in _PROOF_FILES:
        path = os.path.join(folder, name)
        if not os.path.isfile(path) or os.path.getsize(path) < 1024:
            out.append(name)
    return out


def _run_once(p, url: str, kind: str, output_root: str, log_fn, proxy_attempts: list) -> RunResult:
    brand = brand_for_url(url)
    day_folder = os.path.join(output_root, f"{datetime.now():%d.%m.%Y} Работа", brand)
    os.makedirs(day_folder, exist_ok=True)
    folder = os.path.join(day_folder, f"_в_работе_{os.getpid()}_{int(time.time() * 1000) % 100000}")
    os.makedirs(folder, exist_ok=True)

    run = _Run(p, url, kind, folder, proxy_attempts, log_fn)
    reset_mobile_device()
    log_fn(f"[*] Режим: {kind}, бренд: {brand}. Окно браузера откроется в правом нижнем "
           f"углу экрана — держи это место ничем не перекрытым до конца прогона.")
    if kind == LINK_REDIRECT:
        force_desktop = "редирект — кадр перехода снимается с адресной строкой"
    else:
        force_desktop = desktop_url_reason(url)
    desktop = _desktop_for_run() if force_desktop else _desktop_for_run(True)
    if force_desktop:
        log_fn(f"[i] Прогон идёт десктопным браузером ({force_desktop}) — "
               f"мобильная эмуляция выключена на эту ссылку.")
    try:
        with desktop:
            stage_ok = _stage_yandex(run) if kind == LINK_REDIRECT else _stage_landing(run)
            if stage_ok and _settle_on_mirror(run):
                _stage_mirror(run)
            run.stop_recording()
    except Exception as e:
        log_fn(f"[!] Ошибка прогона: {e}")
        run.problem(f"внутренняя ошибка: {str(e).splitlines()[0][:120]}")
    finally:
        run.stop_recording()
        run.close_browser()
        _native_cursor.hide()  # подстраховка: вдруг оборвались посреди перехода

    return _finish_run(run, day_folder)


def _finish_run(run: _Run, day_folder: str) -> RunResult:
    res = RunResult(run.url, run.kind, final_url=run.final_url, proxy=run.proxy)
    res.retry_as_redirect = run.retry_as_redirect

    if run.retry_as_redirect:
        res.outcome, res.reason = proof_xlsx.PROBLEM, "ссылка оказалась редиректом"
    elif run.dead_reason:
        res.outcome, res.reason = proof_xlsx.DEAD, run.dead_reason
    elif run.problems:
        res.outcome, res.reason = proof_xlsx.PROBLEM, "; ".join(run.problems[:3])
    else:
        missing = _missing_proof_files(run.folder)
        if missing:
            res.outcome = proof_xlsx.PROBLEM
            res.reason = "пруф неполон, не хватает файлов: " + ", ".join(missing)
        else:
            res.outcome, res.reason = proof_xlsx.OK, ""

    if res.outcome == proof_xlsx.OK:
        n = _next_folder_number(day_folder)
        final = os.path.join(day_folder, str(n))
        try:
            os.replace(run.folder, final)
            res.folder = f"{os.path.basename(day_folder)}/{n}"
            run.log(f"[✓] Пруф сохранён: {final}")
            _compress_queue.submit(final, run.log)
        except Exception as e:
            run.log(f"[!] Не удалось переименовать папку прогона: {e}")
            res.folder = os.path.basename(run.folder)
    else:
        try:
            shutil.rmtree(run.folder, ignore_errors=True)
        except Exception:
            pass
        run.log(f"[✗] Результат не сохранён ({res.reason})")
    return res


def process_url(url: str, output_root: str, log_fn, primary_proxy: str = "",
                pool: list = None, p=None) -> RunResult:
    url = _normalize_url(url)
    if pool is None:
        bot.load_proxy_config()
        pool = bot._proxy_pool()
    if not primary_proxy and pool:
        primary_proxy = bot._pick_proxy()
    attempts = _proxy_attempts_for(primary_proxy, pool)
    proxy_label = bot._proxy_label(attempts[0]) if attempts[0] else "без прокси"
    log_fn(f"[*] Прокси для этой ссылки: {proxy_label}")

    kind, dead_reason, probe_url = _classify_link(url, attempts, log_fn)
    if kind is None:
        log_fn(f"[✗] Сайт мёртв: {dead_reason}")
        return RunResult(url, "", proof_xlsx.DEAD, dead_reason, probe_url, proxy_label)

    def _go(pw):
        res = _run_once(pw, url, kind, output_root, log_fn, attempts)
        if res.retry_as_redirect:
            log_fn("[*] Переигрываю ссылку как редирект — через выдачу Яндекса.")
            res = _run_once(pw, url, LINK_REDIRECT, output_root, log_fn, attempts)
        return res

    if p is not None:
        res = _go(p)
    else:
        with sync_playwright() as own:
            res = _go(own)

    if not res.proxy:
        res.proxy = proxy_label
    if p is None:
        _compress_queue.drain(log_fn)
    log_fn("=== ГОТОВО ===")
    return res


