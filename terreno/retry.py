"""Reintentos de lecturas remotas: que errores son transitorios (se reintentan) y cuales no (401/403/404, archivo local)."""
from __future__ import annotations

import time
from typing import Callable

# Modulos de cliente HTTP / sistema de archivos remoto cuyos errores se tratan como cortes de red (se detectan por el
# nombre del modulo, sin importar aiohttp ni fsspec).
NETWORK_MODULES = ("aiohttp", "fsspec", "requests", "urllib3", "httpx", "botocore", "s3fs", "http.client")
LOCAL_OS_ERRORS = (FileNotFoundError, PermissionError, NotADirectoryError, IsADirectoryError)
_MISSING_MARKERS = ("no such file", "does not exist", "404", "not found", "not recognized as a supported file format")


def exc_chain(exc: BaseException):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def is_transient(exc: Exception) -> bool:
    """Cortes del servidor o de la red que vale la pena reintentar.

    Se recorre la cadena ``__cause__``/``__context__``. Si algun eslabon trae un codigo HTTP (``status``), manda el
    codigo: >= 500, 408 y 429 son transitorios; cualquier otro (401, 403, 404...) NO se reintenta. Sin codigo: timeouts,
    cortes de conexion, ``OSError`` de lectura remota de h5py (el callback de lectura pierde la excepcion original) y
    errores de clientes aiohttp/fsspec/requests/urllib3 (por nombre de modulo, sin imports duros). Un archivo local
    inexistente o sin permiso no es transitorio.
    """
    chain = list(exc_chain(exc))
    for e in chain:
        status = getattr(e, "status", None)
        if isinstance(status, int) and not isinstance(status, bool):
            return status >= 500 or status in (408, 429)
    for e in chain:
        if isinstance(e, LOCAL_OS_ERRORS):
            return False
    for e in chain:
        if isinstance(e, (TimeoutError, ConnectionError, OSError)):
            return True
        mod = type(e).__module__ or ""
        if any(mod == m or mod.startswith(m + ".") for m in NETWORK_MODULES):
            return True
    return False


def looks_missing(exc: Exception) -> bool:
    """El recurso no existe (404, 'No such file'): reintentar no lo hace aparecer."""
    return any(any(m in str(e).lower() for m in _MISSING_MARKERS) for e in exc_chain(exc))


def retry_read(fn: Callable, *, attempts: int = 3, backoff_s: tuple[float, ...] = (3.0, 8.0),
               retryable: Callable[[Exception], bool] | None = None):
    """Llama ``fn()`` hasta ``attempts`` veces ante errores reintentables; el ultimo error se relanza tal cual."""
    retryable = retryable or (lambda e: is_transient(e) and not looks_missing(e))
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            if attempt == attempts or not retryable(exc):
                raise
            time.sleep(backoff_s[min(attempt, len(backoff_s)) - 1])
    raise AssertionError("inalcanzable")  # pragma: no cover
