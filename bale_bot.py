#!/usr/bin/env python3
"""
بله قربان — Bale Bot  (v4.0)
Full-featured web assistant for Telegram & Bale messenger.
"""

import errno
import shutil
import hashlib
import io
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import subprocess
import tempfile
import threading
import time
import fcntl
import storage as durable_store
import urllib.parse
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

import pytesseract
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from PIL import Image
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Load environment variables from .env file
load_dotenv()

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════════
# ── Multi-platform config ───────────────────────────────────────────────────
# Both Bale and Telegram run simultaneously in separate polling threads.
# Each thread sets _ctx (thread-local) so api/send_* use the right endpoint.
import threading as _tl_threading

_ctx = _tl_threading.local()  # _ctx.base_url, _ctx.platform, _ctx.kb_*

BALE_TOKEN = os.getenv("BALE_TOKEN", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")

# Local Bot API server (https://github.com/tdlib/telegram-bot-api).
# Set TELEGRAM_API_BASE=http://127.0.0.1:8081 to enable 2000MB uploads.
TELEGRAM_API_BASE = os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")
LOCAL_BOT_API = TELEGRAM_API_BASE != "https://api.telegram.org"


# Define session maker before platform config
def _make_session(proxy: str = "") -> requests.Session:
    """Create a requests.Session with connection pooling and retry adapter."""
    s = requests.Session()
    r = Retry(
        total=3,
        backoff_factor=0.4,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
    )
    # Multipart POST retries are owned by _post_file; retrying here duplicates media.
    s.mount("https://", HTTPAdapter(max_retries=r, pool_connections=24, pool_maxsize=24, pool_block=True))
    s.mount("http://", HTTPAdapter(max_retries=r, pool_connections=24, pool_maxsize=24, pool_block=True))
    if proxy:
        try:
            # Use socks5h so DNS is resolved through the proxy (needed for geo-blocked domains)
            _proxy = (
                proxy.replace("socks5://", "socks5h://")
                if proxy.startswith("socks5://")
                else proxy
            )
            s.proxies = {"http": _proxy, "https": _proxy}
            # Test SOCKS support early to give a clear error
            import socks  # noqa — just check it's importable
        except ImportError:
            log.warning(
                "PySocks not installed — WARP proxy disabled. "
                "Run: pip install PySocks --break-system-packages"
            )
            s.proxies = {}
    return s


def _acquire_instance_lock(path=Path("/tmp/baleghorbanbot.lock")):
    """Return an exclusive process lock, or None when another bot owns it."""
    handle = open(path, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


# Per-platform constants (used by whichever platform is active in the thread)
_PLATFORM_CFG = {
    "bale": {
        "base_url": f"https://tapi.bale.ai/bot{BALE_TOKEN}",
        "kb_rows": 10,
        "kb_cols": 3,
        "chunk_size": 19 * 1024 * 1024,  # Bale: 20MB limit → 19MB chunks
        "session": _make_session(),
    },
    "telegram": {
        "base_url": f"{TELEGRAM_API_BASE}/bot{TELEGRAM_TOKEN}",
        "kb_rows": 8,
        "kb_cols": 3,
        # Local server → ~2000MB; cloud → 49MB.
        "chunk_size": (1990 if LOCAL_BOT_API else 49) * 1024 * 1024,
        "session": _make_session(),
    },
}


def _set_platform_ctx(platform: str, *, chat_id=None, operation=None, callback_operation=False):
    """Start a fresh request/job context, including on reused pool threads."""
    cfg = _PLATFORM_CFG[platform]
    _ctx.chat_id = chat_id
    _ctx.requester = operation.get("requester") if isinstance(operation, dict) else None
    _ctx.operation = operation
    _ctx.callback_operation = callback_operation
    _ctx.platform = platform
    _ctx.base_url = cfg["base_url"]
    _ctx.kb_rows = cfg["kb_rows"]
    _ctx.kb_cols = cfg["kb_cols"]
    _ctx.chunk_size = cfg["chunk_size"]
    _ctx.session = cfg["session"]  # shared persistent Session (thread-safe reads)
    # Also set max video duration for splitting (Telegram allows larger files)
    _ctx.video_split_mb = 48.0 if platform == "telegram" else 18.0
    if platform == "telegram":
        _ctx.video_split_mb = 1990.0 if LOCAL_BOT_API else 48.0
    else:
        _ctx.video_split_mb = 18.0


# Helpers to read thread-local context — fall back to Bale defaults
def _base_url() -> str:
    return getattr(_ctx, "base_url", _PLATFORM_CFG["bale"]["base_url"])


def _platform() -> str:
    return getattr(_ctx, "platform", "bale")


def _pkey(cid: int) -> str:
    """Scope a chat_id by the current thread's platform.

    Bale and Telegram hand out chat IDs from overlapping numeric ranges, so
    the same integer `cid` can be a completely different person on each
    platform. Every per-chat dict must key on (platform, cid) — this is
    called from inside get_state/set_state/bump/etc. so all call sites keep
    passing a plain int cid and get correct isolation for free.
    """
    return f"{_platform()}:{cid}"


def _session() -> requests.Session:
    return getattr(_ctx, "session", _PLATFORM_CFG["bale"]["session"])


def _chunk_size() -> int:
    """Get chunk size based on platform - Telegram allows 50MB, Bale 20MB"""
    platform = getattr(_ctx, "platform", "bale")
    if platform == "telegram":
        return getattr(_ctx, "chunk_size", TELEGRAM_MAX_FILE_SIZE)
    return getattr(_ctx, "chunk_size", BALE_MAX_FILE_SIZE)


# Keyboard limit helpers — use thread-local per-platform values
def _KB_MAX_ROWS():
    """Get keyboard max rows from thread-local context."""
    return getattr(_ctx, "kb_rows", 10)


KB_MAX_ROWS = 10  # default fallback (replaced per-thread via _ctx)
KB_MAX_COLS = 3
KB_MAX_BUTTONS = KB_MAX_ROWS * KB_MAX_COLS

# Legacy TOKEN for any remaining single-platform references
TOKEN = BALE_TOKEN or TELEGRAM_TOKEN

# Platform Limits
# Telegram supports up to 50MB for standard bots, 2GB for Premium bots
# We use 48MB to leave some buffer
TELEGRAM_MAX_FILE_SIZE = 48 * 1024 * 1024  # 48MB for Telegram (can go up to 2GB with Premium)
BALE_MAX_FILE_SIZE = 20 * 1024 * 1024      # 20MB for Bale (hard limit)
MAX_FILE_SIZE = BALE_MAX_FILE_SIZE  # Default to Bale for safety, updated dynamically per-thread

# Prefer the bot's isolated environment over an outdated system executable.
YTDLP_BIN = str(Path(__file__).resolve().parent / ".venv/bin/yt-dlp")
if not Path(YTDLP_BIN).is_file():
    YTDLP_BIN = shutil.which("yt-dlp") or "yt-dlp"
MAX_IMAGE_SIZE = 10 * 1024 * 1024
MAX_OCR_SIZE = 5 * 1024 * 1024
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")

# YouTube: export cookies with:
#   yt-dlp --cookies-from-browser chrome --cookies /path/yt_cookies.txt https://youtube.com
YOUTUBE_COOKIES_FILE = os.getenv("YOUTUBE_COOKIES_FILE", "")

# Telegram MTProto (for reading channels). Get from https://my.telegram.org
TG_API_ID = os.getenv("TG_API_ID", "")
TG_API_HASH = os.getenv("TG_API_HASH", "")
TG_SESSION = os.getenv("TG_SESSION_FILE", "tg_session")  # session file path

# Movie/TV APIs
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "")
OMDB_API_KEY = os.getenv("OMDB_API_KEY", "")
OPENSUBTITLES_API_KEY = os.getenv("OPENSUBTITLES_API_KEY", "")
SUBSOURCE_API_KEY = os.getenv("SUBSOURCE_API_KEY", "")
BAZARR_URL = os.getenv("BAZARR_URL", "")
BAZARR_API_KEY = os.getenv("BAZARR_API_KEY", "")

# Twitter: cookies file for yt-dlp (log into twitter.com in browser first)
TWITTER_COOKIES_FILE = os.getenv("TWITTER_COOKIES_FILE", "")

# Instagram: optional login credentials
INSTAGRAM_USER = os.getenv("INSTAGRAM_USER", "")
INSTAGRAM_PASS = os.getenv("INSTAGRAM_PASS", "")
INSTAGRAM_COOKIES_FILE = os.getenv("INSTAGRAM_COOKIES_FILE", "")

# Spotify: optional client credentials for spotdl (helps with playlists/albums)
# Get from: https://developer.spotify.com/dashboard
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")

# RapidAPI removed — consistently returned 403, replaced by probe-first strategy.

# Cloudflare WARP proxy — set WARP_PROXY=socks5://127.0.0.1:40000 to route
# yt-dlp and HTTP requests through WARP (avoids datacenter IP blocks).
# Install: https://pkg.cloudflareclient.com/ then: warp-cli set-mode proxy && warp-cli connect
WARP_PROXY = os.getenv("WARP_PROXY", "")  # e.g. "socks5://127.0.0.1:40000"

def _warp_selfcheck():
    if not WARP_PROXY:
        log.warning("WARP self-check: WARP_PROXY NOT set — Sci-Hub/Anna's/blocked DNS will FAIL.")
        return
    px = WARP_PROXY.replace("socks5://", "socks5h://")
    try:
        r = requests.get("https://www.cloudflare.com/cdn-cgi/trace/",
                         proxies={"http": px, "https": px}, timeout=10)
        on = ("warp=on" in r.text) or ("warp=plus" in r.text)
        log.info("WARP self-check: proxy=%s  ->  warp=%s", WARP_PROXY,
                 "ON ✅" if on else "OFF ❌ (proxy up but traffic not on WARP)")
    except Exception as e:
        log.error("WARP self-check: proxy %s UNREACHABLE — %s", WARP_PROXY, e)

# Cobalt API — self-hosted instance for reliable social/YouTube downloads.
# Self-host: https://github.com/imputnet/cobalt
# Default assumes local instance on port 9000.
# Set COBALT_URL= (empty) to disable, or point to a remote instance.
COBALT_URL = os.getenv("COBALT_URL", "http://localhost:9000")

# VirusTotal — free tier: 4 req/min, 500 req/day, 32MB max
# Get your key at https://www.virustotal.com/gui/join-us
VIRUSTOTAL_API_KEY = os.getenv("VIRUSTOTAL_API_KEY", "")

# Unpaywall — free API for open-access papers (requires email registration)
# Register at: https://unpaywall.org/products/api
UNPAYWALL_EMAIL = os.getenv("UNPAYWALL_EMAIL", "dev@balebot.dev")

# Library Genesis — no login required, public API
# Only reliable mirrors that work from Hetzner/Iran hosting
LIBGEN_MIRRORS = [
    "https://libgen.li",
    "https://libgen.gs",
    "https://libgen.vg",
    "https://libgen.is",
    "https://libgen.st",
    "https://libgen.rs",
    "https://libgen.la",
    "https://libgen.bz",
    "https://libgen.gl",
]
LIBGEN_DL_MIRRORS = [
    "https://libgen.li",
    "https://libgen.gs",
    "https://libgen.vg",
    "https://libgen.is",
    "https://libgen.st",
    "https://libgen.rs",
    "https://libgen.la",
    "https://libgen.bz",
    "https://libgen.gl",
]
# Anna's Archive - comprehensive open access directory (mirrors libgen + more)
# NOTE (2026-08): .org/.se removed — SOCKS-blocked from this VPS with long
# retries; .li currently parked. Kept: fast-fail mirrors for when they return.
ANNAS_ARCHIVE_MIRRORS = [
    "https://annas-archive.li",
    "https://annas-archive.gl",
    "https://annas-archive.pk",
    "https://annas-archive.gd",
]
# Sci-Hub for scientific papers - only reliable mirrors
SCIHUB_MIRRORS = [
    "https://sci-hub.su",
    "https://sci-hub.se",
    "https://sci-hub.st",
    "https://sci-hub.ru",
    "https://sci-hub.wf",
    "https://sci-hub.ee",
    "https://sci-hub.red",
    "https://sci-hub.box",
    "https://sci-net.xyz",

]


# ═══════════════════════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)-8s] %(funcName)s:%(lineno)d — %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bale_bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)

# ═══════════════════════════════════════════════════════════════════════════════
# SHARED HTTP SESSION
# ═══════════════════════════════════════════════════════════════════════════════

WEB = _make_session()


def _get_web(use_warp: bool = False) -> requests.Session:
    """Return WEB session, optionally routing through WARP proxy."""
    if use_warp and WARP_PROXY:
        if not hasattr(_get_web, "_warp_session"):
            _get_web._warp_session = _make_session(WARP_PROXY)
        return _get_web._warp_session
    return WEB


# ═══════════════════════════════════════════════════════════════════════════════
# STATE  (thread-safe — all access via lock)
# ═══════════════════════════════════════════════════════════════════════════════
import threading as _threading
from concurrent.futures import Future, ThreadPoolExecutor

_state_lock = _threading.Lock()
_cache_lock = _threading.Lock()
_stats_lock = _threading.Lock()
_url_lock = _threading.Lock()

# Caps concurrent CPU-heavy subprocess work (yt-dlp downloads, ffmpeg
# transcoding/splitting). The thread pool below allows 40 concurrent request
# handlers — fine for searches/API calls — but 40 simultaneous yt-dlp+ffmpeg
# processes would thrash a small VPS's CPU and make every download crawl.
# Admit at most one waiting job per worker; never block update workers.
_HEAVY_WORKERS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2"))
HEAVY_DOWNLOAD_SEMAPHORE = _threading.Semaphore(_HEAVY_WORKERS)
_heavy_admission = _threading.BoundedSemaphore(_HEAVY_WORKERS * 2)
_heavy_executor = ThreadPoolExecutor(max_workers=_HEAVY_WORKERS, thread_name_prefix="heavy")
_browser_admission = _threading.BoundedSemaphore(1)
_BUSY_TEXT = "⏳ سرویس در حال حاضر شلوغ است؛ لطفاً کمی بعد دوباره تلاش کنید."


class _BrowserBusy(RuntimeError):
    pass


def _browser_job(fn):
    """One browser chain at a time, including subprocess fallback renderers."""
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not _browser_admission.acquire(blocking=False):
            raise _BrowserBusy(_BUSY_TEXT)
        try:
            return fn(*args, **kwargs)
        finally:
            _browser_admission.release()

    return wrapper


DISK_RESERVE_BYTES = 256 * 1024 * 1024


def _ensure_disk_space(working_bytes=0):
    """Keep emergency headroom before temporary downloads/conversions."""
    free = shutil.disk_usage(tempfile.gettempdir()).free
    required = DISK_RESERVE_BYTES + max(0, working_bytes)
    if free < required:
        log.warning("disk preflight: free=%d required=%d", free, required)
        raise OSError(errno.ENOSPC, "فضای کافی برای پردازش فایل موجود نیست؛ بعداً دوباره تلاش کنید.")


def _heavy_download(fn):
    """Run downloads off update workers with bounded, nonblocking admission."""
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        platform = _platform()
        cid = args[0] if args and isinstance(args[0], int) else kwargs.get("cid")
        operation = getattr(_ctx, "operation", None)
        callback_operation = getattr(_ctx, "callback_operation", False)
        requester = getattr(_ctx, "requester", None)

        def run():
            _set_platform_ctx(platform, chat_id=cid, operation=operation,
                              callback_operation=callback_operation)
            _ctx.requester = requester
            with HEAVY_DOWNLOAD_SEMAPHORE:
                try:
                    _ensure_disk_space()
                    return fn(*args, **kwargs)
                except Exception as e:
                    # The future is discarded — an exception here would fail
                    # silently and leave the status bubble spinning forever.
                    log.error("heavy %s: %s", fn.__name__, e, exc_info=True)
                    if cid:
                        try:
                            _status(cid, "❌ " + friendly_error(e),
                                    reply_markup=home_kb(), final=True)
                        except Exception:
                            pass
                    return None

        if not _heavy_admission.acquire(blocking=False):
            if cid:
                _status(cid, _BUSY_TEXT, reply_markup=home_kb(), final=True)
            future = Future()
            future.set_result(None)
            return future
        try:
            future = _heavy_executor.submit(run)
        except BaseException:
            _heavy_admission.release()
            raise
        # Also releases cancelled queued futures; never release twice in run().
        future.add_done_callback(lambda _: _heavy_admission.release())
        return future

    return wrapper

user_state: dict[int, dict] = {}
user_stats: dict[int, dict] = {}
# Cache pending search results so callback buttons can refer to them
result_cache: dict[str, list] = {}  # key → list[dict]
_youtube_info_cache: dict[str, tuple[float, dict]] = {}

# ═══════════════════════════════════════════════════════════════════════════════
# BALE API HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

# Extensions Bale accepts natively — no ZIP wrapping needed (mirrors index.js)
EXEMPT_EXT = {
    "jpg",
    "jpeg",
    "png",
    "gif",
    "webp",
    "bmp",
    "tiff",
    "tif",
    "svg",
    "ico",
    "heic",
    "heif",
    "avif",
    "mp4",
    "mkv",
    "avi",
    "mov",
    "wmv",
    "flv",
    "webm",
    "m4v",
    "3gp",
    "ts",
    "mts",
    "mp3",
    "ogg",
    "wav",
    "flac",
    "m4a",
    "aac",
    "wma",
    "opus",
    "aiff",
    "zip",
    "rar",
    "7z",
    "tar",
    "gz",
    "bz2",
    "xz",
    "zst",
    "lz4",
    "pdf",
    "doc",
    "docx",
    "xls",
    "xlsx",
    "ppt",
    "pptx",
    "odt",
    "ods",
    "odp",
    "epub",
}

MIME_MAP = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
    "mp4": "video/mp4",
    "mkv": "video/x-matroska",
    "mov": "video/quicktime",
    "avi": "video/x-msvideo",
    "webm": "video/webm",
    "mp3": "audio/mpeg",
    "ogg": "audio/ogg",
    "wav": "audio/wav",
    "flac": "audio/flac",
    "m4a": "audio/mp4",
    "aac": "audio/aac",
    "opus": "audio/opus",
    "pdf": "application/pdf",
    "zip": "application/zip",
    "7z": "application/x-7z-compressed",
    "txt": "text/plain",
    "html": "text/html",
    "json": "application/json",
}


def _real_ext(filename: str) -> str:
    """Return the meaningful extension, stripping a trailing .part suffix.

    yt-dlp appends .part while downloading (and with --max-filesize), so:
      video.f398.mp4.part  →  'mp4'
      video.mp4            →  'mp4'
      archive.zip          →  'zip'
    """
    name = filename.lower()
    if name.endswith(".part"):
        name = name[:-5]
    return name.rsplit(".", 1)[-1] if "." in name else ""


def _mime(filename: str) -> str:
    return MIME_MAP.get(_real_ext(filename), "application/octet-stream")


def _should_wrap(filename: str) -> bool:
    """ZIP-wrapping is disabled — files ship raw on both platforms.

    Never re-enable this for Telegram: users unwrap on their own device.
    Bale accepts every raw extension that bounded_media() feeds through.
    """
    return False


def _wrap_zip(data: bytes, filename: str) -> tuple[bytes, str]:
    """Wrap data in a ZIP and return (zip_bytes, new_filename)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(filename, data)
    buf.seek(0)
    base = filename.rsplit(".", 1)[0] if "." in filename else filename
    return buf.read(), f"{base}.zip"


def _safe_json(r: requests.Response) -> dict:
    """Parse JSON safely — log raw body on failure."""
    try:
        return r.json()
    except Exception:
        log.error("Non-JSON response (status=%d): %r", r.status_code, r.text[:200])
        return {"ok": False, "_raw": r.text[:200]}


def api(method: str, _retries: int = 4, **kwargs) -> dict:
    """Call Bot API (Bale or Telegram) with exponential backoff retry."""
    delay = 2
    last_err = None
    for attempt in range(_retries):
        try:
            r = _session().post(f"{_base_url()}/{method}", json=kwargs, timeout=30)
            result = _safe_json(r)
            if result.get("ok"):
                return result
            # Bale returns ok=False with error_code on server errors
            code = result.get("error_code", 0)
            if code == 429:
                # Only trust finite positive numeric delays, capped at 60s.
                parameters = result.get("parameters")
                retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
                wait = delay
                if isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool) and 0 < retry_after <= 60:
                    wait = retry_after
                # Rate-limited — back off more aggressively
                log.warning(
                    "api %s: rate-limited (429) — retry %d/%d in %ds",
                    method,
                    attempt + 1,
                    _retries,
                    wait,
                )
                if attempt + 1 < _retries:
                    time.sleep(wait)
                delay = min(delay * 3, 60)  # More aggressive backoff for rate limits
                last_err = result
                continue
            elif code in (500, 502, 503, 504) or r.status_code >= 500:
                log.warning(
                    "api %s: server error %s — retry %d/%d in %ds",
                    method,
                    result,
                    attempt + 1,
                    _retries,
                    delay,
                )
                if attempt + 1 < _retries:
                    time.sleep(delay)
                delay = min(delay * 2, 30)
                last_err = result
                continue
            # Permanent client error — don't retry
            log.error("api %s: %s", method, result)
            return result
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError,
        ) as e:
            log.warning(
                "api %s: connection error %s — retry %d/%d in %ds",
                method,
                e,
                attempt + 1,
                _retries,
                delay,
            )
            if attempt + 1 < _retries:
                time.sleep(delay)
            delay = min(delay * 2, 30)
            last_err = {"ok": False, "_err": str(e)}
        except Exception as e:
            log.error("api %s: %s", method, e)
            return {"ok": False}
    log.error("api %s: all %d retries failed. last=%s", method, _retries, last_err)
    return {"ok": False, "_retries_exhausted": True}


def _notify_bale_error(chat_id: int):
    """Send a user-facing message when the server is unresponsive."""
    try:
        _session().post(
            f"{_base_url()}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": "⚠️ سرور در حال حاضر پاسخ نمی‌دهد. لطفاً چند دقیقه دیگر دوباره امتحان کنید.",
            },
            timeout=15,
        )
    except Exception:
        pass


def send_message(
    chat_id, text, reply_markup=None, reply_to_message_id=None, parse_mode=None
) -> dict:
    if getattr(_ctx, "callback_operation", False) and _operation_get(chat_id):
        _ctx.callback_operation = False
        return _operation_finish(chat_id, text, reply_markup, parse_mode)
    kw = dict(chat_id=chat_id, text=str(text)[:4096])
    if reply_markup:
        kw["reply_markup"] = (
            json.dumps(reply_markup)
            if not isinstance(reply_markup, str)
            else reply_markup
        )
    if reply_to_message_id:
        kw["reply_to_message_id"] = reply_to_message_id
    if parse_mode:
        kw["parse_mode"] = parse_mode
    return api("sendMessage", **kw)


# -- Single self-editing status bubble per chat (clean progress UX) --
# One operation bubble per chat. Text edits this bubble; media deletes it first.
_STATUS = {}
_STATUS_LOCK = threading.Lock()
_STATUS_SPIN = "◐◓◑◒"
_OPERATION_TICK_SECONDS = 1.0
_OPERATION_ACTION_SECONDS = 4.0
_OPERATION_EDIT_SECONDS = 1.0
_OPERATION_MAX_SECONDS = 600  # hard cap: a bubble may never outlive 10 minutes


def _mk_rm(rm):
    return json.dumps(rm) if (rm and not isinstance(rm, str)) else rm


def _raw_send(cid, text, reply_markup=None, reply_to_message_id=None, parse_mode=None):
    kw = dict(chat_id=cid, text=str(text)[:4096])
    rm = _mk_rm(reply_markup)
    if rm:
        kw["reply_markup"] = rm
    if reply_to_message_id:
        kw["reply_to_message_id"] = reply_to_message_id
    if parse_mode:
        kw["parse_mode"] = parse_mode
    return api("sendMessage", **kw)


def edit_message(cid, message_id, text, reply_markup=None, parse_mode=None):
    if not message_id:
        return _raw_send(cid, text, reply_markup=reply_markup, parse_mode=parse_mode)
    kw = dict(chat_id=cid, message_id=message_id, text=str(text)[:4096])
    rm = _mk_rm(reply_markup)
    if rm:
        kw["reply_markup"] = rm
    if parse_mode:
        kw["parse_mode"] = parse_mode
    return api("editMessageText", **kw)


def _operation_get(cid):
    with _STATUS_LOCK:
        return _STATUS.get(_pkey(cid))


def _operation_body(st):
    if st["final"] or not st["spinner"]:
        return str(st["text"])
    st["tick"] += 1
    return f"{_STATUS_SPIN[st['tick'] % len(_STATUS_SPIN)]} {st['text']}\n\n⏱ {int(time.monotonic() - st['start'])} ثانیه"


def _operation_render(cid, st):
    # Per-operation lock: Bot API I/O must never hold the global chat registry lock.
    with st["lock"]:
        body = _operation_body(st)
        if body == st.get("body"):
            return {"ok": True}
        if st.get("mid"):
            result = edit_message(cid, st["mid"], body, st.get("reply_markup"), st.get("parse_mode"))
            # A transient/rate-limit edit failure must not stack a new status bubble.
            if not result or not result.get("ok"):
                return result or {"ok": False}
        else:
            result = _raw_send(cid, body, st.get("reply_markup"), parse_mode=st.get("parse_mode"))
            st["mid"] = ((result or {}).get("result") or {}).get("message_id")
        if result and result.get("ok"):
            st["body"] = body
        return result


def _operation_current(cid, st):
    with _STATUS_LOCK:
        return _STATUS.get(_pkey(cid)) is st


def _operation_tick(cid, key, st):
    _set_platform_ctx(st["platform"], chat_id=cid, operation=st)
    while not st["stop"].wait(_OPERATION_TICK_SECONDS):
        if not _operation_current(cid, st) or st["final"]:
            return
        now = time.monotonic()
        if now - st["start"] >= _OPERATION_MAX_SECONDS:
            # Safety net: a stuck operation must never spin/typing forever.
            log.warning("operation bubble cid=%s exceeded %ds — auto-finalizing", cid, _OPERATION_MAX_SECONDS)
            _operation_update(cid, "⌛ عملیات بیش از حد طول کشید. لطفاً دوباره تلاش کنید.",
                              reply_markup=home_kb(), final=True)
            return
        if now - st["action_at"] >= _OPERATION_ACTION_SECONDS:
            chat_action(cid, st["action"])
            st["action_at"] = now
        if now - st["edit_at"] >= _OPERATION_EDIT_SECONDS:
            _operation_render(cid, st)
            st["edit_at"] = now


def _operation_start(cid, text, message_id=None, reply_markup=None, parse_mode=None, spinner=True):
    key = _pkey(cid)
    now = time.monotonic()
    st = {"mid": message_id, "start": now, "tick": 0, "text": text,
          "reply_markup": reply_markup, "parse_mode": parse_mode, "spinner": spinner,
          "platform": _platform(), "requester": getattr(_ctx, "requester", None), "action": "typing", "action_at": now,
          "edit_at": now, "body": None, "final": False,
          "stop": threading.Event(), "lock": threading.Lock()}
    with _STATUS_LOCK:
        old = _STATUS.get(key)
        _STATUS[key] = st
    if old:
        old["stop"].set()
    _ctx.operation = st
    chat_action(cid, st["action"])
    _operation_render(cid, st)
    threading.Thread(target=_operation_tick, args=(cid, key, st), daemon=True).start()
    return st


def _operation_update(cid, text, reply_markup=None, parse_mode=None, final=False, spinner=True):
    owner = getattr(_ctx, "operation", None)
    if owner is not None and not _operation_current(cid, owner):
        return {"ok": False}
    st = owner or _operation_get(cid)
    if not st:
        st = _operation_start(cid, text, reply_markup=reply_markup, parse_mode=parse_mode, spinner=spinner)
        return _operation_finish(cid, text, reply_markup, parse_mode) if final else {"ok": True}
    if not _operation_current(cid, st):
        return {"ok": False}
    with st["lock"]:
        st["text"], st["reply_markup"], st["parse_mode"] = text, reply_markup, parse_mode
        st["spinner"], st["final"] = spinner, final
    result = _operation_render(cid, st)
    if final:
        with _STATUS_LOCK:
            if _STATUS.get(_pkey(cid)) is st:
                _STATUS.pop(_pkey(cid), None)
        st["stop"].set()
        if getattr(_ctx, "operation", None) is st:
            _ctx.operation = None
            _ctx.callback_operation = False
    return result


def _operation_finish(cid, text, reply_markup=None, parse_mode=None):
    return _operation_update(cid, text, reply_markup, parse_mode, final=True, spinner=False)


def _operation_prepare_media(cid, owner=None):
    owner = owner or getattr(_ctx, "operation", None)
    with _STATUS_LOCK:
        st = _STATUS.get(_pkey(cid))
        if owner and st is not owner:
            return False
        if st:
            _STATUS.pop(_pkey(cid), None)
    if not st:
        return False
    st["stop"].set()
    if st.get("mid"):
        api("deleteMessage", chat_id=cid, message_id=st["mid"])
    if getattr(_ctx, "operation", None) is st:
        _ctx.operation = None
        _ctx.callback_operation = False
    return True


# ── Friendly error helpers ────────────────────────────────────────────────
# Users must never see raw exceptions or admin config keys. All failure
# messages go through these translators (2026-09-20 sweep).

_FRIENDLY_PATTERNS: list[tuple[str, str]] = [
    # (substring of raw error, user-facing Persian guidance)
    ("executable doesn't exist", "سرویس دانلود موقتاً آماده نیست — کمی بعد دوباره تلاش کنید."),
    ("browser failed", "سرویس دانلود موقتاً آماده نیست — کمی بعد دوباره تلاش کنید."),
    ("empty media response", "اینستاگرام اجازهٔ دسترسی مهمان نمی‌دهد (پست خصوصی یا محدودشده است)."),
    ("sign in to confirm", "سرویس تأیید ورود خواسته — این ویدیو از سمت سرویسدهنده محدود شده است."),
    ("login required", "این محتوا نیاز به حساب کاربری دارد و قابل دانلود نیست."),
    ("private", "این پست خصوصی است و فقط برای افراد تأییدشده دیده می‌شود."),
    ("not found", "چنین محتوایی پیدا نشد — لینک را بررسی کنید."),
    ("404", "چنین محتوایی پیدا نشد — لینک را بررسی کنید."),
    ("429", "سرویس موقتاً محدود شده — چند دقیقه بعد تلاش کنید."),
    ("cloudflare", "سرویس مبدا دسترسی ربات را محدود کرده — کمی بعد تلاش کنید."),
    ("403", "سرویس مبدا دسترسی به این محتوا را مسدود کرده است."),
    ("timeout", "پاسخی از سرویس نرسید (تأخیر شبکه) — دوباره تلاش کنید."),
    ("connection", "مشکل شبکه در ارتباط با سرویس — دوباره تلاش کنید."),
    ("age", "این محتوا محدودیت سنی دارد و بدون حساب تأییدشده قابل دانلود نیست."),
    ("unavailable", "این محتوا دیگر در دسترس نیست (حذف یا محدود شده)."),
    ("quota", "سهمیهٔ سرویس تمام شده — فردا دوباره امتحان کنید."),
]


def friendly_error(raw) -> str:
    """Map a raw exception/HTTP error string to a short Persian user guide."""
    if isinstance(raw, OSError) and raw.errno == errno.ENOSPC:
        return "فضای کافی برای پردازش فایل موجود نیست؛ بعداً دوباره تلاش کنید."
    if isinstance(raw, _BrowserBusy):
        return _BUSY_TEXT
    text = str(raw or "").lower()
    for needle, msg in _FRIENDLY_PATTERNS:
        if needle in text:
            return msg
    return "خطای غیرمنتظره — لطفاً دوباره تلاش کنید. اگر تکرار شد، لینک دیگری را امتحان کنید."


_APK_ATTEMPT_MAP: list[tuple[str, str]] = [
    ("browser failed", "مرورگر دانلود در دسترس نبود"),
    ("executable", "مرورگر دانلود در دسترس نبود"),
    ("http 404", "در این منبع یافت نشد"),
    ("http 403", "منبع دسترسی را مسدود کرد"),
    ("http 429", "منبع موقتاً محدود شده"),
    ("http 5", "سرور منبع موقتاً از کار افتاد"),
    ("exceeds", "حجم فایل بیش از حد مجاز بود"),
    ("too small", "فایل ناقص دریافت شد"),
    ("not a zip", "فایل نامعتبر بود"),
    ("no dl link", "لینک دانلود ارائه نشد"),
    ("no packages", "نسخه‌ای ثبت نشده"),
    ("no file.path", "فایلی یافت نشد"),
    ("expected for non-foss", "اپ متن‌باز نیست (طبیعی)"),
    ("timeout", "مهلت ارتباط تمام شد"),
    ("connection", "ارتباط برقرار نشد"),
    ("cloudflare", "منبع دسترسی را محدود کرد"),
]


def translate_apk_attempts(attempts: list) -> str:
    """Turn raw source-attempt log strings into short Persian lines."""
    lines = []
    for a in (attempts or [])[:8]:
        if ": OK" in a:
            continue
        low = a.lower()
        summary = None
        for needle, msg in _APK_ATTEMPT_MAP:
            if needle in low:
                summary = msg
                break
        src = a.split(":")[0].strip() if ":" in a else a.strip()
        lines.append(f"• {src}: {summary or 'منبع پاسخ معتبر نداد'}")
    seen, out = set(), []
    for line in lines:
        if line not in seen:
            seen.add(line)
            out.append(line)
    return "\n".join(out[:5]) or "• هیچ منبعی پاسخ نداد"


def _status(cid, text, reply_markup=None, reply_to_message_id=None,
            parse_mode=None, final=False, spinner=True):
    # Media has already been delivered; don't add a redundant “sent” bubble.
    if final and str(text).startswith("✅") and not _operation_get(cid):
        return {"ok": True}
    return (_operation_finish(cid, text, reply_markup, parse_mode) if final
            else _operation_update(cid, text, reply_markup, parse_mode, spinner=spinner))


def _status_clear(cid, delete=True):
    if delete:
        _operation_prepare_media(cid)


class Progress:
    """Paper workflow adapter backed by the shared 1Hz operation bubble."""
    def __init__(self, cid, title="⏳ در حال پردازش…", total_steps=0):
        self.cid, self.title, self.total, self.done, self.lines = cid, title, total_steps, 0, []
        _operation_start(cid, title)

    def _eta(self):
        st = _operation_get(self.cid)
        if self.total and self.done and st:
            remain = int((time.monotonic() - st["start"]) / self.done * max(0, self.total - self.done))
            return "  •  ~" + str(remain) + "s باقی‌مانده"
        return ""

    def _render(self, final=False):
        prog = f" ({self.done}/{self.total})" if self.total else ""
        body = self.title + prog + "\n\n" + "\n".join(self.lines[-10:]) + self._eta()
        return _operation_finish(self.cid, body) if final else _operation_update(self.cid, body)

    def step(self, text, force=False):
        if self.lines and self.lines[-1].startswith("⏳"):
            self.lines[-1] = "▫️" + self.lines[-1][1:]
        if text:
            self.done += 1
            self.lines.append("⏳ " + text)
        self._render()

    def finish(self, text, ok=True):
        if self.lines and self.lines[-1].startswith("⏳"):
            self.lines[-1] = ("✅" if ok else "▫️") + self.lines[-1][1:]
        self.lines.append(("✅ " if ok else "❌ ") + text)
        self._render(final=True)


def _gbooks_cover(title, author=""):
    """Reliable book cover via Google Books (no key needed)."""
    try:
        q = (title or "").strip()
        if author:
            q += " " + author
        if not q:
            return ""
        r = WEB.get("https://www.googleapis.com/books/v1/volumes",
                    params={"q": q, "maxResults": 1, "country": "US"}, timeout=12)
        if r.status_code == 200:
            items = r.json().get("items") or []
            if items:
                links = items[0].get("volumeInfo", {}).get("imageLinks", {})
                u = links.get("thumbnail") or links.get("smallThumbnail") or ""
                return u.replace("http://", "https://").replace("&edge=curl", "")
    except Exception as e:
        log.debug("_gbooks_cover: %s", e)
    return ""


def _libgen_cover_url(md5, sess=None):
    """Extract the cover image URL from a libgen.li book (ads.php) page."""
    if not md5:
        return ""
    sess = sess or _libgen_session()
    for mirror in LIBGEN_MIRRORS:
        try:
            r = sess.get(mirror + "/ads.php?md5=" + md5,
                         headers={"User-Agent": UA_DESK}, timeout=(6, 15))
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for img in soup.find_all("img"):
                src = img.get("src") or img.get("data-src") or ""
                if "/covers/" in src.lower() or "cover" in src.lower():
                    if src.startswith("http"):
                        return src
                    if src.startswith("//"):
                        return "https:" + src
                    return mirror + "/" + src.lstrip("/")
        except Exception as e:
            log.debug("_libgen_cover_url %s: %s", mirror, e)
    return ""


def _annas_scidb(doi, sess=None):
    """Fetch a paper via Anna's Archive SciDB (a Sci-Hub mirror).
    Needs WARP to reach annas-archive.* . Returns (pdf_bytes, source) or None.
    """
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", (doi or "")).strip().rstrip(".,)")
    if not doi:
        return None
    sess = sess or _libgen_session()
    hdrs = {"User-Agent": UA_DESK}
    for mirror in ANNAS_ARCHIVE_MIRRORS:
        try:
            page = mirror + "/scidb/" + doi
            r = sess.get(page, headers=hdrs, timeout=(6, 25), allow_redirects=True)
            if r.status_code != 200:
                continue
            if r.content[:4] == b"%PDF":
                return r.content, "Anna's SciDB"
            pdf_url = ""
            for u in re.findall(r'(?:src|href)="([^"]+)"', r.text):
                lu = u.lower()
                if ".pdf" in lu or "scimag" in lu or ("/scidb/" in lu and "download" in lu):
                    pdf_url = u
                    break
            if not pdf_url:
                m = re.search(r'"(https?://[^"]+\.pdf[^"]*)"', r.text)
                if m:
                    pdf_url = m.group(1)
            if not pdf_url:
                continue
            if pdf_url.startswith("//"):
                pdf_url = "https:" + pdf_url
            elif pdf_url.startswith("/"):
                pdf_url = mirror + pdf_url
            pr = sess.get(pdf_url, headers={**hdrs, "Referer": page},
                          timeout=(6, 90), allow_redirects=True)
            if pr.status_code == 200 and len(pr.content) > 5000 and (
                pr.content[:4] == b"%PDF"
                or "pdf" in pr.headers.get("Content-Type", "").lower()):
                return pr.content, "Anna's SciDB"
        except Exception as e:
            log.debug("_annas_scidb %s: %s", mirror, e)
    return None


def _post_file(
    endpoint: str,
    field: str,
    filename: str,
    data_bytes: bytes,
    extra_data: dict,
    _retries: int = 3,
) -> bool:
    """Low-level multipart file POST to Bale API with retry."""
    delay = 3
    for attempt in range(_retries):
        file_obj = io.BytesIO(data_bytes)
        files = {field: (filename, file_obj, _mime(filename))}
        try:
            r = _session().post(
                f"{_base_url()}/{endpoint}", data=extra_data, files=files, timeout=180
            )
            resp = _safe_json(r)
            ok = resp.get("ok", False)
            if ok:
                _conversion_capture(extra_data["chat_id"], resp.get("result"), filename, len(data_bytes))
                file_obj.close()
                return True
            code = resp.get("error_code", 0)
            # Error 404 "no such group or user" means chat_id is invalid — don't retry
            if code == 404:
                log.error(
                    "%s failed: %s  file=%s  size=%dKB — chat/user not found (404)",
                    endpoint,
                    resp,
                    filename,
                    len(data_bytes) // 1024,
                )
                file_obj.close()
                return False
            if code in (429, 500, 502, 503, 504) or r.status_code >= 500:
                log.warning(
                    "%s: server error %s — retry %d/%d in %ds",
                    endpoint,
                    resp,
                    attempt + 1,
                    _retries,
                    delay,
                )
                file_obj.close()
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            log.error(
                "%s failed: %s  file=%s  size=%dKB",
                endpoint,
                resp,
                filename,
                len(data_bytes) // 1024,
            )
            file_obj.close()
            return False
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError,
        ) as e:
            log.warning(
                "%s: connection error %s — retry %d/%d in %ds",
                endpoint,
                e,
                attempt + 1,
                _retries,
                delay,
            )
            file_obj.close()
            time.sleep(delay)
            delay = min(delay * 2, 30)
        except Exception as e:
            log.error("%s exception: %s  file=%s", endpoint, e, filename)
            file_obj.close()
            return False
    log.error("%s: all %d retries failed for %s", endpoint, _retries, filename)
    return False


def _has_audio_stream(data_bytes: bytes, filename: str = "clip.mp4") -> bool:
    """Return True if the given video file contains at least one audio stream.

    Used to catch cases (mostly longer TikTok clips) where yt-dlp's format
    selection picks a video-only stream that "succeeds" but produces a
    silent file.
    """
    import shutil as _sh

    ffprobe = _sh.which("ffprobe") or "/usr/bin/ffprobe"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / _media_filename(filename)
            src.write_bytes(data_bytes)
            probe = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-select_streams",
                    "a",
                    "-show_entries",
                    "stream=codec_type",
                    "-of",
                    "csv=p=0",
                    str(src),
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            return bool(probe.stdout.strip())
    except Exception as e:
        log.debug("_has_audio_stream: %s", e)
        # If we can't tell, don't block the send — assume it's fine.
        return True


def _media_filename(filename: str) -> str:
    """Keep untrusted media names local on either path convention."""
    name = Path(str(filename).replace("\\", "/").replace("\x00", "")).name
    return name if name not in {"", ".", ".."} else "file"


def _video_meta(data_bytes: bytes, filename: str, thumbnail=True) -> dict:
    """
    Extract video duration (seconds) and generate a thumbnail frame via ffmpeg.
    Returns dict with optional keys: duration (int), thumb_bytes (bytes).
    Fast — runs in <1s for normal clips.
    """
    import shutil as _sh

    ffmpeg = _sh.which("ffmpeg") or "/usr/bin/ffmpeg"
    ffprobe = _sh.which("ffprobe") or "/usr/bin/ffprobe"
    result = {}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / _media_filename(filename)
            src.write_bytes(data_bytes)

            # Duration
            probe = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration:stream=width,height",
                    "-of",
                    "json",
                    str(src),
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if probe.returncode == 0:
                import json as _json

                pj = _json.loads(probe.stdout or "{}")
                fmt = pj.get("format", {})
                dur = float(fmt.get("duration", 0) or 0)
                if dur > 0:
                    result["duration"] = int(dur)
                streams = pj.get("streams", [{}])
                w = streams[0].get("width", 0) if streams else 0
                h = streams[0].get("height", 0) if streams else 0
                if w and h:
                    result["width"] = w
                    result["height"] = h

            if not thumbnail:
                return result

            # Thumbnail — grab frame at 1s (or 0 if short)
            seek = (
                min(1, result.get("duration", 0) // 2)
                if result.get("duration", 0) > 2
                else 0
            )
            thumb_path = Path(tmp) / "thumb.jpg"
            tf = subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-ss",
                    str(seek),
                    "-i",
                    str(src),
                    "-frames:v",
                    "1",
                    "-q:v",
                    "5",
                    "-vf",
                    "scale=320:-2",
                    str(thumb_path),
                ],
                capture_output=True,
                timeout=15,
            )
            if (
                tf.returncode == 0
                and thumb_path.exists()
                and thumb_path.stat().st_size > 100
            ):
                result["thumb_bytes"] = thumb_path.read_bytes()
    except Exception as e:
        log.debug("_video_meta: %s", e)
    return result


def _send_one_chunk(
    chat_id, data_bytes: bytes, filename: str, caption="", media_type="auto"
) -> bool:
    """Send a single chunk using the correct Bale endpoint.
    For videos: extracts duration + thumbnail frame via ffmpeg for proper player display.
    Uses _real_ext() so .part files from yt-dlp route as video/audio correctly.
    """
    filename = _media_filename(filename)
    ext = _real_ext(filename)
    extra = {"chat_id": str(chat_id), "caption": caption[:1024]}

    if media_type == "document":
        return _post_file("sendDocument", "document", filename, data_bytes, extra)
    if media_type == "video" or (media_type == "auto" and ext in {"mp4", "mkv", "avi", "mov", "webm", "m4v"}):
        extra["supports_streaming"] = "true"
        # Explicit metadata: without duration/width/height some players show
        # a wrong ratio/preview (notably long TikTok videos). _video_meta is
        # fast (<1s) for normal clips; skip the temp-copy overhead for huge files.
        meta = (
            _video_meta(data_bytes, filename)
            if len(data_bytes) <= 150 * 1024 * 1024
            else {}
        )
        if meta.get("duration"):
            extra["duration"] = int(meta["duration"])
        if meta.get("width") and meta.get("height"):
            extra["width"] = int(meta["width"])
            extra["height"] = int(meta["height"])
        thumb_bytes = meta.get("thumb_bytes")
        if thumb_bytes:
            # thumb must be sent as a separate multipart field
            delay = 3
            thumb_sent = False
            for attempt in range(3):
                video_obj = io.BytesIO(data_bytes)
                thumb_obj = io.BytesIO(thumb_bytes)
                files = {
                    "video": (filename, video_obj, _mime(filename)),
                    "thumbnail": ("thumb.jpg", thumb_obj, "image/jpeg"),
                }
                try:
                    r = _session().post(
                        f"{_base_url()}/sendVideo", data=extra, files=files, timeout=180
                    )
                    resp = _safe_json(r)
                    if resp.get("ok"):
                        _conversion_capture(chat_id, resp.get("result"), filename, len(data_bytes))
                        thumb_sent = True
                        video_obj.close()
                        thumb_obj.close()
                        return True
                    code = resp.get("error_code", 0)
                    if code in (429, 500, 502, 503, 504) or r.status_code >= 500:
                        video_obj.close()
                        thumb_obj.close()
                        time.sleep(delay)
                        delay = min(delay * 2, 30)
                        continue
                    log.error("sendVideo+thumb failed: %s  file=%s", resp, filename)
                    video_obj.close()
                    thumb_obj.close()
                    break
                except Exception as e:
                    log.warning("sendVideo+thumb: %s retry %d", e, attempt + 1)
                    video_obj.close()
                    thumb_obj.close()
                    time.sleep(delay)
                    delay = min(delay * 2, 30)
            if not thumb_sent:
                log.error(
                    "sendVideo+thumb: all retries failed for %s, falling back to plain sendVideo",
                    filename,
                )
            # Fall through to plain sendVideo if thumb attempt completely failed
            return _post_file("sendVideo", "video", filename, data_bytes, extra)
        return _post_file("sendVideo", "video", filename, data_bytes, extra)
    elif media_type == "audio" or (media_type == "auto" and ext in {
        "mp3",
        "ogg",
        "wav",
        "flac",
        "m4a",
        "aac",
        "opus",
    }):
        return _post_file("sendAudio", "audio", filename, data_bytes, extra)
    elif media_type == "photo" or (media_type == "auto" and ext in {"jpg", "jpeg", "png", "gif", "webp"}):
        return _post_file("sendPhoto", "photo", filename, data_bytes, extra)
    else:
        return _post_file("sendDocument", "document", filename, data_bytes, extra)


def _split_video_ffmpeg(data: bytes, filename: str, max_mb: float = 18.0) -> list:
    """Split a video into time-based parts using ffmpeg.

    Every part is a fully self-contained, independently playable MP4:
    - -c copy   : stream copy, no re-encoding (fast, lossless)
    - -movflags +faststart : moov atom at front so playback starts immediately

    Returns list of (bytes, filename) tuples — one per part.
    Returns [(data, filename)] unchanged if ffmpeg fails for any reason.
    """
    _ensure_disk_space(len(data) * 2)
    import shutil as _sh

    ffmpeg = _sh.which("ffmpeg") or "/usr/bin/ffmpeg"
    ffprobe = _sh.which("ffprobe") or "/usr/bin/ffprobe"

    filename = _media_filename(filename)
    # Strip .part suffix before working with the file
    clean_name = filename[:-5] if filename.lower().endswith(".part") else filename

    with tempfile.TemporaryDirectory() as tmp:
        src_path = Path(tmp) / clean_name
        src_path.write_bytes(data)

        # Get duration
        try:
            probe = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(src_path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            duration = float(probe.stdout.strip() or "0")
        except Exception as e:
            log.error("_split_video_ffmpeg: ffprobe failed: %s", e)
            return [(data, filename)]

        if duration <= 0:
            log.warning("_split_video_ffmpeg: zero duration, cannot split")
            return [(data, filename)]

        total_mb = len(data) / 1024 / 1024
        n = max(2, int(total_mb / max_mb) + 1)
        seg_dur = duration / n
        base = clean_name.rsplit(".", 1)[0] if "." in clean_name else clean_name
        ext = ("." + clean_name.rsplit(".", 1)[1]) if "." in clean_name else ".mp4"

        log.info(
            "_split_video_ffmpeg: %.1fMB / %.1fs → %d parts × %.1fs each",
            total_mb,
            duration,
            n,
            seg_dur,
        )

        chunks = []
        for i in range(n):
            out_path = Path(tmp) / f"{base}.part{i + 1}of{n}{ext}"
            cmd = [
                ffmpeg,
                "-y",
                "-ss",
                str(i * seg_dur),
                "-i",
                str(src_path),
                "-t",
                str(seg_dur),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(out_path),
            ]
            proc = subprocess.run(cmd, capture_output=True, timeout=300)
            if (
                proc.returncode != 0
                or not out_path.exists()
                or out_path.stat().st_size < 1000
            ):
                log.error(
                    "_split_video_ffmpeg: part %d/%d failed rc=%d: %s",
                    i + 1,
                    n,
                    proc.returncode,
                    proc.stderr.decode(errors="replace")[:300],
                )
                return [(data, filename)]  # give up, return unsplit
            cb = out_path.read_bytes()
            chunks.append((cb, out_path.name))
            log.info(
                "_split_video_ffmpeg: part %d/%d → %.1fMB",
                i + 1,
                n,
                len(cb) / 1024 / 1024,
            )

        return chunks


def smart_send(
    chat_id, data: bytes, filename: str, caption="", media_type="auto"
) -> bool:
    """
    Smart file sender:
    - Non-exempt extension        → ZIP-wrap first
    - file ≤ CHUNK_SIZE           → send in one shot
    - video > CHUNK_SIZE          → ffmpeg time-split, every part is a playable MP4
    - other file > CHUNK_SIZE     → raw byte-split + cat instructions
    Handles yt-dlp .part suffix transparently throughout.
    """
    filename = _media_filename(filename)
    if not data:
        log.error("smart_send: empty data for %s", filename)
        return False

    _operation_prepare_media(chat_id)
    log.info(
        "smart_send: %s  %.1fMB  type=%s", filename, len(data) / 1024 / 1024, media_type
    )

    # Determine real extension (strips .part if present)
    ext = _real_ext(filename)
    is_video = media_type == "video" or (media_type == "auto" and ext in {
        "mp4",
        "mkv",
        "avi",
        "mov",
        "webm",
        "m4v",
    })

    # Step 1: ZIP-wrap if the real extension isn't exempt.
    # Skip if it's already a numbered part (part1of3) to avoid double-wrapping.
    import re as _re

    is_numbered_part = bool(_re.search(r"\.part\d+of\d+\.", filename))
    if not is_numbered_part and _should_wrap(filename):
        log.info("smart_send: wrapping %s in ZIP", filename)
        send_message(
            chat_id, f"📦 در حال زیپ کردن `{filename}`…", parse_mode="Markdown"
        )
        data, filename = _wrap_zip(data, filename)
        ext = _real_ext(filename)
        is_video = False
        media_type = "document"
        log.info("smart_send: zipped → %s  %.1fMB", filename, len(data) / 1024 / 1024)

    total_size = len(data)

    # Step 2: Small enough — send in one shot
    chunk_limit = _chunk_size()
    if total_size <= chunk_limit:
        return _send_one_chunk(chat_id, data, filename, caption, media_type)

    # Step 3: Large video → ffmpeg time-split (each part = self-contained playable MP4)
    if is_video:
        send_message(chat_id, "⏳ در حال تقسیم ویدیو…")
        # Telegram supports 50MB — only split on Bale or if file exceeds platform limit
        if _platform() == "telegram" and total_size <= _chunk_size():
            return _send_one_chunk(chat_id, data, filename, caption, media_type)
        # Use platform-specific split size
        max_mb = getattr(_ctx, "video_split_mb", 18.0)
        parts = _split_video_ffmpeg(data, filename, max_mb=max_mb)

        if len(parts) > 1:
            n = len(parts)
            send_message(
                chat_id,
                f"📤 ویدیو بزرگ است — ارسال در *{n} بخش*\n"
                f"_(هر بخش مستقلاً قابل پخش است)_",
                parse_mode="Markdown",
            )
            all_ok = True
            for i, (part_data, part_name) in enumerate(parts):
                send_message(
                    chat_id,
                    f"📤 ارسال بخش {i + 1} از {n} "
                    f"({len(part_data) / 1024 / 1024:.1f}MB)…",
                )
                ok = _send_one_chunk(
                    chat_id,
                    part_data,
                    part_name,
                    caption=(caption if i == 0 else ""),
                    media_type="video",
                )
                if not ok:
                    send_message(chat_id, f"❌ ارسال بخش {i + 1} ناموفق بود — احتمالاً از حد حجم مجاز بزرگ‌تر است. ارسال بخش‌های بعدی متوقف شد؛ فایل کامل تحویل داده نشد.")
                    all_ok = False
                    break
            if all_ok:
                send_message(
                    chat_id,
                    f"✅ همه {n} بخش ارسال شدند.\n_(هر بخش را مستقیم پخش کنید)_",
                    parse_mode="Markdown",
                )
            if all_ok:
                _send_post_download_actions(chat_id)
            return all_ok

    # Step 4: Non-video or ffmpeg fallback → raw byte-split
    chunk_size = _chunk_size()
    total_chunks = (total_size + chunk_size - 1) // chunk_size
    base2 = filename.rsplit(".", 1)[0] if "." in filename else filename
    ext2 = ("." + filename.rsplit(".", 1)[1]) if "." in filename else ""
    log.info("smart_send: byte-splitting %s into %d chunks", filename, total_chunks)
    send_message(
        chat_id,
        f"📤 فایل بزرگ است — ارسال در *{total_chunks} بخش*…",
        parse_mode="Markdown",
    )
    all_ok = True
    digits = len(str(total_chunks))
    for i in range(total_chunks):
        start = i * chunk_size
        end = min(start + chunk_size, total_size)
        chunk = data[start:end]
        chunk_name = f"{base2}.part{i + 1:0{digits}d}of{total_chunks}{ext2}"
        send_message(
            chat_id,
            f"📤 ارسال بخش {i + 1} از {total_chunks} "
            f"({len(chunk) / 1024 / 1024:.1f}MB)…",
        )
        ok = _send_one_chunk(
            chat_id, chunk, chunk_name, caption="", media_type="document"
        )
        if not ok:
            send_message(chat_id, f"❌ ارسال بخش {i + 1} ناموفق بود — احتمالاً از حد حجم مجاز بزرگ‌تر است. ارسال بخش‌های بعدی متوقف شد؛ فایل کامل تحویل داده نشد.")
            all_ok = False
            break
    if all_ok:
        import shlex
        pattern = shlex.quote("./" + base2 + ".part") + "*" + shlex.quote(f"of{total_chunks}{ext2}")
        command = f"LC_ALL=C cat {pattern} > {shlex.quote('./' + filename)}"
        send_message(
            chat_id,
            f"✅ همه {total_chunks} بخش ارسال شدند!\n\n"
            f"برای ترکیب:\n"
            f"{command}",
        )
    if all_ok:
        _send_post_download_actions(chat_id)
    return all_ok


def _send_post_download_actions(chat_id: int):
    """Send inline keyboard with post-download actions (Persian)."""
    kb = {
        "inline_keyboard": [
            [{"text": "🗑 حذف این پیام", "callback_data": "delete_this_msg"}],
            [{"text": "🏠 منوی اصلی", "callback_data": "home"}],
        ]
    }
    send_message(chat_id, "🎯 اقدامات:", reply_markup=kb)


# Convenience wrappers (keep old call sites working)
def send_document(
    chat_id, file_bytes: bytes, filename: str, caption="", reply_to=None
) -> bool:
    return smart_send(chat_id, file_bytes, filename, caption, media_type="document")


def send_video(chat_id, video_bytes: bytes, filename: str, caption="") -> bool:
    return smart_send(chat_id, video_bytes, filename, caption, media_type="video")


def send_photo(chat_id, img_bytes: bytes, caption="", reply_markup=None) -> bool:
    """Photos don't chunk — just post directly with retry."""
    if not img_bytes:
        return False
    _operation_prepare_media(chat_id)
    ext = "jpg"
    data = {"chat_id": str(chat_id), "caption": caption[:1024]}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    delay = 2
    for attempt in range(3):
        try:
            files = {"photo": (f"image.{ext}", io.BytesIO(img_bytes), "image/jpeg")}
            r = _session().post(
                f"{_base_url()}/sendPhoto", data=data, files=files, timeout=60
            )
            resp = _safe_json(r)
            if resp.get("ok"):
                _conversion_capture(chat_id, resp.get("result"), "image.jpg", len(img_bytes))
                return True
            code = resp.get("error_code", 0)
            if code in (429, 500, 502, 503, 504) or r.status_code >= 500:
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            log.error("sendPhoto failed: %s", resp)
            return False
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            log.warning("sendPhoto retry %d: %s", attempt + 1, e)
            time.sleep(delay)
            delay = min(delay * 2, 30)
        except Exception as e:
            log.error("sendPhoto exception: %s", e)
            return False
        finally:
            for upload in files.values():
                upload[1].close()
    return False


def send_audio(chat_id, audio_bytes: bytes, filename: str, caption="") -> bool:
    return smart_send(chat_id, audio_bytes, filename, caption, media_type="audio")


def send_media_group(chat_id, items: list[dict]) -> bool:
    """
    Send up to 10 photos/videos as a single album (media group).
    Each item: {"data": bytes, "fname": str, "is_video": bool, "caption": str (first item only)}

    Returns True if at least one item was sent successfully.
    Bale supports sendMediaGroup identical to Telegram's API.
    Falls back to sending one-by-one if the group call fails.
    """
    if not items:
        return False
    _operation_prepare_media(chat_id)
    # Bale/Telegram limits: max 10 items per group
    items = items[:10]

    # Build multipart: media JSON + file fields
    media_list = []
    files = {}
    for i, item in enumerate(items):
        field = f"file{i}"
        is_vid = item.get("is_video", False)
        mtype = "video" if is_vid else "photo"
        entry: dict = {"type": mtype, "media": f"attach://{field}"}
        if i == 0 and item.get("caption"):
            entry["caption"] = item["caption"][:1024]
        if is_vid:
            entry["supports_streaming"] = True
            meta = _video_meta(item["data"], item["fname"], False)
            if meta.get("duration"):
                entry["duration"] = meta["duration"]
            if meta.get("width"):
                entry["width"] = meta["width"]
                entry["height"] = meta["height"]
        media_list.append(entry)
        mime = "video/mp4" if is_vid else "image/jpeg"
        files[field] = (_media_filename(item["fname"]), io.BytesIO(item["data"]), mime)

    data = {"chat_id": str(chat_id), "media": json.dumps(media_list)}
    delay = 3
    for attempt in range(3):
        try:
            for f in files.values():
                f[1].seek(0)
            r = _session().post(
                f"{_base_url()}/sendMediaGroup", data=data, files=files, timeout=180
            )
            resp = _safe_json(r)
            if resp.get("ok"):
                for message, item in zip(resp.get("result") or [], items):
                    _conversion_capture(chat_id, message, item["fname"], len(item["data"]))
                log.info("send_media_group: OK %d items to %d", len(items), chat_id)
                # Close all BytesIO objects
                for f in files.values():
                    f[1].close()
                return True
            code = resp.get("error_code", 0)
            if code in (429, 500, 502, 503, 504) or r.status_code >= 500:
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            log.error("sendMediaGroup failed: %s", resp)
            break
        except Exception as e:
            log.warning("sendMediaGroup attempt %d: %s", attempt + 1, e)
            time.sleep(delay)
            delay = min(delay * 2, 30)
    # Close all BytesIO objects before falling back
    for f in files.values():
        try:
            f[1].close()
        except Exception:
            pass

    # Fallback: send individually
    log.warning("send_media_group: falling back to individual sends")
    sent = 0
    for item in items:
        mtype = "video" if item.get("is_video") else "photo"
        cap = item.get("caption", "")
        ok = smart_send(
            chat_id, item["data"], item["fname"], caption=cap, media_type=mtype
        )
        if ok:
            sent += 1
    return sent == len(items)


def chat_action(chat_id, action="typing"):
    if not chat_id or chat_id == 0:
        return
    try:
        _session().post(
            f"{_base_url()}/sendChatAction",
            json={"chat_id": chat_id, "action": action},
            timeout=5,
        )
    except Exception:
        pass  # Best-effort — never block on this


def get_file_url(file_id: str) -> Optional[str]:
    resp = api("getFile", file_id=file_id)
    if resp.get("ok"):
        file_path = resp["result"]["file_path"]
        # Build correct URL for each platform
        if _platform() == "telegram":
            return f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
        else:
            return f"https://tapi.bale.ai/file/bot{BALE_TOKEN}/{file_path}"
    return None


def download_bytes(
    url: str, max_bytes=None, headers: Optional[dict] = None
) -> Optional[bytes]:
    # Transfer budget is resolved per request, not frozen to Bale at import.
    # Keep an independent bounded budget even with a local Telegram Bot API.
    if max_bytes is None:
        max_bytes = TELEGRAM_MAX_FILE_SIZE if _platform() == "telegram" else BALE_MAX_FILE_SIZE
    r = None
    if not url or url.strip() in ("", "NA", "na", "none", "None", "null"):
        log.debug("download_bytes: skipping invalid URL %r", url)
        return None
    log.debug("download_bytes: %s", url)
    # Many image/CDN hosts (Bing, Pinterest, hotlink-protected sites) reject
    # requests without a browser-like User-Agent/Referer and return a small
    # HTML error page instead of the real file — which used to be silently
    # accepted as "downloaded" bytes. Always send sane default headers.
    req_headers = {"User-Agent": UA_DESK, "Referer": url}
    if headers:
        req_headers.update(headers)
    try:
        r = _public_get(url, timeout=30, headers=req_headers)
        if r.status_code >= 400:
            log.warning("download_bytes: HTTP %d for %s", r.status_code, url)
            return None
        ctype = (r.headers.get("Content-Type") or "").lower()
        if "text/html" in ctype:
            log.warning("download_bytes: got HTML content-type for %s — skipping", url)
            return None
        chunks, total = [], 0
        for chunk in r.iter_content(8192):
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                log.warning("download_bytes: exceeded %d bytes", max_bytes)
                return None
        data = b"".join(chunks)
        if not data:
            log.warning("download_bytes: empty body for %s", url)
            return None
        return data
    except Exception as e:
        log.error("download_bytes: %s — %s", url, e)
        return None
    finally:
        if r is not None:
            r.close()


def answer_cb(cb_id: str, text=""):
    try:
        api("answerCallbackQuery", callback_query_id=cb_id, text=text)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# RESULT CACHE  (stores search results so buttons can reference them)
# ═══════════════════════════════════════════════════════════════════════════════
import hashlib


def cache_set(key: str, data: list):
    with _cache_lock:
        result_cache[key] = data
        # Prune if too large
        if len(result_cache) > 500:
            oldest = list(result_cache.keys())[:100]
            for k in oldest:
                result_cache.pop(k, None)


def cache_get(key: str) -> list:
    with _cache_lock:
        return result_cache.get(key, [])


def cache_set_owned(key: str, data: list, platform: str, chat_id: int) -> None:
    """Store callback results with a short owner suffix."""
    owner_key = f"{key}_{platform}_{int(chat_id)}"
    cache_set(owner_key, data)


def cache_get_owned(key: str, platform: str, chat_id: int) -> list:
    with _cache_lock:
        return result_cache.get(f"{key}_{platform}_{int(chat_id)}", [])


def _rss_cache_key(cid: int, feed_url: str, page: int = 0) -> str:
    """Create a stable per-chat RSS callback key without exposing the URL."""
    return make_cache_key("rss", f"{_platform()}:{int(cid)}:{feed_url}:{int(page)}", 0)


def make_cache_key(prefix: str, query: str, page: int) -> str:
    h = hashlib.md5(f"{prefix}:{query}:{page}".encode()).hexdigest()[:8]
    return f"{prefix}_{h}"


# ═══════════════════════════════════════════════════════════════════════════════
# KEYBOARDS
# ═══════════════════════════════════════════════════════════════════════════════
def main_menu_kb():
    items = [
        ("🔎 جستجو و خواندن", "nav_read"),
        ("📥 دانلود و شبکه‌ها", "nav_download"),
        ("🎵 موسیقی", "mode_music"),
        ("📚 کتاب و مقاله", "mode_book"),
        ("🧰 ابزارهای بیشتر", "mode_tools"),
        ("⭐️ کتابخانه و تاریخچه", "library"),
        ("🔒 حریم خصوصی", "privacy"),
    ]
    return {"inline_keyboard": [
        [{"text": t, "callback_data": d}] for t, d in items
    ] + [[{"text": "📊 آمار", "callback_data": "stats"},
          {"text": "❓ راهنما", "callback_data": "help"}]]}


def category_kb(category):
    items = {
        "nav_read": [("🔎 جستجوی وب", "mode_search"), ("🌐 مشاهده سایت", "mode_open"),
                     ("📖 ویکی‌پدیا", "mode_wiki"), ("🎓 مقاله علمی", "mode_scholar"),
                     ("📰 اخبار و اشتراک‌ها", "mode_rss")],
        "nav_download": [("📱 شبکه‌های اجتماعی", "mode_social"), ("🎬 فیلم و سریال", "mode_movie"),
                         ("🐙 گیت‌هاب", "mode_github"), ("📱 برنامه اندروید", "mode_apk"),
                         ("🖼 دانلود عکس", "mode_images"), ("🛡 تشخیص ویروس", "mode_virusscan")],
    }[category]
    rows = [[{"text": t, "callback_data": d}] for t, d in items]
    rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
    return {"inline_keyboard": rows}


def social_menu_kb():
    """Sub-menu for all social media platforms."""
    return {
        "inline_keyboard": [
            [
                {"text": "▶️ YouTube", "callback_data": "mode_youtube"},
                {"text": "🐦 Twitter / X", "callback_data": "mode_twitter"},
            ],
            [
                {"text": "📸 Instagram", "callback_data": "mode_instagram"},
                {"text": "🎵 TikTok", "callback_data": "mode_tiktok"},
            ],
            [
                {"text": "🦋 Bluesky", "callback_data": "mode_bluesky"},
                {"text": "📱 Reddit", "callback_data": "mode_reddit"},
            ],
            [
                {"text": "🎮 Twitch", "callback_data": "mode_twitch"},
                {"text": "🎬 Vimeo", "callback_data": "mode_vimeo"},
            ],
            [
                {"text": "📌 Pinterest Video", "callback_data": "mode_pinterest_video"},
                {"text": "✈️ Telegram Channel", "callback_data": "mode_tg_channel"},
            ],
            [{"text": "🏠 منوی اصلی", "callback_data": "home"}],
        ]
    }


def cancel_kb():
    return {"inline_keyboard": [[{"text": "🔙 برگشت", "callback_data": "home"},
                                 {"text": "❌ انصراف", "callback_data": "cancel"}]]}


def home_kb():
    return {"inline_keyboard": [[{"text": "🏠 منوی اصلی", "callback_data": "home"}]]}


def tools_kb():
    """No-cost text/file utilities exposed from the ابزارهای بیشتر menu."""
    return {
        "inline_keyboard": [
            [
                {"text": "🌐 ترجمه سریع به فارسی", "callback_data": "mode_translate_fa"},
                {"text": "🌐 ترجمه (زبان دلخواه)", "callback_data": "mode_translate"},
            ],
            [
                {"text": "💱 تبدیل ارز", "callback_data": "mode_currency"},
                {"text": "🔳 ساخت QR", "callback_data": "mode_qr"},
            ],
            [
                {"text": "🌐 IP / دامنه", "callback_data": "mode_iplookup"},
                {"text": "🖼 متن از عکس OCR", "callback_data": "mode_ocr"},
            ],
            [
                {"text": "🔗 کوتاه‌سازی لینک", "callback_data": "mode_shorten"},
                {"text": "🔎 بازکردن لینک", "callback_data": "mode_expand"},
            ],
            [
                {"text": "🔄 تبدیل قالب فایل", "callback_data": "mode_convert"},
                {"text": "📋 ساخت متن اشتراکی", "callback_data": "mode_paste"},
                {"text": "🖼 فشرده‌سازی عکس", "callback_data": "mode_image_compress"},
            ],
            [
                {"text": "📄 جداکردن صفحات PDF", "callback_data": "mode_pdf_extract"},
                {"text": "📚 ادغام PDF", "callback_data": "mode_pdf_merge"},
            ],
            [
                {"text": "✂️ برش ویدیو", "callback_data": "mode_video_trim"},
                {"text": "🎞 فشرده‌سازی ویدیو", "callback_data": "mode_video_compress"},
            ],
            [{"text": "🏠 منوی اصلی", "callback_data": "home"}],
        ]
    }


def library_kb():
    return {
        "inline_keyboard": [
            [
                {"text": "⭐️ ذخیره‌شده‌ها", "callback_data": "library_saved"},
                {"text": "👀 فهرست تماشا", "callback_data": "library_watchlist"},
            ],
            [
                {"text": "🕘 تاریخچه و تلاش دوباره", "callback_data": "library_history"},
            ],
            [{"text": "🏠 منوی اصلی", "callback_data": "home"}],
        ]
    }


def rss_menu_kb():
    return {
        "inline_keyboard": [
            [
                {"text": "📰 خواندن فید", "callback_data": "rss_read"},
                {"text": "🔔 اشتراک فید", "callback_data": "rss_subscribe"},
            ],
            [
                {"text": "📋 اشتراک‌های من", "callback_data": "rss_subscriptions"},
            ],
            [{"text": "🏠 منوی اصلی", "callback_data": "home"}],
        ]
    }


def _library_items_kb(
    items: list[dict], page: int = 0, has_next: bool = False, watchlist_only: bool = False
) -> dict:
    rows = []
    for item in items[:5]:
        title = str(item.get("title") or "مورد ذخیره‌شده").replace("\n", " ")[:48]
        rows.append([{"text": f"📌 {title}", "callback_data": f"lib_item_{int(item['id'])}"}])
    nav = []
    prefix = "lib_page_watch_" if watchlist_only else "lib_page_saved_"
    if page > 0:
        nav.append({"text": "◀️ قبلی", "callback_data": f"{prefix}{page - 1}"})
    if has_next:
        nav.append({"text": "بعدی ▶️", "callback_data": f"{prefix}{page + 1}"})
    if nav:
        rows.append(nav)
    rows.append([
        {"text": "👀 فهرست تماشا", "callback_data": "library_watchlist"},
        {"text": "🕘 تاریخچه", "callback_data": "library_history"},
    ])
    rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
    return {"inline_keyboard": rows}


def audio_format_kb():
    """Keyboard for selecting audio format/quality."""
    return {
        "inline_keyboard": [
            [{"text": "🎵 MP3 (320kbps)", "callback_data": "audio_fmt_mp3_320"}],
            [{"text": "🎵 MP3 (256kbps)", "callback_data": "audio_fmt_mp3_256"}],
            [{"text": "🎵 MP3 (192kbps)", "callback_data": "audio_fmt_mp3_192"}],
            [{"text": "🎵 M4A (AAC 256kbps)", "callback_data": "audio_fmt_m4a_256"}],
            [{"text": "🎵 OPUS (160kbps)", "callback_data": "audio_fmt_opus_160"}],
            [{"text": "🎵 FLAC (Lossless)", "callback_data": "audio_fmt_flac_0"}],
            [{"text": "⚡ Best Available (no conversion)", "callback_data": "audio_fmt_best_0"}],
            [{"text": "❌ انصراف", "callback_data": "cancel"}],
        ]
    }


def paged_kb(prev_cb, next_cb, page, has_next):
    row = []
    if page > 0:
        row.append({"text": "◀️ قبلی", "callback_data": prev_cb})
    if has_next:
        row.append({"text": "بعدی ▶️", "callback_data": next_cb})
    buttons = []
    if row:
        buttons.append(row)
    buttons.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
    return {"inline_keyboard": buttons}


def image_source_kb():
    return {
        "inline_keyboard": [
            [{"text": "🌐 چند منبع (پیشنهادی)", "callback_data": "img_src_multi"}],
            [
                {"text": "🦆 DuckDuckGo", "callback_data": "img_src_ddg"},
                {"text": "🖼 Bing", "callback_data": "img_src_bing"},
            ],
            [
                {"text": "🔍 Google", "callback_data": "img_src_google"},
                {"text": "📌 پینترست", "callback_data": "img_src_pinterest"},
            ],
            [
                {"text": "📷 Pixabay/Pexels", "callback_data": "img_src_pexels"},
                {"text": "🎨 Wikimedia", "callback_data": "img_src_wiki"},
            ],
            [
                {"text": "🎨 DeviantArt", "callback_data": "img_src_deviantart"},
                {"text": "🎨 Pixiv", "callback_data": "img_src_pixiv"},
            ],
            [
                {"text": "🎨 ArtStation", "callback_data": "img_src_artstation"},
                {"text": "📷 Flickr", "callback_data": "img_src_flickr"},
            ],
            [
                {"text": "📝 Tumblr", "callback_data": "img_src_tumblr"},
                {"text": "📷 Imgur", "callback_data": "img_src_imgur"},
            ],
            [
                {"text": "🦊 Kemono", "callback_data": "img_src_kemono"},
            ],
            [
                {"text": "🎮 Newgrounds", "callback_data": "img_src_newgrounds"},
            ],
            [{"text": "❌ انصراف", "callback_data": "cancel"}],
        ]
    }


def youtube_action_kb():
    return {
        "inline_keyboard": [
            [
                {"text": "📥 دانلود ویدیو", "callback_data": "yt_video"},
                {"text": "🔍 جستجوی یوتیوب", "callback_data": "yt_search"},
            ],
            [
                {"text": "🖼 کاور (Thumbnail)", "callback_data": "yt_thumb"},
                {"text": "📊 آمار ویدیو", "callback_data": "yt_stats"},
            ],
            [
                {"text": "✂️ برش بازه ویدیو", "callback_data": "yt_clip"},
                {"text": "📃 فهرست پخش", "callback_data": "yt_playlist"},
            ],
            [{"text": "❌ انصراف", "callback_data": "cancel"}],
        ]
    }


def github_action_kb():
    return {
        "inline_keyboard": [
            [
                {"text": "🔍 جستجوی مخازن", "callback_data": "gh_search"},
                {"text": "📥 دانلود ZIP", "callback_data": "gh_zip"},
            ],
            [{"text": "📦 دانلود Release", "callback_data": "gh_release"}],
            [{"text": "❌ انصراف", "callback_data": "cancel"}],
        ]
    }


def tg_channel_kb():
    return {
        "inline_keyboard": [
            [
                {"text": "📖 خواندن پیام‌ها (عمومی)", "callback_data": "tg_read_web"},
                {"text": "🔐 خواندن با MTProto", "callback_data": "tg_read_mtproto"},
            ],
            [{"text": "📥 دانلود رسانه پیام", "callback_data": "tg_dl_media"}],
            [{"text": "❌ انصراف", "callback_data": "cancel"}],
        ]
    }


def twitter_kb():
    return {
        "inline_keyboard": [
            [
                {"text": "📰 پیام‌های کاربر", "callback_data": "tw_timeline"},
                {"text": "📥 دانلود ویدیو/عکس", "callback_data": "tw_dl"},
            ],
            [{"text": "🇮🇷 دانلود + ترجمه", "callback_data": "tw_dl_translate"}],
            [{"text": "🧵 خواندن ترید (رشته‌توییت)", "callback_data": "tw_thread"}],
            [{"text": "❌ انصراف", "callback_data": "cancel"}],
        ]
    }


def instagram_kb():
    return {
        "inline_keyboard": [
            [
                {"text": "📋 پست‌های پروفایل", "callback_data": "ig_profile"},
                {"text": "📥 دانلود پست/ریل", "callback_data": "ig_dl"},
            ],
            [{"text": "📸 دانلود استوری (عمومی)", "callback_data": "ig_story"}],
            [
                {"text": "🖼 عکس پروفایل", "callback_data": "ig_profile_pic"},
                {"text": "📸 اسکرین‌شات پروفایل", "callback_data": "ig_profile_shot"},
            ],
            [{"text": "🔲 موزاییک پست", "callback_data": "ig_mosaic"}],
            [{"text": "❌ انصراف", "callback_data": "cancel"}],
        ]
    }


def tiktok_kb():
    return {
        "inline_keyboard": [
            [
                {"text": "🎥 ویدیوهای کاربر", "callback_data": "tt_user"},
                {"text": "📥 دانلود ویدیو", "callback_data": "tt_dl"},
            ],
            [{"text": "🎵 دانلود صدای پست", "callback_data": "tt_sound"}],
            [{"text": "❌ انصراف", "callback_data": "cancel"}],
        ]
    }


def reddit_kb():
    return {
        "inline_keyboard": [
            [
                {"text": "📥 دانلود پست", "callback_data": "reddit_dl"},
                {"text": "👤 پروفایل کاربر", "callback_data": "reddit_profile"},
            ],
            [{"text": "❌ انصراف", "callback_data": "cancel"}],
        ]
    }


def twitch_kb():
    return {
        "inline_keyboard": [
            [
                {"text": "📥 دانلود VOD/Clip", "callback_data": "twitch_dl"},
                {"text": "👤 استریم‌های کاربر", "callback_data": "twitch_user"},
            ],
            [{"text": "❌ انصراف", "callback_data": "cancel"}],
        ]
    }


def vimeo_kb():
    return {
        "inline_keyboard": [
            [
                {"text": "📥 دانلود ویدیو", "callback_data": "vimeo_dl"},
            ],
            [{"text": "❌ انصراف", "callback_data": "cancel"}],
        ]
    }


def pinterest_kb():
    return {
        "inline_keyboard": [
            [
                {"text": "🔍 جستجوی عکس", "callback_data": "pin_search"},
                {"text": "📥 دانلود ویدیو", "callback_data": "pin_dl"},
            ],
            [{"text": "❌ انصراف", "callback_data": "cancel"}],
        ]
    }


def book_kb():
    """منوی دانلود کتاب و مقاله."""
    return {
        "inline_keyboard": [
            [
                {"text": "🔍 جستجوی کتاب", "callback_data": "book_search"},
                {"text": "🔍 جستجوی مقاله", "callback_data": "book_search_art"},
            ],
            [
                {"text": "🎧 کتاب صوتی", "callback_data": "book_ravebook_audio"},
            ],
            [
                {"text": "📄 فقط PDF", "callback_data": "book_filter_pdf"},
                {"text": "📖 فقط EPUB", "callback_data": "book_filter_epub"},
            ],
            [
                {"text": "📝 فقط FB2/MOBI", "callback_data": "book_filter_other"},
                {"text": "🌐 کشف ترجمه (opac.nlai.ir)", "url": "https://opac.nlai.ir/"},
            ],
            [
                {
                    "text": "🔗 منابع بیشتر کتاب",
                    "callback_data": "book_sources",
                }
            ],
            [{"text": "❌ انصراف", "callback_data": "cancel"}],
        ]
    }


# Additional book sources the user asked for. Verified from this VPS:
# libsan.ir is reachable but PAID (no free file); pdfdrive search is
# bot-gated and myanonamouse needs a login, so those two are link-only.
# singlelogin.re no longer resolves (NXDOMAIN) and is intentionally omitted.
BOOK_EXTRA_SOURCES = [
    ("🌐 Libsan (فروشگاه کتاب)", "https://libsan.ir/"),
    ("🌐 PDFDrive", "https://www.pdfdrive.com/"),
    ("🌐 MyAnonamouse", "https://www.myanonamouse.net/"),
    ("🌐 Library Genesis", "https://libgen.li/"),
    ("🌐 Internet Archive (امانت کتاب)", "https://archive.org/"),
]


def book_sources_kb() -> dict:
    """لینک منابع جانبی کتاب."""
    rows = [
        [{"text": label, "url": url}]
        for label, url in BOOK_EXTRA_SOURCES
    ]
    rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
    return {"inline_keyboard": rows}


def book_results_kb(results: list, cache_key: str) -> dict:
    """نتایج جستجوی کتاب به‌صورت دکمه."""
    rows = []
    for i, book in enumerate(results[:10]):
        name = book.get("name", f"کتاب {i + 1}")[:38]
        ext = book.get("extension", "").upper()
        size = book.get("size", "")
        label = f"📚 {name}"
        if ext:
            label += f" [{ext}]"
        if size:
            label += f" {size}"
        rows.append(
            [{"text": label[:60], "callback_data": f"book_item_{cache_key}_{i}"}]
        )
    rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
    return {"inline_keyboard": rows}


def book_book_kb(book_url_key: str) -> dict:
    """دکمه دانلود کتاب."""
    return {
        "inline_keyboard": [
            [{"text": "📥 دانلود کتاب", "callback_data": f"book_dl_{book_url_key}"}],
            [
                {"text": "🔙 برگشت", "callback_data": "book_back"},
                {"text": "🏠 منوی اصلی", "callback_data": "home"},
            ],
        ]
    }


def apk_kb():
    """منوی APK Downloader."""
    return {
        "inline_keyboard": [
            [
                {"text": "🔍 جستجوی اپ", "callback_data": "apk_search"},
                {"text": "📦 دانلود مستقیم", "callback_data": "apk_direct"},
            ],
            [{"text": "❌ انصراف", "callback_data": "cancel"}],
        ]
    }


def apk_results_kb(results: list, cache_key: str) -> dict:
    """نتایج جستجوی Google Play به‌صورت دکمه."""
    rows = []
    for i, app in enumerate(results[:8]):
        title = app.get("title", f"App {i + 1}")[:30]
        score = app.get("score", 0)
        stars = f"⭐{score:.1f}" if score else ""
        price = "" if app.get("free", True) else f" 💰{app.get('price', '')}"
        label = f"📱 {title} {stars}{price}".strip()[:55]
        rows.append([{"text": label, "callback_data": f"apk_item_{cache_key}_{i}"}])
    rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
    return {"inline_keyboard": rows}


def apk_item_kb(app_id: str) -> dict:
    """دکمه‌های اقدام برای یک اپ."""
    if not app_id:
        return {
            "inline_keyboard": [[{"text": "🏠 منوی اصلی", "callback_data": "home"}]]
        }
    safe_id = app_id.replace(".", "_")
    return {
        "inline_keyboard": [
            [
                {"text": "📥 دانلود APK", "callback_data": f"apk_dl_{safe_id}"},
                {"text": "🌐 صفحه Play Store", "callback_data": f"apk_store_{safe_id}"},
            ],
            [
                {"text": "🔙 برگشت به نتایج", "callback_data": "apk_back"},
                {"text": "🏠 منوی اصلی", "callback_data": "home"},
            ],
        ]
    }


def social_results_kb(results: list, cache_key: str, platform: str) -> dict:
    """Generic results keyboard for social media posts (tg/tw/ig/tt)."""
    icon_map = {"tg": "✈️", "tw": "🐦", "ig": "📸", "tt": "🎵"}
    icon = icon_map.get(platform, "📌")
    rows = []
    for i, r in enumerate(results[:15]):
        text = r.get("text", r.get("title", ""))
        first_line = text.split("\n")[0].strip()[:38] if text else f"پیام {i + 1}"
        media_icons = ""
        if r.get("has_photo") or r.get("img_urls"):
            media_icons += "🖼"
        if r.get("has_video") or r.get("is_video"):
            media_icons += "🎬"
        label = f"{icon}{media_icons} {first_line}".strip()[:52]
        rows.append(
            [{"text": label, "callback_data": f"soc_{platform}_{cache_key}_{i}"}]
        )
    rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
    return {"inline_keyboard": rows}


def social_post_kb(url: str, platform: str, has_media: bool) -> dict:
    """Buttons under a displayed post (metadata-only save + normal actions)."""
    url_key = store_url(url, chat_id=getattr(_ctx, "chat_id", None))
    rows = []
    if has_media:
        # Platforms with video content (not short-form)
        video_platforms = {"reddit", "twitch", "vimeo", "pinterest", "hls"}
        if platform in video_platforms:
            rows.append(
                [
                    {
                        "text": "📥 دانلود ویدیو",
                        "callback_data": f"soc_dl_{platform}_{url_key}",
                    },
                    {
                        "text": "🎵 فقط صدا",
                        "callback_data": f"soc_dl_audio_{platform}_{url_key}",
                    },
                    {
                        "text": "🎬 کیفیت",
                        "callback_data": f"soc_dl_quality_{platform}_{url_key}",
                    },
                ]
            )
        else:
            # Short-form platforms (Twitter, TikTok, Instagram)
            rows.append(
                [
                    {
                        "text": "📥 دانلود",
                        "callback_data": f"soc_dl_{platform}_{url_key}",
                    }
                ]
            )
    rows.append(
        [
            {"text": "🔗 باز کردن لینک", "callback_data": f"site_ss_{url_key}"},
            {"text": "⭐️ ذخیره", "callback_data": f"lib_save_{platform}_{url_key}"},
            {"text": "🏠 منوی اصلی", "callback_data": "home"},
        ]
    )
    return {"inline_keyboard": [[button] for row in rows for button in row]}


def translate_kb():
    langs = [
        ("🇮🇷 فارسی", "fa"),
        ("🇬🇧 انگلیسی", "en"),
        ("🇸🇦 عربی", "ar"),
        ("🇩🇪 آلمانی", "de"),
        ("🇫🇷 فرانسوی", "fr"),
        ("🇷🇺 روسی", "ru"),
    ]
    rows, row = [], []
    for label, code in langs:
        row.append({"text": label, "callback_data": f"trlang_{code}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "❌ انصراف", "callback_data": "cancel"}])
    return {"inline_keyboard": rows}


def site_view_kb(url_key: str):
    """Buttons shown after screenshot of a website."""
    return {
        "inline_keyboard": [
            [
                {"text": "📝 متن صفحه", "callback_data": f"site_text_{url_key}"},
                {"text": "🌐 فایل HTML", "callback_data": f"site_html_{url_key}"},
            ],
            [
                {"text": "🗜 ZIP آفلاین", "callback_data": f"site_zip_{url_key}"},
                {"text": "📑 PDF صفحه", "callback_data": f"site_pdf_{url_key}"},
            ],
            [
                {"text": "🌐 ترجمه فارسی", "callback_data": f"site_tr_{url_key}"},
                {"text": "🛡 بررسی VirusTotal", "callback_data": f"site_scan_{url_key}"},
            ],
            [{"text": "🏠 منوی اصلی", "callback_data": "home"}],
        ]
    }


def search_results_kb(
    results: list, cache_key: str, page: int, has_next: bool, mode: str
) -> dict:
    """Each search result becomes an inline button."""
    rows = []
    offset = page * 10
    for i, r in enumerate(results):
        num = offset + i + 1
        title = r.get("title", r.get("name", f"نتیجه {num}"))[:45]
        cb = f"res_{cache_key}_{i}"
        rows.append([{"text": f"{num}. {title}", "callback_data": cb}])
    # Pagination row
    pag = []
    if page > 0:
        pag.append({"text": "◀️ قبلی", "callback_data": f"search_page_{cache_key}_{page - 1}"})
    if has_next:
        pag.append({"text": "بعدی ▶️", "callback_data": f"search_page_{cache_key}_{page + 1}"})
    if pag:
        rows.append(pag)
    rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
    return {"inline_keyboard": rows}


def yt_results_kb(results: list, cache_key: str, page: int, has_next: bool) -> dict:
    rows = []
    offset = page * 8
    for i, r in enumerate(results):
        num = offset + i + 1
        title = r.get("title", f"ویدیو {num}")[:40]
        dur = f" [{r.get('duration', '')}]" if r.get("duration") else ""
        rows.append(
            [
                {
                    "text": f"{num}. {title}{dur}",
                    "callback_data": f"yt_res_{cache_key}_{i}",
                }
            ]
        )
    pag = []
    if page > 0:
        pag.append({"text": "◀️ قبلی", "callback_data": f"yt_page_{page - 1}"})
    if has_next:
        pag.append({"text": "بعدی ▶️", "callback_data": f"yt_page_{page + 1}"})
    if pag:
        rows.append(pag)
    rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
    return {"inline_keyboard": rows}


def gh_repo_kb(results: list, cache_key: str, page: int, has_next: bool) -> dict:
    rows = []
    offset = page * 8
    for i, r in enumerate(results):
        num = offset + i + 1
        name = r.get("full_name", f"repo {num}")[:40]
        stars = r.get("stargazers_count", 0)
        rows.append(
            [
                {
                    "text": f"{num}. {name} ⭐{stars}",
                    "callback_data": f"gh_res_{cache_key}_{i}",
                }
            ]
        )
    pag = []
    if page > 0:
        pag.append({"text": "◀️ قبلی", "callback_data": f"gh_page_{page - 1}"})
    if has_next:
        pag.append({"text": "بعدی ▶️", "callback_data": f"gh_page_{page + 1}"})
    if pag:
        rows.append(pag)
    rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
    return {"inline_keyboard": rows}


def gh_repo_action_kb(repo_full: str) -> dict:
    safe = repo_full.replace("/", "__")
    return {
        "inline_keyboard": [
            [
                {"text": "📥 دانلود ZIP کل مخزن", "callback_data": f"ghact_zip_{safe}"},
                {"text": "📦 آخرین Release", "callback_data": f"ghact_rel_{safe}"},
            ],
            [{"text": "📋 اطلاعات بیشتر", "callback_data": f"ghact_info_{safe}"}],
            [{"text": "🔙 برگشت به نتایج", "callback_data": "gh_back"}],
        ]
    }


def images_more_kb(cache_key: str, page: int, source: str) -> dict:
    return {
        "inline_keyboard": [
            [
                {
                    "text": "📥 دانلود بیشتر",
                    "callback_data": f"img_more_{source}_{cache_key}_{page + 1}",
                }
            ],
            [
                {"text": "🔄 منبع دیگر", "callback_data": "mode_images"},
                {"text": "🏠 منوی اصلی", "callback_data": "home"},
            ],
        ]
    }


def wiki_result_kb(results: list, cache_key: str, lang: str) -> dict:
    rows = []
    for i, r in enumerate(results):
        title = r.get("title", f"مقاله {i + 1}")[:50]
        rows.append(
            [
                {
                    "text": f"📖 {title}",
                    "callback_data": f"wiki_art_{cache_key}_{i}_{lang}",
                }
            ]
        )
    rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
    return {"inline_keyboard": rows}


# ═══════════════════════════════════════════════════════════════════════════════
# WEB SCRAPING FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════
UA_DESK = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
UA_MOB = (
    "Mozilla/5.0 (Linux; Android 13; SM-G991B) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.6261.119 Mobile Safari/537.36"
)


def web_search(query: str, max_results=10, page=0) -> list[dict]:
    log.info("web_search: %r  page=%d", query, page)
    data = {"q": query, "b": "", "kl": "wt-wt"}
    if page > 0:
        data.update(
            {"s": str(page * 30), "dc": str(page * 30 + 1), "v": "l", "o": "json"}
        )
    try:
        r = WEB.post(
            "https://html.duckduckgo.com/html/",
            data=data,
            headers={"User-Agent": UA_DESK, "Accept-Language": "en-US,en;q=0.9"},
            timeout=20,
        )
        log.debug("DDG: status=%d len=%d", r.status_code, len(r.text))
        soup = BeautifulSoup(r.text, "html.parser")
        results = []
        for div in soup.select(".result, .web-result"):
            ta = div.select_one(".result__title a, .result__a, h2 a")
            if not ta:
                continue
            href = ta.get("href", "")
            m = re.search(r"uddg=([^&]+)", href)
            link = urllib.parse.unquote(m.group(1)) if m else href
            if not link.startswith("http"):
                continue
            sn = div.select_one(".result__snippet, .result__body")
            results.append(
                {
                    "title": ta.get_text(strip=True),
                    "link": link,
                    "snippet": sn.get_text(strip=True) if sn else "",
                }
            )
            if len(results) >= max_results:
                break
        log.info("web_search: %d results", len(results))
        if not results:
            log.warning("DDG returned 0 results. HTML head: %s", r.text[:300])
        return results
    except Exception as e:
        log.error("web_search error: %s", e, exc_info=True)
        return []


def fetch_page(url: str) -> Optional[bytes]:
    log.info("fetch_page: %s", url)
    headers = {
        "User-Agent": UA_DESK,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        r = WEB.get(url, headers=headers, timeout=25, allow_redirects=True)
        log.debug("fetch_page: status=%d len=%d", r.status_code, len(r.content))
        r.raise_for_status()
        return r.content
    except requests.exceptions.SSLError:
        log.warning("fetch_page: SSL error, retrying without verify")
        try:
            r = WEB.get(url, headers=headers, timeout=25, verify=False)
            return r.content
        except Exception as e:
            log.error("fetch_page SSL fallback: %s", e)
            return None
    except Exception as e:
        log.error("fetch_page: %s", e, exc_info=True)
        return None


# JS injected before every screenshot/PDF to remove popups, banners, paywalls
_CLEANUP_JS = """
(function() {
  // ── Remove popup/overlay/cookie/chat elements ────────────────────────────
  const badSelectors = [
    '[class*="cookie"]','[id*="cookie"]',
    '[class*="consent"]','[id*="consent"]',
    '[class*="gdpr"]','[id*="gdpr"]',
    '[class*="banner"]','[id*="banner"]',
    '[class*="popup"]','[id*="popup"]',
    '[class*="modal"]','[id*="modal"]',
    '[class*="overlay"]','[id*="overlay"]',
    '[class*="paywall"]','[id*="paywall"]',
    '[class*="subscribe"]','[id*="subscribe"]',
    '[class*="newsletter"]','[id*="newsletter"]',
    '[class*="chat"]','[id*="chat"]',
    '[class*="tawk"]','[id*="tawk"]',
    '[class*="intercom"]','[id*="intercom"]',
    '[class*="zendesk"]','[id*="zendesk"]',
    '[class*="drift"]','[id*="drift"]',
    '[class*="crisp"]','[id*="crisp"]',
    '[class*="freshchat"]',
    '.fc-dialog-container','.qc-cmp2-container',
    '#onetrust-banner-sdk','#onetrust-accept-btn-handler',
    '.cc-banner','.cc-window',
    '.pum-overlay','.pum-container',
    '[id*="sp_message"]','[class*="sp_message"]',
  ];
  badSelectors.forEach(sel => {
    try { document.querySelectorAll(sel).forEach(el => el.remove()); } catch(e) {}
  });
  // ── Remove fixed/sticky elements (nav bars, floating buttons, bars) ──────
  document.querySelectorAll('*').forEach(el => {
    const s = window.getComputedStyle(el);
    if ((s.position === 'fixed' || s.position === 'sticky') &&
        (parseInt(s.zIndex) > 100 || s.zIndex === 'auto')) {
      const rect = el.getBoundingClientRect();
      // Only remove elements that cover more than 30% width (real banners)
      if (rect.width > window.innerWidth * 0.3 &&
          (rect.top < 100 || rect.bottom > window.innerHeight - 100)) {
        el.remove();
      }
    }
  });
  // ── Restore scroll, remove overflow:hidden on body/html ─────────────────
  document.body.style.overflow = 'auto';
  document.documentElement.style.overflow = 'auto';
  document.body.style.position = '';
  // ── Fix font rendering ───────────────────────────────────────────────────
  document.body.style.webkitFontSmoothing = 'antialiased';
  document.body.style.textRendering = 'optimizeLegibility';
})();
"""


@_browser_job
def screenshot_page(url: str) -> Optional[bytes]:
    """
    1920×1080 screenshot via shot-scraper with popup/banner removal.
    Falls back to playwright if shot-scraper unavailable.
    """
    log.info("screenshot_page: %s", url)
    out = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
            out = tf.name
        cmd = [
            "shot-scraper",
            "shot",
            url,
            "-o",
            out,
            "--width",
            "1920",
            "--height",
            "1080",
            "--quality",
            "85",
            "--wait",
            "2000",
            "--javascript",
            _CLEANUP_JS,
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=45)
        if (
            result.returncode == 0
            and Path(out).exists()
            and Path(out).stat().st_size > 1000
        ):
            data = Path(out).read_bytes()
            log.info("screenshot_page (shot-scraper): %dKB", len(data) // 1024)
            return data
        log.warning(
            "shot-scraper rc=%d: %s",
            result.returncode,
            result.stderr.decode(errors="replace")[:200],
        )
    except FileNotFoundError:
        log.warning("shot-scraper not found, falling back to playwright")
    except subprocess.TimeoutExpired:
        log.warning("screenshot_page: shot-scraper timed out")
    except Exception as e:
        log.error("screenshot_page shot-scraper: %s", e)
    finally:
        if out is not None:
            Path(out).unlink(missing_ok=True)

    # Fallback: playwright
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ]
            )
            try:
                page = browser.new_page(viewport={"width": 1920, "height": 1080})
                page.goto(url, timeout=25000, wait_until="domcontentloaded")
                time.sleep(2)
                page.evaluate(_CLEANUP_JS)
                img = page.screenshot(
                    type="jpeg",
                    quality=85,
                    clip={"x": 0, "y": 0, "width": 1920, "height": 1080},
                )
            finally:
                browser.close()
        log.info("screenshot_page (playwright fallback): %dKB", len(img) // 1024)
        return img
    except Exception as e:
        log.error("screenshot_page playwright: %s", e, exc_info=True)
        return None


def page_to_zip(url: str) -> Optional[bytes]:
    log.info("page_to_zip: %s", url)
    html = fetch_page(url)
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("index.html", html)
        base = (
            f"{urllib.parse.urlparse(url).scheme}://{urllib.parse.urlparse(url).netloc}"
        )
        for tag in soup.find_all(["img", "link", "script"])[:25]:
            src = tag.get("src") or tag.get("href", "")
            if not src or src.startswith("data:"):
                continue
            asset_url = urllib.parse.urljoin(base, src)
            try:
                ar = WEB.get(asset_url, timeout=8)
                fname = Path(urllib.parse.urlparse(asset_url).path).name or "asset"
                zf.writestr(f"assets/{fname}", ar.content)
            except Exception:
                pass
    buf.seek(0)
    return buf.read()


def page_to_text(url: str) -> str:
    html = fetch_page(url)
    if not html:
        return "❌ خطا در دریافت صفحه."
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "nav", "footer", "aside"]):
        t.decompose()
    lines = [l for l in soup.get_text("\n", strip=True).split("\n") if l.strip()]
    return "\n".join(lines[:100])


@_browser_job
def page_to_pdf(url: str) -> Optional[bytes]:
    """
    Generate a full-page PDF via shot-scraper (uses Playwright's print-to-PDF
    internally — captures the entire scrollable page as a proper multi-page PDF).
    Falls back to wkhtmltopdf, then raw HTML.
    """
    log.info("page_to_pdf: %s", url)

    # Strategy 1: shot-scraper pdf (best — full page, proper PDF, popup removal)
    out = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            out = tf.name
        cmd = [
            "shot-scraper",
            "pdf",
            url,
            "-o",
            out,
            "--wait",
            "2000",
            "--javascript",
            _CLEANUP_JS,
            "--media-screen",  # use screen CSS (better fonts/colors)
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=60)
        if (
            result.returncode == 0
            and Path(out).exists()
            and Path(out).stat().st_size > 500
        ):
            data = Path(out).read_bytes()
            log.info("page_to_pdf (shot-scraper): %dKB", len(data) // 1024)
            return data
        log.warning(
            "shot-scraper pdf rc=%d: %s",
            result.returncode,
            result.stderr.decode(errors="replace")[:200],
        )
    except FileNotFoundError:
        log.warning("shot-scraper not found for PDF")
    except subprocess.TimeoutExpired:
        log.warning("page_to_pdf: shot-scraper timed out")
    except Exception as e:
        log.error("page_to_pdf shot-scraper: %s", e)
    finally:
        if out is not None:
            Path(out).unlink(missing_ok=True)

    # Strategy 2: Playwright print-to-PDF directly
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ]
            )
            try:
                page = browser.new_page(viewport={"width": 1920, "height": 1080})
                page.goto(url, timeout=30000, wait_until="networkidle")
                time.sleep(2)
                page.evaluate(_CLEANUP_JS)
                pdf_bytes = page.pdf(
                    format="A4",
                    print_background=True,
                    margin={
                        "top": "10mm",
                        "bottom": "10mm",
                        "left": "10mm",
                        "right": "10mm",
                    },
                )
            finally:
                browser.close()
        if pdf_bytes and len(pdf_bytes) > 500:
            log.info("page_to_pdf (playwright): %dKB", len(pdf_bytes) // 1024)
            return pdf_bytes
    except Exception as e:
        log.warning("page_to_pdf playwright: %s", e)

    # Strategy 3: wkhtmltopdf
    out = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            out = tf.name
        subprocess.run(
            [
                "wkhtmltopdf",
                "--quiet",
                "--load-error-handling",
                "ignore",
                "--no-stop-slow-scripts",
                "--javascript-delay",
                "2000",
                "--page-size",
                "A4",
                "--encoding",
                "utf-8",
                url,
                out,
            ],
            capture_output=True,
            timeout=60,
        )
        if Path(out).exists() and Path(out).stat().st_size > 500:
            data = Path(out).read_bytes()
            log.info("page_to_pdf (wkhtmltopdf): %dKB", len(data) // 1024)
            return data
    except Exception as e:
        log.error("page_to_pdf wkhtmltopdf: %s", e)
    finally:
        if out is not None:
            Path(out).unlink(missing_ok=True)

    log.warning("page_to_pdf: all strategies failed, returning raw HTML")
    return fetch_page(url)


# ─── Scholar ──────────────────────────────────────────────────────────────────
def scholar_search(query: str, page=0) -> list[dict]:
    log.info("scholar_search: %r page=%d", query, page)
    start = page * 10
    try:
        r = WEB.get(
            "https://scholar.google.com/scholar",
            params={"q": query, "hl": "en", "num": 10, "start": start},
            headers={"User-Agent": UA_DESK, "Accept-Language": "en-US,en;q=0.9"},
            timeout=20,
        )
        log.debug("scholar: status=%d len=%d", r.status_code, len(r.text))
        soup = BeautifulSoup(r.text, "html.parser")
        results = []
        for div in soup.select(".gs_ri"):
            ta = div.select_one(".gs_rt a") or div.select_one("h3 a")
            if not ta:
                continue
            sn = div.select_one(".gs_rs")
            meta = div.select_one(".gs_a")
            results.append(
                {
                    "title": ta.get_text(strip=True),
                    "link": ta.get("href", ""),
                    "snippet": sn.get_text(strip=True) if sn else "",
                    "meta": meta.get_text(strip=True) if meta else "",
                }
            )
        log.info("scholar_search: %d results", len(results))
        return results
    except Exception as e:
        log.error("scholar_search: %s", e, exc_info=True)
        return []


# ─── Wikipedia ────────────────────────────────────────────────────────────────
def wikipedia_search(query: str, lang="fa") -> list[dict]:
    log.info("wikipedia_search: %r lang=%s", query, lang)
    HDR = {"User-Agent": "BaleBot/1.0"}
    for base in [f"https://{lang}.wikipedia.org", f"https://{lang}.m.wikipedia.org"]:
        try:
            r = WEB.get(
                f"{base}/w/api.php",
                params={
                    "action": "opensearch",
                    "search": query,
                    "limit": 8,
                    "namespace": 0,
                    "format": "json",
                },
                headers=HDR,
                timeout=15,
            )
            log.debug("wiki opensearch: status=%d", r.status_code)
            if r.status_code == 200:
                d = r.json()
                titles = d[1] if len(d) > 1 else []
                descs = d[2] if len(d) > 2 else []
                urls = d[3] if len(d) > 3 else []
                results = [
                    {
                        "title": t,
                        "snippet": descs[i] if i < len(descs) else "",
                        "url": urls[i] if i < len(urls) else "",
                        "key": t.replace(" ", "_"),
                        "lang": lang,
                    }
                    for i, t in enumerate(titles)
                ]
                if results:
                    log.info("wiki_search: %d results", len(results))
                    return results
        except Exception as e:
            log.error("wikipedia_search %s: %s", base, e)
    return []


def wikipedia_article(title: str, lang="fa") -> Optional[str]:
    log.info("wikipedia_article: %r lang=%s", title, lang)
    HDR = {"User-Agent": "BaleBot/1.0"}
    key = urllib.parse.quote(title.replace(" ", "_"))
    for base in [f"https://{lang}.wikipedia.org", f"https://{lang}.m.wikipedia.org"]:
        try:
            r = WEB.get(
                f"{base}/api/rest_v1/page/summary/{key}", headers=HDR, timeout=15
            )
            log.debug("wiki summary: status=%d", r.status_code)
            if r.status_code == 200:
                extract = r.json().get("extract", "")
                if extract and len(extract) > 80:
                    # Try full text
                    try:
                        r2 = WEB.get(
                            f"{base}/w/api.php",
                            params={
                                "action": "query",
                                "titles": title,
                                "prop": "extracts",
                                "explaintext": 1,
                                "exsectionformat": "plain",
                                "format": "json",
                                "utf8": 1,
                            },
                            headers=HDR,
                            timeout=20,
                        )
                        log.debug("wiki full: status=%d", r2.status_code)
                        if r2.status_code == 200:
                            for pg in (
                                r2.json().get("query", {}).get("pages", {}).values()
                            ):
                                full = pg.get("extract", "")
                                if full and len(full) > len(extract):
                                    return full
                    except Exception:
                        pass
                    return extract
        except Exception as e:
            log.error("wikipedia_article %s: %s", base, e)
    return None


# ─── YouTube ──────────────────────────────────────────────────────────────────
PLAYLIST_MAX_ITEMS = 10


def _is_youtube_playlist(url: str) -> bool:
    return "youtube.com" in url and "list=" in url


def youtube_playlist_items(url: str, max_items: int = PLAYLIST_MAX_ITEMS) -> list[dict]:
    """List a bounded set of playlist videos; actual downloads stay per-video."""
    if not _is_youtube_playlist(url):
        return []
    try:
        result = subprocess.run(
            [
                YTDLP_BIN,
                "--flat-playlist",
                "--playlist-items",
                f"1-{max(1, min(max_items, PLAYLIST_MAX_ITEMS))}",
                "--print",
                "%(id)s|||%(title)s|||%(uploader)s|||%(duration_string)s",
                "--no-warnings",
                "--no-check-certificate",
                url,
            ],
            capture_output=True,
            timeout=30,
        )
        output = result.stdout.decode(errors="replace") if isinstance(result.stdout, bytes) else result.stdout
        items = []
        for line in output.splitlines():
            parts = line.split("|||")
            if not parts or not parts[0].strip():
                continue
            video_id = parts[0].strip()
            items.append({
                "id": video_id,
                "title": parts[1].strip() if len(parts) > 1 else "ویدیو",
                "uploader": parts[2].strip() if len(parts) > 2 else "",
                "duration": parts[3].strip() if len(parts) > 3 else "",
                "url": f"https://www.youtube.com/watch?v={video_id}",
            })
        return items
    except Exception as e:
        log.warning("youtube_playlist_items: %s", e)
        return []


def youtube_search(query: str, max_results=8) -> list[dict]:
    log.info("youtube_search: %r", query)
    try:
        result = subprocess.run(
            [
                YTDLP_BIN,
                "--flat-playlist",
                "--print",
                "%(id)s|||%(title)s|||%(uploader)s|||%(duration_string)s|||%(thumbnail)s|||%(view_count)s|||%(like_count)s|||%(description)s",
                f"ytsearch{max_results}:{query}",
                "--no-warnings",
                "--no-check-certificate",
                "--extractor-args",
                "youtube:lang=en",
                "--geo-bypass-country",
                "US",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        items = []
        for line in result.stdout.strip().split("\n"):
            parts = line.split("|||")
            if len(parts) >= 2 and parts[0].strip():
                vid_id = parts[0].strip()
                items.append(
                    {
                        "id": vid_id,
                        "title": parts[1].strip() if len(parts) > 1 else "",
                        "uploader": parts[2].strip() if len(parts) > 2 else "",
                        "duration": parts[3].strip() if len(parts) > 3 else "",
                        "thumbnail": parts[4].strip()
                        if len(parts) > 4 and parts[4].strip() not in ("NA", "")
                        else f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg",
                        "view_count": parts[5].strip() if len(parts) > 5 else "",
                        "like_count": parts[6].strip() if len(parts) > 6 else "",
                        "description": parts[7].strip()[:300] if len(parts) > 7 else "",
                        "url": f"https://www.youtube.com/watch?v={vid_id}",
                    }
                )
        log.info("youtube_search: %d results", len(items))
        return items
    except Exception as e:
        log.error("youtube_search: %s", e, exc_info=True)
        return []


def _yt_probe_format(
    url: str, client: str, audio_only: bool, cookies_file: str, max_height: int = 720
) -> Optional[str]:
    """
    Run yt-dlp -j to get available formats for this URL+client, then pick
    the best matching format_id directly — like the GH Actions workflow does.
    Returns a format_id string, or None if probe failed.

    This avoids "Requested format not available" errors that happen when you
    blindly specify format selectors without knowing what streams exist.
    Videos that only have DASH streams (no pre-muxed mp4) will always fail
    with b[ext=mp4] — probing first lets us fall back to DASH+mux gracefully.
    """
    probe_cmd = [
        YTDLP_BIN,
        "-j",
        "--no-playlist",
        "--no-warnings",
        "--no-check-certificate",
        "--socket-timeout",
        "20",
        "--extractor-args",
        f"youtube:player_client={client}",
        "--remote-components",
        "ejs:github",
        "--js-runtimes",
        "node",
        "--geo-bypass-country",
        "US",
    ]
    # Fast discovery must not inherit problematic cookie/proxy state.
    probe_cmd.append(url)

    try:
        proc = subprocess.run(probe_cmd, capture_output=True, timeout=12)
        if proc.returncode != 0:
            log.debug(
                "_yt_probe_format: client=%s rc=%d stderr=%s",
                client,
                proc.returncode,
                proc.stderr.decode(errors="replace")[:200],
            )
            return None
        info = json.loads(proc.stdout.decode(errors="replace"))
        formats = info.get("formats", [])
        if not formats:
            return None

        if audio_only:
            # Best audio-only stream by bitrate (like GH: ba[format_note*=original]/ba)
            audio_fmts = [
                f
                for f in formats
                if f.get("vcodec") == "none"
                and f.get("acodec") != "none"
                and f.get("acodec") not in (None, "none")
            ]
            if not audio_fmts:
                # fallback: any format with audio
                audio_fmts = [
                    f for f in formats if f.get("acodec") not in (None, "none")
                ]
            if not audio_fmts:
                return None
            # Prefer "original" quality flag (GH uses format_note*=original), else highest abr
            original = [
                f
                for f in audio_fmts
                if "original" in (f.get("format_note") or "").lower()
            ]
            pool = original if original else audio_fmts
            best = max(pool, key=lambda f: f.get("abr") or f.get("tbr") or 0)
            log.debug(
                "_yt_probe_format: audio fmt=%s abr=%s client=%s",
                best["format_id"],
                best.get("abr"),
                client,
            )
            return best["format_id"]
        else:
            # Combined streams (vcodec+acodec both present) — best height ≤ max_height
            combined = [
                f
                for f in formats
                if f.get("vcodec") not in (None, "none")
                and f.get("acodec") not in (None, "none")
                and (f.get("height") or 0) <= max_height
            ]
            if combined:
                # Pick by height desc, then fps desc (GH: max_by(.height, .fps))
                best = max(
                    combined,
                    key=lambda f: ((f.get("height") or 0), (f.get("fps") or 0)),
                )
                log.debug(
                    "_yt_probe_format: combined fmt=%s h=%s fps=%s client=%s",
                    best["format_id"],
                    best.get("height"),
                    best.get("fps"),
                    client,
                )
                return best["format_id"]

            # No combined — build video+audio merge spec (DASH)
            video_fmts = [
                f
                for f in formats
                if f.get("vcodec") not in (None, "none")
                and f.get("acodec") in (None, "none")
                and (f.get("height") or 0) <= max_height
            ]
            audio_fmts = [
                f
                for f in formats
                if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")
            ]
            if video_fmts and audio_fmts:
                best_v = max(
                    video_fmts,
                    key=lambda f: ((f.get("height") or 0), (f.get("fps") or 0)),
                )
                # Prefer original audio quality flag (GH: ba[format_note*=original]/ba)
                orig_a = [
                    f
                    for f in audio_fmts
                    if "original" in (f.get("format_note") or "").lower()
                ]
                best_a = max(
                    orig_a or audio_fmts,
                    key=lambda f: f.get("abr") or f.get("tbr") or 0,
                )
                fmt_spec = f"{best_v['format_id']}+{best_a['format_id']}"
                log.debug("_yt_probe_format: DASH merge=%s client=%s", fmt_spec, client)
                return fmt_spec

            return None
    except Exception as e:
        log.debug("_yt_probe_format: client=%s error=%s", client, e)
        return None


def youtube_get_formats(url: str) -> dict:
    """
    Probe a YouTube URL and return available video qualities and subtitle tracks.

    Returns:
      {
        "formats": [{"label": "1080p 60fps", "height": 1080, "fps": 60,
                     "fmt_spec": "137+251", "client": "tv_embedded"}, …],
        "subtitles": [{"code": "en", "name": "English"}, …],
        "title": "Video Title",
        "thumbnail": "https://…",
        "duration": "10:30",
      }
    """
    log.info("youtube_get_formats: %s", url)
    cached = _youtube_info_cache.get(url)
    if cached and time.monotonic() - cached[0] < 120:
        return cached[1]
    import shutil

    cookies_file = (
        YOUTUBE_COOKIES_FILE
        if (YOUTUBE_COOKIES_FILE and Path(YOUTUBE_COOKIES_FILE).exists())
        else ""
    )

    # yt-dlp defaults track current supported clients; do not force auth-only tv_embedded.
    # Fast direct metadata avoids cookie/proxy stalls; recovery stays in download fallbacks.
    probe_clients = ["android_vr", "web_safari"]
    info = None
    used_client = None

    for client in probe_clients:
        probe_cmd = [
            YTDLP_BIN,
            "-j",
            "--no-playlist",
            "--no-warnings",
            "--no-check-certificate",
            "--socket-timeout",
            "20",
            "--extractor-args",
            f"youtube:player_client={client}",
            "--remote-components",
            "ejs:github",
            "--js-runtimes",
            "node",
            "--geo-bypass-country",
            "US",
        ]
        probe_cmd.append(url)
        try:
            proc = subprocess.run(probe_cmd, capture_output=True, timeout=12)
            if proc.returncode != 0:
                log.debug(
                    "youtube_get_formats: client=%s rc=%d stderr=%s",
                    client,
                    proc.returncode,
                    proc.stderr.decode(errors="replace")[:200],
                )
                continue
            if proc.stdout.strip():
                info = json.loads(proc.stdout.decode(errors="replace"))
                used_client = client
                log.debug("youtube_get_formats: got info via client=%s", client)
                break
        except Exception as e:
            log.debug("youtube_get_formats: client=%s error=%s", client, e)
            continue

    if not info:
        log.warning("youtube_get_formats: all clients failed")
        _youtube_info_cache[url] = (time.monotonic(), {})
        return {}

    formats_raw = info.get("formats", [])

    # ── Build quality list ────────────────────────────────────────────────────
    # Group by height — pick best (highest bitrate) video+audio pair per height
    seen_heights: dict[int, dict] = {}

    video_fmts = [
        f
        for f in formats_raw
        if f.get("vcodec") not in (None, "none") and (f.get("height") or 0) > 0
    ]
    audio_fmts = [
        f
        for f in formats_raw
        if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")
    ]

    # Best audio stream (prefer "original" flag)
    orig_a = [
        f for f in audio_fmts if "original" in (f.get("format_note") or "").lower()
    ]
    best_audio = max(
        orig_a or audio_fmts,
        key=lambda f: f.get("abr") or f.get("tbr") or 0,
        default=None,
    )

    for vf in video_fmts:
        h = vf.get("height", 0)
        fps = vf.get("fps") or 0
        # combined (pre-muxed) preferred, else DASH+best_audio
        has_audio = vf.get("acodec") not in (None, "none")
        if has_audio:
            fmt_spec = vf["format_id"]
        elif best_audio:
            fmt_spec = f"{vf['format_id']}+{best_audio['format_id']}"
        else:
            continue  # no audio available — skip

        tbr = vf.get("tbr") or vf.get("vbr") or 0
        key = h
        existing = seen_heights.get(key)
        if existing is None or tbr > existing["_tbr"]:
            fps_label = f" {int(fps)}fps" if fps and fps > 30 else ""
            seen_heights[key] = {
                "label": f"{h}p{fps_label}",
                "height": h,
                "fps": fps,
                "fmt_spec": fmt_spec,
                "client": used_client,
                "_tbr": tbr,
                "tbr_estimate": tbr,  # Store for filesize calculation
            }

    quality_list = sorted(
        seen_heights.values(), key=lambda x: x["height"], reverse=True
    )
    # Remove internal _tbr key and add filesize info
    for q in quality_list:
        q.pop("_tbr", None)
        # Estimate filesize based on duration and bitrate (tbr in kbps)
        tbr = q.get("tbr_estimate") or 0
        duration_sec = info.get("duration") or 0
        if tbr and duration_sec:
            # filesize in MB = (tbr_kbps * duration_sec) / 8 / 1024
            estimated_mb = int((tbr * duration_sec) / 8 / 1024)
            q["filesize_mb"] = estimated_mb

    # ── Subtitles ─────────────────────────────────────────────────────────────
    subs_raw = {}
    subs_raw.update(info.get("subtitles", {}))
    subs_raw.update(info.get("automatic_captions", {}))  # auto-generated too

    LANG_NAMES = {
        "en": "English",
        "fa": "فارسی",
        "ar": "العربية",
        "de": "Deutsch",
        "fr": "Français",
        "es": "Español",
        "ru": "Русский",
        "zh": "中文",
        "ja": "日本語",
        "ko": "한국어",
        "tr": "Türkçe",
        "it": "Italiano",
        "pt": "Português",
        "nl": "Nederlands",
        "pl": "Polski",
        "hi": "हिन्दी",
        "ur": "اردو",
    }
    subtitle_list = []
    for code in sorted(subs_raw.keys()):
        base = code.split("-")[0]
        name = LANG_NAMES.get(base) or LANG_NAMES.get(code) or code.upper()
        is_auto = code in info.get("automatic_captions", {})
        subtitle_list.append(
            {
                "code": code,
                "name": f"{name} {'(auto)' if is_auto else ''}".strip(),
            }
        )

    result = {
        "formats": quality_list,
        "subtitles": subtitle_list[:20],  # cap at 20 languages
        "title": info.get("title", ""),
        "description": info.get("description", ""),
        "thumbnail": info.get("thumbnail", ""),
        "duration": info.get("duration_string", ""),
        "client": used_client,
    }
    _youtube_info_cache[url] = (time.monotonic(), result)
    return result


def _clip_range(start: str, end: str) -> Optional[tuple[int, int]]:
    def seconds(value: str) -> Optional[int]:
        try:
            parts = [int(part) for part in value.strip().split(":")]
            if not 1 <= len(parts) <= 3 or any(part < 0 for part in parts):
                return None
            total = 0
            for part in parts:
                total = total * 60 + part
            return total
        except ValueError:
            return None
    left, right = seconds(start), seconds(end)
    if left is None or right is None or not left < right <= left + 1800:
        return None
    return left, right


def youtube_download(
    url: str,
    audio_only=False,
    fmt_spec: str = "",
    sub_code: str = "",
    yt_client: str = "",
    clip: Optional[tuple[int, int]] = None,
    max_height: int = 720,
) -> Optional[tuple[bytes, str]]:
    """Download YouTube video/audio.

    Strategy (yt-dlp first, Cobalt as final fallback):
      1. Probe-then-download: run yt-dlp -j to inspect real available formats,
         then download the exact format_id. Tried across 5 clients in order.
         Inspired by the GH Actions workflow approach that avoids blind format
         selectors which fail when only DASH streams are available.
      2. Safety-net: broad format selector with --format-sort.
      3. Cobalt API (self-hosted) — last resort fallback.

    fmt_spec: pre-chosen format spec from youtube_get_formats (skips probe).
    sub_code: subtitle language code to embed (e.g. "en", "fa").
    yt_client: player client to use when fmt_spec is given.
    """
    _ensure_disk_space()
    log.info(
        "youtube_download: url=%r audio=%s fmt=%r sub=%r",
        url,
        audio_only,
        fmt_spec,
        sub_code,
    )
    import shutil

    ffmpeg_dir = str(Path(shutil.which("ffmpeg") or "/usr/bin/ffmpeg").parent)
    cookies_file = (
        YOUTUBE_COOKIES_FILE
        if (YOUTUBE_COOKIES_FILE and Path(YOUTUBE_COOKIES_FILE).exists())
        else ""
    )

    if cookies_file:
        log.info("youtube_download: using cookies from %s", cookies_file)
        gate_cmd = [YTDLP_BIN, "-j", "--no-playlist", "--no-warnings", "--socket-timeout", "10", "--cookies", cookies_file]
        if WARP_PROXY:
            gate_cmd += ["--proxy", WARP_PROXY]
        gate_cmd.append(url)
        try:
            gate = subprocess.run(gate_cmd, capture_output=True, timeout=12)
            blocked = "Sign in to confirm" in gate.stderr.decode(errors="replace")
            if gate.returncode == 0:
                try:
                    formats = json.loads(gate.stdout.decode(errors="replace")).get("formats", [])
                    blocked = not any(f.get("vcodec") not in (None, "none") or f.get("acodec") not in (None, "none") for f in formats)
                except (ValueError, TypeError):
                    blocked = True
            if blocked:
                log.warning("youtube_download: cookie session has no usable streams; skipping retries")
                return None
        except subprocess.TimeoutExpired:
            log.warning("youtube_download: cookie session check timed out")
            return None
    else:
        log.warning("youtube_download: no cookies file — bot-detection likely")

    def _subtitle_args(code: str) -> list:
        """Build yt-dlp args to embed a specific subtitle track."""
        if not code:
            return []
        return [
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs",
            code,
            "--sub-format",
            "srt",
            "--embed-subs",
            "--convert-subs",
            "srt",
        ]

    def _run(client: str, fmt_spec: str, use_recovery: bool = False) -> Optional[tuple[bytes, str]]:
        """Download selected format; use proxy/cookies only on recovery attempts."""
        with tempfile.TemporaryDirectory() as tmp:
            cmd = [
                YTDLP_BIN,
                "--no-playlist",
                "--no-warnings",
                "--no-check-certificate",
                "--ffmpeg-location",
                ffmpeg_dir,
                "--socket-timeout",
                "30",
                "--retries",
                "2",
                "--extractor-args",
                f"youtube:player_client={client}",
                "--remote-components",
                "ejs:github",
                "--js-runtimes",
                "node",
                "-f",
                fmt_spec,
                "--merge-output-format",
                "mp4",
                # Remove --max-filesize to allow full video downloads
                # File size limits are handled by smart_send() chunking
                "-o",
                os.path.join(tmp, "%(title)s.%(ext)s"),
            ]
            if use_recovery and cookies_file:
                cmd += ["--cookies", cookies_file]
            if use_recovery and WARP_PROXY:
                cmd += ["--proxy", WARP_PROXY]
            if sub_code:
                cmd += _subtitle_args(sub_code)
            if clip:
                cmd += ["--download-sections", f"*{clip[0]}-{clip[1]}"]
            # SponsorBlock: remove sponsor segments
            cmd += ["--sponsorblock-remove", "all"]
            cmd.append(url)
            log.debug("yt-dlp cmd: %s", " ".join(cmd))
            proc = subprocess.run(cmd, capture_output=True, timeout=300)
            if proc.returncode != 0:
                log.error(
                    "yt-dlp rc=%d stderr=%s",
                    proc.returncode,
                    proc.stderr.decode(errors="replace")[:300],
                )
                return None
            files = [
                f
                for f in Path(tmp).glob("*")
                if f.stat().st_size > 100 and not f.suffix == ".srt"
            ]
            if not files:
                return None
            f = sorted(files, key=lambda x: x.stat().st_size, reverse=True)[0]
            data = f.read_bytes()
            log.info("yt-dlp OK: %s  %.1fMB", f.name, len(data) / 1024 / 1024)
            return data, f.name

    # ── Fast path: caller already probed and chose a format ──────────────────
    if fmt_spec:
        client = yt_client or "tv_embedded"
        if audio_only:
            with tempfile.TemporaryDirectory() as tmp:
                cmd = [
                    YTDLP_BIN,
                    "--no-playlist",
                    "--no-warnings",
                    "--no-check-certificate",
                    "--ffmpeg-location",
                    ffmpeg_dir,
                    "--socket-timeout",
                    "30",
                    "--retries",
                    "2",
                    "--extractor-args",
                    f"youtube:player_client={client}",
                    "--remote-components",
                    "ejs:github",
                    "--js-runtimes",
                    "node",
                    "-f",
                    fmt_spec,
                    "-x",
                    "--audio-format",
                    "mp3",
                    "--audio-quality",
                    "0",
                    "-o",
                    os.path.join(tmp, "%(title)s.%(ext)s"),
                ]
                if cookies_file:
                    cmd += ["--cookies", cookies_file]
                if WARP_PROXY:
                    cmd += ["--proxy", WARP_PROXY]
                # SponsorBlock: remove sponsor segments (for audio too, marks chapters)
                cmd += ["--sponsorblock-remove", "all"]
                cmd.append(url)
                proc = subprocess.run(cmd, capture_output=True, timeout=300)
                if proc.returncode == 0:
                    files = [f for f in Path(tmp).glob("*") if f.stat().st_size > 1000]
                    if files:
                        f = files[0]
                        return f.read_bytes(), f.name
        else:
            result = _run(client, fmt_spec)
            if result:
                return result
        log.warning("youtube_download: fast-path failed, falling through to full probe")

    # ── Strategy 1: Probe-then-download across 5 clients ─────────────────────
    # Client order follows GH Actions workflow:
    #   default  — yt-dlp's own best-effort selection
    #   android  — pre-muxed mp4, reliable for many videos
    #   ios      — pre-muxed mp4, good fallback
    #   tv_embedded — bypasses embed restrictions
    #   web_music,web,tv — combined multi-client probe (GH fallback 4 approach)
    probe_clients = [
        "tv_embedded",
        "android",
        "ios",
        "default",
        "web_music,web,tv,web_embedded",
    ]

    for client in probe_clients:
        log.info("youtube_download: probing client=%s", client)
        fmt_spec = _yt_probe_format(
            url, client, audio_only, cookies_file, max_height=max_height
        )
        if not fmt_spec:
            log.debug("youtube_download: no format found for client=%s", client)
            continue
        log.info("youtube_download: trying fmt=%s client=%s", fmt_spec, client)

        if audio_only:
            # For audio, use -x to extract+convert to mp3
            with tempfile.TemporaryDirectory() as tmp:
                cmd = [
                    YTDLP_BIN,
                    "--no-playlist",
                    "--no-warnings",
                    "--no-check-certificate",
                    "--ffmpeg-location",
                    ffmpeg_dir,
                    "--socket-timeout",
                    "30",
                    "--retries",
                    "2",
                    "--extractor-args",
                    f"youtube:player_client={client}",
                    "--remote-components",
                    "ejs:github",
                    "--js-runtimes",
                    "node",
                    "-f",
                    fmt_spec,
                    "-x",
                    "--audio-format",
                    "mp3",
                    "--audio-quality",
                    "0",
                    "-o",
                    os.path.join(tmp, "%(title)s.%(ext)s"),
                ]
                if cookies_file:
                    cmd += ["--cookies", cookies_file]
                if WARP_PROXY:
                    cmd += ["--proxy", WARP_PROXY]
                # SponsorBlock: remove sponsor segments
                cmd += ["--sponsorblock-remove", "all"]
                cmd.append(url)
                log.debug("yt-dlp audio cmd: %s", " ".join(cmd))
                proc = subprocess.run(cmd, capture_output=True, timeout=300)
                if proc.returncode == 0:
                    files = [f for f in Path(tmp).glob("*") if f.stat().st_size > 1000]
                    if files:
                        f = files[0]
                        data = f.read_bytes()
                        log.info(
                            "yt-dlp audio OK: %s  %.1fMB",
                            f.name,
                            len(data) / 1024 / 1024,
                        )
                        return data, f.name
                log.error(
                    "yt-dlp audio rc=%d: %s",
                    proc.returncode,
                    proc.stderr.decode(errors="replace")[:200],
                )
        else:
            result = _run(client, fmt_spec)
            if result:
                return result

    # ── Strategy 2: Safety-net broad selector (no probe) ─────────────────────
    # If all probes failed (e.g. network hiccup during -j), try a very permissive
    # selector that lets yt-dlp decide entirely with --format-sort.
    log.warning("youtube_download: all probes failed, trying broad safety-net")
    for client in ("android", "ios", "tv_embedded"):
        log.info("youtube_download: safety-net trying client=%s", client)
        with tempfile.TemporaryDirectory() as tmp:
            cmd = [
                YTDLP_BIN,
                "--no-playlist",
                "--no-warnings",
                "--no-check-certificate",
                "--ffmpeg-location",
                ffmpeg_dir,
                "--socket-timeout",
                "30",
                "--retries",
                "2",
                "--extractor-args",
                f"youtube:player_client={client}",
                "--remote-components",
                "ejs:github",
                "--js-runtimes",
                "node",
                "-S",
                "height:720,ext:mp4:m4a",
                "--merge-output-format",
                "mp4",
                "-o",
                os.path.join(tmp, "%(title)s.%(ext)s"),
            ]
            if cookies_file:
                cmd += ["--cookies", cookies_file]
            if WARP_PROXY:
                cmd += ["--proxy", WARP_PROXY]
            if audio_only:
                cmd += ["-x", "--audio-format", "mp3"]
            if sub_code:
                cmd += _subtitle_args(sub_code)
            cmd.append(url)
            log.debug("yt-dlp safety-net cmd: %s", " ".join(cmd))
            proc = subprocess.run(cmd, capture_output=True, timeout=300)
            if proc.returncode == 0:
                files = [
                    f
                    for f in Path(tmp).glob("*")
                    if f.stat().st_size > 100 and f.suffix != ".srt"
                ]
                if files:
                    f = sorted(files, key=lambda x: x.stat().st_size, reverse=True)[0]
                    data = f.read_bytes()
                    log.info(
                        "yt-dlp safety-net OK: %s  %.1fMB",
                        f.name,
                        len(data) / 1024 / 1024,
                    )
                    return data, f.name
            else:
                log.error(
                    "safety-net rc=%d client=%s: %s",
                    proc.returncode,
                    client,
                    proc.stderr.decode(errors="replace")[:200],
                )

    # ── Strategy 3: Cobalt API fallback ───────────────────────────────────────
    log.warning("youtube_download: yt-dlp exhausted, trying Cobalt fallback")
    cobalt_result = _youtube_cobalt(url, audio_only=audio_only)
    if cobalt_result:
        return cobalt_result

    log.error("youtube_download: all strategies exhausted for %s", url)
    return None


def _cobalt_download(
    url: str, audio_only: bool = False, quality: str = "720"
) -> Optional[tuple[bytes, str]]:
    """
    Download single item via Cobalt API.
    For carousels/multiple items use _cobalt_download_all().
    """
    results = _cobalt_download_all(url, audio_only=audio_only, quality=quality)
    if results:
        return results[0]["data"], results[0]["fname"]
    return None


def _cobalt_download_all(
    url: str, audio_only: bool = False, quality: str = "720"
) -> list[dict]:
    """
    Download ALL items via Cobalt API (handles carousels, playlists, etc).
    Returns list of {data, fname, is_video} dicts.
    Set COBALT_URL env var to your self-hosted instance.

    For TikTok and other platforms, ensures audio is included in video downloads.
    """
    if not COBALT_URL:
        log.debug("_cobalt_download_all: COBALT_URL not set, skipping")
        return []
    log.info("_cobalt_download_all: %s audio=%s via %s", url, audio_only, COBALT_URL)
    try:
        # Cobalt API v2 format - see https://github.com/imputnet/cobalt
        # Fixed payload format to avoid "error.api.invalid_body" errors
        payload: dict = {
            "url": url.strip(),  # Ensure no trailing whitespace
            "videoQuality": quality,
            "filenameStyle": "basic",
        }
        if audio_only:
            payload["downloadMode"] = "audio"
            payload["audioFormat"] = "mp3"
            payload["audioBitrate"] = "128"
        else:
            payload["downloadMode"] = "auto"
            # For TikTok and other platforms: ensure full video with audio is downloaded
            # Don't set isAudioOnly or dubLang to avoid audio stripping
            payload["youtubeVideoCodec"] = "h264"  # prefer compatible codec

        # Add TikTok-specific settings to ensure audio is included
        if "tiktok.com" in url:
            payload["tiktokFullAudio"] = True
            payload["tiktokH265"] = False  # Prefer H264 for better compatibility

        resp = WEB.post(
            COBALT_URL,   # Cobalt v10 API is POST / (was POST /json → 404)
            json=payload,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=30,
        )
        if resp.status_code not in (200, 201):
            log.warning(
                "_cobalt_download_all: status=%d body=%s",
                resp.status_code,
                resp.text[:200],
            )
            return []

        j = resp.json()
        status = j.get("status", "")
        log.debug("_cobalt_download_all: status=%s", status)

        if status == "error":
            code = j.get("error", {}).get("code", "unknown")
            log.warning("_cobalt_download_all: API error code=%s", code)
            # Handle age-restricted content errors
            if code == "error.api.content.post.age":
                log.info("_cobalt_download_all: age-restricted content, will try yt-dlp with cookies")
            return []

        def _sniff_type(
            raw: bytes, item_url: str, content_type_hint: str = ""
        ) -> tuple[str, bool]:
            """Return (ext, is_video) by inspecting magic bytes and content-type."""
            ct = content_type_hint.lower()
            # Magic bytes detection (most reliable)
            if raw[:4] == b"\xff\xd8\xff\xe0" or raw[:4] == b"\xff\xd8\xff\xe1":
                return "jpg", False  # JPEG
            if raw[:8] == b"\x89PNG\r\n\x1a\n":
                return "png", False  # PNG
            if raw[:4] in (b"RIFF",) and raw[8:12] == b"WEBP":
                return "webp", False  # WEBP
            if raw[:4] == b"GIF8":
                return "gif", False  # GIF
            # MP4/MOV: ftyp box or moov atom near start
            if raw[4:8] in (b"ftyp", b"moov", b"mdat", b"pnot", b"wide", b"free"):
                return "mp4", True
            if raw[:3] == b"ID3" or raw[:2] == b"\xff\xfb":
                return "mp3", False  # Audio
            # Content-type fallback
            if "image/jpeg" in ct:
                return "jpg", False
            if "image/png" in ct:
                return "png", False
            if "image/webp" in ct:
                return "webp", False
            if "image/gif" in ct:
                return "gif", False
            if "video/" in ct:
                return "mp4", True
            if "audio/" in ct:
                return "mp3", False
            # URL extension fallback
            url_lower = item_url.lower().split("?")[0]
            for ext, is_vid in [
                (".jpg", "jpg"),
                (".jpeg", "jpg"),
                (".png", "png"),
                (".webp", "webp"),
                (".gif", "gif"),
                (".mp4", "mp4"),
                (".mov", "mp4"),
                (".mp3", "mp3"),
            ]:
                if url_lower.endswith(ext):
                    return is_vid, ext == ".mp4" or ext == ".mov"
            # Default: assume video if large, image if small
            is_vid = len(raw) > 2_000_000
            return ("mp4" if is_vid else "jpg"), is_vid

        def _fetch_item(
            item_url: str, is_video_hint: bool = True, ext_hint: str = ""
        ) -> Optional[dict]:
            if not item_url:
                return None
            try:
                r = WEB.get(item_url, timeout=60, stream=False)
                if not r.ok or len(r.content) < 500:
                    return None
                raw = r.content
                ct = r.headers.get("content-type", "")
                ext, is_video = _sniff_type(raw, item_url, ct)
                if audio_only:
                    is_video = False
                domain_m = re.search(r"(?:https?://)?(?:www\.)?([^/]+)", url)
                prefix = domain_m.group(1).split(".")[0] if domain_m else "media"
                fname = f"{prefix}_media.{ext}"
                log.info(
                    "cobalt item OK: %.1fMB %s is_video=%s",
                    len(raw) / 1024 / 1024,
                    fname,
                    is_video,
                )
                return {"data": raw, "fname": fname, "is_video": is_video}
            except Exception as e:
                log.warning("_fetch_item: %s", e)
                return None

        # Single direct URL
        if j.get("url") and status in ("tunnel", "redirect", "stream"):
            result = _fetch_item(j["url"], is_video_hint=not audio_only)
            return [result] if result else []

        # Picker = carousel / multiple items
        if j.get("picker"):
            results = []
            for item in j["picker"][:20]:  # max 20 slides
                item_url = item.get("url", "")
                item_type = item.get("type", "")
                is_video_hint = item_type == "video"
                r = _fetch_item(item_url, is_video_hint=is_video_hint)
                if r:
                    results.append(r)
            log.info(
                "cobalt picker: %d/%d items fetched", len(results), len(j["picker"])
            )
            return results

        log.warning(
            "_cobalt_download_all: unhandled status=%s j=%s", status, str(j)[:200]
        )
        return []

    except requests.exceptions.ConnectionError:
        log.warning("_cobalt_download_all: cannot connect to %s", COBALT_URL)
        return []
    except Exception as e:
        log.error("_cobalt_download_all: %s", e)
        return []


def _youtube_cobalt(url: str, audio_only: bool = False) -> Optional[tuple[bytes, str]]:
    """YouTube download via Cobalt API (self-hosted)."""
    return _cobalt_download(url, audio_only=audio_only, quality="720")


def _trim_video(
    src: Path, tmp: str, ffmpeg_dir="/usr/bin"
) -> Optional[tuple[bytes, Path]]:
    out = Path(tmp) / ("trimmed_" + src.name)
    ffprobe = str(Path(ffmpeg_dir) / "ffprobe")
    ffmpeg = str(Path(ffmpeg_dir) / "ffmpeg")
    try:
        probe = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(src),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        duration = float(probe.stdout.strip() or "300")
        vbr = max(300, int((48 * 1024 * 1024 * 8) / duration / 1000) - 128)
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-i",
                str(src),
                "-c:v",
                "libx264",
                "-b:v",
                f"{vbr}k",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-movflags",
                "+faststart",
                str(out),
            ],
            capture_output=True,
            timeout=300,
            check=True,
        )
        data = out.read_bytes()
        return data, out
    except Exception as e:
        log.error("_trim_video: %s", e)
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# MUSIC PLATFORMS  (YouTube · YouTube Music · SoundCloud · JioSaavn · Deezer · Spotify)
# ═══════════════════════════════════════════════════════════════════════════════


def _ffmpeg_dir() -> str:
    import shutil

    return str(Path(shutil.which("ffmpeg") or "/usr/bin/ffmpeg").parent)


# ── YouTube / SoundCloud search via yt-dlp ───────────────────────────────────
def _music_duration_seconds(value: object) -> int:
    """Parse display durations such as ``3:42`` safely."""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    try:
        parts = [int(part) for part in str(value).strip().split(":")]
    except ValueError:
        return 0
    if not parts or len(parts) > 3:
        return 0
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds


def music_pick_duration_match(items: list[dict], target_duration: object) -> list[dict]:
    """Order search hits by closeness to a known catalog duration."""
    target = _music_duration_seconds(target_duration)
    if target <= 0:
        return items
    return sorted(items, key=lambda item: (
        _music_duration_seconds(item.get("duration")) <= 0,
        abs(_music_duration_seconds(item.get("duration")) - target),
    ))


def music_search_ytdlp(
    query: str, max_results: int = 5, source: str = "youtube"
) -> list[dict]:
    """Search for tracks via yt-dlp (YouTube, YouTube Music, or SoundCloud)."""
    log.info("music_search_ytdlp: %r source=%s", query, source)
    if source == "soundcloud":
        search_url = f"scsearch{max_results}:{query}"
    elif source == "ytmusic":
        search_url = f"https://music.youtube.com/search?q={urllib.parse.quote(query)}"
    else:
        search_url = f"ytsearch{max_results}:{query}"

    extra = []
    if (
        source in ("youtube", "ytmusic")
        and YOUTUBE_COOKIES_FILE
        and Path(YOUTUBE_COOKIES_FILE).exists()
    ):
        extra = ["--cookies", YOUTUBE_COOKIES_FILE]

    # For SoundCloud: use webpage_url (the actual SC permalink) not url (raw API URL)
    # For YouTube: webpage_url and url are the same watch?v= link
    url_field = "%(webpage_url)s" if source == "soundcloud" else "%(url)s"

    try:
        if source == "ytmusic":
            cmd = (
                [
                    YTDLP_BIN,
                    "--flat-playlist",
                    "--no-warnings",
                    "--no-check-certificate",
                    "--ffmpeg-location",
                    _ffmpeg_dir(),
                    "--playlist-items",
                    f"1-{max_results}",
                ]
                + extra
                + [
                    "--print",
                    f"%(id)s|||%(title)s|||%(uploader)s|||%(duration_string)s|||%(webpage_url)s|||%(thumbnail)s",
                    f"https://music.youtube.com/search?q={urllib.parse.quote(query + ' audio')}",
                ]
            )
        else:
            cmd = (
                [
                    YTDLP_BIN,
                    "--flat-playlist",
                    "--no-warnings",
                    "--no-check-certificate",
                    "--ffmpeg-location",
                    _ffmpeg_dir(),
                ]
                + extra
                + [
                    "--print",
                    f"%(id)s|||%(title)s|||%(uploader)s|||%(duration_string)s|||{url_field}|||%(thumbnail)s",
                    search_url,
                ]
            )

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        items = []
        for line in result.stdout.strip().split("\n"):
            parts = line.split("|||")
            if len(parts) < 2 or not parts[0].strip():
                continue
            vid_id = parts[0].strip()
            title = parts[1].strip()
            uploader = parts[2].strip() if len(parts) > 2 else ""
            duration = parts[3].strip() if len(parts) > 3 else ""
            url = parts[4].strip() if len(parts) > 4 else ""
            thumb = parts[5].strip() if len(parts) > 5 else ""
            # Fix NA/None values
            if not url or url in ("NA", "none", "None"):
                if source in ("youtube", "ytmusic") and vid_id:
                    url = f"https://www.youtube.com/watch?v={vid_id}"
                elif source == "soundcloud" and vid_id:
                    # vid_id for SC is numeric; webpage_url should have been set but fallback
                    url = ""  # don't guess — bad SC API URLs cause 404s
                else:
                    url = ""
            if not thumb or thumb in ("NA", "none", "None"):
                if source in ("youtube", "ytmusic") and vid_id:
                    thumb = f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg"
                else:
                    thumb = ""
            if not url or not title:
                continue
            # Reject raw SoundCloud API URLs (api.soundcloud.com/tracks/...) — they 404 on re-download
            if source == "soundcloud" and "api.soundcloud.com" in url:
                log.debug("SC search: skipping raw API URL %s", url)
                continue
            display_source = "ytmusic" if source == "ytmusic" else source
            items.append(
                {
                    "id": vid_id,
                    "title": title,
                    "uploader": uploader,
                    "duration": duration,
                    "url": url,
                    "thumbnail": thumb,
                    "source": display_source,
                }
            )
            if len(items) >= max_results:
                break
        log.info("music_search_ytdlp (%s): %d results", source, len(items))
        return items
    except Exception as e:
        log.error("music_search_ytdlp (%s): %s", source, e)
        return []


# ── JioSaavn search (no API key required) ────────────────────────────────────
def jiosaavn_search(query: str, max_results: int = 5) -> list[dict]:
    """Search JioSaavn using their public API — no key needed."""
    log.info("jiosaavn_search: %r", query)
    try:
        # JioSaavn public search endpoint
        url = "https://www.jiosaavn.com/api.php"
        params = {
            "__call": "search.getResults",
            "_format": "json",
            "_marker": "0",
            "api_version": "4",
            "ctx": "web6dot0",
            "query": query,
            "n": str(max_results),
            "p": "1",
        }
        r = WEB.get(
            url,
            params=params,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://www.jiosaavn.com/",
            },
            timeout=15,
        )
        if r.status_code != 200:
            log.error("jiosaavn_search: HTTP %d", r.status_code)
            return []
        data = r.json()
        songs = data.get("results", [])
        items = []
        for s in songs[:max_results]:
            title = s.get("title", s.get("song", ""))
            uploader = s.get("primary_artists", s.get("singers", ""))
            duration = s.get("duration", "")
            song_id = s.get("id", "")
            # Build perma URL
            perma = s.get("perma_url", "")
            # Encrypted media URL — needed for download
            enc_url = s.get("media_preview_url", s.get("media_url", ""))
            thumb = s.get("image", "").replace("150x150", "500x500")
            if duration:
                try:
                    secs = int(duration)
                    duration = f"{secs // 60}:{secs % 60:02d}"
                except Exception:
                    pass
            if not (title and song_id):
                continue
            items.append(
                {
                    "id": song_id,
                    "title": title,
                    "uploader": uploader,
                    "duration": str(duration),
                    "url": perma or f"https://www.jiosaavn.com/song/-/{song_id}",
                    "thumbnail": thumb,
                    "source": "jiosaavn",
                    "enc_url": enc_url,
                    "perma_url": perma,
                }
            )
        log.info("jiosaavn_search: %d results", len(items))
        return items
    except Exception as e:
        log.error("jiosaavn_search: %s", e)
        return []


def jiosaavn_download(item: dict) -> Optional[tuple[bytes, str]]:
    """Download a JioSaavn track given a search result dict."""
    log.info("jiosaavn_download: %s", item.get("title", "?"))
    try:
        song_id = item.get("id", "")
        perma = item.get("perma_url", "")
        title = item.get("title", "track")
        uploader = item.get("uploader", "")

        # Approach 1: use yt-dlp which supports JioSaavn
        if perma:
            result = music_download_ytdlp(perma, source="jiosaavn")
            if result:
                return result

        # Approach 2: fetch song details API to get stream URL
        if song_id:
            detail_url = "https://www.jiosaavn.com/api.php"
            params = {
                "__call": "song.getDetails",
                "_format": "json",
                "_marker": "0",
                "api_version": "4",
                "ctx": "web6dot0",
                "pids": song_id,
            }
            r = WEB.get(
                detail_url,
                params=params,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://www.jiosaavn.com/",
                },
                timeout=15,
            )
            if r.status_code == 200:
                d = r.json()
                song_data = d.get(song_id, {})
                enc = song_data.get("encrypted_media_url", "")
                if enc:
                    # Decrypt the media URL
                    stream_url = _jiosaavn_decrypt(enc)
                    if stream_url:
                        resp = WEB.get(stream_url, timeout=60, stream=True)
                        if resp.status_code == 200:
                            data = resp.content
                            safe = re.sub(r"[^\w\s-]", "", title)[:50]
                            fname = f"{safe}.mp3"
                            log.info(
                                "jiosaavn_download: OK %.1fMB", len(data) / 1024 / 1024
                            )
                            return data, fname

        log.error("jiosaavn_download: all methods failed for %s", title)
        return None
    except Exception as e:
        log.error("jiosaavn_download: %s", e)
        return None


def _jiosaavn_decrypt(enc_url: str) -> Optional[str]:
    """Decrypt JioSaavn encrypted media URL using their known DES key.

    Note: This uses a hardcoded key for known JioSaavn API compatibility.
    If the API changes, decryption will fail gracefully.
    """
    if not enc_url or not isinstance(enc_url, str):
        log.debug("jiosaavn_decrypt: invalid input")
        return None
    try:
        from base64 import b64decode

        # JioSaavn uses DES ECB with a known key
        try:
            from Crypto.Cipher import DES
        except ImportError:
            log.warning(
                "jiosaavn_decrypt: pycryptodome not installed, skipping decrypt"
            )
            return None
        key = b"38346591"
        try:
            enc_bytes = b64decode(enc_url)
        except Exception as e:
            log.debug("jiosaavn_decrypt: invalid base64: %s", e)
            return None
        if not enc_bytes or len(enc_bytes) < 8:
            log.debug("jiosaavn_decrypt: decrypted data too short")
            return None
        try:
            cipher = DES.new(key, DES.MODE_ECB)
            decrypted = cipher.decrypt(enc_bytes)
        except Exception as e:
            log.debug("jiosaavn_decrypt: DES decryption failed: %s", e)
            return None
        # Remove padding
        url = decrypted.decode("utf-8", errors="ignore").rstrip(
            "\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\x0c\r\x0e\x0f\x10"
        )
        if not url or not url.startswith("http"):
            log.debug("jiosaavn_decrypt: invalid decrypted URL format")
            return None
        # Upgrade to 320kbps
        url = url.replace("_96.mp4", "_320.mp4").replace("_160.mp4", "_320.mp4")
        log.debug("jiosaavn_decrypt: OK %s", url[:60])
        return url
    except Exception as e:
        log.error("jiosaavn_decrypt: %s", e)
        return None


# ── Deezer search (public API, no key) ───────────────────────────────────────
def deezer_search(query: str, max_results: int = 5) -> list[dict]:
    """Search Deezer using their public API — no API key required."""
    log.info("deezer_search: %r", query)
    try:
        url = "https://api.deezer.com/search"
        params = {"q": query, "limit": max_results, "output": "json"}
        r = WEB.get(
            url, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=15
        )
        if r.status_code != 200:
            log.error("deezer_search: HTTP %d", r.status_code)
            return []
        data = r.json()
        items = []
        for track in data.get("data", [])[:max_results]:
            title = track.get("title", "")
            uploader = track.get("artist", {}).get("name", "")
            duration = track.get("duration", 0)
            track_id = track.get("id", "")
            link = track.get("link", f"https://www.deezer.com/track/{track_id}")
            thumb = track.get("album", {}).get(
                "cover_big", track.get("album", {}).get("cover_medium", "")
            )
            dur_str = f"{duration // 60}:{duration % 60:02d}" if duration else ""
            if not (title and track_id):
                continue
            items.append(
                {
                    "id": str(track_id),
                    "title": title,
                    "uploader": uploader,
                    "duration": dur_str,
                    "url": link,
                    "thumbnail": thumb,
                    "source": "deezer",
                }
            )
        log.info("deezer_search: %d results", len(items))
        return items
    except Exception as e:
        log.error("deezer_search: %s", e)
        return []


def deezer_download(item: dict) -> Optional[tuple[bytes, str]]:
    """
    Download a Deezer track.
    Deezer URLs are DRM-protected — yt-dlp will never work.
    We go straight to searching YouTube Music → YouTube → SoundCloud by title+artist.
    """
    title = item.get("title", "")
    artist = item.get("uploader", "")
    log.info("deezer_download: %s — %s", artist, title)

    if not title:
        return None

    search_q = f"{artist} - {title}".strip(" -") if artist else title

    # 1. YouTube Music
    hits = music_search_ytdlp(search_q, max_results=2, source="ytmusic")
    if not hits:
        hits = music_search_ytdlp(search_q, max_results=2, source="youtube")
    for hit in hits:
        result = music_download_ytdlp(hit["url"], source="youtube")
        if result:
            log.info("deezer_download: found via YouTube for %r", search_q)
            return result

    # 2. SoundCloud
    sc_hits = music_search_ytdlp(search_q, max_results=2, source="soundcloud")
    for hit in sc_hits:
        result = music_download_ytdlp(hit["url"], source="soundcloud")
        if result:
            log.info("deezer_download: found via SoundCloud for %r", search_q)
            return result

    log.error("deezer_download: all methods failed for %r", search_q)
    return None


# ── Unified multi-source search ───────────────────────────────────────────────
def music_search_multi(query: str) -> list[dict]:
    """Search YouTube, YouTube Music, SoundCloud, JioSaavn, and Deezer simultaneously."""
    import threading as _t

    results: dict[str, list] = {}

    def _s(name, fn, *args):
        try:
            results[name] = fn(*args)
        except Exception as e:
            log.error("music_search_multi [%s]: %s", name, e)
            results[name] = []

    threads = [
        _t.Thread(target=_s, args=("youtube", music_search_ytdlp, query, 4, "youtube")),
        _t.Thread(target=_s, args=("ytmusic", music_search_ytdlp, query, 4, "ytmusic")),
        _t.Thread(
            target=_s, args=("soundcloud", music_search_ytdlp, query, 3, "soundcloud")
        ),
        _t.Thread(target=_s, args=("jiosaavn", jiosaavn_search, query, 4)),
        _t.Thread(target=_s, args=("deezer", deezer_search, query, 4)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=25)

    # Interleave: round-robin across sources so user sees variety
    sources_order = ["ytmusic", "youtube", "jiosaavn", "deezer", "soundcloud"]
    lists = [results.get(s, []) for s in sources_order]
    merged = []
    seen_titles = set()
    i = 0
    while len(merged) < 20:
        added = False
        for lst in lists:
            if i < len(lst):
                item = lst[i]
                dedup_key = item.get("title", "").lower()[:30]
                if dedup_key not in seen_titles:
                    seen_titles.add(dedup_key)
                    merged.append(item)
                added = True
        if not added:
            break
        i += 1
    log.info("music_search_multi: %d total results", len(merged))
    return merged[:20]


def music_download_ytdlp(url: str, source: str = "auto", audio_format: str = "mp3", audio_quality: str = "0") -> Optional[tuple[bytes, str]]:
    """Download audio via yt-dlp with specified format/quality. Returns None on DRM or permanent failure."""
    _ensure_disk_space()
    log.info("music_download_ytdlp: %s source=%s fmt=%s q=%s", url, source, audio_format, audio_quality)
    import shutil

    ffmpeg_dir = str(Path(shutil.which("ffmpeg") or "/usr/bin/ffmpeg").parent)
    is_yt = "youtube.com" in url or "youtu.be" in url
    is_sc = "soundcloud.com" in url
    is_ytm = "music.youtube.com" in url

    # Deezer is DRM-protected — never attempt, saves ~4×5s of wasted retries
    if "deezer.com" in url:
        log.warning("music_download_ytdlp: skipping DRM-protected Deezer URL")
        return None

    # Reject raw SoundCloud API URLs (not downloadable via yt-dlp)
    if "api.soundcloud.com" in url and "soundcloud%3A" in url:
        log.warning("music_download_ytdlp: skipping raw SC API URL %s", url)
        return None

    timeout = 120 if is_sc else 300  # SC tracks are small, fail fast

    def _run_audio(extra_args: list, use_cookies: bool = False) -> Optional[tuple[bytes, str]]:
        with tempfile.TemporaryDirectory() as tmp:
            base = [
                YTDLP_BIN,
                "--no-playlist",
                "--no-warnings",
                "--no-check-certificate",
                "--ffmpeg-location",
                ffmpeg_dir,
                "--socket-timeout",
                "20",
                "--retries",
                "2",
                "-o",
                os.path.join(tmp, "%(title).60s.%(ext)s"),
            ]
            if is_yt or is_ytm:
                if use_cookies and YOUTUBE_COOKIES_FILE and Path(YOUTUBE_COOKIES_FILE).exists():
                    base += ["--cookies", YOUTUBE_COOKIES_FILE]
                base += [
                    "--extractor-args",
                    "youtube:player_client=mweb,default",
                    "--js-runtimes",
                    "node",
                ]
            elif is_sc:
                base += ["--extractor-args", "soundcloud:client=web"]
            if WARP_PROXY:
                base += ["--proxy", WARP_PROXY]
            # SponsorBlock: remove sponsor segments (for audio too, marks chapters)
            base += ["--sponsorblock-remove", "all"]
            cmd = base + extra_args + [url]
            log.debug("music dl: %s", " ".join(cmd))
            try:
                proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                log.error("music dl: timed out after %ds", timeout)
                return None
            if proc.returncode != 0:
                stderr = proc.stderr.decode(errors="replace")
                # Early-exit on permanent errors — no point retrying with different flags
                if "DRM" in stderr or "is not supported" in stderr:
                    log.error("music dl: DRM/unsupported: %s", stderr[:200])
                    raise RuntimeError("DRM")
                log.error("music dl rc=%d: %s", proc.returncode, stderr[:300])
                return None
            files = [
                f
                for f in Path(tmp).glob("*")
                if f.suffix.lower()
                in {".mp3", ".m4a", ".ogg", ".opus", ".flac", ".webm"}
                and f.stat().st_size > 1000
            ]
            if not files:
                return None
            best = max(files, key=lambda f: f.stat().st_size)
            data = best.read_bytes()
            log.info("music dl OK: %s  %.1fMB", best.name, len(data) / 1024 / 1024)
            return data, best.name

    # Format strategy based on selected format/quality
    # audio_quality: 0=best, 192=192k, 256=256k, 320=320k
    q = audio_quality if audio_quality != "0" else "0"

    if audio_format == "best":
        # Best available without conversion
        format_attempts = [
            ["-f", "bestaudio/best"],
            ["-x"],
        ]
    else:
        format_attempts = [
            [
                "-f",
                "bestaudio/best",
                "-x",
                "--audio-format",
                audio_format,
                "--audio-quality",
                q,
                "--embed-thumbnail",
                "--add-metadata",
            ],
            ["-f", "bestaudio/best", "-x", "--audio-format", audio_format, "--audio-quality", q],
            ["-f", "bestaudio/best", "-x"],
            ["-x"],
        ]

    try:
        # For YouTube/YouTube Music: try WITHOUT cookies first (cookies often break format selection)
        # Then try WITH cookies if that fails
        if is_yt or is_ytm:
            for use_cookies in (False, True):
                for args in format_attempts:
                    result = _run_audio(args, use_cookies=use_cookies)
                    if result:
                        return result
        else:
            for args in format_attempts:
                result = _run_audio(args)
                if result:
                    return result
    except RuntimeError as e:
        if "DRM" in str(e):
            return None
        raise

    return None


def spotify_download(url: str) -> Optional[tuple[bytes, str]]:
    """Download Spotify track/album/playlist via spotdl → MP3."""
    _ensure_disk_space()
    log.info("spotify_download: %s", url)
    import shutil
    import sys

    ffmpeg_path = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
    # spotdl may be installed as a module even if the binary isn't on PATH
    spotdl_path = (
        shutil.which("spotdl")
        or os.path.join(os.path.dirname(sys.executable), "spotdl")
        or os.path.expanduser("~/.local/bin/spotdl")
    )
    # Verify it actually exists before trying
    if not os.path.isfile(spotdl_path or ""):
        spotdl_path = "spotdl"  # last resort — let subprocess resolve it
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [
            spotdl_path,
            "download",
            url,
            "--format",
            "mp3",
            "--bitrate",
            "192k",
            "--output",
            tmp,
            "--ffmpeg",
            ffmpeg_path,
            "--log-level",
            "ERROR",
        ]
        if SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
            cmd += [
                "--client-id",
                SPOTIFY_CLIENT_ID,
                "--client-secret",
                SPOTIFY_CLIENT_SECRET,
            ]
        log.debug("spotdl: %s", " ".join(cmd))
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=300)
            if proc.returncode != 0:
                log.error(
                    "spotdl rc=%d: %s",
                    proc.returncode,
                    proc.stderr.decode(errors="replace")[:400],
                )
                return None
            files = sorted(Path(tmp).glob("*.mp3"), key=lambda f: f.stat().st_size)
            if not files:
                return None
            if len(files) == 1:
                return files[0].read_bytes(), files[0].name
            # Multiple files (album/playlist) → zip
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in files:
                    zf.writestr(f.name, f.read_bytes())
            buf.seek(0)
            album_name = url.split("/")[-1][:40] or "album"
            return buf.read(), f"{album_name}.zip"
        except subprocess.TimeoutExpired:
            log.error("spotdl: timed out")
            return None
        except Exception as e:
            log.error("spotdl: %s", e)
            return None


def soundcloud_download(url: str) -> Optional[tuple[bytes, str]]:
    """Download SoundCloud track via yt-dlp."""
    return music_download_ytdlp(url, source="soundcloud")


def music_download_with_fallback(
    query_or_url: str,
    title: str = "",
    artist: str = "",
    source_hint: str = "auto",
    status_cb=None,
    _tried_yt_ids: set = None,  # internal: YT video IDs already attempted
    duration: object = None,
) -> Optional[tuple[bytes, str]]:
    """
    Full fallback chain: Spotify → YouTube Music → SoundCloud → JioSaavn → Deezer(→YT/SC)
    Tracks already-tried YouTube IDs so the chain never retries the same video twice.
    """
    if _tried_yt_ids is None:
        _tried_yt_ids = set()
    tried_cobalt_ids: set = set()

    def _cb(msg: str):
        if status_cb:
            try:
                status_cb(msg)
            except Exception:
                pass

    is_url = query_or_url.startswith("http")

    # Cleanest possible search query
    if artist and title:
        search_q = f"{artist} - {title}"
    elif title:
        search_q = title
    elif not is_url:
        search_q = query_or_url
    else:
        search_q = ""

    # ── Step 1: Spotify ───────────────────────────────────────────────────────
    if is_url and "spotify.com" in query_or_url:
        _cb("🟢 در حال دانلود از Spotify…")
        result = spotify_download(query_or_url)
        if result:
            log.info("fallback: Spotify OK")
            return result
        log.warning("fallback: Spotify failed, trying YouTube Music")
        _cb("⚠️ Spotify ناموفق — در حال جستجو در یوتیوب موزیک…")

    # ── Step 2: YouTube Music + YouTube ──────────────────────────────────────
    # Skip if the original call was already a direct YT URL (tried in _do_audio_download)
    skip_yt_search = is_url and (
        "youtube.com" in query_or_url or "youtu.be" in query_or_url
    )

    if not skip_yt_search and search_q:
        _cb("🎵 در حال جستجو در یوتیوب موزیک…")
        yt_hits = music_search_ytdlp(search_q, max_results=5, source="ytmusic")
        if not yt_hits:
            yt_hits = music_search_ytdlp(search_q, max_results=5, source="youtube")
        for hit in music_pick_duration_match(yt_hits, duration):
            vid_id = hit.get("id", "") or hit.get("url", "")
            if vid_id in _tried_yt_ids:
                continue
            _tried_yt_ids.add(vid_id)
            result = music_download_ytdlp(hit["url"], source="youtube")
            if result:
                log.info("fallback: YouTube Music search OK")
                return result

    # ── Step 2.5: Cobalt API for YouTube/YouTube Music (bypasses format issues) ──
    if COBALT_URL and search_q:
        _cb("🔷 در حال تلاش با Cobalt (YouTube/YouTube Music)…")
        yt_hits = music_search_ytdlp(search_q, max_results=5, source="ytmusic")
        if not yt_hits:
            yt_hits = music_search_ytdlp(search_q, max_results=5, source="youtube")
        for hit in music_pick_duration_match(yt_hits, duration):
            vid_id = hit.get("id", "") or hit.get("url", "")
            if vid_id in tried_cobalt_ids:
                continue
            tried_cobalt_ids.add(vid_id)
            result = _youtube_cobalt(hit["url"], audio_only=True)
            if result:
                log.info("fallback: Cobalt YouTube/YouTube Music OK")
                return result
    log.warning("fallback: YouTube failed, trying SoundCloud")
    _cb("⚠️ یوتیوب ناموفق — در حال جستجو در SoundCloud…")

    # ── Step 3: SoundCloud ────────────────────────────────────────────────────
    if is_url and "soundcloud.com" in query_or_url:
        _cb("☁️ در حال دانلود از SoundCloud…")
        result = soundcloud_download(query_or_url)
        if result:
            log.info("fallback: SoundCloud direct OK")
            return result
    elif search_q:
        _cb("☁️ در حال جستجو در SoundCloud…")
        sc_hits = music_search_ytdlp(search_q, max_results=3, source="soundcloud")
        for hit in sc_hits:
            result = soundcloud_download(hit["url"])
            if result:
                log.info("fallback: SoundCloud search OK")
                return result
    log.warning("fallback: SoundCloud failed, trying Cobalt (YouTube/YouTube Music)")
    _cb("⚠️ SoundCloud ناموفق — در حال تلاش با Cobalt (YouTube/YouTube Music)…")

    # ── Step 3.5: Cobalt API (for YouTube/YouTube Music audio) ──────────────────
    if COBALT_URL and search_q:
        _cb("🔷 در حال تلاش با Cobalt (YouTube/YouTube Music)…")
        yt_hits = music_search_ytdlp(search_q, max_results=5, source="ytmusic")
        if not yt_hits:
            yt_hits = music_search_ytdlp(search_q, max_results=5, source="youtube")
        for hit in music_pick_duration_match(yt_hits, duration):
            vid_id = hit.get("id", "") or hit.get("url", "")
            if vid_id in tried_cobalt_ids:
                continue
            tried_cobalt_ids.add(vid_id)
            result = _youtube_cobalt(hit["url"], audio_only=True)
            if result:
                log.info("fallback: Cobalt YouTube/YouTube Music OK")
                return result
    log.warning("fallback: Cobalt failed, trying JioSaavn")
    _cb("⚠️ Cobalt ناموفق — در حال جستجو در JioSaavn…")

    # ── Step 4: JioSaavn ─────────────────────────────────────────────────────
    if search_q:
        _cb("🎶 در حال جستجو در JioSaavn…")
        saavn_hits = jiosaavn_search(search_q, max_results=2)
        for hit in saavn_hits:
            result = jiosaavn_download(hit)
            if result:
                log.info("fallback: JioSaavn OK")
                return result
    log.warning("fallback: JioSaavn failed, trying Deezer")
    _cb("⚠️ JioSaavn ناموفق — در حال جستجو در Deezer…")

    # ── Step 5: Deezer (metadata search → YT/SC download, no DRM) ────────────
    # Note: deezer_download() itself searches YouTube and SoundCloud internally.
    # We pass the already-tried IDs so it doesn't repeat failed videos.
    if search_q:
        _cb("🎧 در حال جستجو در Deezer…")
        dz_hits = deezer_search(search_q, max_results=2)
        for hit in dz_hits:
            # deezer_download searches YT/SC — pass tried IDs to avoid repeats
            result = _deezer_download_dedup(hit, _tried_yt_ids)
            if result:
                log.info("fallback: Deezer metadata OK")
                return result

    log.error("fallback: ALL sources failed for %r", search_q or query_or_url)
    return None


def _deezer_download_dedup(item: dict, tried_ids: set) -> Optional[tuple[bytes, str]]:
    """Like deezer_download() but skips YouTube IDs already attempted."""
    title = item.get("title", "")
    artist = item.get("uploader", "")
    if not title:
        return None
    search_q = f"{artist} - {title}".strip(" -") if artist else title

    for src in ("ytmusic", "youtube"):
        hits = music_search_ytdlp(search_q, max_results=3, source=src)
        for hit in hits:
            vid_id = hit.get("id", "") or hit.get("url", "")
            if vid_id in tried_ids:
                continue
            tried_ids.add(vid_id)
            result = music_download_ytdlp(hit["url"], source="youtube")
            if result:
                return result

    sc_hits = music_search_ytdlp(search_q, max_results=2, source="soundcloud")
    for hit in sc_hits:
        result = music_download_ytdlp(hit["url"], source="soundcloud")
        if result:
            return result

    return None


# ─── Generic yt-dlp social downloader ────────────────────────────────────────
def social_download_ytdlp(
    url: str, audio_only=False, cookies_file: str = "", format_id=None
) -> Optional[tuple[bytes, str]]:
    """Download from any yt-dlp-supported URL (TikTok, Instagram, Twitter, etc.)
    Falls back to Cobalt API if yt-dlp fails.
    """
    _ensure_disk_space()
    log.info("social_download_ytdlp: %r  audio=%s fmt=%s", url, audio_only, format_id)
    import shutil

    ffmpeg_dir = str(Path(shutil.which("ffmpeg") or "/usr/bin/ffmpeg").parent)
    base = [
        YTDLP_BIN,
        "--no-playlist",
        "--no-warnings",
        "--no-check-certificate",
        "--ffmpeg-location",
        ffmpeg_dir,
        "--socket-timeout",
        "30",
        "--retries",
        "3",
    ]
    if format_id:
        base += ["-f", format_id]
    if cookies_file and Path(cookies_file).exists():
        base += ["--cookies", cookies_file]
    if WARP_PROXY:
        base += ["--proxy", WARP_PROXY]
    # SponsorBlock: remove sponsor segments
    base += ["--sponsorblock-remove", "all"]

    def _run(extra, target):
        with tempfile.TemporaryDirectory() as tmp:
            cmd = (
                base
                + ["-o", os.path.join(tmp, "%(title).60s.%(ext)s")]
                + extra
                + [target]
            )
            log.debug("social yt-dlp: %s", " ".join(cmd))
            proc = subprocess.run(cmd, capture_output=True, timeout=300)
            if proc.returncode != 0:
                log.warning(
                    "social yt-dlp rc=%d: %s",
                    proc.returncode,
                    proc.stderr.decode(errors="replace")[:500],
                )
                return None
            files = [f for f in Path(tmp).glob("*") if f.stat().st_size > 100]
            if not files:
                return None
            f = sorted(files, key=lambda x: x.stat().st_size, reverse=True)[0]
            data = f.read_bytes()
            log.info("social yt-dlp OK: %s  %.1fMB", f.name, len(data) / 1024 / 1024)
            return data, f.name

    if audio_only:
        result = _run(["-x", "--audio-format", "mp3", "--audio-quality", "5"], url)
    else:
        result = None
        # For TikTok, ensure we get video WITH audio. TikTok's extractor can
        # expose a video-only "bestvideo" stream for longer clips; merging
        # that with "bestaudio" sometimes yields a technically-successful but
        # silent file. Try single combined-stream formats first (these are
        # TikTok's own muxed CDN files and always carry audio), then fall
        # back to explicit video+audio merge, verifying each attempt with
        # ffprobe before accepting it.
        is_tiktok = "tiktok.com" in url.lower()
        attempts = (
            [
                ["-f", "best[ext=mp4]/best"],
                ["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4"],
                ["--merge-output-format", "mp4"],
            ]
            if is_tiktok
            else [
                ["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4"],
                ["-f", "best[ext=mp4]/best", "--embed-thumbnail"],
                ["--merge-output-format", "mp4"],
            ]
        )
        for fmt in attempts:
            candidate = _run(fmt, url)
            if not candidate:
                continue
            cand_data, cand_name = candidate
            if _has_audio_stream(cand_data, cand_name):
                result = candidate
                break
            log.warning(
                "social yt-dlp: %s produced a silent file, trying next format", fmt
            )
            result = result or candidate  # keep as last-resort fallback
        # If every attempt was silent, `result` holds the last one so we
        # still deliver *something* rather than nothing.

    if result:
        return result

    # Cobalt API fallback — handles TikTok, Twitter/X, Instagram, YouTube Shorts
    return _social_cobalt(url, audio_only)


def _social_cobalt(url: str, audio_only: bool = False) -> Optional[tuple[bytes, str]]:
    """Social media download via Cobalt API (TikTok, Twitter, Instagram, etc.)"""
    return _cobalt_download(url, audio_only=audio_only)


# ─── Telegram channel reader ─────────────────────────────────────────────────
def tg_channel_read_web(channel: str, limit: int = 20) -> list[dict]:
    """
    Read public Telegram channel via t.me/s/ preview (no auth needed).
    Extracts full text, image URLs, video info.
    """
    log.info("tg_channel_read_web: @%s limit=%d", channel, limit)
    channel = channel.lstrip("@").strip()
    try:
        r = WEB.get(
            f"https://t.me/s/{channel}",
            headers={"User-Agent": UA_DESK, "Accept-Language": "en-US,en;q=0.9"},
            timeout=20,
        )
        log.debug("tg_web: status=%d len=%d", r.status_code, len(r.text))
        if r.status_code != 200:
            log.error("tg_channel_read_web: status=%d", r.status_code)
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        messages = []
        for wrap in soup.select(".tgme_widget_message_wrap")[:limit]:
            msg_div = wrap.select_one(".tgme_widget_message")
            if not msg_div:
                continue
            msg_id = msg_div.get("data-post", "")

            # Full text (no truncation)
            text_el = wrap.select_one(".tgme_widget_message_text")
            text = text_el.get_text("\n", strip=True) if text_el else ""

            # Date
            time_el = wrap.select_one("time")
            date = time_el.get("datetime", "") if time_el else ""

            # Message link
            link_el = wrap.select_one("a.tgme_widget_message_date")
            msg_url = link_el.get("href", "") if link_el else ""

            # ── Extract image URLs ────────────────────────────────────────
            img_urls = []
            # Photos: background-image style on photo wrap
            for photo_el in wrap.select(".tgme_widget_message_photo_wrap"):
                style = photo_el.get("style", "")
                m = re.search(r"url\(['\"]?(https?://[^'\")\s]+)['\"]?\)", style)
                if m:
                    img_urls.append(m.group(1))
            # Also check <img> tags inside message
            for img_el in wrap.select("img.tgme_widget_message_photo"):
                src = img_el.get("src", "")
                if src and src not in img_urls:
                    img_urls.append(src)

            # ── Video info ────────────────────────────────────────────────
            has_video = bool(
                wrap.select(
                    ".tgme_widget_message_video_wrap, "
                    ".tgme_widget_message_video_player, "
                    "video"
                )
            )
            video_thumb = ""
            video_el = wrap.select_one(".tgme_widget_message_video_wrap")
            if video_el:
                style = video_el.get("style", "")
                m = re.search(r"url\(['\"]?(https?://[^'\")\s]+)['\"]?\)", style)
                if m:
                    video_thumb = m.group(1)

            has_doc = bool(wrap.select(".tgme_widget_message_document"))

            messages.append(
                {
                    "id": msg_id,
                    "text": text,
                    "date": date[:16].replace("T", " "),
                    "url": msg_url,
                    "img_urls": img_urls,
                    "has_photo": bool(img_urls),
                    "has_video": has_video,
                    "video_thumb": video_thumb,
                    "has_doc": has_doc,
                }
            )
        log.info("tg_channel_read_web: %d messages from @%s", len(messages), channel)
        return list(reversed(messages))
    except Exception as e:
        log.error("tg_channel_read_web: %s", e, exc_info=True)
        return []


async def tg_channel_read_mtproto(channel: str, limit: int = 20) -> list[dict]:
    """
    Read Telegram channel via MTProto (Telethon).
    Requires TG_API_ID and TG_API_HASH env vars.
    """
    log.info("tg_channel_read_mtproto: @%s", channel)
    if not TG_API_ID or not TG_API_HASH:
        log.error("TG_API_ID / TG_API_HASH not set")
        return []
    try:
        from telethon import TelegramClient
        from telethon.tl.types import (
            MessageMediaDocument,
            MessageMediaPhoto,
            MessageMediaWebPage,
        )

        client = TelegramClient(TG_SESSION, int(TG_API_ID), TG_API_HASH)
        await client.start()
        messages = []
        async for msg in client.iter_messages(channel, limit=limit):
            media = getattr(msg, "media", None)
            has_photo = isinstance(media, MessageMediaPhoto)
            has_doc = isinstance(media, MessageMediaDocument)
            has_video = has_doc and getattr(
                getattr(media, "document", None), "mime_type", ""
            ).startswith("video/")
            messages.append(
                {
                    "id": str(msg.id),
                    "text": (msg.text or ""),
                    "date": str(msg.date)[:16],
                    "url": f"https://t.me/{channel.lstrip('@')}/{msg.id}",
                    "img_urls": [],
                    "has_photo": has_photo,
                    "has_video": has_video,
                    "video_thumb": "",
                    "has_doc": has_doc and not has_video,
                    "_msg_obj": msg,  # kept for media download
                    "_client": None,  # filled by caller
                }
            )
        await client.disconnect()
        log.info("tg_mtproto: %d messages", len(messages))
        return list(reversed(messages))
    except Exception as e:
        log.error("tg_channel_read_mtproto: %s", e, exc_info=True)
        return []


def tg_download_media_web(msg_url: str) -> Optional[tuple[bytes, str]]:
    """
    Download media from a public Telegram message URL.
    Strategy 1: Telegram embed widget API (gets higher-res images)
    Strategy 2: t.me/s/ HTML scrape
    Strategy 3: yt-dlp for videos
    """
    log.info("tg_download_media: %s", msg_url)
    m = re.search(r"t\.me/(?:s/)?([^/]+)/(\d+)", msg_url)
    if not m:
        log.error("tg_download: cannot parse URL %s", msg_url)
        return None
    channel, msg_id = m.group(1), m.group(2)

    # Strategy 1: Telegram embed widget
    for embed_url in [
        f"https://t.me/{channel}/{msg_id}?embed=1&mode=tme",
        f"https://t.me/{channel}/{msg_id}?embed=1",
    ]:
        try:
            r = WEB.get(
                embed_url,
                headers={
                    "User-Agent": UA_DESK,
                    "Accept": "text/html",
                    "Referer": "https://t.me/",
                },
                timeout=20,
            )
            log.debug(
                "tg_embed: status=%d len=%d url=%s",
                r.status_code,
                len(r.text),
                embed_url,
            )
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")

            # ── Document / file attachment — CHECKED FIRST ─────────────────
            # Telegram CDN links appear as href on .tgme_widget_message_document
            # OR as data-url on the document wrap, OR directly in <a href>
            doc_url = ""
            doc_fname = f"tg_{channel}_{msg_id}.bin"
            for sel in [
                "a.tgme_widget_message_document",
                ".tgme_widget_message_document_wrap > a",
                ".tgme_widget_message_document_wrap a[href]",
                "a[href*='telesco.pe']",
                "a[href*='cdn.telegram.org']",
                # data-url variant
                "[data-url*='telesco.pe']",
                "[data-url*='cdn.telegram.org']",
            ]:
                el = soup.select_one(sel)
                if not el:
                    continue
                candidate = el.get("href") or el.get("data-url", "")
                if candidate and (
                    "telesco.pe" in candidate
                    or "cdn.telegram.org" in candidate
                    or candidate.startswith("http")
                ):
                    doc_url = candidate
                    # Try to get filename
                    title_el = el.select_one(
                        ".tgme_widget_message_document_title,"
                        " .document-name, [class*='title'], [class*='name']"
                    )
                    ext_el = el.select_one(
                        ".tgme_widget_message_document_extra,"
                        " .document-ext, [class*='ext']"
                    )
                    raw_name = title_el.get_text(strip=True) if title_el else ""
                    raw_ext = ext_el.get_text(strip=True).lower() if ext_el else ""
                    if raw_name:
                        if raw_ext and not raw_name.endswith(raw_ext):
                            raw_name = f"{raw_name}.{raw_ext}"
                        doc_fname = re.sub(r"[^\w.\-]", "_", raw_name)[:80]
                    break

            if doc_url:
                log.info(
                    "tg_embed: downloading document %s from %s", doc_fname, doc_url[:80]
                )
                doc_bytes = download_bytes(doc_url, 200 * 1024 * 1024)
                if doc_bytes and len(doc_bytes) > 10:
                    log.info(
                        "tg_embed: document OK %dKB %s",
                        len(doc_bytes) // 1024,
                        doc_fname,
                    )
                    return doc_bytes, doc_fname

            # ── Video ───────────────────────────────────────────────────────
            video_el = soup.select_one("video source[src], video[src]")
            if video_el:
                vurl = video_el.get("src", "")
                if vurl and vurl.startswith("http"):
                    vbytes = download_bytes(vurl, 200 * 1024 * 1024)
                    if vbytes and len(vbytes) > 1000:
                        fname = f"tg_{channel}_{msg_id}.mp4"
                        log.info("tg_embed: video %.1fMB", len(vbytes) / 1024 / 1024)
                        return vbytes, fname

            # ── Photo ───────────────────────────────────────────────────────
            img_urls = []
            for el in soup.select(
                ".tgme_widget_message_photo_wrap, .tgme_widget_message_photo"
            ):
                style = el.get("style", "")
                for m2 in re.finditer(
                    r"url\(['\"]?(https?://[^'\")\s]+\.(?:jpg|jpeg|png|webp))['\"]?\)",
                    style,
                    re.I,
                ):
                    img_urls.append(m2.group(1))
            for img in soup.select(".tgme_widget_message_photo img"):
                src = img.get("src", "")
                if src and src.startswith("http"):
                    img_urls.append(src)

            if img_urls:
                img_bytes = download_bytes(img_urls[0], MAX_IMAGE_SIZE)
                if img_bytes and len(img_bytes) > 5000:
                    ext = img_urls[0].rsplit(".", 1)[-1].split("?")[0].lower() or "jpg"
                    if ext not in {"jpg", "jpeg", "png", "webp"}:
                        ext = "jpg"
                    fname = f"tg_{channel}_{msg_id}.{ext}"
                    log.info("tg_embed: image %dKB", len(img_bytes) // 1024)
                    return img_bytes, fname

        except Exception as e:
            log.warning("tg_embed %s: %s", embed_url, e)

    # Strategy 2: t.me/s/ HTML scrape (before=msg_id+1 to get exact message)
    try:
        preview_url = f"https://t.me/s/{channel}?before={int(msg_id) + 1}"
        r2 = WEB.get(preview_url, headers={"User-Agent": UA_DESK}, timeout=20)
        log.debug("tg_scrape: status=%d len=%d", r2.status_code, len(r2.text))
        if r2.status_code == 200:
            soup2 = BeautifulSoup(r2.text, "html.parser")
            for wrap in soup2.select(".tgme_widget_message_wrap"):
                div = wrap.select_one(".tgme_widget_message")
                if not div:
                    continue
                post_id = div.get("data-post", "")
                if not post_id.endswith(f"/{msg_id}"):
                    continue
                # Images via background-image
                for photo_el in wrap.select(".tgme_widget_message_photo_wrap"):
                    style = photo_el.get("style", "")
                    mi = re.search(r"url\(['\"]?(https?://[^'\")\s]+)['\"]?\)", style)
                    if mi:
                        img_bytes = download_bytes(mi.group(1), MAX_IMAGE_SIZE)
                        if img_bytes and len(img_bytes) > 500:
                            ext = mi.group(1).rsplit(".", 1)[-1].split("?")[0] or "jpg"
                            fname = f"tg_{channel}_{msg_id}.{ext}"
                            log.info("tg_scrape: image %dKB", len(img_bytes) // 1024)
                            return img_bytes, fname
    except Exception as e:
        log.warning("tg_scrape: %s", e)

    # Strategy 3: yt-dlp for video content
    log.info("tg_download: trying yt-dlp for %s", msg_url)
    result = social_download_ytdlp(msg_url)
    if result:
        return result

    log.error("tg_download_media_web: all strategies failed for %s", msg_url)
    return None


# ─── Twitter / X ─────────────────────────────────────────────────────────────
# Public Nitter instances (Twitter frontend, no auth needed)
# Nitter instances — auto-tested on startup, sorted by availability
# Updated list from https://github.com/zedeus/nitter/wiki/Instances (May 2026)
NITTER_INSTANCES = [
    "https://nitter.cz",
    "https://nitter.poast.org",
    "https://twiiit.com",
    "https://nitter.net",
    "https://nitter.privacydev.net",
    "https://nitter.42l.fr",
]
_nitter_cache: list[str] = []
_nitter_cache_time: float = 0.0


def _get_working_nitter() -> list[str]:
    """Return list of currently working Nitter instances (cached for 30 min, parallel check)."""
    global _nitter_cache, _nitter_cache_time
    import concurrent.futures
    import time

    now = time.time()
    if _nitter_cache and (now - _nitter_cache_time) < 1800:
        return _nitter_cache

    def _check(inst: str) -> Optional[str]:
        try:
            r = WEB.get(
                f"{inst}/x",
                timeout=4,
                headers={"User-Agent": UA_MOB},
                allow_redirects=True,
            )
            if r.status_code in (200, 302) and len(r.text) > 500:
                return inst
        except Exception:
            pass
        return None

    working = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(_check, inst): inst for inst in NITTER_INSTANCES}
        for fut in concurrent.futures.as_completed(futures, timeout=8):
            result = fut.result()
            if result:
                working.append(result)
                if len(working) >= 2:
                    break

    if not working:
        log.warning("No working Nitter instances — using first two as fallback")
        working = NITTER_INSTANCES[:2]
    _nitter_cache = working
    _nitter_cache_time = now
    log.info("Working Nitter instances: %s", working)
    return working


def twitter_get_channel(username: str, limit: int = 20) -> list[dict]:
    """
    Read Twitter/X user timeline.
    Strategy 1: Nitter HTML scrape (working instances)
    Strategy 2: Twitter Syndication API
    Strategy 3: xcancel RSS
    """
    log.info("twitter_get_channel: @%s", username)
    username = username.lstrip("@").strip()

    # Strategy 1: Nitter working instances
    for instance in _get_working_nitter():
        url = f"{instance}/{username}"
        try:
            r = WEB.get(
                url,
                headers={"User-Agent": UA_MOB, "Accept-Language": "en-US,en;q=0.9"},
                timeout=15,
            )
            log.debug(
                "nitter %s: status=%d len=%d", instance, r.status_code, len(r.text)
            )
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            tweets = []
            for item in soup.select(".timeline-item")[:limit]:
                tweet_content = item.select_one(".tweet-content")
                if not tweet_content:
                    continue
                text = tweet_content.get_text("\n", strip=True)
                date_el = item.select_one(".tweet-date a")
                date = date_el.get("title", "") if date_el else ""
                tweet_path = date_el.get("href", "") if date_el else ""
                tweet_url = (
                    f"https://twitter.com{tweet_path}"
                    if tweet_path.startswith("/")
                    else tweet_path
                )
                nitter_url = f"{instance}{tweet_path}" if tweet_path else ""
                has_video = bool(item.select(".attachment.video-container, .gif"))
                img_tags = item.select(".still-image img, .attachment.image img")
                img_urls = [
                    f"{instance}{img.get('src', '')}"
                    if img.get("src", "").startswith("/")
                    else img.get("src", "")
                    for img in img_tags
                    if img.get("src")
                ]
                tweets.append(
                    {
                        "text": text,
                        "date": date[:19].replace("T", " "),
                        "url": tweet_url,
                        "nitter_url": nitter_url,
                        "has_video": has_video,
                        "has_photo": bool(img_urls),
                        "img_urls": img_urls,
                    }
                )
            if tweets:
                log.info("twitter: %d tweets via %s", len(tweets), instance)
                return tweets
        except Exception as e:
            log.warning("nitter %s: %s", instance, e)

    # Strategy 2: Twitter Syndication API (no auth, public timelines)
    try:
        r2 = WEB.get(
            f"https://syndication.twitter.com/srv/timeline-profile/screen-name/"
            f"{username}?showReplies=false",
            headers={"User-Agent": UA_DESK},
            timeout=15,
        )
        log.debug("twitter syndication: status=%d", r2.status_code)
        if r2.status_code == 200:
            m = re.search(
                r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r2.text, re.S
            )
            if m:
                jdata = json.loads(m.group(1))
                entries = (
                    jdata.get("props", {})
                    .get("pageProps", {})
                    .get("timeline", {})
                    .get("entries", [])
                )
                tweets = []
                for entry in entries[:limit]:
                    tw = entry.get("content", {}).get("tweet", {})
                    if not tw:
                        continue
                    tw_id = tw.get("id_str", "")
                    media = tw.get("extended_entities", {}).get("media", []) or []
                    img_urls = [
                        md.get("media_url_https", "")
                        for md in media
                        if md.get("type") == "photo"
                    ]
                    tweets.append(
                        {
                            "text": tw.get("full_text") or tw.get("text", ""),
                            "date": tw.get("created_at", "")[:19],
                            "url": f"https://twitter.com/{username}/status/{tw_id}",
                            "nitter_url": "",
                            "has_video": False,
                            "has_photo": bool(img_urls),
                            "img_urls": img_urls,
                        }
                    )
                if tweets:
                    log.info("twitter syndication: %d tweets", len(tweets))
                    return tweets
    except Exception as e:
        log.warning("twitter syndication: %s", e)

    # Strategy 3: xcancel RSS
    try:
        r3 = WEB.get(
            f"https://xcancel.com/{username}/rss",
            headers={"User-Agent": UA_DESK},
            timeout=15,
        )
        log.debug("xcancel rss: status=%d", r3.status_code)
        if r3.status_code == 200:
            soup = BeautifulSoup(r3.content, "xml")
            tweets = []
            for item in soup.select("item")[:limit]:
                title = (item.find("title") or {}).get_text(strip=True)
                link = (item.find("link") or {}).get_text(strip=True)
                desc = BeautifulSoup(
                    (item.find("description") or {}).get_text(strip=True), "html.parser"
                ).get_text(" ", strip=True)
                pubdate = (item.find("pubDate") or {}).get_text(strip=True)
                tweets.append(
                    {
                        "text": f"{title}\n{desc}".strip(),
                        "date": pubdate[:19],
                        "url": link,
                        "nitter_url": "",
                        "has_video": False,
                        "has_photo": False,
                        "img_urls": [],
                    }
                )
            if tweets:
                log.info("xcancel RSS: %d tweets", len(tweets))
                return tweets
    except Exception as e:
        log.warning("xcancel rss: %s", e)

    # Strategy 4: bird.makeup RSS (another reliable Nitter-compatible frontend)
    try:
        r4 = WEB.get(
            f"https://bird.makeup/users/{username}/feed.atom",
            headers={"User-Agent": UA_DESK},
            timeout=15,
        )
        log.debug("bird.makeup: status=%d", r4.status_code)
        if r4.status_code == 200:
            soup = BeautifulSoup(r4.content, "xml")
            tweets = []
            for entry in soup.select("entry")[:limit]:
                title = (entry.find("title") or {}).get_text(strip=True)
                link_el = entry.find("link")
                link = link_el.get("href", "") if link_el else ""
                content = BeautifulSoup(
                    (entry.find("content") or {}).get_text(strip=True), "html.parser"
                ).get_text(" ", strip=True)
                updated = (entry.find("updated") or {}).get_text(strip=True)
                tweets.append(
                    {
                        "text": f"{title}\n{content}".strip(),
                        "date": updated[:19],
                        "url": link,
                        "nitter_url": "",
                        "has_video": False,
                        "has_photo": False,
                        "img_urls": [],
                    }
                )
            if tweets:
                log.info("bird.makeup: %d tweets", len(tweets))
                return tweets
    except Exception as e:
        log.warning("bird.makeup: %s", e)

    log.error("twitter_get_channel: all strategies failed for @%s", username)
    return []


def twitter_download_media(tweet_url: str) -> Optional[list[tuple[bytes, str]]]:
    """
    Download ALL media (images + videos) from a tweet.
    Returns list of (bytes, filename) tuples, or None on total failure.
    """
    log.info("twitter_download_media: %s", tweet_url)
    tweet_url = tweet_url.replace("x.com/", "twitter.com/")
    m = re.search(r"twitter\.com/([^/]+)/status/(\d+)", tweet_url)
    username = m.group(1) if m else ""
    tweet_id = m.group(2) if m else ""

    # Strategy 0: Cobalt API (handles multi-image picker)
    cobalt_items = _cobalt_download_all(tweet_url, audio_only=False)
    if cobalt_items:
        results = [(it["data"], it["fname"]) for it in cobalt_items if it.get("data")]
        if results:
            log.info("twitter_download_media: cobalt %d items", len(results))
            return results

    # Strategy 1: vxtwitter / fxtwitter — collect ALL media in one pass
    if username and tweet_id:
        for api_url in [
            f"https://api.vxtwitter.com/{username}/status/{tweet_id}",
            f"https://api.fxtwitter.com/{username}/status/{tweet_id}",
        ]:
            try:
                r = WEB.get(api_url, headers={"User-Agent": UA_DESK}, timeout=15)
                if r.status_code != 200:
                    continue
                jdata = r.json()
                tweet = jdata.get("tweet") or jdata
                media_list = (
                    tweet.get("media", {}).get("all", [])
                    or tweet.get("media_extended", [])
                    or tweet.get("media", [])
                )
                collected = []
                for idx, media in enumerate(media_list):
                    murl = media.get("url") or media.get("media_url_https") or ""
                    mtype = media.get("type", "")
                    if not murl:
                        continue
                    log.info("vxtwitter: item %d type=%s url=%s", idx, mtype, murl[:80])
                    content = download_bytes(murl)
                    if content and len(content) > 1000:
                        ext = (
                            "mp4"
                            if mtype == "video"
                            else murl.rsplit(".", 1)[-1].split("?")[0] or "jpg"
                        )
                        fname = f"tweet_{tweet_id}_{idx}.{ext}"
                        collected.append((content, fname))
                if collected:
                    log.info("vxtwitter OK: %d items", len(collected))
                    return collected
            except Exception as e:
                log.warning("vxtwitter %s: %s", api_url, e)

    # Strategy 2: yt-dlp with twitter cookies
    cookies = TWITTER_COOKIES_FILE if TWITTER_COOKIES_FILE else ""
    result = social_download_ytdlp(tweet_url, cookies_file=cookies)
    if result:
        return [result]

    # Strategy 3: yt-dlp via Nitter
    if username and tweet_id:
        for instance in NITTER_INSTANCES:
            nitter_url = f"{instance}/{username}/status/{tweet_id}"
            r2 = social_download_ytdlp(nitter_url)
            if r2:
                return [r2]

    # Strategy 4: scrape images from Nitter HTML
    if username and tweet_id:
        for instance in NITTER_INSTANCES:
            try:
                page = WEB.get(
                    f"{instance}/{username}/status/{tweet_id}",
                    headers={"User-Agent": UA_DESK},
                    timeout=15,
                )
                if page.status_code != 200:
                    continue
                soup = BeautifulSoup(page.text, "html.parser")
                collected = []
                for img in soup.select(".still-image img, .attachment img"):
                    src = img.get("src", "")
                    if not src:
                        continue
                    full = f"{instance}{src}" if src.startswith("/") else src
                    content = download_bytes(full, MAX_IMAGE_SIZE)
                    if content and len(content) > 1000:
                        collected.append(
                            (content, f"tweet_{tweet_id}_{len(collected)}.jpg")
                        )
                if collected:
                    log.info("Nitter img scrape OK: %d items", len(collected))
                    return collected
            except Exception as e:
                log.warning("Nitter scrape %s: %s", instance, e)

    log.error("twitter_download_media: all strategies failed for %s", tweet_url)
    return None


# ─── Instagram ───────────────────────────────────────────────────────────────
def instagram_download_all(url: str) -> list[dict]:
    """
    Download ALL media from an Instagram post (single/carousel/reel).
    Returns list of {data, fname, caption, is_video} — up to 20 items.
    """
    log.info("instagram_download_all: %s", url)

    # Strategy 1: Cobalt API
    # - Reels/videos → tunnel → single mp4 ✓
    # - Carousels → picker → multiple items ✓
    # - Image-only posts → tunnel → Cobalt wraps it as a single-frame mp4 slideshow ✗
    #   In that case Cobalt returns 1 item that our magic-byte sniffer will mark as
    #   is_video=False (it's actually an image container). We use it only for reels.
    cobalt_items = _cobalt_download_all(url, audio_only=False)
    if cobalt_items:
        # If Cobalt returned a picker (multiple items) — use them all
        if len(cobalt_items) > 1:
            log.info("instagram: cobalt picker with %d items", len(cobalt_items))
            return [
                {
                    "data": it["data"],
                    "fname": it["fname"],
                    "caption": "",
                    "is_video": it.get("is_video", False),
                }
                for it in cobalt_items
            ]
        # Single item: if it's a real video (mp4 by magic bytes), use it (reel/video post)
        single = cobalt_items[0]
        if single.get("is_video", True):
            log.info("instagram: cobalt single video item")
            return [
                {
                    "data": single["data"],
                    "fname": single["fname"],
                    "caption": "",
                    "is_video": True,
                }
            ]
        # Single non-video from cobalt = image post — fall through to get full carousel
        log.info(
            "instagram: cobalt returned single image, trying instaloader for carousel"
        )

    # Strategy 2: instaloader (handles carousel / multiple slides natively)
    try:
        import instaloader

        L = instaloader.Instaloader(
            download_videos=True,
            download_video_thumbnails=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            compress_json=False,
            quiet=True,
        )
        if INSTAGRAM_USER and INSTAGRAM_PASS:
            try:
                L.login(INSTAGRAM_USER, INSTAGRAM_PASS)
                log.info("instaloader: logged in as %s", INSTAGRAM_USER)
            except Exception as e:
                log.warning("instaloader login failed: %s", e)

        m = re.search(r"instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]+)", url)
        if not m:
            raise ValueError("Cannot extract shortcode from URL")
        shortcode = m.group(1)
        post = instaloader.Post.from_shortcode(L.context, shortcode)

        caption = (post.caption or "")[:1000]
        log.info("instagram: typename=%s caption_len=%d", post.typename, len(caption))

        results = []
        with tempfile.TemporaryDirectory() as tmp:
            L.dirname_pattern = tmp
            L.filename_pattern = "{shortcode}_{media_id}"
            L.download_post(post, target=tmp)

            all_files = sorted(Path(tmp).rglob("*"), key=lambda f: f.name)
            media_files = [
                f
                for f in all_files
                if f.suffix.lower()
                in {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov"}
                and f.stat().st_size > 1000
            ][:20]
            log.info("instaloader: %d media files for %s", len(media_files), shortcode)

            for f in media_files:
                is_video = f.suffix.lower() in {".mp4", ".mov"}
                results.append(
                    {
                        "data": f.read_bytes(),
                        "fname": f.name,
                        "caption": caption,
                        "is_video": is_video,
                    }
                )

        if results:
            log.info("instagram_download_all: %d items via instaloader", len(results))
            return results
    except Exception as e:
        log.error("instaloader: %s", e)

    # Strategy 3: yt-dlp with cookies
    try:
        ig_cookies = INSTAGRAM_COOKIES_FILE
        result = social_download_ytdlp(url, cookies_file=ig_cookies)
        if result:
            data, fname = result
            is_video = fname.lower().endswith((".mp4", ".mov", ".webm"))
            return [{"data": data, "fname": fname, "caption": "", "is_video": is_video}]
    except Exception as e:
        log.error("yt-dlp instagram: %s", e)

    # Strategy 4: use the cobalt single image we already fetched, if any
    if cobalt_items:
        single = cobalt_items[0]
        log.info("instagram: falling back to cobalt single image")
        return [
            {
                "data": single["data"],
                "fname": single["fname"],
                "caption": "",
                "is_video": single.get("is_video", False),
            }
        ]

    # Strategy 5: Instagram oEmbed API — returns thumbnail for public photo posts
    try:
        m = re.search(r"instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]+)", url)
        if m:
            shortcode = m.group(1)
            oe_resp = WEB.get(
                f"https://www.instagram.com/p/{shortcode}/media/?size=l",
                headers={
                    "User-Agent": UA_MOB,
                    "Accept": "image/webp,image/apng,image/*,*/*",
                    "Referer": "https://www.instagram.com/",
                },
                allow_redirects=True,
                timeout=20,
            )
            if oe_resp.status_code == 200 and len(oe_resp.content) > 5000:
                ct = oe_resp.headers.get("content-type", "")
                if "image" in ct:
                    ext = "jpg" if "jpeg" in ct else ct.split("/")[-1].split(";")[0]
                    fname = f"instagram_{shortcode}.{ext}"
                    log.info(
                        "instagram media redirect OK: %dKB %s",
                        len(oe_resp.content) // 1024,
                        fname,
                    )
                    return [
                        {
                            "data": oe_resp.content,
                            "fname": fname,
                            "caption": "",
                            "is_video": False,
                        }
                    ]
    except Exception as e:
        log.error("instagram media redirect: %s", e)

    return []


def instagram_get_profile(username: str) -> list[dict]:
    """
    Get recent posts from a public Instagram profile.
    Uses WARP proxy if available to bypass datacenter IP blocks.
    """
    log.info("instagram_get_profile: @%s", username)
    username = username.strip().lstrip("@")
    sess = _get_web(use_warp=bool(WARP_PROXY))

    # Strategy 1: web_profile_info API
    user = _ig_fetch_user_info(username)
    if user and not user.get("_oembed_only"):
        posts = []
        edges = user.get("edge_owner_to_timeline_media", {}).get("edges", [])
        for edge in edges[:12]:
            node = edge.get("node", {})
            sc = node.get("shortcode", "")
            cap = ""
            cap_edges = node.get("edge_media_to_caption", {}).get("edges", [])
            if cap_edges:
                cap = cap_edges[0].get("node", {}).get("text", "")
            posts.append(
                {
                    "url": f"https://www.instagram.com/p/{sc}/",
                    "shortcode": sc,
                    "text": cap,
                    "date": str(node.get("taken_at_timestamp", ""))[:10],
                    "is_video": node.get("is_video", False),
                    "likes": node.get("edge_liked_by", {}).get("count", 0),
                    "typename": node.get("__typename", ""),
                    "display_url": node.get("display_url", ""),
                }
            )
        if posts:
            log.info("instagram_get_profile: %d posts via web API", len(posts))
            return posts

    # Strategy 2: instaloader (needs credentials)
    if INSTAGRAM_USER and INSTAGRAM_PASS:
        try:
            import instaloader

            L = instaloader.Instaloader(quiet=True, download_pictures=False)
            try:
                L.login(INSTAGRAM_USER, INSTAGRAM_PASS)
            except Exception as le:
                log.warning("instaloader login failed: %s", le)
            profile = instaloader.Profile.from_username(L.context, username)
            posts = []
            for post in profile.get_posts():
                posts.append(
                    {
                        "url": f"https://www.instagram.com/p/{post.shortcode}/",
                        "shortcode": post.shortcode,
                        "text": post.caption or "",
                        "date": str(post.date_local)[:16],
                        "is_video": post.is_video,
                        "likes": post.likes,
                        "typename": post.typename,
                        "display_url": getattr(post, "url", "")
                        or getattr(post, "display_url", ""),
                    }
                )
                if len(posts) >= 12:
                    break
            log.info("instagram_get_profile (instaloader): %d posts", len(posts))
            return posts
        except Exception as e:
            log.error("instagram_get_profile instaloader: %s", e)

    return []


# ─── TikTok ──────────────────────────────────────────────────────────────────
def _fetch_tikwm_data(url: str, timeout=30) -> Optional[dict]:
    """Successful metadata only; callers may reuse it within one operation."""
    try:
        r = WEB.post("https://www.tikwm.com/api/", data={"url": url, "hd": "1"},
                     headers={"User-Agent": UA_DESK}, timeout=timeout)
        j = r.json()
        if j.get("code") == 0 and isinstance(j.get("data"), dict):
            return j["data"]
    except Exception as e:
        log.warning("tikwm metadata: %s", e)
    return None


def tiktok_download(url: str, tikwm_data=None) -> Optional[tuple[bytes, str]]:
    """Download a TikTok video WITH audio (tikwm first for muxed audio)."""
    log.info("tiktok_download: %s", url)
    # Strategy 1: tikwm.com — single muxed MP4 (audio+video, no watermark).
    try:
        d = tikwm_data if tikwm_data is not None else _fetch_tikwm_data(url, timeout=20)
        if d is not None:
            play = d.get("hdplay") or d.get("play") or d.get("wmplay")
            if play:
                if play.startswith("/"):
                    play = "https://www.tikwm.com" + play
                vr = WEB.get(play, headers={"User-Agent": UA_DESK,
                             "Referer": "https://www.tikwm.com/"}, timeout=90)
                data = vr.content
                if len(data) > 10000 and _has_audio_stream(data, "tt.mp4"):
                    title = (d.get("title") or "tiktok_video")[:60] or "tiktok_video"
                    log.info("tiktok_download OK via tikwm: %.1fMB", len(data)/1024/1024)
                    return data, f"{title}.mp4"
                log.warning("tiktok_download: tikwm file silent/small, falling back")
    except Exception as e:
        log.warning("tiktok_download: tikwm error: %s", e)

    # Strategy 2: yt-dlp — ONLY accept if it actually carries audio.
    result = social_download_ytdlp(url, audio_only=False,
                                   cookies_file=TWITTER_COOKIES_FILE or YOUTUBE_COOKIES_FILE)
    if result and _has_audio_stream(result[0], result[1]):
        return result

    # Strategy 3: Cobalt (tiktokFullAudio) — only if it has audio.
    cobalt = _cobalt_download(url)
    if cobalt and _has_audio_stream(cobalt[0], cobalt[1]):
        return cobalt

    # Strategy 4: snaptik.app
    try:
        log.info("tiktok_download: trying snaptik.app")
        r = WEB.post("https://snaptik.app/action-2024.php", data={"url": url},
                     headers={"User-Agent": UA_DESK, "Referer": "https://snaptik.app/",
                              "X-Requested-With": "XMLHttpRequest"}, timeout=15)
        if r.status_code == 200 and "download.php" in r.text:
            dl_match = re.search(r'href=["\']([^"\']*download\.php[^"\']*)["\']', r.text)
            if dl_match:
                dl_url = dl_match.group(1)
                if not dl_url.startswith("http"):
                    dl_url = "https://snaptik.app/" + dl_url
                video_data = WEB.get(dl_url, timeout=60).content
                if len(video_data) > 10000:
                    return video_data, "tiktok_video.mp4"
    except Exception as e:
        log.warning("tiktok_download: snaptik fallback error: %s", e)

    # Last resort: return whatever yt-dlp produced (maybe silent) rather than nothing.
    if result:
        log.warning("tiktok_download: returning possibly-silent yt-dlp file")
        return result
    log.error("tiktok_download: all methods failed")
    return None


# ─── Reddit ──────────────────────────────────────────────────────────────────
def reddit_download(url: str, audio_only=False, format_id=None) -> Optional[list[tuple[bytes, str]]]:
    """Download Reddit post media (video, images, gallery). Returns list of (data, filename)."""
    log.info("reddit_download: %s audio_only=%s fmt=%s", url, audio_only, format_id)
    return social_download_ytdlp(url, audio_only=audio_only, cookies_file="", format_id=format_id)


def reddit_user_posts(username: str, limit: int = 10) -> list[dict]:
    """Get Reddit user posts via yt-dlp flat extraction."""
    log.info("reddit_user_posts: u/%s", username)
    username = username.lstrip("u/").lstrip("/")
    try:
        result = subprocess.run(
            [
                YTDLP_BIN,
                "--flat-playlist",
                "--no-warnings",
                "--no-check-certificate",
                "--print",
                "%(id)s|||%(title)s|||%(duration_string)s|||%(url)s|||%(thumbnail)s",
                f"https://www.reddit.com/user/{username}/submitted",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        items = []
        for line in result.stdout.strip().split("\n"):
            parts = line.split("|||")
            if len(parts) >= 2:
                post_id = parts[0].strip()
                items.append(
                    {
                        "id": post_id,
                        "title": parts[1].strip(),
                        "duration": parts[2].strip() if len(parts) > 2 else "",
                        "url": parts[3].strip() if len(parts) > 3 else f"https://www.reddit.com/comments/{post_id}/",
                        "thumbnail": parts[4].strip() if len(parts) > 4 else "",
                    }
                )
            if len(items) >= limit:
                break
        log.info("reddit_user_posts: %d posts", len(items))
        return items
    except Exception as e:
        log.error("reddit_user_posts: %s", e)
        return []


# ─── Twitch ──────────────────────────────────────────────────────────────────
def twitch_download(url: str, audio_only=False, format_id=None) -> Optional[list[tuple[bytes, str]]]:
    """Download Twitch VOD or Clip via yt-dlp."""
    log.info("twitch_download: %s audio_only=%s fmt=%s", url, audio_only, format_id)
    return social_download_ytdlp(url, audio_only=audio_only, cookies_file="", format_id=format_id)


def twitch_user_videos(username: str, limit: int = 10) -> list[dict]:
    """Get Twitch user videos via yt-dlp flat extraction."""
    log.info("twitch_user_videos: %s", username)
    username = username.lstrip("@")
    try:
        result = subprocess.run(
            [
                YTDLP_BIN,
                "--flat-playlist",
                "--no-warnings",
                "--no-check-certificate",
                "--print",
                "%(id)s|||%(title)s|||%(duration_string)s|||%(url)s|||%(thumbnail)s",
                f"https://www.twitch.tv/{username}/videos",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        items = []
        for line in result.stdout.strip().split("\n"):
            parts = line.split("|||")
            if len(parts) >= 2:
                vid_id = parts[0].strip()
                items.append(
                    {
                        "id": vid_id,
                        "title": parts[1].strip(),
                        "duration": parts[2].strip() if len(parts) > 2 else "",
                        "url": parts[3].strip() if len(parts) > 3 else f"https://www.twitch.tv/videos/{vid_id}",
                        "thumbnail": parts[4].strip() if len(parts) > 4 else "",
                    }
                )
            if len(items) >= limit:
                break
        log.info("twitch_user_videos: %d videos", len(items))
        return items
    except Exception as e:
        log.error("twitch_user_videos: %s", e)
        return []


# ─── Vimeo ──────────────────────────────────────────────────────────────────
def vimeo_download(url: str, audio_only=False, format_id=None) -> Optional[list[tuple[bytes, str]]]:
    """Download Vimeo video via yt-dlp."""
    log.info("vimeo_download: %s audio_only=%s fmt=%s", url, audio_only, format_id)
    return social_download_ytdlp(url, audio_only=audio_only, cookies_file="", format_id=format_id)


# ─── Pinterest Video ────────────────────────────────────────────────────────
def pinterest_video_download(url: str, audio_only=False, format_id=None) -> Optional[list[tuple[bytes, str]]]:
    """Download Pinterest video via yt-dlp."""
    log.info("pinterest_video_download: %s audio_only=%s fmt=%s", url, audio_only, format_id)
    return social_download_ytdlp(url, audio_only=audio_only, cookies_file="", format_id=format_id)


# ─── Direct HLS/m3u8 ────────────────────────────────────────────────────────
def hls_download(url: str) -> Optional[tuple[bytes, str]]:
    """Download direct HLS/m3u8 stream via yt-dlp."""
    log.info("hls_download: %s", url)
    result = social_download_ytdlp(url, audio_only=False, cookies_file="")
    if result:
        return result
    # Fallback: try direct download
    return download_bytes(url)


# ─── GitHub ───────────────────────────────────────────────────────────────────
    """Get TikTok user video list with thumbnails via yt-dlp flat extraction."""
    log.info("tiktok_user_videos: @%s", username)
    username = username.lstrip("@")
    try:
        result = subprocess.run(
            [
                YTDLP_BIN,
                "--flat-playlist",
                "--no-warnings",
                "--no-check-certificate",
                "--print",
                "%(id)s|||%(title)s|||%(duration_string)s|||%(url)s|||%(thumbnail)s",
                f"https://www.tiktok.com/@{username}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        items = []
        for line in result.stdout.strip().split("\n"):
            parts = line.split("|||")
            if len(parts) >= 2:
                vid_id = parts[0].strip()
                items.append(
                    {
                        "id": vid_id,
                        "title": parts[1].strip(),
                        "duration": parts[2].strip() if len(parts) > 2 else "",
                        "url": parts[3].strip()
                        if len(parts) > 3
                        else f"https://www.tiktok.com/@{username}/video/{vid_id}",
                        "thumbnail": parts[4].strip() if len(parts) > 4 else "",
                    }
                )
            if len(items) >= limit:
                break
        log.info("tiktok_user_videos: %d videos", len(items))
        return items
    except Exception as e:
        log.error("tiktok_user_videos: %s", e)
        return []


# ─── GitHub ───────────────────────────────────────────────────────────────────


def _gh_headers() -> dict:
    h = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "BaleBot/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


def github_search_repos(query: str, page=0) -> list[dict]:
    log.info("github_search_repos: %r page=%d", query, page)
    try:
        r = WEB.get(
            "https://api.github.com/search/repositories",
            params={"q": query, "sort": "stars", "per_page": 8, "page": page + 1},
            headers=_gh_headers(),
            timeout=20,
        )
        log.debug("github_search: status=%d", r.status_code)
        if r.status_code == 200:
            items = r.json().get("items", [])
            log.info("github_search: %d repos", len(items))
            return items
        log.error("github_search error: %s", r.text[:200])
        return []
    except Exception as e:
        log.error("github_search_repos: %s", e, exc_info=True)
        return []


def github_repo_info(full_name: str) -> Optional[dict]:
    log.info("github_repo_info: %s", full_name)
    try:
        r = WEB.get(
            f"https://api.github.com/repos/{full_name}",
            headers=_gh_headers(),
            timeout=15,
        )
        if r.status_code == 200:
            return r.json()
        return None
    except Exception as e:
        log.error("github_repo_info: %s", e)
        return None


def github_latest_release(full_name: str) -> Optional[dict]:
    log.info("github_latest_release: %s", full_name)
    try:
        r = WEB.get(
            f"https://api.github.com/repos/{full_name}/releases/latest",
            headers=_gh_headers(),
            timeout=15,
        )
        log.debug("gh release: status=%d", r.status_code)
        if r.status_code == 200:
            return r.json()
        return None
    except Exception as e:
        log.error("github_latest_release: %s", e)
        return None


def github_zip(repo_url: str) -> Optional[bytes]:
    log.info("github_zip: %s", repo_url)
    m = re.match(r"https?://github\.com/([^/]+/[^/]+?)(?:\.git|/|$)", repo_url)
    if not m:
        log.error("github_zip: cannot parse URL")
        return None
    slug = m.group(1)
    for branch in ["main", "master"]:
        url = f"https://github.com/{slug}/archive/refs/heads/{branch}.zip"
        try:
            r = WEB.get(url, timeout=120, stream=True)
            log.debug("github_zip: %s status=%d", url, r.status_code)
            if r.status_code == 200:
                data = r.content
                log.info("github_zip: %dMB", len(data) // 1024 // 1024)
                return data
        except Exception as e:
            log.error("github_zip %s: %s", branch, e)
    return None


# ─── Images ───────────────────────────────────────────────────────────────────
def images_bing(query: str, max_results=8) -> list[dict]:
    """Bing Images - no SafeSearch forced, returns actual results."""
    log.info("images_bing: %r", query)
    try:
        r = WEB.get(
            "https://www.bing.com/images/search",
            params={"q": query, "form": "HDRSC2", "first": "1"},
            headers={"User-Agent": UA_DESK, "Referer": "https://www.bing.com/"},
            timeout=20,
        )
        log.debug("bing_images: status=%d len=%d", r.status_code, len(r.text))
        results, seen = [], set()
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", class_="iusc"):
            m_attr = a.get("m")
            if not m_attr:
                continue
            try:
                meta = json.loads(m_attr)
            except (json.JSONDecodeError, TypeError):
                continue
            u = meta.get("murl") or meta.get("turl")
            if not u or u in seen:
                continue
            seen.add(u)
            results.append({"url": u, "title": meta.get("t") or query,
                            "_referer": "https://www.bing.com/"})
            if len(results) >= max_results:
                break
        if not results:
            for pattern in [r'"murl":"(https?://[^"]+?)"', r'"turl":"(https?://[^"]+?)"']:
                for u in re.findall(pattern, r.text):
                    u = u.replace("\\/", "/").replace("&", "&")
                    if u in seen or any(x in u for x in ("bing.com/rp/", "microsoft.com", "127.0.0.1", "localhost")):
                        continue
                    seen.add(u)
                    results.append({"url": u, "title": query, "_referer": "https://www.bing.com/"})
                    if len(results) >= max_results:
                        break
                if results:
                    break
        log.info("images_bing: %d results", len(results))
        return results
    except Exception as e:
        log.error("images_bing: %s", e, exc_info=True)
        return []


def images_duckduckgo(query: str, max_results=8) -> list[dict]:
    """DuckDuckGo image search via HTML scrape (i.js API blocked)."""
    log.info("images_duckduckgo: %r", query)
    try:
        r = WEB.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": UA_DESK, "Referer": "https://duckduckgo.com/"},
            timeout=20,
        )
        if r.status_code != 200:
            log.warning("images_duckduckgo: status=%d", r.status_code)
            return []

        soup = BeautifulSoup(r.text, "html.parser")
        results = []
        seen = set()

        # Find result links with images
        for a in soup.find_all("a", class_="result__image"):
            img = a.find("img")
            if img and img.get("data-src"):
                u = img["data-src"]
                if u not in seen and u.startswith("http"):
                    seen.add(u)
                    results.append({
                        "url": u,
                        "title": query,
                        "_referer": "https://duckduckgo.com/"
                    })
                    if len(results) >= max_results:
                        break

        # Fallback: regex search for image URLs
        if not results:
            for u in re.findall(r'data-src="(https?://[^"]+\.(?:jpg|jpeg|png|webp))"', r.text):
                if u not in seen:
                    seen.add(u)
                    results.append({"url": u, "title": query, "_referer": "https://duckduckgo.com/"})
                    if len(results) >= max_results:
                        break

        log.info("images_duckduckgo: %d results", len(results))
        return results
    except Exception as e:
        log.error("images_duckduckgo: %s", e, exc_info=True)
        return []

def images_google(query: str, max_results=8) -> list[dict]:
    """Google Images scrape - best effort fallback."""
    log.info("images_google: %r", query)
    try:
        r = WEB.get("https://www.google.com/search",
                    params={"q": query, "tbm": "isch", "hl": "en"},
                    headers={"User-Agent": UA_DESK, "Accept-Language": "en-US,en;q=0.9"},
                    timeout=20)
        if r.status_code != 200:
            return []
        results, seen = [], set()
        for u in re.findall(r'\["(https?://[^"]+?\.(?:jpg|jpeg|png|webp))",\d+,\d+\]', r.text):
            u = u.replace("\\u003d", "=").replace("\\u0026", "&")
            if u in seen or "gstatic.com" in u or "google.com" in u:
                continue
            seen.add(u)
            results.append({"url": u, "title": query})
            if len(results) >= max_results:
                break
        log.info("images_google: %d results", len(results))
        return results
    except Exception as e:
        log.error("images_google: %s", e, exc_info=True)
        return []

def images_multi(query: str, max_results=8) -> list[dict]:
    """Aggregate DuckDuckGo -> Bing(strict) -> Wikimedia -> Google + new sources, deduped."""
    log.info("images_multi: %r", query)
    out, seen = [], set()
    for fn in (images_duckduckgo, images_bing, images_wikimedia, images_google,
               images_deviantart, images_pixiv, images_artstation, images_flickr,
               images_tumblr, images_imgur, images_kemono,
               images_newgrounds, images_danbooru, images_gelbooru):
        try:
            for r in fn(query, max_results=max_results):
                u = r.get("url", "")
                if u and u not in seen:
                    seen.add(u)
                    out.append(r)
                if len(out) >= max_results:
                    return out
        except Exception as e:
            log.debug("images_multi %s: %s", getattr(fn, "__name__", fn), e)
    return out


def images_pinterest(query: str, max_results=8) -> list[dict]:
    log.info("images_pinterest: %r", query)
    # Strategy 1: Pinterest API
    try:
        r = WEB.get(
            "https://www.pinterest.com/resource/BaseSearchResource/get/",
            headers={
                "User-Agent": UA_DESK,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json",
            },
            params={
                "source_url": f"/search/pins/?q={urllib.parse.quote(query)}",
                "data": json.dumps(
                    {"options": {"query": query, "scope": "pins"}, "context": {}}
                ),
                "_": str(int(time.time() * 1000)),
            },
            timeout=20,
        )
        log.debug("pinterest_api: status=%d", r.status_code)
        if r.status_code == 200 and r.text.strip().startswith("{"):
            pins = (
                r.json().get("resource_response", {}).get("data", {}).get("results", [])
            )
            results = []
            for pin in pins:
                for size in ("orig", "736x", "474x"):
                    u = pin.get("images", {}).get(size, {}).get("url", "")
                    if u:
                        results.append({"url": u, "title": pin.get("title") or query})
                        break
                if len(results) >= max_results:
                    break
            if results:
                log.info("pinterest_api: %d results", len(results))
                return results
    except Exception as e:
        log.error("pinterest_api: %s", e)

    # Strategy 2: HTML scrape
    try:
        r2 = WEB.get(
            f"https://www.pinterest.com/search/pins/?q={urllib.parse.quote(query)}&rs=typed",
            headers={"User-Agent": UA_DESK},
            timeout=25,
        )
        log.debug("pinterest_html: status=%d len=%d", r2.status_code, len(r2.text))
        seen: set[str] = set()
        results = []
        for pat in [
            r'"orig":\s*\{"url":"(https://i\.pinimg\.com/[^"]+)"',
            r'"(https://i\.pinimg\.com/originals/[^"]+\.(?:jpg|jpeg|png|webp))"',
            r'"(https://i\.pinimg\.com/736x/[^"]+\.(?:jpg|jpeg|png|webp))"',
        ]:
            for u in re.findall(pat, r2.text):
                u = u.replace("\\u002F", "/")
                if u not in seen:
                    seen.add(u)
                    results.append(
                        {"url": u, "title": query, "_referer": "https://www.pinterest.com/"}
                    )
                if len(results) >= max_results:
                    break
        if results:
            log.info("pinterest_html: %d results", len(results))
            return results
        log.warning("pinterest_html: 0 results. Page head: %s", r2.text[:300])
    except Exception as e:
        log.error("pinterest_html: %s", e)

    # Strategy 3: Fall back to Bing with pinterest site: filter
    log.info("pinterest: falling back to Bing")
    return images_bing(f"site:pinterest.com {query}", max_results)


def images_pexels(query: str, max_results=8) -> list[dict]:
    """Pixabay API (requires free API key) + Unsplash fallback."""
    log.info("images_pexels: %r", query)
    # Key from env or hardcoded backup — get your own free key at pixabay.com/api/docs/
    # Note: The hardcoded key may be rate-limited. Set PIXABAY_KEY env var for production.
    PIXABAY_KEY = os.getenv("PIXABAY_KEY", "")
    if not PIXABAY_KEY:
        log.warning("pixabay: no API key set - set PIXABAY_KEY env var at pixabay.com/api/docs/")

    if PIXABAY_KEY:
        try:
            r = _get_web(use_warp=True).get(
                "https://pixabay.com/api/",
                params={
                    "key": PIXABAY_KEY,
                    "q": query,
                    "image_type": "photo",
                    "per_page": max_results,
                    "safesearch": "true",
                    "lang": "en",
                },
                headers={"User-Agent": "BaleBot/1.0"},
                timeout=15,
            )
            log.debug("pixabay: status=%d", r.status_code)
            if r.status_code == 200:
                hits = r.json().get("hits", [])
                results = [
                    {
                        "url": h.get("webformatURL") or h.get("largeImageURL"),
                        "title": h.get("tags", query)[:60],
                    }
                    for h in hits
                    if h.get("webformatURL")
                ]
                if results:
                    log.info("pixabay: %d results", len(results))
                    return results
            elif r.status_code == 400:
                log.warning("pixabay: invalid API key (400) - check PIXABAY_KEY env var")
            elif r.status_code == 403:
                log.warning("pixabay: API key forbidden (403) - key may be blocked")
            else:
                log.warning("pixabay: status=%d resp=%s", r.status_code, r.text[:100])
        except Exception as e:
            log.error("images_pexels pixabay: %s", e)

    # Unsplash API fallback (no key required for basic usage)
    try:
        results = []
        # Use Unsplash API directly
        r_unsplash = WEB.get(
            f"https://unsplash.com/napi/search/photos?query={urllib.parse.quote(query)}&per_page={max_results}",
            headers={
                "User-Agent": UA_DESK,
                "Accept": "application/json",
                "Referer": "https://unsplash.com",
            },
            timeout=15,
        )
        if r_unsplash.status_code == 200:
            data = r_unsplash.json()
            photos = data.get("results", [])
            for photo in photos[:max_results]:
                url = photo.get("urls", {}).get("regular", "")
                if url:
                    results.append({
                        "url": url,
                        "title": photo.get("description", {}).get("raw", query)[:60] or query,
                        "_referer": "https://unsplash.com",
                    })
            if results:
                log.info("unsplash_api: %d results", len(results))
                return results
    except Exception as e:
        log.debug("unsplash api fallback: %s", e)

    # Final fallback to Bing
    log.info("pexels: falling back to Bing")
    return images_bing(query, max_results)


def images_wikimedia(query: str, max_results=8) -> list[dict]:
    """Search Wikimedia Commons for images with better filtering and relevance."""
    log.info("images_wikimedia: %r", query)
    try:
        r = _get_web(use_warp=True).get(
            "https://commons.wikimedia.org/w/api.php",
            params={
                "action": "query",
                "generator": "search",
                "gsrsearch": query,
                "gsrnamespace": "6",  # File namespace
                "gsrlimit": str(max_results * 3),
                "prop": "imageinfo",
                "iiprop": "url|size|mime",
                "format": "json",
            },
            headers={"User-Agent": "BaleBot/1.0"},
            timeout=15,
        )
        log.debug("wikimedia_search: status=%d", r.status_code)
        results = []
        pages = r.json().get("query", {}).get("pages", {})
        query_terms = query.lower().split()

        # Score-based ranking for better relevance
        scored_results = []
        for pg in sorted(pages.values(), key=lambda p: p.get("index", 999)):
            ii = (pg.get("imageinfo") or [{}])[0]
            url = ii.get("url", "")
            mime = ii.get("mime", "")
            title = pg.get("title", "")

            # Accept jpeg, png, webp — skip SVG, GIF, TIFF, OGG
            if url and mime in ("image/jpeg", "image/png", "image/webp", "image/jpg"):
                title_lower = title.lower()
                file_name = title_lower.replace("file:", "").replace("_", " ")

                # Score by how many query terms match
                score = sum(1 for term in query_terms if term in file_name)

                # Bonus if title starts with query
                if file_name.startswith(query_terms[0] if query_terms else ""):
                    score += 5

                # Only include if at least one term matches OR we have few results
                if score > 0 or len(results) < max_results // 2:
                    scored_results.append((score, {"url": url, "title": title}))

        # Sort by score (descending) and take top results
        scored_results.sort(key=lambda x: -x[0])
        results = [item for _, item in scored_results[:max_results]]

        log.info("images_wikimedia: %d results", len(results))
        return results
    except Exception as e:
        log.error("images_wikimedia: %s", e, exc_info=True)
        return []


def images_deviantart(query: str, max_results=8) -> list[dict]:
    """Search DeviantArt via DuckDuckGo HTML + direct."""
    log.info("images_deviantart: %r", query)
    results = []
    try:
        # Search via DuckDuckGo for site:deviantart.com
        r = _get_web(use_warp=True).get(
            "https://html.duckduckgo.com/html/",
            params={"q": f"site:deviantart.com {query}"},
            headers={"User-Agent": UA_DESK, "Accept": "text/html"},
            timeout=15,
        )
        if r.status_code == 200:
            import re
            # Find image URLs in results
            for match in re.finditer(r'data-src="([^"]+\.(?:jpg|jpeg|png|webp))"', r.text):
                url = match.group(1)
                if url and "deviantart" in url:
                    results.append({"url": url, "title": query, "_referer": "https://www.deviantart.com/"})
                    if len(results) >= max_results:
                        break
        if results:
            log.info("deviantart: %d results", len(results))
            return results[:max_results]
    except Exception as e:
        log.debug("deviantart ddg: %s", e)
    # Fallback to Bing
    return images_bing(f"site:deviantart.com {query}", max_results)


def images_pixiv(query: str, max_results=8) -> list[dict]:
    """Search Pixiv via DuckDuckGo + direct."""
    log.info("images_pixiv: %r", query)
    results = []
    try:
        r = _get_web(use_warp=True).get(
            "https://html.duckduckgo.com/html/",
            params={"q": f"site:pixiv.net {query}"},
            headers={"User-Agent": UA_DESK, "Accept": "text/html"},
            timeout=15,
        )
        if r.status_code == 200:
            import re
            for match in re.finditer(r'data-src="([^"]+\.(?:jpg|jpeg|png|webp))"', r.text):
                url = match.group(1)
                if url and ("pixiv" in url or "pximg" in url):
                    results.append({"url": url, "title": query, "_referer": "https://www.pixiv.net/"})
                    if len(results) >= max_results:
                        break
        if results:
            log.info("pixiv: %d results", len(results))
            return results[:max_results]
    except Exception as e:
        log.debug("pixiv ddg: %s", e)
    return images_bing(f"site:pixiv.net {query}", max_results)


def images_artstation(query: str, max_results=8) -> list[dict]:
    """Search ArtStation via DuckDuckGo + direct."""
    log.info("images_artstation: %r", query)
    results = []
    try:
        r = _get_web(use_warp=True).get(
            "https://html.duckduckgo.com/html/",
            params={"q": f"site:artstation.com {query}"},
            headers={"User-Agent": UA_DESK, "Accept": "text/html"},
            timeout=15,
        )
        if r.status_code == 200:
            import re
            for match in re.finditer(r'data-src="([^"]+\.(?:jpg|jpeg|png|webp))"', r.text):
                url = match.group(1)
                if url and "artstation" in url:
                    results.append({"url": url, "title": query, "_referer": "https://www.artstation.com/"})
                    if len(results) >= max_results:
                        break
        if results:
            log.info("artstation: %d results", len(results))
            return results[:max_results]
    except Exception as e:
        log.debug("artstation ddg: %s", e)
    return images_bing(f"site:artstation.com {query}", max_results)


def images_flickr(query: str, max_results=8) -> list[dict]:
    """Search Flickr via API (no key needed for public)."""
    log.info("images_flickr: %r", query)
    try:
        r = _get_web(use_warp=True).get(
            "https://api.flickr.com/services/rest/",
            params={
                "method": "flickr.photos.search",
                "api_key": os.getenv("FLICKR_KEY", "38c3d5c1555555555555555555555555"),  # placeholder
                "text": query,
                "per_page": max_results,
                "format": "json",
                "nojsoncallback": "1",
                "media": "photos",
                "safe_search": "1",
            },
            headers={"User-Agent": "BaleBot/1.0"},
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            photos = data.get("photos", {}).get("photo", [])
            results = []
            for p in photos:
                url = f"https://live.staticflickr.com/{p['server']}/{p['id']}_{p['secret']}_b.jpg"
                results.append({"url": url, "title": p.get("title", query), "_referer": "https://www.flickr.com/"})
            if results:
                log.info("flickr: %d results", len(results))
                return results[:max_results]
    except Exception as e:
        log.debug("flickr api: %s", e)
    return images_bing(f"site:flickr.com {query}", max_results)


def images_tumblr(query: str, max_results=8) -> list[dict]:
    """Search Tumblr via DuckDuckGo."""
    log.info("images_tumblr: %r", query)
    results = []
    try:
        r = _get_web(use_warp=True).get(
            "https://html.duckduckgo.com/html/",
            params={"q": f"site:tumblr.com {query}"},
            headers={"User-Agent": UA_DESK, "Accept": "text/html"},
            timeout=15,
        )
        if r.status_code == 200:
            import re
            for match in re.finditer(r'data-src="([^"]+\.(?:jpg|jpeg|png|webp|gif))"', r.text):
                url = match.group(1)
                if url and "tumblr" in url:
                    results.append({"url": url, "title": query, "_referer": "https://www.tumblr.com/"})
                    if len(results) >= max_results:
                        break
        if results:
            log.info("tumblr: %d results", len(results))
            return results[:max_results]
    except Exception as e:
        log.debug("tumblr ddg: %s", e)
    return images_bing(f"site:tumblr.com {query}", max_results)


def images_imgur(query: str, max_results=8) -> list[dict]:
    """Search Imgur via DuckDuckGo + API fallback."""
    log.info("images_imgur: %r", query)
    results = []
    try:
        r = _get_web(use_warp=True).get(
            "https://html.duckduckgo.com/html/",
            params={"q": f"site:imgur.com {query}"},
            headers={"User-Agent": UA_DESK, "Accept": "text/html"},
            timeout=15,
        )
        if r.status_code == 200:
            import re
            for match in re.finditer(r'data-src="([^"]+\.(?:jpg|jpeg|png|webp|gif))"', r.text):
                url = match.group(1)
                if url and "imgur" in url:
                    results.append({"url": url, "title": query, "_referer": "https://imgur.com/"})
                    if len(results) >= max_results:
                        break
        if results:
            log.info("imgur: %d results", len(results))
            return results[:max_results]
    except Exception as e:
        log.debug("imgur ddg: %s", e)
    return images_bing(f"site:imgur.com {query}", max_results)


def images_kemono(query: str, max_results=8) -> list[dict]:
    """Search Kemono (party/kemono.su) via DuckDuckGo."""
    log.info("images_kemono: %r", query)
    results = []
    try:
        r = _get_web(use_warp=True).get(
            "https://html.duckduckgo.com/html/",
            params={"q": f"site:kemono.su OR site:kemono.party {query}"},
            headers={"User-Agent": UA_DESK, "Accept": "text/html"},
            timeout=15,
        )
        if r.status_code == 200:
            import re
            for match in re.finditer(r'data-src="([^"]+\.(?:jpg|jpeg|png|webp))"', r.text):
                url = match.group(1)
                if url and ("kemono" in url or "coomer" in url):
                    results.append({"url": url, "title": query, "_referer": "https://kemono.su/"})
                    if len(results) >= max_results:
                        break
        if results:
            log.info("kemono: %d results", len(results))
            return results[:max_results]
    except Exception as e:
        log.debug("kemono ddg: %s", e)
    return images_bing(f"(site:kemono.su OR site:kemono.party) {query}", max_results)


def images_newgrounds(query: str, max_results=8) -> list[dict]:
    """Search Newgrounds via DuckDuckGo."""
    log.info("images_newgrounds: %r", query)
    results = []
    try:
        r = _get_web(use_warp=True).get(
            "https://html.duckduckgo.com/html/",
            params={"q": f"site:newgrounds.com {query}"},
            headers={"User-Agent": UA_DESK, "Accept": "text/html"},
            timeout=15,
        )
        if r.status_code == 200:
            import re
            for match in re.finditer(r'data-src="([^"]+\.(?:jpg|jpeg|png|webp))"', r.text):
                url = match.group(1)
                if url and "newgrounds" in url:
                    results.append({"url": url, "title": query, "_referer": "https://www.newgrounds.com/"})
                    if len(results) >= max_results:
                        break
        if results:
            log.info("newgrounds: %d results", len(results))
            return results[:max_results]
    except Exception as e:
        log.debug("newgrounds ddg: %s", e)
    return images_bing(f"site:newgrounds.com {query}", max_results)


def images_danbooru(query: str, max_results=8) -> list[dict]:
    """Search Danbooru via DuckDuckGo."""
    log.info("images_danbooru: %r", query)
    results = []
    try:
        r = _get_web(use_warp=True).get(
            "https://html.duckduckgo.com/html/",
            params={"q": f"site:danbooru.donmai.us {query}"},
            headers={"User-Agent": UA_DESK, "Accept": "text/html"},
            timeout=15,
        )
        if r.status_code == 200:
            import re
            for match in re.finditer(r'data-src="([^"]+\.(?:jpg|jpeg|png|webp))"', r.text):
                url = match.group(1)
                if url and "danbooru" in url:
                    results.append({"url": url, "title": query, "_referer": "https://danbooru.donmai.us/"})
                    if len(results) >= max_results:
                        break
        if results:
            log.info("danbooru: %d results", len(results))
            return results[:max_results]
    except Exception as e:
        log.debug("danbooru ddg: %s", e)
    return images_bing(f"site:danbooru.donmai.us {query}", max_results)


def images_gelbooru(query: str, max_results=8) -> list[dict]:
    """Search Gelbooru via DuckDuckGo."""
    log.info("images_gelbooru: %r", query)
    results = []
    try:
        r = _get_web(use_warp=True).get(
            "https://html.duckduckgo.com/html/",
            params={"q": f"site:gelbooru.com {query}"},
            headers={"User-Agent": UA_DESK, "Accept": "text/html"},
            timeout=15,
        )
        if r.status_code == 200:
            import re
            for match in re.finditer(r'data-src="([^"]+\.(?:jpg|jpeg|png|webp))"', r.text):
                url = match.group(1)
                if url and "gelbooru" in url:
                    results.append({"url": url, "title": query, "_referer": "https://gelbooru.com/"})
                    if len(results) >= max_results:
                        break
        if results:
            log.info("gelbooru: %d results", len(results))
            return results[:max_results]
    except Exception as e:
        log.debug("gelbooru ddg: %s", e)
    return images_bing(f"site:gelbooru.com {query}", max_results)


# ─── Misc ─────────────────────────────────────────────────────────────────────
def translate_text(text: str, target: str, source="auto") -> str:
    log.info("translate_text: target=%s len=%d", target, len(text))
    import html as html_mod

    has_fa = bool(re.search(r"[\u0600-\u06FF]", text))
    if source == "auto":
        source = "fa" if has_fa else "en"
    if source == target:
        return text
    MAX = 490
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 <= MAX:
            cur = (cur + "\n" + line).lstrip("\n")
        else:
            if cur:
                chunks.append(cur)
            while len(line) > MAX:
                chunks.append(line[:MAX])
                line = line[MAX:]
            cur = line
    if cur:
        chunks.append(cur)
    parts = []
    for chunk in chunks:
        try:
            r = WEB.get(
                "https://api.mymemory.translated.net/get",
                params={"q": chunk, "langpair": f"{source}|{target}"},
                timeout=20,
            )
            log.debug("translate chunk: status=%d", r.status_code)
            t = r.json()["responseData"]["translatedText"]
            parts.append(html_mod.unescape(t))
        except Exception as e:
            log.error("translate chunk: %s", e)
            parts.append(chunk)
        time.sleep(0.2)
    return "\n".join(parts)


def ocr_image(img_bytes: bytes) -> str:
    log.info("ocr_image: %d bytes", len(img_bytes))
    try:
        img = Image.open(io.BytesIO(img_bytes))
        text = pytesseract.image_to_string(img, lang="fas+eng")
        log.info("ocr_image: extracted %d chars", len(text))
        return text.strip() or "(متنی یافت نشد)"
    except Exception as e:
        log.error("ocr_image: %s", e, exc_info=True)
        return "❌ خطا در پردازش تصویر."


def ocr_to_pdf(text: str) -> bytes:
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos

    pdf = FPDF()
    pdf.set_margins(15, 15, 15)
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    for line in text.split("\n"):
        safe = line.encode("latin-1", errors="replace").decode("latin-1")
        pdf.cell(0, 8, text=safe, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    return bytes(pdf.output())


def currency_convert(amount: float, frm: str, to: str) -> Optional[str]:
    log.info("currency_convert: %s %s->%s", amount, frm, to)
    try:
        r = WEB.get(
            "https://api.frankfurter.app/latest",
            params={"from": frm.upper(), "to": to.upper()},
            timeout=15,
        )
        log.debug("frankfurter: status=%d", r.status_code)
        if r.status_code == 200:
            rate = r.json().get("rates", {}).get(to.upper())
            if rate:
                return (
                    f"{amount:,.2f} {frm.upper()} = {amount * rate:,.4f} {to.upper()}"
                )
    except Exception as e:
        log.error("currency_convert: %s", e)
    return None


def ip_lookup(target: str) -> str:
    log.info("ip_lookup: %s", target)
    try:
        import socket

        ip = target.strip()
        if not re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
            ip = socket.gethostbyname(ip)
        r = WEB.get(
            f"https://ipapi.co/{ip}/json/",
            headers={"User-Agent": "BaleBot/1.0"},
            timeout=15,
        )
        log.debug("ipapi: status=%d", r.status_code)
        d = r.json()
        if d.get("error"):
            return "❌ اطلاعاتی یافت نشد."
        return (
            f"🌐 *اطلاعات IP: {ip}*\n\n"
            f"🏳 کشور: {d.get('country_name', '—')} ({d.get('country_code', '')})\n"
            f"🏙 شهر: {d.get('city', '—')} / {d.get('region', '—')}\n"
            f"📡 اپراتور: {d.get('org', '—')}\n"
            f"🕐 منطقه زمانی: {d.get('timezone', '—')}\n"
            f"📍 مختصات: {d.get('latitude', '—')}, {d.get('longitude', '—')}"
        )
    except Exception as e:
        log.error("ip_lookup: %s", e, exc_info=True)
        return "❌ خطا در جستجوی IP."


def shorten_url(url: str) -> str:
    log.info("shorten_url: %s", url)
    try:
        r = WEB.get(
            f"https://tinyurl.com/api-create.php?url={urllib.parse.quote(url)}",
            timeout=15,
        )
        log.debug("tinyurl: status=%d resp=%s", r.status_code, r.text[:60])
        if r.status_code == 200 and r.text.startswith("http"):
            return r.text.strip()
        return "❌ خطا در کوتاه‌سازی."
    except Exception as e:
        log.error("shorten_url: %s", e)
        return "❌ خطا در کوتاه‌سازی."


def expand_url(url: str) -> str:
    log.info("expand_url: %s", url)
    try:
        r = WEB.head(url, allow_redirects=True, timeout=15)
        return f"🔗 لینک نهایی:\n{r.url}\n\n_(ریدایرکت‌ها: {len(r.history)})_"
    except Exception as e:
        log.error("expand_url: %s", e)
        return "❌ خطا در باز کردن لینک."


def paste_text(content: str) -> Optional[str]:
    log.info("paste_text: %d chars", len(content))
    try:
        r = WEB.post(
            "https://paste.rs/",
            data=content.encode("utf-8"),
            headers={"Content-Type": "text/plain"},
            timeout=15,
        )
        log.debug("paste.rs: status=%d resp=%s", r.status_code, r.text[:60])
        if r.status_code in (200, 201) and r.text.startswith("http"):
            return r.text.strip()
        return None
    except Exception as e:
        log.error("paste_text: %s", e)
        return None


def generate_qr(text: str) -> Optional[bytes]:
    log.info("generate_qr: %r", text[:40])
    try:
        import qrcode

        qr = qrcode.QRCode(version=1, box_size=10, border=4)
        qr.add_data(text)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return buf.read()
    except ImportError:
        r = WEB.get(
            f"https://api.qrserver.com/v1/create-qr-code/?size=300x300"
            f"&data={urllib.parse.quote(text)}",
            timeout=15,
        )
        return r.content if r.status_code == 200 else None
    except Exception as e:
        log.error("generate_qr: %s", e)
        return None


# ─── Google Play / APK Download ───────────────────────────────────────────────


def gplay_search(query: str, n_hits: int = 8, country: str = "us") -> list[dict]:
    """Search Google Play via public search HTML (real appIds, no API).

    google_play_scraper.search() drops the first result's appId (upstream
    bug: 2026-08 probe returned appId=None for the top hit), so the plain
    Play HTML page is parsed first and the scraper is only a fallback.
    """
    log.info("gplay_search: %r", query)
    apps: list[dict] = []
    try:
        r = WEB.get(
            "https://play.google.com/store/search",
            params={"q": query, "c": "apps", "hl": "en", "gl": country},
            headers={"User-Agent": UA_MOB},
            timeout=20,
        )
        if r.status_code == 200:
            ids: list[str] = []
            for m in re.finditer(r"/store/apps/details\?id=([a-zA-Z0-9._]+)", r.text):
                aid = m.group(1)
                if aid not in ids:
                    ids.append(aid)
                if len(ids) >= n_hits:
                    break
            for aid in ids:
                # Best-effort rating/icon from the text around each id
                seg = r.text[r.text.find(aid): r.text.find(aid) + 6000]
                sc_m = re.search(r"(\d\.\d)", seg)
                ic_m = re.search(
                    r"https://play-lh\.googleusercontent\.com/[A-Za-z0-9_-]{20,}", seg
                )
                apps.append(
                    {
                        "app_id": aid,
                        "title": aid.split(".")[-1].replace("_", " ").title(),
                        "developer": "",
                        "score": float(sc_m.group(1)) if sc_m else 0,
                        "installs": "",
                        "size": "",
                        "icon": ic_m.group(0) if ic_m else "",
                        "free": True,
                        "price": "Free",
                        "summary": "",
                        "version": "",
                        "url": f"https://play.google.com/store/apps/details?id={aid}",
                    }
                )
            if apps:
                log.info("gplay_search: %d results via Play HTML", len(apps))
                return apps
    except Exception as e:
        log.warning("gplay_search Play HTML: %s", e)

    # Fallback: google_play_scraper (title-only hits when appId is lost)
    try:
        from google_play_scraper import search

        results = search(query, n_hits=n_hits, lang="en", country=country)
        for r2 in results:
            aid = r2.get("appId", "")
            if not aid:
                continue
            apps.append(
                {
                    "app_id": aid,
                    "title": r2.get("title", ""),
                    "developer": r2.get("developer", ""),
                    "score": r2.get("score", 0),
                    "installs": r2.get("installs", ""),
                    "size": r2.get("size", ""),
                    "icon": r2.get("icon", ""),
                    "free": r2.get("free", True),
                    "price": r2.get("price", "Free"),
                    "summary": r2.get("summary", "")[:200],
                    "version": r2.get("version", ""),
                    "url": f"https://play.google.com/store/apps/details?id={aid}",
                }
            )
        log.info("gplay_search: %d results via scraper", len(apps))
    except Exception as e:
        log.error("gplay_search: %s", e)
    return apps


def gplay_app_info(app_id: str) -> Optional[dict]:
    """Get full app metadata from Google Play."""
    log.info("gplay_app_info: %s", app_id)
    try:
        from google_play_scraper import app as gp_app

        r = gp_app(app_id, lang="en", country="us")
        return {
            "app_id": r.get("appId", ""),
            "title": r.get("title", ""),
            "developer": r.get("developer", ""),
            "score": r.get("score", 0),
            "ratings": r.get("ratings", 0),
            "installs": r.get("realInstalls", "") or r.get("installs", ""),
            "size": r.get("size", ""),
            "icon": r.get("icon", ""),
            "free": r.get("free", True),
            "price": r.get("price", "Free"),
            "description": (r.get("description", "")[:500]),
            "version": r.get("version", ""),
            "updated": r.get("updated", ""),
            "android": r.get("androidVersionText", ""),
            "category": r.get("genre", ""),
            "url": f"https://play.google.com/store/apps/details?id={r.get('appId', '')}",
        }
    except Exception as e:
        log.error("gplay_app_info: %s", e)
        return None


def _apkpure_earned_cookies() -> dict[str, str]:
    """Solve APKPure's JS challenge once and return its cookies.

    Opens the app download page in headless Chromium, waits for the
    EO-Bot-Js-Token challenge to set its cookie, then harvests cookies
    for the CDN request. Empty dict on any failure.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        try:
            ctx = browser.new_context(
                user_agent="Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = ctx.new_page()
            page.goto(
                "https://apkpure.net/download.html",
                timeout=45000,
                wait_until="domcontentloaded",
            )
            page.wait_for_timeout(9000)
            jar = ctx.cookies("https://apkpure.net") + ctx.cookies(
                "https://d.apkpure.net"
            )
            return {c["name"]: c["value"] for c in jar}
        finally:
            browser.close()


def _apkcombo_dl_url(app_id: str) -> str:
    """Get an apkcombo R2 download URL via headless browser. Empty on failure."""
    from playwright.sync_api import sync_playwright

    slug = app_id.split(".")[-1].lower() or "app"
    url = f"https://apkcombo.com/{slug}/{app_id}/download/apk"
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        try:
            page = browser.new_page(
                user_agent="Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
                "Chrome/120.0 Mobile Safari/537.36"
            )
            page.goto(url, timeout=45000, wait_until="domcontentloaded")
            page.wait_for_timeout(4000)
            hrefs = page.eval_on_selector_all(
                "a[href]", "els => els.map(e => e.href)"
            )
            cands = [
                h for h in hrefs
                if h and "apkcombo-installer" not in h.lower()
                and (
                    "r2?u=" in h
                    or "r2.cloudflarestorage.com" in h
                    or h.lower().endswith((".apk", ".xapk"))
                )
            ]
            for h in cands:
                if "r2?u=" in h or "r2.cloudflarestorage.com" in h:
                    return h
            return cands[0] if cands else ""
        finally:
            browser.close()


def _apk_try(url: str, attempts: list, name: str, cookie: str = None,
             max_mb: int = 200) -> Optional[tuple[bytes, str]]:
    """Stream an APK candidate; record the attempt; return bytes if real."""
    t0 = time.time()
    try:
        hdrs = {
            "User-Agent": UA_MOB,
            "Referer": "https://apkpure.net/",
            "Accept": "*/*",
        }
        if cookie:
            hdrs["Cookie"] = cookie
        with WEB.get(url, headers=hdrs, allow_redirects=True,
                     timeout=(15, 90), stream=True) as r:
            ct = r.headers.get("content-type", "")
            if r.status_code != 200 or "html" in ct.lower():
                attempts.append(f"{name}: HTTP {r.status_code} {ct[:24]}")
                return None
            buf = bytearray()
            for chunk in r.iter_content(131072):
                buf.extend(chunk)
                if len(buf) > max_mb * 1024 * 1024:
                    attempts.append(f"{name}: exceeds {max_mb}MB limit")
                    return None
        if len(buf) < 50_000:
            attempts.append(f"{name}: too small ({len(buf)}B)")
            return None
        if buf[:2] != b"PK":
            attempts.append(f"{name}: not a zip/APK ({buf[:2]!r})")
            return None
        attempts.append(f"{name}: OK {len(buf) / 1e6:.1f}MB {time.time() - t0:.1f}s")
        log.info("apk_download %s OK: %.1fMB", name, len(buf) / 1e6)
        return bytes(buf)
    except Exception as e:
        attempts.append(f"{name}: {str(e)[:60]}")
        log.warning("apk_download %s: %s", name, e)
        return None


def apk_download(app_id: str, info: dict = None) -> Optional[tuple[bytes, str, list]]:
    """Download APK from multiple sources.

    Strategy 1: APKPure CDN with Playwright-earned challenge cookies
    Strategy 2: apkcombo R2 link (via headless browser, no challenge)
    Strategy 3: Aptoide public API (app/get, then apps/search fallback)
    Strategy 4: F-Droid (open-source apps only; 404 = expected miss)

    Returns (apk_bytes, filename, attempts) or (None, None, attempts).
    """
    log.info("apk_download: %s", app_id)
    attempts: list[str] = []
    fname = f"{app_id}.apk"

    # Strategy 1: APKPure CDN (APK variant) with challenge cookies
    try:
        ckjar = _apkpure_earned_cookies()
        cookie = "; ".join(f"{k}={v}" for k, v in ckjar.items())
        got = _apk_try(
            f"https://d.apkpure.net/b/APK/{app_id}?version=latest",
            attempts, "apkpure-cdn", cookie=cookie,
        )
        if got:
            return got, fname, attempts
        # XAPK bundle variant (needs APKPure installer; delivered as-is)
        got = _apk_try(
            f"https://d.apkpure.net/b/XAPK/{app_id}?version=latest",
            attempts, "apkpure-xapk", cookie=cookie,
        )
        if got:
            return got, f"{app_id}.xapk", attempts
    except Exception as e:
        attempts.append(f"apkpure: browser failed ({str(e)[:50]})")
        log.warning("apk_download apkpure: %s", e)

    # Strategy 2: apkcombo (plain site, R2 link via headless browser)
    try:
        dl = _apkcombo_dl_url(app_id)
        if dl:
            got = _apk_try(dl, attempts, "apkcombo", max_mb=300)
            if got:
                ext = ".xapk" if dl.lower().endswith(".xapk") else ".apk"
                return got, f"{app_id}{ext}", attempts
        else:
            attempts.append("apkcombo: no dl link")
    except Exception as e:
        attempts.append(f"apkcombo: {str(e)[:60]}")
        log.warning("apk_download apkcombo: %s", e)

    # Strategy 3: Aptoide — /get often lacks file.path; /apps/search has it
    aptoide_urls = [
        f"https://ws75.aptoide.com/api/7/app/get/package_name={app_id}/limit=1",
        f"https://ws75.aptoide.com/api/7/apps/search?query={app_id}&limit=3",
    ]
    for api_url in aptoide_urls:
        try:
            r5 = WEB.get(api_url, headers={"User-Agent": "BaleBot/1.0"}, timeout=15)
            if r5.status_code != 200:
                attempts.append(f"aptoide({api_url.split('/api/7/')[1].split('?')[0]}): HTTP {r5.status_code}")
                continue
            j = r5.json()
            nodes = [j.get("nodes", {})]
            nodes += j.get("datalist", {}).get("list", [])
            dl_url = ""
            for node in nodes:
                data = node.get("data", node) if isinstance(node, dict) else {}
                if not isinstance(data, dict):
                    continue
                if data.get("package") and data.get("package") != app_id:
                    continue
                dl_url = (data.get("file", {}) or {}).get("path", "") or dl_url
                if dl_url:
                    break
            if dl_url:
                got = _apk_try(dl_url, attempts, "aptoide")
                if got:
                    return got, fname, attempts
            else:
                attempts.append("aptoide: no file.path")
        except Exception as e:
            attempts.append(f"aptoide: {str(e)[:60]}")
            log.warning("apk_download aptoide: %s", e)

    # Strategy 4: F-Droid (FOSS only — 404 means "not here", not an error)
    try:
        api_r = WEB.get(
            f"https://f-droid.org/api/v1/packages/{app_id}",
            headers={"User-Agent": "BaleBot/1.0"},
            timeout=10,
        )
        if api_r.status_code != 200:
            attempts.append(f"fdroid: HTTP {api_r.status_code} (expected for non-FOSS)")
        else:
            versions = api_r.json().get("packages", [])
            if versions:
                ver_code = versions[0].get("versionCode", 0)
                fdroid_dl = f"https://f-droid.org/repo/{app_id}_{ver_code}.apk"
                got = _apk_try(fdroid_dl, attempts, "fdroid")
                if got:
                    return got, fname, attempts
            else:
                attempts.append("fdroid: no packages")
    except Exception as e:
        attempts.append(f"fdroid: {str(e)[:60]}")
        log.debug("apk_download fdroid: %s", e)

    log.error("apk_download: all sources failed for %s (%s)", app_id, attempts)
    return None, None, attempts


def _libgen_session() -> requests.Session:
    """Return HTTP session (WARP proxy if configured)."""
    return _get_web(use_warp=bool(WARP_PROXY))


def annas_archive_search(
    query: str, count: int = 10, extensions: list = None, sess=None
) -> list[dict]:
    """Search Anna's Archive (aggregates libgen, Z-Library, Sci-Hub, and more).

    Anna's Archive book pages use the same MD5 hashes as libgen for most
    entries, so results returned here plug directly into libgen_download()
    (which already has an Anna's Archive fallback in its download chain).
    """
    log.info("annas_archive_search: %r", query)
    if sess is None:
        sess = _libgen_session()

    for mirror in ANNAS_ARCHIVE_MIRRORS:
        try:
            r = sess.get(
                f"{mirror}/search",
                params={"q": query},
                headers={"User-Agent": UA_DESK},
                timeout=20,
            )
            log.debug(
                "annas_archive_search %s: HTTP %d len=%d",
                mirror,
                r.status_code,
                len(r.content),
            )
            if r.status_code != 200 or len(r.content) < 500:
                continue

            soup = BeautifulSoup(r.text, "html.parser")
            results = []
            seen_md5 = set()
            for a in soup.select("a[href*='/md5/']"):
                href = a.get("href", "")
                mm = re.search(r"/md5/([a-fA-F0-9]{32})", href)
                if not mm:
                    continue
                md5 = mm.group(1).lower()
                if md5 in seen_md5:
                    continue

                text = a.get_text(" ", strip=True)
                if not text:
                    continue

                # Title is generally the longest contiguous text chunk;
                # format/size/language info usually appears in a trailing
                # bracketed/comma-separated segment, e.g.
                # "... English [en], .pdf, 🚀 12.3MB"
                ext = ""
                ext_m = re.search(
                    r"\.(pdf|epub|mobi|djvu|azw3?|fb2|txt|cbz|cbr)\b", text, re.I
                )
                if ext_m:
                    ext = ext_m.group(1).lower()
                if extensions and ext and ext not in [e.lower() for e in extensions]:
                    continue

                size_m = re.search(r"([\d.]+\s?(?:MB|KB|GB))", text, re.I)
                size_str = size_m.group(1) if size_m else ""

                # Best-effort title: strip off the trailing metadata segment
                # if we found one, otherwise use the first ~120 chars.
                title = text
                if ext_m:
                    title = text[: ext_m.start()].rstrip(" ,.")
                title = title[:150].strip() or query

                seen_md5.add(md5)
                results.append(
                    {
                        "id": md5,
                        "name": title,
                        "authors": [],
                        "year": "",
                        "publisher": "",
                        "language": "",
                        "extension": ext,
                        "size": size_str,
                        "cover": "",
                        "md5": md5,
                        "url": f"{mirror}/md5/{md5}",
                        "_source": "annas_archive",
                    }
                )
                if len(results) >= count:
                    break

            if results:
                log.info(
                    "annas_archive_search: %d results via %s", len(results), mirror
                )
                return results
        except Exception as e:
            log.warning("annas_archive_search %s: %s", mirror, e)

    return []


# Rave Book Search (ravebooksearch.com) is a thin UI over this Google CSE.
# The site itself is behind a Cloudflare challenge; its CSE is not.
RAVEBOOK_CSE_CX = "001315572920893299880:mcnnf1udq8e"
# CSE of ravebooksearch.com/index-audiobooks.html (audiobooks index)
RAVEBOOK_AUDIO_CSE_CX = "04bb79690e2ff47f8"


@_browser_job
def ravebook_search(query: str, mode: str = "ebooks", cx: str = None) -> list[dict]:
    """Search Rave Book Search's underlying Google CSE via headless browser.

    Returns list of {name, url, host} dicts (deduplicated, max 10).
    """
    log.info("ravebook_search: %r mode=%s", query, mode)
    from playwright.sync_api import sync_playwright

    url = (
        "https://cse.google.com/cse?cx="
        + (cx or RAVEBOOK_CSE_CX)
        + "&q="
        + urllib.parse.quote_plus(query)
    )
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )
            try:
                page = browser.new_page()
                page.goto(url, timeout=45000, wait_until="domcontentloaded")
                page.wait_for_selector("a.gs-title", timeout=15000)
                page.wait_for_timeout(1500)
                results = page.eval_on_selector_all(
                    "a.gs-title",
                    "els => els.map(e => ({t: e.textContent || '', h: e.href || ''}))",
                )
            finally:
                browser.close()
    except Exception as e:
        log.error("ravebook_search: %s", e, exc_info=True)
        return []

    out, seen = [], set()
    for r in results:
        href = r.get("h", "")
        title = (r.get("t") or "").strip()
        if not href or not title or href in seen:
            continue
        # CSE sometimes wraps targets in a /url?q=… redirect — unwrap it
        if "google." in href and "url?q=" in href:
            m = re.search(r"[?&](?:url|q)=([^&]+)", href)
            if m:
                href = urllib.parse.unquote(m.group(1))
        seen.add(href)
        host = ""
        m = re.match(r"https?://([^/]+)", href)
        if m:
            host = m.group(1)
        out.append({"name": title, "url": href, "host": host})
        if len(out) >= 10:
            break
    return out


def libgen_search(query: str, count: int = 10, extensions: list = None,
                  _ol_fallback: bool = False) -> list[dict]:
    """Search libgen.li by parsing the results table rows directly.

    libgen.li is the only mirror reachable from most hosts (annas-archive.*
    are DNS-blocked, libgen.lc serves a JS fingerprint wall). We read title,
    author, extension, size and md5 straight from each row, so results are
    always downloadable. Falls back to Open Library metadata only.
    """
    log.info("libgen_search: %r  ext=%s ol=%s", query, extensions, _ol_fallback)
    if not query.strip():
        return []
    sess = _libgen_session()
    exts = [e.lower().lstrip(".") for e in extensions] if extensions else None

    if not _ol_fallback:
      for mirror in LIBGEN_MIRRORS:
        try:
            r = sess.get(
                f"{mirror}/index.php",
                params={"req": query, "columns[]": "t", "objects[]": "f",
                        "res": max(count * 3, 25), "gmode": "on"},
                headers={"User-Agent": UA_DESK},
                timeout=20,
            )
            log.debug("libgen_search %s: HTTP %d len=%d", mirror, r.status_code, len(r.content))
            if r.status_code != 200 or len(r.content) < 500:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            table = (soup.find("table", id="tablelibgen")
                     or soup.find("table", class_="table")
                     or soup.find("table"))
            rows = table.find_all("tr") if table else []
            results, seen = [], set()
            for tr in rows:
                anchors = tr.find_all("a", href=True)
                md5 = ""
                for a in anchors:
                    mm = re.search(r"md5=([a-fA-F0-9]{32})", a["href"])
                    if mm:
                        md5 = mm.group(1).lower()
                        break
                if not md5 or md5 in seen:
                    continue
                title = ""
                for a in anchors:
                    if re.search(r"(edition|book|file|index)\.php", a["href"]):
                        t = a.get_text(" ", strip=True)
                        if t and len(t) > len(title):
                            title = t
                cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
                row_text = " ".join(cells)
                if not title:
                    title = (max(cells, key=len) if cells else query)[:200]
                ext = ""
                em = re.search(r"\b(pdf|epub|mobi|djvu|azw3?|fb2|txt|cbz|cbr|rar|zip)\b", row_text, re.I)
                if em:
                    ext = em.group(1).lower()
                if exts and ext and ext not in exts:
                    continue
                sm = re.search(r"([\d.]+\s?(?:MB|KB|GB))", row_text, re.I)
                size = sm.group(1) if sm else ""
                ym = re.search(r"\b(1[5-9]\d{2}|20[0-3]\d)\b", row_text)
                year = ym.group(1) if ym else ""
                author = cells[1] if len(cells) > 1 else ""
                cover = ""
                _img = tr.find("img")
                if _img:
                    _src = _img.get("data-src") or _img.get("src") or ""
                    if _src.startswith("//"):
                        cover = "https:" + _src
                    elif _src.startswith("http"):
                        cover = _src
                    elif _src and "/covers/" in _src.lower():
                        cover = mirror + "/" + _src.lstrip("/")
                seen.add(md5)
                results.append({
                    "id": md5, "name": title[:200],
                    "authors": [author] if author else [],
                    "year": year, "publisher": "", "language": "",
                    "extension": ext, "size": size, "cover": cover,
                    "md5": md5, "url": f"{mirror}/ads.php?md5={md5}",
                    "_source": "libgen",
                })
                if len(results) >= count:
                    break
            if results:
                log.info("libgen_search: %d results via %s", len(results), mirror)
                return results
            log.debug("libgen_search: no rows parsed at %s", mirror)
        except Exception as e:
            log.warning("libgen_search %s: %s", mirror, e)

    # Fallback: Open Library (metadata only, no direct download link)
    try:
        r = sess.get(
            "https://openlibrary.org/search.json",
            params={"q": query, "limit": count,
                    "fields": "key,title,author_name,first_publish_year,cover_i"},
            headers={"User-Agent": "BaleBot/1.0"}, timeout=15,
        )
        out = []
        if r.status_code == 200:
            for doc in r.json().get("docs", []):
                t = (doc.get("title") or "").strip()
                if not t:
                    continue
                ci = doc.get("cover_i")
                out.append({
                    "id": doc.get("key", "").split("/")[-1], "name": t,
                    "authors": doc.get("author_name", [])[:3],
                    "year": str(doc.get("first_publish_year") or ""),
                    "publisher": "", "language": "", "extension": "",
                    "size": "", "md5": "",
                    "cover": f"https://covers.openlibrary.org/b/id/{ci}-M.jpg" if ci else "",
                    "url": f"https://openlibrary.org{doc.get('key','')}",
                    "_source": "openlibrary",
                })
                if len(out) >= count:
                    break
        if out:
            log.info("libgen_search: %d results via OpenLibrary (metadata only)", len(out))
        return out
    except Exception as e:
        log.warning("libgen_search OpenLibrary: %s", e)
    return []


def libgen_download(md5: str, title: str = "book", ext: str = "pdf") -> Optional[tuple[bytes, str]]:
    """Download a book by MD5 from libgen.li: ads.php -> get.php?md5=&key=.

    The 'key' query param is mandatory; without it get.php just loops back to
    ads.php. We read it from the ads page link (or regex the raw HTML).
    """
    log.info("libgen_download: md5=%s", md5)
    if not md5:
        return None
    sess = _libgen_session()
    md5l = md5.lower()
    safe = re.sub(r"[^\w\s\-.]", "_", title or "book")[:80]
    fname = f"{safe}.{ext}" if ext else f"{safe}.bin"
    hdrs = {"User-Agent": UA_DESK}

    def _is_file(resp) -> bool:
        ct = resp.headers.get("Content-Type", "").lower()
        return (resp.status_code == 200 and len(resp.content) > 3000 and (
            resp.content[:4] in (b"%PDF", b"PK\x03\x04") or resp.content[:4] == b"AT&T"
            or "pdf" in ct or "epub" in ct or "octet" in ct or "zip" in ct
            or ("text/html" not in ct and len(resp.content) > 20000)))

    def _finish(resp):
        fn = fname
        cd = resp.headers.get("Content-Disposition", "")
        if "filename" in cd:
            mf = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^;"\r\n]+)', cd)
            if mf:
                fn = urllib.parse.unquote(mf.group(1).strip().strip('"'))
        log.info("libgen_download OK: %s  %.1fMB", fn, len(resp.content) / 1024 / 1024)
        return resp.content, fn

    for mirror in LIBGEN_DL_MIRRORS:
        try:
            ads_url = f"{mirror}/ads.php?md5={md5l}"
            rp = sess.get(ads_url, headers=hdrs, timeout=25)
            log.debug("libgen_download ads %s: HTTP %d len=%d", mirror, rp.status_code, len(rp.content))
            if rp.status_code != 200 or len(rp.content) < 100:
                continue
            soup = BeautifulSoup(rp.text, "html.parser")
            dl_url = ""
            a = (soup.select_one("a[href*='get.php']")
                 or soup.select_one("a[href*='key=']")
                 or soup.select_one("#download a[href]")
                 or soup.select_one("h2 a[href]"))
            if a:
                href = a.get("href", "")
                dl_url = href if href.startswith("http") else f"{mirror}/{href.lstrip('/')}"
            else:
                km = re.search(r"get\.php\?[\w=&%.;\-]*md5=[a-fA-F0-9]{32}[\w=&%.;\-]*", rp.text)
                if km:
                    dl_url = f"{mirror}/" + km.group(1).replace("&amp;", "&")
            if not dl_url:
                log.debug("libgen_download: no get link at %s", mirror)
                continue
            log.debug("libgen_download: fetching %s", dl_url)
            resp = sess.get(dl_url, headers={**hdrs, "Referer": ads_url},
                            timeout=180, allow_redirects=True)
            if _is_file(resp):
                return _finish(resp)
            log.debug("libgen_download: not a file at %s CT=%s len=%d",
                      mirror, resp.headers.get("Content-Type", ""), len(resp.content))
        except Exception as e:
            log.warning("libgen_download %s: %s", mirror, e)

    log.error("libgen_download: all mirrors failed for md5=%s", md5)
    return None


def _is_public_http_url(url: str) -> bool:
    """Reject non-HTTP and non-public destinations before any request."""
    try:
        url = str(url or "")
        # Reject parser ambiguities rather than validate a different URL than requests sends.
        if any(ord(char) <= 32 or ord(char) == 127 for char in url) or "\\" in url:
            return False
        parsed = urllib.parse.urlparse(url)
        if (parsed.scheme.lower() not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            return False
        if parsed.port is not None and not 0 < parsed.port <= 65535:
            return False
        host = parsed.hostname.rstrip(".").lower()
        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            addresses = [
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
            ]
        if not addresses:
            return False
        return all(
            address.is_global
            and not address.is_private
            and not address.is_loopback
            and not address.is_link_local
            and not address.is_reserved
            and not address.is_multicast
            and not address.is_unspecified
            for address in addresses
        )
    except (OSError, ValueError):
        return False


def _public_get(url: str, **kwargs):
    """Validate untrusted destinations before each request, including redirects."""
    headers = dict(kwargs.pop("headers", {}) or {})
    for hop in range(6):
        if not _is_public_http_url(url):
            raise ValueError("نشانی اینترنتی مجاز نیست")
        response = WEB.get(url, headers=headers, stream=True, allow_redirects=False, **kwargs)
        if response.status_code not in (301, 302, 303, 307, 308):
            return response
        location = response.headers.get("Location")
        response.close()
        if not location or hop == 5:
            raise ValueError("تغییر مسیر بیش از حد یا نامعتبر است")
        target = urllib.parse.urljoin(url, location)
        if urllib.parse.urlsplit(target).netloc != urllib.parse.urlsplit(url).netloc:
            headers = {k: v for k, v in headers.items()
                       if k.lower() not in ("authorization", "cookie", "host", "referer")}
        url = target


def _rss_get_bounded(url: str, max_bytes: int) -> bytes:
    """Fetch a feed/site body with a hard byte and redirect budget."""
    if not _is_public_http_url(url):
        return b""
    response = None
    try:
        response = _public_get(
            url,
            headers={"User-Agent": UA_DESK, "Accept": "application/rss+xml, application/atom+xml, text/xml, text/html;q=0.8"},
            timeout=(5, 15),
        )
        if response.status_code != 200:
            return b""
        if not _is_public_http_url(str(getattr(response, "url", url) or url)):
            return b""
        content_length = response.headers.get("Content-Length")
        try:
            if content_length and int(content_length) > max_bytes:
                return b""
        except ValueError:
            pass
        chunks = []
        total = 0
        for chunk in response.iter_content(65536):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                return b""
            chunks.append(chunk)
        return b"".join(chunks)
    except (OSError, ValueError, requests.RequestException) as e:
        log.warning("RSS bounded fetch failed: %s", type(e).__name__)
        return b""
    finally:
        if response is not None:
            response.close()


def rss_fetch(url: str, limit: int = 15) -> list[dict]:
    """Fetch and parse RSS/Atom within a fixed 2 MiB body budget."""
    log.info("rss_fetch: %s", url)
    raw = _rss_get_bounded(url, 2 * 1024 * 1024)
    if not raw:
        return []
    try:
        soup = BeautifulSoup(raw, "xml")
        items_sel = soup.select("item") or soup.select("entry")
        items = []
        for it in items_sel[: max(1, min(int(limit), 15))]:
            title_node = it.find("title")
            title = title_node.get_text(strip=True) if title_node else ""
            link_tag = it.find("link")
            link = link_tag.get("href") or link_tag.get_text(strip=True) if link_tag else ""
            summary_tag = (
                it.find("summary")
                or it.find("description")
                or it.find("content")
                or it.find("content:encoded")
            )
            raw_summary = summary_tag.get_text(strip=True) if summary_tag else ""
            summary = BeautifulSoup(raw_summary, "html.parser").get_text(" ", strip=True)[:600]
            pub_tag = it.find("pubDate") or it.find("published") or it.find("updated")
            published = pub_tag.get_text(strip=True)[:25] if pub_tag else ""
            img_url = ""
            media = it.find("media:thumbnail") or it.find("media:content")
            if media:
                img_url = media.get("url", "")
            if not img_url:
                enc = it.find("enclosure", type=lambda t: t and "image" in t)
                if enc:
                    img_url = enc.get("url", "")
            if not img_url and summary_tag:
                img_tag = BeautifulSoup(str(summary_tag), "html.parser").find("img")
                if img_tag:
                    img_url = img_tag.get("src", "")
            if link:
                link = urllib.parse.urljoin(url, str(link).strip())[:2000]
            items.append({
                "title": title[:500],
                "link": link,
                "summary": summary,
                "published": published,
                "img_url": img_url,
            })
        log.info("rss_fetch: %d items", len(items))
        return items
    except Exception as e:
        log.error("rss_fetch: %s", type(e).__name__, exc_info=True)
        return []


def rss_search_feeds(site_url: str) -> list[str]:
    """Discover a small bounded set of feeds from a public website."""
    log.info("rss_search_feeds: %s", site_url)
    found: list[str] = []
    raw = _rss_get_bounded(site_url, 512 * 1024)
    if not raw:
        return []
    try:
        soup = BeautifulSoup(raw, "html.parser")
        for link in soup.find_all("link", rel="alternate"):
            href = link.get("href", "")
            if href and ("rss" in link.get("type", "").lower() or "atom" in link.get("type", "").lower()):
                candidate = urllib.parse.urljoin(site_url, href)
                if _is_public_http_url(candidate) and candidate not in found:
                    found.append(candidate)
        if not found:
            base = site_url.rstrip("/")
            for path in ("/feed", "/rss", "/feed.xml", "/atom.xml", "/rss.xml"):
                candidate = base + path
                if _is_public_http_url(candidate) and candidate not in found:
                    found.append(candidate)
    except Exception as e:
        log.error("rss_search_feeds: %s", type(e).__name__)
    return found[:5]


def init_user(cid: int):
    stats = durable_store.init_user_stats(_platform(), cid)
    with _stats_lock:
        user_stats[_pkey(cid)] = stats


def _log_history(cid: int, action: str, url: str = "", **details) -> None:
    """Record bounded download/tool metadata; never store media bytes."""
    payload = {"url": str(url or "")[:1000]}
    for key, value in list(details.items())[:10]:
        payload[str(key)[:40]] = str(value)[:200]
    try:
        durable_store.log_usage(_platform(), cid, str(action)[:80], payload)
    except Exception as e:
        log.debug("history log failed: %s", e)


def bump(cid: int, key="requests"):
    durable_store.bump_stat(_platform(), cid, key)


def get_state(cid: int) -> dict:
    return durable_store.get_state(_platform(), cid)


def set_state(cid: int, **kw):
    durable_store.set_state(_platform(), cid, **kw)


def clear_state(cid: int):
    durable_store.clear_state(_platform(), cid)


# ══════��════════════════════════════════════════════════════════════════════════
# STATIC TEXTS
# ═══════════════════════════════════════════════════════════════════════════════
HELP_TEXT = """❓ راهنمای بله قربان

از کجا شروع کنم؟
• برای جستجو یا دانلود، ابزار مربوط را انتخاب کنید و سپس متن یا لینک را بفرستید.
• برای کار با فایل، اول ابزار را انتخاب کنید؛ بعد فایل را ارسال کنید.
• اگر ابزار دیگری فعال است، با «منوی اصلی» از آن خارج شوید.

موضوع موردنظر را انتخاب کنید؛ هر بخش مثال و دکمهٔ شروع دارد."""

HELP_TOPICS = {
    "search": ("🔎 جستجو و خواندن", """🔎 جستجو و خواندن

وب: موضوع را بنویسید؛ مثلاً «آموزش پایتون فارسی». روی نتیجه بزنید تا گزینه‌های مشاهدهٔ سایت و متن صفحه را ببینید.
سایت: لینک کامل مثل https://example.com را بفرستید؛ پس از دریافت تصویر، گزینه‌های دیگر را انتخاب کنید.
کتاب و مقاله: عنوان، نام نویسنده یا کلیدواژه را وارد کنید؛ مثلاً «یادگیری ماشین».
اخبار: لینک سایت یا فید را در بخش اخبار وارد کنید.

برای جابه‌جایی میان نتایج از دکمه‌های قبلی، بعدی و برگشت استفاده کنید.""",
               [("🔎 جستجوی وب", "mode_search"), ("🌐 مشاهده سایت", "mode_open"), ("📚 کتاب و مقاله", "mode_book"), ("📰 اخبار", "mode_rss")]),
    "download": ("📥 دانلود و موسیقی", """📥 دانلود و موسیقی

شبکه‌های اجتماعی: سرویس را انتخاب کنید و لینک پست یا ویدیو را بفرستید، نه لینک نتیجهٔ جستجو.
موسیقی: نام آهنگ و خواننده را بنویسید یا لینک پشتیبانی‌شده را طبق پیام ابزار ارسال کنید.
فیلم، برنامهٔ اندروید و گیت‌هاب از بخش «دانلود و شبکه‌ها» در دسترس‌اند.

محتوای عمومی معمولاً قابل دریافت است؛ محتوای خصوصی، حذف‌شده یا محدودشده ممکن است دریافت نشود. فایل‌های بزرگ ممکن است چندبخشی ارسال شوند؛ پیام نهایی را بررسی کنید.""",
                 [("📱 شبکه‌های اجتماعی", "mode_social"), ("🎵 موسیقی", "mode_music"), ("📥 سایر دانلودها", "nav_download")]),
    "files": ("🧰 فایل و متن", """🧰 فایل و متن

۱. ابزار را انتخاب کنید.
۲. فایل یا متن خواسته‌شده را بفرستید.
۳. اگر ابزار اطلاعات بیشتری خواست، مطابق نمونهٔ همان پیام پاسخ دهید.

• متن از عکس: عکس واضح با نوشتهٔ خوانا بفرستید.
• صفحات PDF: فایل PDF و سپس شمارهٔ صفحات درخواستی را ارسال کنید.
• ادغام PDF: فایل‌ها را به ترتیب دلخواه بفرستید و طبق پیام ابزار پایان دهید.
• برش ویدیو: ویدیو و بازهٔ زمانی را طبق نمونهٔ ابزار بفرستید.
• تبدیل قالب: عکس JPG/PNG/WebP به عکس یا PDF؛ صدا به MP3/M4A/OGG/Opus؛ ویدیو به MP4/WebM یا استخراج MP3. دکمهٔ تبدیل زیر فایل یا پاسخ /convert را بزنید. سقف ورودی ۲۰ مگابایت، مدت ۳ دقیقه و دکمه‌ها ۳۰ دقیقه اعتبار دارند.
• ترجمه: ابتدا زبان را انتخاب کنید و بعد متن را ارسال کنید.

اگر فایل را پیش از انتخاب ابزار فرستاده‌اید، پس از انتخاب ابزار دوباره آن را بفرستید.""",
              [("🧰 همهٔ ابزارها", "mode_tools"), ("🖼 متن از عکس", "mode_ocr"), ("🌐 ترجمه", "mode_translate")]),
    "library": ("⭐ ذخیره‌ها و اشتراک‌ها", """⭐ ذخیره‌ها و اشتراک‌ها

کتابخانه: از دکمهٔ ذخیره، هرجا نمایش داده شد، برای نگه‌داشتن لینک استفاده کنید. ذخیرهٔ لینک به معنی نگه‌داری دائمی فایل نیست.
تاریخچه: درخواست‌های اخیر و گزینهٔ تلاش دوباره را ببینید؛ دسترسی به منبع ممکن است تغییر کرده باشد.
اخبار: برای دریافت خبرهای تازه، فید را مشترک شوید. برای لغو، «اشتراک‌های من» را باز کنید، فید را انتخاب و لغو را تأیید کنید.

جزئیات نگه‌داری اطلاعات در بخش حریم خصوصی آمده است.""",
                [("⭐ کتابخانه و تاریخچه", "library"), ("🔔 اشتراک‌های من", "rss_subscriptions"), ("🔒 حریم خصوصی", "privacy")]),
    "problems": ("🛠 رفع مشکل", """🛠 اگر درخواست انجام نشد

• دکمه منقضی شده: ابزار را دوباره باز کنید و جستجو یا لینک را مجدداً بفرستید؛ این پیام لزوماً به معنی حذف ذخیره‌های شما نیست.
• لینک دریافت نشد: بررسی کنید لینک کامل، عمومی و همچنان قابل بازشدن باشد؛ سپس دوباره تلاش کنید.
• فایل پردازش نشد: نوع فایل و اطلاعات درخواستی ابزار را بررسی کنید؛ تصویر واضح‌تر یا فایل کوچک‌تر هم ممکن است کمک کند.
• ارسال چندبخشی متوقف شد: فایل کامل تحویل نشده است؛ پیام نهایی را بخوانید و دوباره تلاش کنید.
• محدودیت استفاده: بخش آمار کاربری را ببینید.
• ابزار اشتباه پاسخ می‌دهد: به منوی اصلی برگردید و ابزار درست را انتخاب کنید.

هنگام پردازش کمی صبر کنید؛ ارسال چندبارهٔ یک درخواست کمکی نمی‌کند. «انصراف» از مرحلهٔ ورودی خارج می‌شود و تضمین توقف کاری که شروع شده نیست.""",
                 [("📊 آمار کاربری", "stats"), ("⭐ تاریخچه و تلاش دوباره", "library_history")]),
}


def show_help(cid, topic=None):
    clear_state(cid)
    page = HELP_TOPICS.get(topic)
    if page:
        _, text, actions = page
        items = actions + [("🔙 موضوع‌های راهنما", "help"), ("🏠 منوی اصلی", "home")]
    else:
        text = HELP_TEXT
        items = [(label, "help_" + key) for key, (label, _, _) in HELP_TOPICS.items()]
        items.append(("🏠 منوی اصلی", "home"))
    send_message(cid, text, reply_markup={"inline_keyboard": [
        [{"text": label, "callback_data": callback}] for label, callback in items
    ]})

PRIVACY_TEXT = """🔒 *حریم خصوصی بله قربان*

این ربات فایل یا محتوای کامل پیام‌های شما را به‌صورت دائمی ذخیره نمی‌کند.

📌 *چه چیزی نگه‌داری می‌شود؟*
• پیام‌ها و فایل‌ها فقط برای اجرای همان درخواست پردازش می‌شوند.
• نتایج و لینک‌های موقت برای کارکرد دکمه‌ها در حافظه نگه‌داری می‌شوند و با راه‌اندازی مجدد پاک می‌شوند.
• آمار استفاده، تاریخچهٔ کوتاه، لینک‌های ذخیره‌شده در «کتابخانه من» و تنظیمات اشتراک RSS فقط به‌صورت فراداده در پایگاه‌دادهٔ محلی نگه‌داری می‌شوند.
• محتوای پیام‌ها، متن فایل‌ها و فایل‌های رسانه‌ای در پایگاه‌داده ذخیره نمی‌شوند؛ فایل‌های موقت پس از پردازش پاک می‌شوند.
• تاریخچه و کتابخانه سقف مشخص دارند و فقط اطلاعات لازم برای تلاش دوباره و مدیریت موارد ذخیره‌شده نگه‌داری می‌شوند.

🌐 *سرویس‌های خارجی*
ربات برای انجام کارها به سرویس‌هایی مثل DuckDuckGo، Wikipedia، YouTube، GitHub و سرویس ترجمه درخواست می‌فرستد. این سرویس‌ها سیاست حریم خصوصی خودشان را دارند.

🛡 *امنیت*
• ربات روی سرور اختصاصی اجرا می‌شود.
• توکن ربات محرمانه است و در کد قرار نمی‌گیرد.

📩 *سوال دارید؟*
با مدیر ربات در تماس باشید."""

# ═══════════════════════════════════════════════════════════════════════════════
# USER LIBRARY / HISTORY
# ═══════════════════════════════════════════════════════════════════════════════

def _library_list(cid: int, watchlist_only: bool = False, page: int = 0) -> None:
    page_size = 5
    items = durable_store.list_library_items(
        _platform(), cid, limit=page_size + 1, watchlist_only=watchlist_only,
        offset=page * page_size,
    )
    has_next = len(items) > page_size
    items = items[:page_size]
    if not items:
        label = "فهرست تماشا" if watchlist_only else "کتابخانه"
        send_message(cid, f"📭 {label} هنوز خالی است.", reply_markup=library_kb())
        return
    title = "👀 فهرست تماشا" if watchlist_only else "⭐️ کتابخانه من"
    send_message(cid, f"{title} — فقط اطلاعات لینک ذخیره می‌شود:",
                 reply_markup=_library_items_kb(
                     items, page=page, has_next=has_next, watchlist_only=watchlist_only
                 ))


def _history_label(entry):
    names = {"pdf_extract": "جداکردن صفحات PDF", "pdf_merge": "ادغام PDF", "video_trim": "برش ویدیو",
             "video_compress": "فشرده‌سازی ویدیو", "image_compress": "فشرده‌سازی عکس", "rss_digest": "خلاصه اخبار",
             "downloads": "دانلود", "searches": "جستجو", "translations": "ترجمه", "ocr": "متن از عکس", "requests": "درخواست"}
    action = names.get(str(entry.get("action")), "عملیات")
    details = entry.get("details") or {}
    title = details.get("title") or details.get("file_name") or urllib.parse.urlparse(str(details.get("url") or "")).netloc
    stamp = entry.get("created_at") or ""
    if isinstance(stamp, (int, float)):
        stamp = datetime.fromtimestamp(stamp).strftime("%m/%d %H:%M")
    return f"{action} — {str(title).replace(chr(10), ' ')[:36]} {str(stamp)[:16]}".strip(" —")


def _library_history(cid: int, page: int = 0) -> None:
    page_size = 5
    entries = durable_store.get_usage_history(
        _platform(), cid, limit=page_size + 1, offset=page * page_size
    )
    if not entries:
        if page > 0:
            send_message(cid, "📄 صفحهٔ دیگری در تاریخچه وجود ندارد.", reply_markup=library_kb())
        else:
            send_message(cid, "📭 تاریخچه‌ای ثبت نشده است.", reply_markup=library_kb())
        return
    has_next = len(entries) > page_size
    entries = entries[:page_size]
    rows = []
    for entry in entries:
        action = _history_label(entry)
        rows.append([{"text": f"🕘 {action}", "callback_data": f"hist_item_{int(entry['id'])}"}])
    nav = []
    if page > 0:
        nav.append({"text": "◀️ قبلی", "callback_data": f"hist_page_{page - 1}"})
    if has_next:
        nav.append({"text": "بعدی ▶️", "callback_data": f"hist_page_{page + 1}"})
    if nav:
        rows.append(nav)
    rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
    send_message(
        cid, "🕘 آخرین عملیات‌ها — برای تلاش دوباره انتخاب کنید:",
        reply_markup={"inline_keyboard": rows},
    )


def _library_item(cid: int, item_id: int) -> None:
    item = durable_store.get_library_item(_platform(), cid, item_id)
    if not item:
        _status(cid, "❌ این مورد دیگر در کتابخانه نیست.", final=True)
        return
    text = f"📌 {item.get('title') or 'مورد ذخیره‌شده'}"
    if item.get("details", {}).get("platform"):
        text += f"\nمنبع: {item['details']['platform']}"
    rows = [
        [
            {"text": "📥 تلاش دوباره", "callback_data": f"lib_retry_{int(item['id'])}"},
            {"text": "👀 فهرست تماشا", "callback_data": f"lib_watch_{int(item['id'])}"},
        ]
    ]
    if item.get("url"):
        rows.insert(0, [{"text": "🔗 باز کردن لینک", "url": item["url"]}])
    rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
    send_message(cid, text, reply_markup={"inline_keyboard": rows})


def _history_item(cid: int, entry_id: int) -> None:
    entry = durable_store.get_usage_entry(_platform(), cid, entry_id)
    if not entry:
        _status(cid, "❌ این تاریخچه منقضی شده است.", final=True)
        return
    details = entry.get("details") or {}
    url = str(details.get("url") or "")
    if not url.startswith("http"):
        send_message(cid, f"🕘 {_history_label(entry)}", reply_markup=library_kb())
        return
    rows = [
        [{"text": "📥 تلاش دوباره", "callback_data": f"hist_retry_{int(entry['id'])}"}],
        [{"text": "🏠 منوی اصلی", "callback_data": "home"}],
    ]
    send_message(cid, "🔁 می‌خواهید همین لینک را دوباره امتحان کنید؟",
                 reply_markup={"inline_keyboard": rows})


def _retry_url(cid: int, url: str) -> None:
    if not url.startswith("http"):
        _status(cid, "❌ لینک قبلی معتبر نیست.", final=True)
        return
    set_state(cid, mode=None)
    # Reuse the existing smart URL router without inventing a second download path.
    handle_message({"chat": {"id": cid}, "text": url})


def _save_library_url(cid: int, platform: str, url_key: str) -> None:
    url = get_url_for_chat(url_key, cid)
    if not url:
        _status(cid, "❔ لینک منقضی شده است.", final=True)
        return
    durable_store.add_library_item(
        _platform(), cid, title=f"{platform} — {url[:80]}", url=url,
        kind="saved", details={"platform": platform},
    )
    _status(cid, "✅ در کتابخانه ذخیره شد (فقط لینک، بدون فایل).", final=True)


def _rss_subscriptions(cid: int, page: int = 0) -> None:
    page_size = 5
    subs = durable_store.get_subscriptions(
        _platform(), cid, limit=page_size + 1, offset=page * page_size
    )
    has_next = len(subs) > page_size
    subs = subs[:page_size]
    if not subs:
        send_message(cid, "📭 هنوز هیچ فیدی را دنبال نمی‌کنید.", reply_markup=rss_menu_kb())
        return
    rows = []
    for sub in subs:
        url = str(sub.get("topic") or "")
        label = url.replace("https://", "")[:46]
        url_key = store_url(url, chat_id=cid)
        rows.append([{"text": f"🔔 {label}", "callback_data": f"rss_unsub_{url_key}"}])
    nav = []
    if page > 0:
        nav.append({"text": "◀️ قبلی", "callback_data": f"rss_subs_page_{page - 1}"})
    if has_next:
        nav.append({"text": "بعدی ▶️", "callback_data": f"rss_subs_page_{page + 1}"})
    if nav:
        rows.append(nav)
    rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
    send_message(cid, "🔔 اشتراک‌های RSS شما:", reply_markup={"inline_keyboard": rows})


def _rss_subscribe(cid: int, url: str) -> None:
    url = url.strip()
    if not url.startswith("http"):
        _status(cid, "❌ آدرس فید باید با http:// یا https:// شروع شود.", final=True)
        return
    if not any(token in url.lower() for token in ("rss", "feed", "atom", "xml")):
        feeds = rss_search_feeds(url)
        if feeds:
            url = feeds[0]
    items = rss_fetch(url, limit=3)
    if not items:
        _status(cid, "❌ فید معتبر پیدا نشد؛ آدرس RSS را بررسی کنید.", final=True)
        return
    try:
        durable_store.subscribe(
            _platform(), cid, url,
            {"interval_seconds": 900, "next_check_at": int(time.time()) + 900, "seen": []},
        )
    except ValueError as e:
        if str(e) == "RSS_MAX_SUBS_PER_USER":
            _status(cid, "⚠️ حداکثر ۱۰ فید برای هر کاربر فعال است.", final=True)
        elif str(e) == "RSS_MAX_SUBS_TOTAL":
            _status(cid, "⚠️ ظرفیت اشتراک فعلاً تکمیل است؛ کمی بعد دوباره امتحان کنید.", final=True)
        else:
            log.warning("RSS subscribe failed: %s", e)
            _status(cid, "❌ ثبت اشتراک انجام نشد؛ دوباره تلاش کنید.", final=True)
        return
    _status(cid, "✅ اشتراک فید ثبت شد؛ خبرهای جدید در خلاصه‌های بعدی ارسال می‌شوند.", final=True)


def _rss_digest_text(items: list[dict]) -> str:
    lines = ["📰 خلاصه اخبار جدید"]
    for item in items[:5]:
        title = str(item.get("title") or "خبر بدون عنوان").replace("\n", " ").strip()
        line = f"• {title[:180]}"
        link = str(item.get("link") or "").strip()
        if link:
            line += f"\n  {link[:500]}"
        lines.append(line)
    return "\n".join(lines)[:3900]


def _rss_item_key(item: dict) -> str:
    link = str(item.get("link") or "").strip()
    if link:
        return link[:300]
    return f"{item.get('title', '')}|{item.get('published', '')}"[:300]


def _rss_interval(config: dict) -> int:
    try:
        return max(300, min(int(config.get("interval_seconds", 900)), 86400))
    except (TypeError, ValueError):
        return 900


def _process_rss_once(now: Optional[int] = None) -> int:
    """Process due RSS feeds once, sequentially, without media or a queue."""
    now = int(time.time() if now is None else now)
    delivered = 0
    topic_items = {}
    for subscription in durable_store.get_due_subscriptions(now=now, limit=20):
        platform = str(subscription.get("platform") or "")
        try:
            cid = int(subscription.get("chat_id"))
        except (TypeError, ValueError):
            continue
        topic = str(subscription.get("topic") or "").strip()
        config = dict(subscription.get("config") or {})
        seen = [str(x)[:300] for x in (config.get("seen") or []) if x][-30:]
        seen_set = set(seen)
        next_check = now + _rss_interval(config)
        if platform not in ("bale", "telegram") or not topic:
            continue
        claimed = False
        try:
            if topic not in topic_items:
                topic_items[topic] = rss_fetch(topic, limit=10)[:10]
            items = topic_items[topic]
            fresh = [item for item in items if _rss_item_key(item) not in seen_set]
            if not fresh:
                config["next_check_at"] = next_check
                durable_store.update_subscription_config(platform, cid, topic, config)
                continue
            if not durable_store.claim_subscription_delivery(platform, cid, now):
                config["next_check_at"] = next_check
                durable_store.update_subscription_config(platform, cid, topic, config)
                continue
            claimed = True
            _set_platform_ctx(platform)
            _ctx.chat_id = cid
            response = send_message(cid, _rss_digest_text(fresh), reply_markup=home_kb())
            if not (response or {}).get("ok"):
                durable_store.release_subscription_delivery(platform, cid, now)
                claimed = False
                config["next_check_at"] = next_check
                durable_store.update_subscription_config(platform, cid, topic, config)
                continue
            config["seen"] = (seen + [_rss_item_key(item) for item in fresh])[-30:]
            config["next_check_at"] = next_check
            durable_store.update_subscription_config(platform, cid, topic, config)
            _log_history(
                cid, "rss_digest", topic, items=len(fresh)
            )
            delivered += 1
        except Exception as e:
            log.warning("RSS digest failed for %s: %s", topic, e)
            if claimed:
                try:
                    durable_store.release_subscription_delivery(platform, cid, now)
                except Exception:
                    log.warning("RSS quota release failed for %s", topic, exc_info=True)
            try:
                config["next_check_at"] = next_check
                durable_store.update_subscription_config(platform, cid, topic, config)
            except Exception:
                log.warning("RSS next-check update failed for %s", topic, exc_info=True)
    return delivered


_RSS_POLL_SECONDS = max(300, int(os.getenv("RSS_POLL_SECONDS", "900")))
_rss_stop = threading.Event()
_rss_thread: Optional[threading.Thread] = None


def _rss_scheduler_loop() -> None:
    while not _rss_stop.is_set():
        try:
            _process_rss_once()
            durable_store.cleanup_expired()
        except Exception:
            log.error("RSS scheduler pass failed", exc_info=True)
        _rss_stop.wait(_RSS_POLL_SECONDS)


def _start_rss_scheduler() -> Optional[threading.Thread]:
    global _rss_thread
    if _rss_thread is not None and _rss_thread.is_alive():
        return _rss_thread
    _rss_stop.clear()
    _rss_thread = threading.Thread(
        target=_rss_scheduler_loop, name="rss-scheduler", daemon=True
    )
    _rss_thread.start()
    return _rss_thread


# ═══════════════════════════════════════════════════════════════════════════════
# URL KEY CACHE  (for site-view button callbacks)
# ═══════════════════════════════════════════════════════════════════════════════
url_cache: dict[str, str] = {}  # short_key → full_url
url_owners: dict[str, tuple[str, int]] = {}  # short_key → (platform, chat_id)
URL_CACHE_MAX = 2000


def _prune_url_cache() -> None:
    """Bound callback URL memory; caller holds ``_url_lock``."""
    while len(url_cache) > URL_CACHE_MAX:
        key = next(iter(url_cache), None)
        if key is None:
            break
        url_cache.pop(key, None)
        url_owners.pop(key, None)


def store_url(url: str, chat_id: int = None) -> str:
    """Store URL in cache with the creating platform+chat owner."""
    if chat_id is None:
        chat_id = getattr(_ctx, "chat_id", None)
    key = secrets.token_hex(6)
    with _url_lock:
        url_cache[key] = url
        if chat_id is not None:
            url_owners[key] = (_platform(), int(chat_id))
        _prune_url_cache()
    return key


def get_url(key: str) -> Optional[str]:
    with _url_lock:
        return url_cache.get(key)


def get_url_chat(key: str) -> Optional[int]:
    """Get stored chat_id for a URL key."""
    with _url_lock:
        owner = url_owners.get(key)
        return owner[1] if owner else None


def get_url_for_chat(key: str, chat_id: int) -> Optional[str]:
    """Resolve a callback URL only for the creating platform+chat."""
    with _url_lock:
        if url_owners.get(key) != (_platform(), int(chat_id)):
            return None
        return url_cache.get(key)


# ═══════════════════════════════════════════════════════════════════════════════
# DO_ HANDLERS
# ═══════════════════════════════════════════════════════════════════════════════
def _cache_search(cid, kind, query, page, results):
    key = make_cache_key(kind, f"{_platform()}:{cid}:{query}", page)
    cache_set_owned(key, [{"kind": kind, "query": query, "page": page, "results": results,
                           "expires_at": time.time() + durable_store.CACHE_TTL}], _platform(), cid)
    return key


def _search_source(cid, key):
    source = cache_get_owned(key, _platform(), cid)
    return source[0] if source and source[0].get("expires_at", 0) > time.time() else None


def _show_search_source(cid, source, key):
    results, page, kind = source["results"], source["page"], source["kind"]
    send_message(cid, f"🔎 نتایج: {source['query']} — صفحه {page + 1}",
                 reply_markup=search_results_kb(results, key, page, len(results) >= (10 if kind == "ws" else 8), kind))


def do_search(cid: int, query: str, page=0):
    bump(cid, "searches")
    chat_action(cid)
    results = web_search(query, 10, page)
    key = _cache_search(cid, "ws", query, page, results)
    set_state(cid, mode="search", last_query=query, page=page, cache_key=key)
    if not results:
        _status(cid, "❌ نتیجه‌ای یافت نشد.", reply_markup=home_kb(), final=True)
        return
    text = f"🔎 *نتایج جستجو:* _{query}_  (صفحه {page + 1})\nروی هر نتیجه کلیک کنید:"
    kb = search_results_kb(results, key, page, len(results) == 10, "ws")
    send_message(cid, text, parse_mode="Markdown", reply_markup=kb)


def do_scholar(cid: int, query: str, page=0):
    bump(cid, "searches")
    chat_action(cid)
    results = scholar_search(query, page)
    key = _cache_search(cid, "sc", query, page, results)
    set_state(cid, mode="scholar", last_query=query, page=page, cache_key=key)
    if not results:
        _status(cid, "❌ مقاله‌ای یافت نشد.", reply_markup=home_kb(), final=True)
        return
    text = f"📚 *Google Scholar:* _{query}_  (صفحه {page + 1})"
    kb = search_results_kb(results, key, page, len(results) >= 8, "sc")
    send_message(cid, text, parse_mode="Markdown", reply_markup=kb)


def unified_search(cid: int, query: str, page=0):
    """Unified search for both academic papers and books.
    Query format: 'paper:query' or 'book:query' or plain query (combined)
    """
    bump(cid, "searches")
    chat_action(cid)

    # Determine search type
    if ":" in query and query.split(":")[0] in ["paper", "book"]:
        search_type, clean_query = query.split(":", 1)
        search_type = search_type.strip().lower()
    else:
        search_type = "combined"
        clean_query = query

    # Store search state
    set_state(
        cid, mode="unified", last_query=clean_query, search_type=search_type, page=page
    )

    # Perform search based on type
    if search_type == "paper":
        return do_scholar(cid, clean_query, page)
    elif search_type == "book":
        return do_book_search(cid, clean_query)
    else:
        # Combined search - perform both searches
        _status(
            cid, f"⏳ در حال جستجوی ترکیبی: _{clean_query}_…", parse_mode="Markdown"
        )

        # Perform searches
        scholar_results = scholar_search(clean_query, page)
        book_results = libgen_search(clean_query, count=5)

        # Merge results
        combined_results = scholar_results + book_results

        # Cache and send results
        key = _cache_search(cid, "uni", clean_query, page, combined_results)

        if not combined_results:
            _status(cid, "❌ نتیجه‌ای یافت نشد.", reply_markup=home_kb(), final=True)
            return

        text = f"🔍 *جستجوی ترکیبی:* _{clean_query}_ (صفحه {page + 1})"
        kb = search_results_kb(
            combined_results, key, page, len(combined_results) >= 8, "uni"
        )
        send_message(cid, text, parse_mode="Markdown", reply_markup=kb)


def do_wiki_search(cid: int, query: str):
    bump(cid, "searches")
    chat_action(cid)
    results = wikipedia_search(query, "fa")
    lang = "fa"
    if not results:
        results = wikipedia_search(query, "en")
        lang = "en"
    if not results:
        _status(cid, "❌ مقاله‌ای در ویکی‌پدیا یافت نشد.", reply_markup=home_kb(), final=True)
        return
    key = make_cache_key("wk", query, 0)
    cache_set(key, results)
    set_state(cid, mode="wiki", last_query=query, cache_key=key, wiki_lang=lang)
    text = f"📖 *ویکی‌پدیا:* _{query}_\nروی مقاله کلیک کنید:"
    kb = wiki_result_kb(results, key, lang)
    send_message(cid, text, parse_mode="Markdown", reply_markup=kb)


def do_wiki_article(cid: int, title: str, lang: str):
    bump(cid, "searches")
    chat_action(cid)
    _status(cid, f"⏳ در حال دریافت مقاله _{title}_…", parse_mode="Markdown")
    text = wikipedia_article(title, lang)
    if not text and lang == "fa":
        text = wikipedia_article(title, "en")
    if not text:
        _status(cid, "❌ مقاله یافت نشد.", reply_markup=home_kb(), final=True)
        return
    send_message(cid, f"📖 *{title}*\n\n{text[:3500]}", parse_mode="Markdown")
    if len(text) > 3500:
        send_document(
            cid, text.encode("utf-8"), f"{title[:40]}.txt", caption="📄 متن کامل مقاله"
        )
    _status(cid, "✅", reply_markup=home_kb(), final=True)
    clear_state(cid)


def _is_youtube(url: str) -> bool:
    return any(
        x in url
        for x in [
            "youtube.com/watch",
            "youtu.be/",
            "youtube.com/shorts/",
            "youtube.com/live/",
            "m.youtube.com",
            "music.youtube.com",
        ]
    )


def do_open_url(cid: int, url: str):
    bump(cid, "downloads")
    if not url.startswith("http"):
        url = "https://" + url
    url_key = store_url(url, chat_id=cid)
    _status(cid, f"⏳ در حال گرفتن اسکرین‌شات از:\n{url}")
    chat_action(cid, "upload_photo")
    ss = screenshot_page(url)
    if ss:
        short_url = url[:60] + ("…" if len(url) > 60 else "")
        send_photo(
            cid, ss, caption=f"🌐 {short_url}", reply_markup=site_view_kb(url_key)
        )
    else:
        _status(
            cid,
            "⚠️ اسکرین‌شات ممکن نبود. از دکمه‌های زیر استفاده کنید:",
            reply_markup=site_view_kb(url_key),
            final=True,
        )
    clear_state(cid)


# Extensions treated as "direct file link" → offer download instead of screenshot
_DIRECT_FILE_EXTS = {
    "zip", "rar", "7z", "tar", "gz", "bz2", "xz", "pdf", "doc", "docx", "xls",
    "xlsx", "ppt", "pptx", "odt", "mp3", "m4a", "wav", "flac", "ogg", "opus",
    "mp4", "mkv", "avi", "mov", "webm", "apk", "exe", "msi", "iso", "dmg",
    "deb", "rpm", "appimage", "jar", "txt", "csv", "json", "xml", "yaml", "yml",
    "jpg", "jpeg", "png", "gif", "webp", "svg", "bmp", "psd", "epub", "mobi",
    "srt", "sub", "ttf", "otf", "woff", "woff2", "ipynb", "py", "js", "ts",
}


def _is_direct_file_url(url: str) -> bool:
    path = urllib.parse.urlparse(url).path
    name = path.rstrip("/").split("/")[-1]
    if "." not in name:
        return False
    ext = name.rsplit(".", 1)[-1].lower()
    return ext in _DIRECT_FILE_EXTS


def do_file_link(cid: int, url: str):
    """Direct file link → offer download-and-send or page screenshot."""
    url_key = store_url(url, chat_id=cid)
    kb = {
        "inline_keyboard": [
            [
                {
                    "text": "📥 دانلود و ارسال فایل",
                    "callback_data": f"dfile_dl_{url_key}",
                },
                {
                    "text": "🌐 اسکرین‌شات صفحه",
                    "callback_data": f"dfile_shot_{url_key}",
                },
            ],
            [{"text": "🏠 منوی اصلی", "callback_data": "home"}],
        ]
    }
    send_message(
        cid,
        f"📎 لینک فایل شناسایی شد:\n{url[:200]}\n\nچه کاری انجام دهم؟",
        reply_markup=kb,
    )


def do_direct_file_download(cid: int, url: str):
    """Download a direct file link and upload it to the user's chat."""
    bump(cid, "downloads")
    _status(cid, "⏳ در حال دانلود فایل…")
    chat_action(cid, "upload_document")
    data = download_bytes(url, 500 * 1024 * 1024)
    if not data:
        _status(
            cid,
            "❌ دانلود ناموفق. ممکن است فایل حجیم یا لینک محافظت‌شده باشد.",
            reply_markup=home_kb(), final=True,
        )
        return
    fname = urllib.parse.unquote(
        urllib.parse.urlparse(url).path.rstrip("/").split("/")[-1]
    ) or "file"
    if smart_send(cid, data, fname, caption=f"📥 {url[:60]}"):
        _status(cid, "✅ فایل ارسال شد.", reply_markup=home_kb(), final=True)
    else:
        _status(cid, "❌ ارسال ناموفق.", reply_markup=home_kb(), final=True)
    clear_state(cid)


def do_youtube_search_cmd(cid: int, query: str, page=0):
    bump(cid, "searches")
    chat_action(cid)
    results = youtube_search(query, 8)
    key = make_cache_key("yt", query, page)
    cache_set(key, results)
    set_state(cid, mode="yt_search", last_query=query, page=page, cache_key=key)
    if not results:
        _status(cid, "❌ ویدیویی یافت نشد.", reply_markup=home_kb(), final=True)
        return
    text = f"📺 *یوتیوب:* _{query}_\nروی ویدیو کلیک کنید:"
    kb = yt_results_kb(results, key, page, len(results) == 8)
    send_message(cid, text, parse_mode="Markdown", reply_markup=kb)


@_heavy_download
def do_youtube_clip(cid: int, text: str):
    match = re.match(r"(https?://\S+)\s+(\S+)\s*-\s*(\S+)$", text.strip())
    if not match or not _is_youtube(match.group(1)):
        _status(cid, "❌ قالب درست نیست. نمونه: لینک 01:20-03:45", reply_markup=home_kb(), final=True)
        return
    clip = _clip_range(match.group(2), match.group(3))
    if not clip:
        _status(cid, "❌ بازه معتبر نیست یا بیشتر از ۳۰ دقیقه است.", reply_markup=home_kb(), final=True)
        return
    _status(cid, "⏳ در حال دریافت بخش انتخاب‌شده…")
    chat_action(cid, "upload_video")
    _finish_video(cid, youtube_download(match.group(1), clip=clip))


def _playlist_quality_kb(key: str) -> dict:
    return {"inline_keyboard": [
        [{"text": "📹 360p", "callback_data": f"yt_plq_{key}_360"},
         {"text": "📹 480p", "callback_data": f"yt_plq_{key}_480"},
         {"text": "📹 720p", "callback_data": f"yt_plq_{key}_720"}],
        [{"text": "🎵 فقط صدا MP3", "callback_data": f"yt_plq_{key}_a"}],
        [{"text": "❌ لغو", "callback_data": "home"}],
    ]}


def _playlist_subtitle_kb(key: str, audio_only: bool) -> dict:
    kind = "a" if audio_only else "v"
    return {"inline_keyboard": [
        [{"text": "🇮🇷 فارسی", "callback_data": f"yt_pls_{key}_{kind}_fa"},
         {"text": "🇬🇧 English", "callback_data": f"yt_pls_{key}_{kind}_en"}],
        [{"text": "⏭ بدون زیرنویس", "callback_data": f"yt_pls_{key}_{kind}_none"}],
        [{"text": "❌ لغو", "callback_data": "home"}],
    ]}


def do_youtube_playlist(cid: int, url: str):
    """List at most ten videos, then collect one shared batch preference."""
    _status(cid, "⏳ در حال خواندن فهرست پخش…")
    items = youtube_playlist_items(url)
    if not items:
        _status(cid, "❌ فهرست پخش یافت نشد یا در دسترس نیست.", reply_markup=home_kb(), final=True)
        return
    key = make_cache_key("ytpl", url, 0)
    cache_set(key, items)
    send_message(
        cid,
        f"📃 فهرست پخش — {len(items)} ویدیوی اول\nیک کیفیت برای همه ویدیوها انتخاب کنید:",
        reply_markup=_playlist_quality_kb(key),
    )


def _playlist_download_one(cid: int, item: dict, max_height: int, audio_only: bool, sub_code: str) -> bool:
    result = youtube_download(item["url"], audio_only=audio_only, sub_code=sub_code, max_height=max_height)
    if not result:
        return False
    data, fname = result
    return bool(smart_send(cid, data, Path(fname).name, caption=item.get("title", "")[:200], media_type="audio" if audio_only else "video"))


@_heavy_download
def _run_youtube_playlist(cid: int, items: list[dict], max_height: int, audio_only: bool, sub_code: str):
    """Sequential batch runner; quota is claimed per item immediately before work."""
    sent = 0
    platform = _platform()
    for index, item in enumerate(items, 1):
        if not durable_store.claim_download(platform, cid):
            _status(cid, f"⚠️ سهمیه دانلود روزانه پایان یافت. {sent} از {len(items)} ارسال شد.", reply_markup=home_kb(), final=True)
            return
        _status(cid, f"⏳ ویدیوی {index}/{len(items)}: در حال دانلود…")
        if _playlist_download_one(cid, item, max_height, audio_only, sub_code):
            sent += 1
    _status(cid, f"✅ فهرست پخش کامل شد. {sent}/{len(items)} ارسال شد.", reply_markup=home_kb(), final=True)


def do_youtube_dl(cid: int, url: str):
    """Entry point for direct YouTube URL download — shows quality picker."""
    if not ("youtu.be" in url or "youtube.com" in url):
        _status(cid, "❌ لینک یوتیوب معتبر وارد کنید.", reply_markup=home_kb(), final=True)
        return
    if _is_youtube_playlist(url):
        do_youtube_playlist(cid, url)
        return
    bump(cid, "downloads")
    _youtube_quality_picker(cid, url)


@_heavy_download
def _youtube_quality_picker(cid: int, url: str, audio_only: bool = False):
    """Probe available formats and show quality selection keyboard to user.

    First sends thumbnail + description, then quality selector with filesize info.
    """
    _status(cid, "⏳ در حال بررسی کیفیت‌های موجود…")
    chat_action(cid)
    info = youtube_get_formats(url)
    if not info:
        # Probe failed — fall back to direct download
        _status(cid, "⚠️ بررسی کیفیت ممکن نبود. در حال دانلود با بهترین کیفیت…")
        chat_action(cid, "upload_video")
        result = youtube_download(url, audio_only=audio_only)
        _finish_video(cid, result)
        return

    # Cache info so callback can retrieve it.
    # info_key = "yti" + url_key (no underscores between fields in callback_data)
    url_key = store_url(url, chat_id=cid)
    info_key = "yti" + url_key  # e.g. "ytibdc4d2d4ad" — no internal _
    cache_set(info_key, [info])  # wrapped in list so cache_get works

    formats = info.get("formats", [])
    subtitles = info.get("subtitles", [])
    title = info.get("title", "")[:50]
    thumbnail = info.get("thumbnail", "")
    description = info.get("description", "") or ""
    duration = info.get("duration_string", "")

    # Truncate description to Telegram/Bale message limit (4096 chars), leaving
    # room for the title/duration lines prepended below.
    if len(description) > 3800:
        description = description[:3800] + "..."

    if audio_only or not formats:
        # Audio mode or no video formats found — skip quality picker
        _youtube_sub_picker(
            cid, url_key, info_key, fmt_spec="", client="", audio_only=True
        )
        return

    # First: Send thumbnail with description
    caption_parts = [f"📺 *{title}*"]
    if duration:
        caption_parts.append(f"⏱ مدت: {duration}")
    if description:
        caption_parts.append(f"\n{description}")
    caption = "\n".join(caption_parts)

    BALE_CAPTION_LIMIT = 1024
    inline_cap = caption if len(caption) <= BALE_CAPTION_LIMIT else ""

    if thumbnail:
        try:
            thumb_data = download_bytes(thumbnail)
            if thumb_data:
                smart_send(
                    cid, thumb_data, "thumbnail.jpg", caption=inline_cap, media_type="photo"
                )
            elif not inline_cap:
                send_message(cid, caption, parse_mode="Markdown")
        except Exception as e:
            log.debug("Failed to send thumbnail: %s", e)
    else:
        send_message(cid, caption if inline_cap else f"📺 *{title}*", parse_mode="Markdown")

    # Caption (with full description) too long for the photo caption limit —
    # send it as its own message so nothing gets cut off.
    if not inline_cap:
        send_message(cid, caption[:4096], parse_mode="Markdown")

    # Build quality keyboard with filesize info
    rows = []
    for i, fmt in enumerate(formats[:8]):  # cap at 8 rows
        label = fmt["label"]
        filesize_mb = fmt.get("filesize_mb")
        if filesize_mb:
            label_with_size = f"{label} • {filesize_mb}MB"
        else:
            label_with_size = label
        rows.append(
            [
                {
                    "text": f"📹 {label_with_size}",
                    "callback_data": f"yt_qual_{url_key}_{info_key}_{i}",
                }
            ]
        )

    rows.append(
        [
            {
                "text": "🎵 فقط صدا (MP3)",
                "callback_data": f"yt_qualA_{url_key}_{info_key}",
            }
        ]
    )
    rows.append([{"text": "❌ لغو", "callback_data": "home"}])

    text = "کیفیت دانلود را انتخاب کنید:"
    send_message(
        cid, text, parse_mode="Markdown", reply_markup={"inline_keyboard": rows}
    )


def _youtube_sub_picker(
    cid: int,
    url_key: str,
    info_key: str,
    fmt_spec: str,
    client: str,
    audio_only: bool = False,
    page: int = 0,
):
    """Show subtitle selection keyboard — 3-col, priority order, paginated."""
    info_list = cache_get(info_key)
    info = info_list[0] if info_list else {}
    subtitles = info.get("subtitles", [])
    title = info.get("title", "")[:50]

    if not subtitles:
        _youtube_execute_download(
            cid, url_key, info_key, fmt_spec, client, audio_only, sub_code=""
        )
        return

    # Sort: fa first, en second, then alphabetical
    def _sub_priority(s):
        c = s["code"].split("-")[0]
        if c == "fa":
            return "0"
        if c == "en":
            return "1"
        return "2" + c

    subs_sorted = sorted(subtitles, key=_sub_priority)

    # Pagination: fit (KB_MAX_ROWS - 2) full rows of KB_MAX_COLS per page
    per_page = (getattr(_ctx, "kb_rows", 10) - 2) * KB_MAX_COLS
    total = len(subs_sorted)
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(0, min(page, pages - 1))
    page_subs = subs_sorted[page * per_page : (page + 1) * per_page]

    def _sub_cb(code):
        sfmt = _safe_fmt(fmt_spec)
        enc_cli = client.replace("_", "~~")  # encode underscores in client name
        return f"yt_sub_{url_key}_{info_key}_{code}_{int(audio_only)}_{sfmt}_{enc_cli}"

    # Build 3-column rows
    rows = []
    row = []
    for sub in page_subs:
        row.append({"text": f"💬 {sub['name']}", "callback_data": _sub_cb(sub["code"])})
        if len(row) == KB_MAX_COLS:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    # Pagination nav row
    nav = []
    if page > 0:
        nav.append(
            {
                "text": "◀ قبلی",
                "callback_data": f"yt_subpg_{url_key}_{info_key}_{page - 1}_{int(audio_only)}_{_safe_fmt(fmt_spec)}_{client.replace(chr(95), chr(126) * 2)}",
            }
        )
    if page < pages - 1:
        nav.append(
            {
                "text": f"بعدی ▶ ({page + 1}/{pages})",
                "callback_data": f"yt_subpg_{url_key}_{info_key}_{page + 1}_{int(audio_only)}_{_safe_fmt(fmt_spec)}_{client.replace(chr(95), chr(126) * 2)}",
            }
        )
    if nav:
        rows.append(nav)

    # Fixed bottom row: no-sub + cancel
    rows.append(
        [
            {"text": "⏭ بدون زیرنویس", "callback_data": _sub_cb("NONE")},
            {"text": "❌ لغو", "callback_data": "home"},
        ]
    )

    pg_label = f" (صفحه {page + 1}/{pages})" if pages > 1 else ""
    send_message(
        cid,
        f"💬 *{title}*\n\nزیرنویس دلخواه را انتخاب کنید{pg_label}:",
        parse_mode="Markdown",
        reply_markup={"inline_keyboard": rows},
    )


def _safe_fmt(fmt_spec: str) -> str:
    """Encode fmt_spec for use in callback_data (replace + with ~, spaces with _)."""
    return fmt_spec.replace("+", "~").replace(" ", "_")


def _decode_fmt(encoded: str) -> str:
    """Reverse _safe_fmt encoding."""
    return encoded.replace("~", "+").replace("_", " ")


@_heavy_download
def _youtube_execute_download(
    cid: int,
    url_key: str,
    info_key: str,
    fmt_spec: str,
    client: str,
    audio_only: bool,
    sub_code: str,
):
    """Final step: perform the actual download and send to user."""
    url = get_url_for_chat(url_key, cid) or ""
    target_cid = cid
    if not url:
        _status(
            target_cid, "❌ لینک منقضی شده. دوباره امتحان کنید.", reply_markup=home_kb(), final=True)
        return

    info_list = cache_get(info_key)
    info = info_list[0] if info_list else {}
    title = info.get("title", "")

    sub_label = f" + زیرنویس ({sub_code})" if sub_code else ""
    label = "🎵 صدا" if audio_only else "📺 ویدیو"
    _status(
        target_cid,
        f"⏳ در حال دانلود {label}{sub_label}…\n_(ممکن است چند دقیقه طول بکشد)_",
        parse_mode="Markdown",
    )
    chat_action(target_cid, "upload_video" if not audio_only else "upload_voice")

    result = youtube_download(
        url,
        audio_only=audio_only,
        fmt_spec=fmt_spec,
        sub_code=sub_code if sub_code != "NONE" else "",
        yt_client=client,
    )
    _finish_video(target_cid, result, audio_only=audio_only)


def _finish_video(cid: int, result, audio_only: bool = False):
    if not result:
        _status(
            cid,
            "❌ دانلود ناموفق بود.\n"
            "• ویدیو ممکن است در دسترس نباشد\n"
            "• ویدیوی دیگری امتحان کنید",
            parse_mode="Markdown",
            reply_markup=home_kb(), final=True)
        return
    data, fname = result
    fname = Path(fname).name
    size_mb = len(data) / 1024 / 1024
    log.info("_finish_video: %s  %.1fMB", fname, size_mb)
    if smart_send(cid, data, fname, caption="", media_type="audio" if audio_only else "video"):
        _status(cid, f"✅ ارسال شد.", reply_markup=home_kb(), final=True)
    else:
        _status(cid,
                "❌ ارسال فایل ناموفق بود.\n"
                "• فایل ممکن است از حد حجم مجاز بزرگ‌تر باشد\n"
                "• کیفیت پایین‌تر را امتحان کنید یا کمی بعد دوباره تلاش کنید",
                reply_markup=home_kb(), final=True)
    clear_state(cid)


def do_music(cid: int, query: str):
    """
    دانلود موزیک — جستجو در همه پلتفرم‌ها یا دانلود مستقیم از لینک.
    """
    bump(cid, "downloads")

    # لینک مستقیم — دانلود فوری
    if query.startswith("http"):
        if "spotify.com" in query:
            do_spotify_dl(cid, query)
            return
        elif _is_youtube(query) or "soundcloud.com" in query:
            _do_audio_download(
                cid,
                query,
                source="soundcloud" if "soundcloud.com" in query else "youtube",
            )
            return
        else:
            _do_audio_download(cid, query, source="auto")
            return

    # جستجو — نمایش نتایج به صورت دکمه
    _status(
        cid, f"🔍 در حال جستجو در همه پلتفرم‌ها: _{query}_…", parse_mode="Markdown"
    )
    chat_action(cid)
    results = music_search_multi(query)

    if not results:
        _status(
            cid, "❌ نتیجه‌ای یافت نشد. عبارت دیگری امتحان کنید.", reply_markup=home_kb(), final=True)
        return

    key = make_cache_key("mu", query, 0)
    cache_set(key, results)
    set_state(cid, mode="music", last_query=query, cache_key=key)

    rows = []
    for i, r in enumerate(results):
        title = r.get("title", f"آهنگ {i + 1}")[:35]
        uploader = r.get("uploader", "")[:18]
        dur = r.get("duration", "")
        source = r.get("source", "")
        src_icon = {
            "youtube": "▶️",
            "ytmusic": "🎵",
            "soundcloud": "☁️",
            "jiosaavn": "🎶",
            "deezer": "🎧",
        }.get(source, "🎵")
        parts = [f"{src_icon} {title}"]
        if uploader:
            parts.append(f"— {uploader}")
        if dur:
            parts.append(f"({dur})")
        label = " ".join(parts)[:60]
        rows.append([{"text": label, "callback_data": f"mu_dl_{key}_{i}"}])

    rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
    _status(
        cid,
        f"🎵 *نتایج موزیک برای:* _{query}_\nروی آهنگ بزنید تا دانلود شود:",
        parse_mode="Markdown",
        reply_markup={"inline_keyboard": rows}, final=True)


@_heavy_download
def _do_audio_download(
    cid: int,
    url: str,
    source: str = "auto",
    title: str = "",
    artist: str = "",
    track_item: dict = None,
    audio_format: str = "mp3",
    audio_quality: str = "320",
):
    """دانلود صدا از URL یا جستجو با زنجیره fallback کامل."""
    bump(cid, "downloads")
    chat_action(cid, "record_voice")

    def status_cb(text: str, reply_markup=None, parse_mode=None, final=False):
        _status(cid, text, reply_markup=reply_markup, parse_mode=parse_mode, final=final)

    status_cb("⏳ در حال دانلود آهنگ…")
    tried_ids: set = set()

    # JioSaavn و Deezer نیاز به تابع دانلود اختصاصی دارند
    if source == "jiosaavn" and track_item:
        result = jiosaavn_download(track_item)
        if not result:
            result = music_download_with_fallback(
                title or url,
                title=title,
                artist=artist,
                source_hint=source,
                status_cb=status_cb,
                _tried_yt_ids=tried_ids,
                duration=(track_item or {}).get("duration"),
            )
    elif source == "deezer" and track_item:
        result = deezer_download(track_item)
        if not result:
            result = music_download_with_fallback(
                title or url,
                title=title,
                artist=artist,
                source_hint=source,
                status_cb=status_cb,
                _tried_yt_ids=tried_ids,
                duration=(track_item or {}).get("duration"),
            )
    else:
        # برای YouTube/SoundCloud — یک بار مستقیم امتحان، سپس fallback
        # Extract YT video ID so fallback doesn't retry the same video
        if "youtube.com" in url or "youtu.be" in url:
            import urllib.parse as _up

            qs = _up.parse_qs(_up.urlparse(url).query)
            vid = qs.get("v", [""])[0] or url
            tried_ids.add(vid)
        result = music_download_ytdlp(url, source=source, audio_format=audio_format, audio_quality=audio_quality)
        if not result:
            status_cb("⚠️ دانلود مستقیم ناموفق — در حال جستجو در منابع دیگر…")
            result = music_download_with_fallback(
                url,
                title=title,
                artist=artist,
                source_hint=source,
                status_cb=status_cb,
                _tried_yt_ids=tried_ids,
                duration=(track_item or {}).get("duration"),
            )

    if not result:
        status_cb(
            "❌ دانلود آهنگ ناموفق بود.\n"
            "• عبارت دیگری جستجو کنید\n"
            "• ممکن است محتوا در همه منابع محدودیت داشته باشد",
            reply_markup=home_kb(), final=True)
        return

    data, fname = result
    fname = Path(fname).name
    caption = f"🎵 {title or fname[:60]}"
    if smart_send(cid, data, fname, caption=caption, media_type="audio"):
        _status(cid, "✅ ارسال شد!", final=True)
    else:
        _status(cid, "❌ ارسال فایل ناموفق بود.", final=True)
    clear_state(cid)


@_heavy_download
def do_spotify_dl(cid: int, url: str):
    """Download Spotify track/album/playlist via spotdl, with full platform fallback."""
    bump(cid, "downloads")
    is_track = "/track/" in url
    is_album = "/album/" in url
    is_playlist = "/playlist/" in url
    kind = (
        "آهنگ"
        if is_track
        else "آلبوم"
        if is_album
        else "پلیلیست"
        if is_playlist
        else "محتوا"
    )

    _status(cid, f"⏳ در حال دانلود {kind} از Spotify…")
    chat_action(cid, "record_voice")

    def status_cb(text: str):
        _status(cid, text)

    # For single tracks, use full fallback chain; for albums/playlists only spotdl
    if is_track:
        result = music_download_with_fallback(
            url, source_hint="spotify", status_cb=status_cb
        )
    else:
        result = spotify_download(url)

    if not result:
        _status(
            cid,
            "❌ دانلود از Spotify ناموفق بود.\n\n"
            "کمی بعد دوباره امتحان کنید یا نام آهنگ را در بخش موسیقی جستجو کنید.",
            parse_mode="Markdown",
            reply_markup=home_kb(), final=True)
        return

    data, fname = result
    caption = f"🎵 {fname[:60]}"
    if smart_send(cid, data, fname, caption=caption):
        _status(cid, "✅ ارسال شد!", reply_markup=home_kb(), final=True)
    else:
        _status(cid, "❌ ارسال ناموفق بود.", reply_markup=home_kb(), final=True)
    clear_state(cid)


def do_images(cid: int, query: str, source: str, page=0):
    bump(cid, "searches")
    chat_action(cid, "upload_photo")
    source_names = {
        "multi": "🌐 چند منبع (پیشنهادی)",
        "ddg": "🦆 DuckDuckGo",
        "bing": "🖼 Bing",
        "google": "🔍 Google",
        "pinterest": "📌 پینترست",
        "pexels": "📷 Pixabay/Pexels",
        "wiki": "🎨 Wikimedia",
        "deviantart": "🎨 DeviantArt",
        "pixiv": "🎨 Pixiv",
        "artstation": "🎨 ArtStation",
        "flickr": "📷 Flickr",
        "tumblr": "📝 Tumblr",
        "imgur": "📷 Imgur",
        "kemono": "🦊 Kemono",
    }
    _status(
        cid,
        f"⏳ جستجوی عکس [{source_names.get(source, source)}]: _{query}_…",
        parse_mode="Markdown",
    )
    offset = page * 6
    fn_map = {
        "multi": images_multi,
        "ddg": images_duckduckgo,
        "bing": images_bing,
        "google": images_google,
        "pinterest": images_pinterest,
        "pexels": images_pexels,
        "wiki": images_wikimedia,
        "deviantart": images_deviantart,
        "pixiv": images_pixiv,
        "artstation": images_artstation,
        "flickr": images_flickr,
        "tumblr": images_tumblr,
        "imgur": images_imgur,
        "kemono": images_kemono,
    }
    fn = fn_map.get(source, images_multi)
    results = fn(query, max_results=offset + 8)
    page_results = results[offset : offset + 6]
    key = make_cache_key(f"img_{source}", query, page)
    cache_set(key, results)
    set_state(
        cid,
        mode=f"img_{source}",
        last_query=query,
        page=page,
        img_source=source,
        cache_key=key,
    )
    if not page_results:
        _status(cid, "❌ تصویری یافت نشد.", reply_markup=home_kb(), final=True)
        return

    # Build media group items
    media_items = []
    for r in page_results[:6]:
        try:
            hdrs = {"Referer": r["_referer"]} if r.get("_referer") else None
            img_bytes = r.get("_bytes") or download_bytes(
                r["url"], MAX_IMAGE_SIZE, headers=hdrs
            )
            if not img_bytes or len(img_bytes) <= 1000:
                log.warning(
                    "do_images: skip %s — download failed or too small (%s bytes)",
                    r.get("url"),
                    len(img_bytes) if img_bytes else 0,
                )
                continue
            media_items.append({
                "data": img_bytes,
                "fname": f"img_{len(media_items)}.jpg",
                "is_video": False,
                "caption": r.get("title", "")[:80] if len(media_items) == 0 else "",
            })
        except Exception as ex:
            log.error("do_images prepare: %s", ex, exc_info=True)

    if media_items:
        if len(media_items) == 1:
            # Single photo - use send_photo with keyboard
            send_photo(cid, media_items[0]["data"], caption=media_items[0]["caption"])
        else:
            # Multiple photos - send as album
            send_media_group(cid, media_items)
        sent = len(media_items)
    else:
        sent = 0

    kb = images_more_kb(key, page, source)
    _status(cid, f"✅ {sent} عکس ارسال شد.", reply_markup=kb, final=True)


def do_github_search(cid: int, query: str, page=0):
    bump(cid, "searches")
    chat_action(cid)
    results = github_search_repos(query, page)
    key = make_cache_key("gh", query, page)
    cache_set(key, results)
    set_state(cid, mode="gh_search", last_query=query, page=page, cache_key=key)
    if not results:
        _status(cid, "❌ مخزنی یافت نشد.", reply_markup=home_kb(), final=True)
        return
    text = f"🐙 *GitHub:* _{query}_  (صفحه {page + 1})\nروی مخزن کلیک کنید:"
    kb = gh_repo_kb(results, key, page, len(results) == 8)
    send_message(cid, text, parse_mode="Markdown", reply_markup=kb)


def do_github_zip(cid: int, url: str):
    bump(cid, "downloads")
    _status(cid, "⏳ در حال دانلود ZIP مخزن…")
    data = github_zip(url)
    if not data:
        _status(cid, "❌ دانلود ناموفق.", reply_markup=home_kb(), final=True)
        return
    m = re.search(r"github\.com/([^/]+/[^/]+?)(?:\.git|/|$)", url)
    slug = m.group(1).replace("/", "_") if m else "repo"
    if send_document(cid, data, f"{slug}.zip", caption=f"📥 {url[:60]}"):
        _status(cid, "✅ ZIP ارسال شد.", reply_markup=home_kb(), final=True)
    else:
        _status(cid, "❌ ارسال ناموفق.", reply_markup=home_kb(), final=True)
    clear_state(cid)


def do_github_release(cid: int, repo_full: str, page: int = 0):
    """Show GitHub release assets with full pagination — no asset left behind."""
    bump(cid, "downloads")
    chat_action(cid)
    rel = github_latest_release(repo_full)
    if not rel:
        _status(cid,
                "❌ هیچ نسخهٔ منتشرشده‌ای (Release) برای این مخزن نیست.\n"
                "• شاید مخزن فقط سورس‌کد دارد — گزینهٔ «دانلود ZIP» را امتحان کنید\n"
                "• نام مخزن را بررسی کنید (مثال: owner/repo)",
                reply_markup=home_kb(), final=True)
        return

    tag = rel.get("tag_name", "")
    body = (rel.get("body") or "")[:400]
    assets = rel.get("assets", [])

    # Cache all assets
    safe_repo = repo_full.replace("/", "__")
    akey = f"ghrel_{safe_repo}"
    cache_set(akey, assets)

    # Build info text (only on page 0)
    if page == 0:
        lines_txt = [
            f"📦 *آخرین Release: {repo_full}*",
            f"🏷 نسخه: `{tag}`",
            f"📅 تاریخ: {rel.get('published_at', '')[:10]}",
        ]
        if body:
            lines_txt.append(f"\n{body}")
        if assets:
            lines_txt.append(f"\n*{len(assets)} فایل قابل دانلود:*")
            for i, a in enumerate(assets):
                size_mb = a.get("size", 0) // 1024 // 1024
                lines_txt.append(f"{i + 1}. {a['name']} ({size_mb}MB)")
        info_text = "\n".join(lines_txt)
    else:
        info_text = f"📦 *{repo_full}* — {tag}\n\nانتخاب فایل برای دانلود:"

    # Pagination: (KB_MAX_ROWS - 2) assets per page, 1 per row
    per_page = getattr(_ctx, "kb_rows", 10) - 2
    total = len(assets)
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(0, min(page, pages - 1))
    page_assets = assets[page * per_page : (page + 1) * per_page]

    rows = []
    for i, a in enumerate(page_assets):
        real_idx = page * per_page + i
        size_mb = a.get("size", 0) / 1024 / 1024
        rows.append(
            [
                {
                    "text": f"⬇️ {a['name'][:38]} ({size_mb:.0f}MB)",
                    "callback_data": f"ghrel_dl_{safe_repo}_{real_idx}",
                }
            ]
        )

    # Navigation row
    nav = []
    if page > 0:
        nav.append(
            {"text": "◀ قبلی", "callback_data": f"ghrel_pg_{safe_repo}_{page - 1}"}
        )
    if page < pages - 1:
        nav.append(
            {
                "text": f"بعدی ▶ ({page + 1}/{pages})",
                "callback_data": f"ghrel_pg_{safe_repo}_{page + 1}",
            }
        )
    if nav:
        rows.append(nav)

    rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])

    send_message(
        cid, info_text, parse_mode="Markdown", reply_markup={"inline_keyboard": rows}
    )


def do_ocr_photo(cid: int, photos: list, reply_id=None):
    bump(cid, "ocr")
    chat_action(cid)
    photo = sorted(photos, key=lambda p: p.get("file_size", 0))[-1]
    if photo.get("file_size", 0) > MAX_OCR_SIZE:
        _status(cid, "❌ حجم عکس بیشتر از ۵MB است.", final=True)
        return
    url = get_file_url(photo["file_id"])
    if not url:
        _status(cid, "❌ خطا در دریافت فایل.", final=True)
        return
    data = download_bytes(url, MAX_OCR_SIZE)
    if not data:
        _status(cid, "❌ خطا در دانلود عکس.", final=True)
        return
    _status(cid, "⏳ در حال پردازش تصویر…")
    extracted = ocr_image(data)
    send_message(
        cid,
        f"📝 *متن استخراج شده:*\n\n{extracted[:3500]}",
        parse_mode="Markdown",
        reply_to_message_id=reply_id,
    )
    try:
        pdf = ocr_to_pdf(extracted)
        send_document(cid, pdf, "ocr_result.pdf", caption="📑 متن OCR به‌صورت PDF")
    except Exception as e:
        log.error("ocr_to_pdf: %s", e)
    clear_state(cid)
    _status(cid, "✅ OCR انجام شد.", reply_markup=home_kb(), final=True)


def do_translate(cid: int, text: str, lang: str):
    bump(cid, "translations")
    chat_action(cid)
    result = translate_text(text, lang)
    send_message(
        cid, f"🌐 *ترجمه:*\n\n{result}", parse_mode="Markdown", reply_markup=home_kb()
    )
    clear_state(cid)


def do_currency(cid: int, text: str):
    m = re.match(
        r"([\d,\.]+)\s+([A-Za-z]{3})\s+(?:to\s+)?([A-Za-z]{3})",
        text.strip(),
        re.IGNORECASE,
    )
    if not m:
        _status(
            cid,
            "❌ فرمت اشتباه. مثال: `100 USD to IRR`",
            parse_mode="Markdown",
            reply_markup=home_kb(), final=True)
        return
    amount, frm, to = m.groups()
    amount = float(amount.replace(",", ""))
    result = currency_convert(amount, frm, to)
    send_message(
        cid,
        f"💱 *{result}*" if result else "❌ خطا در تبدیل.",
        parse_mode="Markdown",
        reply_markup=home_kb(),
    )
    clear_state(cid)


def do_ip_lookup(cid: int, target: str):
    chat_action(cid)
    result = ip_lookup(target.strip())
    send_message(cid, result, parse_mode="Markdown", reply_markup=home_kb())
    clear_state(cid)


def do_shorten(cid: int, url: str):
    if not url.startswith("http"):
        url = "https://" + url
    result = shorten_url(url)
    send_message(cid, f"🔗 {result}", reply_markup=home_kb())
    clear_state(cid)


def do_expand(cid: int, url: str):
    if not url.startswith("http"):
        url = "https://" + url
    result = expand_url(url)
    send_message(cid, result, parse_mode="Markdown", reply_markup=home_kb())
    clear_state(cid)


def do_paste(cid: int, text: str):
    url = paste_text(text)
    if url:
        send_message(cid, f"📋 آپلود شد:\n{url}", reply_markup=home_kb())
    else:
        _status(cid, "❌ خطا در آپلود.", reply_markup=home_kb(), final=True)
    clear_state(cid)


def do_qr(cid: int, text: str):
    chat_action(cid, "upload_photo")
    qr = generate_qr(text)
    if qr:
        send_photo(cid, qr, caption=f"📱 QR: {text[:60]}")
    else:
        _status(cid, "❌ خطا در ساخت QR.", final=True)
    _status(cid, "✅", reply_markup=home_kb(), final=True)
    clear_state(cid)


def _compress_image_bytes(data: bytes, quality: int = 72) -> tuple[bytes, str]:
    """Compress one image in memory; never persist a source or output file."""
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(data)) as source:
        source.load()
        image = ImageOps.exif_transpose(source)
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        # Pixel cap prevents decompression-bomb-like RAM spikes.
        image.thumbnail((4096, 4096), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=max(35, min(85, int(quality))),
                   optimize=True, progressive=True)
        return out.getvalue(), "compressed.jpg"


FILE_OPS_TTL = 900
FILE_OPS_MAX_ITEMS = 5
FILE_OPS_MAX_BYTES = 20 * 1024 * 1024
FILE_OPS_MAX_TOTAL_BYTES = 100 * 1024 * 1024
FILE_OPS_MAX_OWNERS = 100
PDF_MAX_BYTES = 20 * 1024 * 1024
VIDEO_MAX_INPUT_BYTES = 48 * 1024 * 1024
VIDEO_MAX_OUTPUT_BYTES = 48 * 1024 * 1024
_file_ops_cache: dict[tuple[str, int], dict] = {}
_file_ops_lock = threading.Lock()


def _pdf_operation_supported() -> bool:
    """Return whether the optional local PDF reader is importable."""
    try:
        import pypdf  # noqa: F401
    except (ImportError, ModuleNotFoundError):
        return False
    return True


def _parse_pdf_pages(spec: str, page_count: int) -> Optional[list[int]]:
    """Parse a small page selector such as ``1,3-5`` into zero-based indices."""
    text = str(spec or "").replace("،", ",").replace("٫", ",").strip()
    if not text or page_count < 1:
        return None
    pages: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        match = re.fullmatch(r"(\d+)(?:\s*[-–]\s*(\d+))?", part)
        if not match:
            return None
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start < 1 or end < start or start > page_count or end > page_count:
            return None
        pages.extend(range(start - 1, end))
    pages = list(dict.fromkeys(pages))
    return pages if 1 <= len(pages) <= min(page_count, 100) else None


def _pdf_extract_pages_bytes(data: bytes, page_spec: str) -> tuple[bytes, str]:
    """Extract selected PDF pages in memory with bounded input/output."""
    if not data or len(data) > PDF_MAX_BYTES or not _pdf_operation_supported():
        raise ValueError("unsupported_pdf")
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(data))
    selected = _parse_pdf_pages(page_spec, len(reader.pages))
    if not selected:
        raise ValueError("invalid_pages")
    writer = PdfWriter()
    for index in selected:
        writer.add_page(reader.pages[index])
    output = io.BytesIO()
    writer.write(output)
    result = output.getvalue()
    if not result or len(result) > PDF_MAX_BYTES:
        raise ValueError("pdf_too_large")
    return result, "selected_pages.pdf"


def _pdf_merge_bytes(pdfs: list[bytes]) -> tuple[bytes, str]:
    """Merge at most five local PDF byte strings in memory."""
    if not pdfs or len(pdfs) > FILE_OPS_MAX_ITEMS or not _pdf_operation_supported():
        raise ValueError("unsupported_pdf")
    if sum(len(item or b"") for item in pdfs) > PDF_MAX_BYTES:
        raise ValueError("pdf_too_large")
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for item in pdfs:
        reader = PdfReader(io.BytesIO(item))
        if not reader.pages:
            raise ValueError("empty_pdf")
        for page in reader.pages:
            writer.add_page(page)
    output = io.BytesIO()
    writer.write(output)
    result = output.getvalue()
    if not result or len(result) > PDF_MAX_BYTES:
        raise ValueError("pdf_too_large")
    return result, "merged.pdf"


def _file_ops_items(platform: str, chat_id: int) -> list[dict]:
    key = (str(platform), int(chat_id))
    with _file_ops_lock:
        entry = _file_ops_cache.get(key)
        if not entry:
            return []
        if entry["expires_at"] <= time.monotonic():
            _file_ops_cache.pop(key, None)
            return []
        return list(entry["items"])


def _file_ops_prune(now: Optional[float] = None) -> None:
    """Evict expired owners and cap both owner count and retained bytes."""
    now = time.monotonic() if now is None else now
    with _file_ops_lock:
        expired = [key for key, entry in _file_ops_cache.items() if entry["expires_at"] <= now]
        for key in expired:
            _file_ops_cache.pop(key, None)
        entries = sorted(
            _file_ops_cache.items(), key=lambda pair: (pair[1]["expires_at"], pair[0])
        )
        overflow = len(entries) - FILE_OPS_MAX_OWNERS
        for key, _entry in entries[:max(0, overflow)]:
            _file_ops_cache.pop(key, None)
        entries = sorted(
            _file_ops_cache.items(), key=lambda pair: (pair[1]["expires_at"], pair[0])
        )
        total = sum(int(entry.get("total_bytes", 0)) for _key, entry in entries)
        for key, entry in entries:
            if total <= FILE_OPS_MAX_TOTAL_BYTES:
                break
            _file_ops_cache.pop(key, None)
            total -= int(entry.get("total_bytes", 0))


def _file_ops_set(platform: str, chat_id: int, item: dict) -> bool:
    """Replace one owner's pending file-operation item with a hard byte cap."""
    data = item.get("data") if isinstance(item, dict) else b""
    if not isinstance(item, dict) or not isinstance(data, (bytes, bytearray)) or not data:
        return False
    if len(data) > PDF_MAX_BYTES and item.get("kind") != "video":
        return False
    if len(data) > VIDEO_MAX_INPUT_BYTES:
        return False
    now = time.monotonic()
    _file_ops_prune(now)
    key = (str(platform), int(chat_id))
    with _file_ops_lock:
        if key not in _file_ops_cache:
            total = sum(int(entry.get("total_bytes", 0)) for entry in _file_ops_cache.values())
            if total + len(data) > FILE_OPS_MAX_TOTAL_BYTES:
                return False
        _file_ops_cache[key] = {
            "expires_at": now + FILE_OPS_TTL,
            "items": [dict(item)],
            "total_bytes": len(data),
        }
    return True


def _file_ops_add(platform: str, chat_id: int, item: dict) -> bool:
    """Append a PDF for merge without allowing unbounded retained uploads."""
    data = item.get("data") if isinstance(item, dict) else b""
    if not isinstance(data, (bytes, bytearray)) or not data:
        return False
    now = time.monotonic()
    _file_ops_prune(now)
    key = (str(platform), int(chat_id))
    with _file_ops_lock:
        current_entry = _file_ops_cache.get(key)
        current = list(current_entry["items"]) if current_entry else []
        if len(current) >= FILE_OPS_MAX_ITEMS:
            return False
        if sum(len(x.get("data", b"")) for x in current) + len(data) > PDF_MAX_BYTES:
            return False
        if key not in _file_ops_cache:
            total = sum(int(entry.get("total_bytes", 0)) for entry in _file_ops_cache.values())
            if total + len(data) > FILE_OPS_MAX_TOTAL_BYTES:
                return False
        _file_ops_cache[key] = {
            "expires_at": now + FILE_OPS_TTL,
            "items": current + [dict(item)],
            "total_bytes": sum(len(x.get("data", b"")) for x in current) + len(data),
        }
    return True


def _file_ops_get(platform: str, chat_id: int):
    items = _file_ops_items(platform, chat_id)
    if not items:
        return None
    return items[0] if len(items) == 1 else items


def _file_ops_clear(platform: str, chat_id: int) -> None:
    with _file_ops_lock:
        _file_ops_cache.pop((str(platform), int(chat_id)), None)


def _uploaded_file_name(info: dict, fallback: str) -> str:
    """Return a safe local filename from either platform's upload shape."""
    raw = str(
        (info or {}).get("file_name")
        or (info or {}).get("filename")
        or (info or {}).get("name")
        or fallback
    ).strip()
    name = Path(raw).name or fallback
    return name[:120]


def _download_uploaded_file(info: dict, max_bytes: int) -> tuple[bytes, str] | None:
    """Download one platform upload with a hard transfer budget."""
    file_id = str((info or {}).get("file_id") or (info or {}).get("id") or "")
    if not file_id:
        return None
    try:
        url = get_file_url(file_id)
    except Exception as exc:
        log.debug("file upload getFile failed: %s", type(exc).__name__)
        return None
    if not url:
        return None
    data = download_bytes(url, max_bytes)
    if not data or len(data) > max_bytes:
        return None
    return data, url


# Conversion references are short-lived metadata only; media lives in the worker tempdir.
CONVERT_MAX_BYTES = 20 * 1024 * 1024
CONVERT_MAX_PIXELS = 16_000_000
CONVERT_TTL = 1800
CONVERT_MAX_SOURCES = 512
_CONVERT_TARGETS = {"image": ("jpg", "png", "webp", "pdf"),
                    "audio": ("mp3", "m4a", "ogg", "opus"),
                    "video": ("mp4", "webm", "mp3")}
_convert_sources = {}
_convert_lock = threading.Lock()
_convert_jobs = set()


def _conversion_info(msg):
    if not isinstance(msg, dict):
        return None
    for kind in ("document", "video", "audio", "voice", "video_note"):
        if isinstance(msg.get(kind), dict):
            info = dict(msg[kind])
            if kind != "document":
                info.setdefault("file_name", {"video": "video.mp4", "video_note": "video.mp4",
                                              "audio": "audio.mp3", "voice": "voice.ogg"}[kind])
            return info
    photos = msg.get("photo")
    if isinstance(photos, list) and photos:
        info = dict(max(photos, key=lambda x: x.get("file_size", 0)))
        info.setdefault("file_name", "image.jpg")
        return info
    return None


def _conversion_kind(info):
    name = _uploaded_file_name(info, "file").lower()
    if re.search(r"\.part\d+(?:of\d+)?(?:\.|$)", name):
        return None  # Raw binary fragments are not independently convertible.
    ext = name.rsplit(".", 1)[-1]
    mime = str(info.get("mime_type", ""))
    if ext in ("jpg", "jpeg", "png", "webp") or mime.startswith("image/") or info.get("width") and not info.get("duration"):
        return "image"
    if ext in ("mp4", "webm", "mkv", "mov", "avi", "m4v") or mime.startswith("video/") or info.get("width"):
        return "video"
    if ext in ("mp3", "m4a", "ogg", "opus", "wav", "flac", "aac") or mime.startswith("audio/") or info.get("duration"):
        return "audio"
    return None


def _conversion_register(cid, uid, info):
    import secrets
    if not uid or not info or not info.get("file_id"):
        return None
    if int(info.get("file_size") or 0) > CONVERT_MAX_BYTES:
        return None
    kind = _conversion_kind(info)
    if not kind:
        return None
    now = time.monotonic()
    with _convert_lock:
        for token, source in list(_convert_sources.items()):
            if source["expires"] <= now:
                del _convert_sources[token]
        while len(_convert_sources) >= CONVERT_MAX_SOURCES:
            del _convert_sources[next(iter(_convert_sources))]
        token = secrets.token_hex(8)
        # No captions, URLs, bytes, or mutable last-upload state.
        _convert_sources[token] = {"platform": _platform(), "cid": cid, "uid": uid,
                                   "info": {k: info[k] for k in ("file_id", "file_name", "file_size", "mime_type") if k in info},
                                   "kind": kind, "expires": now + CONVERT_TTL}
    return token


def _conversion_source(cid, uid, token):
    with _convert_lock:
        source = _convert_sources.get(token)
        if not source or source["expires"] <= time.monotonic():
            _convert_sources.pop(token, None)
            return None
        if (source["platform"], source["cid"], source["uid"]) != (_platform(), cid, uid):
            return None
        return source


def _conversion_offer(cid, uid, info):
    token = _conversion_register(cid, uid, info)
    if token:
        send_message(cid, "🔄 تبدیل همین فایل (تا ۳۰ دقیقه):", reply_markup={"inline_keyboard": [[
            {"text": "🔄 انتخاب قالب", "callback_data": "cv_menu:" + token}]]})
    return token


def _conversion_capture(cid, result, filename="", size=0):
    """Observe successful API results without changing the sender's boolean contract."""
    try:
        if re.search(r"\.part\d+(?:of\d+)?(?:\.|$)", filename.lower()):
            return
        uid = getattr(_ctx, "requester", None) or (getattr(_ctx, "operation", None) or {}).get("requester")
        # Do not guess a group requester from its chat ID.
        if not uid:
            uid = int(cid) if int(cid) > 0 else None
        for message in result if isinstance(result, list) else [result]:
            info = _conversion_info(message)
            if info:
                info.setdefault("file_name", filename or "image.jpg")
                info.setdefault("file_size", size)
                _conversion_offer(int(cid), uid, info)
    except Exception:
        log.debug("conversion source capture failed", exc_info=True)


def _conversion_menu(cid, uid, token):
    source = _conversion_source(cid, uid, token)
    if not source:
        _status(cid, "❌ این دکمه منقضی شده یا متعلق به شما نیست؛ فایل را دوباره بفرستید.", final=True)
        return
    rows = [[{"text": target.upper(), "callback_data": f"cv_do:{token}:{target}"}
             for target in _CONVERT_TARGETS[source["kind"]]]]
    send_message(cid, "🔄 قالب خروجی را انتخاب کنید. تبدیل ممکن است کیفیت را کاهش دهد؛ در JPG و PDF شفافیت روی زمینهٔ سفید قرار می‌گیرد. عکس متحرک پشتیبانی نمی‌شود. سقف ورودی ۲۰ مگابایت و مدت صدا/ویدیو ۳ دقیقه است.",
                 reply_markup={"inline_keyboard": rows})


def _conversion_probe(path, include_duration=False):
    import subprocess
    result = subprocess.run(["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe",
                             "-format_whitelist", "mov,matroska,webm,mp3,ogg,wav,flac,aac",
                             "-show_streams", "-show_format", "-of", "json", str(path)],
                            capture_output=True, timeout=10, check=True)
    info = json.loads(result.stdout)
    streams = info.get("streams", [])
    videos = [s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    duration = float(info.get("format", {}).get("duration", 0))
    if not 0 < duration <= 180 or not (videos or audio):
        raise ValueError("مدت یا محتوای فایل مجاز نیست؛ حداکثر سه دقیقه.")
    if videos and (int(videos[0].get("width", 0)) * int(videos[0].get("height", 0)) > 8_294_400):
        raise ValueError("ابعاد ویدیو بیشتر از حد مجاز است.")
    result = ("video" if videos else "audio", bool(audio))
    return result + (duration,) if include_duration else result


def _convert_local(source, target, directory, output_limit):
    """Decode content, not the claimed suffix. All subprocess switches are fixed."""
    import subprocess
    import warnings
    from PIL import Image, UnidentifiedImageError
    output = Path(directory) / ("converted." + target)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(source)
            with image:
                if image.format not in ("JPEG", "PNG", "WEBP") or getattr(image, "n_frames", 1) != 1:
                    raise ValueError("قالب تصویر یا عکس متحرک پشتیبانی نمی‌شود.")
                if image.width * image.height > CONVERT_MAX_PIXELS:
                    raise ValueError("ابعاد عکس بیشتر از حد مجاز است.")
                if target not in _CONVERT_TARGETS["image"]:
                    raise ValueError("قالب خروجی برای این تصویر مجاز نیست.")
                image.load()
                if target in ("jpg", "pdf"):
                    rgba = image.convert("RGBA")
                    flat = Image.new("RGB", image.size, "white")
                    flat.paste(rgba, mask=rgba.getchannel("A"))
                    flat.save(output, format="JPEG" if target == "jpg" else "PDF")
                else:
                    image.save(output, format=target.upper())
    except UnidentifiedImageError:
        kind, has_audio, duration = _conversion_probe(source, include_duration=True)
        if target not in _CONVERT_TARGETS[kind] or target == "mp3" and not has_audio:
            raise ValueError("قالب خروجی با محتوای فایل سازگار نیست یا فایل صدا ندارد.")
        codecs = {"mp3": ["-vn", "-c:a", "libmp3lame", "-b:a", "128k"],
                  "m4a": ["-vn", "-c:a", "aac", "-b:a", "128k"],
                  "ogg": ["-vn", "-c:a", "libvorbis", "-q:a", "4"],
                  "opus": ["-vn", "-c:a", "libopus", "-b:a", "96k"],
                  "mp4": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart"],
                  "webm": ["-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-crf", "36", "-b:v", "0", "-c:a", "libopus"]}
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-threads", "2",
               "-filter_threads", "1", "-protocol_whitelist", "file,pipe",
               "-format_whitelist", "mov,matroska,webm,mp3,ogg,wav,flac,aac", "-i", str(source),
               "-map", "0:v:0?" if target in ("mp4", "webm") else "0:a:0"]
        if target in ("mp4", "webm"):
            cmd += ["-map", "0:a:0?", "-vf", "scale=trunc(min(1280\\,iw)/2)*2:trunc(ih*min(1280\\,iw)/iw/2)*2"]
        cmd += ["-map_metadata", "-1", "-map_chapters", "-1"] + codecs[target] + ["-threads", "2", "-fs", str(output_limit + 1), str(output)]
        subprocess.run(cmd, capture_output=True, timeout=120, check=True)
        if _conversion_probe(output, include_duration=True)[2] < duration - 0.2:
            raise ValueError("خروجی کامل نیست؛ فایل کوچک‌تری بفرستید.")
    if not output.exists() or not 0 < output.stat().st_size < output_limit:
        raise ValueError("خروجی بیشتر از سقف ارسال یکپارچه است؛ فایل کوچک‌تری بفرستید.")
    return output


@_heavy_download
def _conversion_worker(cid, source, target):
    try:
        _status(cid, "⏳ در حال دریافت و تبدیل فایل…")
        downloaded = _download_uploaded_file(source["info"], CONVERT_MAX_BYTES)
        if not downloaded:
            raise ValueError("دریافت فایل ناموفق بود؛ فایل کوچک‌تر یا دوباره ارسال شود.")
        data = downloaded[0]
        if len(data) > CONVERT_MAX_BYTES:
            raise ValueError("حجم فایل بیشتر از حد مجاز است.")
        limit = min(CONVERT_MAX_BYTES, _chunk_size())
        _ensure_disk_space(CONVERT_MAX_BYTES * 3)
        with tempfile.TemporaryDirectory(prefix="convert_") as directory:
            path = Path(directory) / "source"
            path.write_bytes(data)
            del data, downloaded
            output = _convert_local(path, target, directory, limit)
            # No splitting: format-sensitive output must remain a whole document.
            ok = send_document(cid, output.read_bytes(), output.name, caption="🔄 فایل تبدیل‌شده")
        _status(cid, "✅ تبدیل و ارسال انجام شد." if ok else "❌ تبدیل انجام شد ولی ارسال ناموفق بود؛ دوباره تلاش کنید.", final=True)
    except Exception as exc:
        log.warning("conversion failed: %s", type(exc).__name__)
        _status(cid, "❌ تبدیل انجام نشد؛ فایل خراب، ناسازگار یا بیشتر از محدودیت حجم، زمان یا ابعاد است. فایل کوچک‌تر بفرستید.", final=True)


def _conversion_start(cid, uid, token, target):
    source = _conversion_source(cid, uid, token)
    if not source or target not in _CONVERT_TARGETS[source["kind"]]:
        _status(cid, "❌ درخواست نامعتبر یا منقضی شده است؛ فایل را دوباره بفرستید.", final=True)
        return None
    key = (source["platform"], cid, uid, source["info"]["file_id"])
    with _convert_lock:
        if key in _convert_jobs:
            _status(cid, "⏳ تبدیل همین فایل در حال انجام است؛ صبر کنید.", final=True)
            return None
        _convert_jobs.add(key)
    try:
        future = _conversion_worker(cid, source, target)
    except BaseException:
        with _convert_lock:
            _convert_jobs.discard(key)
        raise
    def finished(_):
        with _convert_lock:
            _convert_jobs.discard(key)
    future.add_done_callback(finished)
    return future


def _file_upload_is_pdf(info: dict, data: bytes) -> bool:
    name = _uploaded_file_name(info, "upload.pdf").lower()
    mime = str((info or {}).get("mime_type") or (info or {}).get("content_type") or "").lower()
    return name.endswith(".pdf") or mime == "application/pdf" or data[:4] == b"%PDF"


def _file_upload_is_video(info: dict) -> bool:
    name = _uploaded_file_name(info, "upload.mp4").lower()
    mime = str((info or {}).get("mime_type") or (info or {}).get("content_type") or "").lower()
    return name.endswith((".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v")) or mime.startswith("video/")


def _start_file_worker(fn, *args) -> None:
    """Schedule a bounded local operation on the existing heavy executor."""
    fn(*args)


def _parse_video_range(text: str) -> tuple[int, int] | None:
    numbers = [int(value) for value in re.findall(r"\d+", str(text or ""))]
    if len(numbers) < 2:
        return None
    start, end = numbers[0], numbers[1]
    if start < 0 or end <= start or end - start > 1800:
        return None
    return start, end


def _handle_pdf_upload(cid: int, info: dict, mode: str) -> None:
    limit = PDF_MAX_BYTES
    size = int((info or {}).get("file_size") or 0)
    if size > limit:
        _status(cid, "❌ حجم PDF بیشتر از حد مجاز است.", final=True)
        return
    downloaded = _download_uploaded_file(info, limit)
    if not downloaded:
        _status(cid, "❌ دریافت فایل PDF ناموفق بود.", final=True)
        return
    data, _url = downloaded
    if not _file_upload_is_pdf(info, data):
        _status(cid, "❌ فایل ارسالی PDF معتبر نیست.", final=True)
        return
    if mode == "pdf_extract":
        if not _file_ops_set(_platform(), cid, {
            "kind": "pdf", "data": data, "name": _uploaded_file_name(info, "document.pdf"),
        }):
            _status(cid, "❌ فایل PDF قابل پردازش نیست یا بیش از حد مجاز است.", final=True)
            return
        set_state(cid, mode="pdf_extract", file_op_name=_uploaded_file_name(info, "document.pdf"))
        _status(cid, "📄 شمارهٔ صفحه‌ها را بفرستید؛ مثال: `1,3-5`.", final=True)
        return
    if not _file_ops_add(_platform(), cid, {
        "kind": "pdf", "data": data, "name": _uploaded_file_name(info, "document.pdf"),
    }):
        _status(cid, "⚠️ حداکثر ۵ فایل PDF یا حجم کلی مجاز نیست؛ فایل‌های قبلی پاک شدند.", final=True)
        _file_ops_clear(_platform(), cid)
        return
    count = len(_file_ops_items(_platform(), cid))
    set_state(cid, mode="pdf_merge", file_op_count=count)
    _status(
        cid,
        f"📚 فایل PDF {count} ثبت شد. برای پایان `ادغام` را بفرستید.",
        final=True,
    )


def _handle_video_upload(cid: int, info: dict, mode: str) -> None:
    size = int((info or {}).get("file_size") or 0)
    if size > VIDEO_MAX_INPUT_BYTES:
        _status(cid, "❌ حجم ویدیو بیشتر از ۴۸ مگابایت است.", final=True)
        return
    if not _file_upload_is_video(info):
        _status(cid, "❔ فایل ارسالی ویدیو معتبر نیست.", final=True)
        return
    downloaded = _download_uploaded_file(info, VIDEO_MAX_INPUT_BYTES)
    if not downloaded:
        _status(cid, "❌ دریافت ویدیو ناموفق بود.", final=True)
        return
    data, _url = downloaded
    name = _uploaded_file_name(info, "video.mp4")
    if mode == "video_trim":
        if not _file_ops_set(_platform(), cid, {"kind": "video", "data": data, "name": name}):
            _status(cid, "❌ ویدیوی دریافتی قابل پردازش نیست.", final=True)
            return
        set_state(cid, mode="video_trim", file_op_name=name)
        _status(cid, "✂️ بازهٔ شروع و پایان را به ثانیه بفرستید؛ مثال: `5 30`.", final=True)
        return
    _start_file_worker(_process_video_compress, cid, data, name)


@_heavy_download
def _process_pdf_extract(cid: int, data: bytes, name: str, page_spec: str) -> None:
    try:
        result, filename = _pdf_extract_pages_bytes(data, page_spec)
    except Exception as exc:
        log.warning("PDF extract failed: %s", type(exc).__name__)
        _status(cid, "❌ انتخاب صفحه‌ها معتبر نیست یا فایل قابل خواندن نیست.", final=True)
        return
    ok = send_document(cid, result, filename, caption="📄 صفحات انتخاب‌شدهٔ PDF")
    _file_ops_clear(_platform(), cid)
    if ok:
        _log_history(cid, "pdf_extract", file_name=name, pages=page_spec)
    _status(cid, "✅ PDF ارسال شد." if ok else "❌ ارسال PDF ناموفق بود.", final=True)
    clear_state(cid)


@_heavy_download
def _process_pdf_merge(cid: int) -> None:
    items = _file_ops_items(_platform(), cid)
    if len(items) < 2:
        _status(cid, "📚 برای ادغام حداقل دو فایل PDF بفرستید.", final=True)
        return
    try:
        result, filename = _pdf_merge_bytes([item["data"] for item in items])
    except Exception as exc:
        log.warning("PDF merge failed: %s", type(exc).__name__)
        _file_ops_clear(_platform(), cid)
        _status(cid, "❌ ادغام PDF ممکن نبود؛ فایل‌های ناسازگار را بررسی کنید.", final=True)
        return
    ok = send_document(cid, result, filename, caption="📚 PDF ادغام‌شده")
    _file_ops_clear(_platform(), cid)
    if ok:
        _log_history(cid, "pdf_merge", files=len(items))
    _status(cid, "✅ PDF ادغام و ارسال شد." if ok else "❌ ارسال PDF ناموفق بود.", final=True)
    clear_state(cid)


@_heavy_download
def _process_video_trim(cid: int, data: bytes, name: str, start: int, end: int) -> None:
    result = _trim_video_to_bytes(data, name, start, end)
    _file_ops_clear(_platform(), cid)
    if not result:
        _status(cid, "❌ برش ویدیو ممکن نبود؛ فایل یا بازهٔ زمانی را بررسی کنید.", final=True)
        return
    output, output_name = result
    ok = send_video(cid, output, output_name, caption="✂️ بخش ویدیو")
    if ok:
        _log_history(cid, "video_trim", file_name=name, start=start, end=end)
    _status(cid, "✅ بخش ویدیو ارسال شد." if ok else "❌ ارسال ویدیو ناموفق بود.", final=True)
    clear_state(cid)


@_heavy_download
def _process_video_compress(cid: int, data: bytes, name: str) -> None:
    result = _compress_video_to_bytes(data, name)
    if not result:
        _status(cid, "❌ فشرده‌سازی ویدیو ممکن نبود.", final=True)
        return
    output, output_name = result
    ok = send_video(cid, output, output_name, caption="🎞 ویدیوی فشرده")
    if ok:
        _log_history(cid, "video_compress", file_name=name, bytes_in=len(data), bytes_out=len(output))
    _status(cid, "✅ ویدیوی فشرده ارسال شد." if ok else "❌ ارسال ویدیو ناموفق بود.", final=True)
    clear_state(cid)


def _handle_pdf_text(cid: int, text: str) -> None:
    item = _file_ops_get(_platform(), cid)
    if not isinstance(item, dict) or item.get("kind") != "pdf":
        _status(cid, "📄 ابتدا یک فایل PDF بفرستید.", final=True)
        return
    if not _parse_pdf_pages(text, 10_000):
        _status(cid, "❌ قالب صفحه معتبر نیست؛ مثال: `1,3-5`.", final=True)
        return
    _start_file_worker(_process_pdf_extract, cid, item["data"], item.get("name", "document.pdf"), text.strip())


def _handle_pdf_merge_text(cid: int, text: str) -> None:
    if str(text or "").strip().lower() not in {"ادغام", "merge", "done", "تمام"}:
        _status(cid, "📚 برای پایان، کلمهٔ «ادغام» را بفرستید.", final=True)
        return
    items = _file_ops_items(_platform(), cid)
    if len(items) < 2:
        _status(cid, "📚 حداقل دو فایل PDF لازم است.", final=True)
        return
    _start_file_worker(_process_pdf_merge, cid)


def _handle_video_range_text(cid: int, text: str) -> None:
    item = _file_ops_get(_platform(), cid)
    if not isinstance(item, dict) or item.get("kind") != "video":
        _status(cid, "✂️ ابتدا یک ویدیو بفرستید.", final=True)
        return
    parsed = _parse_video_range(text)
    if not parsed:
        _status(cid, "❌ بازهٔ زمانی معتبر نیست؛ مثال: `5 30`.", final=True)
        return
    start, end = parsed
    _start_file_worker(_process_video_trim, cid, item["data"], item.get("name", "video.mp4"), start, end)


def _handle_file_upload(cid: int, info: dict, mode: str) -> None:
    if mode in ("pdf_extract", "pdf_merge"):
        _handle_pdf_upload(cid, info, mode)
        return
    if mode in ("video_trim", "video_compress"):
        _handle_video_upload(cid, info, mode)
        return
    _status(cid, "⚠️ این فایل برای عملیات فعلی مناسب نیست.", final=True)


def _trim_video_to_bytes(
    data: bytes, filename: str, start_seconds: int, end_seconds: int,
    test_output: Optional[Path] = None,
) -> Optional[tuple[bytes, str]]:
    """Trim a local video with a bounded ffmpeg subprocess and cleanup."""
    _ensure_disk_space(len(data) * 2)
    try:
        start = int(start_seconds)
        end = int(end_seconds)
    except (TypeError, ValueError):
        return None
    if not data or len(data) > VIDEO_MAX_INPUT_BYTES or start < 0 or end <= start or end - start > 1800:
        return None
    import shutil as _sh
    ffmpeg = _sh.which("ffmpeg") or "/usr/bin/ffmpeg"
    with tempfile.TemporaryDirectory(prefix="balebot-video-") as temp:
        temp_dir = Path(temp)
        source = temp_dir / "source.mp4"
        output = test_output or temp_dir / "trimmed.mp4"
        source.write_bytes(data)
        cmd = [
            ffmpeg, "-y", "-ss", str(start), "-i", str(source), "-t", str(end - start),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(output),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=300)
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0 or not output.exists() or output.stat().st_size < 1:
            return None
        if output.stat().st_size > VIDEO_MAX_OUTPUT_BYTES:
            return None
        return output.read_bytes(), output.name


def _compress_video_to_bytes(data: bytes, filename: str) -> Optional[tuple[bytes, str]]:
    """Re-encode a local video with a bounded output budget."""
    _ensure_disk_space(len(data) * 2)
    if not data or len(data) > VIDEO_MAX_INPUT_BYTES:
        return None
    import shutil as _sh
    ffmpeg = _sh.which("ffmpeg") or "/usr/bin/ffmpeg"
    with tempfile.TemporaryDirectory(prefix="balebot-video-") as temp:
        temp_dir = Path(temp)
        source = temp_dir / "source.mp4"
        output = temp_dir / "compressed.mp4"
        source.write_bytes(data)
        try:
            result = subprocess.run(
                [
                    ffmpeg, "-y", "-i", str(source), "-c:v", "libx264",
                    "-preset", "veryfast", "-crf", "30", "-c:a", "aac",
                    "-b:a", "128k", "-movflags", "+faststart", str(output),
                ],
                capture_output=True,
                timeout=300,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0 or not output.exists() or output.stat().st_size < 1:
            return None
        if output.stat().st_size > VIDEO_MAX_OUTPUT_BYTES:
            return None
        return output.read_bytes(), output.name


def do_image_compress(cid: int, data: bytes) -> None:
    if not data or len(data) > MAX_IMAGE_SIZE:
        _status(cid, "❌ حجم عکس بیشتر از حد مجاز است.", final=True)
        return
    _status(cid, "🖼 در حال فشرده‌سازی عکس…")
    try:
        result, filename = _compress_image_bytes(data)
    except Exception as e:
        log.warning("image compression failed: %s", e)
        _status(cid, "❌ این فایل تصویر پشتیبانی نمی‌شود؛ لطفاً عکس دیگری بفرستید.", final=True)
        return
    ok = smart_send(cid, result, filename, caption="🖼 تصویر فشرده", media_type="photo")
    if ok:
        _log_history(cid, "image_compress", bytes_in=len(data), bytes_out=len(result))
    _status(cid, "✅ تصویر فشرده ارسال شد." if ok else "❌ ارسال تصویر فشرده ناموفق بود.", final=True)
    clear_state(cid)


# ═══════════════════════════════════════════════════════════════════════════════
# SOCIAL MEDIA HANDLERS
# ═══════════════════════════════════════════════════════════════════════════════


def audiobook_results_kb(results: list, cache_key: str) -> dict:
    """نتایج جستجوی کتاب صوتی به‌صورت دکمه."""
    rows = []
    for i, r in enumerate(results):
        name = (r.get("name") or f"نتیجه {i + 1}")[:50]
        src = r.get("_source", "")
        icon = "🎧" if src == "librivox" else "📚"
        rows.append([{"text": f"{icon} {name}", "callback_data": f"abk_item_{cache_key}_{i}"}])
    rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
    return {"inline_keyboard": rows}


def librivox_search(query: str) -> list[dict]:
    """Search LibriVox (public-domain audiobooks) via archive.org; real files."""
    try:
        r = requests.get(
            "https://archive.org/advancedsearch.php",
            params={
                "q": f'title:("{query}") AND collection:librivoxaudio',
                "fl[]": ["identifier", "title", "creator"],
                "rows": 8,
                "output": "json",
            },
            headers={"User-Agent": UA_DESK},
            timeout=20,
        )
        docs = r.json().get("response", {}).get("docs", []) if r.status_code == 200 else []
    except Exception as e:
        log.warning("librivox_search: %s", e)
        return []
    out = []
    for d in docs:
        ident, title = d.get("identifier", ""), d.get("title", "").strip()
        if not ident or not title:
            continue
        creator = d.get("creator")
        if isinstance(creator, list):
            creator = ", ".join(creator[:2])
        out.append({
            "name": title,
            "authors": [creator] if creator else [],
            "url": f"https://archive.org/details/{ident}",
            "_source": "librivox",
        })
    return out


def archive_org_search(query: str, count: int = 10) -> list[dict]:
    """Search Internet Archive texts — free, and many titles are lending-enabled.

    Results are metadata-only and carry ``ia_url`` (the details page). The real
    file is resolved lazily at download time, so a search costs exactly one
    HTTP request instead of one per result.
    """
    q = (query or "").strip()
    if not q:
        return []
    log.info("archive_org_search: %r", q)
    count = max(1, min(int(count), 10))
    try:
        r = requests.get(
            "https://archive.org/advancedsearch.php",
            params={
                "q": f'title:("{q}") AND mediatype:texts',
                "fl[]": ["identifier", "title", "creator", "year"],
                "rows": count,
                "page": 1,
                "output": "json",
            },
            headers={"User-Agent": UA_DESK},
            timeout=20,
        )
        if r.status_code != 200:
            log.debug("archive_org_search: HTTP %d", r.status_code)
            return []
        docs = r.json().get("response", {}).get("docs", [])
    except Exception as e:
        log.warning("archive_org_search: %s", e)
        return []

    out, seen = [], set()
    for d in docs:
        ident = str(d.get("identifier") or "").strip()
        title = str(d.get("title") or "").strip()
        if not ident or not title or ident in seen:
            continue
        seen.add(ident)
        creator = d.get("creator")
        if isinstance(creator, list):
            creator = ", ".join(str(c) for c in creator[:2])
        out.append({
            "name": title,
            "authors": [str(creator)] if creator else [],
            "year": str(d.get("year") or ""),
            "ia_url": f"https://archive.org/details/{ident}",
        })
        if len(out) >= count:
            break
    return out


def _archive_org_best_file(identifier: str) -> Optional[tuple[str, str]]:
    """Resolve an Archive.org identifier to its best free full-text file.

    Returns ``(download_url, filename)`` or None. One bounded metadata call;
    lending-only items expose no direct file and correctly return None.
    """
    ident = (identifier or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._\-]{1,120}", ident):
        return None
    try:
        r = requests.get(
            f"https://archive.org/metadata/{ident}",
            headers={"User-Agent": UA_DESK},
            timeout=20,
        )
        if r.status_code != 200:
            return None
        files = r.json().get("files", [])
    except Exception as e:
        log.warning("_archive_org_best_file: %s", e)
        return None

    def _rank(f):
        name = str(f.get("name") or "")
        if f.get("private"):
            return 0
        low = name.lower()
        # Lending/DRM wrappers are not real files: they download fine but
        # cannot be opened, so never offer them.
        if low.endswith(("_encrypted.pdf", "_lcp.epub", "_lcp.pdf", "_bw.pdf")):
            return 0
        if low.endswith("_text.pdf"):
            return 4
        if low.endswith(".pdf"):
            return 3
        if low.endswith(".epub"):
            return 2
        if low.endswith(".mobi") or low.endswith(".azw3"):
            return 1
        if low.endswith("_djvu.txt"):
            return 1
        return 0

    best, best_rank = None, 0
    for f in files or []:
        if not f.get("size"):
            continue
        rank = _rank(f)
        if rank > best_rank:
            best, best_rank = f, rank
    if not best or not best_rank:
        return None
    name = str(best["name"])
    return (f"https://archive.org/download/{ident}/{urllib.parse.quote(name)}", name)


def do_audiobook_search(cid: int, query: str):
    """کتاب صوتی: LibriVox (دانلود مستقیم) + Rave audiobook CSE (لینک‌ها)."""
    bump(cid, "searches")
    _status(cid, f"⏳ در حال جستجوی کتاب صوتی: _{query}_…", parse_mode="Markdown")
    chat_action(cid)

    results = librivox_search(query)
    if results:
        key = make_cache_key("abk", query, 0)
        cache_set(key, results)
        set_state(cid, mode="audiobook", last_query=query, cache_key=key)
        _status(
            cid,
            f"🎧 *نتایج LibriVox:* _{query}_\nروی کتاب کلیک کنید:",
            parse_mode="Markdown",
            reply_markup=audiobook_results_kb(results, key), final=True,
        )
        return

    rave = ravebook_search(query, cx=RAVEBOOK_AUDIO_CSE_CX)
    if rave:
        key = make_cache_key("rvbk", query, 0)
        cache_set(key, rave)
        set_state(cid, mode="ravebook", last_query=query, cache_key=key,
                  ravebook_mode="audiobooks")
        _status(
            cid,
            f"🎧 *نتایج کتاب صوتی:* _{query}_\n(لینک دانلود در سایت منبع است)",
            parse_mode="Markdown",
            reply_markup=ravebook_results_kb(rave, key), final=True,
        )
        return

    _status(
        cid,
        "❌ کتاب صوتی یافت نشد. عنوان انگلیسی را امتحان کنید.",
        reply_markup=home_kb(), final=True,
    )


def ravebook_results_kb(results: list, cache_key: str) -> dict:
    """نتایج جستجوی Rave Book Search به‌صورت دکمه."""
    rows = []
    for i, r in enumerate(results):
        name = r.get("name", f"نتیجه {i + 1}")[:52]
        host = r.get("host", "")
        label = f"📚 {name}"
        if host:
            label += f"  ({host})"
        rows.append([{"text": label[:64], "callback_data": f"rvbk_item_{cache_key}_{i}"}])
    rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
    return {"inline_keyboard": rows}


def do_ravebook_search(cid: int, query: str, mode: str = "ebooks"):
    """Search Rave Book Search (Google CSE over verified book sites)."""
    bump(cid, "searches")
    _status(cid, f"⏳ در حال جستجو در Rave Book Search: _{query}_…", parse_mode="Markdown")
    results = ravebook_search(query, mode)
    if not results:
        _status(
            cid,
            "❌ نتیجه‌ای یافت نشد. عنوان + نام نویسنده را دقیق‌تر امتحان کنید.",
            reply_markup=home_kb(), final=True,
        )
        return
    key = make_cache_key("rvbk", query, 0)
    cache_set(key, results)
    set_state(cid, mode="ravebook", last_query=query, cache_key=key)
    send_message(
        cid,
        f"📚 *Rave Book Search:* _{query}_\nنتیجه را انتخاب کنید (لینک دانلود در سایت منبع):",
        parse_mode="Markdown",
        reply_markup=ravebook_results_kb(results, key),
    )


def do_book_search(cid: int, query: str, extensions: list = None):
    """Search Library Genesis → Anna's Archive → Rave Book Search chain."""
    bump(cid, "searches")
    _status(
        cid, f"⏳ در حال جستجوی کتاب: _{query}_…", parse_mode="Markdown"
    )
    chat_action(cid)

    results = libgen_search(query, count=10, extensions=extensions)
    source_label = "Library Genesis"
    if not results:
        results = annas_archive_search(query, count=10, extensions=extensions)
        if results:
            source_label = "Anna's Archive"
    if not results:
        # Internet Archive: free + lending-enabled titles (no paid API)
        results = archive_org_search(query, count=10)
        if results:
            source_label = "Internet Archive"
    if not results:
        # Rave CSE: link-based results over dozens of verified book sites
        rave = ravebook_search(query)
        if rave:
            key = make_cache_key("rvbk", query, 0)
            cache_set(key, rave)
            set_state(cid, mode="ravebook", last_query=query, cache_key=key,
                      ravebook_mode="ebooks")
            _status(
                cid,
                f"📚 *نتایج Rave Book Search:* _{query}_\n"
                "(لینک دانلود در سایت منبع است)",
                parse_mode="Markdown",
                reply_markup=ravebook_results_kb(rave, key), final=True,
            )
            return
        results = libgen_search(query, count=10, extensions=None, _ol_fallback=True)
        if results:
            source_label = "Open Library"

    if not results:
        log.warning("do_book_search: no results for %r", query)
        _status(
            cid, "❌ نتیجه‌ای یافت نشد. کلمات دیگری امتحان کنید.", reply_markup=home_kb(), final=True)
        return

    key = make_cache_key("zl", query, 0)
    cache_set(key, results)
    set_state(cid, mode="book", last_query=query, cache_key=key, zlib_ext=extensions)

    text = f"📚 *نتایج {source_label}:* _{query}_\nروی کتاب کلیک کنید:"
    kb = book_results_kb(results, key)
    send_message(cid, text, parse_mode="Markdown", reply_markup=kb)


def _librivox_files(identifier: str) -> list[dict]:
    """List audio files of a LibriVox item via archive.org metadata API."""
    try:
        r = requests.get(
            f"https://archive.org/metadata/{identifier}",
            headers={"User-Agent": UA_DESK}, timeout=20,
        )
        if r.status_code != 200:
            return []
        files = r.json().get("files", [])
    except Exception as e:
        log.warning("_librivox_files: %s", e)
        return []
    out = []
    for f in files:
        name = f.get("name", "")
        if not name.lower().endswith((".mp3", ".m4b", ".ogg")):
            continue
        out.append({
            "name": name,
            "url": f"https://archive.org/download/{identifier}/{urllib.parse.quote(name)}",
            "size": int(f.get("size") or 0),
        })
    out.sort(key=lambda f: f["name"])
    return out


def do_librivox_show(cid: int, item: dict, key: str, idx: int):
    """کارن کتاب صوتی LibriVox: اطلاعات + لیست فایل‌های صوتی."""
    ident = urllib.parse.urlparse(item["url"]).path.rstrip("/").split("/")[-1]
    _status(cid, "⏳ در حال دریافت فهرست فایل‌های صوتی…")
    files = _librivox_files(ident)
    fname = (files[0]["name"] if files else "audiobook.mp3")
    fkey = store_url(item["url"], chat_id=cid)
    lines = [f"🎧 *{item['name'][:150]}*"]
    if item.get("authors"):
        lines.append(f"✍️ {', '.join(map(str, item['authors'][:2]))}")
    lines.append(f"🌐 منبع: LibriVox (ملک عمومی)")
    if files:
        total_mb = sum(f["size"] for f in files) / 1e6
        lines.append(f"🎵 {len(files)} فایل — مجموع {total_mb:.0f}MB")
    else:
        lines.append("⚠️ فایل صوتی یافت نشد.")
    rows = []
    if files:
        # Store the file list so abk_dl can grab the chosen part
        cache_set(f"abkf_{fkey}", files)
        rows.append([{"text": "📥 دانلود کل کتاب (تکه اول)", "callback_data": f"abk_dl_{fkey}_0"}])
        if len(files) > 1:
            for j, f in enumerate(files[:8]):
                short = f["name"].rsplit(".", 1)[0][-32:]
                rows.append([{ "text": f"🎵 {short}",
                               "callback_data": f"abk_dl_{fkey}_{j}"}])
    rows.append([
        {"text": "🌐 مشاهده در Archive.org", "url": item["url"]},
        {"text": "🔙 نتایج", "callback_data": "abk_back"},
    ])
    rows.append([{"text": "🏠 خانه", "callback_data": "home"}])
    send_message(
        cid,
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup={"inline_keyboard": rows},
    )


def do_book_show_book(cid: int, book: dict):
    """نمایش اطلاعات کامل کتاب + دکمه دانلود."""
    name = book.get("name", "نامشخص")
    authors = book.get("authors", [])
    year = book.get("year", "")
    publisher = book.get("publisher", "")
    language = book.get("language", "")
    extension = book.get("extension", "").upper()
    size = book.get("size", "")
    cover = book.get("cover", "")
    rating = book.get("rating", "")
    url = book.get("url", "")

    # کاور: row -> libgen page -> Google Books
    if not cover and book.get("md5"):
        cover = _libgen_cover_url(book["md5"])
    if not cover:
        _a0 = (authors or [""])[0]
        _a0 = _a0["author"] if isinstance(_a0, dict) else str(_a0)
        cover = _gbooks_cover(name, _a0)
    if cover:
        try:
            img_bytes = download_bytes(cover, MAX_IMAGE_SIZE)
            if img_bytes and len(img_bytes) > 500:
                send_photo(cid, img_bytes, caption=name[:80])
        except Exception as e:
            log.warning("book cover: %s", e)

    # متن اطلاعات
    lines = [f"📚 *{name}*"]
    if authors:
        auth_str = ", ".join(
            a["author"] if isinstance(a, dict) else str(a) for a in authors[:3]
        )
        lines.append(f"✍️ {auth_str}")
    if year:
        lines.append(f"📅 سال: {year}")
    if publisher:
        lines.append(f"🏢 ناشر: {publisher}")
    if language:
        lines.append(f"🌐 زبان: {language}")
    if extension:
        lines.append(f"📄 فرمت: {extension}")
    if size:
        lines.append(f"💾 حجم: {size}")
    if rating:
        lines.append(f"⭐ امتیاز: {rating}")

    # Download button — only if we have an MD5 (libgen source)
    md5 = book.get("md5", "")
    ext = book.get("extension", "pdf") or "pdf"
    rows = []
    ia_url = book.get("ia_url", "")
    if md5:
        # Store the book info with chat_id for callback
        book_key = store_url(f"libgen:{md5}:{ext}", chat_id=cid)
        rows.append([{"text": "📥 دانلود", "callback_data": f"book_dl_{book_key}"}])
    elif ia_url:
        ident = ia_url.rstrip("/").split("/")[-1]
        rows.append(
            [
                {
                    "text": "📖 دانلود از Internet Archive",
                    "callback_data": f"book_iadl_{store_url(ident, chat_id=cid)}",
                }
            ]
        )
        rows.append(
            [{"text": "🌐 مشاهده در Internet Archive", "url": ia_url}]
        )
    else:
        lines.append("\n⚠️ فایل مستقیم در دسترس نیست.")
        if url:
            rows.append(
                [
                    {
                        "text": "🌐 مشاهده در Open Library",
                        "callback_data": f"book_ol_{store_url(url, chat_id=cid)}",
                    }
                ]
            )
    rows.append(
        [
            {"text": "🔙 برگشت", "callback_data": "book_back"},
            {"text": "🏠 خانه", "callback_data": "home"},
        ]
    )
    send_message(
        cid,
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup={"inline_keyboard": rows},
    )


def do_book_download(cid: int, md5_ext: str):
    """
    Download a book from Library Genesis.
    md5_ext is '{md5}_{ext}' as encoded in the callback button.
    """
    bump(cid, "downloads")
    chat_action(cid, "upload_document")

    # Parse md5 and ext from the combined token
    parts = md5_ext.split("_", 1)
    md5 = parts[0].lower() if parts else ""
    ext = parts[1] if len(parts) > 1 else "pdf"

    # Validate: md5 must be 32 hex chars
    if not re.fullmatch(r"[a-f0-9]{32}", md5):
        log.error("do_book_download: invalid md5_ext=%r", md5_ext)
        _status(
            cid,
            "❌ اطلاعات دانلود نامعتبر است. دوباره جستجو کنید.",
            reply_markup=home_kb(), final=True)
        return

    # Try to get title from cache for a nicer filename
    title = "book"
    try:
        st = get_state(cid)
        cached = cache_get(st.get("cache_key", ""))
        if isinstance(cached, list):
            for b in cached:
                if (b.get("md5") or "").lower() == md5:
                    title = b.get("name", "book")
                    break
    except Exception:
        pass

    _status(
        cid,
        "⏳ در حال دانلود کتاب از Library Genesis…\n_(ممکن است چند ثانیه طول بکشد)_",
        parse_mode="Markdown",
    )

    result = libgen_download(md5, title=title, ext=ext)
    if not result:
        log.error("do_book_download: failed md5=%s", md5)
        _status(
            cid, "❌ دانلود ناموفق بود. دوباره امتحان کنید.", reply_markup=home_kb(), final=True)
        return

    data, fname = result
    log.info("libgen: sending %s  %.1fMB", fname, len(data) / 1024 / 1024)
    if smart_send(cid, data, fname, caption=f"📚 {fname[:80]}"):
        _status(cid, "✅ کتاب ارسال شد.", reply_markup=home_kb(), final=True)
    else:
        _status(cid, "❌ ارسال فایل ناموفق بود.", reply_markup=home_kb(), final=True)
    clear_state(cid)


# ── Sci-Hub paper search + download ─────────────────────────────────────────

SCIHUB_MIRRORS = [
    "https://sci-hub.se",  # historically the most stable domain — try first
    "https://sci-hub.ru",  # sometimes JS-gated, still worth a try
    "https://sci-hub.st",  # occasionally 403s, kept as last resort
]


def _extract_doi(text: str) -> str:
    """Extract a DOI from free text or URL."""
    m = re.search(r"\b(10\.\d{4,}/[^\s\"'<>]+)", text)
    return m.group(1).rstrip(".,)") if m else ""


def _scholar_search(query: str, sess: requests.Session) -> list[dict]:
    """
    Search papers. Priority: CrossRef → OpenAlex → Semantic Scholar (S2 rate-limits heavily).
    """

    def _parse_s2(items):
        out = []
        for it in items:
            doi = (it.get("externalIds") or {}).get("DOI", "")
            oa = (it.get("openAccessPdf") or {}).get("url", "")
            out.append(
                {
                    "title": it.get("title", ""),
                    "authors": [
                        a.get("name", "") for a in (it.get("authors") or [])[:3]
                    ],
                    "year": str(it.get("year") or ""),
                    "doi": doi,
                    "oa_url": oa,
                    "url": it.get("url", ""),
                }
            )
        return out

    # Strategy 1: CrossRef — no auth, very permissive, rarely rate-limits
    try:
        r = sess.get(
            "https://api.crossref.org/works",
            params={
                "query": query,
                "rows": 8,
                "select": "title,author,published,DOI,URL,link",
            },
            headers={"User-Agent": "BaleBot/1.0 (mailto:bot@example.com)"},
            timeout=15,
        )
        log.debug("_scholar_search CrossRef: HTTP %d", r.status_code)
        if r.status_code == 200:
            items = r.json().get("message", {}).get("items", [])
            results = []
            for it in items:
                title = (it.get("title") or [""])[0]
                authors = [
                    f"{a.get('given', '')} {a.get('family', '')}".strip()
                    for a in (it.get("author") or [])[:3]
                ]
                pub = it.get("published", {})
                year = str((pub.get("date-parts") or [[""]])[0][0])
                doi = it.get("DOI", "")
                oa_url = ""
                for lnk in it.get("link") or []:
                    if "pdf" in lnk.get("content-type", "").lower():
                        oa_url = lnk.get("URL", "")
                        break
                results.append(
                    {
                        "title": title,
                        "authors": authors,
                        "year": year,
                        "doi": doi,
                        "oa_url": oa_url,
                        "url": it.get("URL", ""),
                    }
                )
            for item in results:
                item["sources"] = ["Crossref"]
            crossref_results = results
        else:
            crossref_results = []
    except Exception as e:
        log.warning("_scholar_search CrossRef: %s", e)
        crossref_results = []

    # Strategy 2: OpenAlex (new API, requires no auth)
    try:
        r = sess.get(
            "https://api.openalex.org/works",
            params={
                "search": query,
                "select": "title,authorships,publication_year,doi,open_access,ids",
                "per_page": 8,
            },
            headers={"User-Agent": "BaleBot/1.0 (mailto:bot@example.com)"},
            timeout=15,
        )
        log.debug("_scholar_search OpenAlex: HTTP %d", r.status_code)
        if r.status_code == 200:
            data = r.json().get("results", [])
            results = []
            for it in data:
                title = it.get("title", "")
                authors = [
                    f"{a.get('author', {}).get('display_name', '')}".strip()
                    for a in it.get("authorships", [])[:3]
                    if a.get("author")
                ]
                year = str(it.get("publication_year", 0))
                # OpenAlex returns doi as full URL e.g. "https://doi.org/10.xxxx/..."
                raw_doi = it.get("doi") or ""
                doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", raw_doi)
                oa_url = it.get("open_access", {}).get("oa_url", "")
                results.append(
                    {
                        "title": title,
                        "authors": authors,
                        "year": year,
                        "doi": doi,
                        "oa_url": oa_url,
                        "url": raw_doi or "",
                        "sources": ["OpenAlex"],
                    }
                )
            openalex_results = results
        else:
            openalex_results = []
    except Exception as e:
        log.warning("_scholar_search OpenAlex: %s", e)
        openalex_results = []

    # Strategy 3: Semantic Scholar (best-effort — rate-limits aggressively at 429)
    s2_results = []
    for attempt in range(2):
        try:
            r = sess.get(
                "https://api.semanticscholar.org/graph/v1/paper/search",
                params={
                    "query": query,
                    "limit": 8,
                    "fields": "title,authors,year,externalIds,openAccessPdf,url",
                },
                headers={"User-Agent": "BaleBot/1.0"},
                timeout=15,
            )
            log.debug(
                "_scholar_search S2 attempt %d: HTTP %d", attempt + 1, r.status_code
            )
            if r.status_code == 200:
                s2_results = _parse_s2(r.json().get("data", []))
                for item in s2_results:
                    item["sources"] = ["Semantic Scholar"]
                break
            if r.status_code == 429:
                time.sleep(2)
                continue
            break
        except Exception as e:
            log.warning("_scholar_search S2: %s", e)
            break

    merged = {}
    for item in crossref_results + openalex_results + s2_results:
        doi = _extract_doi(item.get("doi", "")).lower()
        key = doi or re.sub(r"\W+", "", item.get("title", "").lower())
        if not key:
            continue
        old = merged.get(key)
        if old is None:
            merged[key] = item
            continue
        old["sources"] = list(dict.fromkeys(old.get("sources", []) + item.get("sources", [])))
        for field in ("title", "authors", "year", "oa_url", "url"):
            if not old.get(field) and item.get(field):
                old[field] = item[field]
        if item.get("oa_url"):
            old["oa_url"] = item["oa_url"]
    return list(merged.values())[:16]


def _scihub_download(
    doi_or_url: str, sess: requests.Session
) -> Optional[tuple[bytes, str]]:
    """
    Download a paper PDF via Sci-Hub mirrors.
    Uses WARP session (passed in) for DNS + IP bypass.
    """
    if not doi_or_url:
        return None
    # Clean up DOI
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi_or_url.strip())
    hdrs = {"User-Agent": UA_DESK, "Accept": "text/html,application/xhtml+xml,*/*"}

    for mirror in SCIHUB_MIRRORS:
        try:
            # Seed session cookies via homepage visit
            try:
                sess.get(mirror, headers=hdrs, timeout=8)
            except Exception:
                pass

            page_url = f"{mirror}/{doi}"
            rp = sess.get(
                page_url,
                headers={**hdrs, "Referer": mirror + "/"},
                timeout=20,
                allow_redirects=True,
            )
            log.debug(
                "_scihub_download %s: HTTP %d  len=%d",
                mirror,
                rp.status_code,
                len(rp.content),
            )
            if rp.status_code not in (200, 301, 302):
                continue

            soup = BeautifulSoup(rp.text, "html.parser")
            pdf_url = ""

            # Check if page contains error/captcha indicators
            error_text = rp.text.lower()
            if any(
                x in error_text
                for x in [
                    "captcha",
                    "robot",
                    "unavailable",
                    "not found",
                    "access denied",
                ]
            ):
                log.warning(
                    "_scihub_download %s: page contains error/captcha text", mirror
                )
                continue

            # Log all links and embeds to diagnose structure
            all_tags = [
                (t.name, t.get("src", ""), t.get("href", ""))
                for t in soup.find_all(["embed", "iframe", "a", "button"])
                if t.get("src") or t.get("href") or t.get("onclick")
            ]
            log.debug("_scihub_download %s tags: %s", mirror, all_tags[:10])

            # Pattern 1: embed or iframe with src
            for tag in soup.select("embed[src], iframe[src]"):
                src = tag.get("src", "")
                if src and len(src) > 5:
                    pdf_url = src
                    break

            # Pattern 1b: If BeautifulSoup missed the full iframe src (truncation),
            # try regex on raw HTML — sci-hub.ru embeds PDF in iframe
            if not pdf_url or (
                pdf_url
                and not pdf_url.lower().endswith(".pdf")
                and "/storage/" in pdf_url
            ):
                # Look for iframe src with .pdf in raw HTML
                iframe_match = re.search(
                    r'<iframe[^>]+src=["\']([^"\']*?\.pdf[^"\']*)["\']',
                    rp.text,
                    re.IGNORECASE,
                )
                if iframe_match:
                    pdf_url = iframe_match.group(1)
                    log.debug(
                        "_scihub_download: regex extracted iframe src: %s",
                        pdf_url[:200],
                    )

            # Pattern 2: #article, #buttons, download divs
            if not pdf_url:
                for sel in (
                    "#article a",
                    "#buttons a",
                    "#pdf-buttons a",
                    "div[id*='download'] a",
                    "a[href*='.pdf']",
                ):
                    el = soup.select_one(sel)
                    if el and el.get("href") and len(el.get("href", "")) > 5:
                        pdf_url = el["href"]
                        break

            # Pattern 3: onclick location.href
            if not pdf_url:
                for tag in soup.find_all(onclick=True):
                    m = re.search(
                        r"location\.href\s*=\s*['\"]([^'\"]+)['\"]",
                        tag.get("onclick", ""),
                    )
                    if m:
                        pdf_url = m.group(1)
                        break

            # Pattern 4: any src/href containing the DOI hash or .pdf
            if not pdf_url:
                for tag in soup.find_all(True):
                    for attr in ("src", "href", "data-src"):
                        val = tag.get(attr, "")
                        if (
                            val
                            and (".pdf" in val or "sci-hub" in val)
                            and len(val) > 10
                        ):
                            pdf_url = val
                            break
                    if pdf_url:
                        break

            if not pdf_url:
                log.debug(
                    "_scihub_download: no PDF link found at %s (body len=%d)",
                    mirror,
                    len(rp.content),
                )
                continue

            # Strip URL fragment (#view=FitH etc.) — sci-hub.ru iframes have this
            if "#" in pdf_url:
                pdf_url = pdf_url.split("#")[0]
            # Normalise URL
            if pdf_url.startswith("//"):
                pdf_url = "https:" + pdf_url
            elif pdf_url.startswith("/"):
                pdf_url = mirror + pdf_url
            elif not pdf_url.startswith("http"):
                pdf_url = mirror + "/" + pdf_url

            log.debug("_scihub_download: fetching PDF %s", pdf_url[:200])
            # Carry cookies from the DOI page response into the PDF request
            page_cookies = "; ".join(f"{k}={v}" for k, v in rp.cookies.items())

            def _fetch_pdf(extra_headers: dict):
                return sess.get(
                    pdf_url,
                    headers={
                        "User-Agent": UA_DESK,
                        "Referer": page_url,
                        "Cookie": page_cookies,
                        **extra_headers,
                    },
                    timeout=90,
                    allow_redirects=True,
                )

            resp = _fetch_pdf(
                {
                    "Accept": "application/pdf,application/octet-stream,*/*",
                    "Accept-Encoding": "identity",
                    "Origin": mirror,
                }
            )
            if resp.status_code == 200 and len(resp.content) == 0:
                # Some CDNs return an empty 200 body when the request looks
                # too "scripted" (forced identity encoding / custom Origin).
                # Retry once with a plain browser-like header set.
                log.debug(
                    "_scihub_download: empty body on first attempt, retrying with plain headers"
                )
                resp = _fetch_pdf({"Accept": "*/*"})
            ct = resp.headers.get("Content-Type", "")
            log.debug(
                "_scihub_download: PDF response HTTP %d  CT=%s  len=%d body_start=%r",
                resp.status_code,
                ct,
                len(resp.content),
                resp.content[:50] if resp.content else b"",
            )

            if not resp.content:
                log.warning("_scihub_download %s: empty PDF response, skipping", mirror)
                continue

            # Check if response looks like error/blocking HTML
            if len(resp.content) < 5000 and "text/html" in ct:
                # Likely a blocking page - check for common blocking patterns
                html_text = resp.text.lower()
                blocking_patterns = [
                    "captcha",
                    "robot",
                    "unavailable",
                    "not found",
                    "access denied",
                    "cloudflare",
                    "checking your browser",
                    "please enable cookies",
                    "checking if the site connection is secure",
                ]
                if any(pattern in html_text for pattern in blocking_patterns):
                    log.warning(
                        "_scihub_download %s: detected blocking/captcha page", mirror
                    )
                    continue
                # Additional check: look for PDF magic bytes
                if resp.content[:4] != b"%PDF":
                    log.warning(
                        "_scihub_download %s: response is HTML but no blocking patterns found",
                        mirror,
                    )
                    continue

            # Accept PDF by magic bytes, not just Content-Type
            is_pdf = (
                resp.content[:4] == b"%PDF"
                or "pdf" in ct.lower()
                or "octet" in ct.lower()
            )
            if resp.status_code == 200 and len(resp.content) > 5000 and is_pdf:
                safe = re.sub(r"[^\w\-]", "_", doi)[:60]
                fname = f"{safe}.pdf"
                cd = resp.headers.get("Content-Disposition", "")
                if "filename=" in cd:
                    mf = re.search(r"filename=([^;\r\n]+)", cd)
                    if mf:
                        fname = mf.group(1).strip().strip("\"'")
                log.info(
                    "_scihub_download OK: %.1fMB via %s",
                    len(resp.content) / 1024 / 1024,
                    mirror,
                )
                return resp.content, fname
        except Exception as e:
            log.warning("_scihub_download %s: %s", mirror, e)
    return None


def do_paper_search(cid: int, query: str):
    """جستجوی مقاله علمی via Semantic Scholar + دانلود از Sci-Hub."""
    bump(cid, "searches")
    _status(cid, f"⏳ در حال جستجوی مقاله: _{query}_…", parse_mode="Markdown")
    chat_action(cid)

    sess = _libgen_session()

    # If query looks like a DOI, skip search and go straight to download
    doi = _extract_doi(query)
    if doi:
        _do_paper_dl_doi(cid, doi, title=query, sess=sess)
        return

    results = _scholar_search(query, sess)
    if not results:
        _status(
            cid,
            "❌ مقاله‌ای یافت نشد. DOI یا عنوان دقیق‌تری امتحان کنید.",
            reply_markup=home_kb(), final=True)
        return

    key = make_cache_key("art", query, 0)
    cache_set(key, results)
    set_state(cid, mode="book_art", last_query=query, cache_key=key)

    rows = []
    for i, p in enumerate(results):
        label = f"{p['title'][:45]}…" if len(p["title"]) > 45 else p["title"]
        rows.append([{"text": label, "callback_data": f"paper_item_{key}_{i}"}])
    rows.append([{"text": "🏠 خانه", "callback_data": "home"}])
    kb = {"inline_keyboard": rows}
    _status(
        cid,
        f"📄 *نتایج Semantic Scholar:* _{query}_\nروی مقاله کلیک کنید:",
        parse_mode="Markdown",
        reply_markup=kb, final=True)

def _oa_pdf(doi: str, sess, on_step=None) -> Optional[tuple[bytes, str]]:
    """Try every free OA source for a DOI. Returns (pdf_bytes, source_name)."""
    clean = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi).strip().rstrip(".,)")
    hdrs = {"User-Agent": UA_DESK}

    def _try(pdf_url: str) -> Optional[bytes]:
        if not pdf_url:
            return None
        try:
            rr = sess.get(pdf_url, headers=hdrs, timeout=60, allow_redirects=True)
            if rr.status_code == 200 and len(rr.content) > 5000 and (
                rr.content[:4] == b"%PDF" or "pdf" in rr.headers.get("Content-Type", "").lower()):
                return rr.content
        except Exception as e:
            log.debug("_oa_pdf fetch %s: %s", pdf_url[:60], e)
        return None

    # 1) Unpaywall
    if on_step:
        on_step("Unpaywall")
    try:
        up = sess.get(f"https://api.unpaywall.org/v2/{clean}",
                      params={"email": UNPAYWALL_EMAIL}, timeout=12)
        if up.status_code == 200:
            loc = (up.json().get("best_oa_location") or {})
            data = _try(loc.get("url_for_pdf") or loc.get("url", ""))
            if data:
                return data, "Unpaywall"
    except Exception as e:
        log.debug("_oa_pdf unpaywall: %s", e)

    # 2) OpenAlex
    if on_step:
        on_step("OpenAlex")
    try:
        oa = sess.get(f"https://api.openalex.org/works/https://doi.org/{clean}", timeout=12)
        if oa.status_code == 200:
            j = oa.json()
            loc = j.get("best_oa_location") or j.get("primary_location") or {}
            data = _try(loc.get("pdf_url", ""))
            if data:
                return data, "OpenAlex"
    except Exception as e:
        log.debug("_oa_pdf openalex: %s", e)

    # 3) Semantic Scholar
    if on_step:
        on_step("Semantic Scholar")
    try:
        s2 = sess.get(f"https://api.semanticscholar.org/graph/v1/paper/DOI:{clean}",
                      params={"fields": "openAccessPdf"}, timeout=12)
        if s2.status_code == 200:
            data = _try((s2.json().get("openAccessPdf") or {}).get("url", ""))
            if data:
                return data, "Semantic Scholar"
    except Exception as e:
        log.debug("_oa_pdf s2: %s", e)

    # 4) Europe PMC (PMC full-text PDF)
    if on_step:
        on_step("Europe PMC")
    try:
        ep = sess.get("https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                      params={"query": f"DOI:{clean}", "format": "json", "resultType": "core"}, timeout=12)
        if ep.status_code == 200:
            for res in ep.json().get("resultList", {}).get("result", []):
                pmcid = res.get("pmcid")
                if pmcid:
                    data = _try(f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextPDF")
                    if data:
                        return data, "Europe PMC"
    except Exception as e:
        log.debug("_oa_pdf europepmc: %s", e)

    return None


def _paper_apa(paper: dict) -> str:
    authors = ", ".join(paper.get("authors") or []) or "نویسنده نامشخص"
    year = paper.get("year") or "بی‌تاریخ"
    title = paper.get("title") or "عنوان نامشخص"
    doi = paper.get("doi") or ""
    return f"{authors} ({year}). {title}." + (f" https://doi.org/{doi}" if doi else "")


def _paper_bibtex(paper: dict) -> str:
    doi = paper.get("doi") or "paper"
    key = re.sub(r"[^A-Za-z0-9]", "", doi)[-24:] or "paper"
    fields = {"title": paper.get("title", ""), "author": " and ".join(paper.get("authors") or []), "year": paper.get("year", ""), "doi": doi}
    return "@article{" + key + ",\n" + ",\n".join(f"  {name} = {{{value}}}" for name, value in fields.items() if value) + "\n}"


def _do_paper_dl_doi(cid: int, doi: str, title: str = "", sess=None):
    """Download a paper by DOI with a LIVE progress message.
    Order: Open-Access sources -> Anna's SciDB -> Sci-Hub -> LibGen scimag."""
    if sess is None:
        sess = _libgen_session()
    chat_action(cid, "upload_document")
    safe_title = re.sub(r"[^\w\-]", "_", title or doi)[:60]
    fname = safe_title + ".pdf"
    clean_doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi).strip().rstrip(".,)")

    warp = "WARP \u2705" if WARP_PROXY else "\u26a0\ufe0f WARP off"
    prog = Progress(cid, title="\U0001F50E \u062f\u0627\u0646\u0644\u0648\u062f \u0645\u0642\u0627\u0644\u0647  (" + warp + ")", total_steps=8)

    def _deliver(data, f, src):
        prog.finish("\u06cc\u0627\u0641\u062a \u0634\u062f \u062f\u0631 " + src + " \u2014 \u0627\u0631\u0633\u0627\u0644\u2026", ok=True)
        if smart_send(cid, data, f, caption="\U0001F4C4 " + (title or doi)):
            send_message(cid, "\u2705 \u0645\u0642\u0627\u0644\u0647 \u0627\u0631\u0633\u0627\u0644 \u0634\u062f (" + src + ").", reply_markup=home_kb())
        else:
            send_message(cid, "\u274c \u0627\u0631\u0633\u0627\u0644 \u0646\u0627\u0645\u0648\u0641\u0642 \u0628\u0648\u062f.", reply_markup=home_kb())
        clear_state(cid)

    # 1) Open Access (multi-source; each reported live)
    oa = _oa_pdf(doi, sess, on_step=lambda nm: prog.step("\u0628\u0631\u0631\u0633\u06cc " + nm + "\u2026"))
    if oa:
        _deliver(oa[0], fname, "Open Access \u2013 " + oa[1])
        return

    # 2) Anna's Archive SciDB (Sci-Hub mirror; needs WARP)
    prog.step("\u0628\u0631\u0631\u0633\u06cc Anna's Archive SciDB\u2026")
    res = _annas_scidb(doi, sess)
    if res:
        _deliver(res[0], fname, res[1])
        return

    # 3) Sci-Hub mirrors (through WARP)
    prog.step("\u0628\u0631\u0631\u0633\u06cc Sci-Hub\u2026")
    scihub_sess = _get_web(use_warp=True) if WARP_PROXY else sess
    result = _scihub_download(doi, scihub_sess)
    if result:
        _deliver(result[0], result[1], "Sci-Hub")
        return

    # 4) LibGen scimag (DOI -> md5 -> download)
    prog.step("\u0628\u0631\u0631\u0633\u06cc LibGen\u2026")
    try:
        for mirror in LIBGEN_MIRRORS:
            for params in [{"object": "f", "topic": "a", "doi": clean_doi},
                           {"req": clean_doi}]:
                try:
                    r = sess.get(mirror + "/json.php", params=params,
                                 headers={"User-Agent": UA_DESK}, timeout=(6, 15))
                    if r.status_code != 200 or len(r.content) < 5:
                        continue
                    md5 = ""
                    try:
                        jd = r.json()
                        items = (list(jd.values()) if isinstance(jd, dict)
                                 else (jd if isinstance(jd, list) else []))
                        for it in items:
                            if isinstance(it, dict) and it.get("md5"):
                                md5 = str(it["md5"]).lower().strip()
                                break
                    except Exception:
                        pass
                    if not md5:
                        continue
                    got = libgen_download(md5, title=(title or doi)[:60], ext="pdf")
                    if got:
                        _deliver(got[0], got[1], "LibGen")
                        return
                except Exception as pe:
                    log.debug("paper libgen attempt: %s", pe)
    except Exception as e:
        log.warning("paper libgen: %s", e)

    # Nothing worked
    prog.finish("\u062f\u0631 \u0647\u06cc\u0686 \u0645\u0646\u0628\u0639\u06cc \u06cc\u0627\u0641\u062a \u0646\u0634\u062f.", ok=False)
    tail = "" if WARP_PROXY else "\u26a0\ufe0f WARP \u0641\u0639\u0627\u0644 \u0646\u06cc\u0633\u062a \u2014 Sci-Hub/Anna's \u0645\u0633\u062f\u0648\u062f \u0627\u0633\u062a.\n"
    send_message(
        cid,
        "\u274c \u0627\u06cc\u0646 \u0645\u0642\u0627\u0644\u0647 \u062f\u0631 \u062f\u0633\u062a\u0631\u0633 \u0646\u0628\u0648\u062f.\n\n"
        "\u2139\ufe0f \u0645\u0646\u0627\u0628\u0639: Open Access (Unpaywall/OpenAlex/S2/Europe PMC) + Anna's SciDB + Sci-Hub + LibGen.\n"
        + tail
        + "\U0001F517 DOI: `" + doi + "`",
        parse_mode="Markdown", reply_markup=home_kb(),
    )
    clear_state(cid)


def do_apk_search(cid: int, query: str):
    """Search Google Play and show results as clickable buttons."""
    bump(cid, "searches")
    _status(
        cid, f"🔍 در حال جستجو در Google Play: _{query}_…", parse_mode="Markdown"
    )
    chat_action(cid)
    results = gplay_search(query, n_hits=8)
    if not results:
        _status(
            cid,
            "❌ اپلیکیشنی یافت نشد.\n"
            "• نام پکیج را امتحان کنید (مثل `org.telegram.messenger`)\n"
            "• اپ ممکن است در منطقه شما در دسترس نباشد",
            parse_mode="Markdown",
            reply_markup=home_kb(), final=True)
        return
    key = make_cache_key("apk", query, 0)
    cache_set(key, results)
    set_state(cid, mode="apk", last_query=query, cache_key=key)
    kb = apk_results_kb(results, key)
    _status(
        cid,
        f"📱 *نتایج Google Play برای:* _{query}_\nبرای جزئیات روی اپ کلیک کنید:",
        parse_mode="Markdown",
        reply_markup=kb, final=True)


def do_movie_search(cid: int, query: str, page: int = 1):
    """Search TMDb for movies/TV shows and show results with pagination."""
    bump(cid, "searches")
    _status(
        cid, f"🔍 در حال جستجوی فیلم/سریال: _{query}_…", parse_mode="Markdown"
    )
    chat_action(cid)

    import requests
    tmdb_key = TMDB_API_KEY or os.getenv('TMDB_API_KEY', '')
    if not tmdb_key:
        _status(
            cid,
            "❌ کلید TMDb تنظیم نشده. در .env یا config.py مقدار `TMDB_API_KEY` را ست کنید.",
            reply_markup=home_kb(), final=True)
        return

    try:
        url = "https://api.themoviedb.org/3/search/multi"
        params = {
            "api_key": tmdb_key,
            "query": query,
            "language": "en-US",
            "include_adult": "false",
            "page": page
        }
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        results = data.get("results", [])
        total_pages = data.get("total_pages", 1)

        filtered = [r for r in results if r.get("media_type") in ("movie", "tv")][:10]

        if not filtered:
            _status(
                cid, "❌ نتیجه‌ای یافت نشد. عبارت دیگری امتحان کنید.",
                reply_markup=home_kb(), final=True)
            return

        key = make_cache_key("movie", query, page)
        cache_set(key, filtered)
        set_state(cid, mode="movie", last_query=query, cache_key=key, page=page)

        rows = []
        for i, item in enumerate(filtered):
            title = item.get("title") or item.get("name", "بدون نام")
            media_type = "🎬" if item.get("media_type") == "movie" else "📺"
            year = (item.get("release_date") or item.get("first_air_date") or "")[:4]
            label = f"{media_type} {title}"
            if year:
                label += f" ({year})"
            rows.append([{"text": label[:60], "callback_data": f"movie_item_{key}_{i}"}])

        # Pagination buttons
        if total_pages > 1:
            nav = []
            if page > 1:
                nav.append({"text": "◀️ قبلی", "callback_data": f"movie_page_{key}_{page-1}"})
            nav.append({"text": f"📄 {page}/{total_pages}", "callback_data": "noop"})
            if page < total_pages:
                nav.append({"text": "بعدی ▶️", "callback_data": f"movie_page_{key}_{page+1}"})
            rows.append(nav)

        rows.append([{"text": "🏠 خانه", "callback_data": "home"}])
        kb = {"inline_keyboard": rows}

        _status(
            cid,
            f"🎬 *نتایج جستجو:* _{query}_\nروی هر مورد کلیک کنید:",
            parse_mode="Markdown",
            reply_markup=kb, final=True)

    except Exception as e:
        log.error("do_movie_search: %s", e)
        _status(cid, "❌ خطا در جستجو.", reply_markup=home_kb(), final=True)


def tmdb_search(query: str, max_results: int = 8) -> list[dict]:
    """Search TMDb for movies + TV. Returns list of dicts with id, type, name, year, poster, overview."""
    if not TMDB_API_KEY:
        return []
    try:
        r = requests.get("https://api.themoviedb.org/3/search/multi",
                    params={"api_key": TMDB_API_KEY, "query": query, "include_adult": "false", "language": "fa-IR"},
                    timeout=15)
        if r.status_code != 200:
            return []
        out = []
        for it in r.json().get("results", []):
            mt = it.get("media_type")
            if mt not in ("movie", "tv"):
                continue
            name = it.get("title") or it.get("name") or ""
            date = it.get("release_date") or it.get("first_air_date") or ""
            out.append({"id": it.get("id"), "type": mt, "name": name,
                        "year": (date or "")[:4],
                        "poster": f"https://image.tmdb.org/t/p/w500{it.get('poster_path')}" if it.get("poster_path") else "",
                        "overview": it.get("overview", ""), "_source": "tmdb"})
            if len(out) >= max_results:
                break
        return out
    except Exception as e:
        log.error("tmdb_search: %s", e)
        return []


def tmdb_details(item: dict) -> dict:
    """Full details incl. cast, director/creator, genres, rating, runtime, imdb_id."""
    if item.get("_source") != "tmdb" or not TMDB_API_KEY:
        return item
    kind = item.get("type", "movie")
    try:
        r = requests.get(f"https://api.themoviedb.org/3/{kind}/{item['id']}",
                    params={"api_key": TMDB_API_KEY, "language": "fa-IR", "append_to_response": "credits,external_ids"},
                    timeout=15)
        if r.status_code != 200:
            return item
        j = r.json()
        credits = j.get("credits", {})
        cast = [c.get("name", "") for c in credits.get("cast", [])[:6] if c.get("name")]
        crew = credits.get("crew", [])
        if kind == "movie":
            directors = [c["name"] for c in crew if c.get("job") == "Director"]
        else:
            directors = [c.get("name", "") for c in j.get("created_by", [])]
        item.update({
            "overview": j.get("overview", item.get("overview", "")),
            "genres": [g["name"] for g in j.get("genres", [])],
            "rating": j.get("vote_average", ""),
            "runtime": j.get("runtime") or (j.get("episode_run_time") or [None])[0],
            "seasons": j.get("number_of_seasons"),
            "episodes": j.get("number_of_episodes"),
            "cast": cast, "directors": [d for d in directors if d],
            "tagline": j.get("tagline", ""),
            "imdb_id": j.get("imdb_id") or j.get("external_ids", {}).get("imdb_id", ""),
        })
    except Exception as e:
        log.debug("tmdb_details: %s", e)
    return item


def tmdb_season(tv_id: int, season_number: int) -> list[dict]:
    """Episodes of one TV season (name, number, air_date, rating)."""
    if not TMDB_API_KEY:
        return []
    try:
        r = requests.get(f"https://api.themoviedb.org/3/tv/{tv_id}/season/{season_number}",
                    params={"api_key": TMDB_API_KEY, "language": "fa-IR"}, timeout=15)
        if r.status_code != 200:
            return []
        out = []
        for ep in r.json().get("episodes", []):
            out.append({"n": ep.get("episode_number"), "name": ep.get("name", ""),
                        "air_date": ep.get("air_date", ""),
                        "rating": ep.get("vote_average", "")})
        return out
    except Exception as e:
        log.error("tmdb_season: %s", e)
        return []


def tmdb_watch_providers(kind: str, tmdb_id: int, region: str = "IR") -> dict:
    """Streaming/rent/buy availability for a region via TMDb (JustWatch data)."""
    if not TMDB_API_KEY:
        return {}
    try:
        r = requests.get(f"https://api.themoviedb.org/3/{kind}/{tmdb_id}/watch/providers",
                    params={"api_key": TMDB_API_KEY}, timeout=15)
        if r.status_code != 200:
            return {}
        data = r.json().get("results", {}).get(region, {}) or {}
        out = {"link": data.get("link", "")}
        for k in ("flatrate", "rent", "buy"):
            out[k] = [p.get("provider_name", "") for p in data.get(k, [])]
        return out
    except Exception as e:
        log.error("tmdb_watch_providers: %s", e)
        return {}


def omdb_search(query: str, max_results: int = 8) -> list[dict]:
    """OMDb fallback search (unofficial IMDb data). Requires OMDB_API_KEY."""
    if not OMDB_API_KEY:
        return []
    try:
        r = requests.get("https://www.omdbapi.com/",
                    params={"apikey": OMDB_API_KEY, "s": query}, timeout=15)
        out = []
        for it in (r.json().get("Search", []) if r.status_code == 200 else []):
            out.append({"id": it.get("imdbID"), "type": it.get("Type", "movie"),
                        "name": it.get("Title", ""), "year": it.get("Year", ""),
                        "poster": it.get("Poster", "") if it.get("Poster") != "N/A" else "",
                        "overview": "", "_source": "omdb"})
            if len(out) >= max_results:
                break
        return out
    except Exception as e:
        log.error("omdb_search: %s", e)
        return []


def omdb_details(item: dict) -> dict:
    if item.get("_source") != "omdb" or not OMDB_API_KEY:
        return item
    try:
        r = requests.get("https://www.omdbapi.com/",
                    params={"apikey": OMDB_API_KEY, "i": item["id"], "plot": "full"}, timeout=15)
        if r.status_code == 200:
            j = r.json()
            item.update({"overview": j.get("Plot", ""),
                         "genres": [g.strip() for g in j.get("Genre", "").split(",") if g.strip()],
                         "rating": j.get("imdbRating", ""),
                         "cast": [c.strip() for c in j.get("Actors", "").split(",") if c.strip()],
                         "directors": [d.strip() for d in j.get("Director", "").split(",") if d.strip() and d != "N/A"],
                         "runtime": j.get("Runtime", "")})
    except Exception as e:
        log.debug("omdb_details: %s", e)
    return item


# DDL mirrors
DDL_MOVIE_HOSTS = [h for h in os.getenv("DDL_MOVIE_HOSTS", "https://berlin.saymyname.website").split(",") if h]
DDL_SERIES_HOSTS = [h for h in os.getenv("DDL_SERIES_HOSTS", "https://rio.ggusers.com,https://nairobi.saymyname.website").split(",") if h]
VIDEO_EXTS = (".mkv", ".mp4", ".avi", ".m4v")
_Q_ORDER = {"2160p": 0, "1080p": 1, "720p": 2, "480p": 3, "": 9}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _guess_quality(s: str) -> str:
    for q in ("2160p", "1080p", "720p", "480p"):
        if q in (s or ""):
            return q
    return ""


def _ep_match(nm: str, season: int, episode: int) -> bool:
    nm2 = (nm or "").upper()
    return bool(re.search(rf"S0*{int(season)}E0*{int(episode)}(?!\d)", nm2)
                or re.search(rf"(?<![A-Z0-9])E0*{int(episode)}(?!\d)", nm2))


def _ddl_list(url: str) -> list[dict]:
    """Parse an open-directory (nginx/apache autoindex) page into entries."""
    try:
        r = WEB.get(url, headers={"User-Agent": UA_DESK}, timeout=20)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        base = url if url.endswith("/") else url + "/"
        out = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("?") or href in ("../", "./", "/"):
                continue
            if href.startswith("http") and not href.startswith(base):
                continue
            full = urllib.parse.urljoin(base, href)
            name = urllib.parse.unquote(href.rstrip("/")).split("/")[-1]
            out.append({"name": name, "url": full, "is_dir": href.endswith("/")})
        return out
    except Exception as e:
        log.debug("_ddl_list %s: %s", url, e)
        return []


def ddl_movie_links(name: str, year: str, imdb_id: str = "") -> list[dict]:
    """Return direct download directory links for user to open with Iran IP."""
    imdbnum = imdb_id.replace("tt", "") if imdb_id else ""
    if not (year and imdbnum):
        return []
    out = []
    for host in DDL_MOVIE_HOSTS:
        base = f"{host}/Movies/{year}/{imdbnum}/"
        out.append({"quality": "Direct", "name": f"{name} ({year})", "url": base})
    return out


def ddl_series_links(name: str, season: int, episode: int) -> list[dict]:
    """Return direct download directory links for user to open with Iran IP."""
    base_name = re.sub(r"^(the|a|an)\s+", "", (name or "").strip(), flags=re.I)
    letter = (base_name[:1] or name[:1] or "0").upper()
    ss = f"S{int(season):02d}"
    out = []
    for host in DDL_SERIES_HOSTS:
        show_dir = f"{host}/Series/{letter}/{name}/"
        season_dir = f"{show_dir}{ss}/"
        out.append({"quality": "Direct", "name": f"{name} S{int(season):02d}E{int(episode):02d}", "url": season_dir})
    return out


def yts_torrents(query: str, imdb_id: str = "") -> list[dict]:
    """Public YTS torrents for a movie (quality/size/seeds + magnet). Movies only."""
    try:
        r = requests.get("https://yts.mx/api/v2/list_movies.json",
                    params={"query_term": imdb_id or query, "limit": 1}, timeout=15)
        if r.status_code != 200:
            return []
        movies = r.json().get("data", {}).get("movies", []) or []
        if not movies:
            return []
        m0 = movies[0]
        trackers = ("&tr=udp://tracker.opentrackr.org:1337/announce"
                    "&tr=udp://open.demonii.com:1337/announce"
                    "&tr=udp://tracker.openbittorrent.com:80")
        out = []
        for t in m0.get("torrents", []):
            magnet = (f"magnet:?xt=urn:btih:{t.get('hash')}"
                      f"&dn={urllib.parse.quote(m0.get('title', ''))}" + trackers)
            out.append({"quality": t.get("quality", ""), "type": t.get("type", ""),
                        "size": t.get("size", ""), "seeds": t.get("seeds", 0),
                        "magnet": magnet})
        return out
    except Exception as e:
        log.error("yts_torrents: %s", e)
        return []


def subtitles_fa(imdb_id: str = "", query: str = "", season: int = None, episode: int = None) -> list[dict]:
    """Search Persian subtitles. OpenSubtitles API (with key) -> subf2m scrape."""
    out = []
    if OPENSUBTITLES_API_KEY:
        try:
            params = {"languages": "fa"}
            if imdb_id:
                params["imdb_id"] = imdb_id.replace("tt", "")
            elif query:
                params["query"] = query
            if season is not None:
                params["season_number"] = season
            if episode is not None:
                params["episode_number"] = episode
            r = requests.get("https://api.opensubtitles.com/api/v1/subtitles", params=params,
                        headers={"Api-Key": OPENSUBTITLES_API_KEY,
                                 "User-Agent": "BaleBot v3.0", "Accept": "application/json"},
                        timeout=15)
            if r.status_code == 200:
                data = sorted(r.json().get("data", []),
                              key=lambda d: d.get("attributes", {}).get("download_count", 0),
                              reverse=True)
                for d in data[:8]:
                    at = d.get("attributes", {})
                    files = at.get("files", [])
                    if not files:
                        continue
                    out.append({"name": at.get("release") or query or "subtitle",
                                "file_id": files[0].get("file_id"),
                                "downloads": at.get("download_count", 0),
                                "_source": "opensubtitles"})
                if out:
                    return out
        except Exception as e:
            log.error("subtitles_fa opensubtitles: %s", e)
    # subf2m.co fallback
    if query:
        try:
            s = WEB.get("https://subf2m.co/subtitles/searchbytitle",
                        params={"query": query}, headers={"User-Agent": UA_DESK}, timeout=20)
            m = re.search(r'href="(/subtitles/[^"]+)"', s.text)
            if m:
                fa_url = f"https://subf2m.co{m.group(1)}/farsi_persian"
                lp = WEB.get(fa_url, headers={"User-Agent": UA_DESK}, timeout=20)
                for mm in re.finditer(r'href="(/subtitles/[^"]+?)"', lp.text):
                    href = mm.group(1)
                    if href.count("/") < 3:
                        continue
                    out.append({"name": href.split("/")[-1][:60],
                                "page": f"https://subf2m.co{href}", "_source": "subf2m"})
                    if len(out) >= 8:
                        break
        except Exception as e:
            log.error("subtitles_fa subf2m: %s", e)
    return out


def subtitle_download(entry: dict):
    """Fetch the actual .srt/.zip for a subtitle search entry. Returns (bytes, filename) or None."""
    src = entry.get("_source")
    try:
        if src == "opensubtitles" and OPENSUBTITLES_API_KEY:
            r = requests.post("https://api.opensubtitles.com/api/v1/download",
                         json={"file_id": entry["file_id"]},
                         headers={"Api-Key": OPENSUBTITLES_API_KEY, "User-Agent": "BaleBot v3.0",
                                  "Content-Type": "application/json", "Accept": "application/json"},
                         timeout=20)
            if r.status_code == 200:
                j = r.json()
                link, fn = j.get("link", ""), j.get("file_name", "subtitle.srt")
                if link:
                    sr = WEB.get(link, timeout=60)
                    if sr.status_code == 200 and len(sr.content) > 50:
                        if not fn.lower().endswith((".srt", ".ass", ".vtt")):
                            fn += ".srt"
                        return sr.content, fn
        elif src == "subf2m":
            pg = WEB.get(entry["page"], headers={"User-Agent": UA_DESK}, timeout=20)
            dm = (re.search(r'href="([^"]*/download[^"]*)"', pg.text)
                  or re.search(r'href="([^"]+?\.zip)"', pg.text))
            if dm:
                durl = dm.group(1)
                if not durl.startswith("http"):
                    durl = f"https://subf2m.co{durl}"
                dr = WEB.get(durl, headers={"User-Agent": UA_DESK, "Referer": entry["page"]}, timeout=60)
                if dr.status_code == 200 and len(dr.content) > 100:
                    if dr.content[:2] == b"PK":
                        try:
                            zf = zipfile.ZipFile(io.BytesIO(dr.content))
                            for nm in zf.namelist():
                                if nm.lower().endswith((".srt", ".ass")):
                                    return zf.read(nm), nm.split("/")[-1]
                        except Exception:
                            pass
                    return dr.content, "subtitle_fa.srt"
    except Exception as e:
        log.error("subtitle_download: %s", e)
    return None


def _send_ddl_links(cid: int, title: str, links: list[dict]):
    """Send direct links as plain text (URL-safe) + url inline buttons."""
    if not links:
        send_message(cid, "❌ لینک مستقیم یافت نشد.")
        return
    lines = [f"⬇️ لینک مستقیم: {title}", ""]
    btns = []
    for l in links[:8]:
        q = l.get("quality") or "?"
        lines.append(f"• {q} — {l['name']}")
        lines.append(l["url"])
        lines.append("")
        btns.append([{"text": f"⬇️ {q}", "url": l["url"]}])
    send_message(cid, "\n".join(lines)[:4096],
                 reply_markup={"inline_keyboard": btns} if btns else None)


def movie_detail_kb(item: dict, cache_key: str, idx: int) -> dict:
    rows = []
    if item.get("type") == "tv" and item.get("seasons"):
        srow = []
        for s in range(1, int(item["seasons"]) + 1):
            srow.append({"text": f"فصل {s}", "callback_data": f"movie_season_{cache_key}_{idx}_{s}"})
            if len(srow) == 4:
                rows.append(srow); srow = []
            if s >= 20:
                break
        if srow:
            rows.append(srow)
    rows.append([{"text": "📝 زیرنویس فارسی", "callback_data": f"movie_subs_{cache_key}_{idx}"},
                 {"text": "🎟 پخش/دانلود", "callback_data": f"movie_watch_{cache_key}_{idx}"}])
    rows.append([{"text": "🏠 خانه", "callback_data": "home"}])
    return {"inline_keyboard": rows}


def do_movie_show(cid: int, item: dict, cache_key: str = "", idx: int = 0):
    """Show detailed info for a movie/TV show from TMDb/OMDb."""
    item = tmdb_details(item) if item.get("_source") == "tmdb" else omdb_details(item)
    if item.get("poster"):
        try:
            img = download_bytes(item["poster"], MAX_IMAGE_SIZE)
            if img and len(img) > 500:
                send_photo(cid, img, caption=item.get("name", "")[:80])
        except Exception as e:
            log.debug("movie poster: %s", e)
    icon = "🎬" if item.get("type") == "movie" else "📺"
    lines = [f"{icon} *{item.get('name','')}* ({item.get('year','')})"]
    if item.get("tagline"):
        lines.append(f"_{item['tagline']}_")
    if item.get("genres"):
        lines.append(f"🏷 {', '.join(item['genres'])}")
    if item.get("rating"):
        lines.append(f"⭐ {item['rating']}")
    if item.get("runtime"):
        lines.append(f"⏱ {item['runtime']} min")
    if item.get("seasons"):
        lines.append(f"📺 {item['seasons']} فصل / {item.get('episodes','?')} قسمت")
    if item.get("directors"):
        lbl = "🎥 کارگردان" if item.get("type") == "movie" else "🎥 سازنده"
        lines.append(f"{lbl}: {', '.join(item['directors'])}")
    if item.get("cast"):
        lines.append(f"👤 بازیگران: {', '.join(item['cast'])}")
    if item.get("overview"):
        lines.append(f"\n📝 {item['overview'][:600]}")
    send_message(cid, "\n".join(lines), parse_mode="Markdown",
                 reply_markup=movie_detail_kb(item, cache_key, idx))


def do_movie_season(cid: int, item: dict, season: int, cache_key: str = "", idx: int = 0):
    eps = tmdb_season(item.get("id"), season)
    if not eps:
        send_message(cid, "❌ اطلاعات فصل در دسترس نیست.", reply_markup=home_kb())
        return
    lines = [f"📺 {item.get('name','')} — فصل {season}", ""]
    for ep in eps:
        star = f" ⭐{ep['rating']}" if ep.get("rating") else ""
        ad = f" ({ep['air_date']})" if ep.get("air_date") else ""
        lines.append(f"{ep['n']}. {ep['name']}{ad}{star}")
    rows, srow = [], []
    for ep in eps:
        srow.append({"text": str(ep["n"]),
                     "callback_data": f"movie_ep_{cache_key}_{idx}_{season}_{ep['n']}"})
        if len(srow) == 5:
            rows.append(srow); srow = []
    if srow:
        rows.append(srow)
    rows.append([{"text": "📝 زیرنویس فارسی فصل", "callback_data": f"movie_subsn_{cache_key}_{idx}_{season}"}])
    rows.append([{"text": "🔙 برگشت", "callback_data": f"movie_item_{cache_key}_{idx}"},
                 {"text": "🏠 خانه", "callback_data": "home"}])
    send_message(cid, "\n".join(lines)[:4096], reply_markup={"inline_keyboard": rows})


def do_movie_episode(cid: int, item: dict, season: int, episode: int,
                     cache_key: str = "", idx: int = 0):
    item = tmdb_details(item) if item.get("_source") == "tmdb" else omdb_details(item)
    send_message(cid, f"⏳ یافتن لینک S{int(season):02d}E{int(episode):02d}…")
    chat_action(cid)
    links = ddl_series_links(item.get("name", ""), season, episode)
    _send_ddl_links(cid, f"{item.get('name','')} S{int(season):02d}E{int(episode):02d}", links)
    rows = [[{"text": "📝 زیرنویس فارسی این قسمت",
              "callback_data": f"movie_subse_{cache_key}_{idx}_{season}_{episode}"}],
            [{"text": "🔙 برگشت", "callback_data": f"movie_season_{cache_key}_{idx}_{season}"},
             {"text": "🏠 خانه", "callback_data": "home"}]]
    send_message(cid, "⚙️ گزینه‌ها:", reply_markup={"inline_keyboard": rows})


def do_movie_watch(cid: int, item: dict):
    item = tmdb_details(item) if item.get("_source") == "tmdb" else omdb_details(item)
    kind = "tv" if item.get("type") == "tv" else "movie"
    lines = [f"🎟 {item.get('name','')}"]
    prov = {}
    if item.get("_source") == "tmdb":
        prov = tmdb_watch_providers(kind, item.get("id"), region="IR")
        if not (prov.get("flatrate") or prov.get("rent") or prov.get("buy")):
            prov = tmdb_watch_providers(kind, item.get("id"), region="US")
    if prov.get("flatrate"):
        lines.append(f"📡 استریم: {', '.join(prov['flatrate'])}")
    if prov.get("rent"):
        lines.append(f"💵 اجاره: {', '.join(prov['rent'])}")
    if prov.get("buy"):
        lines.append(f"🛒 خرید: {', '.join(prov['buy'])}")
    if prov.get("link"):
        lines.append(f"🔗 {prov['link']}")
    send_message(cid, "\n".join(lines)[:4096], reply_markup=home_kb())

    if kind == "movie":
        # Direct download mirrors
        links = ddl_movie_links(item.get("name", ""), item.get("year", ""), item.get("imdb_id", ""))
        _send_ddl_links(cid, f"{item.get('name','')} ({item.get('year','')})", links)

        # YTS Torrents
        yts = yts_torrents(item.get("name", ""), item.get("imdb_id", ""))
        if yts:
            yts_lines = [f"🧲 *تورنت‌ها (YTS):*"]
            for t in yts[:5]:
                yts_lines.append(f"• {t['quality']} {t['type']} — {t['size']} — {t['seeds']} seeders")
            send_message(cid, "\n".join(yts_lines), parse_mode="Markdown")
            # Send magnet links as buttons
            rows = [[{"text": f"🧲 {t['quality']} {t['size']}", "url": t['magnet']}] for t in yts[:5]]
            send_message(cid, "مگنت لینک‌ها:", reply_markup={"inline_keyboard": rows})


def do_movie_subs(cid: int, item: dict, season=None, episode=None):
    item = tmdb_details(item) if item.get("_source") == "tmdb" else omdb_details(item)
    send_message(cid, "⏳ در حال جستجوی زیرنویس فارسی…")
    chat_action(cid, "upload_document")
    subs = subtitles_fa(imdb_id=item.get("imdb_id", ""), query=item.get("name", ""),
                        season=season, episode=episode)
    if not subs:
            send_message(cid, "❌ زیرنویس فارسی یافت نشد.", reply_markup=home_kb())
            return
    # Show subtitle options as buttons
    rows = []
    for i, entry in enumerate(subs[:8]):
        src = entry.get("_source", "?")
        name = entry.get("name", "subtitle")[:50]
        dl = entry.get("downloads", 0)
        rows.append([{"text": f"{src}: {name} ({dl}↓)", "callback_data": f"sub_dl_{i}"}])
    rows.append([{"text": "🏠 خانه", "callback_data": "home"}])
    # Cache subs for callback
    key = make_cache_key("subs", item.get("imdb_id", "") or item.get("name", ""), 0)
    cache_set(key, subs)
    set_state(cid, subtitle_cache_key=key)
    send_message(cid, "زیرنویس‌های یافت شده (روی یکی کلیک کنید):", reply_markup={"inline_keyboard": rows})


def do_apk_show(cid: int, app: dict):
    """Show full app info with icon, then download button."""
    app_id = app.get("app_id", "")
    title = app.get("title", "Unknown")
    dev = app.get("developer", "")
    score = app.get("score", 0)
    inst = app.get("installs", "")
    size = app.get("size", "")
    version = app.get("version", "")
    android = app.get("android", "")
    cat = app.get("category", "")
    summary = app.get("summary", "") or app.get("description", "")
    free = app.get("free", True)
    price = app.get("price", "Free")
    icon = app.get("icon", "")

    # Send app icon
    if icon:
        try:
            icon_bytes = download_bytes(icon, MAX_IMAGE_SIZE)
            if icon_bytes and len(icon_bytes) > 500:
                send_photo(cid, icon_bytes, caption=f"📱 {title}")
        except Exception as e:
            log.warning("apk icon: %s", e)

    # App info text
    lines = [f"📱 *{title}*"]
    if dev:
        lines.append(f"👤 Developer: {dev}")
    if score:
        lines.append(f"⭐ Rating: {score:.1f}/5")
    if inst:
        lines.append(f"📊 Installs: {inst}")
    if version:
        lines.append(f"🔢 Version: {version}")
    if size:
        lines.append(f"💾 Size: {size}")
    if android:
        lines.append(f"🤖 Android: {android}")
    if cat:
        lines.append(f"🏷 Category: {cat}")
    lines.append(f"💳 Price: {'Free' if free else price}")
    if summary:
        lines.append(f"\n_{summary[:300]}_")
    lines.append(f"\n`{app_id}`")

    send_message(
        cid, "\n".join(lines), parse_mode="Markdown", reply_markup=apk_item_kb(app_id)
    )


def do_apk_download(cid: int, app_id: str):
    """Download APK and send to user."""
    bump(cid, "downloads")
    # Get fresh info
    info = gplay_app_info(app_id)
    name = info.get("title", app_id) if info else app_id
    size = info.get("size", "") if info else ""
    size_hint = f" (~{size})" if info and size else ""

    # Paid-app early exit — free mirrors never have paid apps
    if info and info.get("free") is False:
        _status(
            cid,
            f"💰 *{name}* اپ پولی است.\n"
            "منابع رایگان (APKPure/Aptoide/F-Droid) اپ‌های پولی را ارائه نمی‌دهند.",
            parse_mode="Markdown",
            reply_markup=home_kb(), final=True,
        )
        clear_state(cid)
        return

    _status(
        cid,
        f"⏳ در حال دانلود APK: *{name}*{size_hint}\n"
        f"`{app_id}`\n\n"
        "در حال امتحان منابع: APKPure ← apkcombo ← Aptoide ← F-Droid…",
        parse_mode="Markdown",
    )
    chat_action(cid, "upload_document")

    data, fname, attempts = apk_download(app_id, info=info)
    if not data:
        # Plain text (no parse_mode): raw source-attempt strings contain
        # chars that break Markdown entity parsing → editMessageText 400 →
        # the status bubble spins forever (the "hang" report, 2026-09-13).
        tries = translate_apk_attempts(attempts)
        _status(
            cid,
            "❌ دانلود APK ناموفق بود.\n\n"
            f"نتایج منابع:\n{tries}\n\n"
            "دلایل احتمالی: اپ منطقه‌قفل است یا در میرورهای رایگان نیست.\n"
            f"دانلود دستی: https://apkpure.net/search?q={urllib.parse.quote(app_id)}",
            reply_markup=home_kb(), final=True)
        return

    size_mb = len(data) / 1024 / 1024
    log.info("APK: sending %s %.1fMB", fname, size_mb)
    note = (
        "\n\n⚠️ *نکته:* این فایل XAPK است — با اپ APKPure Installer نصب شود."
        if fname.endswith(".xapk")
        else "\n\n⚠️ *نکته:* گزینه «نصب از منابع ناشناس» را در تنظیمات اندروید فعال کنید."
    )
    if smart_send(cid, data, fname, caption=f"📱 {name} — {fname}"):
        _status(
            cid,
            f"✅ APK ارسال شد! ({size_mb:.1f}MB){note}",
            parse_mode="Markdown",
            reply_markup=home_kb(), final=True)
    else:
        _status(cid, "❌ ارسال ناموفق بود.", reply_markup=home_kb(), final=True)
    clear_state(cid)


def do_rss(cid: int, query: str):
    """Handle RSS input: URL → fetch feed; non-URL → search for feeds on site."""
    bump(cid, "searches")
    chat_action(cid)

    if query.startswith("http"):
        # Direct RSS URL or website URL → auto-discover
        if not any(x in query for x in ["rss", "feed", "atom", "xml"]):
            _status(cid, "🔍 در حال جستجوی RSS در سایت…")
            feeds = rss_search_feeds(query)
            if not feeds:
                # Try the URL itself as a feed
                feeds = [query]
            if len(feeds) > 1:
                rows = [
                    [
                        {
                            "text": f"📰 {f[:50]}",
                            "callback_data": f"rss_url_{store_url(f, chat_id=cid)}",
                        }
                    ]
                    for f in feeds[:5]
                ]
                rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
                send_message(
                    cid,
                    f"📰 {len(feeds)} فید یافت شد:",
                    reply_markup={"inline_keyboard": rows},
                )
                return
            query = feeds[0]

        # Fetch the feed
        _status(cid, f"⏳ در حال دریافت فید RSS…")
        items = rss_fetch(query, 15)
        if not items:
            _status(
                cid,
                "❌ فید یافت نشد یا معتبر نیست.\n"
                "آدرس فید RSS را مستقیم وارد کنید (مثال: https://example.com/rss)",
                reply_markup=home_kb(), final=True)
            return
        key = _rss_cache_key(cid, query, 0)
        cache_set_owned(key, items, _platform(), cid)
        store_url(query, chat_id=cid)
        set_state(cid, mode="rss", last_query=query, cache_key=key)
        _display_rss_list(cid, items, key)
    else:
        # Non-URL: treat as search query for news
        send_message(
            cid,
            "📰 آدرس فید RSS یا سایت خبری را وارد کنید:\n\n"
            "مثال‌ها:\n"
            "• `https://www.bbc.com/persian/index.xml`\n"
            "• `https://www.isna.ir` ← کشف خودکار\n"
            "• `https://feeds.bbci.co.uk/news/rss.xml`",
            parse_mode="Markdown",
            reply_markup=cancel_kb(),
        )


def _display_rss_list(cid: int, items: list, key: str, page: int = 0, send: bool = True):
    """Show a bounded RSS page; no more than seven rows are sent."""
    page_size = 5
    page = max(0, min(int(page), max(0, (len(items) - 1) // page_size)))
    start = page * page_size
    visible = items[start:start + page_size]
    rows = []
    for offset, item in enumerate(visible):
        title = str(item.get("title") or f"خبر {start + offset + 1}").replace("\n", " ")[:45]
        rows.append([{"text": f"📰 {title}", "callback_data": f"rss_item_{key}_{start + offset}"}])
    nav = []
    if page > 0:
        nav.append({"text": "◀️ قبلی", "callback_data": f"rss_page_{key}_{page - 1}"})
    if start + page_size < len(items):
        nav.append({"text": "بعدی ▶️", "callback_data": f"rss_page_{key}_{page + 1}"})
    if nav:
        rows.append(nav)
    rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
    keyboard = {"inline_keyboard": rows}
    if send:
        send_message(
            cid,
            f"📰 *صفحه {page + 1} از {max(1, (len(items) + page_size - 1) // page_size)} خبر:*\nروی هر خبر کلیک کنید:",
            parse_mode="Markdown", reply_markup=keyboard,
        )
    return keyboard


def _show_rss_item(cid: int, item: dict):
    """Display a single RSS item with full text and auto-send image."""
    title = item.get("title", "")
    link = item.get("link", "")
    summary = item.get("summary", "")
    published = item.get("published", "")
    img_url = item.get("img_url", "")

    lines = [f"📰 *{title}*" if title else ""]
    if published:
        lines.append(f"🕐 {published[:19]}")
    if summary:
        lines.append(f"\n{summary}")
    if link:
        lines.append(f"\n[🔗 متن کامل]({link})")

    # Auto-download and send image if available
    if img_url:
        try:
            img_bytes = download_bytes(img_url, MAX_IMAGE_SIZE)
            if img_bytes and len(img_bytes) > 500:
                send_photo(cid, img_bytes, caption=title[:80] if title else "")
        except Exception as e:
            log.warning("rss img: %s", e)

    text = "\n".join(l for l in lines if l)
    url_key = store_url(link, chat_id=cid) if link else ""
    kb = site_view_kb(url_key) if url_key else home_kb()
    send_message(
        cid, text or "_(خبر بدون متن)_", parse_mode="Markdown", reply_markup=kb
    )


def do_tg_read(cid: int, channel: str, use_mtproto: bool = False):
    """Read Telegram channel messages and show as clickable buttons."""
    bump(cid, "searches")
    channel = channel.strip().lstrip("@")
    _status(cid, f"⏳ در حال دریافت پیام‌های @{channel}…")
    chat_action(cid)

    if use_mtproto and TG_API_ID and TG_API_HASH:
        import asyncio

        try:
            loop = asyncio.new_event_loop()
            messages = loop.run_until_complete(tg_channel_read_mtproto(channel, 20))
            loop.close()
        except Exception as e:
            log.error("MTProto loop: %s", e)
            messages = []
        if not messages:
            _status(
                cid,
                "❌ MTProto ناموفق بود.\n"
                "مطمئن شوید TG_API_ID و TG_API_HASH تنظیم شده‌اند.",
                reply_markup=home_kb(), final=True)
            return
    else:
        messages = tg_channel_read_web(channel, 20)
        if not messages:
            _status(
                cid,
                "❌ پیامی یافت نشد.\n"
                "• کانال باید عمومی باشد\n"
                "• نام کانال را بررسی کنید (بدون @)",
                reply_markup=home_kb(), final=True)
            return

    key = make_cache_key("tg", channel, 0)
    cache_set(key, messages)
    set_state(cid, mode="tg_channel", last_query=channel, cache_key=key)

    title = f"✈️ @{channel} — آخرین {len(messages)} پیام"
    kb = social_results_kb(messages, key, "tg")
    send_message(cid, title, reply_markup=kb)


def do_tg_show_message(cid: int, msg_data: dict):
    """Display a Telegram message: text + auto-download images + video button."""
    text = msg_data.get("text", "")
    date = msg_data.get("date", "")
    url = msg_data.get("url", "")
    img_urls = msg_data.get("img_urls", [])
    has_video = msg_data.get("has_video", False)
    video_thumb = msg_data.get("video_thumb", "")

    # Send text
    lines = []
    if date:
        lines.append(f"🕐 {date}")
    if text:
        lines.append(f"\n{text}")
    if url:
        lines.append(f"\n[🔗 لینک پیام]({url})")
    if lines:
        send_message(cid, "\n".join(lines), parse_mode="Markdown")

    # Auto-download and send all images
    for img_url in img_urls[:4]:
        try:
            img_bytes = download_bytes(img_url, MAX_IMAGE_SIZE)
            if img_bytes and len(img_bytes) > 500:
                send_photo(cid, img_bytes, caption="��️")
                log.info("tg_show: sent image %dKB", len(img_bytes) // 1024)
        except Exception as e:
            log.warning("tg_show img: %s", e)

    # Video thumbnail + download button
    if has_video:
        if video_thumb:
            try:
                thumb = download_bytes(video_thumb, MAX_IMAGE_SIZE)
                if thumb:
                    send_photo(cid, thumb, caption="🎬 ویدیو")
            except Exception:
                pass
        kb = social_post_kb(url, "tg", True) if url else home_kb()
        send_message(cid, "📥 دانلود ویدیو؟", reply_markup=kb)
    elif not img_urls and not text:
        send_message(
            cid,
            "_(پیام بدون محتوای قابل نمایش)_",
            parse_mode="Markdown",
            reply_markup=home_kb(),
        )
    else:
        _status(cid, "✅", reply_markup=home_kb(), final=True)


def do_tg_dl_media(cid: int, msg_url: str):
    """Download media from a Telegram message URL."""
    bump(cid, "downloads")
    _status(cid, f"⏳ در حال دانلود رسانه از:\n{msg_url}")
    chat_action(cid, "upload_document")
    result = tg_download_media_web(msg_url)
    if not result:
        _status(
            cid,
            "❌ دانلود ناموفق بود — این پیام ممکن است رسانه نداشته باشد.",
            reply_markup=home_kb(), final=True)
        return
    data, fname = result
    smart_send(cid, data, fname, caption=f"✈️ {fname[:60]}")
    _status(cid, "✅ ارسال شد.", reply_markup=home_kb(), final=True)
    clear_state(cid)


def do_twitter_timeline(cid: int, username: str):
    """Fetch and display Twitter/X user timeline via Nitter."""
    bump(cid, "searches")
    username = username.strip().lstrip("@")
    _status(cid, f"⏳ در حال دریافت توییت‌های @{username}…")
    chat_action(cid)
    tweets = twitter_get_channel(username, 20)
    if not tweets:
        _status(
            cid,
            "❌ توییتی یافت نشد.\n"
            "• ممکن است Nitter instances در دسترس نباشند\n"
            "• نام کاربری را بررسی کنید",
            reply_markup=home_kb(), final=True)
        return
    key = make_cache_key("tw", username, 0)
    cache_set(key, tweets)
    set_state(cid, mode="twitter", last_query=username, cache_key=key)
    kb = social_results_kb(tweets, key, "tw")
    send_message(cid, f"🐦 @{username} — {len(tweets)} توییت اخیر:", reply_markup=kb)


def do_twitter_show(cid: int, tweet: dict):
    """Display tweet, auto-download photos, show video download button."""
    text = tweet.get("text", "")
    date = tweet.get("date", "")
    url = tweet.get("url", "")
    has_vid = tweet.get("has_video", False)
    img_urls = tweet.get("img_urls", [])

    lines = []
    if date:
        lines.append(f"🕐 {date}")
    if text:
        lines.append(f"\n{text}")
    if url:
        lines.append(f"\n[🔗 لینک توییت]({url})")

    send_message(cid, "\n".join(lines) or "_(توییت بدون متن)_", parse_mode="Markdown")

    # Auto-download and send ALL images in the tweet
    for img_url in img_urls[:10]:
        try:
            img_bytes = download_bytes(img_url, MAX_IMAGE_SIZE)
            if img_bytes and len(img_bytes) > 500:
                send_photo(cid, img_bytes, caption="")
        except Exception as e:
            log.warning("twitter img dl: %s", e)

    # Store tweet text as caption for media download
    if text:
        set_state(cid, last_caption=text[:1024])

    # Show video download button if has video
    kb = social_post_kb(url, "tw", has_vid) if url else home_kb()
    if url:
        tw_kb = {"inline_keyboard": [[{"text": "🧵 خواندن رشته", "callback_data": f"tw_unroll_{url[:50]}"}]]}
        tw_kb["inline_keyboard"] += kb["inline_keyboard"]
        kb = tw_kb
    if has_vid or not img_urls:
        send_message(cid, "📥 دانلود ویدیو؟", reply_markup=kb)
    else:
        _status(cid, "✅", reply_markup=home_kb(), final=True)


def twitter_thread_fetch(tweet_url: str, max_posts: int = 60) -> Optional[list[dict]]:
    """Fetch a Twitter/X self-thread as an ordered list of posts.

    Primary: FxTwitter /2/thread/{id} — one call returns the unrolled thread
    (works from any member tweet, self-replies only, public accounts).
    Fallback: backward walk of replying_to_status via /i/status/.
    Returns [{id, author, text, created_at, likes, retweets, views, url}] or None.
    """
    tweet_url = tweet_url.replace("x.com/", "twitter.com/").split("?")[0]
    m = re.search(r"twitter\.com/([^/]+)/status/(\d+)", tweet_url)
    if not m:
        return None
    username, tweet_id = m.group(1), m.group(2)
    headers = {"User-Agent": UA_DESK}
    log.info("twitter_thread_fetch: %s/%s", username, tweet_id)

    posts: list[dict] = []
    try:
        r = WEB.get(
            f"https://api.fxtwitter.com/2/thread/{tweet_id}",
            headers=headers, timeout=20,
        )
        if r.status_code == 200:
            thread = r.json().get("thread") or []
            for tw in thread:
                posts.append({
                    "id": tw.get("id") or "",
                    "author": (tw.get("author") or {}).get("screen_name") or username,
                    "text": (tw.get("text") or tw.get("raw_text") or "").strip(),
                    "created_at": tw.get("created_at") or "",
                    "likes": tw.get("likes") or 0,
                    "retweets": tw.get("retweets") or 0,
                    "views": tw.get("views") or 0,
                    "url": tw.get("url") or f"https://x.com/{username}/status/{tw.get('id', '')}",
                })
        elif r.status_code == 404:
            log.warning("twitter_thread_fetch: fxtwitter 404 for %s", tweet_id)
    except Exception as e:
        log.warning("twitter_thread_fetch fxtwitter: %s", e)

    if not posts:
        # Backward walk (child → parent) via /i/status/
        chain: list[dict] = []
        cur = tweet_id
        for _ in range(max_posts):
            try:
                rr = WEB.get(
                    f"https://api.fxtwitter.com/i/status/{cur}",
                    headers=headers, timeout=15,
                )
            except Exception as e:
                log.warning("twitter_thread_fetch walk: %s", e)
                break
            if rr.status_code != 200:
                break
            tw = rr.json().get("tweet") or {}
            chain.append({
                "id": tw.get("id") or cur,
                "author": (tw.get("author") or {}).get("screen_name") or username,
                "text": (tw.get("text") or tw.get("raw_text") or "").strip(),
                "created_at": tw.get("created_at") or "",
                "likes": tw.get("likes") or 0,
                "retweets": tw.get("retweets") or 0,
                "views": tw.get("views") or 0,
                "url": tw.get("url") or f"https://x.com/{username}/status/{cur}",
            })
            nxt = tw.get("replying_to_status")
            if not nxt:
                break
            cur = str(nxt)
            time.sleep(1.5)  # be polite to fxtwitter
        posts = list(reversed(chain))

    if not posts:
        return None
    return posts[:max_posts]


def twitter_thread_render(posts: list[dict]) -> str:
    """Render a thread as one continuous numbered text (twitter-thread.com style)."""
    author = posts[0].get("author") or "unknown"
    total = len(posts)
    views = sum(p.get("views") or 0 for p in posts)
    parts = [
        f"🧵 رشته‌توییت @{author} — {total} پست"
        + (f" · {views:,} بازدید" if views else "")
    ]
    for i, post in enumerate(posts, 1):
        num = f"{i}/{total}"
        body = (post.get("text") or "").strip()
        stats = []
        if post.get("likes"):
            stats.append(f"❤️ {post['likes']:,}")
        if post.get("retweets"):
            stats.append(f"🔁 {post['retweets']:,}")
        stat_line = ("\n" + " · ".join(stats)) if stats else ""
        parts.append(f"{num} — {body}{stat_line}")
    return "\n\n".join(parts)


def do_twitter_thread(cid: int, url: str):
    """دانلود ترید توییتر به صورت یک متن پیوسته."""
    bump(cid, "searches")
    _status(cid, "⏳ در حال دریافت رشته‌توییت…")
    chat_action(cid)
    posts = twitter_thread_fetch(url)
    if not posts:
        _status(
            cid,
            "❌ رشته‌توییت دریافت نشد.\n"
            "• توییت باید عمومی باشد — حساب خصوصی پشتیبانی نمی‌شود\n"
            "• توییت‌های قدیمی‌تر از چند ماه از سمت ایکس در دسترس نیستند\n"
            "• لینک هر پست از رشته (اول، وسط یا آخر) کافی است",
            reply_markup=home_kb(), final=True)
        return
    set_state(cid, mode="twitter", last_query=url)  # keep cancel flow consistent
    author = posts[0].get("author") or "unknown"
    rendered = twitter_thread_render(posts)
    # Chunk at 4096 chars, break on paragraph boundary where possible
    chunks = []
    while rendered:
        if len(rendered) <= 4096:
            chunks.append(rendered)
            break
        cut = rendered.rfind("\n\n", 1000, 4090)
        if cut < 0:
            cut = rendered.rfind("\n", 1000, 4090)
        if cut < 0:
            cut = 4000
        chunks.append(rendered[:cut])
        rendered = rendered[cut:].lstrip("\n")
    for i, chunk in enumerate(chunks):
        final = (i == len(chunks) - 1)
        send_message(
            cid, chunk,
            reply_markup=home_kb() if final else None,
        )
    log.info("do_twitter_thread: %s — %d posts in %d messages", author, len(posts), len(chunks))


def _fetch_tweet_data(tweet_url: str) -> dict:
    """
    Fetch full tweet metadata via vxtwitter/fxtwitter.
    Returns dict with keys: text, author_name, author_handle, avatar_url,
    likes, retweets, replies, views, created_at, media_urls, is_verified
    """
    tweet_url = tweet_url.replace("x.com/", "twitter.com/")
    m = re.search(r"twitter\.com/([^/]+)/status/(\d+)", tweet_url)
    if not m:
        return {}
    username, tweet_id = m.group(1), m.group(2)
    for api_base in ["https://api.vxtwitter.com", "https://api.fxtwitter.com"]:
        try:
            r = WEB.get(
                f"{api_base}/{username}/status/{tweet_id}",
                headers={"User-Agent": UA_DESK},
                timeout=12,
            )
            if r.status_code != 200:
                continue
            jdata = r.json()
            tw = jdata.get("tweet") or jdata
            result = {
                "text": (
                    tw.get("text") or tw.get("full_text") or tw.get("tweetText") or ""
                ).strip(),
                "author_name": tw.get("user_name") or tw.get("author") or username,
                "author_handle": tw.get("user_screen_name")
                or tw.get("authorHandle")
                or username,
                "avatar_url": tw.get("user_profile_image_url")
                or tw.get("authorAvatar")
                or "",
                "likes": tw.get("likes") or tw.get("favorite_count") or 0,
                "retweets": tw.get("retweets") or tw.get("retweet_count") or 0,
                "replies": tw.get("replies") or tw.get("reply_count") or 0,
                "views": tw.get("views") or tw.get("view_count") or 0,
                "created_at": tw.get("date") or tw.get("created_at") or "",
                "is_verified": tw.get("is_verified")
                or tw.get("user_verified")
                or False,
            }
            if result["text"]:
                log.info(
                    "_fetch_tweet_data: OK from %s (%d chars)",
                    api_base,
                    len(result["text"]),
                )
                return result
        except Exception as e:
            log.debug("_fetch_tweet_data %s: %s", api_base, e)
    return {}


def _render_tweet_card(tw: dict, tweet_url: str) -> Optional[bytes]:
    """
    Render a tweet card as PNG: Vazirmatn font, RTL support, media embed, X dark theme.
    """
    try:
        import textwrap as _tw
        from io import BytesIO as _BIO

        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        log.warning("_render_tweet_card: Pillow not installed")
        return None
    try:
        # Download / cache Vazirmatn font
        font_dir = Path(tempfile.gettempdir()) / "bot_fonts"
        font_dir.mkdir(exist_ok=True)
        font_reg = font_dir / "Vazirmatn-Regular.ttf"
        font_bold = font_dir / "Vazirmatn-Bold.ttf"
        VAZIR_URLS = {
            font_reg: "https://github.com/rastikerdar/vazirmatn/raw/master/fonts/ttf/Vazirmatn-Regular.ttf",
            font_bold: "https://github.com/rastikerdar/vazirmatn/raw/master/fonts/ttf/Vazirmatn-Bold.ttf",
        }
        for fpath, furl in VAZIR_URLS.items():
            if not fpath.exists() or fpath.stat().st_size < 10000:
                try:
                    fpath.write_bytes(WEB.get(furl, timeout=15).content)
                    log.info("tweet card: downloaded %s", fpath.name)
                except Exception as fe:
                    log.warning("font dl: %s", fe)

        def _fnt(size, bold=False):
            p = font_bold if bold else font_reg
            if p.exists():
                try:
                    return ImageFont.truetype(str(p), size)
                except Exception:
                    pass
            for fb in (
                ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
                if bold
                else ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
            ):
                if Path(fb).exists():
                    return ImageFont.truetype(fb, size)
            return ImageFont.load_default()

        W, PAD = 720, 28
        INNER = W - PAD * 2
        CARD = (21, 32, 43)
        TEXT = (231, 233, 234)
        MUTED = (113, 118, 123)
        BLUE = (29, 155, 240)
        LIKE_C = (249, 24, 128)
        BORDER = (47, 51, 54)

        fn_name = _fnt(17, bold=True)
        fn_handle = _fnt(15)
        fn_body = _fnt(17)
        fn_small = _fnt(13)
        fn_stats = _fnt(15)

        def _bb(text, font):
            d = ImageDraw.Draw(Image.new("RGB", (1, 1)))
            b = d.textbbox((0, 0), text, font=font)
            return b[2] - b[0], b[3] - b[1]

        def _is_rtl(text):
            return any(
                "\u0600" <= c <= "\u06ff"
                or "\ufb50" <= c <= "\ufdff"
                or "\ufe70" <= c <= "\ufeff"
                for c in text
            )

        def _draw_line(draw, y, text, font, fill):
            if _is_rtl(text):
                tw2, _ = _bb(text, font)
                draw.text((PAD + INNER - tw2, y), text, font=font, fill=fill)
            else:
                draw.text((PAD, y), text, font=font, fill=fill)

        def _wrap(text, font):
            lines = []
            cw, _ = _bb("م", fn_body)
            cpl = max(20, int(INNER / max(cw, 1)))
            for para in text.split("\n"):
                if not para.strip():
                    lines.append("")
                    continue
                lines.extend(_tw.wrap(para, width=cpl) or [""])
            return lines

        # Avatar
        av_size, avatar = 48, None
        if tw.get("avatar_url"):
            try:
                av_data = WEB.get(tw["avatar_url"], timeout=8).content
                av = (
                    Image.open(_BIO(av_data)).convert("RGBA").resize((av_size, av_size))
                )
                mask = Image.new("L", (av_size, av_size), 0)
                ImageDraw.Draw(mask).ellipse([(0, 0), (av_size, av_size)], fill=255)
                base = Image.new("RGBA", (av_size, av_size), CARD + (255,))
                base.paste(av, mask=mask)
                avatar = base
            except Exception:
                pass

        # Embedded media thumbnail
        media_img = None
        try:
            tw_clean = tweet_url.replace("x.com/", "twitter.com/")
            mx = re.search(r"twitter\.com/([^/]+)/status/(\d+)", tw_clean)
            if mx:
                u2, tid = mx.group(1), mx.group(2)
                for api_b in ["https://api.vxtwitter.com", "https://api.fxtwitter.com"]:
                    try:
                        rj = WEB.get(
                            f"{api_b}/{u2}/status/{tid}",
                            headers={"User-Agent": UA_DESK},
                            timeout=10,
                        ).json()
                        tw_obj = rj.get("tweet") or rj
                        ml = (
                            tw_obj.get("media", {}).get("all", [])
                            or tw_obj.get("media_extended", [])
                            or []
                        )
                        for med in ml:
                            thumb = med.get("thumbnail_url") or med.get(
                                "media_url_https", ""
                            )
                            if thumb and "pbs.twimg" in thumb:
                                mi_data = WEB.get(thumb + ":large", timeout=10).content
                                mi = Image.open(_BIO(mi_data)).convert("RGB")
                                ratio = INNER / mi.width
                                mi = mi.resize(
                                    (INNER, int(mi.height * ratio)), Image.LANCZOS
                                )
                                media_img = mi
                                break
                        if media_img:
                            break
                    except Exception:
                        continue
        except Exception:
            pass

        # Body lines
        body = tw.get("text", "")
        blines = _wrap(body, fn_body)
        lh = _bb("Agم", fn_body)[1] + 5
        body_h = len(blines) * lh

        # Date
        date_str = tw.get("created_at", "")
        if date_str:
            try:
                from datetime import datetime as _dt

                for fmt in (
                    "%Y-%m-%dT%H:%M:%S.%fZ",
                    "%a %b %d %H:%M:%S +0000 %Y",
                    "%Y-%m-%dT%H:%M:%SZ",
                    "%Y-%m-%dT%H:%M:%S",
                ):
                    try:
                        date_str = _dt.strptime(date_str, fmt).strftime(
                            "%-I:%M %p · %b %-d, %Y"
                        )
                        break
                    except ValueError:
                        continue
            except Exception:
                pass

        # Height
        MH = (media_img.height + PAD) if media_img else 0
        H = (
            PAD
            + av_size
            + PAD
            + body_h
            + PAD
            + MH
            + 22
            + PAD // 2
            + 1
            + PAD // 2
            + 28
            + PAD
        )

        img = Image.new("RGB", (W, H), CARD)
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle(
            [(1, 1), (W - 2, H - 2)], radius=12, outline=BORDER, width=1
        )

        # Avatar row
        if avatar:
            img.paste(avatar, (PAD, PAD), avatar)
        else:
            draw.ellipse([(PAD, PAD), (PAD + av_size, PAD + av_size)], fill=BLUE)

        nx = PAD + av_size + 12
        draw.text(
            (nx, PAD + 2), tw.get("author_name", "")[:30], font=fn_name, fill=TEXT
        )
        handle_str = "@" + tw.get("author_handle", "")
        draw.text((nx, PAD + 24), handle_str, font=fn_handle, fill=MUTED)
        xw, _ = _bb("𝕏", fn_name)
        draw.text((W - PAD - xw, PAD + 4), "𝕏", font=fn_name, fill=MUTED)

        # Body
        ty = PAD + av_size + PAD
        for line in blines:
            _draw_line(draw, ty, line, fn_body, TEXT)
            ty += lh

        # Media
        if media_img:
            ty += PAD // 2
            rmask = Image.new("L", media_img.size, 0)
            ImageDraw.Draw(rmask).rounded_rectangle(
                [(0, 0), (media_img.width - 1, media_img.height - 1)],
                radius=10,
                fill=255,
            )
            img.paste(media_img, (PAD, ty), rmask)
            ty += media_img.height + PAD // 2

        # Date
        ty += PAD // 4
        draw.text((PAD, ty), date_str, font=fn_small, fill=MUTED)
        ty += 22 + PAD // 2

        # Divider
        draw.line([(PAD, ty), (W - PAD, ty)], fill=BORDER, width=1)
        ty += PAD // 2 + 2

        # Stats — draw custom icons using Pillow shapes (no emoji font needed)
        def _fmt(n):
            try:
                n = int(n or 0)
                if n >= 1_000_000:
                    return f"{n / 1_000_000:.1f}M"
                if n >= 1_000:
                    return f"{n / 1_000:.1f}K"
                return str(n)
            except:
                return "0"

        def _draw_icon(ix, iy, kind, color):
            """Draw a small recognisable icon at (ix, iy) using Pillow primitives."""
            S = 10  # icon bounding box
            if kind == "bubble":
                # Speech bubble: circle + small triangle at bottom-left
                draw.ellipse([(ix, iy), (ix + S, iy + S)], outline=color, width=1)
                draw.polygon(
                    [(ix + 1, iy + S - 2), (ix + 4, iy + S + 3), (ix + 5, iy + S - 2)],
                    fill=color,
                )
            elif kind == "repost":
                # Two curved arrows: simplified as two small L-shapes
                draw.line(
                    [(ix + 2, iy + S - 1), (ix + 2, iy + 2), (ix + S - 2, iy + 2)],
                    fill=color,
                    width=1,
                )
                draw.line(
                    [(ix + S - 3, iy), (ix + S - 1, iy + 2), (ix + S - 3, iy + 4)],
                    fill=color,
                    width=1,
                )
                draw.line(
                    [
                        (ix + S - 2, iy + 1),
                        (ix + S - 2, iy + S - 2),
                        (ix + 2, iy + S - 2),
                    ],
                    fill=color,
                    width=1,
                )
                draw.line(
                    [(ix + 3, iy + S - 1), (ix + 1, iy + S - 3), (ix + 3, iy + S - 4)],
                    fill=color,
                    width=1,
                )
            elif kind == "heart":
                # Heart: two overlapping circles + downward triangle
                h = S // 2
                draw.ellipse([(ix, iy), (ix + h, iy + h)], fill=color)
                draw.ellipse([(ix + h - 1, iy), (ix + S, iy + h)], fill=color)
                draw.polygon(
                    [(ix, iy + h - 2), (ix + S // 2, iy + S + 1), (ix + S, iy + h - 2)],
                    fill=color,
                )
            elif kind == "eye":
                # Eye: almond shape + pupil
                draw.ellipse(
                    [(ix, iy + 2), (ix + S, iy + S - 2)], outline=color, width=1
                )
                draw.ellipse(
                    [(ix + S // 2 - 2, iy + 3), (ix + S // 2 + 2, iy + S - 3)],
                    fill=color,
                )

        stats = [
            ("bubble", tw.get("replies", 0), MUTED),
            ("repost", tw.get("retweets", 0), MUTED),
            ("heart", tw.get("likes", 0), LIKE_C),
            ("eye", tw.get("views", 0), MUTED),
        ]
        stat_labels = ["Reply", "Repost", "Like", "View"]
        sx, col_w = PAD, INNER // 4
        for (kind, val, color), label in zip(stats, stat_labels):
            _draw_icon(sx, ty + 2, kind, color)
            draw.text((sx + 14, ty), _fmt(val), font=fn_stats, fill=color)
            draw.text(
                (sx + 14, ty + fn_stats.size + 1), label, font=fn_small, fill=MUTED
            )
            sx += col_w

        # Badge showing 1/N when tweet has multiple media
        media_count = len(tw.get("_media_count_hint", []))
        if media_count > 1:
            badge = f"1/{media_count}"
            bw, bh = _bb(badge, fn_small)
            bx, by = W - PAD - bw - 10, PAD + 6
            draw.rounded_rectangle(
                [(bx - 4, by - 3), (bx + bw + 4, by + bh + 3)], radius=6, fill=(0, 0, 0)
            )
            draw.text((bx, by), badge, font=fn_small, fill=TEXT)

        buf = _BIO()
        img.save(buf, format="PNG", optimize=True)
        card_bytes = buf.getvalue()
        log.info("_render_tweet_card: %dx%d  %.1fKB", W, H, len(card_bytes) / 1024)
        return card_bytes

    except Exception as e:
        log.error("_render_tweet_card: %s", e, exc_info=True)
        return None


@_heavy_download
def do_twitter_dl(cid: int, url: str, _tw_data_override: dict = None):
    """Download ALL media from a tweet, send tweet card + media group + caption."""
    bump(cid, "downloads")
    _status(cid, "⏳ در حال دانلود از توییت…")
    chat_action(cid, "upload_video")

    tw_data = _tw_data_override or _fetch_tweet_data(url)
    caption = get_state(cid).get("last_caption", "") or tw_data.get("text", "")
    original_text = tw_data.get("_original_text", "")

    # Download all media first so we know the count for the card
    results = twitter_download_media(url)

    # Pass media count hint to card renderer
    if tw_data and results:
        tw_data["_media_count_hint"] = list(range(len(results)))

    card_bytes = _render_tweet_card(tw_data, url) if tw_data else None
    if card_bytes:
        send_photo(cid, card_bytes, caption="")

    if not results:
        if not card_bytes:
            _status(
                cid,
                "❌ رسانه‌ای از این توییت دریافت نشد.\n"
                "• توییت ممکن است رسانه نداشته باشد یا حذف شده باشد\n"
                "• حساب ممکن است خصوصی باشد\n"
                "• برای متن کامل توییت، دکمهٔ «🧵 خواندن رشته» را امتحان کنید",
                reply_markup=home_kb(), final=True)
        else:
            if caption:
                if original_text:
                    send_message(
                        cid, f"🌐 *ترجمه:*\n{caption[:4096]}", parse_mode="Markdown"
                    )
                    send_message(
                        cid,
                        f"📝 *متن اصلی:*\n{original_text[:2000]}",
                        parse_mode="Markdown",
                    )
                else:
                    send_message(cid, caption[:4096])
            _status(cid, "✅ توییت ارسال شد.", reply_markup=home_kb(), final=True)
        clear_state(cid)
        return

    BALE_CAPTION_LIMIT = 1024

    if len(results) == 1:
        data, fname = results[0]
        inline_cap = (
            caption[:BALE_CAPTION_LIMIT] if len(caption) <= BALE_CAPTION_LIMIT else ""
        )
        smart_send(cid, data, fname, caption=inline_cap)
    else:
        group_items = []
        for data, fname in results[:10]:
            is_vid = fname.lower().endswith((".mp4", ".mov", ".webm"))
            group_items.append(
                {"data": data, "fname": fname, "is_video": is_vid, "caption": ""}
            )
        send_media_group(cid, group_items)

    if original_text:
        send_message(cid, f"🌐 *ترجمه:*\n{caption[:4096]}", parse_mode="Markdown")
        send_message(
            cid, f"📝 *متن اصلی:*\n{original_text[:2000]}", parse_mode="Markdown"
        )
    elif caption and (len(results) > 1 or len(caption) > BALE_CAPTION_LIMIT):
        send_message(cid, caption[:4096])

    _status(cid, "✅ ارسال شد.", reply_markup=home_kb(), final=True)
    clear_state(cid)


def instagram_story_download(username: str) -> list[dict]:
    """Download a public Instagram profile's active stories (no login).

    Anonymous Instagram API endpoints are 429-walled for datacenter IPs, so
    this uses the storiesig.info viewer through headless Chromium: submit the
    username, harvest media proxy URLs from the rendered page, fetch them via
    the browser context (the proxy validates session cookies).
    Returns list of {data, fname, is_video}.
    """
    log.info("instagram_story_download: @%s", username)
    from playwright.sync_api import sync_playwright

    username = username.strip().lstrip("@").split("/")[0]
    items: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        try:
            ctx = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = ctx.new_page()
            page.goto("https://storiesig.info/", timeout=45000,
                      wait_until="domcontentloaded")
            page.wait_for_timeout(4000)
            inp = page.query_selector(
                "input[type='text'], input[name='q'], input[placeholder]"
            )
            if not inp:
                log.warning("instagram_story_download: no input on storiesig")
                return []
            inp.fill(username)
            page.keyboard.press("Enter")
            page.wait_for_timeout(12000)
            html_text = page.content()
            urls = [
                u.replace("&amp;", "&")
                for u in re.findall(
                    r"https://media\.storiesig\.info/get\?[^\"']+", html_text
                )
            ]
            # Dedupe by the proxied target (uri param) — the same media can
            # appear with different proxy signatures; drop avatar-size noise.
            seen: set[str] = set()
            media_urls: list[str] = []
            for u in urls:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
                target = (qs.get("uri") or [u])[0]
                if target in seen:
                    continue
                seen.add(target)
                media_urls.append(u)
            log.info("instagram_story_download: %d media urls", len(media_urls))
            for u in media_urls:
                try:
                    r = page.request.get(u, timeout=45000, fail_on_status_code=False)
                    if r.status != 200:
                        continue
                    body = r.body()
                    if body[:2] == b"\xff\xd8" or body[:4] in (b"\x89PNG", b"RIFF"):
                        if len(body) < 20000:  # avatar/thumb noise
                            continue
                        items.append({
                            "data": body,
                            "fname": f"{username}_story_{len(items) + 1}.jpg",
                            "is_video": False,
                        })
                    elif body[4:8] in (b"ftyp", b"moov"):
                        if len(body) < 50000:
                            continue
                        items.append({
                            "data": body,
                            "fname": f"{username}_story_{len(items) + 1}.mp4",
                            "is_video": True,
                        })
                except Exception as e:
                    log.warning("instagram_story_download item: %s", e)
                if len(items) >= 15:
                    break
        finally:
            browser.close()
    log.info("instagram_story_download: %d items", len(items))
    return items


def do_instagram_story(cid: int, username: str):
    """دانلود استوری‌های عمومی اینستاگرام."""
    bump(cid, "downloads")
    username = username.strip().lstrip("@").split("/")[0]
    if not username:
        _status(cid, "❌ نام کاربری نامعتبر است.", reply_markup=home_kb(), final=True)
        return
    _status(cid, f"⏳ در حال دریافت استوری‌های @{username}… (حدود ۳۰ ثانیه)")
    chat_action(cid)
    items = instagram_story_download(username)
    if not items:
        _status(
            cid,
            f"❌ استوری‌ای برای @{username} یافت نشد.\n"
            "• اکانت باید عمومی باشد\n"
            "• استوری فعال (۲۴ ساعت اخیر) باید موجود باشد",
            reply_markup=home_kb(), final=True,
        )
        return
    ok = 0
    if len(items) > 1:
        group = [
            {"data": it["data"], "fname": it["fname"],
             "is_video": it["is_video"], "caption": f"📸 استوری @{username}" if i == 0 else ""}
            for i, it in enumerate(items[:10])
        ]
        if send_media_group(cid, group):
            ok += len(group)
        for it in items[10:]:
            if smart_send(cid, it["data"], it["fname"], caption="",
                          media_type="video" if it["is_video"] else "photo"):
                ok += 1
    else:
        it = items[0]
        if smart_send(cid, it["data"], it["fname"],
                      caption=f"📸 استوری @{username}",
                      media_type="video" if it["is_video"] else "photo"):
            ok += 1
    _status(cid, f"✅ {ok} استوری ارسال شد.", reply_markup=home_kb(), final=True)
    clear_state(cid)


def do_instagram_profile(cid: int, username: str):
    """Fetch and display Instagram profile posts."""
    bump(cid, "searches")
    username = username.strip().lstrip("@")
    _status(cid, f"⏳ در حال دریافت پست‌های @{username}…")
    chat_action(cid)
    posts = instagram_get_profile(username)
    if not posts:
        _status(
            cid,
            "❌ پستی یافت نشد.\n"
            "• پروفایل باید عمومی باشد\n"
            "• برای پروفایل‌های خصوصی INSTAGRAM_USER/PASS تنظیم کنید",
            reply_markup=home_kb(), final=True)
        return
    key = make_cache_key("ig", username, 0)
    cache_set(key, posts)
    set_state(cid, mode="instagram", last_query=username, cache_key=key)
    kb = social_results_kb(posts, key, "ig")
    send_message(cid, f"📸 @{username} — {len(posts)} پست اخیر:", reply_markup=kb)


def _fetch_instagram_caption(url: str) -> str:
    """Fetch Instagram post caption via oEmbed API (no login needed for public posts)."""
    try:
        m = re.search(r"instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]+)", url)
        if not m:
            return ""
        shortcode = m.group(1)
        r = WEB.get(
            f"https://www.instagram.com/api/v1/oembed/?url=https://www.instagram.com/p/{shortcode}/",
            headers={"User-Agent": UA_MOB, "Accept": "application/json"},
            timeout=10,
        )
        if r.status_code == 200:
            j = r.json()
            title = j.get("title", "").strip()
            author = j.get("author_name", "").strip()
            if title:
                cap = title
                if author:
                    cap += f"\n\n— @{author}"
                log.info("_fetch_instagram_caption: %d chars via oEmbed", len(cap))
                return cap  # full caption — caller truncates as needed
    except Exception as e:
        log.debug("_fetch_instagram_caption oEmbed: %s", e)

    try:
        r = WEB.get(
            f"https://graph.instagram.com/oembed?url={url}&omitscript=true",
            headers={"User-Agent": UA_MOB},
            timeout=10,
        )
        if r.status_code == 200:
            j = r.json()
            title = j.get("title", "").strip()
            if title:
                return title
    except Exception as e:
        log.debug("_fetch_instagram_caption graph: %s", e)

    return ""


@_heavy_download
def do_instagram_dl(cid: int, url: str):
    """دانلود تمام رسانه‌های یک پست اینستاگرام (تکی / کاروسل / ریل) با کپشن کامل."""
    bump(cid, "downloads")
    _status(cid, "⏳ در حال دانلود از اینستاگرام…")
    chat_action(cid, "upload_video")

    items = instagram_download_all(url)
    items = [it for it in items if it.get("data") and len(it["data"]) > 5000]

    if not items:
        _status(
            cid,
            "❌ دانلود از اینستاگرام ناموفق بود.\n"
            "• مطمئن شوید پست عمومی است (حساب خصوصی پشتیبانی نمی‌شود)\n"
            "• پست‌های محدود سنی فقط با حساب تأییدشده قابل دریافت‌اند\n"
            "• چند دقیقه بعد دوباره تلاش کنید — گاهی اینستاگرام موقتاً محدود می‌کند\n"
            "• می‌توانید لینک پست دیگر را امتحان کنید",
            reply_markup=home_kb(), final=True)
        return

    # Caption priority: instaloader → oEmbed → state
    caption = ""
    for it in items:
        if it.get("caption"):
            caption = it["caption"]
            break
    if not caption:
        caption = get_state(cid).get("last_caption", "") or ""
    if not caption:
        caption = _fetch_instagram_caption(url)

    total = len(items)
    log.info("do_instagram_dl: sending %d items, caption_len=%d", total, len(caption))

    BALE_CAPTION_LIMIT = 1024

    all_ok = True
    if total == 1:
        # Single item — send with caption inline if it fits
        item = items[0]
        mtype = "video" if item.get("is_video") else "photo"
        inline_cap = (
            caption[:BALE_CAPTION_LIMIT] if len(caption) <= BALE_CAPTION_LIMIT else ""
        )
        all_ok = smart_send(
            cid, item["data"], item["fname"], caption=inline_cap, media_type=mtype
        )
        if caption and not inline_cap:
            all_ok = bool(send_message(cid, caption[:4096]).get("ok")) and all_ok
    else:
        # Multiple items — send as media group (album)
        # Caption on first item only, but only if short enough; otherwise separate msg
        group_items = []
        for i, item in enumerate(items[:10]):
            inline_cap = (
                caption[:BALE_CAPTION_LIMIT]
                if i == 0 and len(caption) <= BALE_CAPTION_LIMIT
                else ""
            )
            group_items.append(
                {
                    "data": item["data"],
                    "fname": item["fname"],
                    "is_video": item.get("is_video", False),
                    "caption": inline_cap,
                }
            )
        all_ok = send_media_group(cid, group_items)

        # If more than 10 items, send the rest individually
        for item in items[10:]:
            mtype = "video" if item.get("is_video") else "photo"
            ok = smart_send(cid, item["data"], item["fname"], caption="", media_type=mtype)
            all_ok = bool(ok) and all_ok

        # Send long caption as separate message
        if caption and len(caption) > BALE_CAPTION_LIMIT:
            all_ok = bool(send_message(cid, caption[:4096]).get("ok")) and all_ok

    _status(cid, f"✅ {total} مورد ارسال شد." if all_ok else "⚠️ ارسال کامل نشد. بعضی رسانه‌ها ارسال نشده‌اند.", reply_markup=home_kb(), final=True)
    if all_ok:
        clear_state(cid)


def do_tiktok_user(cid: int, username: str):
    """Fetch TikTok user video list."""
    bump(cid, "searches")
    username = username.strip().lstrip("@")
    _status(cid, f"⏳ در حال دریافت ویدیوهای @{username}…")
    chat_action(cid)
    videos = tiktok_user_videos(username, 10)
    if not videos:
        _status(
            cid,
            "❌ ویدیویی یافت نشد.\n"
            "• نام کاربری را بررسی کنید\n"
            "• پروفایل باید عمومی باشد",
            reply_markup=home_kb(), final=True)
        return
    key = make_cache_key("tt", username, 0)
    cache_set(key, videos)
    set_state(cid, mode="tiktok", last_query=username, cache_key=key)
    kb = social_results_kb(videos, key, "tt")
    send_message(cid, f"🎵 @{username} — {len(videos)} ویدیو اخیر:", reply_markup=kb)


def _fetch_tiktok_caption(url: str, tikwm_data=None) -> str:
    """Reuse this operation's title, otherwise fetch via yt-dlp."""
    if tikwm_data and tikwm_data.get("title"):
        return str(tikwm_data["title"]).strip()[:2000]
    try:
        r = subprocess.run(
            [
                YTDLP_BIN,
                "--no-warnings",
                "--no-check-certificate",
                "--dump-json",
                "--no-playlist",
                url,
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if r.returncode == 0:
            import json as _j

            info = _j.loads(r.stdout)
            desc = info.get("description") or info.get("title") or ""
            return desc.strip()[:2000]
    except Exception as e:
        log.debug("_fetch_tiktok_caption: %s", e)
    return ""


def _tiktok_music_dl(url: str) -> Optional[tuple[bytes, str]]:
    """Fetch a TikTok post's sound (music) via the tikwm API.

    Works for videos and photo posts alike — returns (mp3/m4a bytes, fname).
    """
    log.info("_tiktok_music_dl: %s", url)
    try:
        r = WEB.post(
            "https://www.tikwm.com/api/",
            data={"url": url, "hd": "1"},
            headers={"User-Agent": UA_DESK},
            timeout=30,
        )
        j = r.json()
        d = j.get("data", {}) if j.get("code") == 0 else {}
        music_url = d.get("music") or (d.get("music_info", {}) or {}).get("play", "")
        if not music_url:
            log.warning("_tiktok_music_dl: no music in tikwm response")
            return None
        title = (d.get("music_info", {}) or {}).get("title") or d.get("title") or "tiktok_sound"
        title = re.sub(r"[^\w\s-]", "", title)[:48].strip() or "tiktok_sound"
        mr = WEB.get(
            music_url,
            headers={"User-Agent": UA_DESK, "Referer": "https://www.tikwm.com/"},
            timeout=60,
        )
        if mr.status_code == 200 and len(mr.content) > 5000:
            ext = ".mp4" if mr.content[4:8] in (b"ftyp", b"moov") else ".mp3"
            log.info("_tiktok_music_dl OK: %.0fKB", len(mr.content) / 1024)
            return mr.content, f"{title}{ext}"
        log.warning("_tiktok_music_dl: music fetch HTTP %s", mr.status_code)
    except Exception as e:
        log.warning("_tiktok_music_dl: %s", e)
    return None


def _photo_slideshow_video(image: bytes, audio: bytes, out_name: str) -> Optional[bytes]:
    """Build a vertical slideshow MP4 (static image + music) via ffmpeg."""
    import shutil as _sh

    ffmpeg = _sh.which("ffmpeg") or "/usr/bin/ffmpeg"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / "img.jpg"
            aud = Path(tmp) / "aud.m4a"
            out = Path(tmp) / out_name
            img.write_bytes(image)
            aud.write_bytes(audio)
            # -loop 1: repeat the still; -shortest: end with the music.
            # 720x1280 @ 12fps: a still frame needs no more — keeps encode ~10s.
            proc = subprocess.run(
                [
                    ffmpeg, "-y",
                    "-loop", "1", "-framerate", "12", "-i", str(img),
                    "-i", str(aud),
                    "-c:v", "libx264", "-preset", "ultrafast",
                    "-vf", "scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2,format=yuv420p",
                    "-r", "12",
                    "-c:a", "aac", "-b:a", "128k",
                    "-shortest", "-t", "180", "-movflags", "+faststart",
                    str(out),
                ],
                capture_output=True,
                timeout=180,
            )
            if proc.returncode == 0 and out.exists() and out.stat().st_size > 50000:
                return out.read_bytes()
            log.error("_photo_slideshow_video: rc=%d %s",
                      proc.returncode, proc.stderr.decode(errors="replace")[-250:])
    except Exception as e:
        log.error("_photo_slideshow_video: %s", e)
    return None


@_heavy_download
def do_tiktok_dl(cid: int, url: str):
    """دانلود TikTok — ویدیو، پست عکس (با موزیک) یا صدا بر اساس نوع پست."""
    bump(cid, "downloads")
    _status(cid, "⏳ در حال دانلود از TikTok…")
    chat_action(cid, "upload_video")

    # Keep metadata local: no negative cache, stale URLs, or cross-user reuse.
    tikwm_data = _fetch_tikwm_data(url)
    photo_data = _tiktok_is_photo_post(url, tikwm_data) if tikwm_data is not None else None

    if photo_data:
        images = photo_data["images"]
        music_url = photo_data.get("music") or ""
        caption = (photo_data.get("title") or "")[:1024]

        music_bytes = download_bytes(music_url, headers={"Referer": "https://www.tikwm.com/"}) if music_url else None
        all_ok = not music_url or bool(music_bytes)
        music_in_video = False
        if len(images) == 1:
            image = download_bytes(images[0])
            delivered = False
            if image and music_bytes:
                _status(cid, "🎬 در حال ساخت ویدیو از عکس و موزیک…")
                vid = _photo_slideshow_video(image, music_bytes, "tiktok_photo.mp4")
                if vid:
                    delivered = smart_send(cid, vid, "tiktok_photo.mp4", caption=caption)
                    music_in_video = bool(delivered)
            if image and not delivered:
                delivered = smart_send(cid, image, "tiktok_photo_1.jpg", caption=caption, media_type="photo")
            all_ok = bool(delivered) and all_ok
        else:
            # Download and release one album at a time; never discard slides 11+.
            for start in range(0, len(images), 10):
                group_items = []
                for i, img_url in enumerate(images[start:start + 10], start):
                    image = download_bytes(img_url)
                    if not image:
                        all_ok = False
                        continue
                    group_items.append({"data": image, "fname": f"tiktok_photo_{i + 1}.jpg",
                                        "is_video": False, "caption": caption if i == 0 else ""})
                if len(group_items) == 1:
                    item = group_items[0]
                    ok = smart_send(cid, item["data"], item["fname"], caption=item["caption"], media_type="photo")
                elif group_items:
                    ok = send_media_group(cid, group_items)
                else:
                    ok = False
                all_ok = bool(ok) and all_ok
                group_items.clear()

        if music_bytes and not music_in_video:
            mname = re.sub(r"[^\w\s-]", "", photo_data.get("title") or "")[:48].strip() or "tiktok_sound"
            ok = smart_send(cid, music_bytes, f"{mname}.mp3",
                            caption="🎵 موزیک این پست", media_type="audio")
            all_ok = bool(ok) and all_ok
        _status(cid, "✅ ارسال شد." if all_ok else "⚠️ دریافت یا ارسال بعضی رسانه‌ها ناموفق بود. می‌توانید دوباره تلاش کنید.",
                reply_markup=home_kb(), final=True)
        if all_ok:
            clear_state(cid)
        return

    # ── Normal video flow (audio-verified chain) ─────────────────────────
    result = tiktok_download(url, tikwm_data) if tikwm_data is not None else tiktok_download(url)
    if not result:
        _status(cid,
                "❌ دانلود از تیک‌تاک ناموفق بود.\n"
                "• ویدیو ممکن است خصوصی یا حذف شده باشد\n"
                "• سرویس موقتاً شلوغ است — کمی بعد تلاش کنید",
                reply_markup=home_kb(), final=True)
        return
    data, fname = result

    # Audio-only payload masquerading as a video = photo/sound post that
    # slipped through; retry once via the photo-post path.
    if _tiktok_has_audio_video_mismatch(data, fname):
        api_photo = _tiktok_is_photo_post(url)
        if api_photo:
            images = api_photo.get("images") or []
            music_url = api_photo.get("music") or ""
            caption = (api_photo.get("title") or "")[:1024]
            music_bytes = download_bytes(music_url, headers={"Referer": "https://www.tikwm.com/"}) if music_url else None
            delivered = False
            if len(images) == 1 and music_bytes:
                image = download_bytes(images[0])
                if image:
                    _status(cid, "🎬 در حال ساخت ویدیو از عکس و موزیک…")
                    vid = _photo_slideshow_video(image, music_bytes, "tiktok_photo.mp4")
                    if vid:
                        delivered = smart_send(cid, vid, "tiktok_photo.mp4", caption=caption)
            elif len(images) == 1:
                image = download_bytes(images[0])
                if image:
                    delivered = smart_send(cid, image, "tiktok_photo_1.jpg", caption=caption, media_type="photo")
            if music_bytes and not delivered:
                mname = re.sub(r"[^\w\s-]", "", api_photo.get("title") or "")[:48].strip() or "tiktok_sound"
                delivered = smart_send(cid, music_bytes, f"{mname}.mp3",
                                       caption="🎵 موزیک این پست", media_type="audio")
            _status(cid, "✅ ارسال شد." if delivered else "⚠️ دریافت یا ارسال بعضی رسانه‌ها ناموفق بود.",
                    reply_markup=home_kb(), final=True)
            if delivered:
                clear_state(cid)
            return
        log.warning("tiktok: mp3-in-mp4 payload but no photo data; sending as-is")

    # Caption: state (from timeline browse) → yt-dlp metadata fetch
    caption = get_state(cid).get("last_caption", "") or (
        _fetch_tiktok_caption(url, tikwm_data) if tikwm_data is not None else _fetch_tiktok_caption(url)
    )

    BALE_CAPTION_LIMIT = 1024
    inline_cap = (
        caption[:BALE_CAPTION_LIMIT] if len(caption) <= BALE_CAPTION_LIMIT else ""
    )
    all_ok = smart_send(cid, data, fname, caption=inline_cap)
    if caption and not inline_cap:
        all_ok = bool(send_message(cid, caption[:4096]).get("ok")) and all_ok
    _status(cid, "✅ ارسال شد." if all_ok else "⚠️ ارسال کامل نشد. لطفاً دوباره تلاش کنید.", reply_markup=home_kb(), final=True)
    if all_ok:
        clear_state(cid)


# --- TikTok photo-post detection (short links / API-based) ---
# NOTE: deliberately NOT @_heavy_download — it runs synchronously inside
# do_tiktok_dl (itself heavy-wrapped); a nested submit returns a Future
# and would deadlock the HEAVY_DOWNLOAD_SEMAPHORE.
def _tiktok_is_photo_post(url: str, tikwm_data=None) -> Optional[dict]:
    """Return tikwm data dict if the post is a photo (image) post, else None.

    Short share links (/t/...) and music-bearing photo posts can't be
    detected from the URL string alone, so we ask the API: photo posts
    carry an images[] array.
    """
    d = tikwm_data if tikwm_data is not None else _fetch_tikwm_data(url)
    return d if d and d.get("images") else None


def _tiktok_has_audio_video_mismatch(data: bytes, filename: str) -> bool:
    """True if bytes decode as audio-only (mp3) but the filename says mp4.

    Prevents TikTok photo posts whose 'play' URL is actually the music file
    from being shipped to the user as a silent .mp4 video.
    """
    if not filename.lower().endswith((".mp4", ".mov", ".mkv", ".webm")):
        return False
    if data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        log.warning("tiktok: mp3 payload in %s filename - treating as photo/sound post", filename)
        return True
    return False


def do_tiktok_sound(cid: int, url: str):
    """دانلود صدای (موزیک) یک پست TikTok."""
    bump(cid, "downloads")
    _status(cid, "⏳ در حال دریافت صدای TikTok…")
    chat_action(cid)
    got = _tiktok_music_dl(url)
    if not got:
        _status(
            cid,
            "❌ دریافت صدا ناموفق بود.\nلینک باید پست TikTok باشد (ویدیو یا عکس).",
            reply_markup=home_kb(), final=True,
        )
        return
    data, fname = got
    ok = smart_send(cid, data, fname, caption="🎵 صدای تیک‌تاک", media_type="audio")
    _status(cid, "✅ صدا ارسال شد." if ok else "❌ ارسال صدا ناموفق بود.", reply_markup=home_kb(), final=True)
    if ok:
        clear_state(cid)


# ─── Reddit Download Handler ─────────────────────────────────────────────
@_heavy_download
def do_reddit_dl(cid: int, url: str, audio_only=False, format_id=None):
    """Download Reddit post media."""
    bump(cid, "downloads")
    _status(cid, "⏳ در حال دانلود از رددیت…")
    chat_action(cid, "upload_video" if not audio_only else "upload_audio")
    result = reddit_download(url, audio_only=audio_only, format_id=format_id)
    if not result:
        _status(cid, "❌ دانلود ناموفق بود.", reply_markup=home_kb(), final=True)
        return
    for data, fname in result:
        smart_send(cid, data, fname)
    _status(cid, "✅ ارسال شد.", reply_markup=home_kb(), final=True)
    clear_state(cid)


def do_reddit_profile(cid: int, username: str):
    """Show Reddit user posts."""
    bump(cid, "searches")
    _status(cid, f"⏳ دریافت پست‌های u/{username}…")
    posts = reddit_user_posts(username)
    if not posts:
        _status(cid, "❌ پستی یافت نشد.", reply_markup=home_kb(), final=True)
        return
    key = make_cache_key("reddit", f"{_platform()}:{cid}:u/{username}", 0)
    cache_set_owned(key, posts, _platform(), cid)
    rows = []
    for i, p in enumerate(posts[:10]):
        label = f"📝 {p['title'][:50]}"
        rows.append([{"text": label, "callback_data": f"reddit_item_{key}_{i}"}])
    rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
    send_message(cid, f"📱 پست‌های u/{username}:", reply_markup={"inline_keyboard": rows})
    clear_state(cid)


# ─── Twitch Download Handler ─────────────────────────────────────────────
@_heavy_download
def do_twitch_dl(cid: int, url: str, audio_only=False, format_id=None):
    """Download Twitch VOD/Clip."""
    bump(cid, "downloads")
    _status(cid, "⏳ در حال دانلود از تویچ…")
    chat_action(cid, "upload_video" if not audio_only else "upload_audio")
    result = twitch_download(url, audio_only=audio_only, format_id=format_id)
    if not result:
        _status(cid, "❌ دانلود ناموفق بود.", reply_markup=home_kb(), final=True)
        return
    for data, fname in result:
        smart_send(cid, data, fname)
    _status(cid, "✅ ارسال شد.", reply_markup=home_kb(), final=True)
    clear_state(cid)


def do_twitch_user(cid: int, username: str):
    """Show Twitch user videos."""
    bump(cid, "searches")
    _status(cid, f"⏳ دریافت ویدیوهای {username}…")
    vids = twitch_user_videos(username)
    if not vids:
        _status(cid, "❌ ویدیویی یافت نشد.", reply_markup=home_kb(), final=True)
        return
    key = make_cache_key("twitch", f"{_platform()}:{cid}:{username}", 0)
    cache_set_owned(key, vids, _platform(), cid)
    rows = []
    for i, v in enumerate(vids[:10]):
        dur = f" ⏱{v['duration']}" if v.get('duration') else ""
        label = f"🎮 {v['title'][:45]}{dur}"
        rows.append([{"text": label, "callback_data": f"twitch_item_{key}_{i}"}])
    rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
    send_message(cid, f"🎮 ویدیوهای {username}:", reply_markup={"inline_keyboard": rows})
    clear_state(cid)


# ─── Vimeo Download Handler ──────────────────────────────────────────────
@_heavy_download
def do_vimeo_dl(cid: int, url: str, audio_only=False, format_id=None):
    """Download Vimeo video."""
    bump(cid, "downloads")
    _status(cid, "⏳ در حال دانلود از ویمئو…")
    chat_action(cid, "upload_video" if not audio_only else "upload_audio")
    result = vimeo_download(url, audio_only=audio_only, format_id=format_id)
    if not result:
        _status(cid, "❌ دانلود ناموفق بود.", reply_markup=home_kb(), final=True)
        return
    for data, fname in result:
        smart_send(cid, data, fname)
    _status(cid, "✅ ارسال شد.", reply_markup=home_kb(), final=True)
    clear_state(cid)


# ─── Pinterest Video Download Handler ─────────────────────────────────────
@_heavy_download
def do_pinterest_video_dl(cid: int, url: str, audio_only=False, format_id=None):
    """Download Pinterest video."""
    bump(cid, "downloads")
    _status(cid, "⏳ در حال دانلود از پینترست…")
    chat_action(cid, "upload_video" if not audio_only else "upload_audio")
    result = pinterest_video_download(url, audio_only=audio_only, format_id=format_id)
    if not result:
        _status(cid, "❌ دانلود ناموفق بود.", reply_markup=home_kb(), final=True)
        return
    for data, fname in result:
        smart_send(cid, data, fname)
    _status(cid, "✅ ارسال شد.", reply_markup=home_kb(), final=True)
    clear_state(cid)


# ─── Direct HLS Download Handler ─────────────────────────────────────────
@_heavy_download
def do_hls_dl(cid: int, url: str, audio_only=False, format_id=None):
    """Download direct HLS/m3u8 stream."""
    bump(cid, "downloads")
    _status(cid, "⏳ در حال دانلود استریم HLS…")
    chat_action(cid, "upload_video" if not audio_only else "upload_audio")
    result = hls_download(url, audio_only=audio_only, format_id=format_id)
    if not result:
        _status(cid, "❌ دانلود ناموفق بود.", reply_markup=home_kb(), final=True)
        return
    data, fname = result
    smart_send(cid, data, fname)
    _status(cid, "✅ ارسال شد.", reply_markup=home_kb(), final=True)
    clear_state(cid)


# ═══════════════════════════════════════════════════════════════════════════════
# VIRUS SCAN  (VirusTotal free API — 4 req/min, 500 req/day, 32MB max)
# Set VIRUSTOTAL_API_KEY in .env
# ═══════════════════════════════════════════════════════════════════════════════


def virustotal_scan_file(data: bytes, filename: str) -> Optional[dict]:
    """
    Upload file to VirusTotal via vt-py and poll for result.
    Returns dict with: malicious, suspicious, clean, total, verdict, detections.
    """
    if not VIRUSTOTAL_API_KEY:
        return None
    if len(data) > 32 * 1024 * 1024:
        return {"error": "فایل بزرگتر از ۳۲MB است — حد مجاز VirusTotal"}

    log.info("virustotal_scan_file: %s  %.1fMB", filename, len(data) / 1024 / 1024)
    try:
        import vt
    except ImportError:
        log.error("vt-py not installed — pip install vt-py --break-system-packages")
        return {"error": "کتابخانه vt-py نصب نیست — با pip install vt-py نصب کنید"}
    try:
        client = vt.Client(VIRUSTOTAL_API_KEY)
        try:
            # wait_for_completion=True blocks until analysis done (no 'size' param)
            analysis = client.scan_file(io.BytesIO(data), wait_for_completion=True)
            stats = analysis.stats
            malicious = stats.get("malicious", 0)
            suspicious = stats.get("suspicious", 0)
            undetected = stats.get("undetected", 0)
            harmless = stats.get("harmless", 0)
            total = malicious + suspicious + undetected + harmless
            detections = sorted(
                [
                    f"{eng}: {res.get('result', '?')}"
                    for eng, res in (analysis.results or {}).items()
                    if res.get("category") in ("malicious", "suspicious")
                ]
            )[:20]
            verdict = (
                "malicious"
                if malicious > 0
                else "suspicious"
                if suspicious > 0
                else "clean"
            )
            # SHA256 is encoded in analysis id: file-{sha256}-{datetime}
            sha256 = ""
            parts = (analysis.id or "").split("-")
            if len(parts) >= 2 and len(parts[1]) == 64:
                sha256 = parts[1]
            log.info("VT file: %s  %d/%d", verdict, malicious, total)
            return {
                "malicious": malicious,
                "suspicious": suspicious,
                "clean": undetected + harmless,
                "total": total,
                "verdict": verdict,
                "detections": detections,
                "sha256": sha256,
                "analysis_id": analysis.id,
            }
        finally:
            client.close()
    except Exception as e:
        msg = str(e)
        log.error("virustotal_scan_file: %s", msg)
        if "WrongCredentials" in type(e).__name__ or "401" in msg:
            return {"error": "کلید API VirusTotal نامعتبر است"}
        if "QuotaExceeded" in type(e).__name__ or "429" in msg:
            return {"error": "محدودیت روزانه VirusTotal تمام شد (۵۰۰ اسکن/روز)"}
        return {"error": f"خطای VirusTotal: {msg[:100]}"}


def virustotal_scan_url(url: str) -> Optional[dict]:
    """Scan a URL with VirusTotal via vt-py."""
    if not VIRUSTOTAL_API_KEY:
        return None
    log.info("virustotal_scan_url: %s", url)
    try:
        import vt

        client = vt.Client(VIRUSTOTAL_API_KEY)
        try:
            analysis = client.scan_url(url)
            for _ in range(18):
                time.sleep(5)
                analysis = client.get_object(f"/analyses/{analysis.id}")
                if analysis.status == "completed":
                    break
            if analysis.status != "completed":
                return {"error": "تحلیل VirusTotal تمام نشد"}

            stats = analysis.stats
            malicious = stats.get("malicious", 0)
            suspicious = stats.get("suspicious", 0)
            undetected = stats.get("undetected", 0)
            harmless = stats.get("harmless", 0)
            total = malicious + suspicious + undetected + harmless
            detections = sorted(
                [
                    f"{eng}: {res.get('result', '?')}"
                    for eng, res in (analysis.results or {}).items()
                    if res.get("category") in ("malicious", "suspicious")
                ]
            )[:20]
            verdict = (
                "malicious"
                if malicious > 0
                else "suspicious"
                if suspicious > 0
                else "clean"
            )
            log.info("VT url result: %s  %d/%d", verdict, malicious, total)
            return {
                "malicious": malicious,
                "suspicious": suspicious,
                "clean": undetected + harmless,
                "total": total,
                "verdict": verdict,
                "detections": detections,
                "sha256": "",
                "analysis_id": analysis.id,
            }
        finally:
            client.close()
    except ImportError:
        log.error("vt-py not installed")
        return None
    except Exception as e:
        log.error("virustotal_scan_url: %s", e)
        if "QuotaExceededError" in type(e).__name__ or "429" in str(e):
            return {"error": "محدودیت روزانه VirusTotal تمام شد"}
        return None


def _format_vt_result(result: dict, name: str) -> str:
    """Format VirusTotal result as a readable Persian message."""
    if result.get("error"):
        return f"⚠️ {result['error']}"

    verdict = result.get("verdict", "unknown")
    malicious = result.get("malicious", 0)
    suspicious = result.get("suspicious", 0)
    total = result.get("total", 0)
    clean = result.get("clean", 0)
    sha256 = result.get("sha256", "")

    emoji = "✅" if verdict == "clean" else "⚠️" if verdict == "suspicious" else "🚨"
    verdict_fa = {"clean": "پاک", "suspicious": "مشکوک", "malicious": "مخرب"}.get(
        verdict, verdict
    )

    lines = [
        f"{emoji} *نتیجه بررسی:* {verdict_fa}",
        f"📄 `{name[:60]}`",
        "",
        f"🔴 مخرب: {malicious} آنتی‌ویروس",
        f"🟡 مشکوک: {suspicious} آنتی‌ویروس",
        f"🟢 پاک: {clean} آنتی‌ویروس",
        f"📊 مجموع: {total} آنتی‌ویروس",
    ]
    if result.get("detections"):
        lines.append("\n⚠️ *موارد شناسایی‌شده:*")
        for d in result["detections"][:10]:
            lines.append(f"  • `{d}`")
    if sha256:
        lines.append(f"\n🔗 [گزارش کامل](https://www.virustotal.com/gui/file/{sha256})")
    return "\n".join(lines)


def do_virusscan(cid: int, data: bytes, filename: str):
    """Scan uploaded file with VirusTotal and report result."""
    if not VIRUSTOTAL_API_KEY:
        send_message(
            cid,
            "⚠️ کلید API VirusTotal تنظیم نشده است.\n"
            "متغیر محیطی `VIRUSTOTAL_API_KEY` را تنظیم کنید.\n"
            "کلید رایگان: https://www.virustotal.com/gui/join-us",
            parse_mode="Markdown",
            reply_markup=home_kb(),
        )
        return

    size_mb = len(data) / 1024 / 1024
    _status(
        cid,
        f"🔍 در حال آپلود `{filename}` ({size_mb:.1f}MB) به VirusTotal…\n"
        "ممکن است تا ۶۰ ثانیه طول بکشد.",
        parse_mode="Markdown",
    )
    chat_action(cid, "typing")

    result = virustotal_scan_file(data, filename)
    if result is None:
        _status(cid, "❌ خطا در ارتباط با VirusTotal.", reply_markup=home_kb(), final=True)
        return

    msg = _format_vt_result(result, filename)
    send_message(cid, msg, parse_mode="Markdown", reply_markup=home_kb())
    clear_state(cid)


def do_virusscan_url(cid: int, url: str):
    """Scan a URL with VirusTotal."""
    if not VIRUSTOTAL_API_KEY:
        send_message(
            cid, "⚠️ کلید API VirusTotal تنظیم نشده است.", reply_markup=home_kb()
        )
        return
    _status(cid, f"🔍 در حال بررسی آدرس در VirusTotal…")
    chat_action(cid, "typing")
    result = virustotal_scan_url(url)
    if result is None:
        _status(cid, "❌ خطا در ارتباط با VirusTotal.", reply_markup=home_kb(), final=True)
        return
    msg = _format_vt_result(result, url)
    send_message(cid, msg, parse_mode="Markdown", reply_markup=home_kb())
    clear_state(cid)


# ═══════════════════════════════════════════════════════════════════════════════
# YOUTUBE EXTRAS  (thumbnail download · video stats card)
# ═══════════════════════════════════════════════════════════════════════════════


def do_youtube_thumbnail(cid: int, url: str):
    """Download and send YouTube video thumbnail at highest available resolution."""
    bump(cid, "downloads")
    _status(cid, "⏳ در حال دریافت کاور ویدیو…")
    # yt-dlp --dump-json to get thumbnail URL, then try maxresdefault → hqdefault
    try:
        r = subprocess.run(
            [YTDLP_BIN, "--no-warnings", "--dump-json", "--no-playlist", url],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if r.returncode == 0:
            import json as _j

            info = _j.loads(r.stdout)
            thumb_url = info.get("thumbnail", "")
            vid_id = info.get("id", "")
            title = info.get("title", "ویدیو")[:60]
            # Try multiple resolutions
            candidates = []
            if vid_id:
                candidates = [
                    f"https://img.youtube.com/vi/{vid_id}/maxresdefault.jpg",
                    f"https://img.youtube.com/vi/{vid_id}/hqdefault.jpg",
                    f"https://img.youtube.com/vi/{vid_id}/mqdefault.jpg",
                ]
            if thumb_url:
                candidates.insert(0, thumb_url)
            for turl in candidates:
                data = download_bytes(turl, MAX_IMAGE_SIZE)
                if data and len(data) > 5000:
                    fname = f"thumbnail_{vid_id or 'yt'}.jpg"
                    send_photo(cid, data, caption=f"🖼 {title}")
                    _status(cid, "✅ کاور ارسال شد.", reply_markup=home_kb(), final=True)
                    return
    except Exception as e:
        log.error("do_youtube_thumbnail: %s", e)
    _status(cid, "❌ دریافت کاور ناموفق بود.", reply_markup=home_kb(), final=True)


def do_youtube_stats(cid: int, url: str):
    """Fetch YouTube video stats (views, likes, description) and send as a card."""
    bump(cid, "searches")
    _status(cid, "⏳ در حال دریافت آمار ویدیو…")
    try:
        r = subprocess.run(
            [YTDLP_BIN, "--no-warnings", "--dump-json", "--no-playlist", url],
            capture_output=True,
            text=True,
            timeout=25,
        )
        if r.returncode != 0:
            _status(cid, "❌ دریافت اطلاعات ناموفق بود.", reply_markup=home_kb(), final=True)
            return
        import json as _j

        info = _j.loads(r.stdout)
        title = info.get("title", "")[:80]
        uploader = info.get("uploader", info.get("channel", ""))
        views = info.get("view_count", 0) or 0
        likes = info.get("like_count", 0) or 0
        duration = info.get("duration_string", info.get("duration", ""))
        upload = info.get("upload_date", "")
        desc = (info.get("description") or "")[:800]
        vid_id = info.get("id", "")

        def _fmt(n):
            try:
                n = int(n)
                if n >= 1_000_000:
                    return f"{n / 1_000_000:.1f}M"
                if n >= 1_000:
                    return f"{n / 1_000:.1f}K"
                return str(n)
            except Exception:
                return str(n)

        date_str = ""
        if upload and len(upload) == 8:
            date_str = f"{upload[6:8]}/{upload[4:6]}/{upload[:4]}"

        lines = [
            f"▶️ *{title}*",
            f"👤 {uploader}",
            f"",
            f"👁 بازدید: {_fmt(views)}",
            f"👍 لایک: {_fmt(likes)}",
            f"⏱ مدت: {duration}",
        ]
        if date_str:
            lines.append(f"📅 تاریخ: {date_str}")
        if desc:
            lines += ["", f"📝 *توضیحات:*", desc]

        text = "\n".join(lines)

        # Also send thumbnail
        thumb_url = info.get("thumbnail", "")
        if vid_id:
            for tu in [f"https://img.youtube.com/vi/{vid_id}/hqdefault.jpg", thumb_url]:
                data = download_bytes(tu, MAX_IMAGE_SIZE) if tu else None
                if data and len(data) > 5000:
                    send_photo(cid, data, caption=title[:1024])
                    break

        send_message(cid, text, parse_mode="Markdown", reply_markup=home_kb())
    except Exception as e:
        log.error("do_youtube_stats: %s", e)
        _status(cid, "❌ دریافت آمار ناموفق بود.", reply_markup=home_kb(), final=True)


# ═══════════════════════════════════════════════════════════════════════════════
# INSTAGRAM EXTRAS  (profile pic · profile screenshot · carousel mosaic)
# ═══════════════════════════════════════════════════════════════════════════════


def _ig_fetch_user_info(username: str) -> dict:
    """
    Fetch Instagram user info via multiple public strategies.
    Automatically uses WARP proxy if configured (bypasses Hetzner datacenter blocks).
    Uses a fresh session per call to avoid sharing 429-tainted state.
    """
    username = username.strip().lstrip("@")
    # Always create a fresh session for Instagram (avoids reusing 429-tainted connection state)
    proxy = WARP_PROXY or ""
    sess = _make_session(proxy)

    # Strategy 1: Instagram internal web API (requires residential/WARP IP)
    for ua in [
        (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
        ),
        (
            "Instagram 289.0.0.77.109 Android (33/13; 420dpi; 1080x2400; "
            "samsung; SM-G991B; o1s; exynos2100; en_US; 481895820)"
        ),
    ]:
        try:
            r = sess.get(
                f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}",
                headers={
                    "User-Agent": ua,
                    "X-IG-App-ID": "936619743392459",
                    "Accept": "*/*",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer": f"https://www.instagram.com/{username}/",
                    "X-Requested-With": "XMLHttpRequest",
                },
                timeout=15,
            )
            log.debug("_ig_fetch_user_info web_profile_info: HTTP %d", r.status_code)
            if r.status_code == 200:
                j = r.json()
                user = j.get("data", {}).get("user", {})
                if user:
                    log.info(
                        "_ig_fetch_user_info: OK via web API (%d edges)",
                        len(
                            user.get("edge_owner_to_timeline_media", {}).get(
                                "edges", []
                            )
                        ),
                    )
                    return user
            if r.status_code == 429:
                log.warning("_ig_fetch_user_info: 429 rate limit (ua=%s…)", ua[:30])
                time.sleep(3)
        except Exception as e:
            log.warning("_ig_fetch_user_info: %s", e)

    # Strategy 2: Instagram oEmbed (works without auth for public accounts)
    try:
        r = sess.get(
            f"https://www.instagram.com/api/v1/oembed/?url=https://instagram.com/{username}/",
            headers={"User-Agent": UA_MOB, "Accept": "application/json"},
            timeout=10,
        )
        log.debug("_ig_fetch_user_info oEmbed: HTTP %d", r.status_code)
        if r.status_code == 200:
            j = r.json()
            if j.get("author_name"):
                # Build minimal user dict from oembed
                return {
                    "full_name": j.get("author_name", ""),
                    "biography": "",
                    "edge_followed_by": {"count": 0},
                    "edge_follow": {"count": 0},
                    "edge_owner_to_timeline_media": {"count": 0, "edges": []},
                    "external_url": "",
                    "is_verified": False,
                    "profile_pic_url": j.get("thumbnail_url", ""),
                    "profile_pic_url_hd": j.get("thumbnail_url", ""),
                    "_oembed_only": True,
                }
    except Exception as e:
        log.warning("_ig_fetch_user_info oEmbed: %s", e)

    # Strategy 3: public graphql endpoint
    try:
        r = sess.get(
            f"https://www.instagram.com/{username}/?__a=1&__d=dis",
            headers={
                "User-Agent": UA_MOB,
                "Accept": "*/*",
                "X-IG-App-ID": "936619743392459",
            },
            timeout=15,
        )
        log.debug("_ig_fetch_user_info graphql: HTTP %d", r.status_code)
        if r.status_code == 200:
            j = r.json()
            user = (
                j.get("graphql", {}).get("user") or j.get("data", {}).get("user") or {}
            )
            if user:
                log.info("_ig_fetch_user_info: OK via graphql")
                return user
    except Exception as e:
        log.warning("_ig_fetch_user_info graphql: %s", e)

    # Strategy 4: instaloader (authenticated, uses its own session management)
    if INSTAGRAM_USER and INSTAGRAM_PASS:
        try:
            import instaloader

            L = instaloader.Instaloader(
                quiet=True,
                download_pictures=False,
                download_videos=False,
                download_video_thumbnails=False,
            )
            try:
                L.login(INSTAGRAM_USER, INSTAGRAM_PASS)
            except Exception as le:
                log.warning("_ig_fetch_user_info instaloader login: %s", le)
            profile = instaloader.Profile.from_username(L.context, username)
            log.info("_ig_fetch_user_info: OK via instaloader")
            return {
                "full_name": profile.full_name,
                "username": profile.username,
                "biography": profile.biography or "",
                "edge_followed_by": {"count": profile.followers},
                "edge_follow": {"count": profile.followees},
                "edge_owner_to_timeline_media": {
                    "count": profile.mediacount,
                    "edges": [],
                },
                "external_url": profile.external_url or "",
                "is_verified": profile.is_verified,
                "profile_pic_url": profile.profile_pic_url,
                "profile_pic_url_hd": profile.profile_pic_url,
                "_from_instaloader": True,
            }
        except Exception as e:
            log.warning("_ig_fetch_user_info instaloader: %s", e)

    log.error("_ig_fetch_user_info: all strategies failed for @%s", username)
    return {}


def do_instagram_profile_pic(cid: int, username: str):
    """Download and send Instagram profile picture at full size."""
    bump(cid, "downloads")
    username = username.strip().lstrip("@")
    _status(cid, f"⏳ در حال دریافت عکس پروفایل @{username}…")

    # Try public API first
    user = _ig_fetch_user_info(username)
    pic_url = user.get("profile_pic_url_hd") or user.get("profile_pic_url") or ""

    if not pic_url and INSTAGRAM_USER and INSTAGRAM_PASS:
        try:
            import instaloader

            L = instaloader.Instaloader(quiet=True, download_pictures=False)
            try:
                L.login(INSTAGRAM_USER, INSTAGRAM_PASS)
            except Exception:
                pass
            profile = instaloader.Profile.from_username(L.context, username)
            pic_url = profile.profile_pic_url
        except Exception as e:
            log.error("do_instagram_profile_pic instaloader: %s", e)

    if pic_url:
        data = download_bytes(pic_url, MAX_IMAGE_SIZE)
        if data and len(data) > 1000:
            send_photo(cid, data, caption=f"🖼 تصویر پروفایل @{username}")
            _status(cid, "✅ ارسال شد.", reply_markup=home_kb(), final=True)
            return

    _status(cid, "❌ دریافت عکس پروفایل ناموفق بود.", reply_markup=home_kb(), final=True)


def do_instagram_profile_screenshot(cid: int, username: str):
    """
    Build a profile card image with:
    bio, followers, following, post count, last 9 post thumbnails.
    Uses Pillow — no browser needed.
    """
    bump(cid, "downloads")
    username = username.strip().lstrip("@")
    _status(cid, f"⏳ در حال ساخت اسکرین‌شات پروفایل @{username}…")
    try:
        import textwrap as _tw
        from io import BytesIO as _BIO

        from PIL import Image, ImageDraw, ImageFont

        # Fetch user info via public API (no login needed for public accounts)
        user = _ig_fetch_user_info(username)
        if not user and INSTAGRAM_USER and INSTAGRAM_PASS:
            try:
                import instaloader as _il

                L2 = _il.Instaloader(quiet=True, download_pictures=False)
                L2.login(INSTAGRAM_USER, INSTAGRAM_PASS)
                p2 = _il.Profile.from_username(L2.context, username)
                user = {
                    "full_name": p2.full_name,
                    "biography": p2.biography,
                    "edge_followed_by": {"count": p2.followers},
                    "edge_follow": {"count": p2.followees},
                    "edge_owner_to_timeline_media": {"count": p2.mediacount},
                    "external_url": p2.external_url,
                    "is_verified": p2.is_verified,
                    "profile_pic_url_hd": p2.profile_pic_url,
                    "_il_profile": p2,
                }
            except Exception as ie:
                log.error("do_instagram_profile_screenshot instaloader: %s", ie)

        if not user:
            _status(
                cid,
                "❌ پروفایل یافت نشد.\n• نام کاربری را بررسی کنید\n• پروفایل باید عمومی باشد",
                reply_markup=home_kb(), final=True)
            return

        full_name = user.get("full_name") or username
        bio = user.get("biography") or ""
        followers = user.get("edge_followed_by", {}).get("count", 0)
        following = user.get("edge_follow", {}).get("count", 0)
        post_count = user.get("edge_owner_to_timeline_media", {}).get("count", 0)
        ext_url = user.get("external_url") or ""
        is_verified = user.get("is_verified", False)
        pic_url = user.get("profile_pic_url_hd") or user.get("profile_pic_url", "")

        # Fetch 9 post thumbnails from web API data
        thumbs = []
        edges = user.get("edge_owner_to_timeline_media", {}).get("edges", [])
        for edge in edges[:9]:
            node = edge.get("node", {})
            turl = node.get("display_url", "") or node.get("thumbnail_src", "")
            if turl:
                td = download_bytes(turl, MAX_IMAGE_SIZE)
                if td:
                    thumbs.append(td)
        # Fallback: instaloader posts if API gave no edges
        if not thumbs and "_il_profile" in user:
            for post in user["_il_profile"].get_posts():
                if len(thumbs) >= 9:
                    break
                try:
                    td = download_bytes(post.url, MAX_IMAGE_SIZE)
                    if td:
                        thumbs.append(td)
                except Exception:
                    pass

        # ── Build card ──────────────────────────────────────────────────
        W, PAD = 640, 20
        BG = (250, 250, 250)
        DARK = (30, 30, 30)
        MUTED = (120, 120, 120)
        PINK = (228, 64, 95)

        def _fnt(size, bold=False):
            for p in (
                ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
                if bold
                else ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
            ):
                if Path(p).exists():
                    return ImageFont.truetype(p, size)
            return ImageFont.load_default()

        fn_name = _fnt(18, bold=True)
        fn_body = _fnt(14)
        fn_small = _fnt(12)

        # Avatar
        AV = 80
        av_img = None
        try:
            av_data = download_bytes(pic_url, MAX_IMAGE_SIZE)
            av = Image.open(_BIO(av_data)).convert("RGBA").resize((AV, AV))
            mask = Image.new("L", (AV, AV), 0)
            ImageDraw.Draw(mask).ellipse([(0, 0), (AV, AV)], fill=255)
            base = Image.new("RGBA", (AV, AV), BG + (255,))
            base.paste(av, mask=mask)
            av_img = base
        except Exception:
            pass

        # Bio wrap
        bio_lines = []
        for para in bio.split("\n"):
            bio_lines.extend(_tw.wrap(para, width=55) or [""])

        lh = 18
        bio_h = len(bio_lines) * lh
        GRID_S = (W - PAD * 2 - 4) // 3  # size of each thumbnail cell

        HEADER_H = PAD + max(AV, 80) + PAD
        INFO_H = 24 + PAD  # stats row
        BIO_H = bio_h + (PAD if bio else 0)
        URL_H = 20 + PAD if ext_url else 0
        GRID_H = (GRID_S + 2) if thumbs else 0
        if len(thumbs) > 3:
            GRID_H = (GRID_S + 2) * ((len(thumbs) - 1) // 3 + 1)
        H = HEADER_H + INFO_H + BIO_H + URL_H + GRID_H + PAD

        img = Image.new("RGB", (W, H), BG)
        draw = ImageDraw.Draw(img)

        # Avatar
        ay = PAD
        if av_img:
            img.paste(av_img, (PAD, ay), av_img)

        # Name + verified
        nx = PAD + AV + PAD
        vbadge = " ✓" if is_verified else ""
        draw.text((nx, ay), full_name + vbadge, font=fn_name, fill=DARK)
        draw.text((nx, ay + 22), f"@{username}", font=fn_body, fill=MUTED)

        # Stats row below avatar
        ty = PAD + max(AV, 60) + PAD // 2

        def _fmt(n):
            if n >= 1_000_000:
                return f"{n / 1_000_000:.1f}M"
            if n >= 1_000:
                return f"{n / 1_000:.1f}K"
            return str(n)

        stats = [
            (str(post_count), "پست"),
            (_fmt(followers), "فالوور"),
            (_fmt(following), "دنبال‌شده"),
        ]
        col = W // 3
        for i, (val, label) in enumerate(stats):
            cx = i * col + col // 2
            vw, _ = draw.textbbox((0, 0), val, font=fn_name)[2:4]  # approximate
            draw.text((cx - 20, ty), val, font=fn_name, fill=DARK, align="center")
            draw.text((cx - 20, ty + 20), label, font=fn_small, fill=MUTED)

        ty += INFO_H

        # Bio
        for line in bio_lines:
            draw.text((PAD, ty), line, font=fn_body, fill=DARK)
            ty += lh
        if bio:
            ty += PAD // 2

        # External URL
        if ext_url:
            draw.text((PAD, ty), f"🔗 {ext_url[:60]}", font=fn_small, fill=PINK)
            ty += URL_H

        # Grid of thumbnails
        if thumbs:
            draw.line([(PAD, ty), (W - PAD, ty)], fill=(220, 220, 220), width=1)
            ty += PAD // 2
            for i, tb in enumerate(thumbs[:9]):
                try:
                    ti = Image.open(_BIO(tb)).convert("RGB")
                    ti = ti.resize((GRID_S, GRID_S), Image.LANCZOS)
                    gx = PAD + (i % 3) * (GRID_S + 2)
                    gy = ty + (i // 3) * (GRID_S + 2)
                    img.paste(ti, (gx, gy))
                except Exception:
                    pass

        buf = _BIO()
        img.save(buf, format="PNG", optimize=True)
        card = buf.getvalue()
        log.info("ig_profile_shot: %dx%d  %.1fKB", W, H, len(card) / 1024)
        send_photo(cid, card, caption=f"📸 پروفایل @{username}")
        _status(cid, "✅ ارسال شد.", reply_markup=home_kb(), final=True)

    except Exception as e:
        log.error("do_instagram_profile_screenshot: %s", e)
        _status(
            cid,
            "❌ ساخت اسکرین‌شات ناموفق بود.\n"
            "• پروفایل باید عمومی باشد\n"
            "• `instaloader` نصب باشد",
            reply_markup=home_kb(), final=True)


def do_instagram_mosaic(cid: int, url: str):
    """
    Build a mosaic grid image from all carousel slides of an Instagram post.
    Sends the grid as a single PNG.
    """
    bump(cid, "downloads")
    _status(cid, "⏳ در حال دانلود اسلایدها و ساخت موزاییک…")
    try:
        from io import BytesIO as _BIO

        from PIL import Image

        items = instagram_download_all(url)
        images = [it for it in items if not it.get("is_video") and it.get("data")]
        if not images:
            _status(
                cid, "❌ تصویری برای موزاییک یافت نشد.", reply_markup=home_kb(), final=True)
            return

        n = len(images)
        cols = min(3, n)
        rows = (n + cols - 1) // cols
        S = 320  # cell size

        grid = Image.new("RGB", (cols * S, rows * S), (30, 30, 30))
        for i, item in enumerate(images[:9]):
            try:
                tile = Image.open(_BIO(item["data"])).convert("RGB")
                # Crop to square centre
                w, h = tile.size
                side = min(w, h)
                tile = tile.crop(
                    ((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2)
                )
                tile = tile.resize((S, S), Image.LANCZOS)
                gx = (i % cols) * S
                gy = (i // cols) * S
                grid.paste(tile, (gx, gy))
            except Exception:
                pass

        buf = _BIO()
        grid.save(buf, format="JPEG", quality=88, optimize=True)
        mosaic = buf.getvalue()
        log.info(
            "do_instagram_mosaic: %dx%d  %d images  %.1fKB",
            cols * S,
            rows * S,
            n,
            len(mosaic) / 1024,
        )
        caption = _fetch_instagram_caption(url)
        send_photo(
            cid,
            mosaic,
            caption=(caption[:1024] if caption else f"🔲 موزاییک — {n} تصویر"),
        )
        if caption and len(caption) > 1024:
            send_message(cid, caption[:4096])
        _status(cid, "✅ ارسال شد.", reply_markup=home_kb(), final=True)

    except Exception as e:
        log.error("do_instagram_mosaic: %s", e)
        _status(cid, "❌ ساخت موزاییک ناموفق بود.", reply_markup=home_kb(), final=True)


# ═══════════════════════════════════════════════════════════════════════════════
# TWITTER EXTRAS  (auto-translate tweet text)
# ═══════════════════════════════════════════════════════════════════════════════


def do_twitter_dl_translate(cid: int, url: str):
    """Download tweet media + auto-translate tweet text to Persian."""
    tw_data = _fetch_tweet_data(url)
    original = tw_data.get("text", "")
    if original:
        try:
            translated = translate_text(original, target="fa", source="auto")
            tw_data["text"] = translated
            tw_data["_original_text"] = original
        except Exception as e:
            log.warning("tweet translate: %s", e)
    do_twitter_dl(cid, url, _tw_data_override=tw_data)


# ═══════════════════════════════════════════════════════════════════════════════
# SOUNDCLOUD EXTRAS  (cover art + track info · on.soundcloud.com short links)
# ═══════════════════════════════════════════════════════════════════════════════


def do_soundcloud_info(cid: int, url: str):
    """
    Show SoundCloud track info (cover, title, artist, duration, plays)
    then offer download.
    """
    bump(cid, "searches")
    # Resolve on.soundcloud.com short links
    if "on.soundcloud.com" in url:
        try:
            r = WEB.head(url, allow_redirects=True, timeout=10)
            url = r.url
            log.info("soundcloud short link resolved: %s", url)
        except Exception:
            pass

    _status(cid, "⏳ در حال دریافت اطلاعات آهنگ…")
    try:
        r = subprocess.run(
            [YTDLP_BIN, "--no-warnings", "--dump-json", "--no-playlist", url],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if r.returncode != 0:
            # Fall back to direct download without info
            do_music(cid, url)
            return

        import json as _j

        info = _j.loads(r.stdout)
        title = info.get("title", "")[:80]
        uploader = info.get("uploader", info.get("artist", ""))[:50]
        duration = info.get("duration_string", "")
        plays = info.get("view_count", 0) or 0
        likes = info.get("like_count", 0) or 0
        desc = (info.get("description") or "")[:400]
        thumb = info.get("thumbnail", "")

        def _fmt(n):
            try:
                n = int(n)
                if n >= 1_000_000:
                    return f"{n / 1_000_000:.1f}M"
                if n >= 1_000:
                    return f"{n / 1_000:.1f}K"
                return str(n)
            except Exception:
                return str(n)

        lines = [
            f"☁️ *{title}*",
            f"🎤 {uploader}",
            f"⏱ {duration}" + (f"  |  👁 {_fmt(plays)}" if plays else ""),
        ]
        if likes:
            lines.append(f"❤️ {_fmt(likes)}")
        if desc:
            lines += ["", desc]

        if thumb:
            data = download_bytes(thumb, MAX_IMAGE_SIZE)
            if data and len(data) > 1000:
                send_photo(cid, data, caption="\n".join(lines)[:1024])
            else:
                send_message(cid, "\n".join(lines), parse_mode="Markdown")
        else:
            send_message(cid, "\n".join(lines), parse_mode="Markdown")

        # Offer download button
        url_key = store_url(url, chat_id=cid)
        kb = {
            "inline_keyboard": [
                [
                    {"text": "⬇️ دانلود آهنگ", "callback_data": f"sc_dl_{url_key}"},
                    {"text": "🏠 منوی اصلی", "callback_data": "home"},
                ]
            ]
        }
        send_message(cid, "برای دانلود دکمه را بزنید:", reply_markup=kb)

    except Exception as e:
        log.error("do_soundcloud_info: %s", e)
        do_music(cid, url)


# ═══════════════════════════════════════════════════════════════════════════════
# BLUESKY  (bsky.app — posts, videos, images, text)
# ═══════════════════════════════════════════════════════════════════════════════

BSKY_API = "https://public.api.bsky.app/xrpc"


def bsky_get_post(url: str) -> Optional[dict]:
    """
    Fetch a Bluesky post via the public ATP API (no login needed for public posts).
    Returns the post record dict or None.
    """
    # URL format: https://bsky.app/profile/user.bsky.social/post/RKEY
    m = re.search(r"bsky\.app/profile/([^/]+)/post/([A-Za-z0-9]+)", url)
    if not m:
        return None
    handle, rkey = m.group(1), m.group(2)
    try:
        # Resolve handle to DID
        r = WEB.get(
            f"{BSKY_API}/com.atproto.identity.resolveHandle",
            params={"handle": handle},
            headers={"Accept": "application/json"},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        did = r.json().get("did", "")
        if not did:
            return None

        # Fetch post
        at_uri = f"at://{did}/app.bsky.feed.post/{rkey}"
        r2 = WEB.get(
            f"{BSKY_API}/app.bsky.feed.getPosts",
            params={"uris": at_uri},
            headers={"Accept": "application/json"},
            timeout=10,
        )
        if r2.status_code != 200:
            return None
        posts = r2.json().get("posts", [])
        if not posts:
            return None

        post = posts[0]
        record = post.get("record", {})
        author = post.get("author", {})
        embed = post.get("embed", {})

        # Extract media URLs
        media_items = []
        etype = embed.get("$type", "")
        if "images" in etype or "images" in embed:
            for img in embed.get("images", []):
                furl = img.get("fullsize") or img.get("thumb", "")
                if furl:
                    media_items.append(
                        {"url": furl, "type": "image", "alt": img.get("alt", "")}
                    )
        elif "video" in etype or "video" in embed:
            vurl = embed.get("playlist") or embed.get("video", {}).get("ref", {}).get(
                "$link", ""
            )
            if vurl:
                media_items.append({"url": vurl, "type": "video"})

        return {
            "text": record.get("text", ""),
            "author_name": author.get("displayName", "") or author.get("handle", ""),
            "author_handle": author.get("handle", ""),
            "avatar_url": author.get("avatar", ""),
            "created_at": record.get("createdAt", ""),
            "likes": post.get("likeCount", 0),
            "reposts": post.get("repostCount", 0),
            "replies": post.get("replyCount", 0),
            "media": media_items,
            "did": did,
            "rkey": rkey,
        }
    except Exception as e:
        log.error("bsky_get_post: %s", e)
        return None


def do_bluesky_dl(cid: int, url: str):
    """Download media + text from a Bluesky post."""
    bump(cid, "downloads")
    _status(cid, "⏳ در حال دریافت از Bluesky…")
    chat_action(cid, "upload_photo")

    post = bsky_get_post(url)
    if not post:
        _status(cid, "❌ پست یافت نشد یا خصوصی است.", reply_markup=home_kb(), final=True)
        return

    text = post.get("text", "")
    author = post.get("author_handle", "")
    media = post.get("media", [])

    BALE_CAPTION_LIMIT = 1024

    if not media:
        # Text-only post — just send the text
        msg = f"🦋 *@{author}*\n\n{text}" if text else f"🦋 @{author}"
        send_message(cid, msg, parse_mode="Markdown", reply_markup=home_kb())
        return

    # Download all media items
    downloaded = []
    for item in media[:10]:
        murl = item.get("url", "")
        mtype = item.get("type", "image")
        if not murl:
            continue
        data = download_bytes(murl, MAX_FILE_SIZE)
        if data and len(data) > 500:
            ext = "mp4" if mtype == "video" else "jpg"
            downloaded.append(
                {
                    "data": data,
                    "fname": f"bsky_{rkey_from_url(url)}.{ext}",
                    "is_video": mtype == "video",
                    "caption": "",
                }
            )

    if not downloaded:
        _status(cid, "❌ دانلود رسانه ناموفق بود.", reply_markup=home_kb(), final=True)
        return

    caption = f"🦋 @{author}\n\n{text}".strip() if text else f"🦋 @{author}"

    if len(downloaded) == 1:
        item = downloaded[0]
        mtype = "video" if item["is_video"] else "photo"
        inline_cap = (
            caption[:BALE_CAPTION_LIMIT] if len(caption) <= BALE_CAPTION_LIMIT else ""
        )
        smart_send(
            cid, item["data"], item["fname"], caption=inline_cap, media_type=mtype
        )
    else:
        downloaded[0]["caption"] = (
            caption[:BALE_CAPTION_LIMIT] if len(caption) <= BALE_CAPTION_LIMIT else ""
        )
        send_media_group(cid, downloaded)

    if len(caption) > BALE_CAPTION_LIMIT:
        send_message(cid, caption[:4096])

    _status(cid, "✅ ارسال شد.", reply_markup=home_kb(), final=True)
    clear_state(cid)


def rkey_from_url(url: str) -> str:
    m = re.search(r"/post/([A-Za-z0-9]+)", url)
    return m.group(1) if m else "post"


def handle_message(msg: dict):
    cid = msg["chat"]["id"]
    text = msg.get("text", "")
    _ctx.requester = (msg.get("from") or {}).get("id")
    if getattr(_ctx, "operation", None):
        _ctx.operation["requester"] = _ctx.requester
    photos = msg.get("photo")
    init_user(cid)
    log.info("MSG cid=%d text=%r has_photo=%s", cid, text[:60], bool(photos))

    # Commands
    if text.startswith("/start"):
        clear_state(cid)
        send_message(
            cid,
            "👋 سلام! به *بله قربان* خوش آمدید.\n\n"
            "🔎 جستجو و خواندن وب، دانلود ویدیو و موسیقی، کتاب و مقاله، ترجمه و ابزارهای فایل؛ همه در همین گفتگو.\n\n"
            "*همین حالا بفرستید:*\n"
            "• موضوع یا سؤال برای جستجوی وب؛ مثلاً «آموزش پایتون فارسی»\n"
            "• لینک ویدیو یا پست عمومی برای دانلود\n"
            "• لینک سایت برای مشاهده، یا لینک مستقیم فایل برای دریافت\n\n"
            "برای ترجمه، کار با PDF و سایر ابزارها، ابتدا گزینهٔ مربوط را انتخاب کنید و سپس متن یا فایل را بفرستید. راهنمای بیشتر در «❓ راهنما»:",
            parse_mode="Markdown",
            reply_markup=main_menu_kb(),
        )
        return
    if text.startswith("/help"):
        show_help(cid)
        return
    if text.startswith("/cancel"):
        clear_state(cid)
        _status(cid, "✅ لغو شد.", reply_markup=main_menu_kb(), final=True)
        return
    if text.startswith("/ocr"):
        reply = msg.get("reply_to_message")
        if reply and reply.get("photo"):
            do_ocr_photo(cid, reply["photo"], msg["message_id"])
            return
        set_state(cid, mode="ocr")
        send_message(cid, "🖼 عکس حاوی متن ارسال کنید:", reply_markup=cancel_kb())
        return

    if text.split(" ", 1)[0].split("@")[0] == "/convert":
        info = _conversion_info(msg.get("reply_to_message"))
        if info:
            token = _conversion_register(cid, _ctx.requester, info)
            if token:
                _conversion_menu(cid, _ctx.requester, token)
                return
        set_state(cid, mode="convert")
        send_message(cid, "🔄 عکس، صدا یا ویدیو را بفرستید؛ یا روی پیام فایل با /convert پاسخ دهید.", reply_markup=cancel_kb())
        return
    conversion_upload = _conversion_info(msg)
    if conversion_upload and get_state(cid).get("mode") == "convert":
        token = _conversion_register(cid, _ctx.requester, conversion_upload)
        if token:
            _conversion_menu(cid, _ctx.requester, token)
        else:
            _status(cid, "❌ فایل قابل تبدیل نیست یا بیشتر از ۲۰ مگابایت است.", final=True)
        return

    # Photo
    if photos:
        st = get_state(cid)
        if st.get("mode") == "image_compress":
            photo = sorted(photos, key=lambda p: p.get("file_size", 0))[-1]
            if photo.get("file_size", 0) > MAX_IMAGE_SIZE:
                _status(cid, "❌ حجم عکس بیشتر از حد مجاز است.", final=True)
            else:
                file_url = get_file_url(photo["file_id"])
                file_data = download_bytes(file_url, MAX_IMAGE_SIZE) if file_url else None
                if file_data:
                    do_image_compress(cid, file_data)
                else:
                    _status(cid, "❌ دریافت عکس ناموفق بود؛ دوباره تلاش کنید.", final=True)
            return
        if st.get("mode") == "ocr" or not st.get("mode"):
            _conversion_offer(cid, _ctx.requester, _conversion_info(msg))
            do_ocr_photo(cid, photos, msg["message_id"])
            return

    # Document / file upload
    upload = msg.get("document") or msg.get("video") or msg.get("video_note") or msg.get("audio") or msg.get("voice")
    if upload and isinstance(upload, dict):
        st = get_state(cid)
        if st.get("mode") in ("pdf_extract", "pdf_merge", "video_trim", "video_compress"):
            _handle_file_upload(cid, upload, st.get("mode"))
            return

    doc = msg.get("document")
    if doc and isinstance(doc, dict):
        st = get_state(cid)
        if st.get("mode") == "virusscan":
            # Bale's getFile API only works for files ≤ ~20MB
            file_size = doc.get("file_size", 0)
            BALE_DL_LIMIT = 20 * 1024 * 1024  # 20MB
            VT_LIMIT = 32 * 1024 * 1024  # 32MB

            if file_size > VT_LIMIT:
                send_message(
                    cid,
                    f"⚠️ حجم فایل ({file_size // 1024 // 1024}MB) بیشتر از حد مجاز VirusTotal (32MB) است.",
                    reply_markup=home_kb(),
                )
                clear_state(cid)
                return

            if file_size > BALE_DL_LIMIT:
                send_message(
                    cid,
                    f"⚠️ حجم فایل ({file_size // 1024 // 1024}MB) بیشتر از حد دانلود Bale (20MB) است.\n"
                    "فایل را مستقیم در VirusTotal آپلود کنید: https://www.virustotal.com",
                    reply_markup=home_kb(),
                )
                clear_state(cid)
                return

            file_url = get_file_url(doc["file_id"])
            if not file_url:
                _status(
                    cid, "❌ دریافت لینک فایل ناموفق بود.", reply_markup=home_kb(), final=True)
                return
            file_data = download_bytes(file_url, VT_LIMIT)
            if not file_data:
                _status(cid, "❌ دانلود فایل ناموفق بود.", reply_markup=home_kb(), final=True)
                return
            fname = doc.get("file_name") or f"file_{doc['file_id'][:8]}"
            # Capture platform BEFORE spawning thread — _ctx is thread-local,
            # the new thread would otherwise have no context and send to the wrong API
            captured_platform = _platform()
            operation = getattr(_ctx, "operation", None)
            callback_operation = getattr(_ctx, "callback_operation", False)

            def _scan_thread(
                cid=cid, data=file_data, name=fname, plat=captured_platform
            ):
                _set_platform_ctx(plat, chat_id=cid, operation=operation,
                                  callback_operation=callback_operation)
                do_virusscan(cid, data, name)

            threading.Thread(target=_scan_thread, daemon=True).start()
            return

    if upload and isinstance(upload, dict):
        name = str(upload.get("file_name") or "").lower()
        mime = str(upload.get("mime_type") or "").lower()
        _conversion_offer(cid, _ctx.requester, upload)
        choices = []
        if mime == "application/pdf" or name.endswith(".pdf"):
            choices += [("📄 جداکردن صفحات", "mode_pdf_extract"), ("📚 ادغام PDF", "mode_pdf_merge")]
        elif msg.get("video") or msg.get("video_note") or mime.startswith("video/") or name.endswith((".mp4", ".mkv", ".webm", ".mov", ".avi")):
            choices += [("✂️ برش ویدیو", "mode_video_trim"), ("🎞 فشرده‌سازی ویدیو", "mode_video_compress")]
        choices += [("🛡 بررسی ویروس", "mode_virusscan"), ("🧰 ابزارها", "mode_tools"), ("🏠 منوی اصلی", "home")]
        send_message(cid, "📎 فایل دریافت شد؛ ابزاری را انتخاب کنید و سپس فایل را دوباره بفرستید.",
                     reply_markup={"inline_keyboard": [[{"text": t, "callback_data": d}] for t, d in choices]})
        return

    if not text:
        return

    st = get_state(cid)
    mode = st.get("mode")

    dispatch = {
        "search": lambda: do_search(cid, text),
        "scholar": lambda: do_scholar(cid, text),
        "wiki": lambda: do_wiki_search(cid, text),
        "wiki_article": lambda: do_wiki_article(cid, text, st.get("wiki_lang", "fa")),
        "open": lambda: do_open_url(cid, text),
        "yt_dl": lambda: do_youtube_dl(cid, text),
        "yt_playlist": lambda: do_youtube_playlist(cid, text),
        "yt_clip": lambda: do_youtube_clip(cid, text),
        "yt_search": lambda: do_youtube_search_cmd(cid, text),
        "music": lambda: do_music(cid, text),
        "gh_search": lambda: do_github_search(cid, text),
        "gh_zip": lambda: do_github_zip(cid, text),
        "gh_release": lambda: do_github_release(cid, text),
        "translate": lambda: do_translate(cid, text, st.get("target_lang", "en")),
        "virusscan": lambda: (
            do_virusscan_url(cid, text)
            if text.startswith("http")
            else send_message(
                cid, "🛡 لطفاً یک فایل ارسال کنید یا یک آدرس اینترنتی بنویسید."
            )
        ),
        "currency": lambda: do_currency(cid, text),
        "iplookup": lambda: do_ip_lookup(cid, text),
        "shorten": lambda: do_shorten(cid, text),
        "expand": lambda: do_expand(cid, text),
        "paste": lambda: do_paste(cid, text),
        "qr": lambda: do_qr(cid, text),
        "ocr": lambda: send_message(
            cid, "🖼 لطفاً عکس ارسال کنید.", reply_markup=cancel_kb()
        ),
        "images_query": lambda: handle_message_images_query(cid, text, st),
        "images_source": lambda: send_message(
            cid, "🖼 منبع را از دکمه‌های زیر انتخاب کنید:", reply_markup=image_source_kb()
        ),
        "img_bing": lambda: do_images(cid, st.get("last_query", ""), "bing"),
        "img_pinterest": lambda: do_images(cid, st.get("last_query", ""), "pinterest"),
        "img_pexels": lambda: do_images(cid, st.get("last_query", ""), "pexels"),
        "img_wiki": lambda: do_images(cid, st.get("last_query", ""), "wiki"),
        "img_multi": lambda: do_images(cid, st.get("last_query", ""), "multi"),
        "img_ddg": lambda: do_images(cid, st.get("last_query", ""), "ddg"),
        "img_google": lambda: do_images(cid, st.get("last_query", ""), "google"),
        "tg_read": lambda: do_tg_read(cid, text, False),
        "tg_mtproto": lambda: do_tg_read(cid, text, True),
        "tg_dl": lambda: do_tg_dl_media(cid, text),
        "twitter_tl": lambda: do_twitter_timeline(cid, text),
        "twitter_dl": lambda: do_twitter_dl(cid, text),
        "twitter_dl_translate": lambda: do_twitter_dl_translate(cid, text),
        "ig_profile": lambda: do_instagram_profile(cid, text),
        "ig_dl": lambda: do_instagram_dl(cid, text),
        "ig_story": lambda: do_instagram_story(cid, text),
        "ig_mosaic": lambda: do_instagram_mosaic(cid, text),
        "ig_profile_pic": lambda: do_instagram_profile_pic(cid, text),
        "ig_profile_shot": lambda: do_instagram_profile_screenshot(cid, text),
        "tt_user": lambda: do_tiktok_user(cid, text),
        "tt_dl": lambda: do_tiktok_dl(cid, text),
        "tt_sound": lambda: do_tiktok_sound(cid, text),
        "bluesky": lambda: do_bluesky_dl(cid, text),
        "yt_thumb": lambda: do_youtube_thumbnail(cid, text),
        "yt_stats": lambda: do_youtube_stats(cid, text),
        "rss": lambda: do_rss(cid, text),
        "rss_subscribe": lambda: _rss_subscribe(cid, text),
        "image_compress": lambda: send_message(cid, "🖼 لطفاً عکس را ارسال کنید.", reply_markup=cancel_kb()),
        "pdf_extract": lambda: _handle_pdf_text(cid, text),
        "pdf_merge": lambda: _handle_pdf_merge_text(cid, text),
        "video_trim": lambda: _handle_video_range_text(cid, text),
        "video_compress": lambda: send_message(cid, "🎞 لطفاً ویدیو را ارسال کنید.", reply_markup=cancel_kb()),
        "book": lambda: do_book_search(cid, text, st.get("zlib_ext")),
        "audiobook": lambda: do_audiobook_search(cid, text),
        "ravebook": lambda: do_ravebook_search(cid, text, st.get("ravebook_mode", "ebooks")),
        "book_art": lambda: do_paper_search(cid, text),
        "apk": lambda: do_apk_search(cid, text),
        "movie": lambda: do_movie_search(cid, text),
        "reddit_dl": lambda: do_reddit_dl(cid, text),
        "reddit_profile": lambda: do_reddit_profile(cid, text),
        "twitch_dl": lambda: do_twitch_dl(cid, text),
        "twitch_user": lambda: do_twitch_user(cid, text),
        "vimeo_dl": lambda: do_vimeo_dl(cid, text),
        "pin_dl": lambda: do_pinterest_video_dl(cid, text),
        "hls_dl": lambda: do_hls_dl(cid, text),
    }

    if mode and mode in dispatch:
        dispatch[mode]()
        return

    if text.startswith("http"):
        # Smart URL detection
        if _is_youtube(text):
            do_youtube_dl(cid, text)
        elif "github.com" in text:
            # Repo link → same action menu as after search-result selection.
            # Anchored: exactly owner/repo (optionally .git / trailing slash) at
            # string end — blob/raw/tree/issues etc. fall through to file link.
            m = re.match(
                r"https?://github\.com/([^/]+/[^/]+?)(?:\.git)?/?$", text.rstrip("/")
            )
            if m:
                repo_full = m.group(1)
                info = github_repo_info(repo_full)
                if info:
                    desc = (info.get("description") or "")[:300]
                    txt = (
                        f"🐙 *{info.get('full_name', repo_full)}*\n"
                        f"⭐ {info.get('stargazers_count', 0)} | "
                        f"🍴 {info.get('forks_count', 0)} | "
                        f"📝 {info.get('language', '—')}\n\n"
                        f"{desc}\n\n"
                        f"🔗 {info.get('html_url', text)}"
                    )
                    send_message(
                        cid,
                        txt,
                        parse_mode="Markdown",
                        reply_markup=gh_repo_action_kb(info.get("full_name", repo_full)),
                    )
                    clear_state(cid)
                else:
                    do_github_zip(cid, text)
            else:
                # github.com blob/raw/tree link → treat as file
                do_file_link(cid, text)
        elif _is_direct_file_url(text):
            do_file_link(cid, text)
        elif "spotify.com" in text:
            do_spotify_dl(cid, text)
        elif "soundcloud.com" in text or "on.soundcloud.com" in text:
            do_soundcloud_info(cid, text)
        elif "tiktok.com" in text:
            if "/music/" in text or "/sound/" in text:
                do_tiktok_sound(cid, text)
            else:
                do_tiktok_dl(cid, text)
        elif "twitter.com" in text or "x.com" in text:
            if st.get("mode") == "twitter_thread" or "/threads/" in text:
                do_twitter_thread(cid, text)
            else:
                do_twitter_dl(cid, text)
        elif "instagram.com" in text:
            if "/stories/" in text:
                do_instagram_story(cid, text.split("/stories/")[-1].strip("/"))
            else:
                do_instagram_dl(cid, text)
        elif "bsky.app" in text:
            do_bluesky_dl(cid, text)
        elif "t.me/" in text:
            do_tg_dl_media(cid, text)
        elif "reddit.com" in text:
            do_reddit_dl(cid, text)
        elif "twitch.tv" in text or "clips.twitch.tv" in text:
            do_twitch_dl(cid, text)
        elif "vimeo.com" in text:
            do_vimeo_dl(cid, text)
        elif "pinterest.com" in text and ("/pin/" in text or "video" in text.lower()):
            do_pinterest_video_dl(cid, text)
        elif ".m3u8" in text or ".m3u" in text:
            do_hls_dl(cid, text)
        else:
            do_open_url(cid, text)
    else:
        do_search(cid, text)


# ═══════════════════════════════════════════════════════════════════════════════
# CALLBACK HANDLER
# ═══════════════════════════════════════════════════════════════════════════════
def handle_callback(cb: dict):
    msg = cb.get("message") or {}
    cid = (msg.get("chat") or {}).get("id")

    # Fallback: extract chat_id from callback query's 'from' field if message.chat is missing
    # This handles cases where the callback comes from an inline query or edited message
    if not cid:
        from_user = cb.get("from") or {}
        cid = from_user.get("id")

    # Additional fallback: try to get chat_id from callback data prefix if stored
    # Some platforms may not include full message object in callbacks
    if not cid:
        log.warning("handle_callback: no chat id in callback, cb keys: %s", list(cb.keys()))
        # Try to find chat_id in nested structures
        for key in ["message", "inline_message_id", "chat_instance"]:
            if key in cb:
                val = cb[key]
                if isinstance(val, dict) and "chat" in val:
                    cid = (val.get("chat") or {}).get("id")
                    if cid:
                        break

    # Critical fallback for SoundCloud and other downloads: check if we have cached URL with chat_id
    if not cid:
        # Try to extract from callback data pattern - some callbacks store chat info
        m = re.match(r"(sc_dl|yt_dl|bk_dl)_\w+", data if (data := cb.get("data", "")) else "")
        if m:
            log.error("handle_callback: CRITICAL - cannot determine chat_id for %s, skipping. Full cb: %r", m.group(0), cb)
        else:
            log.error("handle_callback: CRITICAL - cannot determine chat_id, skipping callback. Full cb: %r", cb)
        return

    if not cid:
        log.error("handle_callback: CRITICAL - cannot determine chat_id, skipping callback. Full cb: %r", cb)
        return
    data = cb.get("data", "")
    _ctx.requester = (cb.get("from") or {}).get("id")
    answer_cb(cb["id"])
    init_user(cid)
    if msg.get("message_id"):
        _operation_start(cid, "⏳ در حال پردازش…", message_id=msg["message_id"])
        _ctx.callback_operation = True
    if getattr(_ctx, "operation", None):
        _ctx.operation["requester"] = _ctx.requester
    log.info("CB cid=%d data=%r", cid, data)
    if data.startswith("cv_menu:"):
        _conversion_menu(cid, _ctx.requester, data.split(":", 1)[1])
        return
    if data.startswith("cv_do:"):
        parts = data.split(":")
        if len(parts) == 3:
            _conversion_start(cid, _ctx.requester, parts[1], parts[2])
        return
    if data == "mode_convert":
        clear_state(cid)
        set_state(cid, mode="convert")
        send_message(cid, "🔄 فایل عکس، صدا یا ویدیو را بفرستید؛ یا روی فایل با /convert پاسخ دهید.", reply_markup=cancel_kb())
        return

    st = get_state(cid)

    # ── Home / Cancel ─────────────────────────────────────────────────────
    if data in ("home", "cancel"):
        clear_state(cid)
        send_message(cid, "🏠 منوی اصلی:", reply_markup=main_menu_kb())
        return

    if data.startswith("mode_") or data in ("nav_read", "nav_download", "library", "rss_subscriptions"):
        clear_state(cid)
        st = get_state(cid)
    if data in ("nav_read", "nav_download"):
        send_message(cid, "🔎 جستجو و خواندن:" if data == "nav_read" else "📥 دانلود و شبکه‌ها:", reply_markup=category_kb(data))
        return

    # ── Post-download actions ──────────────────────────────────────────────────
    if data == "redownload_quality":
        _status(cid, "🔁 برای انتخاب کیفیت، لینک منبع را دوباره بفرستید.", reply_markup=home_kb(), final=True)
        return

    if data == "delete_this_msg":
        # Delete this message (the one with the buttons)
        try:
            api("deleteMessage", chat_id=cid, message_id=msg["message_id"])
        except Exception as e:
            log.warning("delete_this_msg: %s", e)
        return

    if data == "mode_social":
        send_message(cid, "📲 شبکه‌های اجتماعی:", reply_markup=social_menu_kb())
        return

    if data == "library":
        send_message(cid, "⭐️ کتابخانه من — اطلاعات لینک‌ها فقط ذخیره می‌شود:", reply_markup=library_kb())
        return

    if data == "library_saved":
        _library_list(cid)
        return

    if data == "library_watchlist":
        _library_list(cid, watchlist_only=True)
        return

    m = re.match(r"lib_page_watch_(\d+)$", data)
    if m:
        _library_list(cid, watchlist_only=True, page=int(m.group(1)))
        return

    m = re.match(r"lib_page_saved_(\d+)$", data)
    if m:
        _library_list(cid, page=int(m.group(1)))
        return

    if data == "library_history":
        _library_history(cid)
        return

    m = re.match(r"lib_item_(\d+)$", data)
    if m:
        _library_item(cid, int(m.group(1)))
        return

    m = re.match(r"lib_watch_(\d+)$", data)
    if m:
        item = durable_store.toggle_watchlist(_platform(), cid, int(m.group(1)))
        if not item:
            _status(cid, "❌ این مورد در کتابخانه نیست.", final=True)
        else:
            _status(cid, "✅ به فهرست تماشا اضافه شد." if item.get("watchlist") else "✅ از فهرست تماشا حذف شد.", final=True)
        return

    m = re.match(r"lib_retry_(\d+)$", data)
    if m:
        item = durable_store.get_library_item(_platform(), cid, int(m.group(1)))
        if not item:
            _status(cid, "❌ این مورد دیگر در کتابخانه نیست.", final=True)
        else:
            _retry_url(cid, item.get("url", ""))
        return

    m = re.match(r"hist_page_(\d+)$", data)
    if m:
        _library_history(cid, page=int(m.group(1)))
        return

    m = re.match(r"hist_item_(\d+)$", data)
    if m:
        _history_item(cid, int(m.group(1)))
        return

    m = re.match(r"hist_retry_(\d+)$", data)
    if m:
        entry = durable_store.get_usage_entry(_platform(), cid, int(m.group(1)))
        if not entry:
            _status(cid, "❌ این تاریخچه منقضی شده است.", final=True)
        else:
            _retry_url(cid, str((entry.get("details") or {}).get("url") or ""))
        return

    m = re.match(r"lib_save_([a-z0-9]+)_(\w+)$", data)
    if m:
        _save_library_url(cid, m.group(1), m.group(2))
        return

    if data == "mode_tools":
        send_message(cid, "🧰 ابزارهای رایگان:", reply_markup=tools_kb())
        return

    if data == "mode_translate_fa":
        set_state(cid, mode="translate", target_lang="fa")
        send_message(cid, "🌐 متن را برای ترجمه به فارسی بفرستید:", reply_markup=cancel_kb())
        return

    if data == "mode_currency":
        set_state(cid, mode="currency")
        send_message(cid, "💱 نمونه: `100 USD to IRR`", reply_markup=cancel_kb())
        return

    if data == "mode_shorten":
        set_state(cid, mode="shorten")
        send_message(cid, "🔗 لینکی را که می‌خواهید کوتاه کنید بفرستید:", reply_markup=cancel_kb())
        return

    if data == "mode_expand":
        set_state(cid, mode="expand")
        send_message(cid, "🔎 لینک کوتاه یا ریدایرکت‌دار را بفرستید:", reply_markup=cancel_kb())
        return

    if data == "mode_paste":
        set_state(cid, mode="paste")
        send_message(cid, "📋 متنی را که می‌خواهید به‌صورت لینک اشتراکی بسازید بفرستید:", reply_markup=cancel_kb())
        return

    if data == "mode_qr":
        set_state(cid, mode="qr")
        send_message(cid, "🔳 متن یا لینکی را برای ساخت QR بفرستید:", reply_markup=cancel_kb())
        return

    if data == "mode_image_compress":
        set_state(cid, mode="image_compress")
        send_message(cid, "🖼 عکس را بفرستید؛ فایل فشرده بدون ذخیره‌سازی دائمی ارسال می‌شود:", reply_markup=cancel_kb())
        return

    if data == "mode_pdf_extract":
        set_state(cid, mode="pdf_extract")
        send_message(cid, "📄 فایل PDF را بفرستید؛ بعد شمارهٔ صفحه‌ها را مثل `1,3-5` وارد کنید:", reply_markup=cancel_kb())
        return

    if data == "mode_pdf_merge":
        set_state(cid, mode="pdf_merge")
        send_message(cid, "📚 فایل‌های PDF را یکی‌یکی بفرستید؛ حداکثر ۵ فایل ادغام می‌شود.", reply_markup=cancel_kb())
        return

    if data == "mode_video_trim":
        set_state(cid, mode="video_trim")
        send_message(cid, "✂️ ویدیو را بفرستید؛ بعد بازهٔ زمانی را مثل `5 30` (شروع و پایان به ثانیه) بفرستید:", reply_markup=cancel_kb())
        return

    if data == "mode_video_compress":
        set_state(cid, mode="video_compress")
        send_message(cid, "🎞 ویدیو را برای فشرده‌سازی محلی بفرستید؛ حداکثر ۴۸ مگابایت:", reply_markup=cancel_kb())
        return

    if data == "help" or data.startswith("help_"):
        show_help(cid, data[5:] if data.startswith("help_") else None)
        return

    if data == "privacy":
        send_message(cid, PRIVACY_TEXT, parse_mode="Markdown", reply_markup=home_kb())
        return

    if data == "stats":
        s = durable_store.get_user_stats(_platform(), cid)
        txt = (
            f"📊 *آمار کاربری*\n\n"
            f"🗓 عضویت: {s.get('joined', '—')}\n"
            f"📈 مجموع: {s.get('requests', 0)}\n"
            f"🔎 جستجو: {s.get('searches', 0)}\n"
            f"📥 دانلود: {s.get('downloads', 0)}\n"
            f"🌐 ترجمه: {s.get('translations', 0)}\n"
            f"🖼 OCR: {s.get('ocr', 0)}"
        )
        send_message(cid, txt, parse_mode="Markdown", reply_markup=home_kb())
        return

    # ── Audio format selection ────────────────────────────────────────────────────
    m = re.match(r"audio_fmt_([a-z0-9]+)_(\d+)$", data)
    if m:
        fmt = m.group(1)
        quality = m.group(2)
        st = get_state(cid)
        url = st.get("last_url", "")
        if not url:
            _status(cid, "❌ لینک منقضی شده. دوباره امتحان کنید.", reply_markup=home_kb(), final=True)
            return
        _do_audio_download(
            cid,
            url,
            source="auto",
            audio_format=fmt,
            audio_quality=quality,
            title=st.get("last_title", ""),
            artist=st.get("last_artist", ""),
            track_item=st.get("last_track_item", None),
        )
        return

    # ── Mode launchers ────────────────────────────────────────────────────
    mode_prompts = {
        "mode_search": ("search", "🔎 کلمه یا عبارت جستجو را بنویسید:"),
        "mode_open": ("open", "🌐 آدرس سایت را وارد کنید (https://…):"),
        "mode_scholar": ("scholar", "📚 عنوان یا کلمه‌کلیدی مقاله:"),
        "mode_wiki": ("wiki", "📖 موضوع ویکی‌پدیا:"),
        "mode_music": (
            "music",
            "🎵 نام آهنگ یا خواننده را بنویسید\nیا لینک Spotify / SoundCloud / YouTube را بفرستید:",
        ),
        "mode_apk": (
            "apk",
            "📱 نام اپ یا شناسه پکیج را وارد کنید (مثال: org.telegram.messenger):",
        ),
        "mode_movie": ("movie", "🎬 نام فیلم یا سریال را بنویسید:"),
        "mode_iplookup": ("iplookup", "🌐 آدرس IP یا دامنه:"),
        "rss_subscribe": ("rss_subscribe", "📰 آدرس فید RSS برای اشتراک را وارد کنید:"),
        "mode_virusscan": (
            "virusscan",
            "🛡 فایل خود را ارسال کنید تا بررسی شود\nیا یک آدرس اینترنتی بفرستید تا اسکن شود:",
        ),
        "mode_bluesky": (
            "bluesky",
            "🦋 لینک پست Bluesky را بفرستید:\nمثال: https://bsky.app/profile/user.bsky.social/post/123",
        ),
        "ig_profile_pic": (
            "ig_profile_pic",
            "📸 نام کاربری اینستاگرام را وارد کنید (بدون @):",
        ),
        "ig_profile_shot": (
            "ig_profile_shot",
            "📸 نام کاربری اینستاگرام را وارد کنید (بدون @):",
        ),
        "ig_mosaic": (
            "ig_mosaic",
            "🔲 لینک پست اینستاگرام را برای ساخت موزاییک بفرستید:",
        ),
    }
    if data in mode_prompts:
        mode, prompt = mode_prompts[data]
        set_state(cid, mode=mode)
        send_message(cid, prompt, reply_markup=cancel_kb())
        return

    if data == "mode_translate":
        set_state(cid, mode="translate_lang")
        send_message(cid, "🌐 زبان مقصد را انتخاب کنید:", reply_markup=translate_kb())
        return

    if data.startswith("trlang_"):
        lang = data.split("_", 1)[1]
        set_state(cid, mode="translate", target_lang=lang)
        _status(
            cid, f"✅ زبان انتخاب شد. متن را بفرستید:", reply_markup=cancel_kb(), final=True)
        return

    if data == "mode_ocr":
        set_state(cid, mode="ocr")
        send_message(cid, "🖼 عکس حاوی متن ارسال کنید:", reply_markup=cancel_kb())
        return

    # ── YouTube ───────────────────────────────────────────────────────────
    if data == "mode_youtube":
        send_message(cid, "📺 یوتیوب:", reply_markup=youtube_action_kb())
        return
    if data == "yt_video":
        set_state(cid, mode="yt_dl")
        send_message(cid, "📥 لینک ویدیو یوتیوب:", reply_markup=cancel_kb())
        return
    if data == "yt_search":
        set_state(cid, mode="yt_search")
        send_message(cid, "🔍 کلمه جستجو:", reply_markup=cancel_kb())
        return
    if data == "yt_clip":
        set_state(cid, mode="yt_clip")
        send_message(cid, "✂️ لینک یوتیوب و بازه را در یک خط بفرستید.\nمثال: `https://youtu.be/... 01:20-03:45`\nحداکثر بازه: ۳۰ دقیقه.", parse_mode="Markdown", reply_markup=cancel_kb())
        return
    if data == "yt_playlist":
        set_state(cid, mode="yt_playlist")
        send_message(cid, "📃 لینک فهرست پخش یوتیوب را بفرستید.\n۱۰ ویدیوی اول نمایش داده می‌شود.", reply_markup=cancel_kb())
        return

    # YouTube result click → show info card with download button
    m = re.match(r"yt_res_(\w+)_(\d+)$", data)
    if m:
        key, idx = m.group(1), int(m.group(2))
        results = cache_get(key)
        if not results or idx >= len(results):
            _status(cid, "❌ نتیجه منقضی شده. دوباره جستجو کنید.", final=True)
            return
        vid = results[idx]

        # Build info card text
        title = vid.get("title", "")
        uploader = vid.get("uploader", "")
        duration = vid.get("duration", "")
        views = vid.get("view_count", "")
        likes = vid.get("like_count", "")
        desc = vid.get("description", "")
        url = vid.get("url", "")

        def _fmt_num(n):
            try:
                if not n or str(n).strip().upper() in ("NA", "NONE", "", "0"):
                    return ""
                n = int(str(n).replace(",", ""))
                if n >= 1_000_000:
                    return f"{n / 1_000_000:.1f}M"
                if n >= 1_000:
                    return f"{n / 1_000:.1f}K"
                return str(n)
            except Exception:
                return ""

        lines = [f"📺 *{title}*"]
        if uploader:
            lines.append(f"🎬 {uploader}")
        if duration:
            lines.append(f"⏱ {duration}")
        views_fmt = _fmt_num(views)
        likes_fmt = _fmt_num(likes)
        if views_fmt:
            lines.append(f"👁 {views_fmt} بازدید")
        if likes_fmt:
            lines.append(f"👍 {likes_fmt} لایک")
        if desc:
            lines.append(f"\n_{desc[:200]}_")

        info_text = "\n".join(lines)

        # Store selected video for download
        dl_key = "ytdl_" + hashlib.md5(url.encode()).hexdigest()[:8]
        cache_set(dl_key, [vid])

        kb = {
            "inline_keyboard": [
                [
                    {
                        "text": "📥 انتخاب کیفیت ویدیو",
                        "callback_data": f"yt_do_dl_{dl_key}",
                    },
                    {"text": "🎵 دانلود صدا", "callback_data": f"yt_do_audio_{dl_key}"},
                ],
                [
                    {"text": "🔙 برگشت", "callback_data": f"yt_back_{key}"},
                    {"text": "🏠 منوی اصلی", "callback_data": "home"},
                ],
            ]
        }

        # Send thumbnail + info card
        thumb_url = (
            vid.get("thumbnail")
            or f"https://i.ytimg.com/vi/{vid.get('id', '')}/hqdefault.jpg"
        )
        thumb = download_bytes(thumb_url, MAX_IMAGE_SIZE)
        if thumb:
            send_photo(cid, thumb, caption=info_text[:1000], reply_markup=kb)
        else:
            send_message(cid, info_text, parse_mode="Markdown", reply_markup=kb)
        return

    # YouTube download from info card → quality picker
    m = re.match(r"yt_do_(dl|audio)_(\w+)$", data)
    if m:
        dl_type, dl_key = m.group(1), m.group(2)
        vids = cache_get(dl_key)
        if not vids:
            _status(cid, "❌ نتیجه منقضی شده. دوباره جستجو کنید.", final=True)
            return
        vid = vids[0]
        audio_only = dl_type == "audio"
        url = vid.get("url", "")
        if audio_only:
            # Audio: skip quality picker, go straight to download
            url_key = store_url(url, chat_id=cid)
            info_key = "yti" + url_key
            cache_set(
                info_key,
                [
                    {
                        "title": vid.get("title", ""),
                        "subtitles": [],
                        "formats": [],
                        "thumbnail": vid.get("thumbnail", ""),
                    }
                ],
            )
            _youtube_execute_download(
                cid,
                url_key,
                info_key,
                fmt_spec="",
                client="",
                audio_only=True,
                sub_code="",
            )
        else:
            _youtube_quality_picker(cid, url, audio_only=False)
        return

    # ── YouTube playlist shared preferences ──────────────────────────────────
    m = re.match(r"yt_plq_(\w+)_(a|360|480|720)$", data)
    if m:
        key, choice = m.groups()
        if not cache_get(key):
            _status(cid, "❌ فهرست پخش منقضی شده. دوباره امتحان کنید.", final=True)
            return
        send_message(cid, "💬 زیرنویس برای همه ویدیوها:", reply_markup=_playlist_subtitle_kb(key, choice == "a"))
        return

    m = re.match(r"yt_pls_(\w+)_(a|360|480|720)_(fa|en|none)$", data)
    if m:
        key, choice, sub_code = m.groups()
        items = cache_get(key)
        if not items:
            _status(cid, "❌ فهرست پخش منقضی شده. دوباره امتحان کنید.", final=True)
            return
        _run_youtube_playlist(cid, items, 720 if choice == "a" else int(choice), choice == "a", "" if sub_code == "none" else sub_code)
        return

    # ── YouTube quality selection ─────────────────────────────────────────────
    # yt_qual_{url_key}_{info_key}_{fmt_idx}
    # url_key = 10-char hex, info_key = "yti" + 10-char hex — no internal underscores
    m = re.match(r"yt_qual_([0-9a-f]+)_(yti[0-9a-f]+)_(\d+)$", data)
    if m:
        url_key, info_key, fmt_idx = m.group(1), m.group(2), int(m.group(3))
        info_list = cache_get(info_key)
        if not info_list:
            _status(cid, "❌ اطلاعات منقضی. دوباره امتحان کنید.", final=True)
            return
        info = info_list[0]
        formats = info.get("formats", [])
        if fmt_idx >= len(formats):
            _status(cid, "❌ کیفیت انتخابی موجود نیست.", final=True)
            return
        chosen = formats[fmt_idx]
        fmt_spec = chosen["fmt_spec"]
        client = chosen.get("client", "tv_embedded")
        _youtube_sub_picker(cid, url_key, info_key, fmt_spec, client, audio_only=False)
        return

    # yt_qualA_{url_key}_{info_key}  (audio-only)
    m = re.match(r"yt_qualA_([0-9a-f]+)_(yti[0-9a-f]+)$", data)
    if m:
        url_key, info_key = m.group(1), m.group(2)
        _youtube_execute_download(
            cid, url_key, info_key, fmt_spec="", client="", audio_only=True, sub_code=""
        )
        return

    # ── YouTube subtitle selection ────────────────────────────────────────────
    # yt_sub_{url_key}_{info_key}_{sub_code}_{audio_only}_{fmt_spec_encoded}_{client_encoded}
    # client is encoded: tv_embedded -> tv~embedded (underscores replaced with ~~ )
    m = re.match(
        r"yt_sub_([0-9a-f]+)_(yti[0-9a-f]+)_([^_]+)_([01])_([^_]*)_(.*)$", data
    )
    if m:
        url_key = m.group(1)
        info_key = m.group(2)
        sub_code = m.group(3)  # "NONE" or language code
        audio_only = m.group(4) == "1"
        fmt_spec = _decode_fmt(m.group(5))
        client = m.group(6).replace("~~", "_")  # restore underscores in client
        _youtube_execute_download(
            cid,
            url_key,
            info_key,
            fmt_spec,
            client,
            audio_only,
            "" if sub_code == "NONE" else sub_code,
        )
        return

    # ── YouTube subtitle page navigation ──────────────────────────────────────
    m = re.match(
        r"yt_subpg_([0-9a-f]+)_(yti[0-9a-f]+)_(\d+)_([01])_([^_]*)_(.*)$", data
    )
    if m:
        url_key = m.group(1)
        info_key = m.group(2)
        page = int(m.group(3))
        audio_only = m.group(4) == "1"
        fmt_spec = _decode_fmt(m.group(5))
        client = m.group(6).replace("~~", "_")
        _youtube_sub_picker(cid, url_key, info_key, fmt_spec, client, audio_only, page)
        return

    # YouTube back to search results
    m = re.match(r"yt_back_(\w+)$", data)
    if m:
        key = m.group(1)
        results = cache_get(key)
        if results:
            page = st.get("page", 0)
            text = f"📺 *یوتیوب — نتایج جستجو:*\nروی ویدیو کلیک کنید:"
            kb = yt_results_kb(results, key, page, len(results) == 8)
            send_message(cid, text, parse_mode="Markdown", reply_markup=kb)
        return

    m = re.match(r"yt_page_(\d+)$", data)
    if m:
        page = int(m.group(1))
        query = st.get("last_query", "")
        if query:
            do_youtube_search_cmd(cid, query, page)
        return

    # ── Direct file link actions ─────────────────────────────────────────
    m = re.match(r"dfile_dl_(\w+)$", data)
    if m:
        url = get_url_for_chat(m.group(1), cid)
        if not url:
            _status(cid, "❌ لینک منقضی شده.", final=True)
            return
        do_direct_file_download(cid, url)
        return

    m = re.match(r"dfile_shot_(\w+)$", data)
    if m:
        url = get_url_for_chat(m.group(1), cid)
        if not url:
            _status(cid, "❌ لینک منقضی شده.", final=True)
            return
        do_open_url(cid, url)
        return

    # ── GitHub ────────────────────────────────────────────────────────────
    if data == "mode_github":
        send_message(cid, "🐙 GitHub:", reply_markup=github_action_kb())
        return
    if data == "gh_search":
        set_state(cid, mode="gh_search")
        send_message(cid, "🔍 نام مخزن یا کلمه کلیدی:", reply_markup=cancel_kb())
        return
    if data == "gh_zip":
        set_state(cid, mode="gh_zip")
        send_message(cid, "📥 لینک مخزن GitHub:", reply_markup=cancel_kb())
        return
    if data == "gh_release":
        set_state(cid, mode="gh_release")
        send_message(
            cid, "📦 نام کامل مخزن (مثال: microsoft/vscode):", reply_markup=cancel_kb()
        )
        return

    # GitHub repo result click
    m = re.match(r"gh_res_(\w+)_(\d+)$", data)
    if m:
        key, idx = m.group(1), int(m.group(2))
        results = cache_get(key)
        if not results or idx >= len(results):
            _status(cid, "❌ نتیجه منقضی شده.", final=True)
            return
        repo = results[idx]
        full = repo.get("full_name", "")
        desc = repo.get("description") or ""
        txt = (
            f"🐙 *{full}*\n"
            f"⭐ {repo.get('stargazers_count', 0)} | "
            f"🍴 {repo.get('forks_count', 0)} | "
            f"📝 {repo.get('language', '—')}\n\n"
            f"{desc[:300]}\n\n"
            f"🔗 {repo.get('html_url', '')}"
        )
        send_message(
            cid, txt, parse_mode="Markdown", reply_markup=gh_repo_action_kb(full)
        )
        return

    m = re.match(r"gh_page_(\d+)$", data)
    if m:
        page = int(m.group(1))
        query = st.get("last_query", "")
        if query:
            do_github_search(cid, query, page)
        return

    # GitHub repo actions
    m = re.match(r"ghact_zip_(.+)$", data)
    if m:
        repo = m.group(1).replace("__", "/")
        do_github_zip(cid, f"https://github.com/{repo}")
        return

    m = re.match(r"ghact_rel_(.+)$", data)
    if m:
        repo = m.group(1).replace("__", "/")
        do_github_release(cid, repo)
        return

    m = re.match(r"ghact_info_(.+)$", data)
    if m:
        repo = m.group(1).replace("__", "/")
        info = github_repo_info(repo)
        if info:
            txt = (
                f"📋 *{repo}*\n"
                f"⭐ Stars: {info.get('stargazers_count', 0)}\n"
                f"🍴 Forks: {info.get('forks_count', 0)}\n"
                f"👀 Watchers: {info.get('watchers_count', 0)}\n"
                f"📝 Language: {info.get('language', '—')}\n"
                f"📅 Created: {info.get('created_at', '')[:10]}\n"
                f"🔄 Updated: {info.get('updated_at', '')[:10]}\n"
                f"📄 License: {(info.get('license') or {}).get('name', '—')}\n"
                f"🌐 {info.get('homepage') or info.get('html_url', '')}"
            )
            send_message(cid, txt, parse_mode="Markdown", reply_markup=home_kb())
        else:
            _status(cid, "❌ اطلاعاتی یافت نشد.", reply_markup=home_kb(), final=True)
        return

    # GitHub ZIP-mode repo link → show the same action menu as link detection
    m = re.match(r"ghact_menu_(.+)$", data)
    if m:
        repo = m.group(1).replace("__", "/")
        send_message(
            cid,
            f"🐙 *{repo}*\nیک عملیات انتخاب کنید:",
            parse_mode="Markdown",
            reply_markup=gh_repo_action_kb(repo),
        )
        return

    # GitHub: back to results (link-opened menus have no results page → home)
    if data == "gh_back":
        send_message(cid, "🏠 منوی اصلی:", reply_markup=main_menu_kb())
        return

    # GitHub release asset download
    m = re.match(r"ghrel_dl_(.+)_(\d+)$", data)
    if m:
        repo = m.group(1).replace("__", "/")
        idx = int(m.group(2))
        akey = f"ghrel_{m.group(1)}"
        assets = cache_get(akey)
        if not assets or idx >= len(assets):
            _status(cid, "❌ نتیجه منقضی.", final=True)
            return
        asset = assets[idx]
        url = asset.get("browser_download_url", "")
        size_mb = asset.get("size", 0) / 1024 / 1024
        _status(cid, f"⏳ دانلود: {asset['name']} ({size_mb:.1f}MB)…")
        chat_action(cid, "upload_document")
        d = download_bytes(url, 500 * 1024 * 1024)  # allow up to 500MB download
        if d:
            ok = smart_send(cid, d, asset["name"], caption=f"📦 {repo}")
            _status(cid, "✅ ارسال شد." if ok else "❌ ارسال فایل ناموفق بود.", reply_markup=home_kb(), final=True)
        else:
            _status(cid, "❌ دانلود ناموفق.", reply_markup=home_kb(), final=True)
        return

    # GitHub release page navigation
    m = re.match(r"ghrel_pg_(.+)_(\d+)$", data)
    if m:
        repo = m.group(1).replace("__", "/")
        page = int(m.group(2))
        do_github_release(cid, repo, page=page)
        return

    # ── Images ────────────────────────────────────────────────────────────
    if data == "mode_images":
        set_state(cid, mode="images_query")
        send_message(
            cid, "🖼 کلمه کلیدی عکس مورد نظر را بنویسید:", reply_markup=cancel_kb()
        )
        return

    img_src_map = {
        "img_src_multi": "multi",
        "img_src_ddg": "ddg",
        "img_src_bing": "bing",
        "img_src_google": "google",
        "img_src_pinterest": "pinterest",
        "img_src_pexels": "pexels",
        "img_src_wiki": "wiki",
        "img_src_deviantart": "deviantart",
        "img_src_pixiv": "pixiv",
        "img_src_artstation": "artstation",
        "img_src_flickr": "flickr",
        "img_src_tumblr": "tumblr",
        "img_src_imgur": "imgur",
        "img_src_kemono": "kemono",
        "img_src_newgrounds": "newgrounds",
    }
    if data in img_src_map:
        src = img_src_map[data]
        query = st.get("last_query", "")
        if not query:
            _status(
                cid, "❌ ابتدا کلمه کلیدی وارد کنید.", reply_markup=cancel_kb(), final=True)
            return
        set_state(cid, mode=f"img_{src}", last_query=query, img_source=src, page=0)
        do_images(cid, query, src, 0)
        return

    # "دانلود بیشتر" button
    m = re.match(r"img_more_(\w+)_(\w+)_(\d+)$", data)
    if m:
        source, key, page = m.group(1), m.group(2), int(m.group(3))
        query = st.get("last_query", "")
        do_images(cid, query, source, page)
        return

    # book_iadl_{url_key} — resolve an Archive.org identifier, then download
    m = re.match(r"book_iadl_(\w+)$", data)
    if m:
        url_key = m.group(1)
        ident = get_url_for_chat(url_key, cid) or ""
        target_cid = cid
        if not ident:
            _status(target_cid, "❌ لینک منقضی شده.", reply_markup=home_kb(), final=True)
            return
        _status(target_cid, "⏳ در حال آماده‌سازی فایل از Internet Archive…")
        chat_action(target_cid, "upload_document")
        title = "book"
        try:
            st = get_state(target_cid)
            cached = cache_get(st.get("cache_key", ""))
            if isinstance(cached, list):
                for b in cached:
                    if str(b.get("ia_url", "")).rstrip("/").split("/")[-1] == ident:
                        title = b.get("name", "book")
                        break
        except Exception:
            pass

        resolved = _archive_org_best_file(ident)
        if not resolved:
            _status(
                target_cid,
                "❔ این عنوان در Internet Archive فقط امانتی است و فایل رایگان ندارد.\n"
                "می‌توانید از سایت خودش امانت بگیرید.",
                reply_markup={"inline_keyboard": [
                    [{"text": "🌐 باز کردن Internet Archive",
                      "url": f"https://archive.org/details/{ident}"}],
                    [{"text": "🏠 منوی اصلی", "callback_data": "home"}],
                ]},
                final=True,
            )
            return

        dl_url, fname = resolved
        data_bytes = download_bytes(dl_url, TELEGRAM_MAX_FILE_SIZE)
        if not data_bytes or len(data_bytes) < 2000:
            _status(
                target_cid,
                "❌ دانلود ناموفق بود. دوباره امتحان کنید.",
                reply_markup=home_kb(), final=True)
            return
        if smart_send(target_cid, data_bytes, fname, caption=f"📚 {fname[:80]}"):
            _status(target_cid, "✅ کتاب ارسال شد.", reply_markup=home_kb(), final=True)
        else:
            _status(target_cid, "❌ ارسال فایل ناموفق بود.", reply_markup=home_kb(), final=True)
        clear_state(target_cid)
        return

    # book_ol_{url_key} — show Open Library link
    m = re.match(r"book_ol_(\w+)$", data)
    if m:
        url_key = m.group(1)
        ol_url = get_url_for_chat(url_key, cid) or ""
        target_cid = cid
        if ol_url:
            send_message(target_cid, f"🌐 لینک Open Library:\n{ol_url}", reply_markup=home_kb())
        else:
            _status(target_cid, "❌ لینک منقضی شده.", reply_markup=home_kb(), final=True)
        return

    # paper_item_{cache_key}_{idx} — user clicked a paper in results
    m = re.match(r"paper_item_(\w+)_(\d+)$", data)
    if m:
        key, idx = m.group(1), int(m.group(2))
        results = cache_get(key)
        if not results or idx >= len(results):
            _status(cid, "❌ نتیجه منقضی شده. دوباره جستجو کنید.", final=True)
            return
        paper = results[idx]
        title = paper.get("title", "")
        authors = ", ".join(paper.get("authors", []))
        year = paper.get("year", "")
        doi = paper.get("doi", "")
        oa_url = paper.get("oa_url", "")

        lines = [f"📄 *{title}*"]
        if authors:
            lines.append(f"✍️ {authors}")
        if year:
            lines.append(f"📅 {year}")
        if doi:
            lines.append(f"🔗 DOI: `{doi}`")

        rows = []
        if paper.get("sources"):
            lines.append("📚 منابع: " + "، ".join(paper["sources"]))
        paper_key = make_cache_key("paper", doi or title, 0)
        cache_set(paper_key, [paper])
        rows.append([
            {"text": "📚 استناد APA", "callback_data": f"paper_apa_{paper_key}"},
            {"text": "📄 BibTeX", "callback_data": f"paper_bib_{paper_key}"},
        ])
        if doi:
            doi_key = store_url(doi, chat_id=cid)
            rows.append(
                [
                    {
                        "text": "📥 دریافت مقاله",
                        "callback_data": f"paper_dl_{doi_key}",
                    }
                ]
            )
        if oa_url:
            oa_key = store_url(oa_url, chat_id=cid)
            rows.append(
                [
                    {
                        "text": "🔓 دانلود Open Access",
                        "callback_data": f"paper_oa_{oa_key}",
                    }
                ]
            )
        rows.append(
            [
                {"text": "🔙 برگشت", "callback_data": "book_back"},
                {"text": "🏠 خانه", "callback_data": "home"},
            ]
        )
        send_message(
            cid,
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup={"inline_keyboard": rows},
        )
        return

    m = re.match(r"paper_(apa|bib)_(\w+)$", data)
    if m:
        style, key = m.group(1), m.group(2)
        papers = cache_get(key)
        if not papers:
            _status(cid, "❌ اطلاعات مقاله منقضی شده.", final=True)
            return
        text = _paper_apa(papers[0]) if style == "apa" else _paper_bibtex(papers[0])
        send_message(cid, text, parse_mode="Markdown", reply_markup=home_kb())
        return

    # paper_dl_{doi_key} — download from configured paper sources
    m = re.match(r"paper_dl_(\w+)$", data)
    if m:
        url_key = m.group(1)
        doi = get_url_for_chat(url_key, cid) or ""
        target_cid = cid
        if not doi:
            _status(target_cid, "❌ DOI یافت نشد.", final=True)
            return
        _do_paper_dl_doi(target_cid, doi)
        return

    # paper_oa_{url_key} — download open access PDF directly
    m = re.match(r"paper_oa_(\w+)$", data)
    if m:
        url_key = m.group(1)
        oa_url = get_url_for_chat(url_key, cid) or ""
        target_cid = cid
        if not oa_url:
            _status(target_cid, "❌ لینک یافت نشد.", final=True)
            return
        _status(target_cid, "⏳ در حال دانلود Open Access PDF…")
        chat_action(target_cid, "upload_document")
        sess = _libgen_session()
        try:
            resp = sess.get(
                oa_url,
                headers={"User-Agent": UA_DESK},
                timeout=60,
                allow_redirects=True,
            )
            if resp.status_code == 200 and len(resp.content) > 5000:
                fname = (
                    re.sub(r"[^\w\-.]", "_", oa_url.split("/")[-1])[:60] or "paper.pdf"
                )
                if not fname.endswith(".pdf"):
                    fname += ".pdf"
                if smart_send(target_cid, resp.content, fname, caption=f"📄 {fname}"):
                    _status(target_cid, "✅ مقاله ارسال شد.", reply_markup=home_kb(), final=True)
                else:
                    _status(cid, "❌ ارسال ناموفق بود.", reply_markup=home_kb(), final=True)
            else:
                _status(cid, "❌ دانلود ناموفق بود.", reply_markup=home_kb(), final=True)
        except Exception as e:
            log.warning("paper_oa: %s", e)
            _status(cid, "❌ خطا در دانلود.", reply_markup=home_kb(), final=True)
        clear_state(cid)
        return

    # ── APK callbacks ─────────────────────────────────────────────────────
    if data == "mode_apk":
        set_state(cid, mode="apk")
        send_message(
            cid,
            "📱 *Google Play APK Downloader*\n\n"
            "Enter an app name or package ID:\n"
            "• `telegram` ← search by name\n"
            "• `org.telegram.messenger` ← direct package ID",
            parse_mode="Markdown",
            reply_markup=cancel_kb(),
        )
        return

    # ── Movie/TV callbacks ──────────────────────────────────────────────────
    if data == "mode_movie":
        set_state(cid, mode="movie")
        send_message(
            cid,
            "🎬 *جستجوی فیلم و سریال (TMDb)*\n\n"
            "نام فیلم یا سریال را بنویسید:",
            parse_mode="Markdown",
            reply_markup=cancel_kb(),
        )
        return

    # movie_item_{cache_key}_{idx} — user clicked a movie/TV in results
    m = re.match(r"movie_item_(\w+)_(\d+)$", data)
    if m:
        key, idx = m.group(1), int(m.group(2))
        results = cache_get(key)
        if not results or idx >= len(results):
            _status(cid, "❌ نتیجه منقضی شده. دوباره جستجو کنید.", final=True)
            return
        item = results[idx]
        do_movie_show(cid, item, key, idx)
        return

    # movie_season_{cache_key}_{idx}_{season}
    m = re.match(r"movie_season_(\w+)_(\d+)_(\d+)$", data)
    if m:
        key, idx, season = m.group(1), int(m.group(2)), int(m.group(3))
        results = cache_get(key)
        if results and idx < len(results):
            do_movie_season(cid, results[idx], season, key, idx)
        return

    # movie_ep_{cache_key}_{idx}_{season}_{episode}
    m = re.match(r"movie_ep_(\w+)_(\d+)_(\d+)_(\d+)$", data)
    if m:
        key, idx, season, ep = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))
        results = cache_get(key)
        if results and idx < len(results):
            do_movie_episode(cid, results[idx], season, ep, key, idx)
        return

    # movie_subs_{cache_key}_{idx}
    m = re.match(r"movie_subs_(\w+)_(\d+)$", data)
    if m:
        key, idx = m.group(1), int(m.group(2))
        results = cache_get(key)
        if results and idx < len(results):
            do_movie_subs(cid, results[idx])
        return

    # movie_subsn_{cache_key}_{idx}_{season}
    m = re.match(r"movie_subsn_(\w+)_(\d+)_(\d+)$", data)
    if m:
        key, idx, season = m.group(1), int(m.group(2)), int(m.group(3))
        results = cache_get(key)
        if results and idx < len(results):
            do_movie_subs(cid, results[idx], season=season)
        return

    # movie_subse_{cache_key}_{idx}_{season}_{episode}
    m = re.match(r"movie_subse_(\w+)_(\d+)_(\d+)_(\d+)$", data)
    if m:
        key, idx, season, ep = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))
        results = cache_get(key)
        if results and idx < len(results):
            do_movie_subs(cid, results[idx], season=season, episode=ep)
        return

    # movie_watch_{cache_key}_{idx}
    m = re.match(r"movie_watch_(\w+)_(\d+)$", data)
    if m:
        key, idx = m.group(1), int(m.group(2))
        results = cache_get(key)
        if results and idx < len(results):
            do_movie_watch(cid, results[idx])
        return

    # sub_dl_{index} — download selected subtitle from cached list
    m = re.match(r"sub_dl_(\d+)$", data)
    if m:
        sub_idx = int(m.group(1))
        # Get the cached subtitle list from the user's state
        st = get_state(cid)
        sub_key = st.get("subtitle_cache_key")
        if sub_key:
            subs = cache_get(sub_key)
            if subs and sub_idx < len(subs):
                entry = subs[sub_idx]
                res = subtitle_download(entry)
                if res:
                    data, fn = res
                    if smart_send(cid, data, fn, caption=f"📝 زیرنویس فارسی: {entry.get('name','')}"):
                        send_message(cid, "✅ زیرنویس ارسال شد.", reply_markup=home_kb())
                        return
                send_message(cid, "❌ دانلود زیرنویس ناموفق بود.", reply_markup=home_kb())
                return
        send_message(cid, "❌ لیست زیرنویس منقضی شده.", reply_markup=home_kb())
        return

    # ── APK callbacks ─────────────────────────────────────────────────────
    # apk_item_{cache_key}_{idx} — user clicked an app in results
    m = re.match(r"apk_item_(\w+)_(\d+)$", data)
    if m:
        key, idx = m.group(1), int(m.group(2))
        results = cache_get(key)
        if not results or idx >= len(results):
            _status(cid, "❌ نتیجه منقضی شده. دوباره جستجو کنید.", final=True)
            return
        app = results[idx]
        # Fetch full info
        chat_action(cid)
        full_info = gplay_app_info(app["app_id"])
        do_apk_show(cid, full_info or app)
        return

    # apk_dl_{safe_app_id} — download APK
    m = re.match(r"apk_dl_(.+)$", data)
    if m:
        safe_id = m.group(1)
        app_id = safe_id.replace("_", ".")
        do_apk_download(cid, app_id)
        return

    # apk_gplay_{safe_app_id} — open Google Play page (screenshot)
    m = re.match(r"apk_(?:store|gplay)_(.+)$", data)
    if m:
        safe_id = m.group(1)
        app_id = safe_id.replace("_", ".")
        gplay_url = f"https://play.google.com/store/apps/details?id={app_id}"
        do_open_url(cid, gplay_url)
        return

    # apk_back — go back to last APK search results
    if data == "apk_back":
        query = st.get("last_query", "")
        key = st.get("cache_key", "")
        results = cache_get(key) if key else []
        if results:
            kb = apk_results_kb(results, key)
            _status(
                cid, f"📱 نتایج برای: _{query}_", parse_mode="Markdown", reply_markup=kb, final=True)
        else:
            _status(
                cid,
                "❌ جستجو منقضی شده. دوباره جستجو کنید.",
                reply_markup=main_menu_kb(), final=True)
        return

    # ── Audiobook (LibriVox) callbacks ────────────────────────────────────
    m = re.match(r"abk_item_(\w+)_(\d+)$", data)
    if m:
        key, idx = m.group(1), int(m.group(2))
        results = cache_get(key)
        if not results or idx >= len(results):
            _status(cid, "❌ نتیجه منقضی.", final=True)
            return
        do_librivox_show(cid, results[idx], key, idx)
        return

    m = re.match(r"abk_dl_(\w+)_(\d+)$", data)
    if m:
        fkey, idx = m.group(1), int(m.group(2))
        files = cache_get(f"abkf_{fkey}")
        if not files or idx >= len(files):
            _status(cid, "❌ لینک منقضی شده.", final=True)
            return
        f = files[idx]
        do_direct_file_download(cid, f["url"])
        return

    if data == "abk_back":
        key = st.get("cache_key", "")
        results = cache_get(key) if key else []
        if results:
            send_message(
                cid,
                "🎧 نتایج کتاب صوتی — کتاب را انتخاب کنید:",
                reply_markup=audiobook_results_kb(results, key),
            )
        else:
            send_message(cid, "🎧 جستجوی جدید:", reply_markup=book_kb())
        return

    # ── Rave Book Search callbacks ────────────────────────────────────────
    m = re.match(r"rvbk_item_(\w+)_(\d+)$", data)
    if m:
        key, idx = m.group(1), int(m.group(2))
        results = cache_get(key)
        if not results or idx >= len(results):
            _status(cid, "❌ نتیجه منقضی.", final=True)
            return
        r = results[idx]
        kurl = store_url(r["url"], chat_id=cid)
        txt = (
            f"📚 *{r['name'][:200]}*\n"
            f"🌐 منبع: `{r.get('host', '—')}`\n\n"
            "لینک دانلود در سایت منبع است (Rave هیچ فایلی میزبانی نمی‌کند)."
        )
        send_message(
            cid,
            txt,
            parse_mode="Markdown",
            reply_markup={
                "inline_keyboard": [
                    [
                        {"text": "🔗 بازکردن صفحه", "url": r["url"]},
                        {"text": "🌐 اسکرین‌شات", "callback_data": f"rvbk_shot_{kurl}"},
                    ],
                    [
                        {"text": "📥 دانلود مستقیم", "callback_data": f"dfile_dl_{kurl}"},
                        {"text": "🔙 نتایج", "callback_data": "rvbk_back"},
                    ],
                    [{"text": "🏠 منوی اصلی", "callback_data": "home"}],
                ]
            },
        )
        return

    if data == "rvbk_back":
        key = st.get("cache_key", "")
        results = cache_get(key) if key else []
        if results:
            send_message(
                cid,
                "📚 نتایج Rave Book Search — نتیجه را انتخاب کنید:",
                reply_markup=ravebook_results_kb(results, key),
            )
        else:
            send_message(cid, "📚 جستجوی جدید:", reply_markup=book_kb())
        return

    m = re.match(r"rvbk_shot_(\w+)$", data)
    if m:
        url = get_url_for_chat(m.group(1), cid)
        if not url:
            _status(cid, "❌ لینک منقضی شده.", final=True)
            return
        do_open_url(cid, url)
        return

    # ── Z-Library callbacks ───────────────────────────────────────────────
    if data == "mode_book":
        send_message(cid, "📚 دانلود کتاب و مقاله:", reply_markup=book_kb())
        return

    if data == "book_sources":
        send_message(
            cid,
            "🔗 *منابع بیشتر کتاب:*\n"
            "این منابع فایل رایگانِ مستقیم نمی‌دهند، پس لینک مستقیمشان را باز می‌کنیم.\n"
            "برای دانلود رایگان، از منابع داخل ربات استفاده کنید.",
            parse_mode="Markdown",
            reply_markup=book_sources_kb(),
        )
        return

    if data == "book_ravebook":
        set_state(cid, mode="ravebook", ravebook_mode="ebooks")
        send_message(
            cid,
            "📚 جستجو در Rave Book Search (ده‌ها سایت کتاب):\nعنوان کتاب یا نویسنده را بنویسید:",
            reply_markup=cancel_kb(),
        )
        return

    if data == "book_ravebook_audio":
        set_state(cid, mode="audiobook")
        send_message(
            cid,
            "🎧 جستجوی کتاب صوتی:\nعنوان کتاب یا نویسنده را بنویسید:",
            reply_markup=cancel_kb(),
        )
        return

    if data in ("book_search", "book_search_art"):
        if data == "book_search":
            set_state(cid, mode="book", zlib_ext=None)
            send_message(
                cid,
                "🔍 عنوان کتاب یا نام نویسنده را بنویسید:",
                reply_markup=cancel_kb(),
            )
        else:
            set_state(cid, mode="book_art")
            send_message(
                cid,
                "🔍 عنوان مقاله، DOI، یا نام نویسنده را بنویسید:",
                reply_markup=cancel_kb(),
            )
        return

    if data == "book_filter_pdf":
        set_state(cid, mode="book", zlib_ext=["PDF"])
        send_message(cid, "🔍 عنوان کتاب (فقط PDF):", reply_markup=cancel_kb())
        return

    if data == "book_filter_epub":
        set_state(cid, mode="book", zlib_ext=["EPUB"])
        send_message(cid, "🔍 عنوان کتاب (فقط EPUB):", reply_markup=cancel_kb())
        return

    if data == "book_filter_other":
        set_state(cid, mode="book", zlib_ext=["FB2", "MOBI", "AZW3"])
        send_message(cid, "🔍 عنوان کتاب (FB2/MOBI/AZW3):", reply_markup=cancel_kb())
        return

    if data == "book_back":
        query = st.get("last_query", "")
        key = st.get("cache_key", "")
        results = cache_get(key) if key else []
        if results:
            kb = book_results_kb(results, key)
            _status(
                cid, f"📚 نتایج: _{query}_", parse_mode="Markdown", reply_markup=kb, final=True)
        else:
            send_message(cid, "🏠", reply_markup=main_menu_kb())
        return

    # zlib_item_{cache_key}_{idx}  — کلیک روی کتاب
    m = re.match(r"book_item_(\w+)_(\d+)$", data)
    if m:
        key, idx = m.group(1), int(m.group(2))
        results = cache_get(key)
        if not results or idx >= len(results):
            _status(cid, "❌ نتیجه منقضی.", final=True)
            return
        do_book_show_book(cid, results[idx])
        return

    # book_dl_{url_key}  — دانلود کتاب (url_key points to libgen:{md5}:{ext})
    m = re.match(r"book_dl_(\w+)$", data)
    if m:
        url_key = m.group(1)
        stored = get_url_for_chat(url_key, cid)
        if not stored:
            _status(cid, "❌ لینک منقضی شده.", final=True)
            return
        # Parse libgen:{md5}:{ext} format
        if stored.startswith("libgen:"):
            parts = stored.split(":")
            if len(parts) >= 3:
                md5, ext = parts[1], parts[2]
                do_book_download(cid, f"{md5}_{ext}")
                return
        _status(cid, "❌ اطلاعات کتاب نامعتبر است.", final=True)
        return

    # ── RSS callbacks ─────────────────────────────────────────────────────
    if data == "mode_rss":
        send_message(cid, "📰 اخبار RSS:", reply_markup=rss_menu_kb())
        return

    if data == "rss_read":
        set_state(cid, mode="rss")
        send_message(cid, "📰 آدرس فید RSS یا سایت خبری را وارد کنید:", reply_markup=cancel_kb())
        return

    if data == "rss_subscribe":
        set_state(cid, mode="rss_subscribe")
        send_message(cid, "🔔 آدرس فید RSS را برای اشتراک خودکار بفرستید:", reply_markup=cancel_kb())
        return

    if data == "rss_subscriptions":
        _rss_subscriptions(cid)
        return

    m = re.match(r"rss_subs_page_(\d+)$", data)
    if m:
        _rss_subscriptions(cid, page=int(m.group(1)))
        return

    m = re.match(r"rss_keep_(\w+)$", data)
    if m:
        cache_set_owned("unsub_" + m.group(1), [], _platform(), cid)
        _rss_subscriptions(cid)
        return

    m = re.match(r"rss_(unsub|confirm)_(\w+)$", data)
    if m:
        action, key = m.groups()
        url = get_url_for_chat(key, cid)
        if action == "unsub":
            if not url:
                _status(cid, "❔ دکمه منقضی شده است؛ فهرست اشتراک‌ها را دوباره باز کنید.", reply_markup=rss_menu_kb(), final=True)
                return
            cache_set_owned("unsub_" + key, [time.time() + 300], _platform(), cid)
            send_message(cid, f"🔕 اشتراک این فید لغو شود؟\n{url}", reply_markup={"inline_keyboard": [
                [{"text": "بله، لغو اشتراک", "callback_data": f"rss_confirm_{key}"}],
                [{"text": "🔙 حفظ اشتراک", "callback_data": f"rss_keep_{key}"}]]})
        else:
            pending = cache_get_owned("unsub_" + key, _platform(), cid)
            cache_set_owned("unsub_" + key, [], _platform(), cid)
            if not url or not pending or pending[0] < time.time():
                _status(cid, "❔ تأیید منقضی شده است؛ اشتراک‌ها را دوباره باز کنید.", reply_markup=rss_menu_kb(), final=True)
            elif any(sub.get("topic") == url for sub in durable_store.get_subscriptions(_platform(), cid)):
                durable_store.unsubscribe(_platform(), cid, url)
                _status(cid, "✅ اشتراک فید لغو شد.", reply_markup=rss_menu_kb(), final=True)
            else:
                _status(cid, "ℹ️ این فید دیگر در اشتراک‌های شما نیست.", reply_markup=rss_menu_kb(), final=True)
        return

    m = re.match(r"rss_url_(\w+)$", data)
    if m:
        url_key = m.group(1)
        url = get_url_for_chat(url_key, cid)
        if not url:
            _status(cid, "❌ لینک منقضی.", final=True)
            return
        _status(cid, "⏳ دریافت فید…")
        items = rss_fetch(url, 15)
        if not items:
            _status(cid, "❌ فید خالی یا نامعتبر.", reply_markup=home_kb(), final=True)
            return
        key = _rss_cache_key(cid, url, 0)
        cache_set_owned(key, items, _platform(), cid)
        set_state(cid, mode="rss", last_query=url, cache_key=key)
        _display_rss_list(cid, items, key)
        return

    m = re.match(r"rss_page_(\w+)_(\d+)$", data)
    if m:
        key, page = m.group(1), int(m.group(2))
        items = cache_get_owned(key, _platform(), cid)
        if not items:
            _status(cid, "❔ فید منقضی شده است؛ دوباره باز کنید.", final=True)
            return
        _display_rss_list(cid, items, key, page=page)
        return

    m = re.match(r"rss_item_(\w+)_(\d+)$", data)
    if m:
        key, idx = m.group(1), int(m.group(2))
        items = cache_get_owned(key, _platform(), cid)
        if not items or idx < 0 or idx >= len(items):
            _status(cid, "❔ خبر منقضی شده است؛ فید را دوباره باز کنید.", final=True)
            return
        _show_rss_item(cid, items[idx])
        return

    # ── Site view buttons ─────────────────────────────────────────────────
    m = re.match(r"site_(tr|scan)_(\w+)$", data)
    if m:
        action, url_key = m.group(1), m.group(2)
        url = get_url_for_chat(url_key, cid)
        if not url:
            _status(cid, "❌ لینک منقضی شده.", reply_markup=home_kb(), final=True)
            return
        if action == "tr":
            _status(cid, "⏳ در حال ترجمه متن صفحه…")
            text = translate_text(page_to_text(url)[:8000], "fa")
            send_message(cid, f"🌐 ترجمه فارسی صفحه:\n\n{text[:3500]}", reply_markup=home_kb())
        else:
            do_virusscan_url(cid, url)
        return

    m = re.match(r"site_(text|html|zip|pdf)_(\w+)$", data)
    if m:
        action, url_key = m.group(1), m.group(2)
        url = get_url_for_chat(url_key, cid)
        if not url:
            _status(cid, "❌ لینک منقضی شده.", reply_markup=home_kb(), final=True)
            return
        chat_action(cid, "upload_document")
        ok = False
        if action == "text":
            txt = page_to_text(url)
            ok = bool(send_message(cid, f"📝 متن صفحه:\n\n{txt[:3500]}", reply_markup=home_kb()).get("ok"))
            if len(txt) > 3500:
                ok = send_document(cid, txt.encode("utf-8"), "page_text.txt") and ok
        elif action == "html":
            html = fetch_page(url)
            if html:
                ok = send_document(cid, html, "page.html", caption=f"🌐 {url[:60]}")
            else:
                _status(cid, "❌ خطا در دریافت HTML.", reply_markup=home_kb(), final=True)
                return
        elif action == "zip":
            _status(cid, "⏳ در حال ساخت ZIP…")
            zdata = page_to_zip(url)
            if zdata:
                domain = urllib.parse.urlparse(url).netloc.replace(".", "_")
                ok = send_document(cid, zdata, f"{domain}_offline.zip")
            else:
                _status(cid, "❌ خطا در ZIP.", reply_markup=home_kb(), final=True)
                return
        elif action == "pdf":
            _status(cid, "⏳ در حال تولید PDF…")
            pdf = page_to_pdf(url)
            if pdf:
                ext = "pdf" if pdf[:4] == b"%PDF" else "html"
                ok = send_document(cid, pdf, f"page.{ext}", caption=f"📑 {url[:60]}")
            else:
                _status(cid, "❌ خطا در PDF.", reply_markup=home_kb(), final=True)
                return
        _status(cid, "✅ ارسال شد." if ok else "❌ ارسال خروجی ناموفق بود.", reply_markup=site_view_kb(url_key), final=True)
        return

    # ── Search result click (web, scholar) ────────────────────────────────
    m = re.match(r"res_(\w+)_(\d+)$", data)
    if m:
        key, idx = m.group(1), int(m.group(2))
        source = _search_source(cid, key)
        results = source["results"] if source else []
        if not results or idx >= len(results):
            _status(cid, "❌ نتیجه منقضی.", final=True)
            return
        r = results[idx]
        # Show detail with options
        link = r.get("link") or r.get("url", "")
        snippet = r.get("snippet") or r.get("meta", "")
        txt = f"🔗 *{r.get('title', '')}*\n\n{snippet[:400]}\n\n[باز کردن لینک]({link})"
        url_key = store_url(link, chat_id=cid)
        kb = {
            "inline_keyboard": [
                [
                    {"text": "🌐 مشاهده سایت", "callback_data": f"site_ss_{url_key}"},
                    {"text": "📝 متن صفحه", "callback_data": f"site_text_{url_key}"},
                ],
                [
                    {"text": "🌐 HTML", "callback_data": f"site_html_{url_key}"},
                    {"text": "🔙 برگشت به نتایج", "callback_data": f"search_back_{key}"},
                ],
            ]
        }
        send_message(cid, txt, parse_mode="Markdown", reply_markup=kb)
        return

    m = re.match(r"site_ss_(\w+)$", data)
    if m:
        url_key = m.group(1)
        url = get_url_for_chat(url_key, cid)
        if not url:
            _status(cid, "❌ لینک منقضی.", final=True)
            return
        _status(cid, f"⏳ گرفتن اسکرین‌شات…")
        ss = screenshot_page(url)
        if ss:
            send_photo(cid, ss, caption=url[:60], reply_markup=site_view_kb(url_key))
        else:
            _status(
                cid, "❌ اسکرین‌شات ممکن نبود.", reply_markup=site_view_kb(url_key), final=True)
        return

    m = re.match(r"search_back_(\w+)$", data) or re.match(r"search_page_(\w+)_(\d+)$", data)
    if m:
        action = "back" if data.startswith("search_back_") else "page"
        key = m.group(1)
        target = m.group(2) if action == "page" else None
        source = _search_source(cid, key)
        if not source:
            _status(cid, "❔ دکمهٔ جستجو منقضی شده است؛ دوباره جستجو کنید.", reply_markup=home_kb(), final=True)
        elif action == "back":
            _show_search_source(cid, source, key)
        else:
            target_key = make_cache_key(source["kind"], f"{_platform()}:{cid}:{source['query']}", int(target))
            cached = _search_source(cid, target_key)
            if cached:
                _show_search_source(cid, cached, target_key)
            else:
                handler = {"ws": do_search, "sc": do_scholar, "uni": unified_search}[source["kind"]]
                handler(cid, source["query"], int(target))
        return

    if data == "results_back" or re.match(r"page_(ws|sc|uni)_\d+$", data):
        _status(cid, "❔ این دکمه قدیمی است؛ جستجو را دوباره آغاز کنید.", reply_markup=home_kb(), final=True)
        return

    # ── Wiki article click ────────────────────────────────────────────────
    m = re.match(r"wiki_art_(\w+)_(\d+)_(\w+)$", data)
    if m:
        key, idx, lang = m.group(1), int(m.group(2)), m.group(3)
        results = cache_get(key)
        if not results or idx >= len(results):
            _status(cid, "❌ نتیجه منقضی.", final=True)
            return
        article = results[idx]
        do_wiki_article(cid, article["title"], lang)
        return

    # ── Telegram channel ──────────────────────────────────────────────────
    if data == "mode_tg_channel":
        set_state(cid, mode="tg_channel_menu")
        send_message(cid, "✈️ کانال تلگرام:", reply_markup=tg_channel_kb())
        return

    if data == "tg_read_web":
        set_state(cid, mode="tg_read")
        send_message(
            cid,
            "✈️ نام کانال عمومی را وارد کنید:\n_(بدون @ — مثال: `durov`)_",
            parse_mode="Markdown",
            reply_markup=cancel_kb(),
        )
        return

    if data == "tg_read_mtproto":
        if not TG_API_ID or not TG_API_HASH:
            log.error("tg_read_mtproto: TG_API_ID/TG_API_HASH not configured")
            _status(
                cid, "❌ این قابلیت در حال حاضر در دسترس نیست.", reply_markup=home_kb(), final=True)
            return
        set_state(cid, mode="tg_mtproto")
        send_message(
            cid, "✈️ نام کانال را وارد کنید (عمومی یا خصوصی):", reply_markup=cancel_kb()
        )
        return

    if data == "tg_dl_media":
        set_state(cid, mode="tg_dl")
        send_message(
            cid,
            "✈️ لینک پیام تلگرام را وارد کنید:\n_(مثال: `https://t.me/channel/123`)_",
            parse_mode="Markdown",
            reply_markup=cancel_kb(),
        )
        return

    # ── Twitter/X ─────────────────────────────────────────────────────────
    if data == "mode_twitter":
        send_message(cid, "🐦 توییتر / X:", reply_markup=twitter_kb())
        return

    if data == "tw_timeline":
        set_state(cid, mode="twitter_tl")
        send_message(
            cid,
            "🐦 نام کاربری توییتر را وارد کنید:\n_(مثال: `elonmusk`)_",
            parse_mode="Markdown",
            reply_markup=cancel_kb(),
        )
        return

    if data == "tw_dl":
        set_state(cid, mode="twitter_dl")
        send_message(
            cid,
            "🐦 لینک توییت را وارد کنید:\n"
            "_(مثال: `https://twitter.com/user/status/123`)_",
            parse_mode="Markdown",
            reply_markup=cancel_kb(),
        )
        return

    m_twunroll = re.match(r"tw_unroll_(https?://.+)$", data)
    if m_twunroll:
        do_twitter_thread(cid, m_twunroll.group(1))
        return

    if data == "tw_thread":
        set_state(cid, mode="twitter_thread")
        send_message(
            cid,
            "🧵 لینک یکی از پست‌های رشته (ترید) را بفرستید:\n"
            "_(مثال: `https://x.com/user/status/123`)_\n"
            "رشته به صورت یک متن پیوسته نمایش داده می‌شود.",
            parse_mode="Markdown",
            reply_markup=cancel_kb(),
        )
        return

    if data == "tw_dl_translate":
        set_state(cid, mode="twitter_dl_translate")
        send_message(
            cid,
            "🐦🇮🇷 لینک توییت را وارد کنید — متن به فارسی ترجمه می‌شود:",
            reply_markup=cancel_kb(),
        )
        return

    # ── Instagram ─────────────────────────────────────────────────────────
    if data == "mode_instagram":
        send_message(cid, "📸 اینستاگرام:", reply_markup=instagram_kb())
        return

    if data == "ig_profile":
        set_state(cid, mode="ig_profile")
        send_message(
            cid,
            "📸 نام کاربری اینستاگرام را وارد کنید:\n_(مثال: `natgeo`)_",
            parse_mode="Markdown",
            reply_markup=cancel_kb(),
        )
        return

    if data == "ig_dl":
        set_state(cid, mode="ig_dl")
        send_message(
            cid,
            "📸 لینک پست یا ریل را وارد کنید:\n"
            "_(مثال: `https://www.instagram.com/p/ABC123/`)_",
            parse_mode="Markdown",
            reply_markup=cancel_kb(),
        )
        return

    if data == "ig_story":
        set_state(cid, mode="ig_story")
        send_message(
            cid,
            "📸 نام کاربری اینستاگرام را وارد کنید (بدون @):\n"
            "استوری فعال ۲۴ ساعت اخیر دانلود می‌شود.",
            reply_markup=cancel_kb(),
        )
        return

    if data == "ig_profile_pic":
        set_state(cid, mode="ig_profile_pic")
        send_message(
            cid,
            "📸 نام کاربری اینستاگرام را وارد کنید (بدون @):",
            reply_markup=cancel_kb(),
        )
        return

    if data == "ig_profile_shot":
        set_state(cid, mode="ig_profile_shot")
        send_message(
            cid,
            "📸 نام کاربری اینستاگرام را وارد کنید (بدون @):",
            reply_markup=cancel_kb(),
        )
        return

    if data == "ig_mosaic":
        set_state(cid, mode="ig_mosaic")
        send_message(
            cid, "🔲 لینک پست کاروسل اینستاگرام را وارد کنید:", reply_markup=cancel_kb()
        )
        return

    # ── TikTok ────────────────────────────────────────────────────────────
    if data == "mode_tiktok":
        send_message(cid, "🎵 TikTok:", reply_markup=tiktok_kb())
        return

    if data == "tt_user":
        set_state(cid, mode="tt_user")
        send_message(
            cid,
            "🎵 نام کاربری TikTok را وارد کنید:\n_(مثال: `khaby.lame`)_",
            parse_mode="Markdown",
            reply_markup=cancel_kb(),
        )
        return

    if data == "tt_dl":
        set_state(cid, mode="tt_dl")
        send_message(
            cid, "🎵 لینک ویدیو TikTok را وارد کنید:", reply_markup=cancel_kb()
        )
        return

    if data == "tt_sound":
        set_state(cid, mode="tt_sound")
        send_message(
            cid,
            "🎵 لینک پست TikTok (ویدیو یا عکس) را وارد کنید تا صدایش ارسال شود:",
            reply_markup=cancel_kb(),
        )
        return

    # ── Reddit ──────────────────────────────────────────────────────────────
    if data == "mode_reddit":
        send_message(cid, "📱 رددیت:", reply_markup=reddit_kb())
        return

    if data == "reddit_dl":
        set_state(cid, mode="reddit_dl")
        send_message(
            cid,
            "📱 لینک پست رددیت را وارد کنید:\n"
            "_(مثال: `https://www.reddit.com/r/subreddit/comments/abc123/`)_",
            parse_mode="Markdown",
            reply_markup=cancel_kb(),
        )
        return

    if data == "reddit_profile":
        set_state(cid, mode="reddit_profile")
        send_message(
            cid,
            "📱 نام کاربری رددیت را وارد کنید:\n_(مثال: `spez`)_",
            parse_mode="Markdown",
            reply_markup=cancel_kb(),
        )
        return

    # ── Twitch ──────────────────────────────────────────────────────────────
    if data == "mode_twitch":
        send_message(cid, "🎮 تویچ:", reply_markup=twitch_kb())
        return

    if data == "twitch_dl":
        set_state(cid, mode="twitch_dl")
        send_message(
            cid,
            "🎮 لینک VOD یا Clip تویچ را وارد کنید:\n"
            "_(مثال: `https://www.twitch.tv/videos/123456789` یا `https://clips.twitch.tv/abc123`)_",
            parse_mode="Markdown",
            reply_markup=cancel_kb(),
        )
        return

    if data == "twitch_user":
        set_state(cid, mode="twitch_user")
        send_message(
            cid,
            "🎮 نام کاربری تویچ را وارد کنید:\n_(مثال: `shroud`)_",
            parse_mode="Markdown",
            reply_markup=cancel_kb(),
        )
        return

    # ── Vimeo ───────────────────────────────────────────────────────────────
    if data == "mode_vimeo":
        send_message(cid, "🎬 ویمئو:", reply_markup=vimeo_kb())
        return

    if data == "vimeo_dl":
        set_state(cid, mode="vimeo_dl")
        send_message(
            cid,
            "🎬 لینک ویدیو ویمئو را وارد کنید:\n"
            "_(مثال: `https://vimeo.com/123456789`)_",
            parse_mode="Markdown",
            reply_markup=cancel_kb(),
        )
        return

    # ── Pinterest Video ─────────────────────────────────────────────────────
    if data == "mode_pinterest_video":
        send_message(cid, "📌 پینترست ویدیو:", reply_markup=pinterest_kb())
        return

    if data == "pin_dl":
        set_state(cid, mode="pin_dl")
        send_message(
            cid,
            "📌 لینک ویدیو پینترست را وارد کنید:\n"
            "_(مثال: `https://www.pinterest.com/pin/123456789/`)_",
            parse_mode="Markdown",
            reply_markup=cancel_kb(),
        )
        return

    # ── Bluesky ───────────────────────────────────────────────────────────
    if data == "mode_bluesky":
        set_state(cid, mode="bluesky")
        send_message(
            cid,
            "🦋 لینک پست Bluesky را وارد کنید:\n"
            "_(مثال: `https://bsky.app/profile/user.bsky.social/post/abc123`)_",
            parse_mode="Markdown",
            reply_markup=cancel_kb(),
        )
        return

    # ── YouTube extras ────────────────────────────────────────────────────
    if data == "yt_thumb":
        set_state(cid, mode="yt_thumb")
        send_message(
            cid,
            "▶️ لینک ویدیوی یوتیوب را برای دریافت کاور وارد کنید:",
            reply_markup=cancel_kb(),
        )
        return

    if data == "yt_stats":
        set_state(cid, mode="yt_stats")
        send_message(
            cid,
            "▶️ لینک ویدیوی یوتیوب را برای مشاهده آمار وارد کنید:",
            reply_markup=cancel_kb(),
        )
        return

    # ── SoundCloud download from info page ────────────────────────────────
    m_sc = re.match(r"sc_dl_(\w+)$", data)
    if m_sc:
        url_key = m_sc.group(1)
        url = get_url_for_chat(url_key, cid)
        target_cid = cid
        if not url:
            _status(target_cid, "❌ لینک منقضی شده.", final=True)
            return
        # Capture platform BEFORE spawning thread — _ctx is thread-local,
        # the new thread would otherwise default to Bale and send to the
        # wrong API if this button was clicked from Telegram.
        captured_platform = _platform()
        operation = getattr(_ctx, "operation", None)
        callback_operation = getattr(_ctx, "callback_operation", False)

        def _music_thread(cid=target_cid, u=url, plat=captured_platform):
            _set_platform_ctx(plat, chat_id=cid, operation=operation,
                              callback_operation=callback_operation)
            do_music(cid, u)

        threading.Thread(target=_music_thread, daemon=True).start()
        return

    # ── Social result item click: soc_{platform}_{cache_key}_{idx} ───────
    m = re.match(r"soc_(tg|tw|ig|tt)_(\w+)_(\d+)$", data)
    if m:
        platform, key, idx = m.group(1), m.group(2), int(m.group(3))
        results = cache_get(key)
        if not results or idx >= len(results):
            _status(cid, "❌ نتیجه منقضی.", final=True)
            return
        item = results[idx]
        if platform == "tg":
            do_tg_show_message(cid, item)
        elif platform == "tw":
            do_twitter_show(cid, item)
        elif platform == "ig":
            url = item.get("url", "")
            text = item.get("text", "")  # full caption
            date = item.get("date", "")
            is_vid = item.get("is_video", False)
            likes = item.get("likes", 0)
            display_url = item.get("display_url", "")

            # Build caption
            lines = [f"📸 {'🎬 ریل/ویدیو' if is_vid else '🖼 پست'}"]
            if likes:
                lines.append(f"❤️ {likes:,}")
            if date:
                lines.append(f"🕐 {date}")
            if text:
                lines.append(f"\n{text}")
            if url:
                lines.append(f"\n[🔗 لینک]({url})")
            send_message(cid, "\n".join(l for l in lines if l), parse_mode="Markdown")

            # Auto-download thumbnail/image
            if display_url and not is_vid:
                try:
                    img_bytes = download_bytes(display_url, MAX_IMAGE_SIZE)
                    if img_bytes and len(img_bytes) > 500:
                        send_photo(cid, img_bytes, caption="📸")
                except Exception as e:
                    log.warning("ig thumb dl: %s", e)

            # Store caption in state for download
            if text:
                set_state(cid, last_caption=text[:1024])

            # Download button for video or full quality
            kb = social_post_kb(url, "ig", True) if url else home_kb()
            send_message(
                cid,
                "📥 دانلود ویدیو?" if is_vid else "📥 دانلود نسخه کامل؟",
                reply_markup=kb,
            )
        elif platform == "tt":
            url = item.get("url", "")
            title = item.get("title", "")
            dur = item.get("duration", "")
            thumbnail = item.get("thumbnail", "")
            text = f"🎵 *{title}*" if title else "🎵 ویدیوی TikTok"
            if dur:
                text += f"\n⏱ {dur}"
            if url:
                text += f"\n[🔗 لینک]({url})"
            send_message(cid, text, parse_mode="Markdown")
            if thumbnail:
                try:
                    thumb_bytes = download_bytes(thumbnail, MAX_IMAGE_SIZE)
                    if thumb_bytes and len(thumb_bytes) > 500:
                        send_photo(cid, thumb_bytes, caption="🎵")
                except Exception as e:
                    log.warning("tt thumb dl: %s", e)
            # Store full caption so do_tiktok_dl can use it
            set_state(cid, last_caption=title)
            kb = social_post_kb(url, "tt", True) if url else home_kb()
            send_message(cid, "📥 دانلود ویدیو؟", reply_markup=kb)
        return

    # ── Reddit item click: reddit_item_{cache_key}_{idx} ───────
    m = re.match(r"reddit_item_(\w+)_(\d+)$", data)
    if m:
        key, idx = m.group(1), int(m.group(2))
        results = cache_get_owned(key, _platform(), cid)
        if not results or idx < 0 or idx >= len(results):
            _status(cid, "❔ نتیجه منقضی شده است؛ دوباره جستجو کنید.", final=True)
            return
        item = results[idx]
        url = item.get("url", "")
        title = item.get("title", "")
        dur = item.get("duration", "")
        thumbnail = item.get("thumbnail", "")
        text = f"📱 *{title}*" if title else "📱 پست رددیت"
        if dur:
            text += f"\n⏱ {dur}"
        if url:
            text += f"\n[🔗 لینک]({url})"
        send_message(cid, text, parse_mode="Markdown")
        if thumbnail:
            try:
                thumb_bytes = download_bytes(thumbnail, MAX_IMAGE_SIZE)
                if thumb_bytes and len(thumb_bytes) > 500:
                    send_photo(cid, thumb_bytes, caption="📱")
            except Exception as e:
                log.warning("reddit thumb dl: %s", e)
        set_state(cid, last_caption=title)
        kb = social_post_kb(url, "reddit", True) if url else home_kb()
        send_message(cid, "📥 دانلود؟", reply_markup=kb)
        return

    # ── Twitch item click: twitch_item_{cache_key}_{idx} ───────
    m = re.match(r"twitch_item_(\w+)_(\d+)$", data)
    if m:
        key, idx = m.group(1), int(m.group(2))
        results = cache_get_owned(key, _platform(), cid)
        if not results or idx < 0 or idx >= len(results):
            _status(cid, "❔ نتیجه منقضی شده است؛ دوباره جستجو کنید.", final=True)
            return
        item = results[idx]
        url = item.get("url", "")
        title = item.get("title", "")
        dur = item.get("duration", "")
        thumbnail = item.get("thumbnail", "")
        text = f"🎮 *{title}*" if title else "🎮 ویدیوی تویچ"
        if dur:
            text += f"\n⏱ {dur}"
        if url:
            text += f"\n[🔗 لینک]({url})"
        send_message(cid, text, parse_mode="Markdown")
        if thumbnail:
            try:
                thumb_bytes = download_bytes(thumbnail, MAX_IMAGE_SIZE)
                if thumb_bytes and len(thumb_bytes) > 500:
                    send_photo(cid, thumb_bytes, caption="🎮")
            except Exception as e:
                log.warning("twitch thumb dl: %s", e)
        set_state(cid, last_caption=title)
        kb = social_post_kb(url, "twitch", True) if url else home_kb()
        send_message(cid, "📥 دانلود؟", reply_markup=kb)
        return

    # ── Social media quality picker: soc_dl_quality_{platform}_{url_key} ────────
    m = re.match(r"soc_dl_quality_(reddit|twitch|vimeo|pinterest|hls)_(\w+)$", data)
    if m:
        platform, url_key = m.group(1), m.group(2)
        url = get_url_for_chat(url_key, cid) or ""
        if not url:
            _status(cid, "❌ لینک منقضی.", final=True)
            return
        _status(cid, "🔍 دریافت کیفیت‌های موجود…")
        try:
            result = subprocess.run(
                [YTDLP_BIN, "-j", "--no-warnings", url],
                capture_output=True, text=True, timeout=30
            )
            info = json.loads(result.stdout.strip().split("\n")[-1])
            formats = info.get("formats", [])
            video_formats = [f for f in formats if f.get("vcodec") != "none" and f.get("acodec") != "none"]
            if not video_formats:
                video_formats = [f for f in formats if f.get("vcodec") != "none"]

            # Sort by height descending
            video_formats.sort(key=lambda x: x.get("height", 0), reverse=True)

            rows = []
            for f in video_formats[:8]:  # Max 8 options
                h = f.get("height", "?")
                ext = f.get("ext", "mp4")
                fps = f.get("fps", "")
                size = f.get("filesize", 0) or f.get("filesize_approx", 0)
                size_str = f" ({size/1024/1024:.1f}MB)" if size else ""
                fps_str = f"@{fps}fps" if fps else ""
                label = f"{h}p {ext}{fps_str}{size_str}"
                fmt_id = f.get("format_id", "")
                rows.append([{"text": label, "callback_data": f"soc_dl_fmt_{platform}_{url_key}_{fmt_id}"}])

            rows.append([{"text": "❌ انصراف", "callback_data": "cancel"}])
            send_message(cid, "🎬 کیفیت ویدیو را انتخاب کنید:", reply_markup={"inline_keyboard": rows})
        except Exception as e:
            log.error("quality picker error: %s", e)
            _status(cid, "❌ خطا در دریافت کیفیت‌ها.", final=True)
        return
    m = re.match(r"soc_dl_(tg|tw|ig|tt|reddit|twitch|vimeo|pinterest|hls)_(\w+)$", data)
    if m:
        platform, url_key = m.group(1), m.group(2)
        url = get_url_for_chat(url_key, cid) or ""
        if not url:
            _status(cid, "❌ لینک منقضی.", final=True)
            return
        if platform == "tg":
            do_tg_dl_media(cid, url)
        elif platform == "tw":
            do_twitter_dl(cid, url)
        elif platform == "ig":
            do_instagram_dl(cid, url)
        elif platform == "tt":
            do_tiktok_dl(cid, url)
        elif platform == "reddit":
            do_reddit_dl(cid, url)
        elif platform == "twitch":
            do_twitch_dl(cid, url)
        elif platform == "vimeo":
            do_vimeo_dl(cid, url)
        elif platform == "pinterest":
            do_pinterest_video_dl(cid, url)
        return

    # ── Social media audio-only download: soc_dl_audio_{platform}_{url_key} ────────
    m = re.match(r"soc_dl_audio_(tg|tw|ig|tt|reddit|twitch|vimeo|pinterest)_(\w+)$", data)
    if m:
        platform, url_key = m.group(1), m.group(2)
        url = get_url_for_chat(url_key, cid) or ""
        if not url:
            _status(cid, "❌ لینک منقضی.", final=True)
            return
        if platform == "reddit":
            do_reddit_dl(cid, url, audio_only=True)
        elif platform == "twitch":
            do_twitch_dl(cid, url, audio_only=True)
        elif platform == "vimeo":
            do_vimeo_dl(cid, url, audio_only=True)
        elif platform == "pinterest":
            do_pinterest_video_dl(cid, url, audio_only=True)
        elif platform == "tg":
            do_tg_dl_media(cid, url, audio_only=True)
        elif platform == "tw":
            do_twitter_dl(cid, url, audio_only=True)
        elif platform == "ig":
            do_instagram_dl(cid, url, audio_only=True)
        elif platform == "tt":
            do_tiktok_dl(cid, url, audio_only=True)
        return

    # ── Social media quality picker: soc_dl_quality_{platform}_{url_key} ────────
    m = re.match(r"soc_dl_quality_(reddit|twitch|vimeo|pinterest|hls)_(\w+)$", data)
    if m:
        platform, url_key = m.group(1), m.group(2)
        url = get_url_for_chat(url_key, cid) or ""
        if not url:
            _status(cid, "❌ لینک منقضی.", final=True)
            return
        # Probe formats via yt-dlp
        _status(cid, "🔍 در حال بررسی کیفیت‌های موجود...")
        try:
            import subprocess, json
            result = subprocess.run(
                ["yt-dlp", "-j", url],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode != 0:
                _status(cid, "❌ خطا در دریافت اطلاعات ویدیو.", final=True)
                return
            info = json.loads(result.stdout.strip().split('\n')[-1])
            formats = info.get("formats", [])
            # Filter video-only formats (with height)
            video_formats = []
            seen = set()
            for f in formats:
                h = f.get("height")
                if h and h not in seen and f.get("vcodec") != "none":
                    seen.add(h)
                    video_formats.append({
                        "height": h,
                        "ext": f.get("ext", "mp4"),
                        "format_id": f.get("format_id"),
                    })
            video_formats.sort(key=lambda x: x["height"], reverse=True)
            if not video_formats:
                _status(cid, "❌ فرمت ویدیو یافت نشد.", final=True)
                return
            # Build quality keyboard
            rows = []
            for vf in video_formats[:6]:  # Max 6 options
                label = f"{vf['height']}p ({vf['ext']})"
                rows.append([{
                    "text": label,
                    "callback_data": f"soc_dl_fmt_{platform}_{url_key}_{vf['format_id']}",
                }])
            rows.append([{"text": "❌ انصراف", "callback_data": "cancel"}])
            send_message(cid, "🎬 کیفیت مورد نظر را انتخاب کنید:", reply_markup={"inline_keyboard": rows})
        except Exception as e:
            log.error("quality probe: %s", e)
            _status(cid, "❌ دریافت لیست کیفیت‌ها ناموفق بود.\n" + friendly_error(e),
                    reply_markup=home_kb(), final=True)
        return

    # ── Social media format selection: soc_dl_fmt_{platform}_{url_key}_{format_id} ────────
    m = re.match(r"soc_dl_fmt_(reddit|twitch|vimeo|pinterest|hls)_(\w+)_(.+)$", data)
    if m:
        platform, url_key, fmt_id = m.group(1), m.group(2), m.group(3)
        url = get_url_for_chat(url_key, cid) or ""
        if not url:
            _status(cid, "❌ لینک منقضی.", final=True)
            return
        _status(cid, f"⬇️ دانلود با فرمت {fmt_id}...")
        if platform == "reddit":
            do_reddit_dl(cid, url, format_id=fmt_id)
        elif platform == "twitch":
            do_twitch_dl(cid, url, format_id=fmt_id)
        elif platform == "vimeo":
            do_vimeo_dl(cid, url, format_id=fmt_id)
        elif platform == "pinterest":
            do_pinterest_video_dl(cid, url, format_id=fmt_id)
        elif platform == "hls":
            do_hls_dl(cid, url, format_id=fmt_id)
        return

    # ── Music track click: mu_dl_{cache_key}_{idx} ───────────────────────
    m = re.match(r"mu_dl_(\w+)_(\d+)$", data)
    if m:
        key, idx = m.group(1), int(m.group(2))
        results = cache_get(key)
        if not results or idx >= len(results):
            _status(cid, "❌ نتیجه منقضی شده. دوباره جستجو کنید.", final=True)
            return
        track = results[idx]
        url = track.get("url", "")
        title = track.get("title", "")
        artist = track.get("uploader", "")
        thumb = track.get("thumbnail", "")
        source = track.get("source", "youtube")

        # Send thumbnail first
        if thumb:
            try:
                tb = download_bytes(thumb, MAX_IMAGE_SIZE)
                if tb:
                    dur = track.get("duration", "")
                    cap = title
                    if artist:
                        cap += f"\n🎤 {artist}"
                    if dur:
                        cap += f"\n⏱ {dur}"
                    src_label = {
                        "youtube": "▶️ YouTube",
                        "ytmusic": "🎵 YouTube Music",
                        "soundcloud": "☁️ SoundCloud",
                        "jiosaavn": "🎶 JioSaavn",
                        "deezer": "🎧 Deezer",
                    }.get(source, "")
                    if src_label:
                        cap += f"\n{src_label}"
                    send_photo(cid, tb, caption=cap[:1024])
            except Exception as e:
                log.warning("music thumb: %s", e)

        # Store track info in state for audio format selection
        set_state(cid, last_url=url, last_title=title, last_artist=artist, last_track_item=track)
        send_message(cid, "🎵 فرمت صوتی را انتخاب کنید:", reply_markup=audio_format_kb())
        return

    # ── Generic search results: res_{cache_key}_{idx} (Scholar, Wiki, etc.) ───
    m = re.match(r"res_(\w+)_(\d+)$", data)
    if m:
        key, idx = m.group(1), int(m.group(2))
        results = cache_get(key)
        if not results or idx >= len(results):
            _status(cid, "❌ نتیجه منقضی شده. دوباره جستجو کنید.", final=True)
            return
        result = results[idx]

        # Determine source from cache key (first 2 chars)
        source = key[:2] if key else ""

        # Scholar result: show title + snippet + link button
        if source == "sc":
            title = result.get("title", "")
            snippet = result.get("snippet", "")
            meta = result.get("meta", "")
            link = result.get("link", "")

            lines = []
            if title:
                lines.append(f"📚 *{title}*")
            if meta:
                lines.append(f"_{meta}_")
            if snippet:
                lines.append(f"\n{snippet[:300]}")

            text = "\n".join(lines) if lines else "📚 نتیجه جستجو"

            kb = home_kb()
            if link:
                kb = {
                    "inline_keyboard": [
                        [
                            {
                                "text": "🔗 مشاهده در Google Scholar",
                                "callback_data": f"open_url_{store_url(link, chat_id=cid)}",
                            }
                        ],
                        [{"text": "🏠 منوی اصلی", "callback_data": "home"}],
                    ]
                }

            send_message(cid, text[:1000], parse_mode="Markdown", reply_markup=kb)
            return

        # Wiki result: handled by separate wiki_art_ pattern, shouldn't reach here
        # But for safety, fall through to unhandled
        pass

    # ── Unknown/stale callback: stop the spinner and return to a safe menu ──
    log.warning("Unhandled callback: %r", data)
    _status(
        cid,
        "⚠️ این گزینه دیگر معتبر نیست؛ لطفاً از منوی اصلی دوباره انتخاب کنید.",
        reply_markup=main_menu_kb(), final=True,
    )


def handle_message_images_query(cid: int, text: str, st: dict):
    """Called from handle_message when mode==images_query."""
    set_state(cid, mode="images_source", last_query=text)
    send_message(
        cid,
        f"🖼 منبع عکس برای _{text}_ را انتخاب کنید:",
        parse_mode="Markdown",
        reply_markup=image_source_kb(),
    )


# Patch handle_message dispatch for images_query
_orig_dispatch_key = "images_query"

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN DISPATCH
# ═══════════════════════════════════════════════════════════════════════════════
# ── Thread pools ─────────────────────────────────────────────────────────────
# One shared executor for update handlers (downloads, searches, etc.)
# Each platform has its own polling thread; handler threads are shared.
from concurrent.futures import ThreadPoolExecutor

_UPDATE_WORKERS = int(os.getenv("MAX_CONCURRENT_UPDATES", "12"))
_UPDATE_OUTSTANDING = int(os.getenv("MAX_QUEUED_UPDATES", "24"))
_update_executor = ThreadPoolExecutor(max_workers=_UPDATE_WORKERS, thread_name_prefix="upd")
_update_admission = _threading.BoundedSemaphore(_UPDATE_OUTSTANDING)
_update_chat_locks: dict[tuple[str, int], dict] = {}
_update_chat_locks_lock = _threading.Lock()


def _update_chat_id(update: dict):
    body = update.get("message") or (update.get("callback_query") or {}).get("message") or {}
    return (body.get("chat") or {}).get("id")


def _chat_update_lock(platform: str, update: dict):
    cid = _update_chat_id(update)
    if cid is None:
        return None, _threading.Lock()
    key = (platform, cid)
    with _update_chat_locks_lock:
        entry = _update_chat_locks.setdefault(key, {"lock": _threading.Lock(), "pending": 0})
        entry["pending"] += 1
        return key, entry["lock"]


def _submit_update(executor, platform: str, update: dict):
    """Bound backlog and serialize one chat without retaining idle chat locks."""
    _update_admission.acquire()
    key, lock = _chat_update_lock(platform, update)

    def run():
        try:
            with lock:
                return _handle_update_safe(platform, update)
        finally:
            if key is not None:
                with _update_chat_locks_lock:
                    entry = _update_chat_locks.get(key)
                    if entry:
                        entry["pending"] -= 1
                        if not entry["pending"]:
                            _update_chat_locks.pop(key, None)
            _update_admission.release()

    return executor.submit(run)


def _handle_update_safe(platform: str, update: dict):
    """Set platform context then dispatch — runs inside the shared thread pool."""
    cid = _update_chat_id(update)
    _set_platform_ctx(platform, chat_id=cid)
    try:
        handle_update(update)
    except _BrowserBusy:
        if cid:
            _status(cid, _BUSY_TEXT, reply_markup=home_kb(), final=True)
    except Exception as e:
        log.error("[%s] handle_update: %s", platform, e, exc_info=True)
        if cid:
            # Finalize any stuck status bubble so it stops spinning/typing.
            try:
                _status(cid, "❌ " + friendly_error(e),
                        reply_markup=home_kb(), final=True)
            except Exception:
                pass
            _notify_bale_error(cid)


def handle_update(update: dict):
    if "message" in update:
        handle_message(update["message"])
    elif "callback_query" in update:
        handle_callback(update["callback_query"])


def _poll_platform(platform: str, token: str):
    """Long-poll loop for one platform — runs in its own thread.

    Uses a persistent requests.Session for TCP keep-alive (big speed win on
    Telegram). getUpdates long-poll timeout=25s is the Telegram recommendation
    — a read timeout on the poll itself is not an error, just retry immediately.
    Handler errors do NOT increment the failure counter so a bad update doesn't
    back off the whole polling loop.
    """
    _set_platform_ctx(platform)
    sess = _session()  # persistent session for this platform
    base_url = _base_url()
    log.info("[%s] Polling started — token …%s  url=%s", platform, token[-6:], base_url)
    offset = 0
    failures = 0
    while True:
        try:
            r = sess.post(
                f"{base_url}/getUpdates",
                json={"offset": offset, "timeout": 25, "limit": 100},
                timeout=35,  # slightly over long-poll timeout so TCP doesn't cut early
            )
            resp = _safe_json(r)
            if not resp.get("ok"):
                failures += 1
                log.warning(
                    "[%s] getUpdates not ok (#%d): %s", platform, failures, resp
                )
                time.sleep(min(5 * failures, 60))
                continue
            failures = 0
            for update in resp.get("result", []):
                offset = update["update_id"] + 1
                _submit_update(_update_executor, platform, update)
        except requests.exceptions.ReadTimeout:
            # Normal for long-poll — server held the connection open but no updates
            pass
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            failures += 1
            log.warning(
                "[%s] getUpdates connection error (#%d): %s", platform, failures, e
            )
            time.sleep(min(3 * failures, 30))
        except Exception as e:
            failures += 1
            log.error(
                "[%s] Polling error (#%d): %s", platform, failures, e, exc_info=True
            )
            time.sleep(min(5 * failures, 60))


def _init_durable_store():
    durable_store.init_db()
    # Revoke only the two accidental cross-platform dev grants; preserve pro tiers
    # and unrelated grants. Keep correction and intended grants atomic.
    now = int(time.time())
    with durable_store._get_conn() as conn:
        conn.executemany(
            "UPDATE user_tiers SET tier='normal', updated_at=? "
            "WHERE platform=? AND chat_id=? AND tier='dev'",
            [(now, "telegram", 867555398), (now, "bale", 96734183)],
        )
        conn.executemany(
            "INSERT INTO user_tiers (platform, chat_id, tier, created_at, updated_at) "
            "VALUES (?, ?, 'dev', ?, ?) ON CONFLICT(platform, chat_id) DO UPDATE "
            "SET tier='dev', updated_at=excluded.updated_at",
            [(platform, cid, now, now) for platform, cid in
             (("telegram", 96734183), ("bale", 867555398))],
        )
        conn.commit()


def run():
    instance_lock = _acquire_instance_lock()
    if instance_lock is None:
        log.critical("Another bot process already owns /tmp/baleghorbanbot.lock; exiting")
        return
    try:
        _init_durable_store()
    except Exception as e:
        log.critical("SQLite initialization failed: %s", e)
        return
    threads = []

    if BALE_TOKEN:
        log.info("Bale platform enabled — token …%s", BALE_TOKEN[-6:])
        t = _tl_threading.Thread(
            target=_poll_platform,
            args=("bale", BALE_TOKEN),
            name="poll-bale",
            daemon=True,
        )
        t.start()
        threads.append(t)
    else:
        log.warning("BALE_TOKEN not set — Bale platform disabled")

    if TELEGRAM_TOKEN:
        log.info("Telegram platform enabled — token …%s", TELEGRAM_TOKEN[-6:])
        t = _tl_threading.Thread(
            target=_poll_platform,
            args=("telegram", TELEGRAM_TOKEN),
            name="poll-telegram",
            daemon=True,
        )
        t.start()
        threads.append(t)
    else:
        log.warning("TELEGRAM_TOKEN not set — Telegram platform disabled")

    if not threads:
        log.critical("No tokens set! Set BALE_TOKEN and/or TELEGRAM_TOKEN in .env")
        return

    _start_rss_scheduler()
    log.info("Bot running on %d platform(s). Press Ctrl+C to stop.", len(threads))
    try:
        while True:
            time.sleep(1)
            # Restart any dead polling thread
            for t in threads:
                if not t.is_alive():
                    log.error("Polling thread %s died — restarting", t.name)
                    platform = "bale" if "bale" in t.name else "telegram"
                    token = BALE_TOKEN if platform == "bale" else TELEGRAM_TOKEN
                    new_t = _tl_threading.Thread(
                        target=_poll_platform,
                        args=(platform, token),
                        name=t.name,
                        daemon=True,
                    )
                    new_t.start()
                    threads[threads.index(t)] = new_t
    except KeyboardInterrupt:
        log.info("Bot stopped.")
        _update_executor.shutdown(wait=False)

    _warp_selfcheck()


if __name__ == "__main__":
    run()
