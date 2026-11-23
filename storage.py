# bale_bot/storage.py
# SQLite persistence + Redis for multi-instance scaling
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

import redis

from config import get_settings
from logging_config import get_logger

log = get_logger(__name__)
_settings = get_settings()

DB_PATH = Path(_settings.DB_PATH)
if not DB_PATH.is_absolute():
    DB_PATH = (Path(__file__).resolve().parent / DB_PATH).resolve()
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# Durable metadata retention limits. Keep these before API definitions because
# they are used as bounded default arguments below.
LIBRARY_MAX_ITEMS = 100
LIBRARY_MAX_TOTAL = 5000
USAGE_MAX_ITEMS = 100
USAGE_MAX_TOTAL = 5000
RSS_MAX_SUBS_PER_USER = 10
RSS_MAX_SUBS_TOTAL = 500

# Thread-local connection pool for SQLite
_local = threading.local()
_db_lock = threading.Lock()

# Redis client (lazy init)
_redis_client: Optional[redis.Redis] = None
_redis_lock = threading.Lock()


def get_redis() -> Optional[redis.Redis]:
    """Get or create Redis client for distributed caching."""
    global _redis_client
    if not _settings.REDIS_URL:
        return None
    with _redis_lock:
        if _redis_client is None:
            try:
                _redis_client = redis.from_url(
                    _settings.REDIS_URL,
                    encoding="utf-8",
                    decode_responses=True,
                    max_connections=_settings.DB_POOL_SIZE,
                    socket_timeout=5,
                    socket_connect_timeout=5,
                    retry_on_timeout=True,
                )
                # Test connection
                _redis_client.ping()
                log.info("redis_connected", url=_settings.REDIS_URL)
            except Exception as e:
                log.warning("redis_connection_failed", error=str(e))
                _redis_client = None
        return _redis_client


def close_redis() -> None:
    """Close Redis connection."""
    global _redis_client
    with _redis_lock:
        if _redis_client:
            try:
                _redis_client.close()
            except Exception:
                pass
            _redis_client = None


# ═══════════════════════════════════════════════════════════════════════════════
# SQLITE CONNECTION MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════

@contextmanager
def _get_conn():
    """Get a thread-local SQLite connection with WAL mode."""
    if not hasattr(_local, "conn") or _local.conn is None:
        with _db_lock:
            _local.conn = sqlite3.connect(
                DB_PATH,
                check_same_thread=False,
                timeout=30.0,
            )
            _local.conn.execute("PRAGMA journal_mode=WAL")
            _local.conn.execute("PRAGMA synchronous=FULL")
            _local.conn.execute("PRAGMA busy_timeout=30000")
            _local.conn.execute("PRAGMA foreign_keys=ON")
            _local.conn.row_factory = sqlite3.Row
    try:
        yield _local.conn
    except Exception:
        _local.conn.rollback()
        raise


def close_db() -> None:
    """Close thread-local connection."""
    if hasattr(_local, "conn") and _local.conn:
        try:
            _local.conn.close()
        except Exception:
            pass
        _local.conn = None


# ═══════════════════════════════════════════════════════════════════════════════
# SCHEMA INITIALIZATION
# ═══════════════════════════════════════════════════════════════════════════════

SCHEMA = """
-- User state (per chat+platform)
CREATE TABLE IF NOT EXISTS user_state (
    key TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    data JSON NOT NULL,
    updated_at INTEGER NOT NULL
);

-- User statistics (includes tier: normal/pro/dev)
CREATE TABLE IF NOT EXISTS user_stats (
    key TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    data JSON NOT NULL,
    updated_at INTEGER NOT NULL
);

-- User tiers (normal, pro, dev)
CREATE TABLE IF NOT EXISTS user_tiers (
    platform TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    tier TEXT NOT NULL DEFAULT 'normal',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (platform, chat_id)
);

-- Result cache for search results (used by callback buttons)
CREATE TABLE IF NOT EXISTS result_cache (
    key TEXT PRIMARY KEY,
    data JSON NOT NULL,
    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_result_cache_expires ON result_cache(expires_at);

-- URL cache for callback data (short keys -> full URLs)
CREATE TABLE IF NOT EXISTS url_cache (
    key TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    chat_id INTEGER,
    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_url_cache_expires ON url_cache(expires_at);

-- Subscriptions (for future notification features)
CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    topic TEXT NOT NULL,
    config JSON,
    active INTEGER DEFAULT 1,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(platform, chat_id, topic)
);

-- Subscription delivery windows (tier quota enforcement)
CREATE TABLE IF NOT EXISTS subscription_deliveries (
    platform TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    window TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (platform, chat_id, window)
);

-- Usage history (audit log)
CREATE TABLE IF NOT EXISTS usage_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    details JSON,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_history_chat ON usage_history(platform, chat_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_usage_history_action ON usage_history(action, created_at DESC);

-- Durable library metadata (no media bytes are stored)
CREATE TABLE IF NOT EXISTS library_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'saved',
    title TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '{}',
    watchlist INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(platform, chat_id, kind, url)
);
CREATE INDEX IF NOT EXISTS idx_library_items_owner
    ON library_items(platform, chat_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_library_items_watchlist
    ON library_items(platform, chat_id, watchlist, updated_at DESC);

-- Distributed locks (Redis-backed, SQLite fallback)
CREATE TABLE IF NOT EXISTS distributed_locks (
    key TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);

"""
def init_db() -> None:
    """Initialize database schema."""
    with _get_conn() as conn:
        conn.executescript(SCHEMA)
        conn.commit()
    log.info("database_initialized", path=str(DB_PATH))


def cleanup_expired() -> None:
    """Clean expired cache and delivery metadata without touching user content."""
    now = int(time.time())
    today = time.strftime("%Y%m%d", time.gmtime(now))
    with _get_conn() as conn:
        rc_deleted = conn.execute(
            "DELETE FROM result_cache WHERE expires_at < ?", (now,)
        ).rowcount
        uc_deleted = conn.execute(
            "DELETE FROM url_cache WHERE expires_at < ?", (now,)
        ).rowcount
        # Delivery windows are short-lived counters, not user history.
        conn.execute(
            "DELETE FROM subscription_deliveries WHERE window < ?", (today,)
        )
        conn.commit()
    if rc_deleted or uc_deleted:
        log.debug(
            "cache_cleanup", result_cache_deleted=rc_deleted,
            url_cache_deleted=uc_deleted,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# KEY HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _pkey(platform: str, chat_id: int) -> str:
    """Platform-scoped key to avoid Bale/Telegram chat_id collisions."""
    return f"{platform}:{chat_id}"


def _now() -> int:
    return int(time.time())


def _ttl(seconds: int) -> int:
    return _now() + seconds


# ═══════════════════════════════════════════════════════════════════════════════
# USER STATE (replaces user_state dict)
# ═══════════════════════════════════════════════════════════════════════════════

def get_state(platform: str, chat_id: int) -> dict:
    """Read authoritative conversation state from SQLite."""
    key = _pkey(platform, chat_id)

    with _get_conn() as conn:
        row = conn.execute(
            "SELECT data FROM user_state WHERE key = ?", (key,)
        ).fetchone()
        if row:
            return json.loads(row["data"])
    return {"mode": None}


def set_state(platform: str, chat_id: int, **kw) -> None:
    """Commit user state to SQLite before returning."""
    key = _pkey(platform, chat_id)
    data = json.dumps(kw)
    now = _now()

    with _get_conn() as conn:
        conn.execute(
            """INSERT INTO user_state (key, platform, chat_id, data, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at""",
            (key, platform, chat_id, data, now),
        )
        conn.commit()



def clear_state(platform: str, chat_id: int) -> None:
    """Clear user state (reset to default mode)."""
    key = _pkey(platform, chat_id)
    with _get_conn() as conn:
        conn.execute(
            "UPDATE user_state SET data = ?, updated_at = ? WHERE key = ?",
            (json.dumps({"mode": None}), _now(), key),
        )
        conn.commit()



# ═══════════════════════════════════════════════════════════════════════════════
# USER STATS (replaces user_stats dict)
# ═══════════════════════════════════════════════════════════════════════════════

DEFAULT_STATS = {
    "requests": 0,
    "joined": None,  # set on first init
    "searches": 0,
    "downloads": 0,
    "translations": 0,
    "ocr": 0,
}


def _today() -> str:
    """Return current UTC date as YYYY-MM-DD string."""
    return time.strftime("%Y-%m-%d", time.gmtime())


# ═══════════════════════════════════════════════════════════════════════════════
# USER TIERS (normal, pro, dev)
# ═══════════════════════════════════════════════════════════════════════════════

TIER_LIMITS = {
    "normal": {"requests_per_day": 100, "searches_per_day": 50, "downloads_per_day": 20},
    "pro": {"requests_per_day": 1000, "searches_per_day": 500, "downloads_per_day": 200},
    "dev": {"requests_per_day": -1, "searches_per_day": -1, "downloads_per_day": -1},  # -1 = unlimited
}


def get_user_tier(platform: str, chat_id: int) -> str:
    """Get user tier (normal/pro/dev). Defaults to 'normal'."""
    now = _now()
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT tier FROM user_tiers WHERE platform = ? AND chat_id = ?",
            (platform, chat_id),
        ).fetchone()
        if row:
            return row["tier"]

        # A concurrent initializer or tier grant may have inserted first.
        conn.execute(
            "INSERT OR IGNORE INTO user_tiers (platform, chat_id, tier, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (platform, chat_id, "normal", now, now),
        )
        conn.commit()
        return conn.execute(
            "SELECT tier FROM user_tiers WHERE platform = ? AND chat_id = ?",
            (platform, chat_id),
        ).fetchone()["tier"]


def set_user_tier(platform: str, chat_id: int, tier: str) -> None:
    """Set user tier (normal/pro/dev)."""
    if tier not in TIER_LIMITS:
        raise ValueError(f"Invalid tier: {tier}. Must be one of: {list(TIER_LIMITS.keys())}")

    now = _now()
    with _get_conn() as conn:
        conn.execute(
            """INSERT INTO user_tiers (platform, chat_id, tier, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(platform, chat_id) DO UPDATE SET tier=excluded.tier, updated_at=excluded.updated_at""",
            (platform, chat_id, tier, now, now),
        )
        conn.commit()

    log.info("user_tier_set", platform=platform, chat_id=chat_id, tier=tier)


def check_rate_limit(platform: str, chat_id: int, action: str = "requests") -> tuple[bool, dict]:
    """
    Check if user has exceeded their rate limit for an action.
    Returns (allowed, info_dict).
    action: "requests", "searches", "downloads"
    """
    tier = get_user_tier(platform, chat_id)
    limits = TIER_LIMITS[tier]
    limit_key = f"{action}_per_day"
    limit = limits.get(limit_key, -1)

    # Dev tier = unlimited
    if limit == -1:
        return True, {"tier": tier, "limit": -1, "used": 0, "remaining": -1}

    stats = get_user_stats(platform, chat_id)
    # Reset daily counters if date changed
    today = _today()
    daily = stats.get("daily", {})
    if daily.get("date") != today:
        daily = {"date": today, "requests": 0, "searches": 0, "downloads": 0, "translations": 0, "ocr": 0}
    used = daily.get(action, 0)
    remaining = max(0, limit - used)

    return used < limit, {"tier": tier, "limit": limit, "used": used, "remaining": remaining}


def init_user_stats(platform: str, chat_id: int) -> dict:
    """Initialize user stats if not exists (and ensure tier exists)."""
    # Ensure tier entry exists
    get_user_tier(platform, chat_id)

    key = _pkey(platform, chat_id)
    # Stats and quota decisions always use SQLite, never a stale Redis cache.
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT data FROM user_stats WHERE key = ?", (key,)
        ).fetchone()
        if row:
            return json.loads(row["data"])

    # Create new
    stats = DEFAULT_STATS.copy()
    stats["joined"] = time.strftime("%Y-%m-%d")
    # Initialize daily counters
    stats["daily"] = {"date": _today(), "requests": 0, "searches": 0, "downloads": 0, "translations": 0, "ocr": 0}
    data = json.dumps(stats)
    with _get_conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO user_stats (key, platform, chat_id, data, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (key, platform, chat_id, data, _now()),
        )
        conn.commit()
        # Return the winning insert, not defaults discarded by OR IGNORE.
        row = conn.execute(
            "SELECT data FROM user_stats WHERE key = ?", (key,)
        ).fetchone()
        return json.loads(row["data"])


def get_user_stats(platform: str, chat_id: int) -> dict:
    """Get user stats."""
    return init_user_stats(platform, chat_id)


def bump_stat(platform: str, chat_id: int, key: str = "requests") -> None:
    """Increment a stat counter atomically."""
    pkey = _pkey(platform, chat_id)
    today = _today()
    max_retries = 3

    for attempt in range(max_retries):
        try:
            with _get_conn() as conn:
                # Use BEGIN IMMEDIATE to serialize concurrent writers
                conn.execute("BEGIN IMMEDIATE")
                try:
                    # Re-read the row inside the transaction
                    row = conn.execute(
                        "SELECT data FROM user_stats WHERE key = ?", (pkey,)
                    ).fetchone()

                    if row:
                        stats = json.loads(row["data"])
                    else:
                        # Initialize if somehow missing (race with init_user_stats)
                        stats = DEFAULT_STATS.copy()
                        stats["joined"] = time.strftime("%Y-%m-%d")
                        stats["daily"] = {"date": today, "requests": 0, "searches": 0, "downloads": 0, "translations": 0, "ocr": 0}

                    # Ensure daily structure exists and reset if date changed
                    daily = stats.get("daily", {})
                    if daily.get("date") != today:
                        daily = {"date": today, "requests": 0, "searches": 0, "downloads": 0, "translations": 0, "ocr": 0}

                    # Increment lifetime counters
                    stats["requests"] = stats.get("requests", 0) + 1
                    if key != "requests":
                        stats[key] = stats.get(key, 0) + 1

                    # Increment daily counters (don't double-count if key == "requests")
                    daily["requests"] = daily.get("requests", 0) + 1
                    if key != "requests":
                        daily[key] = daily.get(key, 0) + 1
                    stats["daily"] = daily

                    data = json.dumps(stats)
                    conn.execute(
                        """INSERT INTO user_stats (key, platform, chat_id, data, updated_at)
                           VALUES (?, ?, ?, ?, ?)
                           ON CONFLICT(key) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at""",
                        (pkey, platform, chat_id, data, _now()),
                    )
                    conn.commit()

                    return
                except Exception:
                    conn.rollback()
                    raise
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < max_retries - 1:
                time.sleep(0.05)  # 50ms backoff
                continue
            raise


def claim_download(platform: str, chat_id: int) -> bool:
    """Atomically reserve one daily download slot. False means tier quota reached."""
    tier = get_user_tier(platform, chat_id)
    limit = TIER_LIMITS[tier]["downloads_per_day"]
    if limit < 0:
        bump_stat(platform, chat_id, "downloads")
        return True
    pkey, today = _pkey(platform, chat_id), _today()
    with _get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT data FROM user_stats WHERE key = ?", (pkey,)).fetchone()
            stats = json.loads(row["data"]) if row else DEFAULT_STATS.copy()
            daily = stats.get("daily", {})
            if daily.get("date") != today:
                daily = {"date": today, "requests": 0, "searches": 0, "downloads": 0, "translations": 0, "ocr": 0}
            if daily.get("downloads", 0) >= limit:
                conn.rollback()
                return False
            stats["joined"] = stats.get("joined") or time.strftime("%Y-%m-%d")
            stats["requests"] = stats.get("requests", 0) + 1
            stats["downloads"] = stats.get("downloads", 0) + 1
            daily["requests"] = daily.get("requests", 0) + 1
            daily["downloads"] = daily.get("downloads", 0) + 1
            stats["daily"] = daily
            data = json.dumps(stats)
            conn.execute(
                """INSERT INTO user_stats (key, platform, chat_id, data, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at""",
                (pkey, platform, chat_id, data, _now()),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# RESULT CACHE (replaces result_cache dict)
# ═══════════════════════════════════════════════════════════════════════════════

CACHE_TTL = 3600  # 1 hour default


def cache_set(key: str, data: list[dict], ttl: int = CACHE_TTL) -> None:
    """Store search results in cache."""
    expires = _ttl(ttl)
    now = _now()
    jdata = json.dumps(data)

    with _get_conn() as conn:
        conn.execute(
            """INSERT INTO result_cache (key, data, expires_at, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET data=excluded.data, expires_at=excluded.expires_at""",
            (key, jdata, expires, now),
        )
        conn.commit()

    r = get_redis()
    if r:
        r.setex(f"{_settings.REDIS_KEY_PREFIX}rcache:{key}", ttl, jdata)


def cache_get(key: str) -> list[dict]:
    """Get search results from cache."""
    r = get_redis()
    if r:
        cached = r.get(f"{_settings.REDIS_KEY_PREFIX}rcache:{key}")
        if cached:
            return json.loads(cached)

    with _get_conn() as conn:
        row = conn.execute(
            "SELECT data FROM result_cache WHERE key = ? AND expires_at > ?",
            (key, _now()),
        ).fetchone()
        if row:
            return json.loads(row["data"])
    return []


def cache_delete(key: str) -> None:
    """Delete cache entry."""
    with _get_conn() as conn:
        conn.execute("DELETE FROM result_cache WHERE key = ?", (key,))
        conn.commit()

    r = get_redis()
    if r:
        r.delete(f"{_settings.REDIS_KEY_PREFIX}rcache:{key}")


# ═════════════════════════════════════════════════════════════════════════════════
# URL CACHE (replaces url_cache dict)
# ═══════════════════════════════════════════════════════════════════════════════

URL_CACHE_TTL = 86400  # 24 hours


def store_url(key: str, url: str, chat_id: Optional[int] = None, ttl: int = URL_CACHE_TTL) -> None:
    """Store a URL with a short key for callback buttons."""
    expires = _ttl(ttl)
    now = _now()

    with _get_conn() as conn:
        conn.execute(
            """INSERT INTO url_cache (key, url, chat_id, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET url=excluded.url, expires_at=excluded.expires_at, chat_id=excluded.chat_id""",
            (key, url, chat_id, expires, now),
        )
        conn.commit()

    r = get_redis()
    if r:
        r.setex(f"{_settings.REDIS_KEY_PREFIX}url:{key}", ttl, json.dumps({"url": url, "chat_id": chat_id}))


def get_url(key: str) -> Optional[str]:
    """Get URL by key."""
    r = get_redis()
    if r:
        cached = r.get(f"{_settings.REDIS_KEY_PREFIX}url:{key}")
        if cached:
            return json.loads(cached).get("url")

    with _get_conn() as conn:
        row = conn.execute(
            "SELECT url FROM url_cache WHERE key = ? AND expires_at > ?",
            (key, _now()),
        ).fetchone()
        if row:
            return row["url"]
    return None


def get_url_chat(key: str) -> Optional[int]:
    """Get chat_id associated with URL key."""
    r = get_redis()
    if r:
        cached = r.get(f"{_settings.REDIS_KEY_PREFIX}url:{key}")
        if cached:
            return json.loads(cached).get("chat_id")

    with _get_conn() as conn:
        row = conn.execute(
            "SELECT chat_id FROM url_cache WHERE key = ? AND expires_at > ?",
            (key, _now()),
        ).fetchone()
        if row:
            return row["chat_id"]
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# SUBSCRIPTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def subscribe(platform: str, chat_id: int, topic: str, config: Optional[dict] = None) -> None:
    """Subscribe user to one RSS/topic feed with bounded metadata."""
    topic = str(topic or "").strip()
    if not topic:
        return
    now = _now()
    config = dict(config or {})
    config.setdefault("interval_seconds", 900)
    config.setdefault("next_check_at", now)
    config.setdefault("seen", [])
    jconfig = json.dumps(config, ensure_ascii=False, separators=(",", ":"))

    with _get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM subscriptions WHERE platform=? AND chat_id=? AND active=1 AND topic<>?",
                (platform, chat_id, topic),
            ).fetchone()[0]
            if count >= RSS_MAX_SUBS_PER_USER:
                conn.rollback()
                raise ValueError("RSS_MAX_SUBS_PER_USER")
            total = conn.execute(
                "SELECT COUNT(*) FROM subscriptions WHERE active=1 AND topic<>?"
            , (topic,)).fetchone()[0]
            if total >= RSS_MAX_SUBS_TOTAL:
                conn.rollback()
                raise ValueError("RSS_MAX_SUBS_TOTAL")
            conn.execute(
                """DELETE FROM subscriptions
                   WHERE active=0 AND updated_at < ?""",
                (now - 30 * 86400,),
            )
            conn.execute(
                """INSERT INTO subscriptions (platform, chat_id, topic, config, active, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 1, ?, ?)
                   ON CONFLICT(platform, chat_id, topic) DO UPDATE SET active=1,
                     config=excluded.config, updated_at=excluded.updated_at""",
                (platform, chat_id, topic, jconfig, now, now),
            )
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise


def update_subscription_config(
    platform: str, chat_id: int, topic: str, config: Optional[dict] = None
) -> bool:
    """Replace bounded scheduler metadata for one owned subscription."""
    if not isinstance(config, dict):
        return False
    config = dict(config)
    config.setdefault("interval_seconds", 900)
    config.setdefault("seen", [])
    if not isinstance(config.get("seen"), list):
        config["seen"] = []
    config["seen"] = [str(x)[:300] for x in config["seen"][:30]]
    try:
        config["interval_seconds"] = max(300, min(int(config["interval_seconds"]), 86400))
    except (TypeError, ValueError):
        config["interval_seconds"] = 900
    with _get_conn() as conn:
        cur = conn.execute(
            """UPDATE subscriptions SET config=?, updated_at=?
               WHERE platform=? AND chat_id=? AND topic=? AND active=1""",
            (json.dumps(config, ensure_ascii=False, separators=(",", ":")), _now(), platform, chat_id, topic),
        )
        conn.commit()
    return bool(cur.rowcount)


def unsubscribe(platform: str, chat_id: int, topic: str) -> None:
    """Unsubscribe user from a topic."""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE subscriptions SET active = 0, updated_at = ? WHERE platform = ? AND chat_id = ? AND topic = ?",
            (_now(), platform, chat_id, topic),
        )
        conn.commit()


def delete_subscription(platform: str, chat_id: int, topic: str) -> bool:
    """Remove one owned subscription instead of retaining an inactive row."""
    with _get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM subscriptions WHERE platform=? AND chat_id=? AND topic=? AND active=0",
            (platform, chat_id, topic),
        )
        conn.commit()
    return bool(cur.rowcount)


def get_subscriptions(
    platform: str, chat_id: int, limit: int = RSS_MAX_SUBS_PER_USER, offset: int = 0
) -> list[dict]:
    """Get a bounded page of a user's active subscriptions."""
    limit = max(1, min(int(limit), RSS_MAX_SUBS_PER_USER))
    offset = max(0, min(int(offset), RSS_MAX_SUBS_PER_USER - 1))
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT topic, config FROM subscriptions "
            "WHERE platform = ? AND chat_id = ? AND active = 1 "
            "ORDER BY id LIMIT ? OFFSET ?",
            (platform, chat_id, limit, offset),
        ).fetchall()
    return [
        {"topic": r["topic"], "config": json.loads(r["config"]) if r["config"] else None}
        for r in rows
    ]


def get_all_subscriptions() -> list[dict]:
    """Get all active subscriptions."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT platform, chat_id, topic, config FROM subscriptions WHERE active = 1"
        ).fetchall()
        return [_row_dict(r) for r in rows]


def get_due_subscriptions(now: Optional[int] = None, limit: int = 20) -> list[dict]:
    """Return a bounded snapshot of subscriptions whose check time is due."""
    now = _now() if now is None else int(now)
    limit = max(1, min(int(limit), 50))
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, platform, chat_id, topic, config
            FROM subscriptions
            WHERE active=1
              AND json_extract(
                    COALESCE(config, '{}'), '$.next_check_at'
                  ) <= ?
            ORDER BY updated_at, id
            LIMIT ?
            """,
            (now, limit),
        ).fetchall()
    return [_row_dict(r) for r in rows]


def claim_subscription_delivery(platform: str, chat_id: int, now: Optional[int] = None) -> bool:
    """Atomically claim one subscription delivery under the user's tier quota."""
    now = _now() if now is None else now
    tier = get_user_tier(platform, chat_id)
    if tier == "dev":
        return True
    limit, window = (5, time.strftime("%Y%m%d%H", time.gmtime(now))) if tier == "pro" else (1, time.strftime("%Y%m%d", time.gmtime(now)))
    with _get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT count FROM subscription_deliveries WHERE platform=? AND chat_id=? AND window=?",
                (platform, chat_id, window),
            ).fetchone()
            count = row["count"] if row else 0
            if count >= limit:
                conn.rollback()
                return False
            conn.execute(
                "INSERT INTO subscription_deliveries(platform, chat_id, window, count) VALUES (?, ?, ?, 1) "
                "ON CONFLICT(platform, chat_id, window) DO UPDATE SET count=count+1",
                (platform, chat_id, window),
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise


def release_subscription_delivery(platform: str, chat_id: int, now: Optional[int] = None) -> None:
    """Return one quota claim after an unsuccessful RSS delivery."""
    now = _now() if now is None else int(now)
    tier = get_user_tier(platform, chat_id)
    if tier == "dev":
        return
    window = (
        time.strftime("%Y%m%d%H", time.gmtime(now))
        if tier == "pro"
        else time.strftime("%Y%m%d", time.gmtime(now))
    )
    with _get_conn() as conn:
        conn.execute(
            "UPDATE subscription_deliveries SET count=MAX(0,count-1) "
            "WHERE platform=? AND chat_id=? AND window=?",
            (platform, chat_id, window),
        )
        conn.commit()


# ═══════════════════════════════════════════════════════════════════════════════
# USAGE HISTORY
# ═══════════════════════════════════════════════════════════════════════════════

def log_usage(platform: str, chat_id: int, action: str, details: Optional[dict] = None) -> None:
    """Log compact usage metadata and retain only the newest rows per owner."""
    now = _now()
    details = details or {}
    jdetails = json.dumps(details, ensure_ascii=False, separators=(",", ":"))[:2000]

    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO usage_history (platform, chat_id, action, details, created_at) VALUES (?, ?, ?, ?, ?)",
            (platform, chat_id, str(action)[:80], jdetails, now),
        )
        conn.execute(
            """DELETE FROM usage_history
               WHERE id NOT IN (
                 SELECT id FROM usage_history ORDER BY id DESC LIMIT ?
               )""",
            (USAGE_MAX_TOTAL,),
        )
        conn.execute(
            """DELETE FROM usage_history
               WHERE platform=? AND chat_id=? AND id NOT IN (
                 SELECT id FROM usage_history
                 WHERE platform=? AND chat_id=?
                 ORDER BY id DESC LIMIT ?
               )""",
            (platform, chat_id, platform, chat_id, USAGE_MAX_ITEMS),
        )
        conn.commit()


def get_usage_history(
    platform: str, chat_id: int, limit: int = 20, offset: int = 0
) -> list[dict]:
    limit = max(1, min(int(limit), USAGE_MAX_ITEMS))
    offset = max(0, min(int(offset), USAGE_MAX_ITEMS - 1))
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT id, platform, chat_id, action, details, created_at "
            "FROM usage_history WHERE platform=? AND chat_id=? "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            (platform, chat_id, limit, offset),
        ).fetchall()
    return [_row_dict(r) for r in rows]


def get_usage_entry(platform: str, chat_id: int, entry_id: int) -> Optional[dict]:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT id, platform, chat_id, action, details, created_at "
            "FROM usage_history WHERE id=? AND platform=? AND chat_id=?",
            (int(entry_id), platform, chat_id),
        ).fetchone()
    return _row_dict(row)


# ═══════════════════════════════════════════════════════════════════════════════
# LIBRARY / HISTORY
# ═══════════════════════════════════════════════════════════════════════════════


def _row_dict(row: Optional[sqlite3.Row]) -> Optional[dict]:
    if row is None:
        return None
    out = dict(row)
    if "details" in out:
        try:
            out["details"] = json.loads(out["details"] or "{}")
        except (TypeError, ValueError):
            out["details"] = {}
    if "config" in out:
        try:
            out["config"] = json.loads(out["config"]) if out["config"] else None
        except (TypeError, ValueError):
            out["config"] = None
    if "watchlist" in out:
        out["watchlist"] = bool(out["watchlist"])
    return out


def add_library_item(
    platform: str,
    chat_id: int,
    title: str,
    url: str,
    kind: str = "saved",
    details: Optional[dict] = None,
    watchlist: bool = False,
) -> dict:
    """Upsert one metadata-only library item and enforce a per-user cap."""
    url = str(url or "").strip()
    title = str(title or "بدون عنوان").strip()[:200]
    details = details or {}
    jdetails = json.dumps(details, ensure_ascii=False, separators=(",", ":"))
    now = _now()
    with _get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """INSERT INTO library_items
                   (platform, chat_id, kind, title, url, details, watchlist, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(platform, chat_id, kind, url) DO UPDATE SET
                     title=excluded.title, details=excluded.details,
                     watchlist=MAX(library_items.watchlist, excluded.watchlist),
                     updated_at=excluded.updated_at""",
                (platform, chat_id, kind, title, url, jdetails, int(watchlist), now, now),
            )
            conn.execute(
                """DELETE FROM library_items
                   WHERE id NOT IN (
                     SELECT id FROM library_items ORDER BY updated_at DESC, id DESC LIMIT ?
                   )""",
                (LIBRARY_MAX_TOTAL,),
            )
            conn.execute(
                """DELETE FROM library_items
                   WHERE platform=? AND chat_id=? AND id NOT IN (
                     SELECT id FROM library_items
                     WHERE platform=? AND chat_id=?
                     ORDER BY updated_at DESC, id DESC LIMIT ?
                   )""",
                (platform, chat_id, platform, chat_id, LIBRARY_MAX_ITEMS),
            )
            row = conn.execute(
                """SELECT * FROM library_items
                   WHERE platform=? AND chat_id=? AND kind=? AND url=?""",
                (platform, chat_id, kind, url),
            ).fetchone()
            conn.commit()
            return _row_dict(row) or {}
        except Exception:
            conn.rollback()
            raise


def list_library_items(
    platform: str, chat_id: int, limit: int = 20,
    kind: Optional[str] = None, watchlist_only: bool = False,
    offset: int = 0,
) -> list[dict]:
    limit = max(1, min(int(limit), LIBRARY_MAX_ITEMS))
    offset = max(0, min(int(offset), LIBRARY_MAX_ITEMS - 1))
    clauses = ["platform=?", "chat_id=?"]
    params: list[Any] = [platform, chat_id]
    if kind:
        clauses.append("kind=?")
        params.append(kind)
    if watchlist_only:
        clauses.append("watchlist=1")
    params.extend((limit, offset))
    with _get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM library_items WHERE {' AND '.join(clauses)} "
            "ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?", params
        ).fetchall()
    return [_row_dict(r) for r in rows]


def get_library_item(platform: str, chat_id: int, item_id: int) -> Optional[dict]:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM library_items WHERE id=? AND platform=? AND chat_id=?",
            (int(item_id), platform, chat_id),
        ).fetchone()
    return _row_dict(row)


def toggle_watchlist(
    platform: str, chat_id: int, item_id: int, value: Optional[bool] = None
) -> Optional[dict]:
    with _get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM library_items WHERE id=? AND platform=? AND chat_id=?",
                (int(item_id), platform, chat_id),
            ).fetchone()
            if not row:
                conn.rollback()
                return None
            new_value = (not bool(row["watchlist"])) if value is None else int(bool(value))
            conn.execute(
                "UPDATE library_items SET watchlist=?, updated_at=? WHERE id=? AND platform=? AND chat_id=?",
                (new_value, _now(), int(item_id), platform, chat_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM library_items WHERE id=? AND platform=? AND chat_id=?",
                (int(item_id), platform, chat_id),
            ).fetchone()
            return _row_dict(row)
        except Exception:
            conn.rollback()
            raise


def remove_library_item(platform: str, chat_id: int, item_id: int) -> bool:
    with _get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM library_items WHERE id=? AND platform=? AND chat_id=?",
            (int(item_id), platform, chat_id),
        )
        conn.commit()
    return bool(cur.rowcount)


# ═══════════════════════════════════════════════════════════════════════════════
# DISTRIBUTED LOCKS
# ═══════════════════════════════════════════════════════════════════════════════

def acquire_lock(key: str, owner: str, ttl: int = 30) -> bool:
    """Acquire a distributed lock. Returns True if acquired."""
    expires = _ttl(ttl)
    now = _now()

    # Try Redis first
    r = get_redis()
    if r:
        acquired = r.set(f"{_settings.REDIS_KEY_PREFIX}lock:{key}", owner, nx=True, ex=ttl)
        if acquired:
            return True

    # Fallback to SQLite
    with _get_conn() as conn:
        try:
            conn.execute(
                """INSERT INTO distributed_locks (key, owner, expires_at, created_at)
                   VALUES (?, ?, ?, ?)""",
                (key, owner, expires, now),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            # Lock exists, check if expired
            row = conn.execute(
                "SELECT expires_at FROM distributed_locks WHERE key = ?", (key,)
            ).fetchone()
            if row and row["expires_at"] < now:
                conn.execute(
                    "UPDATE distributed_locks SET owner = ?, expires_at = ?, created_at = ? WHERE key = ?",
                    (owner, expires, now, key),
                )
                conn.commit()
                return True
            return False


def release_lock(key: str, owner: str) -> bool:
    """Release a distributed lock. Returns True if released."""
    r = get_redis()
    if r:
        # Lua script for atomic check-and-delete
        lua = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"
        script = r.register_script(lua)
        result = script(keys=[f"{_settings.REDIS_KEY_PREFIX}lock:{key}"], args=[owner])
        if result:
            return True

    # Fallback to SQLite
    with _get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM distributed_locks WHERE key = ? AND owner = ?",
            (key, owner),
        )
        conn.commit()
        return cur.rowcount > 0
