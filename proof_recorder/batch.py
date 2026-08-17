from __future__ import annotations

import re

from playwright.sync_api import sync_playwright

import bot
from . import proof_xlsx, proof_sheet
from .browser import _plan_proxies
from .capture import _compress_queue
from .mobile import _mobile_ua_enabled, set_mobile_run_for_url
from .pipeline import RunResult, process_url
def run_batch(xlsx_path: str, output_root: str, log_fn, stop_event=None, on_progress=None) -> str:
    rows = proof_xlsx.read_links(xlsx_path, log_fn)
    if not rows:
        log_fn("[!] В файле не нашлось ни одной ссылки.")
        return ""

    bot.load_proxy_config()
    pool = bot._proxy_pool()
    if pool:
        per = len(rows) / len(pool)
        log_fn(f"[i] Прокси в работе: {len(pool)}, ссылок: {len(rows)} — "
               f"примерно по {per:.1f} ссылки на прокси.")
    else:
        log_fn("[!] Прокси выключены или список пуст — все ссылки пойдут с вашего IP.")
    plan = _plan_proxies(len(rows), pool)

    report = proof_xlsx.result_path_for(output_root, xlsx_path)
    wb = proof_xlsx.ResultWorkbook(report, log_fn)
    log_fn(f"[i] Отчёт: {report}")

    with sync_playwright() as p:
        for i, row in enumerate(rows):
            if stop_event is not None and stop_event.is_set():
                log_fn(f"[!] Остановлено пользователем на {i + 1}-й ссылке из {len(rows)}.")
                break
            log_fn("")
            log_fn(f"─── [{i + 1}/{len(rows)}] {row.url} ───")
            try:
                res = process_url(row.url, output_root, log_fn,
                                  primary_proxy=plan[i], pool=pool, p=p)
            except Exception as e:
                log_fn(f"[!] Непредвиденная ошибка на ссылке: {e}")
                res = RunResult(row.url, "", proof_xlsx.PROBLEM,
                                f"непредвиденная ошибка: {str(e).splitlines()[0][:120]}")
            wb.add(row.index, row.url, res.outcome, res.kind, res.reason,
                   res.final_url, res.proxy, res.folder)
            log_fn(f"[=] Итог: {proof_xlsx.status_title(res.outcome).upper()}"
                   + (f" — {res.reason}" if res.reason else ""))
            if on_progress:
                on_progress(i + 1, len(rows), dict(wb.counts))

    _compress_queue.drain(log_fn)

    c = wb.counts
    log_fn("")
    log_fn(f"════ ПРОГОН ЗАВЕРШЁН: успешно {c[proof_xlsx.OK]}, "
           f"проблем {c[proof_xlsx.PROBLEM]}, мёртвых {c[proof_xlsx.DEAD]} ════")
    log_fn(f"[i] Отчёт: {report}")
    return report


def parse_sheet_jobs(text: str, default_sheet: str) -> list:
    text = (text or "").strip()
    if not text:
        raise ValueError("диапазон не указан")
    known = {sh.lower(): sh for sh in proof_sheet.SHEETS}
    jobs, current = [], default_sheet
    for chunk in re.split(r"[;,]", text):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = re.match(r"^([A-Za-zА-Яа-я_]+)\s*[:\s]\s*(.+)$", chunk)
        if m:
            name = known.get(m.group(1).strip().lower())
            if not name:
                raise ValueError(f"неизвестный лист {m.group(1)!r} — "
                                 f"есть только {', '.join(proof_sheet.SHEETS)}")
            current, chunk = name, m.group(2).strip()
        row_from, row_to = proof_sheet.parse_range(chunk)
        jobs.append((current, row_from, row_to))
    if not jobs:
        raise ValueError("диапазон не указан")
    return jobs


def run_batch_sheets(apps_script_url: str, jobs: list, user: str, output_root: str,
                     log_fn, stop_event=None, on_progress=None) -> str:
    report = proof_xlsx.result_path_for(
        output_root, "_".join(sorted({sh for sh, _, _ in jobs})))
    wb = proof_xlsx.ResultWorkbook(report, log_fn)
    log_fn(f"[i] Локальный отчёт (общий на весь прогон): {report}")

    for n, (sheet, row_from, row_to) in enumerate(jobs, 1):
        if stop_event is not None and stop_event.is_set():
            log_fn(f"[!] Остановлено — осталось невыполненных заданий: {len(jobs) - n + 1}")
            break
        log_fn("")
        log_fn(f"══════ ЗАДАНИЕ {n}/{len(jobs)}: лист «{sheet}», строки "
               f"{row_from - 1}-{row_to - 1} ══════")
        try:
            run_batch_sheet(apps_script_url, sheet, f"{row_from - 1}-{row_to - 1}",
                            user, output_root, log_fn, stop_event=stop_event,
                            on_progress=on_progress, workbook=wb)
        except Exception as e:
            log_fn(f"[!] Задание {n} прервалось: {e}")

    c = wb.counts
    log_fn("")
    log_fn(f"════ ВСЕ ЗАДАНИЯ ЗАВЕРШЕНЫ: успешно {c[proof_xlsx.OK]}, "
           f"проблем {c[proof_xlsx.PROBLEM]}, мёртвых {c[proof_xlsx.DEAD]} ════")
    log_fn(f"[i] Отчёт: {report}")
    return report


def run_batch_sheet(apps_script_url: str, sheet: str, row_range: str, user: str,
                    output_root: str, log_fn, stop_event=None, on_progress=None,
                    workbook=None) -> str:
    sheet_api = proof_sheet.ProofSheet(apps_script_url, log_fn)
    row_from, row_to = proof_sheet.parse_range(row_range)
    log_fn(f"[*] Занимаю строки {row_from}-{row_to} листа «{sheet}» под именем «{user}»...")
    rows, busy = sheet_api.claim(sheet, row_from, row_to, user)
    if busy:
        log_fn(f"[!] Заняты другими и пропущены ({len(busy)}): "
               f"{', '.join(str(b) for b in busy[:15])}"
               + (" и др." if len(busy) > 15 else ""))
    if not rows:
        log_fn("[!] Свободных ссылок в этом диапазоне не нашлось — брать нечего.")
        return ""
    log_fn(f"[i] Взято в работу ссылок: {len(rows)}")

    targets = []
    for r in rows:
        target = proof_sheet.pick_url(sheet, r.url, r.final_url)
        if target != r.url:
            log_fn(f"[i] Строка {r.row}: беру FINAL_URL ({target}) вместо URL ({r.url})")
        targets.append(target)

    bot.load_proxy_config()
    pool = bot._proxy_pool()
    if pool:
        log_fn(f"[i] Прокси в работе: {len(pool)}, ссылок: {len(rows)} — "
               f"примерно по {len(rows) / len(pool):.1f} ссылки на прокси.")
    else:
        log_fn("[!] Прокси выключены или список пуст — все ссылки пойдут с вашего IP.")
    plan = _plan_proxies(len(rows), pool)

    if workbook is not None:
        wb = workbook
        report = wb.path
    else:
        report = proof_xlsx.result_path_for(output_root, f"{sheet}_{row_from}-{row_to}.xlsx")
        wb = proof_xlsx.ResultWorkbook(report, log_fn)
        log_fn(f"[i] Локальный отчёт: {report}")

    done_rows = set()
    try:
        with sync_playwright() as p:
            for i, row in enumerate(rows):
                if stop_event is not None and stop_event.is_set():
                    log_fn(f"[!] Остановлено пользователем на {i + 1}-й ссылке из {len(rows)}.")
                    break
                target = targets[i]
                set_mobile_run_for_url(target)
                log_fn("")
                log_fn(f"─── [{i + 1}/{len(rows)}] строка {row.row}: {target} ───"
                       + ("  [мобильный]" if _mobile_ua_enabled() else "  [десктоп]"))
                try:
                    res = process_url(target, output_root, log_fn,
                                      primary_proxy=plan[i], pool=pool, p=p)
                except Exception as e:
                    log_fn(f"[!] Непредвиденная ошибка на ссылке: {e}")
                    res = RunResult(target, "", proof_xlsx.PROBLEM,
                                    f"непредвиденная ошибка: {str(e).splitlines()[0][:120]}")

                wb.add(row.row, target, res.outcome, res.kind, res.reason,
                       res.final_url, res.proxy, res.folder)
                sheet_api.mark(sheet, row.row, res.outcome)
                done_rows.add(row.row)
                log_fn(f"[=] Итог: {proof_xlsx.status_title(res.outcome).upper()}"
                       + (f" — {res.reason}" if res.reason else ""))
                if on_progress:
                    on_progress(i + 1, len(rows), dict(wb.counts))
    finally:
        left = [r.row for r in rows if r.row not in done_rows]
        if left:
            freed = sheet_api.release(sheet, left, user)
            log_fn(f"[i] Освободил непроверенных строк: {freed} из {len(left)}")
        _compress_queue.drain(log_fn)

    c = wb.counts
    log_fn("")
    log_fn(f"════ ПРОГОН ЗАВЕРШЁН: успешно {c[proof_xlsx.OK]}, "
           f"проблем {c[proof_xlsx.PROBLEM]}, мёртвых {c[proof_xlsx.DEAD]} ════")
    log_fn(f"[i] Локальный отчёт: {report}")
    return report


