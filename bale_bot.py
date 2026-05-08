#!/usr/bin/env python3
"""
بله قربان — Bale Bot  (v1.0)
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
TOKEN          = os.getenv("BALE_TOKEN", "YOUR_BOT_TOKEN_HERE")
BASE_URL       = f"https://tapi.bale.ai/bot{TOKEN}"
MAX_FILE_SIZE  = 20 * 1024 * 1024
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
INSTAGRAM_USER = os.getenv("INSTAGRAM_USER", "")
INSTAGRAM_PASS = os.getenv("INSTAGRAM_PASS", "")

# Z-Library: ایمیل و رمز حساب از z-library.sk/login
# ثبت‌نام رایگان در: https://z-library.sk/login
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
def _make_session() -> requests.Session:
    s = requests.Session()
    r = Retry(total=3, backoff_factor=0.4,
              status_forcelist=[429, 500, 502, 503, 504],
              allowed_methods=["GET", "POST", "HEAD"])
    s.mount("https://", HTTPAdapter(max_retries=r))
    s.mount("http://",  HTTPAdapter(max_retries=r))
    return s

WEB = _make_session()

# ═══════════════════════════════════════════════════════════════════════════════
# STATE
# ═══════════════════════════════════════════════════════════════════════════════
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

def _mime(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return MIME_MAP.get(ext, "application/octet-stream")

def _should_wrap(filename: str) -> bool:
    """Non-exempt extensions should be ZIP-wrapped so Bale accepts them."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext not in EXEMPT_EXT

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

def api(method: str, **kwargs) -> dict:
    try:
        r = requests.post(f"{BASE_URL}/{method}", json=kwargs, timeout=30)
        return _safe_json(r)
    except Exception as e:
        log.error("api %s: %s", method, e)
        return {"ok": False}

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
               data_bytes: bytes, extra_data: dict) -> bool:
    """Low-level multipart file POST to Bale API."""
    files = {field: (filename, io.BytesIO(data_bytes), _mime(filename))}
    try:
        r = requests.post(f"{BASE_URL}/{endpoint}", data=extra_data,
                          files=files, timeout=180)
        resp = _safe_json(r)
        ok = resp.get("ok", False)
        if not ok:
            log.error("%s failed: %s  file=%s  size=%dKB",
                      endpoint, resp, filename, len(data_bytes)//1024)
        return ok
    except Exception as e:
        log.error("%s exception: %s  file=%s", endpoint, e, filename)
        return False

def _send_one_chunk(chat_id, data_bytes: bytes, filename: str,
                    caption="", media_type="document") -> bool:
    """Send a single chunk using the correct Bale endpoint."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    extra = {"chat_id": str(chat_id), "caption": caption[:1024]}

    if media_type == "video" or ext in {"mp4","mkv","avi","mov","webm","m4v"}:
        extra["supports_streaming"] = "true"
        return _post_file("sendVideo", "video", filename, data_bytes, extra)
    elif media_type == "audio" or ext in {"mp3","ogg","wav","flac","m4a","aac","opus"}:
        return _post_file("sendAudio", "audio", filename, data_bytes, extra)
    elif media_type == "photo" or ext in {"jpg","jpeg","png","gif","webp"}:
        return _post_file("sendPhoto", "photo", filename, data_bytes, extra)
    else:
        return _post_file("sendDocument", "document", filename, data_bytes, extra)

def smart_send(chat_id, data: bytes, filename: str,
               caption="", media_type="auto") -> bool:
    """
    Smart file sender that mirrors index.js logic:
    1. Wrap unsupported extensions in ZIP
    2. If > CHUNK_SIZE, split into .part1ofN chunks
    3. Send each chunk with correct endpoint
    Returns True if all chunks sent successfully.
    """
    if not data:
        log.error("smart_send: empty data for %s", filename)
        return False

    total_mb = len(data) / 1024 / 1024
    log.info("smart_send: %s  %.1fMB  type=%s", filename, total_mb, media_type)

    # Step 1: ZIP wrap if needed (skip for chunks and exempt types)
    import re as _re
    is_chunk = bool(_re.search(r'\.part\d+of\d+\.', filename))
    if not is_chunk and _should_wrap(filename):
        log.info("smart_send: wrapping %s in ZIP", filename)
        send_message(chat_id, f"📦 در حال زیپ کردن `{filename}`…", parse_mode="Markdown")
        data, filename = _wrap_zip(data, filename)
        log.info("smart_send: zipped → %s  %.1fMB", filename, len(data)/1024/1024)

    total_size = len(data)

    # Step 2: Send in one shot if small enough
    if total_size <= CHUNK_SIZE:
        return _send_one_chunk(chat_id, data, filename, caption, media_type)

    # Step 3: Chunk it
    total_chunks = (total_size + CHUNK_SIZE - 1) // CHUNK_SIZE
    base = filename.rsplit(".", 1)[0] if "." in filename else filename
    ext  = ("." + filename.rsplit(".", 1)[1]) if "." in filename else ""
    log.info("smart_send: splitting into %d chunks", total_chunks)
    send_message(chat_id,
                 f"📤 فایل بزرگ است — ارسال در *{total_chunks} بخش*…",
                 parse_mode="Markdown")
    all_ok = True
    for i in range(total_chunks):
        start = i * CHUNK_SIZE
        end   = min(start + CHUNK_SIZE, total_size)
        chunk = data[start:end]
        chunk_name = f"{base}.part{i+1}of{total_chunks}{ext}"
        chunk_mb = len(chunk) / 1024 / 1024
        send_message(chat_id,
                     f"📤 ارسال بخش {i+1} از {total_chunks} ({chunk_mb:.1f}MB)…")
        ok = _send_one_chunk(chat_id, chunk, chunk_name, caption="", media_type="document")
        if not ok:
            send_message(chat_id, f"❌ ارسال بخش {i+1} ناموفق بود.")
            all_ok = False
            break
    if all_ok:
        send_message(chat_id,
                     f"✅ همه {total_chunks} بخش ارسال شدند!\n\n"
                     f"برای ترکیب:\n"
                     f"`cat {base}.part*of{total_chunks}{ext} > {filename}`",
                     parse_mode="Markdown")
    return all_ok

# Convenience wrappers (keep old call sites working)
def send_document(chat_id, file_bytes: bytes, filename: str,
                  caption="", reply_to=None) -> bool:
    return smart_send(chat_id, file_bytes, filename, caption, media_type="document")

def send_video(chat_id, video_bytes: bytes, filename: str, caption="") -> bool:
    return smart_send(chat_id, video_bytes, filename, caption, media_type="video")

def send_photo(chat_id, img_bytes: bytes, caption="", reply_markup=None) -> bool:
    """Photos don't chunk — just post directly."""
    if not img_bytes:
        return False
    ext = "jpg"
    files = {"photo": (f"image.{ext}", io.BytesIO(img_bytes), "image/jpeg")}
    data  = {"chat_id": str(chat_id), "caption": caption[:1024]}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    try:
        r = requests.post(f"{BASE_URL}/sendPhoto", data=data, files=files, timeout=60)
        resp = _safe_json(r)
        ok = resp.get("ok", False)
        if not ok:
            log.error("sendPhoto failed: %s", resp)
        return ok
    except Exception as e:
        log.error("sendPhoto exception: %s", e)
        return False

def send_audio(chat_id, audio_bytes: bytes, filename: str, caption="") -> bool:
    return smart_send(chat_id, audio_bytes, filename, caption, media_type="audio")

def chat_action(chat_id, action="typing"):
    api("sendChatAction", chat_id=chat_id, action=action)

def get_file_url(file_id: str) -> Optional[str]:
    resp = api("getFile", file_id=file_id)
    if resp.get("ok"):
        return f"https://tapi.bale.ai/file/bot{TOKEN}/{resp['result']['file_path']}"
    return None


def download_bytes(url: str, max_bytes=MAX_FILE_SIZE) -> Optional[bytes]:
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
    result_cache[key] = data
    # Prune if too large
    if len(result_cache) > 500:
        oldest = list(result_cache.keys())[:100]
        for k in oldest:
            result_cache.pop(k, None)

def cache_get(key: str) -> list:
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
         {"text": "🎵 موسیقی / دانلود",  "callback_data": "mode_music"}],
        [{"text": "🟢 Spotify",           "callback_data": "mode_spotify"},
         {"text": "☁️ SoundCloud",        "callback_data": "mode_soundcloud"}],
        [{"text": "🖼 دانلود عکس",        "callback_data": "mode_images"},
         {"text": "🐙 GitHub",            "callback_data": "mode_github"}],
        [{"text": "✈️ کانال تلگرام",      "callback_data": "mode_tg_channel"},
         {"text": "🐦 توییتر / X",        "callback_data": "mode_twitter"}],
        [{"text": "📸 اینستاگرام",        "callback_data": "mode_instagram"},
         {"text": "🎵 تیک‌تاک",           "callback_data": "mode_tiktok"}],
        [{"text": "📰 اخبار RSS",         "callback_data": "mode_rss"},
         {"text": "📚 Z-Library کتاب",   "callback_data": "mode_zlib"}],
        [{"text": "🖼 OCR متن از عکس",   "callback_data": "mode_ocr"},
         {"text": "🌐 IP / دامنه",        "callback_data": "mode_iplookup"}],
        [{"text": "📊 آمار کاربری",       "callback_data": "stats"},
         {"text": "🔒 حریم خصوصی",       "callback_data": "privacy"}],
        [{"text": "❓ راهنما",             "callback_data": "help"}],
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
        [{"text": "❌ انصراف",            "callback_data": "cancel"}],
    ]}

def instagram_kb():
    return {"inline_keyboard": [
        [{"text": "📋 پست‌های پروفایل",  "callback_data": "ig_profile"},
         {"text": "📥 دانلود پست/ریل",   "callback_data": "ig_dl"}],
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


    """Generic results keyboard for social media posts."""
    rows = []
    for i, r in enumerate(results[:10]):
        text = r.get("text", r.get("title", ""))[:35] or f"پیام {i+1}"
        icons = ("📷" if r.get("has_photo") else "") + ("🎬" if r.get("has_video") or r.get("is_video") else "")
        label = f"{icons} {text}".strip()[:48]
        rows.append([{"text": label, "callback_data": f"soc_{platform}_{cache_key}_{i}"}])
    rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
    return {"inline_keyboard": rows}

def social_post_kb(url: str, platform: str, has_media: bool) -> dict:
    """Buttons under a displayed post."""
    url_key = hashlib.md5(url.encode()).hexdigest()[:10]
    url_cache[url_key] = url
    rows = []
    if has_media:
        rows.append([{"text": "📥 دانلود رسانه", "callback_data": f"soc_dl_{platform}_{url_key}"}])
    rows.append([{"text": "🔗 باز کردن لینک",  "callback_data": f"site_ss_{url_key}"},
                 {"text": "🏠 منوی اصلی",       "callback_data": "home"}])
    return {"inline_keyboard": rows}


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
            ["yt-dlp", "--flat-playlist", "--print",
             "%(id)s|||%(title)s|||%(uploader)s|||%(duration_string)s|||%(thumbnail)s",
             f"ytsearch{max_results}:{query}", "--no-warnings",
             "--no-check-certificate"],
            capture_output=True, text=True, timeout=30,
        )
        items = []
        for line in result.stdout.strip().split("\n"):
            parts = line.split("|||")
            if len(parts) >= 2 and parts[0].strip():
                vid_id = parts[0].strip()
                items.append({
                    "id": vid_id,
                    "title": parts[1].strip(),
                    "uploader": parts[2].strip() if len(parts)>2 else "",
                    "duration":  parts[3].strip() if len(parts)>3 else "",
                    "thumbnail": parts[4].strip() if len(parts)>4 else
                                 f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg",
                    "url": f"https://www.youtube.com/watch?v={vid_id}",
                })
        log.info("youtube_search: %d results", len(items))
        return items
    except Exception as e:
        log.error("youtube_search: %s", e, exc_info=True)
        return []

def youtube_download(url: str, audio_only=False) -> Optional[tuple[bytes,str]]:
    log.info("youtube_download: url=%r audio=%s", url, audio_only)
    import shutil
    ffmpeg_dir = str(Path(shutil.which("ffmpeg") or "/usr/bin/ffmpeg").parent)

    base_cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--no-check-certificate",
        "--ffmpeg-location", ffmpeg_dir,
        "--geo-bypass",
        "--socket-timeout", "30",
        "--retries", "5",
        "--extractor-args", "youtube:player_client=ios,tv_embedded,web",
    ]

    def _run(extra_args, target_url) -> Optional[tuple[bytes,str]]:
        with tempfile.TemporaryDirectory() as tmp:
            out_tpl = os.path.join(tmp, "%(title).60s.%(ext)s")
            cmd = base_cmd + ["-o", out_tpl] + extra_args + [target_url]
            log.debug("yt-dlp cmd: %s", " ".join(cmd))
            proc = subprocess.run(cmd, capture_output=True, timeout=300)
            stderr = proc.stderr.decode(errors="replace")
            stdout = proc.stdout.decode(errors="replace")
            if proc.returncode != 0:
                log.error("yt-dlp rc=%d stderr=%s", proc.returncode, stderr[:800])
                return None
            log.debug("yt-dlp stdout: %s", stdout[:300])
            files = list(Path(tmp).glob("*"))
            if not files:
                log.error("yt-dlp: no output files in %s", tmp)
                return None
            f = files[0]
            data = f.read_bytes()
            log.info("yt-dlp: %s  %.1fMB", f.name, len(data)/1024/1024)
            return data, f.name

    if audio_only:
        # Try mp3 first, fallback to best audio then convert
        for args in [
            ["-x", "--audio-format", "mp3", "--audio-quality", "5", "-f", "bestaudio/best"],
            ["-x", "--audio-format", "mp3", "--audio-quality", "5"],
            ["-f", "bestaudio", "--audio-format", "mp3", "-x"],
        ]:
            result = _run(args, url)
            if result:
                return result
        return None
    else:
        # Try progressive quality steps — no strict codec filter
        for fmt in [
            "bestvideo[height<=720]+bestaudio/best[height<=720]",
            "bestvideo+bestaudio/best",
            "best[height<=720]",
            "best",
        ]:
            result = _run(["-f", fmt, "--merge-output-format", "mp4"], url)
            if result:
                # Trim if too large
                if len(result[0]) > MAX_FILE_SIZE - 1*1024*1024:
                    log.info("Video %.1fMB too large, trimming…", len(result[0])/1024/1024)
                    with tempfile.TemporaryDirectory() as tmp2:
                        src = Path(tmp2) / result[1]
                        src.write_bytes(result[0])
                        trimmed = _trim_video(src, tmp2, ffmpeg_dir)
                        if trimmed:
                            result = trimmed
                return result
        return None

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
# MUSIC PLATFORMS  (Spotify · SoundCloud · YouTube Music)
# ═══════════════════════════════════════════════════════════════════════════════

def music_search_ytdlp(query: str, max_results: int = 6,
                        source: str = "youtube") -> list[dict]:
    """Search for tracks via yt-dlp (YouTube or SoundCloud)."""
    log.info("music_search_ytdlp: %r source=%s", query, source)
    import shutil
    ffmpeg_dir = str(Path(shutil.which("ffmpeg") or "/usr/bin/ffmpeg").parent)
    search_url = (f"scsearch{max_results}:{query}" if source == "soundcloud"
                  else f"ytsearch{max_results}:{query}")
    try:
        cmd = [
            "yt-dlp", "--flat-playlist", "--no-warnings", "--no-check-certificate",
            "--ffmpeg-location", ffmpeg_dir,
            "--print",
            "%(id)s|||%(title)s|||%(uploader)s|||%(duration_string)s|||%(url)s|||%(thumbnail)s",
            search_url,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        items = []
        for line in result.stdout.strip().split("\n"):
            parts = line.split("|||")
            if len(parts) >= 2 and parts[0].strip():
                items.append({
                    "id":        parts[0].strip(),
                    "title":     parts[1].strip(),
                    "uploader":  parts[2].strip() if len(parts) > 2 else "",
                    "duration":  parts[3].strip() if len(parts) > 3 else "",
                    "url":       parts[4].strip() if len(parts) > 4 else "",
                    "thumbnail": parts[5].strip() if len(parts) > 5 else "",
                    "source":    source,
                })
            if len(items) >= max_results:
                break
        log.info("music_search_ytdlp (%s): %d results", source, len(items))
        return items
    except Exception as e:
        log.error("music_search_ytdlp: %s", e)
        return []


def music_search_multi(query: str) -> list[dict]:
    """Search YouTube + SoundCloud simultaneously, interleave results."""
    import threading
    results: dict[str, list] = {}

    def _s(src): results[src] = music_search_ytdlp(query, 5, src)

    threads = [threading.Thread(target=_s, args=(s,))
               for s in ("youtube", "soundcloud")]
    for t in threads: t.start()
    for t in threads: t.join(timeout=20)

    merged = []
    yt = results.get("youtube", [])
    sc = results.get("soundcloud", [])
    for i in range(max(len(yt), len(sc))):
        if i < len(yt): merged.append(yt[i])
        if i < len(sc): merged.append(sc[i])
    return merged[:10]


def music_download_ytdlp(url: str, source: str = "auto") -> Optional[tuple[bytes, str]]:
    """Download audio via yt-dlp (YouTube/SoundCloud/etc.) → MP3."""
    log.info("music_download_ytdlp: %s source=%s", url, source)
    import shutil
    ffmpeg_dir = str(Path(shutil.which("ffmpeg") or "/usr/bin/ffmpeg").parent)
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [
            "yt-dlp", "--no-playlist", "--no-warnings", "--no-check-certificate",
            "--ffmpeg-location", ffmpeg_dir,
            "--socket-timeout", "30", "--retries", "3",
            "-o", os.path.join(tmp, "%(title).60s.%(ext)s"),
            "-x", "--audio-format", "mp3", "--audio-quality", "0",
            "--embed-thumbnail", "--add-metadata",
        ]
        if "youtube.com" in url or "youtu.be" in url:
            cmd += ["--extractor-args", "youtube:player_client=tv_embedded,ios,web"]
        cmd.append(url)
        log.debug("music dl: %s", " ".join(cmd))
        proc = subprocess.run(cmd, capture_output=True, timeout=300)
        if proc.returncode != 0:
            log.error("music dl rc=%d: %s", proc.returncode,
                      proc.stderr.decode(errors="replace")[:400])
            return None
        files = [f for f in Path(tmp).glob("*")
                 if f.suffix.lower() in {".mp3",".m4a",".ogg",".opus",".flac"}
                 and f.stat().st_size > 1000]
        if not files:
            log.error("music dl: no output files")
            return None
        f = files[0]
        data = f.read_bytes()
        log.info("music dl OK: %s  %.1fMB", f.name, len(data)/1024/1024)
        return data, f.name


def spotify_download(url: str) -> Optional[tuple[bytes, str]]:
    """Download Spotify track/album/playlist via spotdl → MP3."""
    log.info("spotify_download: %s", url)
    import shutil
    ffmpeg_path = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
    spotdl_path = shutil.which("spotdl") or "spotdl"
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




# ─── Generic yt-dlp social downloader ────────────────────────────────────────
def social_download_ytdlp(url: str, audio_only=False,
                           cookies_file: str = "") -> Optional[tuple[bytes, str]]:
    """Download from any yt-dlp-supported URL (TikTok, Instagram, Twitter, etc.)"""
    log.info("social_download_ytdlp: %r  audio=%s", url, audio_only)
    import shutil
    ffmpeg_dir = str(Path(shutil.which("ffmpeg") or "/usr/bin/ffmpeg").parent)
    base = [
        "yt-dlp", "--no-playlist", "--no-warnings", "--no-check-certificate",
        "--ffmpeg-location", ffmpeg_dir,
        "--socket-timeout", "30", "--retries", "3",
    ]
    if cookies_file and Path(cookies_file).exists():
        base += ["--cookies", cookies_file]

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
        return _run(["-x", "--audio-format", "mp3", "--audio-quality", "5"], url)
    else:
        for fmt in [
            ["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4"],
            ["-f", "best", "--merge-output-format", "mp4"],
            ["--merge-output-format", "mp4"],
        ]:
            r = _run(fmt, url)
            if r:
                return r
    return None


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
    Strategy 1: scrape t.me/s/ for image/video URLs and download directly.
    Strategy 2: yt-dlp (for videos embedded in telegram previews).
    """
    log.info("tg_download_media: %s", msg_url)

    # Parse channel + message_id from URL
    # Formats: https://t.me/channel/123  or  https://t.me/s/channel/123
    m = re.search(r"t\.me/(?:s/)?([^/]+)/(\d+)", msg_url)
    if m:
        channel, msg_id = m.group(1), m.group(2)
        # Fetch the single message preview
        preview_url = f"https://t.me/s/{channel}?before={int(msg_id)+1}"
        try:
            r = WEB.get(preview_url,
                        headers={"User-Agent": UA_DESK}, timeout=20)
            log.debug("tg_single_msg: status=%d len=%d", r.status_code, len(r.text))
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "html.parser")
                # Find our specific message
                for wrap in soup.select(".tgme_widget_message_wrap"):
                    div = wrap.select_one(".tgme_widget_message")
                    if not div:
                        continue
                    post_id = div.get("data-post", "")
                    if not post_id.endswith(f"/{msg_id}"):
                        continue
                    # ── Try image ────────────────────────────────────────
                    for photo_el in wrap.select(".tgme_widget_message_photo_wrap"):
                        style = photo_el.get("style", "")
                        mi = re.search(
                            r"url\(['\"]?(https?://[^'\")\s]+)['\"]?\)", style)
                        if mi:
                            img_url = mi.group(1)
                            img_bytes = download_bytes(img_url, MAX_IMAGE_SIZE)
                            if img_bytes and len(img_bytes) > 500:
                                ext = img_url.rsplit(".", 1)[-1].split("?")[0] or "jpg"
                                fname = f"tg_{channel}_{msg_id}.{ext}"
                                log.info("tg_download: image %dKB", len(img_bytes)//1024)
                                return img_bytes, fname
                    # ── Try video via yt-dlp ──────────────────────────────
                    if wrap.select(".tgme_widget_message_video_wrap, video"):
                        break  # fall through to yt-dlp below
        except Exception as e:
            log.warning("tg_download scrape: %s", e)

    # Strategy 2: yt-dlp (works for video messages in public channels)
    log.info("tg_download: trying yt-dlp for %s", msg_url)
    result = social_download_ytdlp(msg_url)
    if result:
        return result

    log.error("tg_download_media_web: all strategies failed for %s", msg_url)
    return None


# ─── Twitter / X ─────────────────────────────────────────────────────────────
# Public Nitter instances (Twitter frontend, no auth needed)
NITTER_INSTANCES = [
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
    "https://nitter.1d4.us",
    "https://nitter.kavin.rocks",
    "https://twiiit.com",
]

def twitter_get_channel(username: str, limit: int = 20) -> list[dict]:
    """Read Twitter/X user timeline via Nitter (no auth needed)."""
    log.info("twitter_get_channel: @%s", username)
    username = username.lstrip("@").strip()
    for instance in NITTER_INSTANCES:
        url = f"{instance}/{username}"
        try:
            r = WEB.get(url, headers={"User-Agent": UA_DESK,
                                       "Accept-Language": "en-US,en;q=0.9"},
                        timeout=20)
            log.debug("nitter %s: status=%d len=%d", instance, r.status_code, len(r.text))
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            tweets = []
            for item in soup.select(".timeline-item")[:limit]:
                tweet_content = item.select_one(".tweet-content")
                if not tweet_content:
                    continue
                # Full text — no truncation
                text = tweet_content.get_text("\n", strip=True)
                date_el = item.select_one(".tweet-date a")
                date = date_el.get("title", "") if date_el else ""
                tweet_path = date_el.get("href", "") if date_el else ""
                tweet_url = f"https://twitter.com{tweet_path}" if tweet_path.startswith("/") else tweet_path
                nitter_url = f"{instance}{tweet_path}" if tweet_path else ""
                has_video = bool(item.select(".attachment.video-container, .gif"))
                # Collect all image URLs
                img_tags = item.select(".still-image img, .attachment.image img")
                img_urls = [img.get("src","") for img in img_tags if img.get("src")]
                # Make absolute
                img_urls = [f"{instance}{u}" if u.startswith("/") else u for u in img_urls]
                tweets.append({
                    "text": text,
                    "date": date[:19].replace("T", " "),
                    "url": tweet_url,
                    "nitter_url": nitter_url,
                    "has_video": has_video,
                    "has_photo": bool(img_urls),
                    "img_urls": img_urls,
                })
            if tweets:
                log.info("twitter: %d tweets via %s", len(tweets), instance)
                return tweets
        except Exception as e:
            log.warning("nitter %s: %s", instance, e)
    log.error("twitter_get_channel: all Nitter instances failed")
    return []


def twitter_download_media(tweet_url: str) -> Optional[tuple[bytes, str]]:
    """
    Download video/image from a tweet.
    1. vxtwitter/fxtwitter API (direct media URL, no auth)
    2. yt-dlp with twitter cookies
    3. yt-dlp via Nitter instances
    4. Direct image scrape from Nitter HTML
    """
    log.info("twitter_download_media: %s", tweet_url)
    tweet_url = tweet_url.replace("x.com/", "twitter.com/")
    m = re.search(r"twitter\.com/([^/]+)/status/(\d+)", tweet_url)
    username = m.group(1) if m else ""
    tweet_id = m.group(2) if m else ""

    # Strategy 1: vxtwitter / fxtwitter API
    if username and tweet_id:
        for api_url in [
            f"https://api.vxtwitter.com/{username}/status/{tweet_id}",
            f"https://api.fxtwitter.com/{username}/status/{tweet_id}",
        ]:
            try:
                r = WEB.get(api_url, headers={"User-Agent": UA_DESK}, timeout=15)
                log.debug("vxtwitter %s: status=%d", api_url, r.status_code)
                if r.status_code != 200:
                    continue
                jdata = r.json()
                tweet = jdata.get("tweet") or jdata
                media_list = (tweet.get("media", {}).get("all", []) or
                              tweet.get("media_extended", []) or
                              tweet.get("media", []))
                for media in media_list:
                    murl  = (media.get("url") or media.get("media_url_https") or "")
                    mtype = media.get("type", "")
                    if not murl:
                        continue
                    log.info("vxtwitter: type=%s url=%s", mtype, murl[:80])
                    content = download_bytes(murl)
                    if content and len(content) > 1000:
                        ext = "mp4" if mtype == "video" else \
                              murl.rsplit(".", 1)[-1].split("?")[0] or "jpg"
                        fname = f"tweet_{tweet_id}.{ext}"
                        log.info("vxtwitter OK: %.1fMB", len(content)/1024/1024)
                        return content, fname
            except Exception as e:
                log.warning("vxtwitter %s: %s", api_url, e)

    # Strategy 2: yt-dlp with twitter cookies
    cookies = TWITTER_COOKIES_FILE if TWITTER_COOKIES_FILE else ""
    result = social_download_ytdlp(tweet_url, cookies_file=cookies)
    if result:
        return result

    # Strategy 3: yt-dlp via Nitter
    if username and tweet_id:
        for instance in NITTER_INSTANCES:
            nitter_url = f"{instance}/{username}/status/{tweet_id}"
            log.debug("Nitter yt-dlp: %s", nitter_url)
            r2 = social_download_ytdlp(nitter_url)
            if r2:
                return r2

    # Strategy 4: scrape images from Nitter HTML
    if username and tweet_id:
        for instance in NITTER_INSTANCES:
            try:
                page = WEB.get(f"{instance}/{username}/status/{tweet_id}",
                               headers={"User-Agent": UA_DESK}, timeout=15)
                if page.status_code != 200:
                    continue
                soup = BeautifulSoup(page.text, "html.parser")
                for img in soup.select(".still-image img, .attachment img"):
                    src = img.get("src", "")
                    if not src:
                        continue
                    full = f"{instance}{src}" if src.startswith("/") else src
                    content = download_bytes(full, MAX_IMAGE_SIZE)
                    if content and len(content) > 1000:
                        fname = f"tweet_{tweet_id}.jpg"
                        log.info("Nitter img scrape OK: %dKB", len(content)//1024)
                        return content, fname
            except Exception as e:
                log.warning("Nitter scrape %s: %s", instance, e)

    log.error("twitter_download_media: all strategies failed for %s", tweet_url)
    return None



# ─── Instagram ───────────────────────────────────────────────────────────────
def instagram_download_all(url: str) -> list[dict]:
    """
    Download ALL media from an Instagram post (single/carousel/reel).
    Returns list of {data, fname, caption, is_video}.
    """
    log.info("instagram_download_all: %s", url)
    results = []

    # Strategy 1: instaloader (handles GraphSidecar carousels natively)
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

        caption = post.caption or ""
        log.info("instagram: typename=%s caption_len=%d", post.typename, len(caption))

        with tempfile.TemporaryDirectory() as tmp:
            L.dirname_pattern = tmp
            L.filename_pattern = "{shortcode}_{media_id}"
            L.download_post(post, target=tmp)

            # Collect all media files sorted by name (preserves carousel order)
            all_files = sorted(Path(tmp).rglob("*"), key=lambda f: f.name)
            media_files = [
                f for f in all_files
                if f.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov"}
                and f.stat().st_size > 1000
            ]
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

    # Strategy 2: yt-dlp (single video/reel, no carousel support)
    try:
        result = social_download_ytdlp(url)
        if result:
            data, fname = result
            return [{"data": data, "fname": fname, "caption": "", "is_video": True}]
    except Exception as e:
        log.error("yt-dlp instagram: %s", e)

    return []


def instagram_download(url: str) -> Optional[tuple[bytes, str]]:
    """Backward-compat single-item wrapper."""
    items = instagram_download_all(url)
    if items:
        return items[0]["data"], items[0]["fname"]
    return None



def instagram_get_profile(username: str) -> list[dict]:
    """Get recent Instagram posts from a public profile via instaloader."""
    log.info("instagram_get_profile: @%s", username)
    try:
        import instaloader
        L = instaloader.Instaloader(quiet=True, download_pictures=False)
        if INSTAGRAM_USER and INSTAGRAM_PASS:
            try:
                L.login(INSTAGRAM_USER, INSTAGRAM_PASS)
            except Exception as e:
                log.warning("instaloader login failed: %s", e)
        profile = instaloader.Profile.from_username(L.context, username.lstrip("@"))
        posts = []
        for post in profile.get_posts():
            # Get display URL (thumbnail for images/videos)
            display_url = getattr(post, "url", "") or getattr(post, "display_url", "")
            posts.append({
                "url": f"https://www.instagram.com/p/{post.shortcode}/",
                "shortcode": post.shortcode,
                "text": post.caption or "",   # Full caption — no truncation
                "date": str(post.date_local)[:16],
                "is_video": post.is_video,
                "likes": post.likes,
                "typename": post.typename,
                "display_url": display_url,
            })
            if len(posts) >= 12:
                break
        log.info("instagram_get_profile: %d posts", len(posts))
        return posts
    except Exception as e:
        log.error("instagram_get_profile: %s", e)
        return []


# ─── TikTok ──────────────────────────────────────────────────────────────────
def tiktok_download(url: str) -> Optional[tuple[bytes, str]]:
    """Download TikTok video via yt-dlp (no auth needed for public videos)."""
    log.info("tiktok_download: %s", url)
    return social_download_ytdlp(url)


def tiktok_user_videos(username: str, limit: int = 10) -> list[dict]:
    """Get TikTok user video list with thumbnails via yt-dlp flat extraction."""
    log.info("tiktok_user_videos: @%s", username)
    username = username.lstrip("@")
    try:
        result = subprocess.run(
            ["yt-dlp", "--flat-playlist", "--no-warnings", "--no-check-certificate",
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
    """Pixabay API (free, no key for basic use) + Unsplash CDN."""
    log.info("images_pexels: %r", query)
    PIXABAY_KEY = "47075717-fbc72d1e73d12c83cfdb8b44e"
    try:
        r = WEB.get("https://pixabay.com/api/",
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

    # Unsplash CDN fallback (redirects to real images)
    try:
        slug = urllib.parse.quote(query.replace(" ",","))
        results = []
        for i in range(min(max_results, 6)):
            r2 = WEB.get(f"https://source.unsplash.com/featured/800x600?{slug}&sig={i}",
                         allow_redirects=True, timeout=20,
                         headers={"User-Agent": "BaleBot/1.0"})
            log.debug("unsplash %d: status=%d len=%d", i, r2.status_code, len(r2.content))
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
    log.info("images_wikimedia: %r", query)
    try:
        r = WEB.get("https://commons.wikimedia.org/w/api.php",
                    params={"action":"query","list":"search","srsearch":query,
                            "srnamespace":"6","srlimit":max_results,"format":"json"},
                    headers={"User-Agent":"BaleBot/1.0"}, timeout=15)
        log.debug("wikimedia_search: status=%d", r.status_code)
        results = []
        for p in r.json().get("query",{}).get("search",[]):
            ir = WEB.get("https://commons.wikimedia.org/w/api.php",
                         params={"action":"query","titles":p["title"],
                                 "prop":"imageinfo","iiprop":"url","format":"json"},
                         headers={"User-Agent":"BaleBot/1.0"}, timeout=10)
            log.debug("wikimedia_info: status=%d", ir.status_code)
            for pg in ir.json().get("query",{}).get("pages",{}).values():
                url = (pg.get("imageinfo") or [{}])[0].get("url","")
                if url and any(url.lower().endswith(e)
                               for e in (".jpg",".jpeg",".png",".webp")):
                    results.append({"url":url,"title":p.get("title",query)})
                    break
            if len(results) >= max_results: break
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


# ─── Z-Library ────────────────────────────────────────────────────────────────

def _zlib_run(coro):
    """Run an async coroutine synchronously (for use from sync bot handlers)."""
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result()
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


async def _zlib_get_client():
    """Get or create a logged-in AsyncZlib client."""
    global _zlib_client
    if _zlib_client is not None:
        return _zlib_client
    if not ZLIB_EMAIL or not ZLIB_PASSWORD:
        log.error("ZLIB_EMAIL / ZLIB_PASSWORD not set")
        return None
    try:
        from zlibrary import AsyncZlib
        from zlibrary.exception import LoginFailed, NoDomainError
        client = AsyncZlib()
        await client.login(ZLIB_EMAIL, ZLIB_PASSWORD)
        _zlib_client = client
        log.info("Z-Library: logged in as %s", ZLIB_EMAIL)
        return client
    except (LoginFailed, NoDomainError) as e:
        log.error("Z-Library login failed: %s (domain unreachable or credentials invalid)", e)
        _zlib_client = None
        return None
    except Exception as e:
        log.error("Z-Library login failed: %s", e)
        _zlib_client = None
        return None


def zlib_search(query: str, count: int = 10,
                extensions: list = None, exact: bool = False) -> list[dict]:
    """
    جستجو در Z-Library.
    بازمی‌گرداند: list of {name, authors, year, publisher, language,
                           extension, size, cover, url, id}
    """
    log.info("zlib_search: %r  ext=%s", query, extensions)

    async def _search():
        client = await _zlib_get_client()
        if not client:
            return []
        try:
            from zlibrary.const import Extension
            from zlibrary.exception import EmptyQueryError, NoProfileError, ParseError
            
            if not query or not query.strip():
                log.error("zlib_search: empty query")
                return []
            
            ext_objs = []
            if extensions:
                ext_map = {e.value.upper(): e for e in Extension}
                for ext in extensions:
                    obj = ext_map.get(ext.upper())
                    if obj:
                        ext_objs.append(obj)

            paginator = await client.search(
                q=query,
                count=count,
                exact=exact,
                extensions=ext_objs,
            )
            await paginator.next()
            results = []
            for book in paginator.result:
                results.append({
                    "id":        book.get("id", ""),
                    "name":      book.get("name", "نامشخص"),
                    "authors":   book.get("authors", []),
                    "year":      book.get("year", ""),
                    "publisher": book.get("publisher", ""),
                    "language":  book.get("language", ""),
                    "extension": book.get("extension", ""),
                    "size":      book.get("size", ""),
                    "cover":     book.get("cover", ""),
                    "url":       book.get("url", ""),
                    "rating":    book.get("rating", ""),
                })
            log.info("zlib_search: %d results", len(results))
            return results
        except (EmptyQueryError, NoProfileError) as e:
            log.error("zlib_search error: %s", e)
            return []
        except ParseError as e:
            log.error("zlib_search error: Could not parse results from Z-Library (website may have changed): %s", e)
            return []
        except Exception as e:
            log.error("zlib_search error: %s", e, exc_info=True)
            return []

    return _zlib_run(_search())


def zlib_download(book_url: str) -> Optional[tuple[bytes, str]]:
    """
    دانلود فایل کتاب از Z-Library.
    بازمی‌گرداند: (file_bytes, filename) یا None
    """
    log.info("zlib_download: %s", book_url)

    async def _dl():
        client = await _zlib_get_client()
        if not client:
            return None
        try:
            from zlibrary.abs import BookItem
            from zlibrary.exception import NoProfileError, ParseError
            
            # Create a BookItem and fetch its details (gets download_url)
            book = BookItem(client._r, client.mirror)
            book["url"] = book_url
            parsed = await book.fetch()
            dl_url = parsed.get("download_url", "")
            if not dl_url or "Unavailable" in dl_url:
                log.error("zlib_download: no download_url in parsed: %s", parsed)
                return None
            log.info("zlib_download: dl_url=%s", dl_url)
            # Download the file
            data = await _async_download(dl_url, client)
            if not data:
                return None
            # Determine filename
            name = parsed.get("name", "book").replace("/", "_")[:60]
            ext  = parsed.get("extension", "pdf").lower()
            fname = f"{name}.{ext}"
            log.info("zlib_download: %s  %.1fMB", fname, len(data)/1024/1024)
            return data, fname
        except NoProfileError as e:
            log.error("zlib_download: not authenticated - %s", e)
            return None
        except ParseError as e:
            log.error("zlib_download: Could not parse book details from Z-Library: %s", e)
            return None
        except Exception as e:
            log.error("zlib_download: %s", e, exc_info=True)
            return None

    async def _async_download(url: str, client) -> Optional[bytes]:
        """Download file using the same session as zlibrary client."""
        from zlibrary.util import GET_request
        try:
            data = await GET_request(url, cookies=client.cookies)
            if data:
                return data if isinstance(data, bytes) else data.encode()
            return None
        except Exception as e:
            log.error("_async_download: %s", e)
            # Fallback: use requests
            r = WEB.get(url, cookies=client.cookies,
                        headers={"User-Agent": UA_DESK}, timeout=120)
            if r.status_code == 200:
                return r.content
            return None

    return _zlib_run(_dl())


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
    user_stats[cid]["requests"] = user_stats[cid].get("requests",0)+1
    user_stats[cid][key]        = user_stats[cid].get(key,0)+1

def get_state(cid: int) -> dict:
    return user_state.get(cid, {})

def set_state(cid: int, **kw):
    user_state[cid] = kw

def clear_state(cid: int):
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
    url_cache[key] = url
    return key

def get_url(key: str) -> Optional[str]:
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
    text = f"📺 *یوتیوب:* _{query}_\nروی ویدیو کلیک کنید تا دانلود شود:"
    kb = yt_results_kb(results, key, page, len(results)==8)
    send_message(cid, text, parse_mode="Markdown", reply_markup=kb)

def do_youtube_dl(cid: int, url: str):
    bump(cid, "downloads")
    if not ("youtu.be" in url or "youtube.com" in url):
        send_message(cid, "❌ لینک یوتیوب معتبر وارد کنید.", reply_markup=home_kb())
        return
    send_message(cid, "⏳ در حال دانلود ویدیو… (ممکن است چند دقیقه طول بکشد)")
    chat_action(cid, "upload_video")
    result = youtube_download(url, audio_only=False)
    _finish_video(cid, result)

def _finish_video(cid: int, result):
    if not result:
        send_message(cid,
                     "❌ دانلود ناموفق بود.\n"
                     "• اجرا کنید: `yt-dlp -U`\n"
                     "• یا یک ویدیو دیگر امتحان کنید.",
                     parse_mode="Markdown", reply_markup=home_kb())
        return
    data, fname = result
    fname = Path(fname).name
    size_mb = len(data)/1024/1024
    log.info("_finish_video: %s  %.1fMB", fname, size_mb)
    # smart_send handles chunking automatically — no size limit needed here
    if smart_send(cid, data, fname, caption=f"📺 {fname[:60]}", media_type="video"):
        send_message(cid, f"✅ ارسال شد ({size_mb:.1f}MB).", reply_markup=home_kb())
    else:
        send_message(cid, "❌ ارسال ناموفق بود.", reply_markup=home_kb())
    clear_state(cid)

def do_music(cid: int, query: str):
    """
    Music search — shows results from YouTube Music + SoundCloud as clickable buttons.
    If query is a Spotify/SoundCloud/YouTube URL, downloads directly.
    """
    bump(cid, "downloads")

    # Direct URL — download immediately
    if query.startswith("http"):
        if "spotify.com" in query:
            do_spotify_dl(cid, query); return
        elif "soundcloud.com" in query:
            do_soundcloud_dl(cid, query); return
        elif _is_youtube(query):
            _do_audio_download(cid, query, source="youtube"); return
        else:
            _do_audio_download(cid, query, source="auto"); return

    # Search query — show results as buttons
    send_message(cid, f"🔍 Searching for: _{query}_…", parse_mode="Markdown")
    chat_action(cid)
    results = music_search_multi(query)

    if not results:
        send_message(cid, "❌ No results found. Try a different search.", reply_markup=home_kb())
        return

    key = make_cache_key("mu", query, 0)
    cache_set(key, results)
    set_state(cid, mode="music", last_query=query, cache_key=key)

    rows = []
    for i, r in enumerate(results):
        title    = r.get("title", f"Track {i+1}")[:38]
        uploader = r.get("uploader", "")[:20]
        dur      = r.get("duration", "")
        source   = r.get("source", "")
        src_icon = {"youtube": "▶️", "soundcloud": "☁️", "ytmusic": "🎵"}.get(source, "🎵")
        parts = [f"{src_icon} {title}"]
        if uploader: parts.append(f"— {uploader}")
        if dur:      parts.append(f"({dur})")
        label = " ".join(parts)[:60]
        rows.append([{"text": label, "callback_data": f"mu_dl_{key}_{i}"}])

    rows.append([{"text": "🏠 Main menu", "callback_data": "home"}])
    send_message(cid,
        f"🎵 *Music results for:* _{query}_\nTap a track to download:",
        parse_mode="Markdown",
        reply_markup={"inline_keyboard": rows})


def _do_audio_download(cid: int, url: str, source: str = "auto",
                        title: str = ""):
    """Download audio from a URL and send to user."""
    bump(cid, "downloads")
    send_message(cid, f"⏳ Downloading audio…")
    chat_action(cid, "record_voice")

    result = music_download_ytdlp(url, source=source)
    if not result:
        send_message(cid,
            "❌ Download failed.\n"
            "• Try running: `yt-dlp -U`\n"
            "• Or paste the URL directly from your browser.",
            parse_mode="Markdown", reply_markup=home_kb())
        return

    data, fname = result
    fname = Path(fname).name
    caption = f"🎵 {title or fname[:60]}"
    if smart_send(cid, data, fname, caption=caption, media_type="audio"):
        send_message(cid, "✅ Sent!", reply_markup=home_kb())
    else:
        send_message(cid, "❌ Send failed.", reply_markup=home_kb())
    clear_state(cid)


def do_spotify_dl(cid: int, url: str):
    """Download Spotify track/album/playlist via spotdl."""
    bump(cid, "downloads")
    is_track    = "/track/" in url
    is_album    = "/album/" in url
    is_playlist = "/playlist/" in url

    kind = "track" if is_track else "album" if is_album else "playlist" if is_playlist else "item"
    send_message(cid, f"⏳ Downloading Spotify {kind}… (may take a while for albums)")
    chat_action(cid, "record_voice")

    result = spotify_download(url)
    if not result:
        send_message(cid,
            "❌ Spotify download failed.\n\n"
            "Make sure `spotdl` is installed:\n"
            "`pip install spotdl`\n\n"
            "For playlists, set Spotify credentials:\n"
            "`export SPOTIFY_CLIENT_ID=...`\n"
            "`export SPOTIFY_CLIENT_SECRET=...`",
            parse_mode="Markdown", reply_markup=home_kb())
        return

    data, fname = result
    caption = f"🟢 Spotify — {fname[:60]}"
    if smart_send(cid, data, fname, caption=caption):
        send_message(cid, "✅ Sent!", reply_markup=home_kb())
    else:
        send_message(cid, "❌ Send failed.", reply_markup=home_kb())
    clear_state(cid)


def do_soundcloud_dl(cid: int, url: str):
    """Download SoundCloud track/playlist."""
    bump(cid, "downloads")
    send_message(cid, "⏳ Downloading from SoundCloud…")
    chat_action(cid, "record_voice")

    result = soundcloud_download(url)
    if not result:
        send_message(cid, "❌ SoundCloud download failed.", reply_markup=home_kb())
        return

    data, fname = result
    caption = f"☁️ SoundCloud — {fname[:60]}"
    if smart_send(cid, data, fname, caption=caption, media_type="audio"):
        send_message(cid, "✅ Sent!", reply_markup=home_kb())
    else:
        send_message(cid, "❌ Send failed.", reply_markup=home_kb())
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

def do_github_release(cid: int, repo_full: str):
    bump(cid, "downloads")
    chat_action(cid)
    rel = github_latest_release(repo_full)
    if not rel:
        send_message(cid, "❌ هیچ Release‌ای یافت نشد.", reply_markup=home_kb())
        return
    tag = rel.get("tag_name","")
    body = rel.get("body","")[:500]
    assets = rel.get("assets",[])
    lines = [f"📦 *آخرین Release: {repo_full}*",
             f"🏷 نسخه: `{tag}`",
             f"📅 تاریخ: {rel.get('published_at','')[:10]}",
             f"\n{body}" if body else ""]
    if assets:
        lines.append("\n*فایل‌های قابل دانلود:*")
        for i, a in enumerate(assets[:8]):
            size_mb = a.get("size",0)//1024//1024
            lines.append(f"{i+1}. {a['name']} ({size_mb}MB)")
    # Build asset download buttons
    rows = []
    for i, a in enumerate(assets[:6]):
        safe_repo = repo_full.replace("/","__")
        rows.append([{"text": f"⬇️ {a['name'][:40]}",
                       "callback_data": f"ghrel_dl_{safe_repo}_{i}"}])
    rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
    # cache assets
    akey = f"ghrel_{repo_full.replace('/','__')}"
    cache_set(akey, assets)
    send_message(cid, "\n".join(lines), parse_mode="Markdown",
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
        send_message(cid,
            "❌ Z-Library پیکربندی نشده.\n\n"
            "۱. در https://z-library.sk/login ثبت‌نام کنید\n"
            "۲. متغیرهای محیطی تنظیم کنید:\n"
            "`export ZLIB_EMAIL=your@email.com`\n"
            "`export ZLIB_PASSWORD=yourpassword`",
            parse_mode="Markdown", reply_markup=home_kb())
        return

    send_message(cid, f"⏳ در حال جستجو در Z-Library: _{query}_…",
                 parse_mode="Markdown")
    chat_action(cid)
    results = zlib_search(query, count=10, extensions=extensions)

    if not results:
        send_message(cid,
            "❌ نتیجه‌ای یافت نشد.\n"
            "• کلمات دیگری امتحان کنید\n"
            "• اگر خطای لاگین است، مطمئن شوید ZLIB_EMAIL/PASSWORD درست است",
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
    url_key = hashlib.md5(url.encode()).hexdigest()[:10] if url else ""
    if url_key:
        url_cache[url_key] = url

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
        send_message(cid,
            "❌ دانلود ناموفق بود.\n"
            "• ممکن است این کتاب نیاز به اشتراک ویژه داشته باشد\n"
            "• یا لاگین منقضی شده — ربات را ری‌استارت کنید",
            reply_markup=home_kb())
        return

    data, fname = result
    log.info("zlib: sending %s  %.1fMB", fname, len(data)/1024/1024)
    if smart_send(cid, data, fname, caption=f"📚 {fname[:80]}"):
        send_message(cid, "✅ کتاب ارسال شد.", reply_markup=home_kb())
    else:
        send_message(cid, "❌ ارسال فایل ناموفق بود.", reply_markup=home_kb())
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
        send_message(cid, "❌ دانلود ناموفق — این پیام ممکن است رسانه نداشته باشد.",
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

    # Auto-download and send images
    for img_url in img_urls[:4]:
        try:
            img_bytes = download_bytes(img_url, MAX_IMAGE_SIZE)
            if img_bytes and len(img_bytes) > 500:
                send_photo(cid, img_bytes, caption="🐦")
        except Exception as e:
            log.warning("twitter img dl: %s", e)

    # Show video download button if has video
    kb = social_post_kb(url, "tw", has_vid) if url else home_kb()
    if has_vid or not img_urls:
        send_message(cid, "👆", reply_markup=kb)
    else:
        send_message(cid, "✅", reply_markup=home_kb())


def do_twitter_dl(cid: int, url: str):
    """Download media from a tweet URL."""
    bump(cid, "downloads")
    send_message(cid, f"⏳ در حال دانلود از توییت…")
    chat_action(cid, "upload_video")
    result = twitter_download_media(url)
    if not result:
        send_message(cid, "❌ دانلود ناموفق.\nاین توییت ممکن است رسانه نداشته باشد.",
                     reply_markup=home_kb())
        return
    data, fname = result
    smart_send(cid, data, fname, caption=f"🐦 {fname[:60]}")
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


def do_instagram_dl(cid: int, url: str):
    """Download ALL media from an Instagram post (single/carousel/reel) with full caption."""
    bump(cid, "downloads")
    send_message(cid, "⏳ Downloading from Instagram…")
    chat_action(cid, "upload_video")

    items = instagram_download_all(url)
    if not items:
        send_message(cid,
            "❌ Download failed.\n"
            "• Post must be public\n"
            "• Set INSTAGRAM_USER/PASS for private posts",
            reply_markup=home_kb())
        return

    caption = items[0].get("caption", "")
    total   = len(items)
    log.info("do_instagram_dl: sending %d items, caption_len=%d", total, len(caption))

    if total > 1:
        send_message(cid, f"📸 Found {total} items in carousel — sending all…")

    sent = 0
    for i, item in enumerate(items):
        # Caption only on first item
        item_caption = caption[:1024] if i == 0 and caption else ""
        try:
            ok = smart_send(cid, item["data"], item["fname"],
                            caption=item_caption,
                            media_type="video" if item["is_video"] else "document")
            if ok:
                sent += 1
        except Exception as e:
            log.error("do_instagram_dl item %d: %s", i, e)

    send_message(cid, f"✅ Sent {sent}/{total} items.", reply_markup=home_kb())
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


def do_tiktok_dl(cid: int, url: str):
    """Download TikTok video."""
    bump(cid, "downloads")
    send_message(cid, f"⏳ در حال دانلود از TikTok…")
    chat_action(cid, "upload_video")
    result = tiktok_download(url)
    if not result:
        send_message(cid, "❌ دانلود ناموفق.", reply_markup=home_kb())
        return
    data, fname = result
    smart_send(cid, data, fname, caption=f"🎵 {fname[:60]}")
    send_message(cid, "✅ ارسال شد.", reply_markup=home_kb())
    clear_state(cid)



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
        "currency":     lambda: do_currency(cid, text),
        "iplookup":     lambda: do_ip_lookup(cid, text),
        "shorten":      lambda: do_shorten(cid, text),
        "expand":       lambda: do_expand(cid, text),
        "paste":        lambda: do_paste(cid, text),
        "qr":           lambda: do_qr(cid, text),
        "ocr":          lambda: send_message(cid,"🖼 لطفاً عکس ارسال کنید.",reply_markup=cancel_kb()),
        "images_pick":  lambda: do_images(cid, st.get("last_query", text),
                                          st.get("img_source","bing")),
        # Social media
        "tg_read":      lambda: do_tg_read(cid, text, False),
        "tg_mtproto":   lambda: do_tg_read(cid, text, True),
        "tg_dl":        lambda: do_tg_dl_media(cid, text),
        "twitter_tl":   lambda: do_twitter_timeline(cid, text),
        "twitter_dl":   lambda: do_twitter_dl(cid, text),
        "ig_profile":   lambda: do_instagram_profile(cid, text),
        "ig_dl":        lambda: do_instagram_dl(cid, text),
        "tt_user":      lambda: do_tiktok_user(cid, text),
        "tt_dl":        lambda: do_tiktok_dl(cid, text),
        # RSS
        "rss":          lambda: do_rss(cid, text),
        # Z-Library
        "zlib":         lambda: do_zlib_search(cid, text,
                                               st.get("zlib_ext")),
    }
    if mode in dispatch:
        dispatch[mode]()
    elif text.startswith("http"):
        # Smart URL detection — route to correct handler automatically
        if _is_youtube(text):
            do_youtube_dl(cid, text)
        elif "spotify.com" in text:
            do_spotify_dl(cid, text)
        elif "soundcloud.com" in text:
            do_soundcloud_dl(cid, text)
        elif any(x in text for x in ["tiktok.com", "vm.tiktok.com"]):
            do_tiktok_dl(cid, text)
        elif any(x in text for x in ["twitter.com", "x.com", "t.co"]):
            do_twitter_dl(cid, text)
        elif "instagram.com" in text:
            do_instagram_dl(cid, text)
        elif "t.me/" in text:
            do_tg_dl_media(cid, text)
        else:
            do_open_url(cid, text)
    else:
        do_search(cid, text)

# ═══════════════════════════════════════════════════════════════════════════════
# CALLBACK HANDLER
# ═══════════════════════════════════════════════════════════════════════════════
def handle_callback(cb: dict):
    cid  = cb["message"]["chat"]["id"]
    data = cb.get("data","")
    answer_cb(cb["id"])
    init_user(cid)
    log.info("CB cid=%d data=%r", cid, data)

    st = get_state(cid)

    # ── Home / Cancel ─────────────────────────────────────────────────────
    if data in ("home","cancel"):
        clear_state(cid)
        send_message(cid, "🏠 منوی اصلی:", reply_markup=main_menu_kb()); return

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
        "mode_music":     ("music",     "🎵 Track/artist name or paste a URL (Spotify/SoundCloud/YouTube):"),
        "mode_spotify":   ("music",     "🟢 Paste a Spotify track, album, or playlist URL:"),
        "mode_soundcloud":("music",     "☁️ Paste a SoundCloud URL or search: artist name - song:"),
        "mode_iplookup":  ("iplookup",  "🌐 آدرس IP یا دامنه:"),
        "mode_rss":       ("rss",       "📰 آدرس فید RSS یا سایت خبری را وارد کنید:"),
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

    # YouTube result click → download
    m = re.match(r"yt_res_(\w+)_(\d+)$", data)
    if m:
        key, idx = m.group(1), int(m.group(2))
        results = cache_get(key)
        if not results or idx >= len(results):
            send_message(cid, "❌ نتیجه منقضی شده. دوباره جستجو کنید."); return
        vid = results[idx]

        # Send thumbnail first
        thumb_url = vid.get("thumbnail") or \
                    f"https://i.ytimg.com/vi/{vid.get('id','')}/hqdefault.jpg"
        if thumb_url:
            try:
                thumb = download_bytes(thumb_url, MAX_IMAGE_SIZE)
                if thumb:
                    uploader = vid.get("uploader","")
                    dur = vid.get("duration","")
                    caption = f"📺 {vid['title']}"
                    if uploader: caption += f"\n🎬 {uploader}"
                    if dur:      caption += f"\n⏱ {dur}"
                    send_photo(cid, thumb, caption=caption)
            except Exception as e:
                log.warning("yt thumb: %s", e)

        send_message(cid, f"⏳ دانلود: _{vid['title']}_…", parse_mode="Markdown")
        chat_action(cid, "upload_video")
        result = youtube_download(vid["url"], audio_only=False)
        _finish_video(cid, result); return

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
        book_url = url_cache.get(url_key, "")
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
            send_message(cid,
                "❌ MTProto پیکربندی نشده.\n\n"
                "برای فعال‌سازی:\n"
                "1. به https://my.telegram.org بروید\n"
                "2. API_ID و API_HASH بگیرید\n"
                "`export TG_API_ID=12345`\n"
                "`export TG_API_HASH=abc123`",
                parse_mode="Markdown", reply_markup=home_kb()); return
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
            text = f"🎵 *{title}*"
            if dur:
                text += f"\n⏱ {dur}"
            if url:
                text += f"\n[🔗 لینک]({url})"
            send_message(cid, text, parse_mode="Markdown")
            # Auto-download thumbnail
            if thumbnail:
                try:
                    thumb_bytes = download_bytes(thumbnail, MAX_IMAGE_SIZE)
                    if thumb_bytes and len(thumb_bytes) > 500:
                        send_photo(cid, thumb_bytes, caption="🎵")
                except Exception as e:
                    log.warning("tt thumb dl: %s", e)
            kb = social_post_kb(url, "tt", True) if url else home_kb()
            send_message(cid, "📥 دانلود ویدیو؟", reply_markup=kb)
        return

    # ── Social media download button: soc_dl_{platform}_{url_key} ────────
    m = re.match(r"soc_dl_(tg|tw|ig|tt)_(\w+)$", data)
    if m:
        platform, url_key = m.group(1), m.group(2)
        url = url_cache.get(url_key, "")
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
            send_message(cid, "❌ Result expired. Search again."); return
        track = results[idx]
        url   = track.get("url", "")
        title = track.get("title", "")
        thumb = track.get("thumbnail", "")

        # Send thumbnail first
        if thumb:
            try:
                tb = download_bytes(thumb, MAX_IMAGE_SIZE)
                if tb:
                    uploader = track.get("uploader", "")
                    dur      = track.get("duration", "")
                    cap = title
                    if uploader: cap += f"\n🎤 {uploader}"
                    if dur:      cap += f"\n⏱ {dur}"
                    send_photo(cid, tb, caption=cap[:1024])
            except Exception as e:
                log.warning("music thumb: %s", e)

        source = track.get("source", "youtube")
        _do_audio_download(cid, url, source=source, title=title)
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
def handle_update(update: dict):
    if "message" in update:
        msg = update["message"]
        cid = msg["chat"]["id"]
        text = msg.get("text","")
        st = get_state(cid)
        # Special case: images_query mode
        if st.get("mode") == "images_query" and text and not text.startswith("/"):
            init_user(cid)
            handle_message_images_query(cid, text, st)
        else:
            handle_message(msg)
    elif "callback_query" in update:
        handle_callback(update["callback_query"])

def run():
    log.info("Bot starting — token ends with …%s", TOKEN[-6:] if len(TOKEN)>6 else "???")
    if TOKEN == "YOUR_BOT_TOKEN_HERE":
        log.critical("BALE_TOKEN not set! Run: export BALE_TOKEN=your_token")
        return
    offset = 0
    while True:
        try:
            resp = api("getUpdates", offset=offset, timeout=30)
            if not resp.get("ok"):
                log.warning("getUpdates not ok: %s", resp)
                time.sleep(5); continue
            for update in resp.get("result", []):
                offset = update["update_id"] + 1
                try:
                    handle_update(update)
                except Exception as e:
                    log.error("handle_update: %s", e, exc_info=True)
        except KeyboardInterrupt:
            log.info("Bot stopped."); break
        except Exception as e:
            log.error("Polling error: %s", e, exc_info=True)
            time.sleep(5)

if __name__ == "__main__":
    run()