
import json

import requests

from . import proof_xlsx   # статусы OK/PROBLEM/DEAD — одни и те же для таблицы и файла

DEFAULT_APPS_SCRIPT_URL = (
    "https://script.google.com/macros/s/"
    "AKfycbzPzf7iD_UgfKFAm8AY8baKAEIoCrud_MsEA6ZRqoZeALVV2_W81ZRbnRjtJXHpLk3-"
    "/exec"
)

SHEET_YANDEX = "Yandex"
SHEET_JIJA = "Jija"
SHEETS = (SHEET_YANDEX, SHEET_JIJA)

_STATUS_TO_MARK = {proof_xlsx.OK: "ok",
                   proof_xlsx.PROBLEM: "problem",
                   proof_xlsx.DEAD: "dead"}

TIMEOUT = (10, 120)   # соединение, ожидание ответа: Apps Script отвечает не быстро


class SheetError(Exception):
    pass


class SheetRow:

    __slots__ = ("row", "url", "final_url", "sheet")

    def __init__(self, row: int, url: str, final_url: str, sheet: str):
        self.row = row
        self.url = url
        self.final_url = final_url
        self.sheet = sheet

    def __repr__(self):
        return f"SheetRow({self.sheet}!{self.row}, {self.url!r})"


def pick_url(sheet: str, url: str, final_url: str) -> str:
    url = (url or "").strip()
    final_url = (final_url or "").strip()
    if sheet != SHEET_JIJA or not final_url:
        return url
    if url == final_url:
        return url
    if len(url) == len(final_url):
        return final_url
    return url


class ProofSheet:

    def __init__(self, apps_script_url: str = "", log_fn=None):
        self.url = (apps_script_url or "").strip() or DEFAULT_APPS_SCRIPT_URL
        self.log = log_fn or (lambda m: None)
        if not self.url:
            raise SheetError("не задан адрес веб-приложения Apps Script")
        if "/exec" not in self.url:
            raise SheetError("адрес Apps Script должен заканчиваться на /exec — "
                             f"получено: {self.url[:60]}")

    def _get(self, params: dict) -> dict:
        try:
            resp = requests.get(self.url, params=params, timeout=TIMEOUT,
                                allow_redirects=True)
        except requests.exceptions.RequestException as e:
            raise SheetError(f"таблица недоступна: {type(e).__name__}") from e
        return self._parse(resp)

    def _post(self, payload: dict) -> dict:
        try:
            resp = requests.post(self.url, data=json.dumps(payload),
                                 headers={"Content-Type": "text/plain;charset=utf-8"},
                                 timeout=TIMEOUT, allow_redirects=True)
        except requests.exceptions.RequestException as e:
            raise SheetError(f"таблица недоступна: {type(e).__name__}") from e
        return self._parse(resp)

    def _parse(self, resp) -> dict:
        if resp.status_code != 200:
            raise SheetError(f"веб-приложение ответило {resp.status_code}. Проверь, что "
                             f"развёртывание доступно «Всем» (Anyone) и адрес — /exec")
        text = (resp.text or "").strip()
        try:
            data = json.loads(text)
        except ValueError:
            hint = ("похоже на страницу входа Google — развёртывание требует авторизации"
                    if "<html" in text[:200].lower() else text[:160])
            raise SheetError(f"ответ не разобрался как JSON: {hint}")
        if isinstance(data, dict) and data.get("error"):
            raise SheetError(str(data["error"]))
        return data if isinstance(data, dict) else {}

    def sheets_info(self) -> list:
        return (self._get({"action": "proof_sheets"}) or {}).get("sheets") or []

    def claim(self, sheet: str, row_from: int, row_to: int, user: str) -> tuple:
        if not user.strip():
            raise SheetError("не указано имя пользователя — без него нельзя занимать строки")
        if row_from < 2:
            raise SheetError("первая строка — заголовок; диапазон начинается со 2-й")
        if row_to < row_from:
            raise SheetError(f"неверный диапазон: {row_from}-{row_to}")

        data = self._post({"action": "proof_claim", "sheet": sheet,
                           "from": row_from, "to": row_to, "user": user.strip()})
        rows = [SheetRow(int(r["row"]), r.get("url", ""), r.get("final_url", ""), sheet)
                for r in (data.get("rows") or []) if r.get("url")]
        busy = [int(x) for x in (data.get("busy") or [])]
        return rows, busy

    def mark(self, sheet: str, row: int, outcome: str) -> bool:
        status = _STATUS_TO_MARK.get(outcome)
        if not status:
            return False
        try:
            data = self._post({"action": "proof_mark", "sheet": sheet,
                               "marks": [{"row": row, "status": status}]})
        except SheetError as e:
            self.log(f"[!] Не удалось отметить строку {row} в таблице: {e}")
            return False
        return bool(data.get("marked"))

    def release(self, sheet: str, rows: list, user: str) -> int:
        rows = [int(r) for r in rows]
        if not rows:
            return 0
        try:
            data = self._post({"action": "proof_release", "sheet": sheet,
                               "rows": rows, "user": user.strip()})
        except SheetError as e:
            self.log(f"[!] Не удалось освободить строки {rows[:5]}…: {e}")
            return 0
        return int(data.get("released") or 0)


def parse_range(text: str) -> tuple:
    text = (text or "").strip().replace(" ", "")
    if not text:
        raise ValueError("диапазон не указан")
    if "-" in text:
        a, b = text.split("-", 1)
    else:
        a = b = text
    try:
        first, last = int(a), int(b)
    except ValueError:
        raise ValueError(f"не понял диапазон {text!r} — нужно вида 1-30")
    if first < 1 or last < first:
        raise ValueError(f"неверный диапазон {text!r}")
    return first + 1, last + 1
