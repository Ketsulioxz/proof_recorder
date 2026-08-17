import concurrent.futures
import socket
import subprocess
import sys

FALLBACK_TIMEOUT = 2.5  # сек. — бюджет ожидания одного резолва через getaddrinfo.

_WORKERS = 100000
_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=_WORKERS, thread_name_prefix="dns")


def resolve_alive(domain: str, timeout: float = None) -> bool:
    future = _executor.submit(socket.getaddrinfo, domain, 443)
    try:
        future.result(timeout=timeout or FALLBACK_TIMEOUT)
        return True
    except Exception:
        return False


def flush_dns_cache(log_fn=print) -> bool:
    if sys.platform != "win32":
        log_fn("[!] Автоочистка DNS-кеша поддержана только на Windows — пропускаю")
        return False
    try:
        result = subprocess.run(
            ["ipconfig", "/flushdns"], capture_output=True, text=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode == 0:
            log_fn("[dns] Локальный DNS-кеш очищен (ipconfig /flushdns)")
            return True
        log_fn(f"[!] ipconfig /flushdns вернул код {result.returncode}: "
               f"{(result.stderr or result.stdout).strip()}")
        return False
    except Exception as e:
        log_fn(f"[!] Не удалось очистить DNS-кеш: {e}")
        return False
