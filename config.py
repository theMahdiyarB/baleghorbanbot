# bale_bot/config.py
# Pydantic settings for type-safe configuration
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parent / ".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # Required
    BALE_TOKEN: str = Field(..., description="Bale bot token from @botfather")
    TELEGRAM_TOKEN: str = Field(default="", description="Telegram bot token from @botfather")

    # Optional - Platform
    TELEGRAM_API_BASE: str = Field(
        default="https://api.telegram.org",
        description="Local Bot API server for large uploads (http://127.0.0.1:8081)",
    )
    LOCAL_BOT_API: bool = Field(default=False, description="Auto-detected from TELEGRAM_API_BASE")

    # Optional - Network / Proxy
    WARP_PROXY: str = Field(
        default="",
        description="WARP SOCKS5 proxy for bypassing DNS blocks (socks5h://127.0.0.1:40000)",
    )
    MAX_CONCURRENT_DOWNLOADS: int = Field(default=6, ge=1, le=50)

    # Optional - API Keys
    GOOGLE_CX: str = Field(default="", description="Google Custom Search CX")
    GOOGLE_API_KEY: str = Field(default="", description="Google Custom Search API Key")
    PEXELS_API_KEY: str = Field(default="", description="Pexels API Key")
    DEEZER_ARL: str = Field(default="", description="Deezer ARL cookie")
    SPOTIFY_CLIENT_ID: str = Field(default="", description="Spotify Client ID")
    SPOTIFY_CLIENT_SECRET: str = Field(default="", description="Spotify Client Secret")
    YOUTUBE_COOKIES_FILE: str = Field(default="", description="yt-dlp cookies file path")

    # Optional - Nitter / Invidious instances
    NITTER_INSTANCES: list[str] = Field(
        default_factory=lambda: ["https://nitter.net", "https://nitter.unixfox.eu", "https://nitter.snopyta.org"],
        description="Nitter instances",
    )
    INVIDIOUS_INSTANCES: list[str] = Field(
        default_factory=lambda: ["https://yewtu.be", "https://invidious.snopyta.org", "https://vid.puffyan.us"],
        description="Invidious instances",
    )

    # Optional - Redis for multi-instance scaling
    REDIS_URL: str = Field(default="", description="Redis URL (redis://localhost:6379/0) for multi-instance")
    REDIS_KEY_PREFIX: str = Field(default="balebot:", description="Redis key prefix")

    # Optional - Database
    DB_PATH: str = Field(default="bot_data.sqlite3", description="SQLite database path")
    DB_POOL_SIZE: int = Field(default=5, ge=1, le=20)

    # Optional - Health / Monitoring
    HEALTH_PORT: int = Field(default=8080, ge=1, le=65535)
    STARTUP_MIRROR_CHECK: bool = Field(default=True, description="Check mirrors at startup")

    # Dev users (unlimited access)
    DEV_USER_IDS: str = Field(
        default="",
        description="Comma-separated user IDs with dev tier (unlimited access). Set in .env; never commit real IDs.",
    )

    # Optional - Movie/TV APIs
    TMDB_API_KEY: str = Field(default="", description="TMDb API key from themoviedb.org")
    OMDB_API_KEY: str = Field(default="", description="OMDb API key from omdbapi.com")
    OPENSUBTITLES_API_KEY: str = Field(default="", description="OpenSubtitles API key")
    SUBSOURCE_API_KEY: str = Field(default="", description="SubSource API key from subsource.net")
    BAZARR_URL: str = Field(default="", description="Bazarr URL (e.g. http://localhost:6767)")
    BAZARR_API_KEY: str = Field(default="", description="Bazarr API key")

    # Optional - GitHub
    GITHUB_TOKEN: str = Field(default="", description="GitHub token for API access")

    # Optional - Paper search APIs (paper-search-mcp)
    UNPAYWALL_EMAIL: str = Field(default="", description="Email for Unpaywall API (required for paper downloads)")
    SEMANTIC_SCHOLAR_API_KEY: str = Field(default="", description="Semantic Scholar API key for higher rate limits")
    CORE_API_KEY: str = Field(default="", description="CORE API key for paper search")
    DOAJ_API_KEY: str = Field(default="", description="DOAJ API key for higher rate limits")
    ZENODO_ACCESS_TOKEN: str = Field(default="", description="Zenodo access token for private records")
    IEEE_API_KEY: str = Field(default="", description="IEEE Xplore API key")
    ACM_API_KEY: str = Field(default="", description="ACM Digital Library API key")
    GOOGLE_SCHOLAR_PROXY_URL: str = Field(default="", description="Proxy URL for Google Scholar bot bypass")
    OPENAIRE_API_KEY: str = Field(default="", description="OpenAIRE API key")

    # Optional - Telegram client
    TG_API_ID: str = Field(default="", description="Telegram API ID from my.telegram.org")
    TG_API_HASH: str = Field(default="", description="Telegram API Hash from my.telegram.org")
    TG_SESSION_FILE: str = Field(default="tg_session", description="Telegram session file path")

    # Optional - Social media cookies
    TWITTER_COOKIES_FILE: str = Field(default="", description="Twitter cookies file path")
    INSTAGRAM_USER: str = Field(default="", description="Instagram username")
    INSTAGRAM_PASS: str = Field(default="", description="Instagram password")
    INSTAGRAM_COOKIES_FILE: str = Field(default="", description="Instagram cookies file path")

    # Optional - Direct download mirrors
    DDL_MOVIE_HOSTS: str = Field(
        default="https://berlin.saymyname.website",
        description="Comma-separated movie DDL mirror hosts",
    )
    DDL_SERIES_HOSTS: str = Field(
        default="https://rio.ggusers.com,https://nairobi.saymyname.website",
        description="Comma-separated series DDL mirror hosts",
    )

    # Optional - External services
    COBALT_URL: str = Field(default="http://localhost:9000", description="Cobalt API URL")
    VIRUSTOTAL_API_KEY: str = Field(default="", description="VirusTotal API key")
    UNPAYWALL_EMAIL: str = Field(default="dev@balebot.dev", description="Unpaywall email for API")
    PIXABAY_KEY: str = Field(default="", description="Pixabay API key")

    # Optional - Paper Search (paper-search-mcp)
    SEMANTIC_SCHOLAR_API_KEY: str = Field(default="", description="Semantic Scholar API key")
    CORE_API_KEY: str = Field(default="", description="CORE API key")
    OPENAIRE_API_KEY: str = Field(default="", description="OpenAIRE API key")
    DOAJ_API_KEY: str = Field(default="", description="DOAJ API key")
    ZENODO_ACCESS_TOKEN: str = Field(default="", description="Zenodo access token")
    IEEE_API_KEY: str = Field(default="", description="IEEE Xplore API key")
    ACM_API_KEY: str = Field(default="", description="ACM Digital Library API key")

    # Optional - Logging
    LOG_LEVEL: str = Field(default="INFO", pattern="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    LOG_JSON: bool = Field(default=True, description="Structured JSON logging")

    @field_validator("TELEGRAM_API_BASE", mode="before")
    @classmethod
    def strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/") if v else "https://api.telegram.org"

    @field_validator("LOCAL_BOT_API", mode="before")
    @classmethod
    def detect_local_api(cls, v: str | bool, info) -> bool:
        if isinstance(v, bool):
            return v
        api_base = info.data.get("TELEGRAM_API_BASE", "https://api.telegram.org")
        return api_base != "https://api.telegram.org"


@lru_cache
def get_settings() -> Settings:
    return Settings()


# Backwards-compat globals for existing code (will be removed after migration)
_settings = get_settings()

BALE_TOKEN = _settings.BALE_TOKEN
TELEGRAM_TOKEN = _settings.TELEGRAM_TOKEN
TELEGRAM_API_BASE = _settings.TELEGRAM_API_BASE
LOCAL_BOT_API = _settings.LOCAL_BOT_API
WARP_PROXY = _settings.WARP_PROXY
MAX_CONCURRENT_DOWNLOADS = _settings.MAX_CONCURRENT_DOWNLOADS
GOOGLE_CX = _settings.GOOGLE_CX
GOOGLE_API_KEY = _settings.GOOGLE_API_KEY
PEXELS_API_KEY = _settings.PEXELS_API_KEY
DEEZER_ARL = _settings.DEEZER_ARL
SPOTIFY_CLIENT_ID = _settings.SPOTIFY_CLIENT_ID
SPOTIFY_CLIENT_SECRET = _settings.SPOTIFY_CLIENT_SECRET
YOUTUBE_COOKIES_FILE = _settings.YOUTUBE_COOKIES_FILE
NITTER_INSTANCES = _settings.NITTER_INSTANCES
INVIDIOUS_INSTANCES = _settings.INVIDIOUS_INSTANCES
REDIS_URL = _settings.REDIS_URL
REDIS_KEY_PREFIX = _settings.REDIS_KEY_PREFIX
DB_PATH = _settings.DB_PATH
DB_POOL_SIZE = _settings.DB_POOL_SIZE
HEALTH_PORT = _settings.HEALTH_PORT
STARTUP_MIRROR_CHECK = _settings.STARTUP_MIRROR_CHECK
LOG_LEVEL = _settings.LOG_LEVEL
LOG_JSON = _settings.LOG_JSON

GITHUB_TOKEN = _settings.GITHUB_TOKEN
TG_API_ID = _settings.TG_API_ID
TG_API_HASH = _settings.TG_API_HASH
TG_SESSION_FILE = _settings.TG_SESSION_FILE
TWITTER_COOKIES_FILE = _settings.TWITTER_COOKIES_FILE
INSTAGRAM_USER = _settings.INSTAGRAM_USER
INSTAGRAM_PASS = _settings.INSTAGRAM_PASS
INSTAGRAM_COOKIES_FILE = _settings.INSTAGRAM_COOKIES_FILE
COBALT_URL = _settings.COBALT_URL
VIRUSTOTAL_API_KEY = _settings.VIRUSTOTAL_API_KEY
UNPAYWALL_EMAIL = _settings.UNPAYWALL_EMAIL
PIXABAY_KEY = _settings.PIXABAY_KEY
TMDB_API_KEY = _settings.TMDB_API_KEY
OMDB_API_KEY = _settings.OMDB_API_KEY
OPENSUBTITLES_API_KEY = _settings.OPENSUBTITLES_API_KEY
SUBSOURCE_API_KEY = _settings.SUBSOURCE_API_KEY
BAZARR_URL = _settings.BAZARR_URL
BAZARR_API_KEY = _settings.BAZARR_API_KEY
SEMANTIC_SCHOLAR_API_KEY = _settings.SEMANTIC_SCHOLAR_API_KEY
CORE_API_KEY = _settings.CORE_API_KEY
OPENAIRE_API_KEY = _settings.OPENAIRE_API_KEY
DOAJ_API_KEY = _settings.DOAJ_API_KEY
ZENODO_ACCESS_TOKEN = _settings.ZENODO_ACCESS_TOKEN
IEEE_API_KEY = _settings.IEEE_API_KEY
ACM_API_KEY = _settings.ACM_API_KEY
GOOGLE_SCHOLAR_PROXY_URL = _settings.GOOGLE_SCHOLAR_PROXY_URL