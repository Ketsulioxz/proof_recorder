from __future__ import annotations

import asyncio
import os
import re
import threading


import bot
from . import proof_xlsx, proof_sheet
from .batch import parse_sheet_jobs, run_batch_sheets
from .browser import browser_processes, copy_user_profile
from .config import _browser_choice, current_profile_dir, load_proof_settings, save_proof_settings
from .mobile import _mobile_supported, _mobile_ua_enabled, desktop_url_reason, set_mobile_run_for_url
from .network import reset_site_data
from .pipeline import open_profile_chrome, process_url
from .winmgr import MONITOR_AUTO, list_monitors
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


class ProofRecorderApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Запись пруфов — лендинги и редиректы")
        root.geometry("900x840")

        bot.load_proxy_config()  # подтягиваем сохранённые прокси (тот же proxy_config.json, что и bot.py)
        self.proxy_rows = []  # [{"url", "var", "row_frame", "status_lbl"}]
        self.stop_event = threading.Event()
        self.busy = False

        frm = ttk.Frame(root, padding=10)
        frm.pack(fill=tk.BOTH, expand=True)

        src = ttk.LabelFrame(frm, text="Ссылки из Google Sheets", padding=8)
        src.grid(row=0, column=0, columnspan=2, sticky="we", rowspan=2)
        src.columnconfigure(1, weight=1)

        ttk.Label(src, text="Адрес Apps Script (/exec):").grid(row=0, column=0, sticky="w")
        self.sheet_url_var = tk.StringVar(
            value=load_proof_settings("apps_script_url",
                                      proof_sheet.DEFAULT_APPS_SCRIPT_URL))
        self.sheet_url_var.trace_add("write", lambda *a: save_proof_settings(
            apps_script_url=self.sheet_url_var.get()))
        ttk.Entry(src, textvariable=self.sheet_url_var).grid(
            row=0, column=1, columnspan=3, sticky="we", padx=(6, 0))

        ttk.Label(src, text="Лист:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.sheet_name_var = tk.StringVar(value=load_proof_settings("sheet", proof_sheet.SHEET_YANDEX))
        self.sheet_name_var.trace_add("write", lambda *a: save_proof_settings(
            sheet=self.sheet_name_var.get()))
        pick = ttk.Frame(src)
        pick.grid(row=1, column=1, sticky="w", padx=(6, 0), pady=(6, 0))
        for name in proof_sheet.SHEETS:
            ttk.Radiobutton(pick, text=name, value=name,
                            variable=self.sheet_name_var).pack(side="left", padx=(0, 10))
        ttk.Button(pick, text="Сколько свободно?",
                   command=self._show_sheet_info).pack(side="left", padx=(8, 0))

        ttk.Label(src, text="Строки:").grid(row=2, column=0, sticky="nw", pady=(6, 0))
        jobs_box = ttk.Frame(src)
        jobs_box.grid(row=2, column=1, columnspan=3, sticky="we", padx=(6, 0), pady=(6, 0))

        add_row = ttk.Frame(jobs_box)
        add_row.pack(fill="x")
        self.job_sheet_var = tk.StringVar(value=proof_sheet.SHEET_YANDEX)
        ttk.Combobox(add_row, textvariable=self.job_sheet_var, width=9, state="readonly",
                     values=list(proof_sheet.SHEETS)).pack(side="left")
        ttk.Label(add_row, text="  с ").pack(side="left")
        self.job_from_var = tk.StringVar(value="1")
        ttk.Entry(add_row, textvariable=self.job_from_var, width=6).pack(side="left")
        ttk.Label(add_row, text=" по ").pack(side="left")
        self.job_to_var = tk.StringVar(value="30")
        ttk.Entry(add_row, textvariable=self.job_to_var, width=6).pack(side="left")
        ttk.Button(add_row, text="Добавить", command=self._add_job).pack(side="left", padx=(8, 0))
        ttk.Button(add_row, text="Удалить выбранное",
                   command=self._del_job).pack(side="left", padx=(4, 0))
        ttk.Button(add_row, text="Очистить",
                   command=self._clear_jobs).pack(side="left", padx=(4, 0))

        self.jobs_list = tk.Listbox(jobs_box, height=4, activestyle="none",
                                    font=("Helvetica", 9))
        self.jobs_list.pack(fill="x", pady=(4, 0))
        self.range_var = tk.StringVar(value=load_proof_settings("row_range", "1-30"))
        self.range_var.trace_add("write", lambda *a: save_proof_settings(
            row_range=self.range_var.get()))
        self._reload_jobs_list()

        ttk.Label(src, text="Ваше имя:").grid(row=1, column=2, sticky="e", pady=(6, 0))
        self.user_var = tk.StringVar(value=load_proof_settings("user", ""))
        self.user_var.trace_add("write", lambda *a: save_proof_settings(user=self.user_var.get()))
        ttk.Entry(src, textvariable=self.user_var, width=18).grid(
            row=1, column=3, sticky="w", padx=(6, 0), pady=(6, 0))

        ttk.Label(src, text="Имя вписывается в столбец USER выбранных строк — пока оно там, "
                            "эти ссылки не достанутся другому. У пройденных ссылок имя "
                            "остаётся, у жёлтых и красных снимается.",
                  foreground="#888", font=("Helvetica", 8), wraplength=700,
                  justify="left").grid(row=3, column=0, columnspan=4, sticky="w", pady=(6, 0))

        ttk.Label(frm, text="Папка сохранения:").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.output_var = tk.StringVar(value=os.getcwd())
        ttk.Entry(frm, textvariable=self.output_var, width=58).grid(
            row=3, column=0, sticky="we")
        ttk.Button(frm, text="Обзор...", command=self._browse_out).grid(
            row=3, column=1, sticky="e", padx=(6, 0))

        ttk.Label(frm, text="Пруфы раскладываются по папкам: «<дата> Работа / <Бренд> / <номер>». "
                            "Окно браузера открывается в правом нижнем углу экрана и снимается "
                            "вместе с панелью задач — держи это место свободным и не работай за "
                            "компьютером во время прогона.",
                  foreground="#888", font=("Helvetica", 8), wraplength=760, justify="left").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(6, 8))

        self._build_monitor_row(frm, row=5)
        self._build_proxy_section(frm, start_row=6)

        btns = ttk.Frame(frm)
        btns.grid(row=13, column=0, columnspan=2, sticky="we", pady=(8, 4))
        self.run_btn = ttk.Button(btns, text="Проверить список", command=self._run_batch)
        self.run_btn.pack(side="left")
        self.stop_btn = ttk.Button(btns, text="Стоп", command=self._on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(6, 16))

        ttk.Label(btns, text="Одна ссылка:").pack(side="left")
        self.url_var = tk.StringVar()
        ttk.Entry(btns, textvariable=self.url_var, width=34).pack(side="left", padx=(6, 6))
        self.single_btn = ttk.Button(btns, text="Проверить", command=self._run_single)
        self.single_btn.pack(side="left")

        self.progress_var = tk.StringVar(value="")
        ttk.Label(frm, textvariable=self.progress_var, font=("Helvetica", 9, "bold")).grid(
            row=14, column=0, columnspan=2, sticky="w", pady=(0, 4))

        self.log_box = tk.Text(frm, height=18, wrap="word", state="disabled")
        self.log_box.grid(row=15, column=0, columnspan=2, sticky="nsew")
        frm.rowconfigure(15, weight=1)
        frm.columnconfigure(0, weight=1)

        scrollbar = ttk.Scrollbar(frm, command=self.log_box.yview)
        scrollbar.grid(row=15, column=2, sticky="ns")
        self.log_box["yscrollcommand"] = scrollbar.set

        self._reload_monitors()

    def _build_monitor_row(self, frm, row: int):
        wrap = ttk.Frame(frm)
        wrap.grid(row=row, column=0, columnspan=2, sticky="we", pady=(2, 0))

        ttk.Label(wrap, text="Монитор для программы:").pack(side="left")
        self.monitor_var = tk.StringVar()
        self.monitor_box = ttk.Combobox(wrap, textvariable=self.monitor_var,
                                        state="readonly", width=34)
        self.monitor_box.pack(side="left", padx=(6, 4))
        self.monitor_box.bind("<<ComboboxSelected>>", lambda e: self._save_monitor())
        ttk.Button(wrap, text="Обновить", width=10,
                   command=self._reload_monitors).pack(side="left")

        ttk.Label(wrap, text="Окно откроется в правом нижнем углу ЭТОГО экрана.",
                  foreground="#888", font=("Helvetica", 8)).pack(side="left", padx=(10, 0))

    def _reload_monitors(self):
        self.monitors = list_monitors()
        titles = ["Как в системе (основной)"] + [m["title"] for m in self.monitors]
        self.monitor_box["values"] = titles
        try:
            saved = int(load_proof_settings("monitor_index", MONITOR_AUTO))
        except (TypeError, ValueError):
            saved = MONITOR_AUTO
        if saved >= 0 and not any(m["index"] == saved for m in self.monitors):
            self._log(f"[!] Монитор №{saved + 1} из настроек не найден — беру основной.")
            save_proof_settings(monitor_index=MONITOR_AUTO)
            saved = MONITOR_AUTO
        self.monitor_var.set(titles[saved + 1] if saved >= 0 else titles[0])

    def _save_monitor(self):
        idx = self.monitor_box.current() - 1   # 0-я строка — «как в системе»
        save_proof_settings(monitor_index=idx if idx >= 0 else MONITOR_AUTO)
        self._log(f"[i] Программа будет работать на мониторе: {self.monitor_var.get()}")

    def _build_proxy_section(self, frm, start_row: int):
        ttk.Separator(frm, orient="horizontal").grid(
            row=start_row, column=0, columnspan=2, sticky="we", pady=(2, 6))

        row1 = ttk.Frame(frm)
        row1.grid(row=start_row + 1, column=0, columnspan=2, sticky="we")

        self.use_proxy_var = tk.BooleanVar(value=bot.USE_PROXY)
        self.use_proxy_var.trace_add("write", lambda *a: self._save_proxies_now())
        ttk.Checkbutton(row1, text="Прокси включён", variable=self.use_proxy_var).pack(side="left")

        self.yandex_direct_var = tk.BooleanVar(value=load_proof_settings())
        self.yandex_direct_var.trace_add(
            "write", lambda *a: save_proof_settings(self.yandex_direct_var.get()))
        ttk.Checkbutton(row1, text="Яндекс — мимо прокси (с моего IP)",
                        variable=self.yandex_direct_var).pack(side="left", padx=(12, 0))

        self.manual_btn = ttk.Button(row1, text="Настроить профиль вручную",
                                     command=self._run_manual_profile)
        self.manual_btn.pack(side="left", padx=(16, 6))

        ttk.Label(row1, text="Добавить:").pack(side="left", padx=(16, 4))
        self.proxy_entry = ttk.Entry(row1, width=32)
        self.proxy_entry.pack(side="left", padx=(0, 6))
        self.proxy_entry.bind("<Return>", lambda e: self._on_add_proxy())
        ttk.Button(row1, text="+ Добавить", command=self._on_add_proxy).pack(side="left", padx=(0, 6))
        ttk.Button(row1, text="Проверить все", command=self._on_check_all_proxies).pack(side="left")

        ttk.Label(frm, text="ip:port | ip:port:user:pass | user:pass@host:port",
                  font=("Helvetica", 8), foreground="#888").grid(
            row=start_row + 2, column=0, columnspan=2, sticky="w")

        row4 = ttk.Frame(frm)
        row4.grid(row=start_row + 3, column=0, columnspan=2, sticky="we", pady=(4, 0))

        # Режим зафиксирован: свой запуск Яндекс.Браузера. Выбор браузера убран.
        save_proof_settings(browser_mode="attach", browser_key="yandex")
        ttk.Label(row4, text="Браузер: Яндекс (свой запуск)").pack(side="left", padx=(0, 8))

        self.copy_profile_btn = ttk.Button(row4, text="Взять куки моего браузера",
                                           command=self._copy_user_profile)
        self.copy_profile_btn.pack(side="left")
        self.cookies_btn = ttk.Button(row4, text="Стереть данные сайта",
                                      command=self._reset_site_data)
        self.cookies_btn.pack(side="left", padx=(6, 0))

        list_wrap = ttk.Frame(frm, relief="solid", borderwidth=1)
        list_wrap.grid(row=start_row + 4, column=0, columnspan=2, sticky="we", pady=(4, 6))
        self.proxy_canvas = tk.Canvas(list_wrap, bg="white", height=100, highlightthickness=0)
        proxy_scroll = ttk.Scrollbar(list_wrap, orient="vertical", command=self.proxy_canvas.yview)
        self.proxy_list_inner = tk.Frame(self.proxy_canvas, bg="white")
        self.proxy_list_inner.bind(
            "<Configure>",
            lambda e: self.proxy_canvas.configure(scrollregion=self.proxy_canvas.bbox("all")))
        self.proxy_canvas.create_window((0, 0), window=self.proxy_list_inner, anchor="nw")
        self.proxy_canvas.configure(yscrollcommand=proxy_scroll.set)
        self.proxy_canvas.pack(side="left", fill="both", expand=True)
        proxy_scroll.pack(side="right", fill="y")

        def _on_wheel(event):
            self.proxy_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
            return "break"

        self.proxy_canvas.bind("<Enter>", lambda e: self.proxy_canvas.bind_all("<MouseWheel>", _on_wheel))
        self.proxy_canvas.bind("<Leave>", lambda e: self.proxy_canvas.unbind_all("<MouseWheel>"))

        for p in bot.PROXY_LIST:
            self._add_proxy_row(p.get("url", ""), p.get("enabled", True))

    def _add_proxy_row(self, url: str, enabled: bool = True):
        url = url.strip()
        if not url:
            return
        row = tk.Frame(self.proxy_list_inner, bg="white")
        row.pack(fill="x", padx=4, pady=1)

        var = tk.BooleanVar(value=enabled)
        var.trace_add("write", lambda *a: self._save_proxies_now())
        tk.Checkbutton(row, variable=var, bg="white", activebackground="white").pack(side="left")

        label_text = bot._proxy_label(bot._normalize_proxy(url)) or url
        tk.Label(row, text=label_text, font=("Courier", 10), bg="white",
                 fg="#333", anchor="w", width=26).pack(side="left", padx=(2, 6))

        status_lbl = tk.Label(row, text="", font=("Helvetica", 9), bg="white",
                              anchor="w", justify="left", wraplength=380)
        status_lbl.pack(side="left", fill="x", expand=True, padx=(0, 4))

        entry = {"url": url, "var": var, "row_frame": row, "status_lbl": status_lbl}
        tk.Button(row, text="✕", font=("Helvetica", 9), fg="#E24B4A", bg="white",
                  relief="flat", cursor="hand2", bd=0,
                  command=lambda: self._remove_proxy_row(entry)).pack(side="right", padx=(0, 4))

        self.proxy_rows.append(entry)

    def _remove_proxy_row(self, entry: dict):
        entry["row_frame"].destroy()
        self.proxy_rows.remove(entry)
        self._save_proxies_now()

    def _on_add_proxy(self):
        raw = self.proxy_entry.get().strip()
        if not raw:
            return
        for t in re.split(r'[\s,;]+', raw):
            if t:
                self._add_proxy_row(t, enabled=True)
        self.proxy_entry.delete(0, "end")
        self._save_proxies_now()

    def _collect_proxy_list(self) -> list:
        return [{"url": e["url"], "enabled": e["var"].get()} for e in self.proxy_rows]

    def _save_proxies_now(self):
        bot.save_proxy_config(self.use_proxy_var.get(), self._collect_proxy_list())

    def _on_check_all_proxies(self):
        rows = list(self.proxy_rows)
        if not rows:
            return
        for e in rows:
            e["status_lbl"].config(text="проверяю...", fg="#888")

        def worker():
            for e in rows:
                ok, info = bot.test_single_proxy(e["url"])
                def update(e=e, ok=ok, info=info):
                    if e not in self.proxy_rows:
                        return
                    e["status_lbl"].config(text=(f"✓ {info}" if ok else f"✗ {info}"),
                                           fg=("#639922" if ok else "#E24B4A"))
                self.root.after(0, update)

        threading.Thread(target=worker, daemon=True).start()

    def _browse_out(self):
        path = filedialog.askdirectory(initialdir=self.output_var.get() or os.getcwd())
        if path:
            self.output_var.set(path)

    def _sheet_api(self):
        try:
            return proof_sheet.ProofSheet(self.sheet_url_var.get(), self._log)
        except proof_sheet.SheetError as e:
            messagebox.showerror("Google Sheets", str(e))
            return None

    def _show_sheet_info(self):
        api = self._sheet_api()
        if not api:
            return

        def work():
            try:
                info = api.sheets_info()
            except proof_sheet.SheetError as e:
                self._log(f"[!] Таблица недоступна: {e}")
                return
            self._log("[i] Состояние листов:")
            for s in info:
                self._log(f"      {s.get('name')}: всего ссылок {s.get('rows')}, "
                          f"свободно {s.get('free')}")

        threading.Thread(target=work, daemon=True).start()

    def _log(self, msg: str):
        def append():
            self.log_box["state"] = "normal"
            self.log_box.insert("end", msg + "\n")
            self.log_box.see("end")
            self.log_box["state"] = "disabled"
        self.root.after(0, append)

    def _set_busy(self, busy: bool):
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.run_btn["state"] = state
        self.single_btn["state"] = state
        self.manual_btn["state"] = state
        self.cookies_btn["state"] = state
        self.copy_profile_btn["state"] = state
        self.stop_btn["state"] = "normal" if busy else "disabled"

    def _on_stop(self):
        self.stop_event.set()
        self.stop_btn["state"] = "disabled"
        self._log("[!] Остановлюсь после текущей ссылки — прерывать прогон на середине "
                  "нельзя, иначе останется незавершённая папка.")

    def _check_output(self) -> str:
        output_root = self.output_var.get().strip()
        if not output_root or not os.path.isdir(output_root):
            messagebox.showerror("Ошибка", "Укажи существующую папку сохранения")
            return ""
        return output_root

    def _start(self, work):
        self.stop_event.clear()
        self._set_busy(True)

        def worker():
            asyncio.set_event_loop(asyncio.new_event_loop())
            try:
                work()
            except Exception as e:
                self._log(f"[!] Прогон прерван ошибкой: {e}")
            finally:
                self.root.after(0, lambda: self._set_busy(False))

        threading.Thread(target=worker, daemon=True).start()

    def _jobs(self) -> list:
        try:
            return parse_sheet_jobs(self.range_var.get(), self.sheet_name_var.get())
        except ValueError:
            return []

    def _reload_jobs_list(self) -> None:
        self.jobs_list.delete(0, "end")
        for sheet, a, b in self._jobs():
            self.jobs_list.insert("end", f"  {sheet}: строки {a - 1}-{b - 1}")
        if not self.jobs_list.size():
            self.jobs_list.insert("end", "  — заданий нет, добавьте диапазон —")

    def _save_jobs(self, jobs: list) -> None:
        self.range_var.set(", ".join(f"{sh}:{a - 1}-{b - 1}" for sh, a, b in jobs))
        self._reload_jobs_list()

    def _add_job(self) -> None:
        sheet = self.job_sheet_var.get()
        try:
            a = int(self.job_from_var.get().strip())
            b = int(self.job_to_var.get().strip())
        except ValueError:
            messagebox.showerror("Ошибка", "Номера строк — целые числа")
            return
        if a < 1 or b < a:
            messagebox.showerror("Ошибка", f"Неверный диапазон {a}-{b}")
            return
        self._save_jobs(self._jobs() + [(sheet, a + 1, b + 1)])

    def _del_job(self) -> None:
        sel = list(self.jobs_list.curselection())
        jobs = self._jobs()
        if not sel or not jobs:
            return
        self._save_jobs([j for i, j in enumerate(jobs) if i not in sel])

    def _clear_jobs(self) -> None:
        self.range_var.set("")
        self._reload_jobs_list()

    def _run_batch(self):
        output_root = self._check_output()
        if not output_root:
            return
        sheet = self.sheet_name_var.get()
        row_range = self.range_var.get().strip()
        user = self.user_var.get().strip()
        if not user:
            messagebox.showerror("Ошибка", "Укажи своё имя — оно вписывается в столбец "
                                           "USER, чтобы эти ссылки не взял кто-то ещё")
            return
        if not self._sheet_api():
            return
        try:
            jobs = parse_sheet_jobs(row_range, sheet)
        except ValueError as e:
            messagebox.showerror("Ошибка", str(e))
            return
        plan_lines = "\n".join(
            f"  • лист «{sh}», строки {a - 1}-{b - 1}" for sh, a, b in jobs)
        plan_lines += ("\n\nРежим на каждую ссылку выбирается по ней самой: "
                       "Vavada и Pinco, а также адреса с коротким хвостом "
                       "домена (1xbet-a42.top) — десктопом, остальные — "
                       "мобильным окном десктопного размера.")
        if not _mobile_supported():
            plan_lines += ("\n\nМобильный режим сейчас недоступен (он работает с Google "
                           "Chrome и Яндекс Браузером, любым способом запуска) — "
                           "всё пойдёт десктопом.")
        if not messagebox.askokcancel(
                "Занять ссылки",
                f"{plan_lines}\n\n"
                f"В столбец USER этих строк будет вписано «{user}» — пока имя там, "
                f"ссылки не достанутся другому.\n\n"
                f"Пруфы лягут в «<дата> Работа / <Бренд> / <номер>».\n\n"
                f"Начинаем?"):
            return

        def on_progress(done, total, counts):
            self.root.after(0, lambda: self.progress_var.set(
                f"Проверено {done} из {total}   ·   успешно {counts[proof_xlsx.OK]}   ·   "
                f"проблем {counts[proof_xlsx.PROBLEM]}   ·   мёртвых {counts[proof_xlsx.DEAD]}"))

        self.progress_var.set("Занимаю строки в таблице...")
        self._log(f"--- Старт: заданий {len(jobs)}, пользователь «{user}» ---")
        for sh, a, b in jobs:
            self._log(f"      лист «{sh}», строки {a - 1}-{b - 1}")
        self._start(lambda: run_batch_sheets(
            self.sheet_url_var.get(), jobs, user, output_root, self._log,
            stop_event=self.stop_event, on_progress=on_progress))

    def _run_manual_profile(self):
        site = self.url_var.get().strip()
        where = (f"Сразу откроется: {site}\n\n" if site
                 else "Откроется стартовая страница; адресная строка обычная — "
                      "переходи куда нужно.\n\n")
        if not messagebox.askokcancel(
                "Настройка профиля вручную",
                "Открою браузер с нашим рабочим профилем и такой же сетью, как "
                "в прогоне: сайты через прокси, Яндекс напрямую.\n\n"
                "Запуск обычный, без Playwright — ни отладчика, ни служебных "
                "ключей. Так заходит человек, и зеркало с жёсткой защитой "
                "пускает именно так.\n\n"
                + where
                + "Прогрей зеркало, которое не пускает без ручного захода, "
                  "согласись сделать Яндекс поиском по умолчанию, войди в аккаунт. "
                  "Всё сохранится в профиль.\n\n"
                  "Закончил — просто закрой окно."):
            return

        def on_step(done, total, results):
            self.root.after(0, lambda: self.progress_var.set(
                "Профиль настроен" if results and results[0]["ok"]
                else "Профиль закрыт"))

        self.progress_var.set("Открываю профиль...")
        self._log("--- Ручная настройка профиля ---")
        self._start(lambda: open_profile_chrome(
            self._log, stop_event=self.stop_event, on_step=on_step,
            start_url=site))

    def _reset_site_data(self):
        site = self.url_var.get().strip()
        if not site:
            messagebox.showerror("Данные сайта",
                                 "Вставь ссылку в поле «Одна ссылка» — "
                                 "данные её сайта и будут стёрты.")
            return
        host = re.sub(r"^\w+://", "", site).split("/")[0].lstrip(".")
        if not messagebox.askokcancel(
                "Стереть данные сайта",
                f"Уберу из профиля ВСЁ, что оставил {host}: куки (включая проверку "
                f"DDoS-Guard), кэш, service worker и хранилища.\n\n"
                f"Именно из-за них зеркало продолжает отвечать 403 даже когда оно "
                f"давно пускает: отлуп оседает в профиле, и браузер отвечает сам "
                f"себе, не спрашивая сеть.\n\n"
                f"Другие сайты не тронутся — Яндекс, вход в аккаунт и настройки "
                f"останутся на месте.\n\n"
                f"На секунду откроется окно браузера: свой профиль Chrome держит "
                f"заблокированным, и почистить его иначе нельзя.\n\nПродолжаем?"):
            return
        self.progress_var.set(f"Чищу {host}...")
        self._log(f"--- Чистка данных {host} ---")
        self._start(lambda: reset_site_data(host, self._log))

    def _copy_user_profile(self):
        choice = _browser_choice()
        if not choice:
            messagebox.showerror("Профиль", "Браузер не найден.")
            return
        running = len(browser_processes(choice["exe"]))
        tail = (f"Сейчас {choice['title']} работает (процессов: {running}) — он "
                f"продолжает жить в фоне и после закрытия окна, поэтому файл кук "
                f"занят. ЗАКРОЮ ЕГО САМ: сначала по-хорошему, как крестиком, "
                f"чтобы он успел дописать свои базы. Открытые вкладки "
                f"восстановятся при следующем запуске.\n\n" if running else "")
        if not messagebox.askokcancel(
                "Взять куки моего браузера",
                f"Скопирую из твоего профиля {choice['title']} три файла — куки, "
                f"настройки и ключ их расшифровки — в профиль программы.\n\n"
                f"Работать прямо в твоём профиле нельзя: открытый браузер держит "
                f"его заблокированным, а Chrome с 136-й версии и вовсе запрещает "
                f"отладочный порт для профиля по умолчанию.\n\n"
                + tail + "Продолжаем?"):
            return
        self.progress_var.set("Копирую профиль...")
        self._log(f"--- Перенос кук из профиля {choice['title']} ---")
        self._start(lambda: copy_user_profile(self._log, close_running=True))

    def _run_single(self):
        url = self.url_var.get().strip()
        output_root = self._check_output()
        if not output_root:
            return
        if not url:
            messagebox.showerror("Ошибка", "Укажи ссылку")
            return
        self.progress_var.set("")
        set_mobile_run_for_url(url)
        why = desktop_url_reason(url)
        self._log(f"--- Старт: {url} ---"
                  + (f"  [десктоп: {why}]" if why
                     else ("  [мобильный]" if _mobile_ua_enabled() else "  [десктоп]")))

        def work():
            res = process_url(url, output_root, self._log)
            self.root.after(0, lambda: self.progress_var.set(
                f"Итог: {proof_xlsx.status_title(res.outcome).upper()}"
                + (f" — {res.reason}" if res.reason else "")))

        self._start(work)


