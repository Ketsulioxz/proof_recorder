import itertools
import json
import os
import re
import threading

BRUTEFORCE_DIR = "bruteforce"

CHARSETS = {
    "letters_digits": "abcdefghijklmnopqrstuvwxyz0123456789",
    "letters": "abcdefghijklmnopqrstuvwxyz",
    "digits": "0123456789",
}

CHARSET_LABELS = {
    "letters_digits": "буквы + цифры (a-z0-9)",
    "letters": "только буквы (a-z)",
    "digits": "только цифры (0-9)",
}

_pool_locks_guard = threading.Lock()
_pool_locks = {}


def _lock_for(path: str) -> threading.Lock:
    with _pool_locks_guard:
        lock = _pool_locks.get(path)
        if lock is None:
            lock = threading.Lock()
            _pool_locks[path] = lock
        return lock


def _sanitize(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "", s) or "x"


def pool_path(prefix: str, suffix: str, length: int, charset_name: str) -> str:
    name = f"{_sanitize(prefix)}_{_sanitize(suffix)}_{length}_{charset_name}.txt"
    return os.path.join(BRUTEFORCE_DIR, name)


def combinations_count(length: int, charset_name: str) -> int:
    return len(CHARSETS[charset_name]) ** length


def count_lines(path: str) -> int:
    if not os.path.exists(path):
        return 0
    with open(path, "r", encoding="utf-8") as f:
        return sum(1 for _ in f)


def generate_pool_if_missing(prefix: str, suffix: str, length: int,
                              charset_name: str, log_fn=print) -> tuple[str, int, bool]:
    os.makedirs(BRUTEFORCE_DIR, exist_ok=True)
    path = pool_path(prefix, suffix, length, charset_name)
    created = False
    if not os.path.exists(path):
        charset = CHARSETS[charset_name]
        total = len(charset) ** length
        log_fn(f"[*] Генерирую пул: {prefix}{'X' * length}{suffix} "
               f"({total} комбинаций) → {path}")
        with open(path, "w", encoding="utf-8") as f:
            for combo in itertools.product(charset, repeat=length):
                f.write(f"{prefix}{''.join(combo)}{suffix}\n")
        log_fn(f"[+] Пул создан: {total} доменов")
        created = True
    remaining = count_lines(path) - current_position(path)
    return path, remaining, created


def _cursor_path(path: str) -> str:
    return path + ".cursor"


def _load_cursor(path: str) -> dict:
    cpath = _cursor_path(path)
    if not os.path.exists(cpath):
        return {"byte_offset": 0, "line_number": 0}
    try:
        with open(cpath, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {"byte_offset": int(data.get("byte_offset", 0)),
                "line_number": int(data.get("line_number", 0))}
    except Exception:
        return {"byte_offset": 0, "line_number": 0}


def _save_cursor(path: str, byte_offset: int, line_number: int):
    cpath = _cursor_path(path)
    tmp_path = cpath + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump({"byte_offset": byte_offset, "line_number": line_number}, f)
    os.replace(tmp_path, cpath)


def current_position(path: str) -> int:
    return _load_cursor(path)["line_number"]


def set_position(path: str, line_number: int) -> int:
    line_number = max(0, line_number)
    lock = _lock_for(path)
    with lock:
        if not os.path.exists(path):
            return 0
        if line_number == 0:
            _save_cursor(path, 0, 0)
            return 0
        with open(path, "r", encoding="utf-8") as f:
            n = 0
            while n < line_number:
                line = f.readline()
                if not line:
                    break
                n += 1
            offset = f.tell()
        _save_cursor(path, offset, n)
        return n


def pop_batch(path: str, batch_size: int) -> list[str]:
    lock = _lock_for(path)
    with lock:
        if not os.path.exists(path):
            return []
        cursor = _load_cursor(path)
        with open(path, "r", encoding="utf-8") as f:
            f.seek(cursor["byte_offset"])
            batch = []
            lines_read = 0
            for _ in range(batch_size):
                line = f.readline()
                if not line:
                    break
                lines_read += 1
                s = line.strip()
                if s:
                    batch.append(s)
            new_offset = f.tell()
        _save_cursor(path, new_offset, cursor["line_number"] + lines_read)
        return batch


def list_pools() -> list[str]:
    if not os.path.isdir(BRUTEFORCE_DIR):
        return []
    return sorted(f for f in os.listdir(BRUTEFORCE_DIR) if f.endswith(".txt"))
