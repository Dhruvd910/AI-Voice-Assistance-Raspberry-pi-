"""The network under her, and the clock on her log.

A leaf: imports nothing of ours.

WHY THERE IS A DNS CACHE IN HERE
    "It takes too much time to think, and my internet is perfect." It was --
    and the stall was not the internet. Measured on this Pi with curl:

        api.groq.com  dns=0.10s  connect=0.22s  total=0.75s
        api.groq.com  dns=5.01s  connect=5.10s  total=5.53s   <- the same host
        api.groq.com  dns=0.00s  connect=0.08s  total=0.56s

    The network's DNS server (a phone hotspot or home router) now and then
    drops one query, and the system resolver waits out its full five-second
    timeout before asking again. A turn looks the name up three times --
    Whisper at Groq, the reply model at OpenRouter, the voice at Cartesia --
    so one turn could sit silent for five or ten seconds, on a connection that
    was fast the whole time. Timing a real turn showed exactly that: Whisper
    1.2s, then 6.0s and 6.2s; Cartesia's first audio 0.56s, then 5.5s twice.

    So names are resolved once and kept. A known name is answered from memory
    at once, every time, and refreshed in the background once it is older than
    CACHE_TTL_S; if the refresh fails the old address keeps being used, which
    is always better than no answer. Only a name never seen before waits on
    the resolver, and the ones she needs are looked up at startup. This is the
    app fixing it for itself, so it works on whatever network the device is
    carried to, without anyone editing /etc/resolv.conf.
"""

import datetime
import socket
import sys
import threading
import time

CACHE_TTL_S = 300.0                 # refreshed in the background after this
WARM_HOSTS = ("api.groq.com", "openrouter.ai", "api.cartesia.ai")

_original_getaddrinfo = socket.getaddrinfo
_cache = {}                          # key -> (resolved_at, result)
_refreshing = set()
_lock = threading.Lock()


def _refresh(key):
    try:
        result = _original_getaddrinfo(*key)
    except OSError:
        return                       # keep serving the last good answer
    finally:
        with _lock:
            _refreshing.discard(key)
    with _lock:
        _cache[key] = (time.monotonic(), result)


def _cached_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    key = (host, port, family, type, proto, flags)
    with _lock:
        hit = _cache.get(key)
        stale = hit is not None and time.monotonic() - hit[0] > CACHE_TTL_S
        if stale and key not in _refreshing:
            _refreshing.add(key)
            threading.Thread(target=_refresh, args=(key,), daemon=True,
                             name="dns-refresh").start()
    if hit is not None:
        return hit[1]
    result = _original_getaddrinfo(host, port, family, type, proto, flags)
    with _lock:
        _cache[key] = (time.monotonic(), result)
    return result


def install_dns_cache():
    """Answer repeat lookups from memory. Safe to call more than once."""
    if socket.getaddrinfo is not _cached_getaddrinfo:
        socket.getaddrinfo = _cached_getaddrinfo

    def warm():
        # The shapes httpx asks with, so the first real request is a hit.
        for host in WARM_HOSTS:
            for args in ((host, 443, 0, socket.SOCK_STREAM, 0, 0),
                         (host, 443, 0, 0, 0, 0)):
                try:
                    _cached_getaddrinfo(*args)
                except OSError as exc:
                    print(f"[NET] Could not look up {host} yet ({exc}).", flush=True)
                    break
    threading.Thread(target=warm, daemon=True, name="dns-warm").start()


class _Stamped:
    """Prefix every line written to the log with the time, to the tenth of a
    second. Without it the log can say WHAT happened but never how long
    anything took, which is the whole question when she is slow."""

    def __init__(self, stream):
        self._stream = stream
        self._at_line_start = True
        self._lock = threading.Lock()

    def write(self, text):
        with self._lock:
            out = []
            for piece in text.splitlines(keepends=True):
                if self._at_line_start and piece.strip():
                    now = datetime.datetime.now()
                    out.append(now.strftime("%H:%M:%S.") + f"{now.microsecond // 100000} ")
                out.append(piece)
                self._at_line_start = piece.endswith("\n")
            return self._stream.write("".join(out))

    def __getattr__(self, name):
        return getattr(self._stream, name)


def stamp_log_lines():
    """Timestamp stdout and stderr, which the launcher sends to logs/liza.log."""
    if not isinstance(sys.stdout, _Stamped):
        sys.stdout = _Stamped(sys.stdout)
    if not isinstance(sys.stderr, _Stamped):
        sys.stderr = _Stamped(sys.stderr)
