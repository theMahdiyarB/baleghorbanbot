#!/usr/bin/env python3
"""
بله قربان — Bale Bot  (v2.11)
Full-featured web assistant for Bale messenger.
"""

import os, re, io, json, time, zipfile, logging, tempfile, hashlib
import requests, subprocess, urllib.parse, pytesseract, threading
from datetime import datetime
from pathlib import Path
from typing import Optional
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from PIL import Image
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════════
# ── Multi-platform config ───────────────────────────────────────────────────
# Both Bale and Telegram run simultaneously in separate polling threads.
# Each thread sets _ctx (thread-local) so api/send_* use the right endpoint.
import threading as _tl_threading
_ctx = _tl_threading.local()   # _ctx.base_url, _ctx.platform, _ctx.kb_*

BALE_TOKEN     = os.getenv("BALE_TOKEN", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")

# Per-platform constants (used by whichever platform is active in the thread)
def _make_session() -> requests.Session:
    """Create a requests.Session with connection pooling and retry adapter."""
    from requests.adapters import HTTPAdapter
    s = requests.Session()
    adapter = HTTPAdapter(
        pool_connections=4,
        pool_maxsize=20,
        max_retries=0,   # we do our own retry logic in api()
    )
    s.mount("https://", adapter)
    s.mount("http://",  adapter)
    return s

_PLATFORM_CFG = {
    "bale": {
        "base_url":   f"https://tapi.bale.ai/bot{BALE_TOKEN}",
        "kb_rows":    10,
        "kb_cols":    3,
        "chunk_size": 19 * 1024 * 1024,   # Bale: 20MB limit → 19MB chunks
        "session":    _make_session(),
    },
    "telegram": {
        "base_url":   f"https://api.telegram.org/bot{TELEGRAM_TOKEN}",
        "kb_rows":    8,
        "kb_cols":    3,
        "chunk_size": 49 * 1024 * 1024,   # Telegram: 50MB limit → 49MB chunks
        "session":    _make_session(),
    },
}

def _set_platform_ctx(platform: str):
    """Called at the start of each polling/handler thread — sets thread-local context."""
    cfg = _PLATFORM_CFG[platform]
    _ctx.platform    = platform
    _ctx.base_url    = cfg["base_url"]
    _ctx.kb_rows     = cfg["kb_rows"]
    _ctx.kb_cols     = cfg["kb_cols"]
    _ctx.chunk_size  = cfg["chunk_size"]
    _ctx.session     = cfg["session"]   # shared persistent Session (thread-safe reads)

# Helpers to read thread-local context — fall back to Bale defaults
def _base_url() -> str:
    return getattr(_ctx, "base_url", _PLATFORM_CFG["bale"]["base_url"])

def _platform() -> str:
    return getattr(_ctx, "platform", "bale")

def _session() -> requests.Session:
    return getattr(_ctx, "session", _PLATFORM_CFG["bale"]["session"])

def _chunk_size() -> int:
    return getattr(_ctx, "chunk_size", 19 * 1024 * 1024)

# Keyboard limit helpers — use thread-local per-platform values
@property
def _KB_MAX_ROWS():
    return getattr(_ctx, "kb_rows", 10)

KB_MAX_ROWS = 10   # default fallback (replaced per-thread via _ctx)
KB_MAX_COLS = 3
KB_MAX_BUTTONS = KB_MAX_ROWS * KB_MAX_COLS

# Legacy TOKEN for any remaining single-platform references
TOKEN = BALE_TOKEN or TELEGRAM_TOKEN

MAX_FILE_SIZE  = 20 * 1024 * 1024

# yt-dlp binary — resolved from PATH at runtime
YTDLP_BIN = "yt-dlp"
MAX_IMAGE_SIZE = 10 * 1024 * 1024
MAX_OCR_SIZE   =  5 * 1024 * 1024
GITHUB_TOKEN   = os.getenv("GITHUB_TOKEN", "")

# YouTube: export cookies with:
#   yt-dlp --cookies-from-browser chrome --cookies /path/yt_cookies.txt https://youtube.com
YOUTUBE_COOKIES_FILE = os.getenv("YOUTUBE_COOKIES_FILE", "")

# Telegram MTProto (for reading channels). Get from https://my.telegram.org
TG_API_ID   = os.getenv("TG_API_ID", "")
TG_API_HASH = os.getenv("TG_API_HASH", "")
TG_SESSION  = os.getenv("TG_SESSION_FILE", "tg_session")  # session file path

# Twitter: cookies file for yt-dlp (log into twitter.com in browser first)
TWITTER_COOKIES_FILE = os.getenv("TWITTER_COOKIES_FILE", "")

# Instagram: optional login credentials
INSTAGRAM_USER         = os.getenv("INSTAGRAM_USER", "")
INSTAGRAM_PASS         = os.getenv("INSTAGRAM_PASS", "")
INSTAGRAM_COOKIES_FILE = os.getenv("INSTAGRAM_COOKIES_FILE", "")

# Spotify: optional client credentials for spotdl (helps with playlists/albums)
# Get from: https://developer.spotify.com/dashboard
SPOTIFY_CLIENT_ID     = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")

# RapidAPI removed — consistently returned 403, replaced by probe-first strategy.

# Cloudflare WARP proxy — set WARP_PROXY=socks5://127.0.0.1:40000 to route
# yt-dlp and HTTP requests through WARP (avoids datacenter IP blocks).
# Install: https://pkg.cloudflareclient.com/ then: warp-cli set-mode proxy && warp-cli connect
WARP_PROXY = os.getenv("WARP_PROXY", "")  # e.g. "socks5://127.0.0.1:40000"

# Cobalt API — self-hosted instance for reliable social/YouTube downloads.
# Self-host: https://github.com/imputnet/cobalt
# Default assumes local instance on port 9000.
# Set COBALT_URL= (empty) to disable, or point to a remote instance.
COBALT_URL = os.getenv("COBALT_URL", "http://localhost:9000")

# VirusTotal — free tier: 4 req/min, 500 req/day, 32MB max
# Get your key at https://www.virustotal.com/gui/join-us
VIRUSTOTAL_API_KEY = os.getenv("VIRUSTOTAL_API_KEY", "")

ZLIB_DOMAINS = [
    "https://z-library.sk",
    "https://z-lib.fm",
    "https://z-lib.id",
    "https://zlibrary.to",
]
ZLIB_EMAIL    = os.getenv("ZLIB_EMAIL", "")
ZLIB_PASSWORD = os.getenv("ZLIB_PASSWORD", "")
_zlib_client  = None   # shared AsyncZlib instance (initialized on first use)


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
def _make_session(proxy: str = "") -> requests.Session:
    s = requests.Session()
    r = Retry(total=3, backoff_factor=0.4,
              status_forcelist=[429, 500, 502, 503, 504],
              allowed_methods=["GET", "POST", "HEAD"])
    s.mount("https://", HTTPAdapter(max_retries=r))
    s.mount("http://",  HTTPAdapter(max_retries=r))
    if proxy:
        try:
            s.proxies = {"http": proxy, "https": proxy}
            # Test SOCKS support early to give a clear error
            import socks  # noqa — just check it's importable
        except ImportError:
            log.warning("PySocks not installed — WARP proxy disabled. "
                        "Run: pip install PySocks --break-system-packages")
            s.proxies = {}
    return s

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
_state_lock  = _threading.Lock()
_cache_lock  = _threading.Lock()
_stats_lock  = _threading.Lock()
_url_lock    = _threading.Lock()

user_state: dict[int, dict] = {}
user_stats: dict[int, dict] = {}
# Cache pending search results so callback buttons can refer to them
result_cache: dict[str, list] = {}   # key → list[dict]

# ═══════════════════════════════════════════════════════════════════════════════
# BALE API HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

# Chunk size matches index.js: 19 MB per part
CHUNK_SIZE = 19 * 1024 * 1024

# Extensions Bale accepts natively — no ZIP wrapping needed (mirrors index.js)
EXEMPT_EXT = {
    "jpg","jpeg","png","gif","webp","bmp","tiff","tif","svg","ico","heic","heif","avif",
    "mp4","mkv","avi","mov","wmv","flv","webm","m4v","3gp","ts","mts",
    "mp3","ogg","wav","flac","m4a","aac","wma","opus","aiff",
    "zip","rar","7z","tar","gz","bz2","xz","zst","lz4",
    "pdf","doc","docx","xls","xlsx","ppt","pptx","odt","ods","odp","epub",
}

MIME_MAP = {
    "jpg":"image/jpeg","jpeg":"image/jpeg","png":"image/png","gif":"image/gif",
    "webp":"image/webp","mp4":"video/mp4","mkv":"video/x-matroska",
    "mov":"video/quicktime","avi":"video/x-msvideo","webm":"video/webm",
    "mp3":"audio/mpeg","ogg":"audio/ogg","wav":"audio/wav","flac":"audio/flac",
    "m4a":"audio/mp4","aac":"audio/aac","opus":"audio/opus",
    "pdf":"application/pdf","zip":"application/zip","7z":"application/x-7z-compressed",
    "txt":"text/plain","html":"text/html","json":"application/json",
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
    """Non-exempt extensions should be ZIP-wrapped so Bale accepts them.
    Strips trailing .part so yt-dlp partial files route correctly.
    """
    return _real_ext(filename) not in EXEMPT_EXT

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
            if code in (429, 500, 502, 503, 504) or r.status_code >= 500:
                log.warning("api %s: server error %s — retry %d/%d in %ds",
                            method, result, attempt+1, _retries, delay)
                time.sleep(delay)
                delay = min(delay * 2, 30)
                last_err = result
                continue
            # Permanent client error — don't retry
            log.error("api %s: %s", method, result)
            return result
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.ChunkedEncodingError) as e:
            log.warning("api %s: connection error %s — retry %d/%d in %ds",
                        method, e, attempt+1, _retries, delay)
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
            json={"chat_id": chat_id,
                  "text": "⚠️ سرور در حال حاضر پاسخ نمی‌دهد. لطفاً چند دقیقه دیگر دوباره امتحان کنید."},
            timeout=15,
        )
    except Exception:
        pass

def send_message(chat_id, text, reply_markup=None,
                 reply_to_message_id=None, parse_mode=None) -> dict:
    kw = dict(chat_id=chat_id, text=str(text)[:4096])
    if reply_markup:
        kw["reply_markup"] = (json.dumps(reply_markup)
                               if not isinstance(reply_markup, str)
                               else reply_markup)
    if reply_to_message_id:
        kw["reply_to_message_id"] = reply_to_message_id
    if parse_mode:
        kw["parse_mode"] = parse_mode
    return api("sendMessage", **kw)

def _post_file(endpoint: str, field: str, filename: str,
               data_bytes: bytes, extra_data: dict,
               _retries: int = 3) -> bool:
    """Low-level multipart file POST to Bale API with retry."""
    delay = 3
    for attempt in range(_retries):
        files = {field: (filename, io.BytesIO(data_bytes), _mime(filename))}
        try:
            r = _session().post(f"{_base_url()}/{endpoint}", data=extra_data,
                              files=files, timeout=180)
            resp = _safe_json(r)
            ok = resp.get("ok", False)
            if ok:
                return True
            code = resp.get("error_code", 0)
            if code in (429, 500, 502, 503, 504) or r.status_code >= 500:
                log.warning("%s: server error %s — retry %d/%d in %ds",
                            endpoint, resp, attempt+1, _retries, delay)
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            log.error("%s failed: %s  file=%s  size=%dKB",
                      endpoint, resp, filename, len(data_bytes)//1024)
            return False
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.ChunkedEncodingError) as e:
            log.warning("%s: connection error %s — retry %d/%d in %ds",
                        endpoint, e, attempt+1, _retries, delay)
            time.sleep(delay)
            delay = min(delay * 2, 30)
        except Exception as e:
            log.error("%s exception: %s  file=%s", endpoint, e, filename)
            return False
    log.error("%s: all %d retries failed for %s", endpoint, _retries, filename)
    return False

def _video_meta(data_bytes: bytes, filename: str) -> dict:
    """
    Extract video duration (seconds) and generate a thumbnail frame via ffmpeg.
    Returns dict with optional keys: duration (int), thumb_bytes (bytes).
    Fast — runs in <1s for normal clips.
    """
    import shutil as _sh
    ffmpeg  = _sh.which("ffmpeg")  or "/usr/bin/ffmpeg"
    ffprobe = _sh.which("ffprobe") or "/usr/bin/ffprobe"
    result  = {}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / filename
            src.write_bytes(data_bytes)

            # Duration
            probe = subprocess.run(
                [ffprobe, "-v", "error",
                 "-show_entries", "format=duration:stream=width,height",
                 "-of", "json", str(src)],
                capture_output=True, text=True, timeout=15)
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
                    result["width"]  = w
                    result["height"] = h

            # Thumbnail — grab frame at 1s (or 0 if short)
            seek = min(1, result.get("duration", 0) // 2) if result.get("duration", 0) > 2 else 0
            thumb_path = Path(tmp) / "thumb.jpg"
            tf = subprocess.run(
                [ffmpeg, "-y", "-ss", str(seek), "-i", str(src),
                 "-frames:v", "1", "-q:v", "5",
                 "-vf", "scale=320:-2",
                 str(thumb_path)],
                capture_output=True, timeout=15)
            if tf.returncode == 0 and thumb_path.exists() and thumb_path.stat().st_size > 100:
                result["thumb_bytes"] = thumb_path.read_bytes()
    except Exception as e:
        log.debug("_video_meta: %s", e)
    return result


def _send_one_chunk(chat_id, data_bytes: bytes, filename: str,
                    caption="", media_type="document") -> bool:
    """Send a single chunk using the correct Bale endpoint.
    For videos: extracts duration + thumbnail frame via ffmpeg for proper player display.
    Uses _real_ext() so .part files from yt-dlp route as video/audio correctly.
    """
    ext = _real_ext(filename)
    extra = {"chat_id": str(chat_id), "caption": caption[:1024]}

    if media_type == "video" or ext in {"mp4","mkv","avi","mov","webm","m4v"}:
        extra["supports_streaming"] = "true"
        # Extract duration + thumbnail for proper video player display
        meta = _video_meta(data_bytes, filename)
        if meta.get("duration"):
            extra["duration"] = str(meta["duration"])
        if meta.get("width"):
            extra["width"]  = str(meta["width"])
            extra["height"] = str(meta["height"])
        thumb_bytes = meta.get("thumb_bytes")
        if thumb_bytes:
            # thumb must be sent as a separate multipart field
            delay = 3
            for attempt in range(3):
                files = {
                    "video": (filename,    io.BytesIO(data_bytes), _mime(filename)),
                    "thumbnail": ("thumb.jpg", io.BytesIO(thumb_bytes), "image/jpeg"),
                }
                try:
                    r = _session().post(f"{_base_url()}/sendVideo",
                                        data=extra, files=files, timeout=180)
                    resp = _safe_json(r)
                    if resp.get("ok"):
                        return True
                    code = resp.get("error_code", 0)
                    if code in (429, 500, 502, 503, 504) or r.status_code >= 500:
                        time.sleep(delay); delay = min(delay*2, 30); continue
                    log.error("sendVideo+thumb failed: %s  file=%s", resp, filename)
                    break
                except Exception as e:
                    log.warning("sendVideo+thumb: %s retry %d", e, attempt+1)
                    time.sleep(delay); delay = min(delay*2, 30)
            else:
                log.error("sendVideo+thumb: all retries failed for %s", filename)
            # Fall through to plain sendVideo if thumb attempt completely failed
            return _post_file("sendVideo", "video", filename, data_bytes, extra)
        return _post_file("sendVideo", "video", filename, data_bytes, extra)
    elif media_type == "audio" or ext in {"mp3","ogg","wav","flac","m4a","aac","opus"}:
        return _post_file("sendAudio", "audio", filename, data_bytes, extra)
    elif media_type == "photo" or ext in {"jpg","jpeg","png","gif","webp"}:
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
    import shutil as _sh
    ffmpeg  = _sh.which("ffmpeg")  or "/usr/bin/ffmpeg"
    ffprobe = _sh.which("ffprobe") or "/usr/bin/ffprobe"

    # Strip .part suffix before working with the file
    clean_name = filename[:-5] if filename.lower().endswith(".part") else filename

    with tempfile.TemporaryDirectory() as tmp:
        src_path = Path(tmp) / clean_name
        src_path.write_bytes(data)

        # Get duration
        try:
            probe = subprocess.run(
                [ffprobe, "-v", "error",
                 "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1",
                 str(src_path)],
                capture_output=True, text=True, timeout=30)
            duration = float(probe.stdout.strip() or "0")
        except Exception as e:
            log.error("_split_video_ffmpeg: ffprobe failed: %s", e)
            return [(data, filename)]

        if duration <= 0:
            log.warning("_split_video_ffmpeg: zero duration, cannot split")
            return [(data, filename)]

        total_mb = len(data) / 1024 / 1024
        n        = max(2, int(total_mb / max_mb) + 1)
        seg_dur  = duration / n
        base     = clean_name.rsplit(".", 1)[0] if "." in clean_name else clean_name
        ext      = ("." + clean_name.rsplit(".", 1)[1]) if "." in clean_name else ".mp4"

        log.info("_split_video_ffmpeg: %.1fMB / %.1fs → %d parts × %.1fs each",
                 total_mb, duration, n, seg_dur)

        chunks = []
        for i in range(n):
            out_path = Path(tmp) / f"{base}.part{i+1}of{n}{ext}"
            cmd = [
                ffmpeg, "-y",
                "-ss", str(i * seg_dur),
                "-i", str(src_path),
                "-t", str(seg_dur),
                "-c", "copy",
                "-movflags", "+faststart",
                str(out_path),
            ]
            proc = subprocess.run(cmd, capture_output=True, timeout=300)
            if proc.returncode != 0 or not out_path.exists() or out_path.stat().st_size < 1000:
                log.error("_split_video_ffmpeg: part %d/%d failed rc=%d: %s",
                          i+1, n, proc.returncode,
                          proc.stderr.decode(errors="replace")[:300])
                return [(data, filename)]   # give up, return unsplit
            cb = out_path.read_bytes()
            chunks.append((cb, out_path.name))
            log.info("_split_video_ffmpeg: part %d/%d → %.1fMB", i+1, n, len(cb)/1024/1024)

        return chunks


def smart_send(chat_id, data: bytes, filename: str,
               caption="", media_type="auto") -> bool:
    """
    Smart file sender:
    - Non-exempt extension        → ZIP-wrap first
    - file ≤ CHUNK_SIZE           → send in one shot
    - video > CHUNK_SIZE          → ffmpeg time-split, every part is a playable MP4
    - other file > CHUNK_SIZE     → raw byte-split + cat instructions
    Handles yt-dlp .part suffix transparently throughout.
    """
    if not data:
        log.error("smart_send: empty data for %s", filename)
        return False

    log.info("smart_send: %s  %.1fMB  type=%s",
             filename, len(data)/1024/1024, media_type)

    # Determine real extension (strips .part if present)
    ext = _real_ext(filename)
    is_video = media_type == "video" or ext in {"mp4","mkv","avi","mov","webm","m4v"}

    # Step 1: ZIP-wrap if the real extension isn't exempt.
    # Skip if it's already a numbered part (part1of3) to avoid double-wrapping.
    import re as _re
    is_numbered_part = bool(_re.search(r'\.part\d+of\d+\.', filename))
    if not is_numbered_part and _should_wrap(filename):
        log.info("smart_send: wrapping %s in ZIP", filename)
        send_message(chat_id, f"📦 در حال زیپ کردن `{filename}`…", parse_mode="Markdown")
        data, filename = _wrap_zip(data, filename)
        ext = _real_ext(filename)
        is_video = False
        log.info("smart_send: zipped → %s  %.1fMB", filename, len(data)/1024/1024)

    total_size = len(data)

    # Step 2: Small enough — send in one shot
    chunk_limit = _chunk_size()
    if total_size <= chunk_limit:
        return _send_one_chunk(chat_id, data, filename, caption, media_type)

    # Step 3: Large video → ffmpeg time-split (each part = self-contained playable MP4)
    if is_video:
        send_message(chat_id, "⏳ در حال تقسیم ویدیو…")
        # Telegram supports 50MB — only split on Bale
        if _platform() == "telegram":
            return _send_one_chunk(chat_id, data, filename, caption, media_type)
        parts = _split_video_ffmpeg(data, filename, max_mb=18.0)

        if len(parts) > 1:
            n = len(parts)
            send_message(chat_id,
                         f"📤 ویدیو بزرگ است — ارسال در *{n} بخش*\n"
                         f"_(هر بخش مستقلاً قابل پخش است)_",
                         parse_mode="Markdown")
            all_ok = True
            for i, (part_data, part_name) in enumerate(parts):
                send_message(chat_id,
                             f"📤 ارسال بخش {i+1} از {n} "
                             f"({len(part_data)/1024/1024:.1f}MB)…")
                ok = _send_one_chunk(chat_id, part_data, part_name,
                                     caption=(caption if i == 0 else ""),
                                     media_type="video")
                if not ok:
                    send_message(chat_id, f"❌ ارسال بخش {i+1} ناموفق بود.")
                    all_ok = False
                    break
            if all_ok:
                send_message(chat_id,
                             f"✅ همه {n} بخش ارسال شدند.\n"
                             f"_(هر بخش را مستقیم پخش کنید)_",
                             parse_mode="Markdown")
            return all_ok
        # ffmpeg failed → fall through to byte-split as last resort

    # Step 4: Non-video or ffmpeg fallback → raw byte-split
    total_chunks = (total_size + CHUNK_SIZE - 1) // CHUNK_SIZE
    base2 = filename.rsplit(".", 1)[0] if "." in filename else filename
    ext2  = ("." + filename.rsplit(".", 1)[1]) if "." in filename else ""
    log.info("smart_send: byte-splitting %s into %d chunks", filename, total_chunks)
    send_message(chat_id,
                 f"📤 فایل بزرگ است — ارسال در *{total_chunks} بخش*…",
                 parse_mode="Markdown")
    all_ok = True
    for i in range(total_chunks):
        start = i * CHUNK_SIZE
        end   = min(start + CHUNK_SIZE, total_size)
        chunk = data[start:end]
        chunk_name = f"{base2}.part{i+1}of{total_chunks}{ext2}"
        send_message(chat_id,
                     f"📤 ارسال بخش {i+1} از {total_chunks} "
                     f"({len(chunk)/1024/1024:.1f}MB)…")
        ok = _send_one_chunk(chat_id, chunk, chunk_name,
                             caption="", media_type="document")
        if not ok:
            send_message(chat_id, f"❌ ارسال بخش {i+1} ناموفق بود.")
            all_ok = False
            break
    if all_ok:
        send_message(chat_id,
                     f"✅ همه {total_chunks} بخش ارسال شدند!\n\n"
                     f"برای ترکیب:\n"
                     f"`cat {base2}.part*of{total_chunks}{ext2} > {filename}`",
                     parse_mode="Markdown")
    return all_ok

# Convenience wrappers (keep old call sites working)
def send_document(chat_id, file_bytes: bytes, filename: str,
                  caption="", reply_to=None) -> bool:
    return smart_send(chat_id, file_bytes, filename, caption, media_type="document")

def send_video(chat_id, video_bytes: bytes, filename: str, caption="") -> bool:
    return smart_send(chat_id, video_bytes, filename, caption, media_type="video")

def send_photo(chat_id, img_bytes: bytes, caption="", reply_markup=None) -> bool:
    """Photos don't chunk — just post directly with retry."""
    if not img_bytes:
        return False
    ext = "jpg"
    data = {"chat_id": str(chat_id), "caption": caption[:1024]}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    delay = 2
    for attempt in range(3):
        try:
            files = {"photo": (f"image.{ext}", io.BytesIO(img_bytes), "image/jpeg")}
            r = _session().post(f"{_base_url()}/sendPhoto", data=data,
                              files=files, timeout=60)
            resp = _safe_json(r)
            if resp.get("ok"):
                return True
            code = resp.get("error_code", 0)
            if code in (429, 500, 502, 503, 504) or r.status_code >= 500:
                time.sleep(delay); delay = min(delay*2, 30); continue
            log.error("sendPhoto failed: %s", resp)
            return False
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            log.warning("sendPhoto retry %d: %s", attempt+1, e)
            time.sleep(delay); delay = min(delay*2, 30)
        except Exception as e:
            log.error("sendPhoto exception: %s", e)
            return False
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
    # Bale/Telegram limits: max 10 items per group
    items = items[:10]

    # Build multipart: media JSON + file fields
    media_list = []
    files      = {}
    for i, item in enumerate(items):
        field = f"file{i}"
        is_vid = item.get("is_video", False)
        mtype  = "video" if is_vid else "photo"
        entry: dict = {"type": mtype, "media": f"attach://{field}"}
        if i == 0 and item.get("caption"):
            entry["caption"] = item["caption"][:1024]
        if is_vid:
            entry["supports_streaming"] = True
            meta = _video_meta(item["data"], item["fname"])
            if meta.get("duration"):
                entry["duration"] = meta["duration"]
            if meta.get("width"):
                entry["width"]  = meta["width"]
                entry["height"] = meta["height"]
        media_list.append(entry)
        mime = "video/mp4" if is_vid else "image/jpeg"
        files[field] = (item["fname"], io.BytesIO(item["data"]), mime)

    data   = {"chat_id": str(chat_id), "media": json.dumps(media_list)}
    delay  = 3
    for attempt in range(3):
        try:
            # Reset BytesIO positions for retry
            for f in files.values():
                f[1].seek(0)
            r = _session().post(f"{_base_url()}/sendMediaGroup",
                                data=data, files=files, timeout=180)
            resp = _safe_json(r)
            if resp.get("ok"):
                log.info("send_media_group: OK %d items to %d", len(items), chat_id)
                return True
            code = resp.get("error_code", 0)
            if code in (429, 500, 502, 503, 504) or r.status_code >= 500:
                time.sleep(delay); delay = min(delay * 2, 30); continue
            log.error("sendMediaGroup failed: %s", resp)
            break
        except Exception as e:
            log.warning("sendMediaGroup attempt %d: %s", attempt + 1, e)
            time.sleep(delay); delay = min(delay * 2, 30)

    # Fallback: send individually
    log.warning("send_media_group: falling back to individual sends")
    sent = 0
    for item in items:
        mtype = "video" if item.get("is_video") else "photo"
        cap   = item.get("caption", "")
        ok    = smart_send(chat_id, item["data"], item["fname"],
                           caption=cap, media_type=mtype)
        if ok:
            sent += 1
    return sent > 0

def chat_action(chat_id, action="typing"):
    if not chat_id or chat_id == 0:
        return
    try:
        _session().post(f"{_base_url()}/sendChatAction",
                      json={"chat_id": chat_id, "action": action},
                      timeout=5)
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


def download_bytes(url: str, max_bytes=MAX_FILE_SIZE) -> Optional[bytes]:
    if not url or url.strip() in ("", "NA", "na", "none", "None", "null"):
        log.debug("download_bytes: skipping invalid URL %r", url)
        return None
    log.debug("download_bytes: %s", url)
    try:
        r = WEB.get(url, timeout=30, stream=True)
        chunks, total = [], 0
        for chunk in r.iter_content(8192):
            chunks.append(chunk); total += len(chunk)
            if total > max_bytes:
                log.warning("download_bytes: exceeded %d bytes", max_bytes)
                return None
        return b"".join(chunks)
    except Exception as e:
        log.error("download_bytes: %s — %s", url, e)
        return None

def answer_cb(cb_id: str, text=""):
    try: api("answerCallbackQuery", callback_query_id=cb_id, text=text)
    except Exception: pass

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

def make_cache_key(prefix: str, query: str, page: int) -> str:
    h = hashlib.md5(f"{prefix}:{query}:{page}".encode()).hexdigest()[:8]
    return f"{prefix}_{h}"

# ═══════════════════════════════════════════════════════════════════════════════
# KEYBOARDS
# ═══════════════════════════════════════════════════════════════════════════════
def main_menu_kb():
    return {"inline_keyboard": [
        [{"text": "🔎 جستجو در وب",      "callback_data": "mode_search"},
         {"text": "🌐 مشاهده سایت",      "callback_data": "mode_open"}],
        [{"text": "📚 مقاله علمی",        "callback_data": "mode_scholar"},
         {"text": "📖 ویکی‌پدیا",         "callback_data": "mode_wiki"}],
        [{"text": "📺 یوتیوب",            "callback_data": "mode_youtube"},
         {"text": "🎵 دانلود موزیک",     "callback_data": "mode_music"}],
        [{"text": "📱 دانلود APK",        "callback_data": "mode_apk"},
         {"text": "📚 Z-Library کتاب",   "callback_data": "mode_zlib"}],
        [{"text": "🖼 دانلود عکس",        "callback_data": "mode_images"},
         {"text": "🐙 GitHub",            "callback_data": "mode_github"}],
        [{"text": "📰 اخبار RSS",         "callback_data": "mode_rss"},
         {"text": "🌐 ترجمه",             "callback_data": "mode_translate"}],
        [{"text": "🖼 OCR متن از عکس",   "callback_data": "mode_ocr"},
         {"text": "🌐 IP / دامنه",        "callback_data": "mode_iplookup"}],
        [{"text": "🛡 تشخیص ویروس",      "callback_data": "mode_virusscan"},
         {"text": "🔒 حریم خصوصی",       "callback_data": "privacy"}],
        # Full-width social networks button
        [{"text": "📲  شبکه‌های اجتماعی  📲", "callback_data": "mode_social"}],
        [{"text": "📊 آمار کاربری",       "callback_data": "stats"},
         {"text": "❓ راهنما",             "callback_data": "help"}],
    ]}


def social_menu_kb():
    """Sub-menu for all social media platforms."""
    return {"inline_keyboard": [
        [{"text": "🐦 توییتر / X",        "callback_data": "mode_twitter"},
         {"text": "📸 اینستاگرام",        "callback_data": "mode_instagram"}],
        [{"text": "🎵 تیک‌تاک",           "callback_data": "mode_tiktok"},
         {"text": "🦋 بلواسکای",           "callback_data": "mode_bluesky"}],
        [{"text": "✈️ کانال تلگرام",      "callback_data": "mode_tg_channel"}],
        [{"text": "🏠 منوی اصلی",         "callback_data": "home"}],
    ]}

def cancel_kb():
    return {"inline_keyboard": [[{"text": "❌ انصراف", "callback_data": "cancel"}]]}

def home_kb():
    return {"inline_keyboard": [[{"text": "🏠 منوی اصلی", "callback_data": "home"}]]}

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
    return {"inline_keyboard": [
        [{"text": "🖼 تصاویر Bing",     "callback_data": "img_src_bing"},
         {"text": "📌 پینترست",          "callback_data": "img_src_pinterest"}],
        [{"text": "📷 Pixabay/Pexels",   "callback_data": "img_src_pexels"},
         {"text": "🎨 Wikimedia",        "callback_data": "img_src_wiki"}],
        [{"text": "❌ انصراف",            "callback_data": "cancel"}],
    ]}

def youtube_action_kb():
    return {"inline_keyboard": [
        [{"text": "📥 دانلود ویدیو",    "callback_data": "yt_video"},
         {"text": "🔍 جستجوی یوتیوب",  "callback_data": "yt_search"}],
        [{"text": "🖼 کاور (Thumbnail)", "callback_data": "yt_thumb"},
         {"text": "📊 آمار ویدیو",      "callback_data": "yt_stats"}],
        [{"text": "❌ انصراف",           "callback_data": "cancel"}],
    ]}

def github_action_kb():
    return {"inline_keyboard": [
        [{"text": "🔍 جستجوی مخازن",   "callback_data": "gh_search"},
         {"text": "📥 دانلود ZIP",      "callback_data": "gh_zip"}],
        [{"text": "📦 دانلود Release",  "callback_data": "gh_release"}],
        [{"text": "❌ انصراف",           "callback_data": "cancel"}],
    ]}

def tg_channel_kb():
    return {"inline_keyboard": [
        [{"text": "📖 خواندن پیام‌ها (عمومی)",  "callback_data": "tg_read_web"},
         {"text": "🔐 خواندن با MTProto",        "callback_data": "tg_read_mtproto"}],
        [{"text": "📥 دانلود رسانه پیام",        "callback_data": "tg_dl_media"}],
        [{"text": "❌ انصراف",                    "callback_data": "cancel"}],
    ]}

def twitter_kb():
    return {"inline_keyboard": [
        [{"text": "📰 پیام‌های کاربر",   "callback_data": "tw_timeline"},
         {"text": "📥 دانلود ویدیو/عکس","callback_data": "tw_dl"}],
        [{"text": "🇮🇷 دانلود + ترجمه",  "callback_data": "tw_dl_translate"}],
        [{"text": "❌ انصراف",            "callback_data": "cancel"}],
    ]}

def instagram_kb():
    return {"inline_keyboard": [
        [{"text": "📋 پست‌های پروفایل",  "callback_data": "ig_profile"},
         {"text": "📥 دانلود پست/ریل",   "callback_data": "ig_dl"}],
        [{"text": "🖼 عکس پروفایل",      "callback_data": "ig_profile_pic"},
         {"text": "📸 اسکرین‌شات پروفایل","callback_data": "ig_profile_shot"}],
        [{"text": "🔲 موزاییک پست",      "callback_data": "ig_mosaic"}],
        [{"text": "❌ انصراف",            "callback_data": "cancel"}],
    ]}

def tiktok_kb():
    return {"inline_keyboard": [
        [{"text": "🎥 ویدیوهای کاربر",   "callback_data": "tt_user"},
         {"text": "📥 دانلود ویدیو",     "callback_data": "tt_dl"}],
        [{"text": "❌ انصراف",            "callback_data": "cancel"}],
    ]}

def zlib_kb():
    """منوی اصلی Z-Library."""
    return {"inline_keyboard": [
        [{"text": "🔍 جستجوی کتاب",   "callback_data": "zlib_search"},
         {"text": "🔍 جستجوی مقاله",  "callback_data": "zlib_search_art"}],
        [{"text": "📄 فقط PDF",        "callback_data": "zlib_filter_pdf"},
         {"text": "📖 فقط EPUB",       "callback_data": "zlib_filter_epub"}],
        [{"text": "📝 فقط FB2/MOBI",   "callback_data": "zlib_filter_other"}],
        [{"text": "❌ انصراف",          "callback_data": "cancel"}],
    ]}


def zlib_results_kb(results: list, cache_key: str) -> dict:
    """نتایج Z-Library به‌صورت دکمه."""
    rows = []
    for i, book in enumerate(results[:10]):
        name    = book.get("name", f"کتاب {i+1}")[:38]
        ext     = book.get("extension", "").upper()
        size    = book.get("size", "")
        label   = f"📚 {name}"
        if ext:  label += f" [{ext}]"
        if size: label += f" {size}"
        rows.append([{"text": label[:60],
                       "callback_data": f"zlib_item_{cache_key}_{i}"}])
    rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
    return {"inline_keyboard": rows}


def zlib_book_kb(book_url_key: str) -> dict:
    """دکمه دانلود کتاب."""
    return {"inline_keyboard": [
        [{"text": "📥 دانلود کتاب",  "callback_data": f"zlib_dl_{book_url_key}"}],
        [{"text": "🔙 برگشت",        "callback_data": "zlib_back"},
         {"text": "🏠 منوی اصلی",   "callback_data": "home"}],
    ]}


def apk_kb():
    """منوی APK Downloader."""
    return {"inline_keyboard": [
        [{"text": "🔍 جستجوی اپ",    "callback_data": "apk_search"},
         {"text": "📦 دانلود مستقیم","callback_data": "apk_direct"}],
        [{"text": "❌ انصراف",         "callback_data": "cancel"}],
    ]}


def apk_results_kb(results: list, cache_key: str) -> dict:
    """نتایج جستجوی Google Play به‌صورت دکمه."""
    rows = []
    for i, app in enumerate(results[:8]):
        title = app.get("title", f"App {i+1}")[:30]
        score = app.get("score", 0)
        stars = f"⭐{score:.1f}" if score else ""
        price = "" if app.get("free", True) else f" 💰{app.get('price','')}"
        label = f"📱 {title} {stars}{price}".strip()[:55]
        rows.append([{"text": label, "callback_data": f"apk_item_{cache_key}_{i}"}])
    rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
    return {"inline_keyboard": rows}


def apk_item_kb(app_id: str) -> dict:
    """دکمه‌های اقدام برای یک اپ."""
    if not app_id:
        return {"inline_keyboard": [[{"text": "🏠 منوی اصلی", "callback_data": "home"}]]}
    safe_id = app_id.replace(".", "_")
    return {"inline_keyboard": [
        [{"text": "📥 دانلود APK",      "callback_data": f"apk_dl_{safe_id}"},
         {"text": "🌐 صفحه Play Store", "callback_data": f"apk_store_{safe_id}"}],
        [{"text": "🔙 برگشت به نتایج",  "callback_data": "apk_back"},
         {"text": "🏠 منوی اصلی",       "callback_data": "home"}],
    ]}




def social_results_kb(results: list, cache_key: str, platform: str) -> dict:
    """Generic results keyboard for social media posts (tg/tw/ig/tt)."""
    icon_map = {"tg": "✈️", "tw": "🐦", "ig": "📸", "tt": "🎵"}
    icon = icon_map.get(platform, "📌")
    rows = []
    for i, r in enumerate(results[:15]):
        text = r.get("text", r.get("title", ""))
        first_line = text.split("\n")[0].strip()[:38] if text else f"پیام {i+1}"
        media_icons = ""
        if r.get("has_photo") or r.get("img_urls"): media_icons += "🖼"
        if r.get("has_video") or r.get("is_video"): media_icons += "🎬"
        label = f"{icon}{media_icons} {first_line}".strip()[:52]
        rows.append([{"text": label, "callback_data": f"soc_{platform}_{cache_key}_{i}"}])
    rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
    return {"inline_keyboard": rows}


def social_post_kb(url: str, platform: str, has_media: bool) -> dict:
    """Buttons under a displayed post."""
    url_key = store_url(url)
    rows = []
    if has_media:
        rows.append([{"text": "📥 دانلود رسانه", "callback_data": f"soc_dl_{platform}_{url_key}"}])
    rows.append([{"text": "🔗 باز کردن لینک",  "callback_data": f"site_ss_{url_key}"},
                 {"text": "🏠 منوی اصلی",       "callback_data": "home"}])
    return {"inline_keyboard": rows}


def translate_kb():
    langs = [("🇮🇷 فارسی","fa"),("🇬🇧 انگلیسی","en"),
             ("🇸🇦 عربی","ar"),("🇩🇪 آلمانی","de"),
             ("🇫🇷 فرانسوی","fr"),("🇷🇺 روسی","ru")]
    rows, row = [], []
    for label, code in langs:
        row.append({"text": label, "callback_data": f"trlang_{code}"})
        if len(row) == 2:
            rows.append(row); row = []
    if row: rows.append(row)
    rows.append([{"text": "❌ انصراف", "callback_data": "cancel"}])
    return {"inline_keyboard": rows}

def site_view_kb(url_key: str):
    """Buttons shown after screenshot of a website."""
    return {"inline_keyboard": [
        [{"text": "📝 متن صفحه",  "callback_data": f"site_text_{url_key}"},
         {"text": "🌐 فایل HTML", "callback_data": f"site_html_{url_key}"}],
        [{"text": "🗜 ZIP آفلاین", "callback_data": f"site_zip_{url_key}"},
         {"text": "📑 PDF صفحه",  "callback_data": f"site_pdf_{url_key}"}],
        [{"text": "🏠 منوی اصلی", "callback_data": "home"}],
    ]}

def search_results_kb(results: list, cache_key: str, page: int,
                      has_next: bool, mode: str) -> dict:
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
        pag.append({"text": "◀️ قبلی", "callback_data": f"page_{mode}_{page-1}"})
    if has_next:
        pag.append({"text": "بعدی ▶️", "callback_data": f"page_{mode}_{page+1}"})
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
        dur = f" [{r.get('duration','')}]" if r.get("duration") else ""
        rows.append([{"text": f"{num}. {title}{dur}", "callback_data": f"yt_res_{cache_key}_{i}"}])
    pag = []
    if page > 0:
        pag.append({"text": "◀️ قبلی", "callback_data": f"yt_page_{page-1}"})
    if has_next:
        pag.append({"text": "بعدی ▶️", "callback_data": f"yt_page_{page+1}"})
    if pag: rows.append(pag)
    rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
    return {"inline_keyboard": rows}

def gh_repo_kb(results: list, cache_key: str, page: int, has_next: bool) -> dict:
    rows = []
    offset = page * 8
    for i, r in enumerate(results):
        num = offset + i + 1
        name = r.get("full_name", f"repo {num}")[:40]
        stars = r.get("stargazers_count", 0)
        rows.append([{"text": f"{num}. {name} ⭐{stars}",
                       "callback_data": f"gh_res_{cache_key}_{i}"}])
    pag = []
    if page > 0:
        pag.append({"text": "◀️ قبلی", "callback_data": f"gh_page_{page-1}"})
    if has_next:
        pag.append({"text": "بعدی ▶️", "callback_data": f"gh_page_{page+1}"})
    if pag: rows.append(pag)
    rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
    return {"inline_keyboard": rows}

def gh_repo_action_kb(repo_full: str) -> dict:
    safe = repo_full.replace("/", "__")
    return {"inline_keyboard": [
        [{"text": "📥 دانلود ZIP کل مخزن",     "callback_data": f"ghact_zip_{safe}"},
         {"text": "📦 آخرین Release",           "callback_data": f"ghact_rel_{safe}"}],
        [{"text": "📋 اطلاعات بیشتر",           "callback_data": f"ghact_info_{safe}"}],
        [{"text": "🔙 برگشت به نتایج",          "callback_data": "gh_back"}],
    ]}

def images_more_kb(cache_key: str, page: int, source: str) -> dict:
    return {"inline_keyboard": [
        [{"text": "📥 دانلود بیشتر", "callback_data": f"img_more_{source}_{cache_key}_{page+1}"}],
        [{"text": "🔄 منبع دیگر",    "callback_data": "mode_images"},
         {"text": "🏠 منوی اصلی",    "callback_data": "home"}],
    ]}

def wiki_result_kb(results: list, cache_key: str, lang: str) -> dict:
    rows = []
    for i, r in enumerate(results):
        title = r.get("title", f"مقاله {i+1}")[:50]
        rows.append([{"text": f"📖 {title}", "callback_data": f"wiki_art_{cache_key}_{i}_{lang}"}])
    rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
    return {"inline_keyboard": rows}

# ═══════════════════════════════════════════════════════════════════════════════
# WEB SCRAPING FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════
UA_DESK = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
           "AppleWebKit/537.36 (KHTML, like Gecko) "
           "Chrome/122.0.0.0 Safari/537.36")
UA_MOB  = ("Mozilla/5.0 (Linux; Android 13; SM-G991B) "
           "AppleWebKit/537.36 (KHTML, like Gecko) "
           "Chrome/122.0.6261.119 Mobile Safari/537.36")

def web_search(query: str, max_results=10, page=0) -> list[dict]:
    log.info("web_search: %r  page=%d", query, page)
    data = {"q": query, "b": "", "kl": "wt-wt"}
    if page > 0:
        data.update({"s": str(page*30), "dc": str(page*30+1), "v": "l", "o": "json"})
    try:
        r = WEB.post("https://html.duckduckgo.com/html/", data=data,
                     headers={"User-Agent": UA_DESK, "Accept-Language": "en-US,en;q=0.9"},
                     timeout=20)
        log.debug("DDG: status=%d len=%d", r.status_code, len(r.text))
        soup = BeautifulSoup(r.text, "html.parser")
        results = []
        for div in soup.select(".result, .web-result"):
            ta = div.select_one(".result__title a, .result__a, h2 a")
            if not ta: continue
            href = ta.get("href","")
            m = re.search(r"uddg=([^&]+)", href)
            link = urllib.parse.unquote(m.group(1)) if m else href
            if not link.startswith("http"): continue
            sn = div.select_one(".result__snippet, .result__body")
            results.append({"title": ta.get_text(strip=True),
                             "link": link,
                             "snippet": sn.get_text(strip=True) if sn else ""})
            if len(results) >= max_results: break
        log.info("web_search: %d results", len(results))
        if not results:
            log.warning("DDG returned 0 results. HTML head: %s", r.text[:300])
        return results
    except Exception as e:
        log.error("web_search error: %s", e, exc_info=True)
        return []

def fetch_page(url: str) -> Optional[bytes]:
    log.info("fetch_page: %s", url)
    headers = {"User-Agent": UA_DESK,
               "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
               "Accept-Language": "en-US,en;q=0.9"}
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
            log.error("fetch_page SSL fallback: %s", e); return None
    except Exception as e:
        log.error("fetch_page: %s", e, exc_info=True); return None


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

def screenshot_page(url: str) -> Optional[bytes]:
    """
    1920×1080 screenshot via shot-scraper with popup/banner removal.
    Falls back to playwright if shot-scraper unavailable.
    """
    log.info("screenshot_page: %s", url)
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
            out = tf.name
        cmd = [
            "shot-scraper", "shot", url,
            "-o", out,
            "--width", "1920",
            "--height", "1080",
            "--quality", "85",
            "--wait", "2000",
            "--javascript", _CLEANUP_JS,
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=45)
        if result.returncode == 0 and Path(out).exists() and Path(out).stat().st_size > 1000:
            data = Path(out).read_bytes()
            Path(out).unlink(missing_ok=True)
            log.info("screenshot_page (shot-scraper): %dKB", len(data)//1024)
            return data
        log.warning("shot-scraper rc=%d: %s", result.returncode,
                    result.stderr.decode(errors="replace")[:200])
        Path(out).unlink(missing_ok=True)
    except FileNotFoundError:
        log.warning("shot-scraper not found, falling back to playwright")
    except subprocess.TimeoutExpired:
        log.warning("screenshot_page: shot-scraper timed out")
    except Exception as e:
        log.error("screenshot_page shot-scraper: %s", e)

    # Fallback: playwright
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(args=[
                "--no-sandbox", "--disable-setuid-sandbox",
                "--disable-dev-shm-usage", "--disable-gpu",
            ])
            page = browser.new_page(viewport={"width": 1920, "height": 1080})
            page.goto(url, timeout=25000, wait_until="domcontentloaded")
            time.sleep(2)
            page.evaluate(_CLEANUP_JS)
            img = page.screenshot(type="jpeg", quality=85,
                                   clip={"x":0,"y":0,"width":1920,"height":1080})
            browser.close()
        log.info("screenshot_page (playwright fallback): %dKB", len(img)//1024)
        return img
    except Exception as e:
        log.error("screenshot_page playwright: %s", e, exc_info=True)
        return None

def page_to_zip(url: str) -> Optional[bytes]:
    log.info("page_to_zip: %s", url)
    html = fetch_page(url)
    if not html: return None
    soup = BeautifulSoup(html, "html.parser")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("index.html", html)
        base = f"{urllib.parse.urlparse(url).scheme}://{urllib.parse.urlparse(url).netloc}"
        for tag in soup.find_all(["img","link","script"])[:25]:
            src = tag.get("src") or tag.get("href","")
            if not src or src.startswith("data:"): continue
            asset_url = urllib.parse.urljoin(base, src)
            try:
                ar = WEB.get(asset_url, timeout=8)
                fname = Path(urllib.parse.urlparse(asset_url).path).name or "asset"
                zf.writestr(f"assets/{fname}", ar.content)
            except Exception: pass
    buf.seek(0)
    return buf.read()

def page_to_text(url: str) -> str:
    html = fetch_page(url)
    if not html: return "❌ خطا در دریافت صفحه."
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script","style","nav","footer","aside"]): t.decompose()
    lines = [l for l in soup.get_text("\n", strip=True).split("\n") if l.strip()]
    return "\n".join(lines[:100])

def page_to_pdf(url: str) -> Optional[bytes]:
    """
    Generate a full-page PDF via shot-scraper (uses Playwright's print-to-PDF
    internally — captures the entire scrollable page as a proper multi-page PDF).
    Falls back to wkhtmltopdf, then raw HTML.
    """
    log.info("page_to_pdf: %s", url)

    # Strategy 1: shot-scraper pdf (best — full page, proper PDF, popup removal)
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            out = tf.name
        cmd = [
            "shot-scraper", "pdf", url,
            "-o", out,
            "--wait", "2000",
            "--javascript", _CLEANUP_JS,
            "--media-screen",        # use screen CSS (better fonts/colors)
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=60)
        if result.returncode == 0 and Path(out).exists() and Path(out).stat().st_size > 500:
            data = Path(out).read_bytes()
            Path(out).unlink(missing_ok=True)
            log.info("page_to_pdf (shot-scraper): %dKB", len(data)//1024)
            return data
        log.warning("shot-scraper pdf rc=%d: %s", result.returncode,
                    result.stderr.decode(errors="replace")[:200])
        Path(out).unlink(missing_ok=True)
    except FileNotFoundError:
        log.warning("shot-scraper not found for PDF")
    except subprocess.TimeoutExpired:
        log.warning("page_to_pdf: shot-scraper timed out")
    except Exception as e:
        log.error("page_to_pdf shot-scraper: %s", e)

    # Strategy 2: Playwright print-to-PDF directly
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(args=[
                "--no-sandbox", "--disable-setuid-sandbox",
                "--disable-dev-shm-usage", "--disable-gpu",
            ])
            page = browser.new_page(viewport={"width": 1920, "height": 1080})
            page.goto(url, timeout=30000, wait_until="networkidle")
            time.sleep(2)
            page.evaluate(_CLEANUP_JS)
            pdf_bytes = page.pdf(
                format="A4",
                print_background=True,
                margin={"top":"10mm","bottom":"10mm","left":"10mm","right":"10mm"},
            )
            browser.close()
        if pdf_bytes and len(pdf_bytes) > 500:
            log.info("page_to_pdf (playwright): %dKB", len(pdf_bytes)//1024)
            return pdf_bytes
    except Exception as e:
        log.warning("page_to_pdf playwright: %s", e)

    # Strategy 3: wkhtmltopdf
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            out = tf.name
        subprocess.run(
            ["wkhtmltopdf","--quiet",
             "--load-error-handling","ignore","--no-stop-slow-scripts",
             "--javascript-delay","2000","--page-size","A4",
             "--encoding","utf-8", url, out],
            capture_output=True, timeout=60,
        )
        if Path(out).exists() and Path(out).stat().st_size > 500:
            data = Path(out).read_bytes()
            Path(out).unlink(missing_ok=True)
            log.info("page_to_pdf (wkhtmltopdf): %dKB", len(data)//1024)
            return data
        Path(out).unlink(missing_ok=True)
    except Exception as e:
        log.error("page_to_pdf wkhtmltopdf: %s", e)

    log.warning("page_to_pdf: all strategies failed, returning raw HTML")
    return fetch_page(url)

# ─── Scholar ──────────────────────────────────────────────────────────────────
def scholar_search(query: str, page=0) -> list[dict]:
    log.info("scholar_search: %r page=%d", query, page)
    start = page * 10
    try:
        r = WEB.get("https://scholar.google.com/scholar",
                    params={"q": query, "hl": "en", "num": 10, "start": start},
                    headers={"User-Agent": UA_DESK, "Accept-Language": "en-US,en;q=0.9"},
                    timeout=20)
        log.debug("scholar: status=%d len=%d", r.status_code, len(r.text))
        soup = BeautifulSoup(r.text, "html.parser")
        results = []
        for div in soup.select(".gs_ri"):
            ta = div.select_one(".gs_rt a") or div.select_one("h3 a")
            if not ta: continue
            sn = div.select_one(".gs_rs")
            meta = div.select_one(".gs_a")
            results.append({
                "title": ta.get_text(strip=True),
                "link":  ta.get("href",""),
                "snippet": sn.get_text(strip=True) if sn else "",
                "meta":    meta.get_text(strip=True) if meta else "",
            })
        log.info("scholar_search: %d results", len(results))
        return results
    except Exception as e:
        log.error("scholar_search: %s", e, exc_info=True)
        return []

# ─── Wikipedia ────────────────────────────────────────────────────────────────
def wikipedia_search(query: str, lang="fa") -> list[dict]:
    log.info("wikipedia_search: %r lang=%s", query, lang)
    HDR = {"User-Agent": "BaleBot/1.0"}
    for base in [f"https://{lang}.wikipedia.org",
                 f"https://{lang}.m.wikipedia.org"]:
        try:
            r = WEB.get(f"{base}/w/api.php",
                        params={"action":"opensearch","search":query,
                                "limit":8,"namespace":0,"format":"json"},
                        headers=HDR, timeout=15)
            log.debug("wiki opensearch: status=%d", r.status_code)
            if r.status_code == 200:
                d = r.json()
                titles = d[1] if len(d)>1 else []
                descs  = d[2] if len(d)>2 else []
                urls   = d[3] if len(d)>3 else []
                results = [{"title": t,
                             "snippet": descs[i] if i<len(descs) else "",
                             "url": urls[i] if i<len(urls) else "",
                             "key": t.replace(" ","_"),
                             "lang": lang}
                           for i,t in enumerate(titles)]
                if results:
                    log.info("wiki_search: %d results", len(results))
                    return results
        except Exception as e:
            log.error("wikipedia_search %s: %s", base, e)
    return []

def wikipedia_article(title: str, lang="fa") -> Optional[str]:
    log.info("wikipedia_article: %r lang=%s", title, lang)
    HDR = {"User-Agent": "BaleBot/1.0"}
    key = urllib.parse.quote(title.replace(" ","_"))
    for base in [f"https://{lang}.wikipedia.org",
                 f"https://{lang}.m.wikipedia.org"]:
        try:
            r = WEB.get(f"{base}/api/rest_v1/page/summary/{key}",
                        headers=HDR, timeout=15)
            log.debug("wiki summary: status=%d", r.status_code)
            if r.status_code == 200:
                extract = r.json().get("extract","")
                if extract and len(extract) > 80:
                    # Try full text
                    try:
                        r2 = WEB.get(f"{base}/w/api.php",
                                     params={"action":"query","titles":title,
                                             "prop":"extracts","explaintext":1,
                                             "exsectionformat":"plain",
                                             "format":"json","utf8":1},
                                     headers=HDR, timeout=20)
                        log.debug("wiki full: status=%d", r2.status_code)
                        if r2.status_code == 200:
                            for pg in r2.json().get("query",{}).get("pages",{}).values():
                                full = pg.get("extract","")
                                if full and len(full) > len(extract):
                                    return full
                    except Exception: pass
                    return extract
        except Exception as e:
            log.error("wikipedia_article %s: %s", base, e)
    return None

# ─── YouTube ──────────────────────────────────────────────────────────────────
def youtube_search(query: str, max_results=8) -> list[dict]:
    log.info("youtube_search: %r", query)
    try:
        result = subprocess.run(
            [YTDLP_BIN, "--flat-playlist", "--print",
             "%(id)s|||%(title)s|||%(uploader)s|||%(duration_string)s|||%(thumbnail)s|||%(view_count)s|||%(like_count)s|||%(description)s",
             f"ytsearch{max_results}:{query}", "--no-warnings",
             "--no-check-certificate",
             "--extractor-args", "youtube:lang=en",
             "--geo-bypass-country", "US"],
            capture_output=True, text=True, timeout=30,
        )
        items = []
        for line in result.stdout.strip().split("\n"):
            parts = line.split("|||")
            if len(parts) >= 2 and parts[0].strip():
                vid_id = parts[0].strip()
                items.append({
                    "id":          vid_id,
                    "title":       parts[1].strip() if len(parts)>1 else "",
                    "uploader":    parts[2].strip() if len(parts)>2 else "",
                    "duration":    parts[3].strip() if len(parts)>3 else "",
                    "thumbnail":   parts[4].strip() if len(parts)>4 and parts[4].strip() not in ("NA","")
                                   else f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg",
                    "view_count":  parts[5].strip() if len(parts)>5 else "",
                    "like_count":  parts[6].strip() if len(parts)>6 else "",
                    "description": parts[7].strip()[:300] if len(parts)>7 else "",
                    "url":         f"https://www.youtube.com/watch?v={vid_id}",
                })
        log.info("youtube_search: %d results", len(items))
        return items
    except Exception as e:
        log.error("youtube_search: %s", e, exc_info=True)
        return []

def _yt_probe_format(url: str, client: str, audio_only: bool,
                     cookies_file: str, max_height: int = 720) -> Optional[str]:
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
        YTDLP_BIN, "-j",
        "--no-playlist", "--no-warnings", "--no-check-certificate",
        "--socket-timeout", "20",
        "--extractor-args", f"youtube:player_client={client}",
        "--remote-components", "ejs:github",
        "--js-runtimes", "node",
        "--geo-bypass-country", "US",
    ]
    if cookies_file:
        probe_cmd += ["--cookies", cookies_file]
    if WARP_PROXY:
        probe_cmd += ["--proxy", WARP_PROXY]
    probe_cmd.append(url)

    try:
        proc = subprocess.run(probe_cmd, capture_output=True, timeout=30)
        if proc.returncode != 0:
            log.debug("_yt_probe_format: client=%s rc=%d stderr=%s",
                      client, proc.returncode, proc.stderr.decode(errors="replace")[:200])
            return None
        info = json.loads(proc.stdout.decode(errors="replace"))
        formats = info.get("formats", [])
        if not formats:
            return None

        if audio_only:
            # Best audio-only stream by bitrate (like GH: ba[format_note*=original]/ba)
            audio_fmts = [f for f in formats
                          if f.get("vcodec") == "none" and f.get("acodec") != "none"
                          and f.get("acodec") not in (None, "none")]
            if not audio_fmts:
                # fallback: any format with audio
                audio_fmts = [f for f in formats if f.get("acodec") not in (None, "none")]
            if not audio_fmts:
                return None
            # Prefer "original" quality flag (GH uses format_note*=original), else highest abr
            original = [f for f in audio_fmts
                        if "original" in (f.get("format_note") or "").lower()]
            pool = original if original else audio_fmts
            best = max(pool, key=lambda f: f.get("abr") or f.get("tbr") or 0)
            log.debug("_yt_probe_format: audio fmt=%s abr=%s client=%s",
                      best["format_id"], best.get("abr"), client)
            return best["format_id"]
        else:
            # Combined streams (vcodec+acodec both present) — best height ≤ max_height
            combined = [f for f in formats
                        if f.get("vcodec") not in (None, "none")
                        and f.get("acodec") not in (None, "none")
                        and (f.get("height") or 0) <= max_height]
            if combined:
                # Pick by height desc, then fps desc (GH: max_by(.height, .fps))
                best = max(combined, key=lambda f: ((f.get("height") or 0),
                                                     (f.get("fps") or 0)))
                log.debug("_yt_probe_format: combined fmt=%s h=%s fps=%s client=%s",
                          best["format_id"], best.get("height"), best.get("fps"), client)
                return best["format_id"]

            # No combined — build video+audio merge spec (DASH)
            video_fmts = [f for f in formats
                          if f.get("vcodec") not in (None, "none")
                          and f.get("acodec") in (None, "none")
                          and (f.get("height") or 0) <= max_height]
            audio_fmts = [f for f in formats
                          if f.get("vcodec") == "none"
                          and f.get("acodec") not in (None, "none")]
            if video_fmts and audio_fmts:
                best_v = max(video_fmts, key=lambda f: ((f.get("height") or 0),
                                                         (f.get("fps") or 0)))
                # Prefer original audio quality flag (GH: ba[format_note*=original]/ba)
                orig_a = [f for f in audio_fmts
                          if "original" in (f.get("format_note") or "").lower()]
                best_a = max(orig_a or audio_fmts,
                             key=lambda f: f.get("abr") or f.get("tbr") or 0)
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
    import shutil
    cookies_file = YOUTUBE_COOKIES_FILE if (YOUTUBE_COOKIES_FILE and
                   Path(YOUTUBE_COOKIES_FILE).exists()) else ""

    probe_clients = ["tv_embedded", "android", "ios", "web_music,web,tv,web_embedded"]
    info = None
    used_client = None

    for client in probe_clients:
        probe_cmd = [
            YTDLP_BIN, "-j",
            "--no-playlist", "--no-warnings", "--no-check-certificate",
            "--socket-timeout", "20",
            "--extractor-args", f"youtube:player_client={client}",
            "--remote-components", "ejs:github",
            "--js-runtimes", "node",
            "--geo-bypass-country", "US",
        ]
        if cookies_file:
            probe_cmd += ["--cookies", cookies_file]
        if WARP_PROXY:
            probe_cmd += ["--proxy", WARP_PROXY]
        probe_cmd.append(url)
        try:
            proc = subprocess.run(probe_cmd, capture_output=True, timeout=40)
            if proc.returncode != 0:
                log.debug("youtube_get_formats: client=%s rc=%d stderr=%s",
                          client, proc.returncode, proc.stderr.decode(errors="replace")[:200])
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
        return {}

    formats_raw = info.get("formats", [])

    # ── Build quality list ────────────────────────────────────────────────────
    # Group by height — pick best (highest bitrate) video+audio pair per height
    seen_heights: dict[int, dict] = {}

    video_fmts = [f for f in formats_raw
                  if f.get("vcodec") not in (None, "none")
                  and (f.get("height") or 0) > 0]
    audio_fmts = [f for f in formats_raw
                  if f.get("vcodec") == "none"
                  and f.get("acodec") not in (None, "none")]

    # Best audio stream (prefer "original" flag)
    orig_a = [f for f in audio_fmts
              if "original" in (f.get("format_note") or "").lower()]
    best_audio = max(orig_a or audio_fmts,
                     key=lambda f: f.get("abr") or f.get("tbr") or 0,
                     default=None)

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
                "label":    f"{h}p{fps_label}",
                "height":   h,
                "fps":      fps,
                "fmt_spec": fmt_spec,
                "client":   used_client,
                "_tbr":     tbr,
            }

    quality_list = sorted(seen_heights.values(),
                          key=lambda x: x["height"], reverse=True)
    # Remove internal _tbr key
    for q in quality_list:
        q.pop("_tbr", None)

    # ── Subtitles ─────────────────────────────────────────────────────────────
    subs_raw = {}
    subs_raw.update(info.get("subtitles", {}))
    subs_raw.update(info.get("automatic_captions", {}))  # auto-generated too

    LANG_NAMES = {
        "en": "English", "fa": "فارسی", "ar": "العربية",
        "de": "Deutsch", "fr": "Français", "es": "Español",
        "ru": "Русский", "zh": "中文", "ja": "日本語",
        "ko": "한국어", "tr": "Türkçe", "it": "Italiano",
        "pt": "Português", "nl": "Nederlands", "pl": "Polski",
        "hi": "हिन्दी", "ur": "اردو",
    }
    subtitle_list = []
    for code in sorted(subs_raw.keys()):
        base = code.split("-")[0]
        name = LANG_NAMES.get(base) or LANG_NAMES.get(code) or code.upper()
        is_auto = code in info.get("automatic_captions", {})
        subtitle_list.append({
            "code": code,
            "name": f"{name} {'(auto)' if is_auto else ''}".strip(),
        })

    return {
        "formats":   quality_list,
        "subtitles": subtitle_list[:20],  # cap at 20 languages
        "title":     info.get("title", ""),
        "thumbnail": info.get("thumbnail", ""),
        "duration":  info.get("duration_string", ""),
        "client":    used_client,
    }


def youtube_download(url: str, audio_only=False,
                     fmt_spec: str = "", sub_code: str = "",
                     yt_client: str = "") -> Optional[tuple[bytes, str]]:
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
    log.info("youtube_download: url=%r audio=%s fmt=%r sub=%r",
             url, audio_only, fmt_spec, sub_code)
    import shutil
    ffmpeg_dir = str(Path(shutil.which("ffmpeg") or "/usr/bin/ffmpeg").parent)
    cookies_file = YOUTUBE_COOKIES_FILE if (YOUTUBE_COOKIES_FILE and
                   Path(YOUTUBE_COOKIES_FILE).exists()) else ""

    if cookies_file:
        log.info("youtube_download: using cookies from %s", cookies_file)
    else:
        log.warning("youtube_download: no cookies file — bot-detection likely")

    def _subtitle_args(code: str) -> list:
        """Build yt-dlp args to embed a specific subtitle track."""
        if not code:
            return []
        return [
            "--write-subs", "--write-auto-subs",
            "--sub-langs", code,
            "--sub-format", "srt",
            "--embed-subs",
            "--convert-subs", "srt",
        ]

    def _run(client: str, fmt_spec: str) -> Optional[tuple[bytes, str]]:
        """Download a specific format spec with a given player client."""
        with tempfile.TemporaryDirectory() as tmp:
            cmd = [
                YTDLP_BIN,
                "--no-playlist", "--no-warnings", "--no-check-certificate",
                "--ffmpeg-location", ffmpeg_dir,
                "--socket-timeout", "30", "--retries", "2",
                "--extractor-args", f"youtube:player_client={client}",
                "--remote-components", "ejs:github",
                "--js-runtimes", "node",
                "-f", fmt_spec,
                "--merge-output-format", "mp4",
                "--split-chapters",
                "--max-filesize", "19M",
                "-o", os.path.join(tmp, "%(title)s.%(ext)s"),
            ]
            if cookies_file:
                cmd += ["--cookies", cookies_file]
            if WARP_PROXY:
                cmd += ["--proxy", WARP_PROXY]
            if sub_code:
                cmd += _subtitle_args(sub_code)
            cmd.append(url)
            log.debug("yt-dlp cmd: %s", " ".join(cmd))
            proc = subprocess.run(cmd, capture_output=True, timeout=300)
            if proc.returncode != 0:
                log.error("yt-dlp rc=%d stderr=%s", proc.returncode,
                          proc.stderr.decode(errors="replace")[:300])
                return None
            files = [f for f in Path(tmp).glob("*")
                     if f.stat().st_size > 100 and not f.suffix == ".srt"]
            if not files:
                return None
            f = sorted(files, key=lambda x: x.stat().st_size, reverse=True)[0]
            data = f.read_bytes()
            log.info("yt-dlp OK: %s  %.1fMB", f.name, len(data)/1024/1024)
            return data, f.name

    # ── Fast path: caller already probed and chose a format ──────────────────
    if fmt_spec:
        client = yt_client or "tv_embedded"
        if audio_only:
            with tempfile.TemporaryDirectory() as tmp:
                cmd = [
                    YTDLP_BIN,
                    "--no-playlist", "--no-warnings", "--no-check-certificate",
                    "--ffmpeg-location", ffmpeg_dir,
                    "--socket-timeout", "30", "--retries", "2",
                    "--extractor-args", f"youtube:player_client={client}",
                    "--remote-components", "ejs:github",
                    "--js-runtimes", "node",
                    "-f", fmt_spec,
                    "-x", "--audio-format", "mp3", "--audio-quality", "0",
                    "--split-chapters",
                    "--max-filesize", "19M",
                    "-o", os.path.join(tmp, "%(title)s.%(ext)s"),
                ]
                if cookies_file:
                    cmd += ["--cookies", cookies_file]
                if WARP_PROXY:
                    cmd += ["--proxy", WARP_PROXY]
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
        fmt_spec = _yt_probe_format(url, client, audio_only, cookies_file, max_height=720)
        if not fmt_spec:
            log.debug("youtube_download: no format found for client=%s", client)
            continue
        log.info("youtube_download: trying fmt=%s client=%s", fmt_spec, client)

        if audio_only:
            # For audio, use -x to extract+convert to mp3
            with tempfile.TemporaryDirectory() as tmp:
                cmd = [
                    YTDLP_BIN,
                    "--no-playlist", "--no-warnings", "--no-check-certificate",
                    "--ffmpeg-location", ffmpeg_dir,
                    "--socket-timeout", "30", "--retries", "2",
                    "--extractor-args", f"youtube:player_client={client}",
                    "--remote-components", "ejs:github",
                    "--js-runtimes", "node",
                    "-f", fmt_spec,
                    "-x", "--audio-format", "mp3", "--audio-quality", "0",
                    "--split-chapters",
                    "--max-filesize", "19M",
                    "-o", os.path.join(tmp, "%(title)s.%(ext)s"),
                ]
                if cookies_file:
                    cmd += ["--cookies", cookies_file]
                if WARP_PROXY:
                    cmd += ["--proxy", WARP_PROXY]
                cmd.append(url)
                log.debug("yt-dlp audio cmd: %s", " ".join(cmd))
                proc = subprocess.run(cmd, capture_output=True, timeout=300)
                if proc.returncode == 0:
                    files = [f for f in Path(tmp).glob("*") if f.stat().st_size > 1000]
                    if files:
                        f = files[0]
                        data = f.read_bytes()
                        log.info("yt-dlp audio OK: %s  %.1fMB", f.name, len(data)/1024/1024)
                        return data, f.name
                log.error("yt-dlp audio rc=%d: %s", proc.returncode,
                          proc.stderr.decode(errors="replace")[:200])
        else:
            result = _run(client, fmt_spec)
            if result:
                return result

    # ── Strategy 2: Safety-net broad selector (no probe) ─────────────────────
    # If all probes failed (e.g. network hiccup during -j), try a very permissive
    # selector that lets yt-dlp decide entirely with --format-sort.
    log.warning("youtube_download: all probes failed, trying broad safety-net")
    for client in ("android", "ios", "tv_embedded"):
        with tempfile.TemporaryDirectory() as tmp:
            cmd = [
                YTDLP_BIN,
                "--no-playlist", "--no-warnings", "--no-check-certificate",
                "--ffmpeg-location", ffmpeg_dir,
                "--socket-timeout", "30", "--retries", "2",
                "--extractor-args", f"youtube:player_client={client}",
                "--remote-components", "ejs:github",
                "--js-runtimes", "node",
                "-S", "height:720,ext:mp4:m4a",
                "--merge-output-format", "mp4",
                "--split-chapters",
                "--max-filesize", "19M",
                "-o", os.path.join(tmp, "%(title)s.%(ext)s"),
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
                files = [f for f in Path(tmp).glob("*")
                         if f.stat().st_size > 100 and f.suffix != ".srt"]
                if files:
                    f = sorted(files, key=lambda x: x.stat().st_size, reverse=True)[0]
                    data = f.read_bytes()
                    log.info("yt-dlp safety-net OK: %s  %.1fMB", f.name, len(data)/1024/1024)
                    return data, f.name
            log.error("safety-net rc=%d client=%s: %s", proc.returncode, client,
                      proc.stderr.decode(errors="replace")[:200])

    # ── Strategy 3: Cobalt API fallback ───────────────────────────────────────
    log.warning("youtube_download: yt-dlp exhausted, trying Cobalt fallback")
    cobalt_result = _youtube_cobalt(url, audio_only=audio_only)
    if cobalt_result:
        return cobalt_result

    log.error("youtube_download: all strategies exhausted for %s", url)
    return None


def _cobalt_download(url: str, audio_only: bool = False,
                     quality: str = "720") -> Optional[tuple[bytes, str]]:
    """
    Download single item via Cobalt API.
    For carousels/multiple items use _cobalt_download_all().
    """
    results = _cobalt_download_all(url, audio_only=audio_only, quality=quality)
    if results:
        return results[0]["data"], results[0]["fname"]
    return None


def _cobalt_download_all(url: str, audio_only: bool = False,
                         quality: str = "720") -> list[dict]:
    """
    Download ALL items via Cobalt API (handles carousels, playlists, etc).
    Returns list of {data, fname, is_video} dicts.
    Set COBALT_URL env var to your self-hosted instance.
    """
    if not COBALT_URL:
        log.debug("_cobalt_download_all: COBALT_URL not set, skipping")
        return []
    log.info("_cobalt_download_all: %s audio=%s via %s", url, audio_only, COBALT_URL)
    try:
        payload: dict = {
            "url":          url,
            "videoQuality": quality,
            "filenameStyle": "basic",
            "alwaysProxy":  False,
        }
        if audio_only:
            payload["downloadMode"] = "audio"
            payload["audioFormat"]  = "mp3"
            payload["audioBitrate"] = "192"
        else:
            payload["downloadMode"] = "auto"

        resp = WEB.post(
            f"{COBALT_URL}/",
            json=payload,
            headers={"Accept": "application/json",
                     "Content-Type": "application/json"},
            timeout=30,
        )
        if resp.status_code not in (200, 201):
            log.warning("_cobalt_download_all: status=%d body=%s",
                        resp.status_code, resp.text[:200])
            return []

        j = resp.json()
        status = j.get("status", "")
        log.debug("_cobalt_download_all: status=%s", status)

        if status == "error":
            code = j.get("error", {}).get("code", "unknown")
            log.warning("_cobalt_download_all: API error code=%s", code)
            return []

        def _sniff_type(raw: bytes, item_url: str, content_type_hint: str = "") -> tuple[str, bool]:
            """Return (ext, is_video) by inspecting magic bytes and content-type."""
            ct = content_type_hint.lower()
            # Magic bytes detection (most reliable)
            if raw[:4] == b"\xff\xd8\xff\xe0" or raw[:4] == b"\xff\xd8\xff\xe1":
                return "jpg", False   # JPEG
            if raw[:8] == b"\x89PNG\r\n\x1a\n":
                return "png", False   # PNG
            if raw[:4] in (b"RIFF",) and raw[8:12] == b"WEBP":
                return "webp", False  # WEBP
            if raw[:4] == b"GIF8":
                return "gif", False   # GIF
            # MP4/MOV: ftyp box or moov atom near start
            if raw[4:8] in (b"ftyp", b"moov", b"mdat", b"pnot", b"wide", b"free"):
                return "mp4", True
            if raw[:3] == b"ID3" or raw[:2] == b"\xff\xfb":
                return "mp3", False   # Audio
            # Content-type fallback
            if "image/jpeg" in ct: return "jpg", False
            if "image/png" in ct:  return "png", False
            if "image/webp" in ct: return "webp", False
            if "image/gif" in ct:  return "gif", False
            if "video/" in ct:     return "mp4", True
            if "audio/" in ct:     return "mp3", False
            # URL extension fallback
            url_lower = item_url.lower().split("?")[0]
            for ext, is_vid in [(".jpg","jpg"), (".jpeg","jpg"), (".png","png"),
                                 (".webp","webp"), (".gif","gif"), (".mp4","mp4"),
                                 (".mov","mp4"), (".mp3","mp3")]:
                if url_lower.endswith(ext):
                    return is_vid, ext == ".mp4" or ext == ".mov"
            # Default: assume video if large, image if small
            is_vid = len(raw) > 2_000_000
            return ("mp4" if is_vid else "jpg"), is_vid

        def _fetch_item(item_url: str, is_video_hint: bool = True,
                        ext_hint: str = "") -> Optional[dict]:
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
                log.info("cobalt item OK: %.1fMB %s is_video=%s", len(raw)/1024/1024, fname, is_video)
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
                item_url  = item.get("url", "")
                item_type = item.get("type", "")
                is_video_hint = item_type == "video"
                r = _fetch_item(item_url, is_video_hint=is_video_hint)
                if r:
                    results.append(r)
            log.info("cobalt picker: %d/%d items fetched",
                     len(results), len(j["picker"]))
            return results

        log.warning("_cobalt_download_all: unhandled status=%s j=%s",
                    status, str(j)[:200])
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

def _trim_video(src: Path, tmp: str, ffmpeg_dir="/usr/bin") -> Optional[tuple[bytes,Path]]:
    out = Path(tmp) / ("trimmed_" + src.name)
    ffprobe = str(Path(ffmpeg_dir)/"ffprobe")
    ffmpeg  = str(Path(ffmpeg_dir)/"ffmpeg")
    try:
        probe = subprocess.run([ffprobe,"-v","error","-show_entries","format=duration",
                                "-of","default=noprint_wrappers=1:nokey=1",str(src)],
                               capture_output=True, text=True, timeout=30)
        duration = float(probe.stdout.strip() or "300")
        vbr = max(300, int((48*1024*1024*8)/duration/1000) - 128)
        subprocess.run([ffmpeg,"-y","-i",str(src),"-c:v","libx264","-b:v",f"{vbr}k",
                        "-c:a","aac","-b:a","128k","-movflags","+faststart",str(out)],
                       capture_output=True, timeout=300, check=True)
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
def music_search_ytdlp(query: str, max_results: int = 5,
                        source: str = "youtube") -> list[dict]:
    """Search for tracks via yt-dlp (YouTube, YouTube Music, or SoundCloud)."""
    log.info("music_search_ytdlp: %r source=%s", query, source)
    if source == "soundcloud":
        search_url = f"scsearch{max_results}:{query}"
    elif source == "ytmusic":
        search_url = f"https://music.youtube.com/search?q={urllib.parse.quote(query)}"
    else:
        search_url = f"ytsearch{max_results}:{query}"

    extra = []
    if source in ("youtube", "ytmusic") and YOUTUBE_COOKIES_FILE and Path(YOUTUBE_COOKIES_FILE).exists():
        extra = ["--cookies", YOUTUBE_COOKIES_FILE]

    # For SoundCloud: use webpage_url (the actual SC permalink) not url (raw API URL)
    # For YouTube: webpage_url and url are the same watch?v= link
    url_field = "%(webpage_url)s" if source == "soundcloud" else "%(url)s"

    try:
        if source == "ytmusic":
            cmd = ([YTDLP_BIN, "--flat-playlist", "--no-warnings", "--no-check-certificate",
                    "--ffmpeg-location", _ffmpeg_dir(), "--playlist-items", f"1-{max_results}"]
                   + extra
                   + ["--print",
                      f"%(id)s|||%(title)s|||%(uploader)s|||%(duration_string)s|||%(webpage_url)s|||%(thumbnail)s",
                      f"https://music.youtube.com/search?q={urllib.parse.quote(query)}"])
        else:
            cmd = ([YTDLP_BIN, "--flat-playlist", "--no-warnings", "--no-check-certificate",
                    "--ffmpeg-location", _ffmpeg_dir()]
                   + extra
                   + ["--print",
                      f"%(id)s|||%(title)s|||%(uploader)s|||%(duration_string)s|||{url_field}|||%(thumbnail)s",
                      search_url])

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        items = []
        for line in result.stdout.strip().split("\n"):
            parts = line.split("|||")
            if len(parts) < 2 or not parts[0].strip():
                continue
            vid_id   = parts[0].strip()
            title    = parts[1].strip()
            uploader = parts[2].strip() if len(parts) > 2 else ""
            duration = parts[3].strip() if len(parts) > 3 else ""
            url      = parts[4].strip() if len(parts) > 4 else ""
            thumb    = parts[5].strip() if len(parts) > 5 else ""
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
            items.append({"id": vid_id, "title": title, "uploader": uploader,
                          "duration": duration, "url": url, "thumbnail": thumb,
                          "source": display_source})
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
        r = WEB.get(url, params=params,
                    headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.jiosaavn.com/"},
                    timeout=15)
        if r.status_code != 200:
            log.error("jiosaavn_search: HTTP %d", r.status_code)
            return []
        data = r.json()
        songs = data.get("results", [])
        items = []
        for s in songs[:max_results]:
            title    = s.get("title", s.get("song", ""))
            uploader = s.get("primary_artists", s.get("singers", ""))
            duration = s.get("duration", "")
            song_id  = s.get("id", "")
            # Build perma URL
            perma = s.get("perma_url", "")
            # Encrypted media URL — needed for download
            enc_url = s.get("media_preview_url", s.get("media_url", ""))
            thumb   = s.get("image", "").replace("150x150", "500x500")
            if duration:
                try:
                    secs = int(duration)
                    duration = f"{secs//60}:{secs%60:02d}"
                except Exception:
                    pass
            if not (title and song_id):
                continue
            items.append({
                "id":        song_id,
                "title":     title,
                "uploader":  uploader,
                "duration":  str(duration),
                "url":       perma or f"https://www.jiosaavn.com/song/-/{song_id}",
                "thumbnail": thumb,
                "source":    "jiosaavn",
                "enc_url":   enc_url,
                "perma_url": perma,
            })
        log.info("jiosaavn_search: %d results", len(items))
        return items
    except Exception as e:
        log.error("jiosaavn_search: %s", e)
        return []


def jiosaavn_download(item: dict) -> Optional[tuple[bytes, str]]:
    """Download a JioSaavn track given a search result dict."""
    log.info("jiosaavn_download: %s", item.get("title", "?"))
    try:
        song_id  = item.get("id", "")
        perma    = item.get("perma_url", "")
        title    = item.get("title", "track")
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
            r = WEB.get(detail_url, params=params,
                        headers={"User-Agent": "Mozilla/5.0",
                                 "Referer": "https://www.jiosaavn.com/"},
                        timeout=15)
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
                            safe = re.sub(r'[^\w\s-]', '', title)[:50]
                            fname = f"{safe}.mp3"
                            log.info("jiosaavn_download: OK %.1fMB", len(data)/1024/1024)
                            return data, fname

        log.error("jiosaavn_download: all methods failed for %s", title)
        return None
    except Exception as e:
        log.error("jiosaavn_download: %s", e)
        return None


def _jiosaavn_decrypt(enc_url: str) -> Optional[str]:
    """Decrypt JioSaavn encrypted media URL using their known DES key."""
    try:
        from base64 import b64decode, b64encode
        # JioSaavn uses DES ECB with a known key
        try:
            from Crypto.Cipher import DES
        except ImportError:
            log.warning("jiosaavn_decrypt: pycryptodome not installed, skipping decrypt")
            return None
        key = b"38346591"
        enc_bytes = b64decode(enc_url)
        cipher = DES.new(key, DES.MODE_ECB)
        decrypted = cipher.decrypt(enc_bytes)
        # Remove padding
        url = decrypted.decode("utf-8", errors="ignore").rstrip("\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\x0c\r\x0e\x0f\x10")
        # Upgrade to 320kbps
        url = url.replace("_96.mp4", "_320.mp4").replace("_160.mp4", "_320.mp4")
        log.debug("jiosaavn_decrypt: %s", url[:80])
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
        r = WEB.get(url, params=params,
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=15)
        if r.status_code != 200:
            log.error("deezer_search: HTTP %d", r.status_code)
            return []
        data = r.json()
        items = []
        for track in data.get("data", [])[:max_results]:
            title    = track.get("title", "")
            uploader = track.get("artist", {}).get("name", "")
            duration = track.get("duration", 0)
            track_id = track.get("id", "")
            link     = track.get("link", f"https://www.deezer.com/track/{track_id}")
            thumb    = track.get("album", {}).get("cover_big",
                       track.get("album", {}).get("cover_medium", ""))
            dur_str  = f"{duration//60}:{duration%60:02d}" if duration else ""
            if not (title and track_id):
                continue
            items.append({
                "id":        str(track_id),
                "title":     title,
                "uploader":  uploader,
                "duration":  dur_str,
                "url":       link,
                "thumbnail": thumb,
                "source":    "deezer",
            })
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
    title  = item.get("title", "")
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
        _t.Thread(target=_s, args=("youtube",   music_search_ytdlp, query, 4, "youtube")),
        _t.Thread(target=_s, args=("ytmusic",   music_search_ytdlp, query, 4, "ytmusic")),
        _t.Thread(target=_s, args=("soundcloud",music_search_ytdlp, query, 3, "soundcloud")),
        _t.Thread(target=_s, args=("jiosaavn",  jiosaavn_search,    query, 4)),
        _t.Thread(target=_s, args=("deezer",    deezer_search,      query, 4)),
    ]
    for t in threads: t.start()
    for t in threads: t.join(timeout=25)

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


def music_download_ytdlp(url: str, source: str = "auto") -> Optional[tuple[bytes, str]]:
    """Download audio via yt-dlp → MP3. Returns None on DRM or permanent failure."""
    log.info("music_download_ytdlp: %s source=%s", url, source)
    import shutil
    ffmpeg_dir = str(Path(shutil.which("ffmpeg") or "/usr/bin/ffmpeg").parent)
    is_yt = "youtube.com" in url or "youtu.be" in url
    is_sc = "soundcloud.com" in url

    # Deezer is DRM-protected — never attempt, saves ~4×5s of wasted retries
    if "deezer.com" in url:
        log.warning("music_download_ytdlp: skipping DRM-protected Deezer URL")
        return None

    # Reject raw SoundCloud API URLs (not downloadable via yt-dlp)
    if "api.soundcloud.com" in url and "soundcloud%3A" in url:
        log.warning("music_download_ytdlp: skipping raw SC API URL %s", url)
        return None

    timeout = 120 if is_sc else 300   # SC tracks are small, fail fast

    def _run_audio(extra_args: list) -> Optional[tuple[bytes, str]]:
        with tempfile.TemporaryDirectory() as tmp:
            base = [
                YTDLP_BIN, "--no-playlist", "--no-warnings", "--no-check-certificate",
                "--ffmpeg-location", ffmpeg_dir,
                "--socket-timeout", "20", "--retries", "2",
                "--max-filesize", "19M",
                "-o", os.path.join(tmp, "%(title).60s.%(ext)s"),
            ]
            if is_yt:
                if YOUTUBE_COOKIES_FILE and Path(YOUTUBE_COOKIES_FILE).exists():
                    base += ["--cookies", YOUTUBE_COOKIES_FILE]
                base += ["--extractor-args",
                         "youtube:player_client=android,ios,tv_embedded,web_music,web"]
            if WARP_PROXY:
                base += ["--proxy", WARP_PROXY]
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
            files = [f for f in Path(tmp).glob("*")
                     if f.suffix.lower() in {".mp3", ".m4a", ".ogg", ".opus", ".flac", ".webm"}
                     and f.stat().st_size > 1000]
            if not files:
                return None
            best = max(files, key=lambda f: f.stat().st_size)
            data = best.read_bytes()
            log.info("music dl OK: %s  %.1fMB", best.name, len(data)/1024/1024)
            return data, best.name

    # Format strategy:
    # 1. Best audio → convert to mp3 (works for most public tracks)
    # 2. bestaudio without conversion (fastest, keeps native format like opus/m4a)
    # 3. Any audio extract (-x only)
    # For YouTube "Requested format not available": bestaudio/best bypasses the issue
    format_attempts = [
        ["-f", "bestaudio/best", "-x", "--audio-format", "mp3", "--audio-quality", "0",
         "--embed-thumbnail", "--add-metadata"],
        ["-f", "bestaudio/best", "-x", "--audio-format", "mp3", "--audio-quality", "0"],
        ["-f", "bestaudio/best", "-x"],
        ["-x"],
    ]

    try:
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
    log.info("spotify_download: %s", url)
    import shutil, sys
    ffmpeg_path = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
    # spotdl may be installed as a module even if the binary isn't on PATH
    spotdl_path = (shutil.which("spotdl")
                   or os.path.join(os.path.dirname(sys.executable), "spotdl")
                   or os.path.expanduser("~/.local/bin/spotdl"))
    # Verify it actually exists before trying
    if not os.path.isfile(spotdl_path or ""):
        spotdl_path = "spotdl"  # last resort — let subprocess resolve it
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [spotdl_path, "download", url,
               "--format", "mp3", "--bitrate", "192k",
               "--output", tmp, "--ffmpeg", ffmpeg_path,
               "--log-level", "ERROR"]
        if SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
            cmd += ["--client-id", SPOTIFY_CLIENT_ID,
                    "--client-secret", SPOTIFY_CLIENT_SECRET]
        log.debug("spotdl: %s", " ".join(cmd))
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=300)
            if proc.returncode != 0:
                log.error("spotdl rc=%d: %s", proc.returncode,
                          proc.stderr.decode(errors="replace")[:400])
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
    _tried_yt_ids: set = None,   # internal: YT video IDs already attempted
) -> Optional[tuple[bytes, str]]:
    """
    Full fallback chain: Spotify → YouTube Music → SoundCloud → JioSaavn → Deezer(→YT/SC)
    Tracks already-tried YouTube IDs so the chain never retries the same video twice.
    """
    if _tried_yt_ids is None:
        _tried_yt_ids = set()

    def _cb(msg: str):
        if status_cb:
            try: status_cb(msg)
            except Exception: pass

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
    skip_yt_search = is_url and ("youtube.com" in query_or_url or "youtu.be" in query_or_url)

    if not skip_yt_search and search_q:
        _cb("🎵 در حال جستجو در یوتیوب موزیک…")
        yt_hits = music_search_ytdlp(search_q, max_results=3, source="ytmusic")
        if not yt_hits:
            yt_hits = music_search_ytdlp(search_q, max_results=3, source="youtube")
        for hit in yt_hits:
            vid_id = hit.get("id", "") or hit.get("url", "")
            if vid_id in _tried_yt_ids:
                continue
            _tried_yt_ids.add(vid_id)
            result = music_download_ytdlp(hit["url"], source="youtube")
            if result:
                log.info("fallback: YouTube Music search OK")
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
    log.warning("fallback: SoundCloud failed, trying JioSaavn")
    _cb("⚠️ SoundCloud ناموفق — در حال جستجو در JioSaavn…")

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
    title  = item.get("title", "")
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
def social_download_ytdlp(url: str, audio_only=False,
                           cookies_file: str = "") -> Optional[tuple[bytes, str]]:
    """Download from any yt-dlp-supported URL (TikTok, Instagram, Twitter, etc.)
    Falls back to Cobalt API if yt-dlp fails.
    """
    log.info("social_download_ytdlp: %r  audio=%s", url, audio_only)
    import shutil
    ffmpeg_dir = str(Path(shutil.which("ffmpeg") or "/usr/bin/ffmpeg").parent)
    base = [
        YTDLP_BIN, "--no-playlist", "--no-warnings", "--no-check-certificate",
        "--ffmpeg-location", ffmpeg_dir,
        "--socket-timeout", "30", "--retries", "3",
    ]
    if cookies_file and Path(cookies_file).exists():
        base += ["--cookies", cookies_file]
    if WARP_PROXY:
        base += ["--proxy", WARP_PROXY]

    def _run(extra, target):
        with tempfile.TemporaryDirectory() as tmp:
            cmd = base + ["-o", os.path.join(tmp, "%(title).60s.%(ext)s")] + extra + [target]
            log.debug("social yt-dlp: %s", " ".join(cmd))
            proc = subprocess.run(cmd, capture_output=True, timeout=300)
            if proc.returncode != 0:
                log.warning("social yt-dlp rc=%d: %s",
                            proc.returncode, proc.stderr.decode(errors="replace")[:500])
                return None
            files = [f for f in Path(tmp).glob("*") if f.stat().st_size > 100]
            if not files:
                return None
            f = sorted(files, key=lambda x: x.stat().st_size, reverse=True)[0]
            data = f.read_bytes()
            log.info("social yt-dlp OK: %s  %.1fMB", f.name, len(data)/1024/1024)
            return data, f.name

    if audio_only:
        result = _run(["-x", "--audio-format", "mp3", "--audio-quality", "5"], url)
    else:
        result = None
        for fmt in [
            ["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4"],
            ["-f", "best", "--merge-output-format", "mp4"],
            ["--merge-output-format", "mp4"],
        ]:
            result = _run(fmt, url)
            if result:
                break

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
        r = WEB.get(f"https://t.me/s/{channel}",
                    headers={"User-Agent": UA_DESK,
                             "Accept-Language": "en-US,en;q=0.9"},
                    timeout=20)
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
            has_video = bool(wrap.select(
                ".tgme_widget_message_video_wrap, "
                ".tgme_widget_message_video_player, "
                "video"))
            video_thumb = ""
            video_el = wrap.select_one(".tgme_widget_message_video_wrap")
            if video_el:
                style = video_el.get("style", "")
                m = re.search(r"url\(['\"]?(https?://[^'\")\s]+)['\"]?\)", style)
                if m:
                    video_thumb = m.group(1)

            has_doc = bool(wrap.select(".tgme_widget_message_document"))

            messages.append({
                "id": msg_id,
                "text": text,
                "date": date[:16].replace("T", " "),
                "url": msg_url,
                "img_urls": img_urls,
                "has_photo": bool(img_urls),
                "has_video": has_video,
                "video_thumb": video_thumb,
                "has_doc": has_doc,
            })
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
        from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument, MessageMediaWebPage
        client = TelegramClient(TG_SESSION, int(TG_API_ID), TG_API_HASH)
        await client.start()
        messages = []
        async for msg in client.iter_messages(channel, limit=limit):
            media = getattr(msg, "media", None)
            has_photo = isinstance(media, MessageMediaPhoto)
            has_doc   = isinstance(media, MessageMediaDocument)
            has_video = has_doc and getattr(getattr(media, "document", None),
                                            "mime_type", "").startswith("video/")
            messages.append({
                "id": str(msg.id),
                "text": (msg.text or ""),
                "date": str(msg.date)[:16],
                "url": f"https://t.me/{channel.lstrip('@')}/{msg.id}",
                "img_urls": [],
                "has_photo": has_photo,
                "has_video": has_video,
                "video_thumb": "",
                "has_doc": has_doc and not has_video,
                "_msg_obj": msg,      # kept for media download
                "_client": None,      # filled by caller
            })
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
            r = WEB.get(embed_url,
                        headers={"User-Agent": UA_DESK,
                                 "Accept": "text/html",
                                 "Referer": "https://t.me/"},
                        timeout=20)
            log.debug("tg_embed: status=%d len=%d url=%s", r.status_code, len(r.text), embed_url)
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
                if candidate and ("telesco.pe" in candidate
                                  or "cdn.telegram.org" in candidate
                                  or candidate.startswith("http")):
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
                    raw_ext  = ext_el.get_text(strip=True).lower() if ext_el else ""
                    if raw_name:
                        if raw_ext and not raw_name.endswith(raw_ext):
                            raw_name = f"{raw_name}.{raw_ext}"
                        doc_fname = re.sub(r"[^\w.\-]", "_", raw_name)[:80]
                    break

            if doc_url:
                log.info("tg_embed: downloading document %s from %s",
                         doc_fname, doc_url[:80])
                doc_bytes = download_bytes(doc_url, 200*1024*1024)
                if doc_bytes and len(doc_bytes) > 10:
                    log.info("tg_embed: document OK %dKB %s",
                             len(doc_bytes)//1024, doc_fname)
                    return doc_bytes, doc_fname

            # ── Video ───────────────────────────────────────────────────────
            video_el = soup.select_one("video source[src], video[src]")
            if video_el:
                vurl = video_el.get("src", "")
                if vurl and vurl.startswith("http"):
                    vbytes = download_bytes(vurl, 200*1024*1024)
                    if vbytes and len(vbytes) > 1000:
                        fname = f"tg_{channel}_{msg_id}.mp4"
                        log.info("tg_embed: video %.1fMB", len(vbytes)/1024/1024)
                        return vbytes, fname

            # ── Photo ───────────────────────────────────────────────────────
            img_urls = []
            for el in soup.select(".tgme_widget_message_photo_wrap, "
                                   ".tgme_widget_message_photo"):
                style = el.get("style", "")
                for m2 in re.finditer(
                    r"url\(['\"]?(https?://[^'\")\s]+\.(?:jpg|jpeg|png|webp))['\"]?\)",
                    style, re.I):
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
                    log.info("tg_embed: image %dKB", len(img_bytes)//1024)
                    return img_bytes, fname

        except Exception as e:
            log.warning("tg_embed %s: %s", embed_url, e)

    # Strategy 2: t.me/s/ HTML scrape (before=msg_id+1 to get exact message)
    try:
        preview_url = f"https://t.me/s/{channel}?before={int(msg_id)+1}"
        r2 = WEB.get(preview_url, headers={"User-Agent": UA_DESK}, timeout=20)
        log.debug("tg_scrape: status=%d len=%d", r2.status_code, len(r2.text))
        if r2.status_code == 200:
            soup2 = BeautifulSoup(r2.text, "html.parser")
            for wrap in soup2.select(".tgme_widget_message_wrap"):
                div = wrap.select_one(".tgme_widget_message")
                if not div:
                    continue
                post_id = div.get("data-post","")
                if not post_id.endswith(f"/{msg_id}"):
                    continue
                # Images via background-image
                for photo_el in wrap.select(".tgme_widget_message_photo_wrap"):
                    style = photo_el.get("style","")
                    mi = re.search(r"url\(['\"]?(https?://[^'\")\s]+)['\"]?\)", style)
                    if mi:
                        img_bytes = download_bytes(mi.group(1), MAX_IMAGE_SIZE)
                        if img_bytes and len(img_bytes) > 500:
                            ext = mi.group(1).rsplit(".",1)[-1].split("?")[0] or "jpg"
                            fname = f"tg_{channel}_{msg_id}.{ext}"
                            log.info("tg_scrape: image %dKB", len(img_bytes)//1024)
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
    import time, concurrent.futures
    now = time.time()
    if _nitter_cache and (now - _nitter_cache_time) < 1800:
        return _nitter_cache

    def _check(inst: str) -> Optional[str]:
        try:
            r = WEB.get(f"{inst}/x", timeout=4,
                        headers={"User-Agent": UA_MOB}, allow_redirects=True)
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
            r = WEB.get(url, headers={"User-Agent": UA_MOB,
                                       "Accept-Language": "en-US,en;q=0.9"},
                        timeout=15)
            log.debug("nitter %s: status=%d len=%d", instance, r.status_code, len(r.text))
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
                tweet_url = (f"https://twitter.com{tweet_path}"
                             if tweet_path.startswith("/") else tweet_path)
                nitter_url = f"{instance}{tweet_path}" if tweet_path else ""
                has_video = bool(item.select(".attachment.video-container, .gif"))
                img_tags  = item.select(".still-image img, .attachment.image img")
                img_urls  = [f"{instance}{img.get('src','')}"
                             if img.get("src","").startswith("/")
                             else img.get("src","")
                             for img in img_tags if img.get("src")]
                tweets.append({
                    "text": text, "date": date[:19].replace("T"," "),
                    "url": tweet_url, "nitter_url": nitter_url,
                    "has_video": has_video, "has_photo": bool(img_urls),
                    "img_urls": img_urls,
                })
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
            headers={"User-Agent": UA_DESK}, timeout=15)
        log.debug("twitter syndication: status=%d", r2.status_code)
        if r2.status_code == 200:
            m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
                          r2.text, re.S)
            if m:
                jdata = json.loads(m.group(1))
                entries = (jdata.get("props",{}).get("pageProps",{})
                               .get("timeline",{}).get("entries",[]))
                tweets = []
                for entry in entries[:limit]:
                    tw = entry.get("content",{}).get("tweet",{})
                    if not tw: continue
                    tw_id  = tw.get("id_str","")
                    media  = tw.get("extended_entities",{}).get("media",[]) or []
                    img_urls = [md.get("media_url_https","") for md in media
                                if md.get("type") == "photo"]
                    tweets.append({
                        "text": tw.get("full_text") or tw.get("text",""),
                        "date": tw.get("created_at","")[:19],
                        "url": f"https://twitter.com/{username}/status/{tw_id}",
                        "nitter_url": "", "has_video": False,
                        "has_photo": bool(img_urls), "img_urls": img_urls,
                    })
                if tweets:
                    log.info("twitter syndication: %d tweets", len(tweets))
                    return tweets
    except Exception as e:
        log.warning("twitter syndication: %s", e)

    # Strategy 3: xcancel RSS
    try:
        r3 = WEB.get(f"https://xcancel.com/{username}/rss",
                     headers={"User-Agent": UA_DESK}, timeout=15)
        log.debug("xcancel rss: status=%d", r3.status_code)
        if r3.status_code == 200:
            soup = BeautifulSoup(r3.content, "xml")
            tweets = []
            for item in soup.select("item")[:limit]:
                title   = (item.find("title") or {}).get_text(strip=True)
                link    = (item.find("link") or {}).get_text(strip=True)
                desc    = BeautifulSoup(
                    (item.find("description") or {}).get_text(strip=True),
                    "html.parser").get_text(" ", strip=True)
                pubdate = (item.find("pubDate") or {}).get_text(strip=True)
                tweets.append({
                    "text": f"{title}\n{desc}".strip(),
                    "date": pubdate[:19], "url": link, "nitter_url": "",
                    "has_video": False, "has_photo": False, "img_urls": [],
                })
            if tweets:
                log.info("xcancel RSS: %d tweets", len(tweets))
                return tweets
    except Exception as e:
        log.warning("xcancel rss: %s", e)

    # Strategy 4: bird.makeup RSS (another reliable Nitter-compatible frontend)
    try:
        r4 = WEB.get(f"https://bird.makeup/users/{username}/feed.atom",
                     headers={"User-Agent": UA_DESK}, timeout=15)
        log.debug("bird.makeup: status=%d", r4.status_code)
        if r4.status_code == 200:
            soup = BeautifulSoup(r4.content, "xml")
            tweets = []
            for entry in soup.select("entry")[:limit]:
                title   = (entry.find("title") or {}).get_text(strip=True)
                link_el = entry.find("link")
                link    = link_el.get("href","") if link_el else ""
                content = BeautifulSoup(
                    (entry.find("content") or {}).get_text(strip=True),
                    "html.parser").get_text(" ", strip=True)
                updated = (entry.find("updated") or {}).get_text(strip=True)
                tweets.append({
                    "text": f"{title}\n{content}".strip(),
                    "date": updated[:19], "url": link, "nitter_url": "",
                    "has_video": False, "has_photo": False, "img_urls": [],
                })
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
                media_list = (tweet.get("media", {}).get("all", []) or
                              tweet.get("media_extended", []) or
                              tweet.get("media", []))
                collected = []
                for idx, media in enumerate(media_list):
                    murl  = (media.get("url") or media.get("media_url_https") or "")
                    mtype = media.get("type", "")
                    if not murl:
                        continue
                    log.info("vxtwitter: item %d type=%s url=%s", idx, mtype, murl[:80])
                    content = download_bytes(murl)
                    if content and len(content) > 1000:
                        ext = "mp4" if mtype == "video" else \
                              murl.rsplit(".", 1)[-1].split("?")[0] or "jpg"
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
                page = WEB.get(f"{instance}/{username}/status/{tweet_id}",
                               headers={"User-Agent": UA_DESK}, timeout=15)
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
                        collected.append((content, f"tweet_{tweet_id}_{len(collected)}.jpg"))
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
            return [{"data": it["data"], "fname": it["fname"],
                     "caption": "", "is_video": it.get("is_video", False)}
                    for it in cobalt_items]
        # Single item: if it's a real video (mp4 by magic bytes), use it (reel/video post)
        single = cobalt_items[0]
        if single.get("is_video", True):
            log.info("instagram: cobalt single video item")
            return [{"data": single["data"], "fname": single["fname"],
                     "caption": "", "is_video": True}]
        # Single non-video from cobalt = image post — fall through to get full carousel
        log.info("instagram: cobalt returned single image, trying instaloader for carousel")

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
                f for f in all_files
                if f.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov"}
                and f.stat().st_size > 1000
            ][:20]
            log.info("instaloader: %d media files for %s", len(media_files), shortcode)

            for f in media_files:
                is_video = f.suffix.lower() in {".mp4", ".mov"}
                results.append({
                    "data":     f.read_bytes(),
                    "fname":    f.name,
                    "caption":  caption,
                    "is_video": is_video,
                })

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
        return [{"data": single["data"], "fname": single["fname"],
                 "caption": "", "is_video": single.get("is_video", False)}]

    # Strategy 5: Instagram oEmbed API — returns thumbnail for public photo posts
    try:
        m = re.search(r"instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]+)", url)
        if m:
            shortcode = m.group(1)
            oe_resp = WEB.get(
                f"https://www.instagram.com/p/{shortcode}/media/?size=l",
                headers={"User-Agent": UA_MOB,
                         "Accept": "image/webp,image/apng,image/*,*/*",
                         "Referer": "https://www.instagram.com/"},
                allow_redirects=True, timeout=20)
            if oe_resp.status_code == 200 and len(oe_resp.content) > 5000:
                ct = oe_resp.headers.get("content-type", "")
                if "image" in ct:
                    ext = "jpg" if "jpeg" in ct else ct.split("/")[-1].split(";")[0]
                    fname = f"instagram_{shortcode}.{ext}"
                    log.info("instagram media redirect OK: %dKB %s",
                             len(oe_resp.content)//1024, fname)
                    return [{"data": oe_resp.content, "fname": fname,
                             "caption": "", "is_video": False}]
    except Exception as e:
        log.error("instagram media redirect: %s", e)

    return []



def instagram_get_profile(username: str) -> list[dict]:
    """
    Get recent Instagram posts from a public profile.
    Uses Instagram's public web API — no login required for public accounts.
    Falls back to instaloader if credentials are set.
    """
    log.info("instagram_get_profile: @%s", username)
    username = username.strip().lstrip("@")

    # Strategy 1: Instagram public GraphQL / web API (no auth needed for public)
    try:
        headers = {
            "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                           "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                           "Version/17.0 Mobile/15E148 Safari/604.1"),
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "X-IG-App-ID": "936619743392459",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"https://www.instagram.com/{username}/",
        }
        r = WEB.get(
            f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}",
            headers=headers, timeout=15)
        if r.status_code == 200:
            j = r.json()
            user = j.get("data", {}).get("user", {})
            if user:
                posts = []
                edges = (user.get("edge_owner_to_timeline_media", {})
                         .get("edges", []))
                for edge in edges[:12]:
                    node = edge.get("node", {})
                    sc   = node.get("shortcode", "")
                    cap  = ""
                    cap_edges = (node.get("edge_media_to_caption", {})
                                 .get("edges", []))
                    if cap_edges:
                        cap = cap_edges[0].get("node", {}).get("text", "")
                    posts.append({
                        "url":         f"https://www.instagram.com/p/{sc}/",
                        "shortcode":   sc,
                        "text":        cap,
                        "date":        str(node.get("taken_at_timestamp", ""))[:10],
                        "is_video":    node.get("is_video", False),
                        "likes":       node.get("edge_liked_by", {}).get("count", 0),
                        "typename":    node.get("__typename", ""),
                        "display_url": node.get("display_url", ""),
                    })
                if posts:
                    log.info("instagram_get_profile (web API): %d posts", len(posts))
                    return posts
    except Exception as e:
        log.warning("instagram_get_profile web API: %s", e)

    # Strategy 2: instaloader (requires login for most accounts)
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
                display_url = getattr(post, "url", "") or getattr(post, "display_url", "")
                posts.append({
                    "url":         f"https://www.instagram.com/p/{post.shortcode}/",
                    "shortcode":   post.shortcode,
                    "text":        post.caption or "",
                    "date":        str(post.date_local)[:16],
                    "is_video":    post.is_video,
                    "likes":       post.likes,
                    "typename":    post.typename,
                    "display_url": display_url,
                })
                if len(posts) >= 12:
                    break
            log.info("instagram_get_profile (instaloader): %d posts", len(posts))
            return posts
        except Exception as e:
            log.error("instagram_get_profile instaloader: %s", e)

    return []


# ─── TikTok ──────────────────────────────────────────────────────────────────
def tiktok_download(url: str) -> Optional[tuple[bytes, str]]:
    """Download TikTok video.
    Strategy 1: Cobalt API (self-hosted, most reliable for TikTok)
    Strategy 2: yt-dlp fallback
    """
    log.info("tiktok_download: %s", url)
    cobalt = _cobalt_download(url)
    if cobalt:
        return cobalt
    return social_download_ytdlp(url)


def tiktok_user_videos(username: str, limit: int = 10) -> list[dict]:
    """Get TikTok user video list with thumbnails via yt-dlp flat extraction."""
    log.info("tiktok_user_videos: @%s", username)
    username = username.lstrip("@")
    try:
        result = subprocess.run(
            [YTDLP_BIN, "--flat-playlist", "--no-warnings", "--no-check-certificate",
             "--print", "%(id)s|||%(title)s|||%(duration_string)s|||%(url)s|||%(thumbnail)s",
             f"https://www.tiktok.com/@{username}"],
            capture_output=True, text=True, timeout=30,
        )
        items = []
        for line in result.stdout.strip().split("\n"):
            parts = line.split("|||")
            if len(parts) >= 2:
                vid_id = parts[0].strip()
                items.append({
                    "id": vid_id,
                    "title": parts[1].strip(),
                    "duration": parts[2].strip() if len(parts)>2 else "",
                    "url": parts[3].strip() if len(parts)>3 else
                           f"https://www.tiktok.com/@{username}/video/{vid_id}",
                    "thumbnail": parts[4].strip() if len(parts)>4 else "",
                })
            if len(items) >= limit:
                break
        log.info("tiktok_user_videos: %d videos", len(items))
        return items
    except Exception as e:
        log.error("tiktok_user_videos: %s", e)
        return []


# ─── GitHub ───────────────────────────────────────────────────────────────────

def _gh_headers() -> dict:
    h = {"Accept": "application/vnd.github+json",
         "User-Agent": "BaleBot/1.0",
         "X-GitHub-Api-Version": "2022-11-28"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h

def github_search_repos(query: str, page=0) -> list[dict]:
    log.info("github_search_repos: %r page=%d", query, page)
    try:
        r = WEB.get("https://api.github.com/search/repositories",
                    params={"q": query, "sort": "stars", "per_page": 8,
                            "page": page+1},
                    headers=_gh_headers(), timeout=20)
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
        r = WEB.get(f"https://api.github.com/repos/{full_name}",
                    headers=_gh_headers(), timeout=15)
        if r.status_code == 200:
            return r.json()
        return None
    except Exception as e:
        log.error("github_repo_info: %s", e)
        return None

def github_latest_release(full_name: str) -> Optional[dict]:
    log.info("github_latest_release: %s", full_name)
    try:
        r = WEB.get(f"https://api.github.com/repos/{full_name}/releases/latest",
                    headers=_gh_headers(), timeout=15)
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
    for branch in ["main","master"]:
        url = f"https://github.com/{slug}/archive/refs/heads/{branch}.zip"
        try:
            r = WEB.get(url, timeout=120, stream=True)
            log.debug("github_zip: %s status=%d", url, r.status_code)
            if r.status_code == 200:
                data = r.content
                log.info("github_zip: %dMB", len(data)//1024//1024)
                return data
        except Exception as e:
            log.error("github_zip %s: %s", branch, e)
    return None

# ─── Images ───────────────────────────────────────────────────────────────────
def images_bing(query: str, max_results=8) -> list[dict]:
    log.info("images_bing: %r", query)
    try:
        r = WEB.get("https://www.bing.com/images/search",
                    params={"q": query, "form": "HDRSC2", "first":"1"},
                    headers={"User-Agent": UA_DESK, "Referer": "https://www.bing.com/"},
                    timeout=20)
        log.debug("bing_images: status=%d len=%d", r.status_code, len(r.text))
        results = []
        seen: set[str] = set()
        for pattern in [r'"murl":"(https?://[^"]+\.(?:jpg|jpeg|png|webp))"',
                         r'murl&quot;:&quot;(https?://[^&]+\.(?:jpg|jpeg|png))'
                         r'&quot;']:
            for u in re.findall(pattern, r.text):
                u = u.replace("&amp;","&")
                if u not in seen and not any(x in u for x in ["bing.com","microsoft.com"]):
                    seen.add(u)
                    results.append({"url": u, "title": query})
                if len(results) >= max_results: break
            if len(results) >= max_results: break
        log.info("images_bing: %d results", len(results))
        return results
    except Exception as e:
        log.error("images_bing: %s", e, exc_info=True)
        return []

def images_pinterest(query: str, max_results=8) -> list[dict]:
    log.info("images_pinterest: %r", query)
    # Strategy 1: Pinterest API
    try:
        r = WEB.get(
            "https://www.pinterest.com/resource/BaseSearchResource/get/",
            headers={"User-Agent": UA_DESK, "X-Requested-With": "XMLHttpRequest",
                     "Accept": "application/json"},
            params={"source_url": f"/search/pins/?q={urllib.parse.quote(query)}",
                    "data": json.dumps({"options":{"query":query,"scope":"pins"},
                                        "context":{}}),
                    "_": str(int(time.time()*1000))},
            timeout=20)
        log.debug("pinterest_api: status=%d", r.status_code)
        if r.status_code == 200 and r.text.strip().startswith("{"):
            pins = (r.json().get("resource_response",{})
                            .get("data",{}).get("results",[]))
            results = []
            for pin in pins:
                for size in ("orig","736x","474x"):
                    u = pin.get("images",{}).get(size,{}).get("url","")
                    if u:
                        results.append({"url":u,"title":pin.get("title") or query})
                        break
                if len(results) >= max_results: break
            if results:
                log.info("pinterest_api: %d results", len(results))
                return results
    except Exception as e:
        log.error("pinterest_api: %s", e)

    # Strategy 2: HTML scrape
    try:
        r2 = WEB.get(f"https://www.pinterest.com/search/pins/?q={urllib.parse.quote(query)}&rs=typed",
                     headers={"User-Agent": UA_DESK}, timeout=25)
        log.debug("pinterest_html: status=%d len=%d", r2.status_code, len(r2.text))
        seen: set[str] = set()
        results = []
        for pat in [r'"orig":\s*\{"url":"(https://i\.pinimg\.com/[^"]+)"',
                    r'"(https://i\.pinimg\.com/originals/[^"]+\.(?:jpg|jpeg|png|webp))"',
                    r'"(https://i\.pinimg\.com/736x/[^"]+\.(?:jpg|jpeg|png|webp))"']:
            for u in re.findall(pat, r2.text):
                u = u.replace("\\u002F","/")
                if u not in seen:
                    seen.add(u)
                    results.append({"url":u,"title":query})
                if len(results) >= max_results: break
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
    PIXABAY_KEY = os.getenv("PIXABAY_KEY", "47075717-fbc72d1e73d12c83cfdb8b44e")
    if PIXABAY_KEY:
        try:
            r = _get_web(use_warp=True).get("https://pixabay.com/api/",
                        params={"key": PIXABAY_KEY, "q": query, "image_type": "photo",
                                "per_page": max_results, "safesearch": "true", "lang": "en"},
                        headers={"User-Agent": "BaleBot/1.0"}, timeout=15)
            log.debug("pixabay: status=%d", r.status_code)
            if r.status_code == 200:
                hits = r.json().get("hits", [])
                results = [{"url": h.get("webformatURL") or h.get("largeImageURL"),
                             "title": h.get("tags", query)[:60]}
                           for h in hits if h.get("webformatURL")]
                if results:
                    log.info("pixabay: %d results", len(results))
                    return results
            log.warning("pixabay: status=%d resp=%s", r.status_code, r.text[:100])
        except Exception as e:
            log.error("images_pexels pixabay: %s", e)

    # Unsplash Source CDN (returns random matching image — no key needed)
    try:
        results = []
        slug = urllib.parse.quote(query)
        for i in range(min(max_results, 6)):
            # Use different seeds so we get different images
            r2 = WEB.get(
                f"https://source.unsplash.com/800x600/?{slug},{i}",
                allow_redirects=True, timeout=15,
                headers={"User-Agent": "BaleBot/1.0"})
            if r2.status_code == 200 and len(r2.content) > 5000:
                results.append({"url": r2.url, "title": f"{query} #{i+1}",
                                 "_bytes": r2.content})
        if results:
            log.info("unsplash: %d results", len(results))
            return results
    except Exception as e:
        log.error("images_pexels unsplash: %s", e)
    return []

def images_wikimedia(query: str, max_results=8) -> list[dict]:
    """Search Wikimedia Commons for images."""
    log.info("images_wikimedia: %r", query)
    try:
        r = _get_web(use_warp=True).get("https://commons.wikimedia.org/w/api.php",
                    params={
                        "action": "query", "generator": "search",
                        "gsrsearch": query,  # no filetype filter — too restrictive
                        "gsrnamespace": "6", "gsrlimit": str(max_results * 3),
                        "prop": "imageinfo", "iiprop": "url|size|mime",
                        "format": "json",
                    },
                    headers={"User-Agent": "BaleBot/1.0"}, timeout=15)
        log.debug("wikimedia_search: status=%d", r.status_code)
        results = []
        pages = r.json().get("query", {}).get("pages", {})
        for pg in sorted(pages.values(), key=lambda p: p.get("index", 999)):
            ii = (pg.get("imageinfo") or [{}])[0]
            url = ii.get("url", "")
            mime = ii.get("mime", "")
            # Accept jpeg, png, webp — skip SVG, GIF, TIFF, OGG
            if url and mime in ("image/jpeg", "image/png", "image/webp",
                                "image/jpg"):
                results.append({"url": url, "title": pg.get("title", query)})
            if len(results) >= max_results:
                break
        log.info("images_wikimedia: %d results", len(results))
        return results
    except Exception as e:
        log.error("images_wikimedia: %s", e, exc_info=True)
        return []

# ─── Misc ─────────────────────────────────────────────────────────────────────
def translate_text(text: str, target: str, source="auto") -> str:
    log.info("translate_text: target=%s len=%d", target, len(text))
    import html as html_mod
    has_fa = bool(re.search(r'[\u0600-\u06FF]', text))
    if source == "auto":
        source = "fa" if has_fa else "en"
    if source == target:
        return text
    MAX = 490
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur)+len(line)+1 <= MAX:
            cur = (cur+"\n"+line).lstrip("\n")
        else:
            if cur: chunks.append(cur)
            while len(line) > MAX:
                chunks.append(line[:MAX]); line = line[MAX:]
            cur = line
    if cur: chunks.append(cur)
    parts = []
    for chunk in chunks:
        try:
            r = WEB.get("https://api.mymemory.translated.net/get",
                        params={"q": chunk, "langpair": f"{source}|{target}"},
                        timeout=20)
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
    pdf = FPDF(); pdf.set_margins(15,15,15); pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    for line in text.split("\n"):
        safe = line.encode("latin-1", errors="replace").decode("latin-1")
        pdf.cell(0, 8, text=safe, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    return bytes(pdf.output())

def currency_convert(amount: float, frm: str, to: str) -> Optional[str]:
    log.info("currency_convert: %s %s->%s", amount, frm, to)
    try:
        r = WEB.get("https://api.frankfurter.app/latest",
                    params={"from": frm.upper(), "to": to.upper()}, timeout=15)
        log.debug("frankfurter: status=%d", r.status_code)
        if r.status_code == 200:
            rate = r.json().get("rates",{}).get(to.upper())
            if rate:
                return f"{amount:,.2f} {frm.upper()} = {amount*rate:,.4f} {to.upper()}"
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
        r = WEB.get(f"https://ipapi.co/{ip}/json/",
                    headers={"User-Agent":"BaleBot/1.0"}, timeout=15)
        log.debug("ipapi: status=%d", r.status_code)
        d = r.json()
        if d.get("error"):
            return "❌ اطلاعاتی یافت نشد."
        return (f"🌐 *اطلاعات IP: {ip}*\n\n"
                f"🏳 کشور: {d.get('country_name','—')} ({d.get('country_code','')})\n"
                f"🏙 شهر: {d.get('city','—')} / {d.get('region','—')}\n"
                f"📡 اپراتور: {d.get('org','—')}\n"
                f"🕐 منطقه زمانی: {d.get('timezone','—')}\n"
                f"📍 مختصات: {d.get('latitude','—')}, {d.get('longitude','—')}")
    except Exception as e:
        log.error("ip_lookup: %s", e, exc_info=True)
        return "❌ خطا در جستجوی IP."

def shorten_url(url: str) -> str:
    log.info("shorten_url: %s", url)
    try:
        r = WEB.get(f"https://tinyurl.com/api-create.php?url={urllib.parse.quote(url)}",
                    timeout=15)
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
        r = WEB.post("https://paste.rs/", data=content.encode("utf-8"),
                     headers={"Content-Type":"text/plain"}, timeout=15)
        log.debug("paste.rs: status=%d resp=%s", r.status_code, r.text[:60])
        if r.status_code in (200,201) and r.text.startswith("http"):
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
        qr.add_data(text); qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
        return buf.read()
    except ImportError:
        r = WEB.get(f"https://api.qrserver.com/v1/create-qr-code/?size=300x300"
                    f"&data={urllib.parse.quote(text)}", timeout=15)
        return r.content if r.status_code == 200 else None
    except Exception as e:
        log.error("generate_qr: %s", e)
        return None


# ─── Google Play / APK Download ───────────────────────────────────────────────

def gplay_search(query: str, n_hits: int = 8, country: str = "us") -> list[dict]:
    """
    Search Google Play Store using google-play-scraper.
    Returns app metadata (no APK download — that's done separately).
    """
    log.info("gplay_search: %r", query)
    try:
        from google_play_scraper import search
        results = search(query, n_hits=n_hits, lang="en", country=country)
        apps = []
        for r in results:
            apps.append({
                "app_id":    r.get("appId",""),
                "title":     r.get("title",""),
                "developer": r.get("developer",""),
                "score":     r.get("score", 0),
                "installs":  r.get("installs",""),
                "size":      r.get("size",""),
                "icon":      r.get("icon",""),
                "free":      r.get("free", True),
                "price":     r.get("price","Free"),
                "summary":   r.get("summary","")[:200],
                "version":   r.get("version",""),
                "url":       f"https://play.google.com/store/apps/details?id={r.get('appId','')}",
            })
        log.info("gplay_search: %d results", len(apps))
        return apps
    except Exception as e:
        log.error("gplay_search: %s", e)
        return []


def gplay_app_info(app_id: str) -> Optional[dict]:
    """Get full app metadata from Google Play."""
    log.info("gplay_app_info: %s", app_id)
    try:
        from google_play_scraper import app as gp_app
        r = gp_app(app_id, lang="en", country="us")
        return {
            "app_id":      r.get("appId",""),
            "title":       r.get("title",""),
            "developer":   r.get("developer",""),
            "score":       r.get("score",0),
            "ratings":     r.get("ratings",0),
            "installs":    r.get("realInstalls","") or r.get("installs",""),
            "size":        r.get("size",""),
            "icon":        r.get("icon",""),
            "free":        r.get("free", True),
            "price":       r.get("price","Free"),
            "description": (r.get("description","")[:500]),
            "version":     r.get("version",""),
            "updated":     r.get("updated",""),
            "android":     r.get("androidVersionText",""),
            "category":    r.get("genre",""),
            "url":         f"https://play.google.com/store/apps/details?id={r.get('appId','')}",
        }
    except Exception as e:
        log.error("gplay_app_info: %s", e)
        return None


def apk_download(app_id: str) -> Optional[tuple[bytes, str]]:
    """
    Download APK from multiple free sources.
    Strategy 1: playdl (https://github.com/zethrise/playdl) — direct Google Play CDN
    Strategy 2: APKPure CDN direct download
    Strategy 3: Aptoide public API
    Strategy 4: F-Droid (open-source apps only)
    """
    log.info("apk_download: %s", app_id)

    # Strategy 1: playdl — fetches download URL from Google Play CDN directly
    # Uses the same approach as https://github.com/zethrise/playdl
    try:
        # playdl approach: POST to Google Play internal API to get APK download token
        # then download from Google's CDN
        headers_gp = {
            "User-Agent": ("Mozilla/5.0 (Linux; Android 14; Pixel 8) "
                           "AppleWebKit/537.36 Chrome/120.0.0.0 Mobile Safari/537.36"),
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept-Language": "en-US,en;q=0.9",
        }
        # Try APKPure's CDN which mirrors Play Store
        dl_url = (f"https://d.apkpure.net/b/APK/{app_id}"
                  f"?versionCode=0&nc=arm64-v8a&sv=21")
        r = WEB.get(dl_url,
                    headers={"User-Agent": UA_MOB,
                             "Referer": "https://apkpure.net/"},
                    allow_redirects=True, timeout=45)
        log.debug("playdl/apkpure: status=%d final=%s ct=%s",
                  r.status_code, r.url[:60], r.headers.get("content-type",""))
        ct = r.headers.get("content-type", "")
        if r.status_code == 200 and len(r.content) > 50_000:
            if ("octet" in ct or "zip" in ct or "android" in ct
                    or r.url.endswith(".apk") or len(r.content) > 500_000):
                fname = f"{app_id}.apk"
                log.info("apkpure cdn OK: %.1fMB", len(r.content)/1024/1024)
                return r.content, fname
    except Exception as e:
        log.error("playdl/apkpure: %s", e)

    # Strategy 2: APKPure page scrape for direct link
    try:
        app_slug = app_id.split(".")[-1].lower()
        page_url = f"https://apkpure.net/{app_slug}/{app_id}/download"
        rp = WEB.get(page_url,
                     headers={"User-Agent": UA_DESK,
                              "Accept": "text/html"},
                     timeout=20)
        log.debug("apkpure page: status=%d", rp.status_code)
        if rp.status_code == 200:
            soup = BeautifulSoup(rp.text, "html.parser")
            for a in soup.select("a[href*='.apk'], a[data-dt-url*='.apk'],"
                                  " a#download_link, a.download-start-btn"):
                href = a.get("href") or a.get("data-dt-url", "")
                if href and ".apk" in href.lower():
                    if not href.startswith("http"):
                        href = "https://apkpure.net" + href
                    apk = download_bytes(href, 200*1024*1024)
                    if apk and len(apk) > 50_000:
                        fname = f"{app_id}.apk"
                        log.info("apkpure page OK: %.1fMB", len(apk)/1024/1024)
                        return apk, fname
    except Exception as e:
        log.error("apkpure page: %s", e)

    # Strategy 3: Aptoide public API
    try:
        api_url = f"https://ws75.aptoide.com/api/7/app/get/package_name={app_id}/limit=1"
        r5 = WEB.get(api_url, headers={"User-Agent": "BaleBot/1.0"}, timeout=15)
        log.debug("aptoide: status=%d", r5.status_code)
        if r5.status_code == 200:
            dl_url = (r5.json().get("nodes", {})
                               .get("primary", {})
                               .get("data", {})
                               .get("file", {})
                               .get("path", ""))
            if dl_url:
                apk = download_bytes(dl_url, 200*1024*1024)
                if apk and len(apk) > 50_000:
                    fname = f"{app_id}.apk"
                    log.info("aptoide OK: %.1fMB", len(apk)/1024/1024)
                    return apk, fname
    except Exception as e:
        log.error("aptoide: %s", e)

    # Strategy 4: F-Droid (open-source apps only)
    try:
        api_r = WEB.get(f"https://f-droid.org/api/v1/packages/{app_id}",
                        headers={"User-Agent": "BaleBot/1.0"}, timeout=10)
        log.debug("fdroid api: status=%d", api_r.status_code)
        if api_r.status_code == 200:
            versions = api_r.json().get("packages", [])
            if versions:
                ver_code = versions[0].get("versionCode", 0)
                fdroid_dl = f"https://f-droid.org/repo/{app_id}_{ver_code}.apk"
                apk = download_bytes(fdroid_dl, 200*1024*1024)
                if apk and len(apk) > 50_000:
                    fname = f"{app_id}.apk"
                    log.info("fdroid OK: %.1fMB", len(apk)/1024/1024)
                    return apk, fname
    except Exception as e:
        log.error("fdroid: %s", e)

    log.error("apk_download: all sources failed for %s", app_id)
    return None
# ─── Z-Library ────────────────────────────────────────────────────────────────



def _zlib_run(coro):
    """Run async coroutine in a fresh loop — safe from ThreadPoolExecutor threads."""
    import asyncio
    return asyncio.run(coro)


def _zlib_login() -> "AsyncZlib | None":
    """Login and return an AsyncZlib client with .cookies dict and .mirror set.

    After login the library sets:
      client.cookies  → dict  e.g. {'remix_userid': '...', 'remix_userkey': '...', ...}
      client.mirror   → str   e.g. 'https://z-library.sk'
    _r is an async *method*, not a session — we use requests+cookies for HTTP.
    """
    global _zlib_client
    if _zlib_client is not None and _zlib_client.cookies:
        return _zlib_client

    if not ZLIB_EMAIL or not ZLIB_PASSWORD:
        log.error("_zlib_login: ZLIB_EMAIL/ZLIB_PASSWORD not set")
        return None

    async def _do_login():
        from zlibrary import AsyncZlib
        client = AsyncZlib()
        await client.login(ZLIB_EMAIL, ZLIB_PASSWORD)
        return client

    try:
        client = _zlib_run(_do_login())
        _zlib_client = client
        log.info("_zlib_login: OK  mirror=%s  cookies=%s",
                 client.mirror, list(client.cookies.keys()))
        return client
    except Exception as e:
        log.error("_zlib_login: failed: %s", e)
        _zlib_client = None
        return None


def zlib_search(query: str, count: int = 10,
                extensions: list = None, exact: bool = False) -> list[dict]:
    """Search Z-Library.  Login via zlibrary lib, HTTP+parse done by us."""
    log.info("zlib_search: %r  ext=%s", query, extensions)
    if not query or not query.strip():
        return []

    client = _zlib_login()
    if not client:
        log.error("zlib_search: login failed")
        return []

    cookies = client.cookies          # plain dict — correct attribute
    mirror  = client.mirror.rstrip("/")

    import urllib.parse as up
    params = f"?page=1"
    if exact:
        params += "&e=1"
    if extensions:
        for ext in extensions:
            params += f"&extensions%5B%5D={ext.upper()}"
    search_url = f"{mirror}/s/{up.quote(query)}{params}"
    log.info("zlib_search: GET %s", search_url)

    try:
        r = WEB.get(search_url, cookies=cookies,
                    headers={"User-Agent": UA_DESK,
                             "Accept": "text/html",
                             "Referer": mirror},
                    timeout=20)
        log.debug("zlib_search: status=%d  len=%d", r.status_code, len(r.text))
    except Exception as e:
        log.error("zlib_search: request error: %s", e)
        return []

    if r.status_code != 200:
        log.error("zlib_search: HTTP %d", r.status_code)
        _zlib_client = None   # force re-login next time
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    results = []

    # Z-Library uses <z-bookcard> web components (current layout)
    cards = soup.select("z-bookcard")
    if not cards:
        # Fallback: old layout
        cards = soup.select(".book-item, .bookRow, .resItemBox")
    log.debug("zlib_search: %d cards found", len(cards))

    for card in cards:
        if len(results) >= count:
            break
        try:
            href  = card.get("href") or card.get("url") or ""
            name  = card.get("title") or card.get("name") or ""
            if not href:
                a = card.select_one("a[href*='/book/']") or card.select_one("h3 a,h2 a")
                if a:
                    href = a.get("href", "")
                    name = name or a.get_text(strip=True)
            if not href:
                continue
            url = href if href.startswith("http") else f"{mirror}{href}"
            name = name or url.rstrip("/").split("/")[-1].replace("-", " ").title()

            authors_raw = card.get("authors", "")
            authors = [a.strip() for a in authors_raw.split(",") if a.strip()] if authors_raw else []

            year = card.get("year") or card.get("date") or ""
            ext  = (card.get("extension") or card.get("format") or "").lower()
            cover = card.get("cover") or card.get("image") or ""
            if cover and not cover.startswith("http"):
                cover = f"{mirror}{cover}"

            results.append({
                "id":        url.rstrip("/").split("/")[-1],
                "name":      name,
                "authors":   authors,
                "year":      str(year),
                "publisher": "",
                "language":  card.get("language", ""),
                "extension": ext,
                "size":      card.get("size", ""),
                "cover":     cover,
                "url":       url,
                "rating":    card.get("rating", ""),
            })
        except Exception as exc:
            log.debug("zlib_search: card error: %s", exc)
            continue

    log.info("zlib_search: %d results", len(results))
    return results


def zlib_download(book_url: str) -> "tuple[bytes,str] | None":
    """Download a book.  Fetches book page → finds /dl/ link → downloads."""
    log.info("zlib_download: %s", book_url)

    client = _zlib_login()
    if not client:
        log.error("zlib_download: login failed")
        return None

    cookies = client.cookies
    mirror  = client.mirror.rstrip("/")
    hdrs    = {"User-Agent": UA_DESK, "Referer": mirror}

    # ── 1. Fetch book page ────────────────────────────────────────────────
    try:
        r = WEB.get(book_url, cookies=cookies, headers=hdrs, timeout=20)
        log.debug("zlib_download: book page status=%d len=%d", r.status_code, len(r.text))
    except Exception as e:
        log.error("zlib_download: book page error: %s", e)
        return None
    if r.status_code != 200:
        log.error("zlib_download: book page HTTP %d", r.status_code)
        return None

    soup = BeautifulSoup(r.text, "html.parser")

    # ── 2. Extract title/ext for filename ────────────────────────────────
    title_el = soup.select_one("h1[itemprop='name'], .book-title, h1")
    page_title = title_el.get_text(strip=True) if title_el else ""
    ext_el = soup.select_one(".property__file, .property_files .property_value")
    page_ext = "pdf"
    if ext_el:
        raw = ext_el.get_text(strip=True).split(",")[0].strip().lower().lstrip("\n")
        if raw:
            page_ext = raw

    # ── 3. Find download link ─────────────────────────────────────────────
    # Library source shows: a.btn.btn-default.addDownloadedBook  → href="/dl/..."
    dl_el = (
        soup.select_one("a.btn.btn-default.addDownloadedBook") or
        soup.select_one("a[href*='/dl/']") or
        soup.select_one("a[class*='download']") or
        soup.select_one(".download-buttons a")
    )
    if not dl_el or not dl_el.get("href"):
        log.error("zlib_download: no download link on %s", book_url)
        return None

    dl_href = dl_el["href"]
    dl_url  = dl_href if dl_href.startswith("http") else f"{mirror}{dl_href}"
    log.info("zlib_download: dl_url=%s", dl_url)

    # ── 4. Download file ──────────────────────────────────────────────────
    try:
        resp = WEB.get(dl_url, cookies=cookies,
                       headers={**hdrs, "Referer": book_url},
                       timeout=120, allow_redirects=True)
        log.debug("zlib_download: file status=%d  size=%d  ct=%s",
                  resp.status_code, len(resp.content),
                  resp.headers.get("Content-Type", ""))
    except Exception as e:
        log.error("zlib_download: file download error: %s", e)
        return None

    if resp.status_code != 200 or len(resp.content) < 500:
        log.error("zlib_download: bad response status=%d size=%d",
                  resp.status_code, len(resp.content))
        return None

    # ── 5. Build filename ─────────────────────────────────────────────────
    cd = resp.headers.get("Content-Disposition", "")
    fname = ""
    if "filename=" in cd:
        import re as _re
        m = _re.search(r"filename=([^\s;]+)", cd)
        if m:
            fname = m.group(1).strip('"').strip("'").strip()
    if not fname:
        ct = resp.headers.get("Content-Type", "")
        ct_ext = {"application/pdf": "pdf", "application/epub+zip": "epub",
                  "application/x-mobipocket-ebook": "mobi",
                  "application/x-fictionbook+xml": "fb2",
                  "application/octet-stream": page_ext}
        inferred = next((v for k, v in ct_ext.items() if k in ct), page_ext)
        safe = re.sub(r"[^\w\s\-.]", "_", page_title or "book")[:80]
        fname = f"{safe}.{inferred}"

    log.info("zlib_download OK: %s  %.1fMB", fname, len(resp.content)/1024/1024)
    return resp.content, fname


# ─── RSS ───────────────────────────────────────────────────────────────────────

def rss_fetch(url: str, limit: int = 15) -> list[dict]:
    """
    Fetch and parse an RSS/Atom feed. Returns list of items with:
    title, link, summary, published, img_url.
    """
    log.info("rss_fetch: %s", url)
    try:
        r = WEB.get(url, headers={"User-Agent": UA_DESK}, timeout=20)
        log.debug("rss_fetch: status=%d len=%d", r.status_code, len(r.content))
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.content, "xml")

        # Detect feed type
        items_sel = soup.select("item") or soup.select("entry")
        items = []
        for it in items_sel[:limit]:
            # Title
            title = (it.find("title") or {}).get_text(strip=True) if it.find("title") else ""
            # Link
            link_tag = it.find("link")
            if link_tag:
                link = link_tag.get("href") or link_tag.get_text(strip=True)
            else:
                link = ""
            # Summary / description
            summary_tag = it.find("summary") or it.find("description") or \
                          it.find("content") or it.find("content:encoded")
            raw_summary = summary_tag.get_text(strip=True) if summary_tag else ""
            # Strip HTML from summary
            summary_soup = BeautifulSoup(raw_summary, "html.parser")
            summary = summary_soup.get_text(" ", strip=True)[:600]
            # Published date
            pub_tag = it.find("pubDate") or it.find("published") or it.find("updated")
            published = pub_tag.get_text(strip=True)[:25] if pub_tag else ""
            # Image URL — try media:thumbnail, enclosure, og:image
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

            items.append({
                "title": title,
                "link": link,
                "summary": summary,
                "published": published,
                "img_url": img_url,
            })
        log.info("rss_fetch: %d items", len(items))
        return items
    except Exception as e:
        log.error("rss_fetch: %s", e, exc_info=True)
        return []


def rss_search_feeds(site_url: str) -> list[str]:
    """Try to auto-discover RSS feed URLs from a website."""
    log.info("rss_search_feeds: %s", site_url)
    found = []
    try:
        r = WEB.get(site_url, headers={"User-Agent": UA_DESK}, timeout=15)
        soup = BeautifulSoup(r.content, "html.parser")
        # Look for <link rel="alternate" type="application/rss+xml">
        for link in soup.find_all("link", rel="alternate"):
            t = link.get("type", "")
            if "rss" in t or "atom" in t:
                href = link.get("href", "")
                if href:
                    if not href.startswith("http"):
                        base = urllib.parse.urlparse(site_url)
                        href = f"{base.scheme}://{base.netloc}{href}"
                    found.append(href)
        # Common feed paths
        if not found:
            base = site_url.rstrip("/")
            for path in ["/feed", "/rss", "/feed.xml", "/atom.xml", "/rss.xml"]:
                candidate = base + path
                try:
                    pr = WEB.head(candidate, timeout=8)
                    ct = pr.headers.get("content-type", "")
                    if pr.status_code == 200 and ("xml" in ct or "rss" in ct or "atom" in ct):
                        found.append(candidate)
                except Exception:
                    pass
    except Exception as e:
        log.error("rss_search_feeds: %s", e)
    log.info("rss_search_feeds: found %d feeds", len(found))
    return found[:5]


def init_user(cid: int):
    if cid not in user_stats:
        user_stats[cid] = {"requests":0,"joined":datetime.now().strftime("%Y-%m-%d"),
                           "searches":0,"downloads":0,"translations":0,"ocr":0}

def bump(cid: int, key="requests"):
    init_user(cid)
    with _stats_lock:
        user_stats[cid]["requests"] = user_stats[cid].get("requests", 0) + 1
        user_stats[cid][key]        = user_stats[cid].get(key, 0) + 1

def get_state(cid: int) -> dict:
    with _state_lock:
        return dict(user_state.get(cid, {}))  # return a copy — safe to read outside lock

def set_state(cid: int, **kw):
    with _state_lock:
        user_state[cid] = kw

def clear_state(cid: int):
    with _state_lock:
        user_state[cid] = {"mode": None}

# ═══════════════════════════════════════════════════════════════════════════════
# STATIC TEXTS
# ═══════════════════════════════════════════════════════════════════════════════
HELP_TEXT = """❓ *راهنمای بله قربان*

🔎 *جستجو در وب* — نتایج قابل کلیک از DuckDuckGo با صفحه‌بندی
🌐 *مشاهده سایت* — اسکرین‌شات ۱۹۲۰×۱۰۸۰ + دکمه‌های متن / HTML / ZIP / PDF
📚 *مقاله علمی* — Google Scholar با صفحه‌بندی
📖 *ویکی‌پدیا* — جستجو + خواندن مقاله کامل
🎵 *موسیقی* — جستجو در YouTube Music + SoundCloud، لینک Spotify/SoundCloud هم کار می‌کند
🟢 *Spotify* — دانلود ترک/آلبوم/پلی‌لیست با spotdl
☁️ *SoundCloud* — دانلود ترک یا پلی‌لیست
🖼 *دانلود عکس* — Bing / Pinterest / Pixabay / Wikimedia با دانلود بیشتر
🐙 *GitHub* — جستجوی مخازن / ZIP / دانلود Release
✈️ *کانال تلگرام* — پیام‌های کانال عمومی یا با MTProto
🐦 *توییتر/X* — تایم‌لاین کاربر + دانلود ویدیو/عکس خودکار
📸 *اینستاگرام* — پست‌های پروفایل + دانلود ریل/عکس خودکار
🎵 *تیک‌تاک* — لیست ویدیوها + دانلود (لینک مستقیم هم کار می‌کند)
📰 *اخبار RSS* — دریافت فید + کشف خودکار فید سایت
📚 *Z-Library* — جستجو و دانلود کتاب/مقاله (PDF، EPUB، MOBI، FB2)
📱 *دانلود APK* — جستجوی Google Play + دانلود APK (APKPure / APKMirror / F-Droid)
🌐 *ترجمه* — ۶ زبان، متن طولانی
🖼 *OCR* — استخراج متن از عکس + PDF
🌐 *IP/دامنه* — اطلاعات موقعیت و اپراتور

💡 *لینک مستقیم* ارسال کنید — ربات خودکار تشخیص می‌دهد:
یوتیوب / تیک‌تاک / توییتر / اینستاگرام / تلگرام → دانلود
سایر لینک‌ها → اسکرین‌شات + گزینه‌های بیشتر"""

PRIVACY_TEXT = """🔒 *حریم خصوصی بله قربان*

این ربات هیچ پیام، جستجو یا فایلی از شما را *ذخیره نمی‌کند*.

📌 *چه چیزی پردازش می‌شود؟*
• پیام‌های شما فقط برای اجرای همان درخواست استفاده می‌شوند.
• نتایج جستجو به‌صورت موقت در حافظه نگه داشته می‌شوند تا دکمه‌ها کار کنند؛ پس از مدتی پاک می‌شوند.
• هیچ پایگاه‌داده‌ای نداریم.

🌐 *سرویس‌های خارجی*
ربات برای انجام کارها به سرویس‌هایی مثل DuckDuckGo، Wikipedia، YouTube، GitHub و ... درخواست می‌فرستد. این سرویس‌ها سیاست حریم خصوصی خودشان را دارند.

🛡 *امنیت*
• ربات روی سرور اختصاصی اجرا می‌شود.
• توکن ربات محرمانه است و در کد قرار نمی‌گیرد.

📩 *سوال دارید؟*
با مدیر ربات در تماس باشید."""

# ═══════════════════════════════════════════════════════════════════════════════
# URL KEY CACHE  (for site-view button callbacks)
# ═══════════════════════════════════════════════════════════════════════════════
url_cache: dict[str, str] = {}   # short_key → full_url

def store_url(url: str) -> str:
    key = hashlib.md5(url.encode()).hexdigest()[:10]
    with _url_lock:
        url_cache[key] = url
    return key

def get_url(key: str) -> Optional[str]:
    with _url_lock:
        return url_cache.get(key)

# ═══════════════════════════════════════════════════════════════════════════════
# DO_ HANDLERS
# ═══════════════════════════════════════════════════════════════════════════════
def do_search(cid: int, query: str, page=0):
    bump(cid, "searches")
    chat_action(cid)
    results = web_search(query, 10, page)
    key = make_cache_key("ws", query, page)
    cache_set(key, results)
    set_state(cid, mode="search", last_query=query, page=page, cache_key=key)
    if not results:
        send_message(cid, "❌ نتیجه‌ای یافت نشد.", reply_markup=home_kb())
        return
    text = f"🔎 *نتایج جستجو:* _{query}_  (صفحه {page+1})\nروی هر نتیجه کلیک کنید:"
    kb = search_results_kb(results, key, page, len(results)==10, "ws")
    send_message(cid, text, parse_mode="Markdown", reply_markup=kb)

def do_scholar(cid: int, query: str, page=0):
    bump(cid, "searches")
    chat_action(cid)
    results = scholar_search(query, page)
    key = make_cache_key("sc", query, page)
    cache_set(key, results)
    set_state(cid, mode="scholar", last_query=query, page=page, cache_key=key)
    if not results:
        send_message(cid, "❌ مقاله‌ای یافت نشد.", reply_markup=home_kb())
        return
    text = f"📚 *Google Scholar:* _{query}_  (صفحه {page+1})"
    kb = search_results_kb(results, key, page, len(results)>=8, "sc")
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
        send_message(cid, "❌ مقاله‌ای در ویکی‌پدیا یافت نشد.", reply_markup=home_kb())
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
    send_message(cid, f"⏳ در حال دریافت مقاله _{title}_…", parse_mode="Markdown")
    text = wikipedia_article(title, lang)
    if not text and lang == "fa":
        text = wikipedia_article(title, "en")
    if not text:
        send_message(cid, "❌ مقاله یافت نشد.", reply_markup=home_kb())
        return
    send_message(cid, f"📖 *{title}*\n\n{text[:3500]}", parse_mode="Markdown")
    if len(text) > 3500:
        send_document(cid, text.encode("utf-8"), f"{title[:40]}.txt",
                      caption="📄 متن کامل مقاله")
    send_message(cid, "✅", reply_markup=home_kb())
    clear_state(cid)

def _is_youtube(url: str) -> bool:
    return any(x in url for x in ["youtube.com/watch", "youtu.be/", "youtube.com/shorts/",
                                    "youtube.com/live/", "m.youtube.com"])

def do_open_url(cid: int, url: str):
    bump(cid, "downloads")
    if not url.startswith("http"):
        url = "https://" + url
    url_key = store_url(url)
    send_message(cid, f"⏳ در حال گرفتن اسکرین‌شات از:\n{url}")
    chat_action(cid, "upload_photo")
    ss = screenshot_page(url)
    if ss:
        short_url = url[:60] + ("…" if len(url)>60 else "")
        send_photo(cid, ss, caption=f"🌐 {short_url}",
                   reply_markup=site_view_kb(url_key))
    else:
        send_message(cid, "⚠️ اسکرین‌شات ممکن نبود. از دکمه‌های زیر استفاده کنید:",
                     reply_markup=site_view_kb(url_key))
    clear_state(cid)

def do_youtube_search_cmd(cid: int, query: str, page=0):
    bump(cid, "searches")
    chat_action(cid)
    results = youtube_search(query, 8)
    key = make_cache_key("yt", query, page)
    cache_set(key, results)
    set_state(cid, mode="yt_search", last_query=query, page=page, cache_key=key)
    if not results:
        send_message(cid, "❌ ویدیویی یافت نشد.", reply_markup=home_kb())
        return
    text = f"📺 *یوتیوب:* _{query}_\nروی ویدیو کلیک کنید:"
    kb = yt_results_kb(results, key, page, len(results)==8)
    send_message(cid, text, parse_mode="Markdown", reply_markup=kb)

def do_youtube_dl(cid: int, url: str):
    """Entry point for direct YouTube URL download — shows quality picker."""
    bump(cid, "downloads")
    if not ("youtu.be" in url or "youtube.com" in url):
        send_message(cid, "❌ لینک یوتیوب معتبر وارد کنید.", reply_markup=home_kb())
        return
    _youtube_quality_picker(cid, url)


def _youtube_quality_picker(cid: int, url: str, audio_only: bool = False):
    """Probe available formats and show quality selection keyboard to user."""
    send_message(cid, "⏳ در حال بررسی کیفیت‌های موجود…")
    chat_action(cid)
    info = youtube_get_formats(url)
    if not info:
        # Probe failed — fall back to direct download
        send_message(cid, "⚠️ بررسی کیفیت ممکن نبود. در حال دانلود با بهترین کیفیت…")
        chat_action(cid, "upload_video")
        result = youtube_download(url, audio_only=audio_only)
        _finish_video(cid, result)
        return

    # Cache info so callback can retrieve it.
    # info_key = "yti" + url_key (no underscores between fields in callback_data)
    url_key = store_url(url)
    info_key = "yti" + url_key          # e.g. "ytibdc4d2d4ad" — no internal _
    cache_set(info_key, [info])  # wrapped in list so cache_get works

    formats   = info.get("formats", [])
    subtitles = info.get("subtitles", [])
    title     = info.get("title", "")[:50]

    if audio_only or not formats:
        # Audio mode or no video formats found — skip quality picker
        _youtube_sub_picker(cid, url_key, info_key, fmt_spec="",
                            client="", audio_only=True)
        return

    # Build quality keyboard — callback: yt_qual_{url_key}_{info_key}_{idx}
    # url_key and info_key are pure hex (no underscores), idx is digits — safe to split
    rows = []
    for i, fmt in enumerate(formats[:8]):  # cap at 8 rows
        label = fmt["label"]
        rows.append([{"text": f"📹 {label}",
                      "callback_data": f"yt_qual_{url_key}_{info_key}_{i}"}])

    rows.append([{"text": "🎵 فقط صدا (MP3)",
                  "callback_data": f"yt_qualA_{url_key}_{info_key}"}])
    rows.append([{"text": "❌ لغو", "callback_data": "home"}])

    text = f"📺 *{title}*\n\nکیفیت دانلود را انتخاب کنید:"
    send_message(cid, text, parse_mode="Markdown",
                 reply_markup={"inline_keyboard": rows})

def _youtube_sub_picker(cid: int, url_key: str, info_key: str,
                         fmt_spec: str, client: str, audio_only: bool = False,
                         page: int = 0):
    """Show subtitle selection keyboard — 3-col, priority order, paginated."""
    info_list = cache_get(info_key)
    info = info_list[0] if info_list else {}
    subtitles = info.get("subtitles", [])
    title = info.get("title", "")[:50]

    if not subtitles:
        _youtube_execute_download(cid, url_key, info_key,
                                  fmt_spec, client, audio_only, sub_code="")
        return

    # Sort: fa first, en second, then alphabetical
    def _sub_priority(s):
        c = s["code"].split("-")[0]
        if c == "fa": return "0"
        if c == "en": return "1"
        return "2" + c
    subs_sorted = sorted(subtitles, key=_sub_priority)

    # Pagination: fit (KB_MAX_ROWS - 2) full rows of KB_MAX_COLS per page
    per_page = (getattr(_ctx, 'kb_rows', 10) - 2) * KB_MAX_COLS
    total = len(subs_sorted)
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(0, min(page, pages - 1))
    page_subs = subs_sorted[page * per_page: (page + 1) * per_page]

    def _sub_cb(code):
        sfmt    = _safe_fmt(fmt_spec)
        enc_cli = client.replace("_", "~~")  # encode underscores in client name
        return f"yt_sub_{url_key}_{info_key}_{code}_{int(audio_only)}_{sfmt}_{enc_cli}"

    # Build 3-column rows
    rows = []
    row = []
    for sub in page_subs:
        row.append({"text": f"💬 {sub['name']}", "callback_data": _sub_cb(sub["code"])})
        if len(row) == KB_MAX_COLS:
            rows.append(row); row = []
    if row:
        rows.append(row)

    # Pagination nav row
    nav = []
    if page > 0:
        nav.append({"text": "◀ قبلی",
                    "callback_data": f"yt_subpg_{url_key}_{info_key}_{page-1}_{int(audio_only)}_{_safe_fmt(fmt_spec)}_{client.replace(chr(95), chr(126)*2)}"})
    if page < pages - 1:
        nav.append({"text": f"بعدی ▶ ({page+1}/{pages})",
                    "callback_data": f"yt_subpg_{url_key}_{info_key}_{page+1}_{int(audio_only)}_{_safe_fmt(fmt_spec)}_{client.replace(chr(95), chr(126)*2)}"})
    if nav:
        rows.append(nav)

    # Fixed bottom row: no-sub + cancel
    rows.append([
        {"text": "⏭ بدون زیرنویس", "callback_data": _sub_cb("NONE")},
        {"text": "❌ لغو",           "callback_data": "home"},
    ])

    pg_label = f" (صفحه {page+1}/{pages})" if pages > 1 else ""
    send_message(cid,
                 f"💬 *{title}*\n\nزیرنویس دلخواه را انتخاب کنید{pg_label}:",
                 parse_mode="Markdown",
                 reply_markup={"inline_keyboard": rows})


def _safe_fmt(fmt_spec: str) -> str:
    """Encode fmt_spec for use in callback_data (replace + with ~, spaces with _)."""
    return fmt_spec.replace("+", "~").replace(" ", "_")


def _decode_fmt(encoded: str) -> str:
    """Reverse _safe_fmt encoding."""
    return encoded.replace("~", "+").replace("_", " ")


def _youtube_execute_download(cid: int, url_key: str, info_key: str,
                               fmt_spec: str, client: str,
                               audio_only: bool, sub_code: str):
    """Final step: perform the actual download and send to user."""
    url = get_url(url_key) or ""
    if not url:
        send_message(cid, "❌ لینک منقضی شده. دوباره امتحان کنید.", reply_markup=home_kb())
        return

    info_list = cache_get(info_key)
    info = info_list[0] if info_list else {}
    title = info.get("title", "")

    sub_label = f" + زیرنویس ({sub_code})" if sub_code else ""
    label = "🎵 صدا" if audio_only else "📺 ویدیو"
    send_message(cid,
                 f"⏳ در حال دانلود {label}{sub_label}…\n"
                 f"_(ممکن است چند دقیقه طول بکشد)_",
                 parse_mode="Markdown")
    chat_action(cid, "upload_video" if not audio_only else "upload_voice")

    result = youtube_download(
        url,
        audio_only=audio_only,
        fmt_spec=fmt_spec,
        sub_code=sub_code if sub_code != "NONE" else "",
        yt_client=client,
    )
    _finish_video(cid, result)


def _finish_video(cid: int, result):
    if not result:
        send_message(cid,
                     "❌ دانلود ناموفق بود.\n"
                     "• ویدیو ممکن است در دسترس نباشد\n"
                     "• ویدیوی دیگری امتحان کنید",
                     parse_mode="Markdown", reply_markup=home_kb())
        return
    data, fname = result
    fname = Path(fname).name
    size_mb = len(data) / 1024 / 1024
    log.info("_finish_video: %s  %.1fMB", fname, size_mb)
    if smart_send(cid, data, fname, caption="", media_type="video"):
        send_message(cid, f"✅ ارسال شد ({size_mb:.1f}MB).", reply_markup=home_kb())
    else:
        send_message(cid, "❌ ارسال ناموفق بود.", reply_markup=home_kb())
    clear_state(cid)


def do_music(cid: int, query: str):
    """
    دانلود موزیک — جستجو در همه پلتفرم‌ها یا دانلود مستقیم از لینک.
    """
    bump(cid, "downloads")

    # لینک مستقیم — دانلود فوری
    if query.startswith("http"):
        if "spotify.com" in query:
            do_spotify_dl(cid, query); return
        elif _is_youtube(query) or "soundcloud.com" in query:
            _do_audio_download(cid, query,
                               source="soundcloud" if "soundcloud.com" in query else "youtube"); return
        else:
            _do_audio_download(cid, query, source="auto"); return

    # جستجو — نمایش نتایج به صورت دکمه
    send_message(cid, f"🔍 در حال جستجو در همه پلتفرم‌ها: _{query}_…", parse_mode="Markdown")
    chat_action(cid)
    results = music_search_multi(query)

    if not results:
        send_message(cid, "❌ نتیجه‌ای یافت نشد. عبارت دیگری امتحان کنید.", reply_markup=home_kb())
        return

    key = make_cache_key("mu", query, 0)
    cache_set(key, results)
    set_state(cid, mode="music", last_query=query, cache_key=key)

    rows = []
    for i, r in enumerate(results):
        title    = r.get("title", f"آهنگ {i+1}")[:35]
        uploader = r.get("uploader", "")[:18]
        dur      = r.get("duration", "")
        source   = r.get("source", "")
        src_icon = {
            "youtube":    "▶️",
            "ytmusic":    "🎵",
            "soundcloud": "☁️",
            "jiosaavn":   "🎶",
            "deezer":     "🎧",
        }.get(source, "🎵")
        parts = [f"{src_icon} {title}"]
        if uploader: parts.append(f"— {uploader}")
        if dur:      parts.append(f"({dur})")
        label = " ".join(parts)[:60]
        rows.append([{"text": label, "callback_data": f"mu_dl_{key}_{i}"}])

    rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
    send_message(cid,
        f"🎵 *نتایج موزیک برای:* _{query}_\nروی آهنگ بزنید تا دانلود شود:",
        parse_mode="Markdown",
        reply_markup={"inline_keyboard": rows})


def _do_audio_download(cid: int, url: str, source: str = "auto",
                        title: str = "", artist: str = "",
                        track_item: dict = None):
    """دانلود صدا از URL یا جستجو با زنجیره fallback کامل."""
    bump(cid, "downloads")
    send_message(cid, "⏳ در حال دانلود آهنگ…")
    chat_action(cid, "record_voice")

    def _status(text: str):
        send_message(cid, text)

    result = None
    tried_ids: set = set()

    # JioSaavn و Deezer نیاز به تابع دانلود اختصاصی دارند
    if source == "jiosaavn" and track_item:
        result = jiosaavn_download(track_item)
        if not result:
            result = music_download_with_fallback(
                title or url, title=title, artist=artist,
                source_hint=source, status_cb=_status,
                _tried_yt_ids=tried_ids)
    elif source == "deezer" and track_item:
        result = deezer_download(track_item)
        if not result:
            result = music_download_with_fallback(
                title or url, title=title, artist=artist,
                source_hint=source, status_cb=_status,
                _tried_yt_ids=tried_ids)
    else:
        # برای YouTube/SoundCloud — یک بار مستقیم امتحان، سپس fallback
        # Extract YT video ID so fallback doesn't retry the same video
        if "youtube.com" in url or "youtu.be" in url:
            import urllib.parse as _up
            qs = _up.parse_qs(_up.urlparse(url).query)
            vid = qs.get("v", [""])[0] or url
            tried_ids.add(vid)
        result = music_download_ytdlp(url, source=source)
        if not result:
            _status("⚠️ دانلود مستقیم ناموفق — در حال جستجو در منابع دیگر…")
            result = music_download_with_fallback(
                url, title=title, artist=artist,
                source_hint=source, status_cb=_status,
                _tried_yt_ids=tried_ids)

    if not result:
        send_message(cid,
            "❌ دانلود آهنگ ناموفق بود.\n"
            "• عبارت دیگری جستجو کنید\n"
            "• ممکن است محتوا در همه منابع محدودیت داشته باشد",
            reply_markup=home_kb())
        return

    data, fname = result
    fname = Path(fname).name
    caption = f"🎵 {title or fname[:60]}"
    if smart_send(cid, data, fname, caption=caption, media_type="audio"):
        send_message(cid, "✅ ارسال شد!", reply_markup=home_kb())
    else:
        send_message(cid, "❌ ارسال فایل ناموفق بود.", reply_markup=home_kb())
    clear_state(cid)


def do_spotify_dl(cid: int, url: str):
    """Download Spotify track/album/playlist via spotdl, with full platform fallback."""
    bump(cid, "downloads")
    is_track    = "/track/" in url
    is_album    = "/album/" in url
    is_playlist = "/playlist/" in url
    kind = "آهنگ" if is_track else "آلبوم" if is_album else "پلیلیست" if is_playlist else "محتوا"

    send_message(cid, f"⏳ در حال دانلود {kind} از Spotify…")
    chat_action(cid, "record_voice")

    def _status(text: str):
        send_message(cid, text)

    # For single tracks, use full fallback chain; for albums/playlists only spotdl
    if is_track:
        result = music_download_with_fallback(
            url, source_hint="spotify", status_cb=_status)
    else:
        result = spotify_download(url)

    if not result:
        send_message(cid,
            "❌ دانلود از Spotify ناموفق بود.\n\n"
            "مطمئن شوید `spotdl` نصب است:\n"
            "`pip install spotdl`",
            parse_mode="Markdown", reply_markup=home_kb())
        return

    data, fname = result
    caption = f"🎵 {fname[:60]}"
    if smart_send(cid, data, fname, caption=caption):
        send_message(cid, "✅ ارسال شد!", reply_markup=home_kb())
    else:
        send_message(cid, "❌ ارسال ناموفق بود.", reply_markup=home_kb())
    clear_state(cid)


def do_images(cid: int, query: str, source: str, page=0):
    bump(cid, "searches")
    chat_action(cid, "upload_photo")
    source_names = {"bing":"🖼 Bing","pinterest":"📌 پینترست",
                    "pexels":"📷 Pixabay","wiki":"🎨 Wikimedia"}
    send_message(cid, f"⏳ جستجوی عکس [{source_names.get(source,source)}]: _{query}_…",
                 parse_mode="Markdown")
    offset = page * 6
    fn_map = {"bing": images_bing, "pinterest": images_pinterest,
              "pexels": images_pexels, "wiki": images_wikimedia}
    fn = fn_map.get(source, images_bing)
    results = fn(query, max_results=offset+8)
    page_results = results[offset:offset+6]
    key = make_cache_key(f"img_{source}", query, page)
    cache_set(key, results)
    set_state(cid, mode=f"img_{source}", last_query=query, page=page,
              img_source=source, cache_key=key)
    if not page_results:
        send_message(cid, "❌ تصویری یافت نشد.", reply_markup=home_kb())
        return
    sent = 0
    for r in page_results[:6]:
        try:
            img_bytes = r.get("_bytes") or download_bytes(r["url"], MAX_IMAGE_SIZE)
            if img_bytes and len(img_bytes) > 1000:
                if send_photo(cid, img_bytes, caption=r.get("title","")[:80]):
                    sent += 1
            time.sleep(0.3)
        except Exception as ex:
            log.error("do_images send: %s", ex)
    kb = images_more_kb(key, page, source)
    send_message(cid, f"✅ {sent} عکس ارسال شد.", reply_markup=kb)

def do_github_search(cid: int, query: str, page=0):
    bump(cid, "searches")
    chat_action(cid)
    results = github_search_repos(query, page)
    key = make_cache_key("gh", query, page)
    cache_set(key, results)
    set_state(cid, mode="gh_search", last_query=query, page=page, cache_key=key)
    if not results:
        send_message(cid, "❌ مخزنی یافت نشد.", reply_markup=home_kb())
        return
    text = f"🐙 *GitHub:* _{query}_  (صفحه {page+1})\nروی مخزن کلیک کنید:"
    kb = gh_repo_kb(results, key, page, len(results)==8)
    send_message(cid, text, parse_mode="Markdown", reply_markup=kb)

def do_github_zip(cid: int, url: str):
    bump(cid, "downloads")
    send_message(cid, "⏳ در حال دانلود ZIP مخزن…")
    data = github_zip(url)
    if not data:
        send_message(cid, "❌ دانلود ناموفق.", reply_markup=home_kb())
        return
    m = re.search(r"github\.com/([^/]+/[^/]+?)(?:\.git|/|$)", url)
    slug = m.group(1).replace("/","_") if m else "repo"
    if send_document(cid, data, f"{slug}.zip", caption=f"📥 {url[:60]}"):
        send_message(cid, "✅ ZIP ارسال شد.", reply_markup=home_kb())
    else:
        send_message(cid, "❌ ارسال ناموفق.", reply_markup=home_kb())
    clear_state(cid)

def do_github_release(cid: int, repo_full: str, page: int = 0):
    """Show GitHub release assets with full pagination — no asset left behind."""
    bump(cid, "downloads")
    chat_action(cid)
    rel = github_latest_release(repo_full)
    if not rel:
        send_message(cid, "❌ هیچ Release‌ای یافت نشد.", reply_markup=home_kb())
        return

    tag    = rel.get("tag_name", "")
    body   = (rel.get("body") or "")[:400]
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
                lines_txt.append(f"{i+1}. {a['name']} ({size_mb}MB)")
        info_text = "\n".join(lines_txt)
    else:
        info_text = f"📦 *{repo_full}* — {tag}\n\nانتخاب فایل برای دانلود:"

    # Pagination: (KB_MAX_ROWS - 2) assets per page, 1 per row
    per_page = getattr(_ctx, 'kb_rows', 10) - 2
    total  = len(assets)
    pages  = max(1, (total + per_page - 1) // per_page)
    page   = max(0, min(page, pages - 1))
    page_assets = assets[page * per_page: (page + 1) * per_page]

    rows = []
    for i, a in enumerate(page_assets):
        real_idx = page * per_page + i
        size_mb  = a.get("size", 0) / 1024 / 1024
        rows.append([{"text": f"⬇️ {a['name'][:38]} ({size_mb:.0f}MB)",
                      "callback_data": f"ghrel_dl_{safe_repo}_{real_idx}"}])

    # Navigation row
    nav = []
    if page > 0:
        nav.append({"text": "◀ قبلی",
                    "callback_data": f"ghrel_pg_{safe_repo}_{page-1}"})
    if page < pages - 1:
        nav.append({"text": f"بعدی ▶ ({page+1}/{pages})",
                    "callback_data": f"ghrel_pg_{safe_repo}_{page+1}"})
    if nav:
        rows.append(nav)

    rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])

    send_message(cid, info_text, parse_mode="Markdown",
                 reply_markup={"inline_keyboard": rows})


def do_ocr_photo(cid: int, photos: list, reply_id=None):
    bump(cid, "ocr")
    chat_action(cid)
    photo = sorted(photos, key=lambda p: p.get("file_size",0))[-1]
    if photo.get("file_size",0) > MAX_OCR_SIZE:
        send_message(cid, "❌ حجم عکس بیشتر از ۵MB است.")
        return
    url = get_file_url(photo["file_id"])
    if not url:
        send_message(cid, "❌ خطا در دریافت فایل."); return
    data = download_bytes(url, MAX_OCR_SIZE)
    if not data:
        send_message(cid, "❌ خطا در دانلود عکس."); return
    send_message(cid, "⏳ در حال پردازش تصویر…")
    extracted = ocr_image(data)
    send_message(cid, f"📝 *متن استخراج شده:*\n\n{extracted[:3500]}",
                 parse_mode="Markdown", reply_to_message_id=reply_id)
    try:
        pdf = ocr_to_pdf(extracted)
        send_document(cid, pdf, "ocr_result.pdf", caption="📑 متن OCR به‌صورت PDF")
    except Exception as e:
        log.error("ocr_to_pdf: %s", e)
    clear_state(cid)
    send_message(cid, "✅ OCR انجام شد.", reply_markup=home_kb())

def do_translate(cid: int, text: str, lang: str):
    bump(cid, "translations")
    chat_action(cid)
    result = translate_text(text, lang)
    send_message(cid, f"🌐 *ترجمه:*\n\n{result}",
                 parse_mode="Markdown", reply_markup=home_kb())
    clear_state(cid)

def do_currency(cid: int, text: str):
    m = re.match(r"([\d,\.]+)\s+([A-Za-z]{3})\s+(?:to\s+)?([A-Za-z]{3})",
                 text.strip(), re.IGNORECASE)
    if not m:
        send_message(cid, "❌ فرمت اشتباه. مثال: `100 USD to IRR`",
                     parse_mode="Markdown", reply_markup=home_kb())
        return
    amount, frm, to = m.groups()
    amount = float(amount.replace(",",""))
    result = currency_convert(amount, frm, to)
    send_message(cid, f"💱 *{result}*" if result else "❌ خطا در تبدیل.",
                 parse_mode="Markdown", reply_markup=home_kb())
    clear_state(cid)

def do_ip_lookup(cid: int, target: str):
    chat_action(cid)
    result = ip_lookup(target.strip())
    send_message(cid, result, parse_mode="Markdown", reply_markup=home_kb())
    clear_state(cid)

def do_shorten(cid: int, url: str):
    if not url.startswith("http"): url = "https://"+url
    result = shorten_url(url)
    send_message(cid, f"🔗 {result}", reply_markup=home_kb())
    clear_state(cid)

def do_expand(cid: int, url: str):
    if not url.startswith("http"): url = "https://"+url
    result = expand_url(url)
    send_message(cid, result, parse_mode="Markdown", reply_markup=home_kb())
    clear_state(cid)

def do_paste(cid: int, text: str):
    url = paste_text(text)
    if url:
        send_message(cid, f"📋 آپلود شد:\n{url}", reply_markup=home_kb())
    else:
        send_message(cid, "❌ خطا در آپلود.", reply_markup=home_kb())
    clear_state(cid)

def do_qr(cid: int, text: str):
    chat_action(cid, "upload_photo")
    qr = generate_qr(text)
    if qr:
        send_photo(cid, qr, caption=f"📱 QR: {text[:60]}")
    else:
        send_message(cid, "❌ خطا در ساخت QR.")
    send_message(cid, "✅", reply_markup=home_kb())
    clear_state(cid)


# ═══════════════════════════════════════════════════════════════════════════════
# SOCIAL MEDIA HANDLERS
# ═══════════════════════════════════════════════════════════════════════════════

def do_zlib_search(cid: int, query: str, extensions: list = None):
    """جستجوی کتاب در Z-Library و نمایش نتایج به‌صورت دکمه."""
    bump(cid, "searches")
    if not ZLIB_EMAIL or not ZLIB_PASSWORD:
        log.error("do_zlib_search: ZLIB_EMAIL/PASSWORD not configured")
        send_message(cid, "❌ جستجوی کتاب در حال حاضر در دسترس نیست.",
                     reply_markup=home_kb())
        return

    send_message(cid, f"⏳ در حال جستجو در Z-Library: _{query}_…",
                 parse_mode="Markdown")
    chat_action(cid)
    results = zlib_search(query, count=10, extensions=extensions)

    if not results:
        log.warning("do_zlib_search: no results for %r", query)
        send_message(cid, "❌ نتیجه‌ای یافت نشد. کلمات دیگری امتحان کنید.",
                     reply_markup=home_kb())
        return

    key = make_cache_key("zl", query, 0)
    cache_set(key, results)
    set_state(cid, mode="zlib", last_query=query, cache_key=key,
              zlib_ext=extensions)

    ext_str = f" [{', '.join(extensions)}]" if extensions else ""
    text = f"📚 *نتایج Z-Library{ext_str}:* _{query}_\nروی کتاب کلیک کنید:"
    kb = zlib_results_kb(results, key)
    send_message(cid, text, parse_mode="Markdown", reply_markup=kb)


def do_zlib_show_book(cid: int, book: dict):
    """نمایش اطلاعات کامل کتاب + دکمه دانلود."""
    name      = book.get("name", "نامشخص")
    authors   = book.get("authors", [])
    year      = book.get("year", "")
    publisher = book.get("publisher", "")
    language  = book.get("language", "")
    extension = book.get("extension", "").upper()
    size      = book.get("size", "")
    cover     = book.get("cover", "")
    rating    = book.get("rating", "")
    url       = book.get("url", "")

    # ارسال کاور کتاب
    if cover:
        try:
            img_bytes = download_bytes(cover, MAX_IMAGE_SIZE)
            if img_bytes and len(img_bytes) > 500:
                send_photo(cid, img_bytes, caption=name[:80])
        except Exception as e:
            log.warning("zlib cover: %s", e)

    # متن اطلاعات
    lines = [f"📚 *{name}*"]
    if authors:
        auth_str = ", ".join(
            a["author"] if isinstance(a, dict) else str(a)
            for a in authors[:3]
        )
        lines.append(f"✍️ {auth_str}")
    if year:      lines.append(f"📅 سال: {year}")
    if publisher: lines.append(f"🏢 ناشر: {publisher}")
    if language:  lines.append(f"🌐 زبان: {language}")
    if extension: lines.append(f"📄 فرمت: {extension}")
    if size:      lines.append(f"💾 حجم: {size}")
    if rating:    lines.append(f"⭐ امتیاز: {rating}")

    # کلید دانلود با URL کتاب
    url_key = store_url(url) if url else ""

    send_message(cid, "\n".join(lines), parse_mode="Markdown",
                 reply_markup=zlib_book_kb(url_key) if url_key else home_kb())


def do_zlib_download(cid: int, book_url: str):
    """دانلود فایل کتاب از Z-Library و ارسال به کاربر."""
    bump(cid, "downloads")
    send_message(cid, "⏳ در حال دانلود کتاب از Z-Library…\n_(ممکن است چند ثانیه طول بکشد)_",
                 parse_mode="Markdown")
    chat_action(cid, "upload_document")

    result = zlib_download(book_url)
    if not result:
        log.error("do_zlib_download: download failed for %s", book_url)
        send_message(cid, "❌ دانلود ناموفق بود. دوباره امتحان کنید.",
                     reply_markup=home_kb())
        return

    data, fname = result
    log.info("zlib: sending %s  %.1fMB", fname, len(data)/1024/1024)
    if smart_send(cid, data, fname, caption=f"📚 {fname[:80]}"):
        send_message(cid, "✅ کتاب ارسال شد.", reply_markup=home_kb())
    else:
        send_message(cid, "❌ ارسال فایل ناموفق بود.", reply_markup=home_kb())
    clear_state(cid)


def do_apk_search(cid: int, query: str):
    """Search Google Play and show results as clickable buttons."""
    bump(cid, "searches")
    send_message(cid, f"🔍 در حال جستجو در Google Play: _{query}_…", parse_mode="Markdown")
    chat_action(cid)
    results = gplay_search(query, n_hits=8)
    if not results:
        send_message(cid,
            "❌ اپلیکیشنی یافت نشد.\n"
            "• نام پکیج را امتحان کنید (مثل `org.telegram.messenger`)\n"
            "• اپ ممکن است در منطقه شما در دسترس نباشد",
            parse_mode="Markdown", reply_markup=home_kb())
        return
    key = make_cache_key("apk", query, 0)
    cache_set(key, results)
    set_state(cid, mode="apk", last_query=query, cache_key=key)
    kb = apk_results_kb(results, key)
    send_message(cid, f"📱 *نتایج Google Play برای:* _{query}_\nبرای جزئیات روی اپ کلیک کنید:",
                 parse_mode="Markdown", reply_markup=kb)


def do_apk_show(cid: int, app: dict):
    """Show full app info with icon, then download button."""
    app_id  = app.get("app_id", "")
    title   = app.get("title", "Unknown")
    dev     = app.get("developer", "")
    score   = app.get("score", 0)
    inst    = app.get("installs", "")
    size    = app.get("size", "")
    version = app.get("version", "")
    android = app.get("android", "")
    cat     = app.get("category", "")
    summary = app.get("summary", "") or app.get("description", "")
    free    = app.get("free", True)
    price   = app.get("price", "Free")
    icon    = app.get("icon", "")

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
    if dev:     lines.append(f"👤 Developer: {dev}")
    if score:   lines.append(f"⭐ Rating: {score:.1f}/5")
    if inst:    lines.append(f"📊 Installs: {inst}")
    if version: lines.append(f"🔢 Version: {version}")
    if size:    lines.append(f"💾 Size: {size}")
    if android: lines.append(f"🤖 Android: {android}")
    if cat:     lines.append(f"🏷 Category: {cat}")
    lines.append(f"💳 Price: {'Free' if free else price}")
    if summary: lines.append(f"\n_{summary[:300]}_")
    lines.append(f"\n`{app_id}`")

    send_message(cid, "\n".join(lines), parse_mode="Markdown",
                 reply_markup=apk_item_kb(app_id))


def do_apk_download(cid: int, app_id: str):
    """Download APK and send to user."""
    bump(cid, "downloads")
    # Get fresh info
    info = gplay_app_info(app_id)
    name = info.get("title", app_id) if info else app_id
    size = info.get("size", "") if info else ""
    size_hint = f" (~{size})" if size else ""

    send_message(cid,
        f"⏳ در حال دانلود APK: *{name}*{size_hint}\n"
        f"`{app_id}`\n\n"
        "در حال امتحان منابع مختلف (APKPure → APKMirror → F-Droid)…",
        parse_mode="Markdown")
    chat_action(cid, "upload_document")

    result = apk_download(app_id)
    if not result:
        send_message(cid,
            "❌ دانلود APK ناموفق بود.\n\n"
            "دلایل احتمالی:\n"
            "• اپ در میرورهای رایگان موجود نیست\n"
            "• اپ نیاز به دستگاه یا منطقه خاص دارد\n"
            "• اپ پولی است (فقط اپ‌های رایگان پشتیبانی می‌شوند)\n\n"
            f"دانلود دستی: https://apkpure.com/{app_id}/{app_id}",
            parse_mode="Markdown", reply_markup=home_kb())
        return

    data, fname = result
    size_mb = len(data) / 1024 / 1024
    log.info("APK: sending %s %.1fMB", fname, size_mb)

    if smart_send(cid, data, fname, caption=f"📱 {name} — {fname}"):
        send_message(cid,
            f"✅ APK ارسال شد! ({size_mb:.1f}MB)\n\n"
            "⚠️ *نکته:* گزینه «نصب از منابع ناشناس» را در تنظیمات اندروید فعال کنید.",
            parse_mode="Markdown", reply_markup=home_kb())
    else:
        send_message(cid, "❌ ارسال ناموفق بود.", reply_markup=home_kb())
    clear_state(cid)


def do_rss(cid: int, query: str):
    """Handle RSS input: URL → fetch feed; non-URL → search for feeds on site."""
    bump(cid, "searches")
    chat_action(cid)

    if query.startswith("http"):
        # Direct RSS URL or website URL → auto-discover
        if not any(x in query for x in ["rss", "feed", "atom", "xml"]):
            send_message(cid, "🔍 در حال جستجوی RSS در سایت…")
            feeds = rss_search_feeds(query)
            if not feeds:
                # Try the URL itself as a feed
                feeds = [query]
            if len(feeds) > 1:
                rows = [[{"text": f"📰 {f[:50]}", "callback_data": f"rss_url_{store_url(f)}"}]
                        for f in feeds[:5]]
                rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
                send_message(cid, f"📰 {len(feeds)} فید یافت شد:",
                             reply_markup={"inline_keyboard": rows})
                return
            query = feeds[0]

        # Fetch the feed
        send_message(cid, f"⏳ در حال دریافت فید RSS…")
        items = rss_fetch(query, 15)
        if not items:
            send_message(cid,
                "❌ فید یافت نشد یا معتبر نیست.\n"
                "آدرس فید RSS را مستقیم وارد کنید (مثال: https://example.com/rss)",
                reply_markup=home_kb())
            return
        key = make_cache_key("rss", query, 0)
        cache_set(key, items)
        store_url(query)
        set_state(cid, mode="rss", last_query=query, cache_key=key)
        _display_rss_list(cid, items, key)
    else:
        # Non-URL: treat as search query for news
        send_message(cid,
            "📰 آدرس فید RSS یا سایت خبری را وارد کنید:\n\n"
            "مثال‌ها:\n"
            "• `https://www.bbc.com/persian/index.xml`\n"
            "• `https://www.isna.ir` ← کشف خودکار\n"
            "• `https://feeds.bbci.co.uk/news/rss.xml`",
            parse_mode="Markdown", reply_markup=cancel_kb())


def _display_rss_list(cid: int, items: list, key: str):
    """Show RSS items as clickable buttons."""
    rows = []
    for i, item in enumerate(items[:15]):
        title = item.get("title", f"خبر {i+1}")[:45]
        rows.append([{"text": f"📰 {title}", "callback_data": f"rss_item_{key}_{i}"}])
    rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
    send_message(cid, f"📰 *{len(items)} خبر دریافت شد:*\nروی هر خبر کلیک کنید:",
                 parse_mode="Markdown",
                 reply_markup={"inline_keyboard": rows})


def _show_rss_item(cid: int, item: dict):
    """Display a single RSS item with full text and auto-send image."""
    title = item.get("title", "")
    link  = item.get("link", "")
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
    url_key = store_url(link) if link else ""
    kb = site_view_kb(url_key) if url_key else home_kb()
    send_message(cid, text or "_(خبر بدون متن)_",
                 parse_mode="Markdown", reply_markup=kb)


def do_tg_read(cid: int, channel: str, use_mtproto: bool = False):
    """Read Telegram channel messages and show as clickable buttons."""
    bump(cid, "searches")
    channel = channel.strip().lstrip("@")
    send_message(cid, f"⏳ در حال دریافت پیام‌های @{channel}…")
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
            send_message(cid,
                "❌ MTProto ناموفق بود.\n"
                "مطمئن شوید TG_API_ID و TG_API_HASH تنظیم شده‌اند.",
                reply_markup=home_kb())
            return
    else:
        messages = tg_channel_read_web(channel, 20)
        if not messages:
            send_message(cid,
                "❌ پیامی یافت نشد.\n"
                "• کانال باید عمومی باشد\n"
                "• نام کانال را بررسی کنید (بدون @)",
                reply_markup=home_kb())
            return

    key = make_cache_key("tg", channel, 0)
    cache_set(key, messages)
    set_state(cid, mode="tg_channel", last_query=channel, cache_key=key)

    title = f"✈️ @{channel} — آخرین {len(messages)} پیام"
    kb = social_results_kb(messages, key, "tg")
    send_message(cid, title, reply_markup=kb)


def do_tg_show_message(cid: int, msg_data: dict):
    """Display a Telegram message: text + auto-download images + video button."""
    text      = msg_data.get("text", "")
    date      = msg_data.get("date", "")
    url       = msg_data.get("url", "")
    img_urls  = msg_data.get("img_urls", [])
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
                send_photo(cid, img_bytes, caption="✈️")
                log.info("tg_show: sent image %dKB", len(img_bytes)//1024)
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
        send_message(cid, "_(پیام بدون محتوای قابل نمایش)_",
                     parse_mode="Markdown", reply_markup=home_kb())
    else:
        send_message(cid, "✅", reply_markup=home_kb())


def do_tg_dl_media(cid: int, msg_url: str):
    """Download media from a Telegram message URL."""
    bump(cid, "downloads")
    send_message(cid, f"⏳ در حال دانلود رسانه از:\n{msg_url}")
    chat_action(cid, "upload_document")
    result = tg_download_media_web(msg_url)
    if not result:
        send_message(cid, "❌ دانلود ناموفق بود — این پیام ممکن است رسانه نداشته باشد.",
                     reply_markup=home_kb())
        return
    data, fname = result
    smart_send(cid, data, fname, caption=f"✈️ {fname[:60]}")
    send_message(cid, "✅ ارسال شد.", reply_markup=home_kb())
    clear_state(cid)


def do_twitter_timeline(cid: int, username: str):
    """Fetch and display Twitter/X user timeline via Nitter."""
    bump(cid, "searches")
    username = username.strip().lstrip("@")
    send_message(cid, f"⏳ در حال دریافت توییت‌های @{username}…")
    chat_action(cid)
    tweets = twitter_get_channel(username, 20)
    if not tweets:
        send_message(cid,
            "❌ توییتی یافت نشد.\n"
            "• ممکن است Nitter instances در دسترس نباشند\n"
            "• نام کاربری را بررسی کنید",
            reply_markup=home_kb())
        return
    key = make_cache_key("tw", username, 0)
    cache_set(key, tweets)
    set_state(cid, mode="twitter", last_query=username, cache_key=key)
    kb = social_results_kb(tweets, key, "tw")
    send_message(cid, f"🐦 @{username} — {len(tweets)} توییت اخیر:", reply_markup=kb)


def do_twitter_show(cid: int, tweet: dict):
    """Display tweet, auto-download photos, show video download button."""
    text      = tweet.get("text", "")
    date      = tweet.get("date", "")
    url       = tweet.get("url", "")
    has_vid   = tweet.get("has_video", False)
    img_urls  = tweet.get("img_urls", [])

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
    if has_vid or not img_urls:
        send_message(cid, "📥 دانلود ویدیو؟", reply_markup=kb)
    else:
        send_message(cid, "✅", reply_markup=home_kb())


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
            r = WEB.get(f"{api_base}/{username}/status/{tweet_id}",
                        headers={"User-Agent": UA_DESK}, timeout=12)
            if r.status_code != 200:
                continue
            jdata = r.json()
            tw = jdata.get("tweet") or jdata
            result = {
                "text":          (tw.get("text") or tw.get("full_text") or tw.get("tweetText") or "").strip(),
                "author_name":   tw.get("user_name") or tw.get("author") or username,
                "author_handle": tw.get("user_screen_name") or tw.get("authorHandle") or username,
                "avatar_url":    tw.get("user_profile_image_url") or tw.get("authorAvatar") or "",
                "likes":         tw.get("likes") or tw.get("favorite_count") or 0,
                "retweets":      tw.get("retweets") or tw.get("retweet_count") or 0,
                "replies":       tw.get("replies") or tw.get("reply_count") or 0,
                "views":         tw.get("views") or tw.get("view_count") or 0,
                "created_at":    tw.get("date") or tw.get("created_at") or "",
                "is_verified":   tw.get("is_verified") or tw.get("user_verified") or False,
            }
            if result["text"]:
                log.info("_fetch_tweet_data: OK from %s (%d chars)", api_base, len(result["text"]))
                return result
        except Exception as e:
            log.debug("_fetch_tweet_data %s: %s", api_base, e)
    return {}


def _render_tweet_card(tw: dict, tweet_url: str) -> Optional[bytes]:
    """
    Render a tweet card as PNG: Vazirmatn font, RTL support, media embed, X dark theme.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
        from io import BytesIO as _BIO
        import textwrap as _tw
    except ImportError:
        log.warning("_render_tweet_card: Pillow not installed")
        return None
    try:
        # Download / cache Vazirmatn font
        font_dir  = Path(tempfile.gettempdir()) / "bot_fonts"
        font_dir.mkdir(exist_ok=True)
        font_reg  = font_dir / "Vazirmatn-Regular.ttf"
        font_bold = font_dir / "Vazirmatn-Bold.ttf"
        VAZIR_URLS = {
            font_reg:  "https://github.com/rastikerdar/vazirmatn/raw/master/fonts/ttf/Vazirmatn-Regular.ttf",
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
                try: return ImageFont.truetype(str(p), size)
                except Exception: pass
            for fb in (["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"] if bold
                       else ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]):
                if Path(fb).exists(): return ImageFont.truetype(fb, size)
            return ImageFont.load_default()

        W, PAD = 720, 28
        INNER  = W - PAD * 2
        CARD   = (21,  32,  43)
        TEXT   = (231, 233, 234)
        MUTED  = (113, 118, 123)
        BLUE   = (29,  155, 240)
        LIKE_C = (249,  24, 128)
        BORDER = (47,   51,  54)

        fn_name   = _fnt(17, bold=True)
        fn_handle = _fnt(15)
        fn_body   = _fnt(17)
        fn_small  = _fnt(13)
        fn_stats  = _fnt(15)

        def _bb(text, font):
            d = ImageDraw.Draw(Image.new("RGB", (1,1)))
            b = d.textbbox((0,0), text, font=font)
            return b[2]-b[0], b[3]-b[1]

        def _is_rtl(text):
            return any("\u0600" <= c <= "\u06FF" or "\uFB50" <= c <= "\uFDFF"
                       or "\uFE70" <= c <= "\uFEFF" for c in text)

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
                av = Image.open(_BIO(av_data)).convert("RGBA").resize((av_size, av_size))
                mask = Image.new("L", (av_size, av_size), 0)
                ImageDraw.Draw(mask).ellipse([(0,0),(av_size,av_size)], fill=255)
                base = Image.new("RGBA", (av_size, av_size), CARD+(255,))
                base.paste(av, mask=mask)
                avatar = base
            except Exception: pass

        # Embedded media thumbnail
        media_img = None
        try:
            tw_clean = tweet_url.replace("x.com/", "twitter.com/")
            mx = re.search(r"twitter\.com/([^/]+)/status/(\d+)", tw_clean)
            if mx:
                u2, tid = mx.group(1), mx.group(2)
                for api_b in ["https://api.vxtwitter.com","https://api.fxtwitter.com"]:
                    try:
                        rj = WEB.get(f"{api_b}/{u2}/status/{tid}",
                                     headers={"User-Agent": UA_DESK}, timeout=10).json()
                        tw_obj = rj.get("tweet") or rj
                        ml = (tw_obj.get("media",{}).get("all",[]) or
                              tw_obj.get("media_extended",[]) or [])
                        for med in ml:
                            thumb = med.get("thumbnail_url") or med.get("media_url_https","")
                            if thumb and "pbs.twimg" in thumb:
                                mi_data = WEB.get(thumb+":large", timeout=10).content
                                mi = Image.open(_BIO(mi_data)).convert("RGB")
                                ratio = INNER / mi.width
                                mi = mi.resize((INNER, int(mi.height*ratio)), Image.LANCZOS)
                                media_img = mi
                                break
                        if media_img: break
                    except Exception: continue
        except Exception: pass

        # Body lines
        body   = tw.get("text", "")
        blines = _wrap(body, fn_body)
        lh     = _bb("Agم", fn_body)[1] + 5
        body_h = len(blines) * lh

        # Date
        date_str = tw.get("created_at","")
        if date_str:
            try:
                from datetime import datetime as _dt
                for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ","%a %b %d %H:%M:%S +0000 %Y",
                            "%Y-%m-%dT%H:%M:%SZ","%Y-%m-%dT%H:%M:%S"):
                    try: date_str = _dt.strptime(date_str,fmt).strftime("%-I:%M %p · %b %-d, %Y"); break
                    except ValueError: continue
            except Exception: pass

        # Height
        MH = (media_img.height + PAD) if media_img else 0
        H  = PAD + av_size + PAD + body_h + PAD + MH + 22 + PAD//2 + 1 + PAD//2 + 28 + PAD

        img  = Image.new("RGB", (W, H), CARD)
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle([(1,1),(W-2,H-2)], radius=12, outline=BORDER, width=1)

        # Avatar row
        if avatar: img.paste(avatar, (PAD, PAD), avatar)
        else: draw.ellipse([(PAD,PAD),(PAD+av_size,PAD+av_size)], fill=BLUE)

        nx = PAD + av_size + 12
        draw.text((nx, PAD+2),  tw.get("author_name","")[:30], font=fn_name,   fill=TEXT)
        handle_str = "@" + tw.get("author_handle", "")
        draw.text((nx, PAD+24), handle_str, font=fn_handle, fill=MUTED)
        xw, _ = _bb("𝕏", fn_name)
        draw.text((W-PAD-xw, PAD+4), "𝕏", font=fn_name, fill=MUTED)

        # Body
        ty = PAD + av_size + PAD
        for line in blines:
            _draw_line(draw, ty, line, fn_body, TEXT)
            ty += lh

        # Media
        if media_img:
            ty += PAD//2
            rmask = Image.new("L", media_img.size, 0)
            ImageDraw.Draw(rmask).rounded_rectangle(
                [(0,0),(media_img.width-1,media_img.height-1)], radius=10, fill=255)
            img.paste(media_img, (PAD, ty), rmask)
            ty += media_img.height + PAD//2

        # Date
        ty += PAD//4
        draw.text((PAD, ty), date_str, font=fn_small, fill=MUTED)
        ty += 22 + PAD//2

        # Divider
        draw.line([(PAD,ty),(W-PAD,ty)], fill=BORDER, width=1)
        ty += PAD//2

        # Stats — use text labels instead of emoji (emoji need special font)
        def _fmt(n):
            try:
                n = int(n or 0)
                if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
                if n >= 1_000:     return f"{n/1_000:.1f}K"
                return str(n)
            except: return "0"

        stat_items = [
            ("Reply",    tw.get("replies",  0), MUTED,   (5, 10, 5)),    # circle
            ("Repost",   tw.get("retweets", 0), MUTED,   (5, 10, 5)),
            ("Like",     tw.get("likes",    0), LIKE_C,  (5, 10, 5)),
            ("View",     tw.get("views",    0), MUTED,   (5, 10, 5)),
        ]
        # Draw small colored dot + label + count per stat
        sx, col_w = PAD, INNER // 4
        DOT_R = 5
        for label, val, color, _ in stat_items:
            dot_x, dot_y = sx, ty + fn_stats.size // 2 - DOT_R + 2
            draw.ellipse([(dot_x, dot_y),
                          (dot_x + DOT_R*2, dot_y + DOT_R*2)], fill=color)
            draw.text((sx + DOT_R*2 + 5, ty),
                      f"{_fmt(val)}", font=fn_stats, fill=color)
            # small muted label below count
            draw.text((sx + DOT_R*2 + 5, ty + fn_stats.size + 1),
                      label, font=fn_small, fill=MUTED)
            sx += col_w

        # If tweet has multiple media, show badge on top-right of card
        media_count = len(tw.get("_media_count_hint", []))
        if media_count > 1:
            badge = f"1/{media_count}"
            bw, bh = _bb(badge, fn_small)
            bx, by = W - PAD - bw - 10, PAD + 6
            draw.rounded_rectangle([(bx-4, by-3), (bx+bw+4, by+bh+3)],
                                   radius=6, fill=(0, 0, 0, 180))
            draw.text((bx, by), badge, font=fn_small, fill=TEXT)

        buf = _BIO()
        img.save(buf, format="PNG", optimize=True)
        card_bytes = buf.getvalue()
        log.info("_render_tweet_card: %dx%d  %.1fKB", W, H, len(card_bytes)/1024)
        return card_bytes

    except Exception as e:
        log.error("_render_tweet_card: %s", e, exc_info=True)
        return None


def do_twitter_dl(cid: int, url: str, _tw_data_override: dict = None):
    """Download ALL media from a tweet, send tweet card + media group + caption."""
    bump(cid, "downloads")
    send_message(cid, "⏳ در حال دانلود از توییت…")
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
            send_message(cid, "❌ این توییت رسانه‌ای ندارد یا دانلود ناموفق بود.",
                         reply_markup=home_kb())
        else:
            if caption:
                if original_text:
                    send_message(cid, f"🌐 *ترجمه:*\n{caption[:4096]}", parse_mode="Markdown")
                    send_message(cid, f"📝 *متن اصلی:*\n{original_text[:2000]}", parse_mode="Markdown")
                else:
                    send_message(cid, caption[:4096])
            send_message(cid, "✅ توییت ارسال شد.", reply_markup=home_kb())
        clear_state(cid)
        return

    BALE_CAPTION_LIMIT = 1024

    if len(results) == 1:
        data, fname = results[0]
        inline_cap = caption[:BALE_CAPTION_LIMIT] if len(caption) <= BALE_CAPTION_LIMIT else ""
        smart_send(cid, data, fname, caption=inline_cap)
    else:
        group_items = []
        for data, fname in results[:10]:
            is_vid = fname.lower().endswith((".mp4", ".mov", ".webm"))
            group_items.append({"data": data, "fname": fname,
                                 "is_video": is_vid, "caption": ""})
        send_media_group(cid, group_items)

    if original_text:
        send_message(cid, f"🌐 *ترجمه:*\n{caption[:4096]}", parse_mode="Markdown")
        send_message(cid, f"📝 *متن اصلی:*\n{original_text[:2000]}", parse_mode="Markdown")
    elif caption and (len(results) > 1 or len(caption) > BALE_CAPTION_LIMIT):
        send_message(cid, caption[:4096])

    send_message(cid, "✅ ارسال شد.", reply_markup=home_kb())
    clear_state(cid)


def do_instagram_profile(cid: int, username: str):
    """Fetch and display Instagram profile posts."""
    bump(cid, "searches")
    username = username.strip().lstrip("@")
    send_message(cid, f"⏳ در حال دریافت پست‌های @{username}…")
    chat_action(cid)
    posts = instagram_get_profile(username)
    if not posts:
        send_message(cid,
            "❌ پستی یافت نشد.\n"
            "• پروفایل باید عمومی باشد\n"
            "• برای پروفایل‌های خصوصی INSTAGRAM_USER/PASS تنظیم کنید",
            reply_markup=home_kb())
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
            timeout=10)
        if r.status_code == 200:
            j = r.json()
            title  = j.get("title", "").strip()
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
            headers={"User-Agent": UA_MOB}, timeout=10)
        if r.status_code == 200:
            j = r.json()
            title = j.get("title", "").strip()
            if title:
                return title
    except Exception as e:
        log.debug("_fetch_instagram_caption graph: %s", e)

    return ""


def do_instagram_dl(cid: int, url: str):
    """دانلود تمام رسانه‌های یک پست اینستاگرام (تکی / کاروسل / ریل) با کپشن کامل."""
    bump(cid, "downloads")
    send_message(cid, "⏳ در حال دانلود از اینستاگرام…")
    chat_action(cid, "upload_video")

    items = instagram_download_all(url)
    items = [it for it in items if it.get("data") and len(it["data"]) > 5000]

    if not items:
        send_message(cid,
            "❌ دانلود ناموفق بود.\n"
            "• پست باید عمومی باشد\n"
            "• برای پست‌های خصوصی INSTAGRAM_USER/PASS را تنظیم کنید",
            reply_markup=home_kb())
        return

    # Caption priority: instaloader → oEmbed → state
    caption = ""
    for it in items:
        if it.get("caption"):
            caption = it["caption"]; break
    if not caption:
        caption = get_state(cid).get("last_caption", "") or ""
    if not caption:
        caption = _fetch_instagram_caption(url)

    total = len(items)
    log.info("do_instagram_dl: sending %d items, caption_len=%d", total, len(caption))

    BALE_CAPTION_LIMIT = 1024

    if total == 1:
        # Single item — send with caption inline if it fits
        item = items[0]
        mtype = "video" if item.get("is_video") else "photo"
        inline_cap = caption[:BALE_CAPTION_LIMIT] if len(caption) <= BALE_CAPTION_LIMIT else ""
        smart_send(cid, item["data"], item["fname"],
                   caption=inline_cap, media_type=mtype)
        if caption and not inline_cap:
            send_message(cid, caption[:4096])
    else:
        # Multiple items — send as media group (album)
        # Caption on first item only, but only if short enough; otherwise separate msg
        group_items = []
        for i, item in enumerate(items[:10]):
            inline_cap = (caption[:BALE_CAPTION_LIMIT]
                          if i == 0 and len(caption) <= BALE_CAPTION_LIMIT else "")
            group_items.append({
                "data":     item["data"],
                "fname":    item["fname"],
                "is_video": item.get("is_video", False),
                "caption":  inline_cap,
            })
        send_media_group(cid, group_items)

        # If more than 10 items, send the rest individually
        for item in items[10:]:
            mtype = "video" if item.get("is_video") else "photo"
            smart_send(cid, item["data"], item["fname"], caption="", media_type=mtype)

        # Send long caption as separate message
        if caption and len(caption) > BALE_CAPTION_LIMIT:
            send_message(cid, caption[:4096])

    send_message(cid, f"✅ {total} مورد ارسال شد.", reply_markup=home_kb())
    clear_state(cid)



def do_tiktok_user(cid: int, username: str):
    """Fetch TikTok user video list."""
    bump(cid, "searches")
    username = username.strip().lstrip("@")
    send_message(cid, f"⏳ در حال دریافت ویدیوهای @{username}…")
    chat_action(cid)
    videos = tiktok_user_videos(username, 10)
    if not videos:
        send_message(cid,
            "❌ ویدیویی یافت نشد.\n"
            "• نام کاربری را بررسی کنید\n"
            "• پروفایل باید عمومی باشد",
            reply_markup=home_kb())
        return
    key = make_cache_key("tt", username, 0)
    cache_set(key, videos)
    set_state(cid, mode="tiktok", last_query=username, cache_key=key)
    kb = social_results_kb(videos, key, "tt")
    send_message(cid, f"🎵 @{username} — {len(videos)} ویدیو اخیر:", reply_markup=kb)


def _fetch_tiktok_caption(url: str) -> str:
    """Fetch TikTok video title/description via yt-dlp --dump-json."""
    try:
        r = subprocess.run(
            [YTDLP_BIN, "--no-warnings", "--no-check-certificate",
             "--dump-json", "--no-playlist", url],
            capture_output=True, text=True, timeout=20)
        if r.returncode == 0:
            import json as _j
            info = _j.loads(r.stdout)
            desc = info.get("description") or info.get("title") or ""
            return desc.strip()[:2000]
    except Exception as e:
        log.debug("_fetch_tiktok_caption: %s", e)
    return ""


def do_tiktok_dl(cid: int, url: str):
    """دانلود ویدیوی TikTok با کپشن کامل."""
    bump(cid, "downloads")
    send_message(cid, "⏳ در حال دانلود از TikTok…")
    chat_action(cid, "upload_video")
    result = tiktok_download(url)
    if not result:
        send_message(cid, "❌ دانلود ناموفق بود.", reply_markup=home_kb())
        return
    data, fname = result

    # Caption: state (from timeline browse) → yt-dlp metadata fetch
    caption = get_state(cid).get("last_caption", "") or _fetch_tiktok_caption(url)

    BALE_CAPTION_LIMIT = 1024
    inline_cap = caption[:BALE_CAPTION_LIMIT] if len(caption) <= BALE_CAPTION_LIMIT else ""
    smart_send(cid, data, fname, caption=inline_cap)
    if caption and not inline_cap:
        send_message(cid, caption[:4096])
    send_message(cid, "✅ ارسال شد.", reply_markup=home_kb())
    clear_state(cid)





# ═══════════════════════════════════════════════════════════════════════════════
# VIRUS SCAN  (VirusTotal free API — 4 req/min, 500 req/day, 32MB max)
# Set VIRUSTOTAL_API_KEY env var. Get key: https://www.virustotal.com/gui/join-us
# ═══════════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════════
# VIRUS SCAN  (VirusTotal via vt-py — 4 req/min, 500 req/day, 32MB max)
# pip install vt-py --break-system-packages
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

    log.info("virustotal_scan_file: %s  %.1fMB", filename, len(data)/1024/1024)
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
            malicious  = stats.get("malicious",  0)
            suspicious = stats.get("suspicious", 0)
            undetected = stats.get("undetected", 0)
            harmless   = stats.get("harmless",   0)
            total      = malicious + suspicious + undetected + harmless
            detections = sorted([
                f"{eng}: {res.get('result','?')}"
                for eng, res in (analysis.results or {}).items()
                if res.get("category") in ("malicious", "suspicious")
            ])[:20]
            verdict = ("malicious"  if malicious  > 0 else
                       "suspicious" if suspicious > 0 else "clean")
            # SHA256 is encoded in analysis id: file-{sha256}-{datetime}
            sha256 = ""
            parts = (analysis.id or "").split("-")
            if len(parts) >= 2 and len(parts[1]) == 64:
                sha256 = parts[1]
            log.info("VT file: %s  %d/%d", verdict, malicious, total)
            return {"malicious": malicious, "suspicious": suspicious,
                    "clean": undetected + harmless, "total": total,
                    "verdict": verdict, "detections": detections,
                    "sha256": sha256, "analysis_id": analysis.id}
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

            stats      = analysis.stats
            malicious  = stats.get("malicious",  0)
            suspicious = stats.get("suspicious", 0)
            undetected = stats.get("undetected", 0)
            harmless   = stats.get("harmless",   0)
            total      = malicious + suspicious + undetected + harmless
            detections = sorted([
                f"{eng}: {res.get('result','?')}"
                for eng, res in (analysis.results or {}).items()
                if res.get("category") in ("malicious", "suspicious")
            ])[:20]
            verdict = ("malicious" if malicious > 0 else
                       "suspicious" if suspicious > 0 else "clean")
            log.info("VT url result: %s  %d/%d", verdict, malicious, total)
            return {"malicious": malicious, "suspicious": suspicious,
                    "clean": undetected + harmless, "total": total,
                    "verdict": verdict, "detections": detections,
                    "sha256": "", "analysis_id": analysis.id}
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
        return f"⚠️ خطا: {result['error']}"

    verdict    = result.get("verdict", "unknown")
    malicious  = result.get("malicious",  0)
    suspicious = result.get("suspicious", 0)
    total      = result.get("total",      0)
    clean      = result.get("clean",      0)
    sha256     = result.get("sha256",     "")

    emoji = "✅" if verdict == "clean" else "⚠️" if verdict == "suspicious" else "🚨"
    verdict_fa = {"clean": "پاک", "suspicious": "مشکوک", "malicious": "مخرب"}.get(verdict, verdict)

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
        lines.append(f"\n🔗 [گزارش کامل]"
                     f"(https://www.virustotal.com/gui/file/{sha256})")
    return "\n".join(lines)


def do_virusscan(cid: int, data: bytes, filename: str):
    """Scan uploaded file with VirusTotal and report result."""
    if not VIRUSTOTAL_API_KEY:
        send_message(cid,
            "⚠️ کلید API VirusTotal تنظیم نشده است.\n"
            "متغیر محیطی `VIRUSTOTAL_API_KEY` را تنظیم کنید.\n"
            "کلید رایگان: https://www.virustotal.com/gui/join-us",
            parse_mode="Markdown", reply_markup=home_kb())
        return

    size_mb = len(data) / 1024 / 1024
    send_message(cid, f"🔍 در حال آپلود `{filename}` ({size_mb:.1f}MB) به VirusTotal…\n"
                       "ممکن است تا ۶۰ ثانیه طول بکشد.",
                 parse_mode="Markdown")
    chat_action(cid, "typing")

    result = virustotal_scan_file(data, filename)
    if result is None:
        send_message(cid, "❌ خطا در ارتباط با VirusTotal.", reply_markup=home_kb())
        return

    msg = _format_vt_result(result, filename)
    send_message(cid, msg, parse_mode="Markdown", reply_markup=home_kb())
    clear_state(cid)


def do_virusscan_url(cid: int, url: str):
    """Scan a URL with VirusTotal."""
    if not VIRUSTOTAL_API_KEY:
        send_message(cid,
            "⚠️ کلید API VirusTotal تنظیم نشده است.",
            reply_markup=home_kb())
        return
    send_message(cid, f"🔍 در حال بررسی آدرس در VirusTotal…")
    chat_action(cid, "typing")
    result = virustotal_scan_url(url)
    if result is None:
        send_message(cid, "❌ خطا در ارتباط با VirusTotal.", reply_markup=home_kb())
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
    send_message(cid, "⏳ در حال دریافت کاور ویدیو…")
    # yt-dlp --dump-json to get thumbnail URL, then try maxresdefault → hqdefault
    try:
        r = subprocess.run([YTDLP_BIN, "--no-warnings", "--dump-json",
                            "--no-playlist", url],
                           capture_output=True, text=True, timeout=20)
        if r.returncode == 0:
            import json as _j
            info = _j.loads(r.stdout)
            thumb_url = info.get("thumbnail", "")
            vid_id    = info.get("id", "")
            title     = info.get("title", "ویدیو")[:60]
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
                    send_message(cid, "✅ کاور ارسال شد.", reply_markup=home_kb())
                    return
    except Exception as e:
        log.error("do_youtube_thumbnail: %s", e)
    send_message(cid, "❌ دریافت کاور ناموفق بود.", reply_markup=home_kb())


def do_youtube_stats(cid: int, url: str):
    """Fetch YouTube video stats (views, likes, description) and send as a card."""
    bump(cid, "searches")
    send_message(cid, "⏳ در حال دریافت آمار ویدیو…")
    try:
        r = subprocess.run([YTDLP_BIN, "--no-warnings", "--dump-json",
                            "--no-playlist", url],
                           capture_output=True, text=True, timeout=25)
        if r.returncode != 0:
            send_message(cid, "❌ دریافت اطلاعات ناموفق بود.", reply_markup=home_kb())
            return
        import json as _j
        info = _j.loads(r.stdout)
        title    = info.get("title", "")[:80]
        uploader = info.get("uploader", info.get("channel", ""))
        views    = info.get("view_count", 0) or 0
        likes    = info.get("like_count", 0) or 0
        duration = info.get("duration_string", info.get("duration", ""))
        upload   = info.get("upload_date", "")
        desc     = (info.get("description") or "")[:800]
        vid_id   = info.get("id", "")

        def _fmt(n):
            try:
                n = int(n)
                if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
                if n >= 1_000:     return f"{n/1_000:.1f}K"
                return str(n)
            except Exception: return str(n)

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
        send_message(cid, "❌ دریافت آمار ناموفق بود.", reply_markup=home_kb())


# ═══════════════════════════════════════════════════════════════════════════════
# INSTAGRAM EXTRAS  (profile pic · profile screenshot · carousel mosaic)
# ═══════════════════════════════════════════════════════════════════════════════

def _ig_fetch_user_info(username: str) -> dict:
    """Fetch Instagram user info via public web API. Returns user dict or {}."""
    username = username.strip().lstrip("@")
    headers = {
        "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                       "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                       "Version/17.0 Mobile/15E148 Safari/604.1"),
        "X-IG-App-ID": "936619743392459",
        "Accept": "*/*",
        "Referer": f"https://www.instagram.com/{username}/",
    }
    try:
        r = WEB.get(
            f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}",
            headers=headers, timeout=15)
        if r.status_code == 200:
            return r.json().get("data", {}).get("user", {})
    except Exception as e:
        log.warning("_ig_fetch_user_info: %s", e)
    return {}


def do_instagram_profile_pic(cid: int, username: str):
    """Download and send Instagram profile picture at full size."""
    bump(cid, "downloads")
    username = username.strip().lstrip("@")
    send_message(cid, f"⏳ در حال دریافت عکس پروفایل @{username}…")

    # Try public API first
    user = _ig_fetch_user_info(username)
    pic_url = (user.get("profile_pic_url_hd") or
               user.get("profile_pic_url") or "")

    if not pic_url and INSTAGRAM_USER and INSTAGRAM_PASS:
        try:
            import instaloader
            L = instaloader.Instaloader(quiet=True, download_pictures=False)
            try: L.login(INSTAGRAM_USER, INSTAGRAM_PASS)
            except Exception: pass
            profile = instaloader.Profile.from_username(L.context, username)
            pic_url = profile.profile_pic_url
        except Exception as e:
            log.error("do_instagram_profile_pic instaloader: %s", e)

    if pic_url:
        data = download_bytes(pic_url, MAX_IMAGE_SIZE)
        if data and len(data) > 1000:
            send_photo(cid, data, caption=f"🖼 تصویر پروفایل @{username}")
            send_message(cid, "✅ ارسال شد.", reply_markup=home_kb())
            return

    send_message(cid, "❌ دریافت عکس پروفایل ناموفق بود.", reply_markup=home_kb())


def do_instagram_profile_screenshot(cid: int, username: str):
    """
    Build a profile card image with:
    bio, followers, following, post count, last 9 post thumbnails.
    Uses Pillow — no browser needed.
    """
    bump(cid, "downloads")
    username = username.strip().lstrip("@")
    send_message(cid, f"⏳ در حال ساخت اسکرین‌شات پروفایل @{username}…")
    try:
        from PIL import Image, ImageDraw, ImageFont
        from io import BytesIO as _BIO
        import textwrap as _tw

        # Fetch user info via public API (no login needed for public accounts)
        user = _ig_fetch_user_info(username)
        if not user and INSTAGRAM_USER and INSTAGRAM_PASS:
            try:
                import instaloader as _il
                L2 = _il.Instaloader(quiet=True, download_pictures=False)
                L2.login(INSTAGRAM_USER, INSTAGRAM_PASS)
                p2 = _il.Profile.from_username(L2.context, username)
                user = {
                    "full_name": p2.full_name, "biography": p2.biography,
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
            send_message(cid,
                "❌ پروفایل یافت نشد.\n• نام کاربری را بررسی کنید\n• پروفایل باید عمومی باشد",
                reply_markup=home_kb())
            return

        full_name  = user.get("full_name") or username
        bio        = user.get("biography") or ""
        followers  = user.get("edge_followed_by", {}).get("count", 0)
        following  = user.get("edge_follow", {}).get("count", 0)
        post_count = user.get("edge_owner_to_timeline_media", {}).get("count", 0)
        ext_url    = user.get("external_url") or ""
        is_verified= user.get("is_verified", False)
        pic_url    = user.get("profile_pic_url_hd") or user.get("profile_pic_url","")

        # Fetch 9 post thumbnails from web API data
        thumbs = []
        edges = user.get("edge_owner_to_timeline_media", {}).get("edges", [])
        for edge in edges[:9]:
            node = edge.get("node", {})
            turl = node.get("display_url","") or node.get("thumbnail_src","")
            if turl:
                td = download_bytes(turl, MAX_IMAGE_SIZE)
                if td: thumbs.append(td)
        # Fallback: instaloader posts if API gave no edges
        if not thumbs and "_il_profile" in user:
            for post in user["_il_profile"].get_posts():
                if len(thumbs) >= 9: break
                try:
                    td = download_bytes(post.url, MAX_IMAGE_SIZE)
                    if td: thumbs.append(td)
                except Exception: pass

        # ── Build card ──────────────────────────────────────────────────
        W, PAD = 640, 20
        BG     = (250, 250, 250)
        DARK   = (30,  30,  30)
        MUTED  = (120, 120, 120)
        PINK   = (228, 64,  95)

        def _fnt(size, bold=False):
            for p in (["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"] if bold
                      else ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]):
                if Path(p).exists():
                    return ImageFont.truetype(p, size)
            return ImageFont.load_default()

        fn_name  = _fnt(18, bold=True)
        fn_body  = _fnt(14)
        fn_small = _fnt(12)

        # Avatar
        AV = 80
        av_img = None
        try:
            av_data = download_bytes(pic_url, MAX_IMAGE_SIZE)
            av = Image.open(_BIO(av_data)).convert("RGBA").resize((AV, AV))
            mask = Image.new("L", (AV, AV), 0)
            ImageDraw.Draw(mask).ellipse([(0,0),(AV,AV)], fill=255)
            base = Image.new("RGBA", (AV, AV), BG+(255,))
            base.paste(av, mask=mask)
            av_img = base
        except Exception: pass

        # Bio wrap
        bio_lines = []
        for para in bio.split("\n"):
            bio_lines.extend(_tw.wrap(para, width=55) or [""])

        lh     = 18
        bio_h  = len(bio_lines) * lh
        GRID_S = (W - PAD*2 - 4) // 3  # size of each thumbnail cell

        HEADER_H = PAD + max(AV, 80) + PAD
        INFO_H   = 24 + PAD                     # stats row
        BIO_H    = bio_h + (PAD if bio else 0)
        URL_H    = 20 + PAD if ext_url else 0
        GRID_H   = (GRID_S + 2) if thumbs else 0
        if len(thumbs) > 3: GRID_H = (GRID_S + 2) * ((len(thumbs)-1)//3+1)
        H = HEADER_H + INFO_H + BIO_H + URL_H + GRID_H + PAD

        img  = Image.new("RGB", (W, H), BG)
        draw = ImageDraw.Draw(img)

        # Avatar
        ay = PAD
        if av_img:
            img.paste(av_img, (PAD, ay), av_img)

        # Name + verified
        nx = PAD + AV + PAD
        vbadge = " ✓" if is_verified else ""
        draw.text((nx, ay), full_name + vbadge, font=fn_name, fill=DARK)
        draw.text((nx, ay+22), f"@{username}", font=fn_body, fill=MUTED)

        # Stats row below avatar
        ty = PAD + max(AV, 60) + PAD//2

        def _fmt(n):
            if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
            if n >= 1_000:     return f"{n/1_000:.1f}K"
            return str(n)

        stats = [
            (str(post_count), "پست"),
            (_fmt(followers),  "فالوور"),
            (_fmt(following),  "دنبال‌شده"),
        ]
        col = W // 3
        for i, (val, label) in enumerate(stats):
            cx = i * col + col//2
            vw, _ = draw.textbbox((0,0), val, font=fn_name)[2:4]  # approximate
            draw.text((cx - 20, ty), val,   font=fn_name,  fill=DARK, align="center")
            draw.text((cx - 20, ty+20), label, font=fn_small, fill=MUTED)

        ty += INFO_H

        # Bio
        for line in bio_lines:
            draw.text((PAD, ty), line, font=fn_body, fill=DARK)
            ty += lh
        if bio: ty += PAD//2

        # External URL
        if ext_url:
            draw.text((PAD, ty), f"🔗 {ext_url[:60]}", font=fn_small, fill=PINK)
            ty += URL_H

        # Grid of thumbnails
        if thumbs:
            draw.line([(PAD, ty), (W-PAD, ty)], fill=(220,220,220), width=1)
            ty += PAD//2
            for i, tb in enumerate(thumbs[:9]):
                try:
                    ti = Image.open(_BIO(tb)).convert("RGB")
                    ti = ti.resize((GRID_S, GRID_S), Image.LANCZOS)
                    gx = PAD + (i % 3) * (GRID_S + 2)
                    gy = ty + (i // 3) * (GRID_S + 2)
                    img.paste(ti, (gx, gy))
                except Exception: pass

        buf = _BIO()
        img.save(buf, format="PNG", optimize=True)
        card = buf.getvalue()
        log.info("ig_profile_shot: %dx%d  %.1fKB", W, H, len(card)/1024)
        send_photo(cid, card, caption=f"📸 پروفایل @{username}")
        send_message(cid, "✅ ارسال شد.", reply_markup=home_kb())

    except Exception as e:
        log.error("do_instagram_profile_screenshot: %s", e)
        send_message(cid, "❌ ساخت اسکرین‌شات ناموفق بود.\n"
                     "• پروفایل باید عمومی باشد\n"
                     "• `instaloader` نصب باشد", reply_markup=home_kb())


def do_instagram_mosaic(cid: int, url: str):
    """
    Build a mosaic grid image from all carousel slides of an Instagram post.
    Sends the grid as a single PNG.
    """
    bump(cid, "downloads")
    send_message(cid, "⏳ در حال دانلود اسلایدها و ساخت موزاییک…")
    try:
        from PIL import Image
        from io import BytesIO as _BIO

        items = instagram_download_all(url)
        images = [it for it in items if not it.get("is_video") and it.get("data")]
        if not images:
            send_message(cid, "❌ تصویری برای موزاییک یافت نشد.", reply_markup=home_kb())
            return

        n    = len(images)
        cols = min(3, n)
        rows = (n + cols - 1) // cols
        S    = 320  # cell size

        grid = Image.new("RGB", (cols * S, rows * S), (30, 30, 30))
        for i, item in enumerate(images[:9]):
            try:
                tile = Image.open(_BIO(item["data"])).convert("RGB")
                # Crop to square centre
                w, h  = tile.size
                side  = min(w, h)
                tile  = tile.crop(((w-side)//2, (h-side)//2,
                                   (w+side)//2, (h+side)//2))
                tile  = tile.resize((S, S), Image.LANCZOS)
                gx    = (i % cols) * S
                gy    = (i // cols) * S
                grid.paste(tile, (gx, gy))
            except Exception: pass

        buf = _BIO()
        grid.save(buf, format="JPEG", quality=88, optimize=True)
        mosaic = buf.getvalue()
        log.info("do_instagram_mosaic: %dx%d  %d images  %.1fKB",
                 cols*S, rows*S, n, len(mosaic)/1024)
        caption = _fetch_instagram_caption(url)
        send_photo(cid, mosaic,
                   caption=(caption[:1024] if caption else f"🔲 موزاییک — {n} تصویر"))
        if caption and len(caption) > 1024:
            send_message(cid, caption[:4096])
        send_message(cid, "✅ ارسال شد.", reply_markup=home_kb())

    except Exception as e:
        log.error("do_instagram_mosaic: %s", e)
        send_message(cid, "❌ ساخت موزاییک ناموفق بود.", reply_markup=home_kb())


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
        except Exception: pass

    send_message(cid, "⏳ در حال دریافت اطلاعات آهنگ…")
    try:
        r = subprocess.run([YTDLP_BIN, "--no-warnings", "--dump-json",
                            "--no-playlist", url],
                           capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            # Fall back to direct download without info
            do_music(cid, url); return

        import json as _j
        info = _j.loads(r.stdout)
        title    = info.get("title", "")[:80]
        uploader = info.get("uploader", info.get("artist", ""))[:50]
        duration = info.get("duration_string", "")
        plays    = info.get("view_count", 0) or 0
        likes    = info.get("like_count", 0) or 0
        desc     = (info.get("description") or "")[:400]
        thumb    = info.get("thumbnail", "")

        def _fmt(n):
            try:
                n = int(n)
                if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
                if n >= 1_000:     return f"{n/1_000:.1f}K"
                return str(n)
            except Exception: return str(n)

        lines = [
            f"☁️ *{title}*",
            f"🎤 {uploader}",
            f"⏱ {duration}" + (f"  |  👁 {_fmt(plays)}" if plays else ""),
        ]
        if likes: lines.append(f"❤️ {_fmt(likes)}")
        if desc:  lines += ["", desc]

        if thumb:
            data = download_bytes(thumb, MAX_IMAGE_SIZE)
            if data and len(data) > 1000:
                send_photo(cid, data, caption="\n".join(lines)[:1024])
            else:
                send_message(cid, "\n".join(lines), parse_mode="Markdown")
        else:
            send_message(cid, "\n".join(lines), parse_mode="Markdown")

        # Offer download button
        url_key = store_url(url)
        kb = {"inline_keyboard": [[
            {"text": "⬇️ دانلود آهنگ", "callback_data": f"sc_dl_{url_key}"},
            {"text": "🏠 منوی اصلی",   "callback_data": "home"},
        ]]}
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
        r = WEB.get(f"{BSKY_API}/com.atproto.identity.resolveHandle",
                    params={"handle": handle},
                    headers={"Accept": "application/json"}, timeout=10)
        if r.status_code != 200:
            return None
        did = r.json().get("did", "")
        if not did:
            return None

        # Fetch post
        at_uri = f"at://{did}/app.bsky.feed.post/{rkey}"
        r2 = WEB.get(f"{BSKY_API}/app.bsky.feed.getPosts",
                     params={"uris": at_uri},
                     headers={"Accept": "application/json"}, timeout=10)
        if r2.status_code != 200:
            return None
        posts = r2.json().get("posts", [])
        if not posts:
            return None

        post  = posts[0]
        record = post.get("record", {})
        author = post.get("author", {})
        embed  = post.get("embed", {})

        # Extract media URLs
        media_items = []
        etype = embed.get("$type", "")
        if "images" in etype or "images" in embed:
            for img in embed.get("images", []):
                furl = img.get("fullsize") or img.get("thumb","")
                if furl:
                    media_items.append({"url": furl, "type": "image",
                                        "alt": img.get("alt","")})
        elif "video" in etype or "video" in embed:
            vurl = (embed.get("playlist") or embed.get("video",{}).get("ref",{})
                    .get("$link",""))
            if vurl:
                media_items.append({"url": vurl, "type": "video"})

        return {
            "text":        record.get("text",""),
            "author_name": author.get("displayName","") or author.get("handle",""),
            "author_handle": author.get("handle",""),
            "avatar_url":  author.get("avatar",""),
            "created_at":  record.get("createdAt",""),
            "likes":       post.get("likeCount",0),
            "reposts":     post.get("repostCount",0),
            "replies":     post.get("replyCount",0),
            "media":       media_items,
            "did":         did,
            "rkey":        rkey,
        }
    except Exception as e:
        log.error("bsky_get_post: %s", e)
        return None


def do_bluesky_dl(cid: int, url: str):
    """Download media + text from a Bluesky post."""
    bump(cid, "downloads")
    send_message(cid, "⏳ در حال دریافت از Bluesky…")
    chat_action(cid, "upload_photo")

    post = bsky_get_post(url)
    if not post:
        send_message(cid, "❌ پست یافت نشد یا خصوصی است.", reply_markup=home_kb())
        return

    text   = post.get("text","")
    author = post.get("author_handle","")
    media  = post.get("media", [])

    BALE_CAPTION_LIMIT = 1024

    if not media:
        # Text-only post — just send the text
        msg = f"🦋 *@{author}*\n\n{text}" if text else f"🦋 @{author}"
        send_message(cid, msg, parse_mode="Markdown", reply_markup=home_kb())
        return

    # Download all media items
    downloaded = []
    for item in media[:10]:
        murl  = item.get("url","")
        mtype = item.get("type","image")
        if not murl: continue
        data = download_bytes(murl, MAX_FILE_SIZE)
        if data and len(data) > 500:
            ext = "mp4" if mtype == "video" else "jpg"
            downloaded.append({"data": data, "fname": f"bsky_{rkey_from_url(url)}.{ext}",
                                "is_video": mtype=="video", "caption":""})

    if not downloaded:
        send_message(cid, "❌ دانلود رسانه ناموفق بود.", reply_markup=home_kb())
        return

    caption = f"🦋 @{author}\n\n{text}".strip() if text else f"🦋 @{author}"

    if len(downloaded) == 1:
        item = downloaded[0]
        mtype = "video" if item["is_video"] else "photo"
        inline_cap = caption[:BALE_CAPTION_LIMIT] if len(caption) <= BALE_CAPTION_LIMIT else ""
        smart_send(cid, item["data"], item["fname"], caption=inline_cap, media_type=mtype)
    else:
        downloaded[0]["caption"] = caption[:BALE_CAPTION_LIMIT] if len(caption) <= BALE_CAPTION_LIMIT else ""
        send_media_group(cid, downloaded)

    if len(caption) > BALE_CAPTION_LIMIT:
        send_message(cid, caption[:4096])

    send_message(cid, "✅ ارسال شد.", reply_markup=home_kb())
    clear_state(cid)


def rkey_from_url(url: str) -> str:
    m = re.search(r"/post/([A-Za-z0-9]+)", url)
    return m.group(1) if m else "post"


def handle_message(msg: dict):
    cid    = msg["chat"]["id"]
    text   = msg.get("text","")
    photos = msg.get("photo")
    init_user(cid)
    log.info("MSG cid=%d text=%r has_photo=%s", cid, text[:60], bool(photos))

    # Commands
    if text.startswith("/start"):
        clear_state(cid)
        send_message(cid,
            "👋 سلام! به *بله قربان* خوش آمدید.\n"
            "یکی از گزینه‌های زیر را انتخاب کنید:",
            parse_mode="Markdown", reply_markup=main_menu_kb())
        return
    if text.startswith("/help"):
        send_message(cid, HELP_TEXT, parse_mode="Markdown"); return
    if text.startswith("/cancel"):
        clear_state(cid)
        send_message(cid, "✅ لغو شد.", reply_markup=main_menu_kb()); return
    if text.startswith("/ocr"):
        reply = msg.get("reply_to_message")
        if reply and reply.get("photo"):
            do_ocr_photo(cid, reply["photo"], msg["message_id"]); return
        set_state(cid, mode="ocr")
        send_message(cid, "🖼 عکس حاوی متن ارسال کنید:", reply_markup=cancel_kb()); return

    # Photo
    if photos:
        st = get_state(cid)
        if st.get("mode") == "ocr" or not st.get("mode"):
            do_ocr_photo(cid, photos, msg["message_id"]); return

    # Document / file upload
    doc = msg.get("document")
    if doc:
        st = get_state(cid)
        if st.get("mode") == "virusscan":
            # Bale's getFile API only works for files ≤ ~20MB
            file_size = doc.get("file_size", 0)
            BALE_DL_LIMIT = 20 * 1024 * 1024   # 20MB
            VT_LIMIT      = 32 * 1024 * 1024   # 32MB

            if file_size > VT_LIMIT:
                send_message(cid,
                    f"⚠️ حجم فایل ({file_size//1024//1024}MB) بیشتر از حد مجاز VirusTotal (32MB) است.",
                    reply_markup=home_kb())
                clear_state(cid)
                return

            if file_size > BALE_DL_LIMIT:
                send_message(cid,
                    f"⚠️ حجم فایل ({file_size//1024//1024}MB) بیشتر از حد دانلود Bale (20MB) است.\n"
                    "فایل را مستقیم در VirusTotal آپلود کنید: https://www.virustotal.com",
                    reply_markup=home_kb())
                clear_state(cid)
                return

            file_url = get_file_url(doc["file_id"])
            if not file_url:
                send_message(cid, "❌ دریافت لینک فایل ناموفق بود.", reply_markup=home_kb())
                return
            file_data = download_bytes(file_url, VT_LIMIT)
            if not file_data:
                send_message(cid, "❌ دانلود فایل ناموفق بود.", reply_markup=home_kb())
                return
            fname = doc.get("file_name") or f"file_{doc['file_id'][:8]}"
            # Capture platform BEFORE spawning thread — _ctx is thread-local,
            # the new thread would otherwise have no context and send to the wrong API
            captured_platform = _platform()
            def _scan_thread(cid=cid, data=file_data, name=fname, plat=captured_platform):
                _set_platform_ctx(plat)
                do_virusscan(cid, data, name)
            threading.Thread(target=_scan_thread, daemon=True).start()
            return

    if not text: return

    st   = get_state(cid)
    mode = st.get("mode")

    dispatch = {
        "search":       lambda: do_search(cid, text),
        "scholar":      lambda: do_scholar(cid, text),
        "wiki":         lambda: do_wiki_search(cid, text),
        "wiki_article": lambda: do_wiki_article(cid, text, st.get("wiki_lang","fa")),
        "open":         lambda: do_open_url(cid, text),
        "yt_dl":        lambda: do_youtube_dl(cid, text),
        "yt_search":    lambda: do_youtube_search_cmd(cid, text),
        "music":        lambda: do_music(cid, text),
        "gh_search":    lambda: do_github_search(cid, text),
        "gh_zip":       lambda: do_github_zip(cid, text),
        "gh_release":   lambda: do_github_release(cid, text),
        "translate":    lambda: do_translate(cid, text, st.get("target_lang","en")),
        "virusscan":    lambda: do_virusscan_url(cid, text) if text.startswith("http") else
                                send_message(cid, "🛡 لطفاً یک فایل ارسال کنید یا یک آدرس اینترنتی بنویسید."),
        "currency":     lambda: do_currency(cid, text),
        "iplookup":     lambda: do_ip_lookup(cid, text),
        "shorten":      lambda: do_shorten(cid, text),
        "expand":       lambda: do_expand(cid, text),
        "paste":        lambda: do_paste(cid, text),
        "qr":           lambda: do_qr(cid, text),
        "ocr":          lambda: send_message(cid,"🖼 لطفاً عکس ارسال کنید.",reply_markup=cancel_kb()),
        "images_pick":  lambda: do_images(cid, st.get("last_query", text), st.get("img_source","bing")),
        "tg_read":      lambda: do_tg_read(cid, text, False),
        "tg_mtproto":   lambda: do_tg_read(cid, text, True),
        "tg_dl":        lambda: do_tg_dl_media(cid, text),
        "twitter_tl":           lambda: do_twitter_timeline(cid, text),
        "twitter_dl":           lambda: do_twitter_dl(cid, text),
        "twitter_dl_translate": lambda: do_twitter_dl_translate(cid, text),
        "ig_profile":           lambda: do_instagram_profile(cid, text),
        "ig_dl":                lambda: do_instagram_dl(cid, text),
        "ig_mosaic":            lambda: do_instagram_mosaic(cid, text),
        "ig_profile_pic":       lambda: do_instagram_profile_pic(cid, text),
        "ig_profile_shot":      lambda: do_instagram_profile_screenshot(cid, text),
        "tt_user":              lambda: do_tiktok_user(cid, text),
        "tt_dl":                lambda: do_tiktok_dl(cid, text),
        "bluesky":              lambda: do_bluesky_dl(cid, text),
        "yt_thumb":             lambda: do_youtube_thumbnail(cid, text),
        "yt_stats":             lambda: do_youtube_stats(cid, text),
        "rss":                  lambda: do_rss(cid, text),
        "zlib":                 lambda: do_zlib_search(cid, text, st.get("zlib_ext")),
        "apk":                  lambda: do_apk_search(cid, text),
    }

    if mode and mode in dispatch:
        dispatch[mode]()
        return

    if text.startswith("http"):
        # Smart URL detection
        if _is_youtube(text): do_youtube_dl(cid, text)
        elif "spotify.com" in text: do_music(cid, text)
        elif "soundcloud.com" in text or "on.soundcloud.com" in text:
            do_soundcloud_info(cid, text)
        elif "tiktok.com" in text: do_tiktok_dl(cid, text)
        elif "twitter.com" in text or "x.com" in text: do_twitter_dl(cid, text)
        elif "instagram.com" in text: do_instagram_dl(cid, text)
        elif "bsky.app" in text: do_bluesky_dl(cid, text)
        elif "t.me/" in text: do_tg_dl_media(cid, text)
        else: do_open_url(cid, text)
    else:
        do_search(cid, text)

# ═══════════════════════════════════════════════════════════════════════════════
# CALLBACK HANDLER
# ═══════════════════════════════════════════════════════════════════════════════
def handle_callback(cb: dict):
    msg = cb.get("message") or {}
    cid = (msg.get("chat") or {}).get("id") or 0
    if not cid:
        log.warning("handle_callback: no chat id in callback, skipping")
        return
    data = cb.get("data", "")
    answer_cb(cb["id"])
    init_user(cid)
    log.info("CB cid=%d data=%r", cid, data)

    st = get_state(cid)

    # ── Home / Cancel ─────────────────────────────────────────────────────
    if data in ("home","cancel"):
        clear_state(cid)
        send_message(cid, "🏠 منوی اصلی:", reply_markup=main_menu_kb()); return

    if data == "mode_social":
        send_message(cid, "📲 شبکه‌های اجتماعی:", reply_markup=social_menu_kb()); return

    if data == "help":
        send_message(cid, HELP_TEXT, parse_mode="Markdown"); return

    if data == "privacy":
        send_message(cid, PRIVACY_TEXT, parse_mode="Markdown",
                     reply_markup=home_kb()); return

    if data == "stats":
        s = user_stats.get(cid,{})
        txt = (f"📊 *آمار کاربری*\n\n"
               f"🗓 عضویت: {s.get('joined','—')}\n"
               f"📈 مجموع: {s.get('requests',0)}\n"
               f"🔎 جستجو: {s.get('searches',0)}\n"
               f"📥 دانلود: {s.get('downloads',0)}\n"
               f"🌐 ترجمه: {s.get('translations',0)}\n"
               f"🖼 OCR: {s.get('ocr',0)}")
        send_message(cid, txt, parse_mode="Markdown", reply_markup=home_kb()); return

    # ── Mode launchers ────────────────────────────────────────────────────
    mode_prompts = {
        "mode_search":    ("search",    "🔎 کلمه یا عبارت جستجو را بنویسید:"),
        "mode_open":      ("open",      "🌐 آدرس سایت را وارد کنید (https://…):"),
        "mode_scholar":   ("scholar",   "📚 عنوان یا کلمه‌کلیدی مقاله:"),
        "mode_wiki":      ("wiki",      "📖 موضوع ویکی‌پدیا:"),
        "mode_music":      ("music",          "🎵 نام آهنگ یا خواننده را بنویسید\nیا لینک Spotify / SoundCloud / YouTube را بفرستید:"),
        "mode_apk":        ("apk",            "📱 نام اپ یا شناسه پکیج را وارد کنید (مثال: org.telegram.messenger):"),
        "mode_iplookup":   ("iplookup",       "🌐 آدرس IP یا دامنه:"),
        "mode_rss":        ("rss",            "📰 آدرس فید RSS یا سایت خبری را وارد کنید:"),
        "mode_virusscan":  ("virusscan",      "🛡 فایل خود را ارسال کنید تا بررسی شود\nیا یک آدرس اینترنتی بفرستید تا اسکن شود:"),
        "mode_bluesky":    ("bluesky",        "🦋 لینک پست Bluesky را بفرستید:\nمثال: https://bsky.app/profile/user.bsky.social/post/123"),
        "ig_profile_pic":  ("ig_profile_pic", "📸 نام کاربری اینستاگرام را وارد کنید (بدون @):"),
        "ig_profile_shot": ("ig_profile_shot","📸 نام کاربری اینستاگرام را وارد کنید (بدون @):"),
        "ig_mosaic":       ("ig_mosaic",      "🔲 لینک پست اینستاگرام را برای ساخت موزاییک بفرستید:"),
    }
    if data in mode_prompts:
        mode, prompt = mode_prompts[data]
        set_state(cid, mode=mode)
        send_message(cid, prompt, reply_markup=cancel_kb()); return

    if data == "mode_translate":
        set_state(cid, mode="translate_lang")
        send_message(cid, "🌐 زبان مقصد را انتخاب کنید:", reply_markup=translate_kb()); return

    if data.startswith("trlang_"):
        lang = data.split("_",1)[1]
        set_state(cid, mode="translate", target_lang=lang)
        send_message(cid, f"✅ زبان انتخاب شد. متن را بفرستید:", reply_markup=cancel_kb()); return

    if data == "mode_ocr":
        set_state(cid, mode="ocr")
        send_message(cid, "🖼 عکس حاوی متن ارسال کنید:", reply_markup=cancel_kb()); return

    # ── YouTube ───────────────────────────────────────────────────────────
    if data == "mode_youtube":
        send_message(cid, "📺 یوتیوب:", reply_markup=youtube_action_kb()); return
    if data == "yt_video":
        set_state(cid, mode="yt_dl")
        send_message(cid, "📥 لینک ویدیو یوتیوب:", reply_markup=cancel_kb()); return
    if data == "yt_search":
        set_state(cid, mode="yt_search")
        send_message(cid, "🔍 کلمه جستجو:", reply_markup=cancel_kb()); return

    # YouTube result click → show info card with download button
    m = re.match(r"yt_res_(\w+)_(\d+)$", data)
    if m:
        key, idx = m.group(1), int(m.group(2))
        results = cache_get(key)
        if not results or idx >= len(results):
            send_message(cid, "❌ نتیجه منقضی شده. دوباره جستجو کنید."); return
        vid = results[idx]

        # Build info card text
        title    = vid.get("title", "")
        uploader = vid.get("uploader", "")
        duration = vid.get("duration", "")
        views    = vid.get("view_count", "")
        likes    = vid.get("like_count", "")
        desc     = vid.get("description", "")
        url      = vid.get("url", "")

        def _fmt_num(n):
            try:
                if not n or str(n).strip().upper() in ("NA", "NONE", "", "0"):
                    return ""
                n = int(str(n).replace(",", ""))
                if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
                if n >= 1_000: return f"{n/1_000:.1f}K"
                return str(n)
            except Exception: return ""

        lines = [f"📺 *{title}*"]
        if uploader: lines.append(f"🎬 {uploader}")
        if duration: lines.append(f"⏱ {duration}")
        views_fmt = _fmt_num(views)
        likes_fmt = _fmt_num(likes)
        if views_fmt: lines.append(f"👁 {views_fmt} بازدید")
        if likes_fmt: lines.append(f"👍 {likes_fmt} لایک")
        if desc:     lines.append(f"\n_{desc[:200]}_")

        info_text = "\n".join(lines)

        # Store selected video for download
        dl_key = "ytdl_" + hashlib.md5(url.encode()).hexdigest()[:8]
        cache_set(dl_key, [vid])

        kb = {"inline_keyboard": [
            [{"text": "📥 انتخاب کیفیت ویدیو", "callback_data": f"yt_do_dl_{dl_key}"},
             {"text": "🎵 دانلود صدا",          "callback_data": f"yt_do_audio_{dl_key}"}],
            [{"text": "🔙 برگشت",               "callback_data": f"yt_back_{key}"},
             {"text": "🏠 منوی اصلی",           "callback_data": "home"}],
        ]}

        # Send thumbnail + info card
        thumb_url = vid.get("thumbnail") or f"https://i.ytimg.com/vi/{vid.get('id','')}/hqdefault.jpg"
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
            send_message(cid, "❌ نتیجه منقضی شده. دوباره جستجو کنید."); return
        vid = vids[0]
        audio_only = (dl_type == "audio")
        url = vid.get("url", "")
        if audio_only:
            # Audio: skip quality picker, go straight to download
            url_key = store_url(url)
            info_key = "yti" + url_key
            cache_set(info_key, [{"title": vid.get("title",""), "subtitles": [],
                                   "formats": [], "thumbnail": vid.get("thumbnail","")}])
            _youtube_execute_download(cid, url_key, info_key,
                                      fmt_spec="", client="",
                                      audio_only=True, sub_code="")
        else:
            _youtube_quality_picker(cid, url, audio_only=False)
        return

    # ── YouTube quality selection ─────────────────────────────────────────────
    # yt_qual_{url_key}_{info_key}_{fmt_idx}
    # url_key = 10-char hex, info_key = "yti" + 10-char hex — no internal underscores
    m = re.match(r"yt_qual_([0-9a-f]+)_(yti[0-9a-f]+)_(\d+)$", data)
    if m:
        url_key, info_key, fmt_idx = m.group(1), m.group(2), int(m.group(3))
        info_list = cache_get(info_key)
        if not info_list:
            send_message(cid, "❌ اطلاعات منقضی. دوباره امتحان کنید."); return
        info = info_list[0]
        formats = info.get("formats", [])
        if fmt_idx >= len(formats):
            send_message(cid, "❌ کیفیت انتخابی موجود نیست."); return
        chosen = formats[fmt_idx]
        fmt_spec = chosen["fmt_spec"]
        client   = chosen.get("client", "tv_embedded")
        _youtube_sub_picker(cid, url_key, info_key, fmt_spec, client, audio_only=False)
        return

    # yt_qualA_{url_key}_{info_key}  (audio-only)
    m = re.match(r"yt_qualA_([0-9a-f]+)_(yti[0-9a-f]+)$", data)
    if m:
        url_key, info_key = m.group(1), m.group(2)
        _youtube_execute_download(cid, url_key, info_key,
                                  fmt_spec="", client="",
                                  audio_only=True, sub_code="")
        return

    # ── YouTube subtitle selection ────────────────────────────────────────────
    # yt_sub_{url_key}_{info_key}_{sub_code}_{audio_only}_{fmt_spec_encoded}_{client_encoded}
    # client is encoded: tv_embedded -> tv~embedded (underscores replaced with ~~ )
    m = re.match(r"yt_sub_([0-9a-f]+)_(yti[0-9a-f]+)_([^_]+)_([01])_([^_]*)_(.*)$", data)
    if m:
        url_key    = m.group(1)
        info_key   = m.group(2)
        sub_code   = m.group(3)   # "NONE" or language code
        audio_only = m.group(4) == "1"
        fmt_spec   = _decode_fmt(m.group(5))
        client     = m.group(6).replace("~~", "_")  # restore underscores in client
        _youtube_execute_download(cid, url_key, info_key,
                                  fmt_spec, client, audio_only,
                                  "" if sub_code == "NONE" else sub_code)
        return

    # ── YouTube subtitle page navigation ──────────────────────────────────────
    m = re.match(r"yt_subpg_([0-9a-f]+)_(yti[0-9a-f]+)_(\d+)_([01])_([^_]*)_(.*)$", data)
    if m:
        url_key    = m.group(1)
        info_key   = m.group(2)
        page       = int(m.group(3))
        audio_only = m.group(4) == "1"
        fmt_spec   = _decode_fmt(m.group(5))
        client     = m.group(6).replace("~~", "_")
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
        query = st.get("last_query","")
        if query: do_youtube_search_cmd(cid, query, page)
        return

    # ── GitHub ────────────────────────────────────────────────────────────
    if data == "mode_github":
        send_message(cid, "🐙 GitHub:", reply_markup=github_action_kb()); return
    if data == "gh_search":
        set_state(cid, mode="gh_search")
        send_message(cid, "🔍 نام مخزن یا کلمه کلیدی:", reply_markup=cancel_kb()); return
    if data == "gh_zip":
        set_state(cid, mode="gh_zip")
        send_message(cid, "📥 لینک مخزن GitHub:", reply_markup=cancel_kb()); return
    if data == "gh_release":
        set_state(cid, mode="gh_release")
        send_message(cid, "📦 نام کامل مخزن (مثال: microsoft/vscode):",
                     reply_markup=cancel_kb()); return

    # GitHub repo result click
    m = re.match(r"gh_res_(\w+)_(\d+)$", data)
    if m:
        key, idx = m.group(1), int(m.group(2))
        results = cache_get(key)
        if not results or idx >= len(results):
            send_message(cid, "❌ نتیجه منقضی شده."); return
        repo = results[idx]
        full = repo.get("full_name","")
        desc = repo.get("description") or ""
        txt = (f"🐙 *{full}*\n"
               f"⭐ {repo.get('stargazers_count',0)} | "
               f"🍴 {repo.get('forks_count',0)} | "
               f"📝 {repo.get('language','—')}\n\n"
               f"{desc[:300]}\n\n"
               f"🔗 {repo.get('html_url','')}")
        send_message(cid, txt, parse_mode="Markdown",
                     reply_markup=gh_repo_action_kb(full)); return

    m = re.match(r"gh_page_(\d+)$", data)
    if m:
        page = int(m.group(1))
        query = st.get("last_query","")
        if query: do_github_search(cid, query, page)
        return

    # GitHub repo actions
    m = re.match(r"ghact_zip_(.+)$", data)
    if m:
        repo = m.group(1).replace("__","/")
        do_github_zip(cid, f"https://github.com/{repo}"); return

    m = re.match(r"ghact_rel_(.+)$", data)
    if m:
        repo = m.group(1).replace("__","/")
        do_github_release(cid, repo); return

    m = re.match(r"ghact_info_(.+)$", data)
    if m:
        repo = m.group(1).replace("__","/")
        info = github_repo_info(repo)
        if info:
            txt = (f"📋 *{repo}*\n"
                   f"⭐ Stars: {info.get('stargazers_count',0)}\n"
                   f"🍴 Forks: {info.get('forks_count',0)}\n"
                   f"👀 Watchers: {info.get('watchers_count',0)}\n"
                   f"📝 Language: {info.get('language','—')}\n"
                   f"📅 Created: {info.get('created_at','')[:10]}\n"
                   f"🔄 Updated: {info.get('updated_at','')[:10]}\n"
                   f"📄 License: {(info.get('license') or {}).get('name','—')}\n"
                   f"🌐 {info.get('homepage') or info.get('html_url','')}")
            send_message(cid, txt, parse_mode="Markdown", reply_markup=home_kb())
        else:
            send_message(cid, "❌ اطلاعاتی یافت نشد.", reply_markup=home_kb())
        return

    # GitHub release asset download
    m = re.match(r"ghrel_dl_(.+)_(\d+)$", data)
    if m:
        repo = m.group(1).replace("__","/")
        idx  = int(m.group(2))
        akey = f"ghrel_{m.group(1)}"
        assets = cache_get(akey)
        if not assets or idx >= len(assets):
            send_message(cid, "❌ نتیجه منقضی."); return
        asset = assets[idx]
        url = asset.get("browser_download_url","")
        size_mb = asset.get("size",0)/1024/1024
        send_message(cid, f"⏳ دانلود: {asset['name']} ({size_mb:.1f}MB)…")
        chat_action(cid, "upload_document")
        d = download_bytes(url, 500 * 1024 * 1024)  # allow up to 500MB download
        if d:
            smart_send(cid, d, asset["name"], caption=f"📦 {repo}")
            send_message(cid, "✅ ارسال شد.", reply_markup=home_kb())
        else:
            send_message(cid, "❌ دانلود ناموفق.", reply_markup=home_kb())
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
        send_message(cid, "🖼 کلمه کلیدی عکس مورد نظر را بنویسید:",
                     reply_markup=cancel_kb()); return

    img_src_map = {"img_src_bing":"bing","img_src_pinterest":"pinterest",
                   "img_src_pexels":"pexels","img_src_wiki":"wiki"}
    if data in img_src_map:
        src = img_src_map[data]
        query = st.get("last_query","")
        if not query:
            send_message(cid, "❌ ابتدا کلمه کلیدی وارد کنید.", reply_markup=cancel_kb()); return
        set_state(cid, mode=f"img_{src}", last_query=query, img_source=src, page=0)
        do_images(cid, query, src, 0); return

    # "دانلود بیشتر" button
    m = re.match(r"img_more_(\w+)_(\w+)_(\d+)$", data)
    if m:
        source, key, page = m.group(1), m.group(2), int(m.group(3))
        query = st.get("last_query","")
        do_images(cid, query, source, page); return

    # ── APK callbacks ─────────────────────────────────────────────────────
    if data == "mode_apk":
        set_state(cid, mode="apk")
        send_message(cid,
            "📱 *Google Play APK Downloader*\n\n"
            "Enter an app name or package ID:\n"
            "• `telegram` ← search by name\n"
            "• `org.telegram.messenger` ← direct package ID",
            parse_mode="Markdown", reply_markup=cancel_kb())
        return

    # apk_item_{cache_key}_{idx} — user clicked an app in results
    m = re.match(r"apk_item_(\w+)_(\d+)$", data)
    if m:
        key, idx = m.group(1), int(m.group(2))
        results = cache_get(key)
        if not results or idx >= len(results):
            send_message(cid, "❌ نتیجه منقضی شده. دوباره جستجو کنید."); return
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
        app_id  = safe_id.replace("_", ".")
        do_apk_download(cid, app_id)
        return

    # apk_gplay_{safe_app_id} — open Google Play page (screenshot)
    m = re.match(r"apk_gplay_(.+)$", data)
    if m:
        safe_id = m.group(1)
        app_id  = safe_id.replace("_", ".")
        gplay_url = f"https://play.google.com/store/apps/details?id={app_id}"
        do_open_url(cid, gplay_url)
        return

    # apk_back — go back to last APK search results
    if data == "apk_back":
        query   = st.get("last_query","")
        key     = st.get("cache_key","")
        results = cache_get(key) if key else []
        if results:
            kb = apk_results_kb(results, key)
            send_message(cid, f"📱 نتایج برای: _{query}_",
                         parse_mode="Markdown", reply_markup=kb)
        else:
            send_message(cid, "❌ جستجو منقضی شده. دوباره جستجو کنید.", reply_markup=main_menu_kb())
        return

    # ── Z-Library callbacks ───────────────────────────────────────────────
    if data == "mode_zlib":
        send_message(cid, "📚 Z-Library:", reply_markup=zlib_kb()); return

    if data in ("zlib_search", "zlib_search_art"):
        prompt = ("🔍 عنوان کتاب یا نام نویسنده را بنویسید:"
                  if data == "zlib_search" else
                  "🔍 عنوان مقاله یا نام نویسنده را بنویسید:")
        set_state(cid, mode="zlib", zlib_ext=None)
        send_message(cid, prompt, reply_markup=cancel_kb()); return

    if data == "zlib_filter_pdf":
        set_state(cid, mode="zlib", zlib_ext=["PDF"])
        send_message(cid, "🔍 عنوان کتاب (فقط PDF):", reply_markup=cancel_kb()); return

    if data == "zlib_filter_epub":
        set_state(cid, mode="zlib", zlib_ext=["EPUB"])
        send_message(cid, "🔍 عنوان کتاب (فقط EPUB):", reply_markup=cancel_kb()); return

    if data == "zlib_filter_other":
        set_state(cid, mode="zlib", zlib_ext=["FB2", "MOBI", "AZW3"])
        send_message(cid, "🔍 عنوان کتاب (FB2/MOBI/AZW3):",
                     reply_markup=cancel_kb()); return

    if data == "zlib_back":
        query   = st.get("last_query", "")
        key     = st.get("cache_key", "")
        results = cache_get(key) if key else []
        if results:
            kb = zlib_results_kb(results, key)
            send_message(cid, f"📚 نتایج: _{query}_",
                         parse_mode="Markdown", reply_markup=kb)
        else:
            send_message(cid, "🏠", reply_markup=main_menu_kb())
        return

    # zlib_item_{cache_key}_{idx}  — کلیک روی کتاب
    m = re.match(r"zlib_item_(\w+)_(\d+)$", data)
    if m:
        key, idx = m.group(1), int(m.group(2))
        results = cache_get(key)
        if not results or idx >= len(results):
            send_message(cid, "❌ نتیجه منقضی."); return
        do_zlib_show_book(cid, results[idx]); return

    # zlib_dl_{url_key}  — دانلود کتاب
    m = re.match(r"zlib_dl_(\w+)$", data)
    if m:
        url_key  = m.group(1)
        book_url = (get_url(url_key) or "")
        if not book_url:
            send_message(cid, "❌ لینک منقضی."); return
        do_zlib_download(cid, book_url); return

    # ── RSS callbacks ─────────────────────────────────────────────────────
    if data == "mode_rss" or data.startswith("rss_"):
        if data == "mode_rss":
            set_state(cid, mode="rss")
            send_message(cid,
                "📰 آدرس فید RSS یا سایت خبری را وارد کنید:\n\n"
                "مثال‌ها:\n"
                "• `https://www.bbc.com/persian/index.xml`\n"
                "• `https://www.isna.ir` ← کشف خودکار",
                parse_mode="Markdown", reply_markup=cancel_kb())
            return

        # rss_url_{url_key}  — user picked a discovered feed
        m = re.match(r"rss_url_(\w+)$", data)
        if m:
            url_key = m.group(1)
            url = get_url(url_key)
            if not url:
                send_message(cid, "❌ لینک منقضی."); return
            send_message(cid, "⏳ دریافت فید…")
            items = rss_fetch(url, 15)
            if not items:
                send_message(cid, "❌ فید خالی یا نامعتبر.", reply_markup=home_kb()); return
            key = make_cache_key("rss", url, 0)
            cache_set(key, items)
            set_state(cid, mode="rss", last_query=url, cache_key=key)
            _display_rss_list(cid, items, key)
            return

        # rss_item_{cache_key}_{idx}
        m = re.match(r"rss_item_(\w+)_(\d+)$", data)
        if m:
            key, idx = m.group(1), int(m.group(2))
            items = cache_get(key)
            if not items or idx >= len(items):
                send_message(cid, "❌ خبر منقضی."); return
            _show_rss_item(cid, items[idx])
            return

    # ── Site view buttons ─────────────────────────────────────────────────
    m = re.match(r"site_(text|html|zip|pdf)_(\w+)$", data)
    if m:
        action, url_key = m.group(1), m.group(2)
        url = get_url(url_key)
        if not url:
            send_message(cid, "❌ لینک منقضی شده.", reply_markup=home_kb()); return
        chat_action(cid, "upload_document")
        if action == "text":
            txt = page_to_text(url)
            send_message(cid, f"📝 متن صفحه:\n\n{txt[:3500]}", reply_markup=home_kb())
            if len(txt) > 3500:
                send_document(cid, txt.encode("utf-8"), "page_text.txt")
        elif action == "html":
            html = fetch_page(url)
            if html: send_document(cid, html, "page.html", caption=f"🌐 {url[:60]}")
            else: send_message(cid, "❌ خطا در دریافت HTML.", reply_markup=home_kb())
        elif action == "zip":
            send_message(cid, "⏳ در حال ساخت ZIP…")
            zdata = page_to_zip(url)
            if zdata:
                domain = urllib.parse.urlparse(url).netloc.replace(".","_")
                send_document(cid, zdata, f"{domain}_offline.zip")
            else: send_message(cid, "❌ خطا در ZIP.", reply_markup=home_kb())
        elif action == "pdf":
            send_message(cid, "⏳ در حال تولید PDF…")
            pdf = page_to_pdf(url)
            if pdf:
                ext = "pdf" if pdf[:4]==b"%PDF" else "html"
                send_document(cid, pdf, f"page.{ext}", caption=f"📑 {url[:60]}")
            else: send_message(cid, "❌ خطا در PDF.", reply_markup=home_kb())
        send_message(cid, "✅", reply_markup=site_view_kb(url_key)); return

    # ── Search result click (web, scholar) ────────────────────────────────
    m = re.match(r"res_(\w+)_(\d+)$", data)
    if m:
        key, idx = m.group(1), int(m.group(2))
        results = cache_get(key)
        if not results or idx >= len(results):
            send_message(cid, "❌ نتیجه منقضی."); return
        r = results[idx]
        # Show detail with options
        link = r.get("link") or r.get("url","")
        snippet = r.get("snippet") or r.get("meta","")
        txt = (f"🔗 *{r.get('title','')}*\n\n"
               f"{snippet[:400]}\n\n"
               f"[باز کردن لینک]({link})")
        url_key = store_url(link)
        kb = {"inline_keyboard": [
            [{"text": "🌐 مشاهده سایت",  "callback_data": f"site_ss_{url_key}"},
             {"text": "📝 متن صفحه",     "callback_data": f"site_text_{url_key}"}],
            [{"text": "🌐 HTML",          "callback_data": f"site_html_{url_key}"},
             {"text": "🔙 برگشت به نتایج","callback_data": "results_back"}],
        ]}
        send_message(cid, txt, parse_mode="Markdown", reply_markup=kb); return

    m = re.match(r"site_ss_(\w+)$", data)
    if m:
        url_key = m.group(1)
        url = get_url(url_key)
        if not url: send_message(cid, "❌ لینک منقضی."); return
        send_message(cid, f"⏳ گرفتن اسکرین‌شات…")
        ss = screenshot_page(url)
        if ss: send_photo(cid, ss, caption=url[:60], reply_markup=site_view_kb(url_key))
        else: send_message(cid, "❌ اسکرین‌شات ممکن نبود.", reply_markup=site_view_kb(url_key))
        return

    if data == "results_back":
        query = st.get("last_query","")
        page  = st.get("page", 0)
        mode  = st.get("mode","")
        if "scholar" in mode: do_scholar(cid, query, page)
        elif query: do_search(cid, query, page)
        else: send_message(cid, "🏠", reply_markup=main_menu_kb())
        return

    # ── Pagination (web search, scholar) ──────────────────────────────────
    pm = re.match(r"page_(ws|sc)_(\d+)$", data)
    if pm:
        kind, page = pm.group(1), int(pm.group(2))
        query = st.get("last_query","")
        if not query: send_message(cid, "❌ جستجوی قبلی یافت نشد."); return
        if kind == "ws": do_search(cid, query, page)
        elif kind == "sc": do_scholar(cid, query, page)
        return

    # ── Wiki article click ────────────────────────────────────────────────
    m = re.match(r"wiki_art_(\w+)_(\d+)_(\w+)$", data)
    if m:
        key, idx, lang = m.group(1), int(m.group(2)), m.group(3)
        results = cache_get(key)
        if not results or idx >= len(results):
            send_message(cid, "❌ نتیجه منقضی."); return
        article = results[idx]
        do_wiki_article(cid, article["title"], lang); return

    # ── Telegram channel ──────────────────────────────────────────────────
    if data == "mode_tg_channel":
        set_state(cid, mode="tg_channel_menu")
        send_message(cid, "✈️ کانال تلگرام:", reply_markup=tg_channel_kb()); return

    if data == "tg_read_web":
        set_state(cid, mode="tg_read")
        send_message(cid,
            "✈️ نام کانال عمومی را وارد کنید:\n_(بدون @ — مثال: `durov`)_",
            parse_mode="Markdown", reply_markup=cancel_kb()); return

    if data == "tg_read_mtproto":
        if not TG_API_ID or not TG_API_HASH:
            log.error("tg_read_mtproto: TG_API_ID/TG_API_HASH not configured")
            send_message(cid, "❌ این قابلیت در حال حاضر در دسترس نیست.",
                         reply_markup=home_kb()); return
        set_state(cid, mode="tg_mtproto")
        send_message(cid,
            "✈️ نام کانال را وارد کنید (عمومی یا خصوصی):",
            reply_markup=cancel_kb()); return

    if data == "tg_dl_media":
        set_state(cid, mode="tg_dl")
        send_message(cid,
            "✈️ لینک پیام تلگرام را وارد کنید:\n"
            "_(مثال: `https://t.me/channel/123`)_",
            parse_mode="Markdown", reply_markup=cancel_kb()); return

    # ── Twitter/X ─────────────────────────────────────────────────────────
    if data == "mode_twitter":
        send_message(cid, "🐦 توییتر / X:", reply_markup=twitter_kb()); return

    if data == "tw_timeline":
        set_state(cid, mode="twitter_tl")
        send_message(cid,
            "🐦 نام کاربری توییتر را وارد کنید:\n_(مثال: `elonmusk`)_",
            parse_mode="Markdown", reply_markup=cancel_kb()); return

    if data == "tw_dl":
        set_state(cid, mode="twitter_dl")
        send_message(cid,
            "🐦 لینک توییت را وارد کنید:\n"
            "_(مثال: `https://twitter.com/user/status/123`)_",
            parse_mode="Markdown", reply_markup=cancel_kb()); return

    if data == "tw_dl_translate":
        set_state(cid, mode="twitter_dl_translate")
        send_message(cid,
            "🐦🇮🇷 لینک توییت را وارد کنید — متن به فارسی ترجمه می‌شود:",
            reply_markup=cancel_kb()); return

    # ── Instagram ─────────────────────────────────────────────────────────
    if data == "mode_instagram":
        send_message(cid, "📸 اینستاگرام:", reply_markup=instagram_kb()); return

    if data == "ig_profile":
        set_state(cid, mode="ig_profile")
        send_message(cid,
            "📸 نام کاربری اینستاگرام را وارد کنید:\n_(مثال: `natgeo`)_",
            parse_mode="Markdown", reply_markup=cancel_kb()); return

    if data == "ig_dl":
        set_state(cid, mode="ig_dl")
        send_message(cid,
            "📸 لینک پست یا ریل را وارد کنید:\n"
            "_(مثال: `https://www.instagram.com/p/ABC123/`)_",
            parse_mode="Markdown", reply_markup=cancel_kb()); return

    if data == "ig_profile_pic":
        set_state(cid, mode="ig_profile_pic")
        send_message(cid, "📸 نام کاربری اینستاگرام را وارد کنید (بدون @):",
                     reply_markup=cancel_kb()); return

    if data == "ig_profile_shot":
        set_state(cid, mode="ig_profile_shot")
        send_message(cid, "📸 نام کاربری اینستاگرام را وارد کنید (بدون @):",
                     reply_markup=cancel_kb()); return

    if data == "ig_mosaic":
        set_state(cid, mode="ig_mosaic")
        send_message(cid,
            "🔲 لینک پست کاروسل اینستاگرام را وارد کنید:",
            reply_markup=cancel_kb()); return

    # ── TikTok ────────────────────────────────────────────────────────────
    if data == "mode_tiktok":
        send_message(cid, "🎵 TikTok:", reply_markup=tiktok_kb()); return

    if data == "tt_user":
        set_state(cid, mode="tt_user")
        send_message(cid,
            "🎵 نام کاربری TikTok را وارد کنید:\n_(مثال: `khaby.lame`)_",
            parse_mode="Markdown", reply_markup=cancel_kb()); return

    if data == "tt_dl":
        set_state(cid, mode="tt_dl")
        send_message(cid,
            "🎵 لینک ویدیو TikTok را وارد کنید:",
            reply_markup=cancel_kb()); return

    # ── Bluesky ───────────────────────────────────────────────────────────
    if data == "mode_bluesky":
        set_state(cid, mode="bluesky")
        send_message(cid,
            "🦋 لینک پست Bluesky را وارد کنید:\n"
            "_(مثال: `https://bsky.app/profile/user.bsky.social/post/abc123`)_",
            parse_mode="Markdown", reply_markup=cancel_kb()); return

    # ── YouTube extras ────────────────────────────────────────────────────
    if data == "yt_thumb":
        set_state(cid, mode="yt_thumb")
        send_message(cid,
            "▶️ لینک ویدیوی یوتیوب را برای دریافت کاور وارد کنید:",
            reply_markup=cancel_kb()); return

    if data == "yt_stats":
        set_state(cid, mode="yt_stats")
        send_message(cid,
            "▶️ لینک ویدیوی یوتیوب را برای مشاهده آمار وارد کنید:",
            reply_markup=cancel_kb()); return

    # ── SoundCloud download from info page ────────────────────────────────
    m_sc = re.match(r"sc_dl_(\w+)$", data)
    if m_sc:
        url_key = m_sc.group(1)
        url = get_url(url_key)
        if not url:
            send_message(cid, "❌ لینک منقضی شده."); return
        threading.Thread(target=do_music, args=(cid, url), daemon=True).start()
        return

    # ── Social result item click: soc_{platform}_{cache_key}_{idx} ───────
    m = re.match(r"soc_(tg|tw|ig|tt)_(\w+)_(\d+)$", data)
    if m:
        platform, key, idx = m.group(1), m.group(2), int(m.group(3))
        results = cache_get(key)
        if not results or idx >= len(results):
            send_message(cid, "❌ نتیجه منقضی."); return
        item = results[idx]
        if platform == "tg":
            do_tg_show_message(cid, item)
        elif platform == "tw":
            do_twitter_show(cid, item)
        elif platform == "ig":
            url = item.get("url", "")
            text = item.get("text", "")       # full caption
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
            send_message(cid,
                         "📥 دانلود ویدیو?" if is_vid else "📥 دانلود نسخه کامل؟",
                         reply_markup=kb)
        elif platform == "tt":
            url       = item.get("url", "")
            title     = item.get("title", "")
            dur       = item.get("duration", "")
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

    # ── Social media download button: soc_dl_{platform}_{url_key} ────────
    m = re.match(r"soc_dl_(tg|tw|ig|tt)_(\w+)$", data)
    if m:
        platform, url_key = m.group(1), m.group(2)
        url = (get_url(url_key) or "")
        if not url:
            send_message(cid, "❌ لینک منقضی."); return
        if platform == "tg":
            do_tg_dl_media(cid, url)
        elif platform == "tw":
            do_twitter_dl(cid, url)
        elif platform == "ig":
            do_instagram_dl(cid, url)
        elif platform == "tt":
            do_tiktok_dl(cid, url)
        return

    # ── Music track click: mu_dl_{cache_key}_{idx} ───────────────────────
    m = re.match(r"mu_dl_(\w+)_(\d+)$", data)
    if m:
        key, idx = m.group(1), int(m.group(2))
        results = cache_get(key)
        if not results or idx >= len(results):
            send_message(cid, "❌ نتیجه منقضی شده. دوباره جستجو کنید."); return
        track = results[idx]
        url    = track.get("url", "")
        title  = track.get("title", "")
        artist = track.get("uploader", "")
        thumb  = track.get("thumbnail", "")
        source = track.get("source", "youtube")

        # Send thumbnail first
        if thumb:
            try:
                tb = download_bytes(thumb, MAX_IMAGE_SIZE)
                if tb:
                    dur = track.get("duration", "")
                    cap = title
                    if artist: cap += f"\n🎤 {artist}"
                    if dur:    cap += f"\n⏱ {dur}"
                    src_label = {"youtube":"▶️ YouTube","ytmusic":"🎵 YouTube Music",
                                 "soundcloud":"☁️ SoundCloud","jiosaavn":"🎶 JioSaavn",
                                 "deezer":"🎧 Deezer"}.get(source, "")
                    if src_label: cap += f"\n{src_label}"
                    send_photo(cid, tb, caption=cap[:1024])
            except Exception as e:
                log.warning("music thumb: %s", e)

        _do_audio_download(cid, url, source=source, title=title,
                           artist=artist, track_item=track)
        return

    # ── Images query entry ────────────────────────────────────────────────
    log.warning("Unhandled callback: %r", data)

def handle_message_images_query(cid: int, text: str, st: dict):
    """Called from handle_message when mode==images_query."""
    set_state(cid, mode="images_source", last_query=text)
    send_message(cid, f"🖼 منبع عکس برای _{text}_ را انتخاب کنید:",
                 parse_mode="Markdown", reply_markup=image_source_kb())

# Patch handle_message dispatch for images_query
_orig_dispatch_key = "images_query"

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN DISPATCH
# ═══════════════════════════════════════════════════════════════════════════════
# ── Thread pools ─────────────────────────────────────────────────────────────
# One shared executor for update handlers (downloads, searches, etc.)
# Each platform has its own polling thread; handler threads are shared.
from concurrent.futures import ThreadPoolExecutor
_update_executor = ThreadPoolExecutor(max_workers=40, thread_name_prefix="upd")


def _handle_update_safe(platform: str, update: dict):
    """Set platform context then dispatch — runs inside the shared thread pool."""
    _set_platform_ctx(platform)   # make api/send_* use the right endpoint
    try:
        handle_update(update)
    except Exception as e:
        log.error("[%s] handle_update: %s", platform, e, exc_info=True)
        try:
            cid = None
            if "message" in update:
                cid = update["message"]["chat"]["id"]
            elif "callback_query" in update:
                cid = update["callback_query"]["message"]["chat"]["id"]
            if cid:
                _notify_bale_error(cid)
        except Exception:
            pass


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
    sess     = _session()          # persistent session for this platform
    base_url = _base_url()
    log.info("[%s] Polling started — token …%s  url=%s", platform, token[-6:], base_url)
    offset   = 0
    failures = 0
    while True:
        try:
            r = sess.post(
                f"{base_url}/getUpdates",
                json={"offset": offset, "timeout": 25, "limit": 100},
                timeout=35,   # slightly over long-poll timeout so TCP doesn't cut early
            )
            resp = _safe_json(r)
            if not resp.get("ok"):
                failures += 1
                log.warning("[%s] getUpdates not ok (#%d): %s", platform, failures, resp)
                time.sleep(min(5 * failures, 60))
                continue
            failures = 0
            for update in resp.get("result", []):
                offset = update["update_id"] + 1
                _update_executor.submit(_handle_update_safe, platform, update)
        except requests.exceptions.ReadTimeout:
            # Normal for long-poll — server held the connection open but no updates
            pass
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            failures += 1
            log.warning("[%s] getUpdates connection error (#%d): %s", platform, failures, e)
            time.sleep(min(3 * failures, 30))
        except Exception as e:
            failures += 1
            log.error("[%s] Polling error (#%d): %s", platform, failures, e, exc_info=True)
            time.sleep(min(5 * failures, 60))


def run():
    threads = []

    if BALE_TOKEN:
        log.info("Bale platform enabled — token …%s", BALE_TOKEN[-6:])
        t = _tl_threading.Thread(
            target=_poll_platform, args=("bale", BALE_TOKEN),
            name="poll-bale", daemon=True)
        t.start()
        threads.append(t)
    else:
        log.warning("BALE_TOKEN not set — Bale platform disabled")

    if TELEGRAM_TOKEN:
        log.info("Telegram platform enabled — token …%s", TELEGRAM_TOKEN[-6:])
        t = _tl_threading.Thread(
            target=_poll_platform, args=("telegram", TELEGRAM_TOKEN),
            name="poll-telegram", daemon=True)
        t.start()
        threads.append(t)
    else:
        log.warning("TELEGRAM_TOKEN not set — Telegram platform disabled")

    if not threads:
        log.critical("No tokens set! Set BALE_TOKEN and/or TELEGRAM_TOKEN in .env")
        return

    log.info("Bot running on %d platform(s). Press Ctrl+C to stop.",  len(threads))
    try:
        while True:
            time.sleep(1)
            # Restart any dead polling thread
            for t in threads:
                if not t.is_alive():
                    log.error("Polling thread %s died — restarting", t.name)
                    platform = "bale" if "bale" in t.name else "telegram"
                    token    = BALE_TOKEN if platform == "bale" else TELEGRAM_TOKEN
                    new_t = _tl_threading.Thread(
                        target=_poll_platform, args=(platform, token),
                        name=t.name, daemon=True)
                    new_t.start()
                    threads[threads.index(t)] = new_t
    except KeyboardInterrupt:
        log.info("Bot stopped.")
        _update_executor.shutdown(wait=False)


if __name__ == "__main__":
    run()