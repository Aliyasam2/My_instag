#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SuperLiveRec Personal Unlimited Pro (v2.0)
==========================================
Personal, unlimited SuperLive livestream recorder & watchlist bot for Telegram,
inspired by @superliverec_bot and StreamRecorder.io.

Key Features:
- Unlimited personal watchlist & simultaneous multi-stream recording
- Telethon MTProto engine: uploads full continuous MP4 files up to 2 GB (no 50 MB Bot API cap!)
- Dual capture engine:
    1) Direct HLS / FLV / RTMP via FFmpeg (-c copy, zero quality loss)
    2) Native Agora RTC capture via agora-python-server-sdk (H.264 + PCM audio muxed to AAC MP4)
- Automatic 4x3 Contact Sheet preview grid & video cover thumbnail (StreamRecorder-style)
- Live dashboard, active recording monitor, searchable SQLite archive, and optional Archive Channel backup
- Disk space guard tailored for VPS / Altare containers (e.g., 6 GiB RAM / 10 GiB Disk)
"""

import asyncio
import datetime
import html
import inspect
import json
import logging
import os
import random
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from aiohttp import web
from telethon import Button, TelegramClient, events
from telethon.errors import FloodWaitError, MessageNotModifiedError
from telethon.tl.types import DocumentAttributeVideo

# ==============================================================================
# CONFIGURATION (Override via Environment Variables on Altare / VPS)
# ==============================================================================

BOT_TOKEN = os.environ.get(
    "BOT_TOKEN",
    "8837565058:AAFDtM7wJRI2AOgV67dPDdK-206STorQ1Ew",
).strip()

# Telethon MTProto credentials (needed for 2 GB uploads without 50 MB Bot API limit)
# You can set your own from https://my.telegram.org via TG_API_ID / TG_API_HASH env vars
TG_API_ID = int(os.environ.get("TG_API_ID", "2040"))
TG_API_HASH = os.environ.get("TG_API_HASH", "b18441a1ff607e10a989891a5462e627").strip()

# Optional: lock bot to your Telegram numeric user ID (0 = auto-bind to first /start user)
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))

# SuperLive Web API & Agora defaults
SUPERLIVE_API_BASE = os.environ.get("SUPERLIVE_API_BASE", "https://api.spl-live.com/api/web").rstrip("/")
DEFAULT_AGORA_APP_ID = os.environ.get("AGORA_APP_ID", "dca16fbf522b4c6f96ecc88721800310").strip()

# Storage & Limits (tuned for 6 GiB RAM / 10 GiB Disk)
BASE_DIR = Path(os.environ.get("DATA_DIR", "./superliverec_data")).resolve()
RECORDINGS_DIR = BASE_DIR / "recordings"
DB_PATH = BASE_DIR / "superliverec.db"
SESSION_PATH = BASE_DIR / "superliverec_bot_session"

BASE_DIR.mkdir(parents=True, exist_ok=True)
RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)

# Max single file size before rolling over to a new continuous part (Telegram MTProto bot limit = 2000 MB)
MAX_SINGLE_FILE_BYTES = int(os.environ.get("MAX_FILE_MB", "1900")) * 1024 * 1024
# Minimum free disk space safeguard (MB) so 10 GiB container never hits ENOSPC
MIN_FREE_DISK_MB = int(os.environ.get("MIN_FREE_DISK_MB", "500"))
# Inactivity timeout for Agora stream end detection (seconds without frames)
AGORA_INACTIVITY_TIMEOUT = int(os.environ.get("AGORA_INACTIVITY_TIMEOUT", "45"))
# Watchlist check interval (seconds)
DEFAULT_POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "25"))
# Max simultaneous recordings safeguard (0 = truly unlimited, default 8 for 6 GiB RAM / 10 GiB Disk)
DEFAULT_MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "8"))

# ==============================================================================
# LOGGING
# ==============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(BASE_DIR / "bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("SuperLiveRec")

# ==============================================================================
# OPTIONAL DEPENDENCY DETECTION (FFmpeg & Agora Server SDK)
# ==============================================================================

def find_ffmpeg() -> Optional[str]:
    """Locate system ffmpeg or bundled imageio-ffmpeg binary."""
    sys_bin = shutil.which("ffmpeg")
    if sys_bin:
        return sys_bin
    try:
        import imageio_ffmpeg  # type: ignore
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def find_ffprobe() -> Optional[str]:
    return shutil.which("ffprobe")


FFMPEG_BIN = find_ffmpeg()
FFPROBE_BIN = find_ffprobe()

AGORA_AVAILABLE = False
AGORA_IMPORT_ERROR = ""
try:
    from agora.rtc.agora_service import AgoraService, AgoraServiceConfig, RTCConnConfig  # type: ignore
    from agora.rtc.agora_base import (  # type: ignore
        AudioScenarioType,
        ChannelProfileType,
        ClientRoleType,
        VideoStreamType,
        VideoSubscriptionOptions,
    )
    from agora.rtc.audio_frame_observer import IAudioFrameObserver  # type: ignore
    try:
        from agora.rtc.rtc_connection_observer import IRTCConnectionObserver  # type: ignore
    except ImportError:
        IRTCConnectionObserver = object  # type: ignore

    try:
        from agora.rtc.video_encoded_frame_observer import IVideoEncodedFrameObserver  # type: ignore
    except ImportError:
        from agora.rtc.video_frame_observer import IVideoEncodedFrameObserver  # type: ignore

    AGORA_AVAILABLE = True
except Exception as _ag_err:
    AGORA_IMPORT_ERROR = str(_ag_err)
    IAudioFrameObserver = object  # type: ignore
    IVideoEncodedFrameObserver = object  # type: ignore
    IRTCConnectionObserver = object  # type: ignore


# ==============================================================================
# HELPER UTILITIES
# ==============================================================================

def fmt_bytes(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} PB"


def fmt_duration(seconds: float) -> str:
    secs = max(0, int(seconds))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def get_disk_stats(path: Path = BASE_DIR) -> Tuple[float, float, float]:
    """Return (used_mb, free_mb, total_mb)."""
    usage = shutil.disk_usage(str(path))
    mb = 1024 * 1024
    return usage.used / mb, usage.free / mb, usage.total / mb


def get_ram_usage_mb() -> Tuple[float, float]:
    """Read container/process RAM usage on Linux without external deps."""
    used_mb, total_mb = 0.0, 0.0
    try:
        # Try cgroup v2 first (Docker / Altare containers)
        cg_cur = Path("/sys/fs/cgroup/memory.current")
        cg_max = Path("/sys/fs/cgroup/memory.max")
        if cg_cur.exists() and cg_max.exists():
            used_mb = int(cg_cur.read_text().strip()) / (1024 * 1024)
            raw_max = cg_max.read_text().strip()
            if raw_max.isdigit():
                total_mb = int(raw_max) / (1024 * 1024)
                return used_mb, total_mb
    except Exception:
        pass
    try:
        meminfo = Path("/proc/meminfo").read_text()
        vals: Dict[str, int] = {}
        for line in meminfo.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                vals[parts[0].rstrip(":")] = int(parts[1])
        total_kb = vals.get("MemTotal", 0)
        avail_kb = vals.get("MemAvailable", vals.get("MemFree", 0))
        total_mb = total_kb / 1024.0
        used_mb = (total_kb - avail_kb) / 1024.0
    except Exception:
        pass
    return used_mb, total_mb


def extract_creator_identifier(raw_input: str) -> str:
    """Extract numeric ID or username from a SuperLive ID, @handle, or share URL."""
    text = raw_input.strip()
    if not text:
        return ""
    # Extract URL if pasted inside a sentence
    url_match = re.search(r"https?://[^\s]+", text)
    if url_match:
        url = url_match.group(0)
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        for key in ("user_id", "uid", "id", "creator_id", "host_id", "profile_id"):
            if key in qs and qs[key]:
                return qs[key][0].strip()
        # Check path segments for numeric ID or username
        segments = [seg for seg in parsed.path.split("/") if seg]
        for seg in reversed(segments):
            if seg.isdigit():
                return seg
        if segments:
            return segments[-1].lstrip("@").strip()
    # Direct numeric ID or handle
    num_match = re.fullmatch(r"\d{4,16}", text)
    if num_match:
        return text
    return text.lstrip("@").strip()


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r"[^\w\-.]+", "_", name.strip())
    return cleaned[:48] or "creator"


# ==============================================================================
# DATABASE LAYER (SQLite — Watchlist, Archive, Settings, SuperLive Session)
# ==============================================================================

class Database:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS watchlist (
                    creator_id TEXT PRIMARY KEY,
                    username TEXT DEFAULT '',
                    display_name TEXT DEFAULT '',
                    avatar_url TEXT DEFAULT '',
                    auto_record INTEGER DEFAULT 1,
                    notify_live INTEGER DEFAULT 1,
                    last_status TEXT DEFAULT 'offline',
                    last_seen_live TEXT DEFAULT '',
                    total_recordings INTEGER DEFAULT 0,
                    added_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS recordings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    creator_id TEXT NOT NULL,
                    creator_name TEXT DEFAULT '',
                    livestream_id TEXT DEFAULT '',
                    started_at TEXT NOT NULL,
                    ended_at TEXT DEFAULT '',
                    duration_sec INTEGER DEFAULT 0,
                    file_size_bytes INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'recording',
                    tg_chat_id INTEGER DEFAULT 0,
                    tg_message_id INTEGER DEFAULT 0,
                    archive_chat_id INTEGER DEFAULT 0,
                    archive_message_id INTEGER DEFAULT 0,
                    notes TEXT DEFAULT ''
                );
                """
            )

    # --- Settings ---
    def get_setting(self, key: str, default: str = "") -> str:
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

    def get_owner_id(self) -> int:
        if OWNER_ID > 0:
            return OWNER_ID
        val = self.get_setting("owner_id", "0")
        return int(val) if val.isdigit() else 0

    def set_owner_id(self, uid: int) -> None:
        self.set_setting("owner_id", str(uid))

    # --- Watchlist ---
    def add_watch(
        self,
        creator_id: str,
        username: str = "",
        display_name: str = "",
        avatar_url: str = "",
    ) -> bool:
        now = datetime.datetime.utcnow().isoformat()
        with self._lock, self._conn() as conn:
            existing = conn.execute(
                "SELECT creator_id FROM watchlist WHERE creator_id = ?", (creator_id,)
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE watchlist
                    SET username = CASE WHEN ? != '' THEN ? ELSE username END,
                        display_name = CASE WHEN ? != '' THEN ? ELSE display_name END,
                        avatar_url = CASE WHEN ? != '' THEN ? ELSE avatar_url END,
                        auto_record = 1
                    WHERE creator_id = ?
                    """,
                    (username, username, display_name, display_name, avatar_url, avatar_url, creator_id),
                )
                return False
            conn.execute(
                """
                INSERT INTO watchlist (creator_id, username, display_name, avatar_url, added_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (creator_id, username, display_name or username or creator_id, avatar_url, now),
            )
            return True

    def remove_watch(self, creator_id: str) -> bool:
        with self._lock, self._conn() as conn:
            cur = conn.execute("DELETE FROM watchlist WHERE creator_id = ?", (creator_id,))
            return cur.rowcount > 0

    def toggle_auto_record(self, creator_id: str) -> Optional[int]:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT auto_record FROM watchlist WHERE creator_id = ?", (creator_id,)
            ).fetchone()
            if not row:
                return None
            new_val = 0 if row["auto_record"] else 1
            conn.execute(
                "UPDATE watchlist SET auto_record = ? WHERE creator_id = ?", (new_val, creator_id)
            )
            return new_val

    def list_watchlist(self) -> List[Dict[str, Any]]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM watchlist ORDER BY (last_status = 'live') DESC, added_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_watch(self, creator_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM watchlist WHERE creator_id = ?", (creator_id,)
            ).fetchone()
            return dict(row) if row else None

    def update_watch_status(
        self,
        creator_id: str,
        status: str,
        display_name: str = "",
        username: str = "",
        increment_recordings: bool = False,
    ) -> None:
        now = datetime.datetime.utcnow().isoformat()
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                UPDATE watchlist
                SET last_status = ?,
                    last_seen_live = CASE WHEN ? = 'live' THEN ? ELSE last_seen_live END,
                    display_name = CASE WHEN ? != '' THEN ? ELSE display_name END,
                    username = CASE WHEN ? != '' THEN ? ELSE username END,
                    total_recordings = total_recordings + ?
                WHERE creator_id = ?
                """,
                (
                    status,
                    status,
                    now,
                    display_name,
                    display_name,
                    username,
                    username,
                    1 if increment_recordings else 0,
                    creator_id,
                ),
            )

    # --- Recordings Archive ---
    def create_recording_record(
        self, creator_id: str, creator_name: str, livestream_id: str = ""
    ) -> int:
        now = datetime.datetime.utcnow().isoformat()
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO recordings (creator_id, creator_name, livestream_id, started_at, status)
                VALUES (?, ?, ?, ?, 'recording')
                """,
                (creator_id, creator_name, livestream_id, now),
            )
            return int(cur.lastrowid)

    def finish_recording_record(
        self,
        rec_id: int,
        status: str,
        duration_sec: int,
        file_size_bytes: int,
        tg_chat_id: int = 0,
        tg_message_id: int = 0,
        archive_chat_id: int = 0,
        archive_message_id: int = 0,
        notes: str = "",
    ) -> None:
        now = datetime.datetime.utcnow().isoformat()
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                UPDATE recordings
                SET ended_at = ?, duration_sec = ?, file_size_bytes = ?, status = ?,
                    tg_chat_id = ?, tg_message_id = ?, archive_chat_id = ?, archive_message_id = ?, notes = ?
                WHERE id = ?
                """,
                (
                    now,
                    duration_sec,
                    file_size_bytes,
                    status,
                    tg_chat_id,
                    tg_message_id,
                    archive_chat_id,
                    archive_message_id,
                    notes,
                    rec_id,
                ),
            )

    def list_recent_recordings(self, limit: int = 15, offset: int = 0) -> List[Dict[str, Any]]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM recordings ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return [dict(r) for r in rows]

    def count_recordings(self) -> int:
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM recordings WHERE status = 'completed'").fetchone()
            return int(row["c"]) if row else 0

    def mark_stale_recordings_interrupted(self) -> int:
        now = datetime.datetime.utcnow().isoformat()
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "UPDATE recordings SET status = 'interrupted', ended_at = ?, notes = 'Server restarted during capture' "
                "WHERE status = 'recording'",
                (now,),
            )
            return cur.rowcount


# ==============================================================================
# SUPERLIVE API CLIENT (Fixed disc_data bug + Robust Stream Discovery)
# ==============================================================================

@dataclass
class CreatorStreamInfo:
    creator_id: str
    username: str = ""
    display_name: str = ""
    avatar_url: str = ""
    is_live: bool = False
    livestream_id: str = ""
    stream_url: str = ""  # Either http(s)/rtmp URL or agora://<app_id>/<channel>?token=...&uid=...
    title: str = ""
    viewers: int = 0
    raw_debug: Dict[str, Any] = field(default_factory=dict)


class SuperLiveClient:
    def __init__(self, db: Database):
        self.db = db
        self._session: Optional[aiohttp.ClientSession] = None
        env_tok = os.environ.get("SUPERLIVE_TOKEN", "").strip()
        env_dev = os.environ.get("SUPERLIVE_DEVICE_ID", "").strip()
        self._token: str = env_tok or db.get_setting("superlive_token", "")
        self._device_id: str = env_dev or db.get_setting("superlive_device_id", "") or uuid.uuid4().hex
        self._auth_scheme: str = db.get_setting("superlive_auth_scheme", "Token")
        self._rtc_id: str = (
            os.environ.get("SUPERLIVE_RTC_ID", "").strip()
            or db.get_setting("superlive_rtc_id", "")
            or str(random.randint(1000000000, 4200000000))
        )
        if not db.get_setting("superlive_device_id", ""):
            db.set_setting("superlive_device_id", self._device_id)
        if not db.get_setting("superlive_rtc_id", ""):
            db.set_setting("superlive_rtc_id", self._rtc_id)

    def _client_params(self, source_url: str = "https://superlive.chat/") -> Dict[str, Any]:
        return {
            "os_type": "web",
            "app_build": "5.25.23",
            "app": "superlive",
            "build_code": "1526-2991979-prod",
            "app_language": "en",
            "device_language": "en",
            "device_preferred_languages": ["en-US", "en"],
            "source_url": source_url,
            "session_source_url": "https://superlive.chat/",
            "referrer": "",
            "incognito": False,
            "rtc_id": self._rtc_id,
            "web_type": 1,
            "display_density": "2.75",
            "display_resolution_width": 393,
            "display_resolution_height": 873,
        }

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=25)
            jar = aiohttp.CookieJar(unsafe=True)
            self._session = aiohttp.ClientSession(timeout=timeout, cookie_jar=jar)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _headers(self) -> Dict[str, str]:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36"
            ),
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
            "Device-Id": self._device_id,
            "Origin": "https://superlive.chat",
            "Referer": "https://superlive.chat/",
        }
        if self._token:
            headers["Authorization"] = f"{self._auth_scheme} {self._token}"
        return headers

    def set_credentials(self, token: str, device_id: str = "") -> None:
        self._token = token.strip()
        self.db.set_setting("superlive_token", self._token)
        if device_id.strip():
            self._device_id = device_id.strip()
            self.db.set_setting("superlive_device_id", self._device_id)

    async def _post(self, endpoint: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{SUPERLIVE_API_BASE}/{endpoint.lstrip('/')}"
        body = dict(payload or {})
        if "client_params" not in body:
            body["client_params"] = self._client_params()
        sess = await self._get_session()
        try:
            async with sess.post(url, json=body, headers=self._headers()) as resp:
                text = await resp.text()
                try:
                    data = json.loads(text)
                    if isinstance(data, dict):
                        data.setdefault("_http_status", resp.status)
                        return data
                    return {"_http_status": resp.status, "data": data}
                except Exception:
                    return {"_http_status": resp.status, "_raw": text[:600]}
        except Exception as e:
            return {"_http_status": 0, "_error": str(e)}

    async def _get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{SUPERLIVE_API_BASE}/{endpoint.lstrip('/')}"
        sess = await self._get_session()
        try:
            async with sess.get(url, params=params or {}, headers=self._headers()) as resp:
                text = await resp.text()
                try:
                    data = json.loads(text)
                    if isinstance(data, dict):
                        data.setdefault("_http_status", resp.status)
                        return data
                    return {"_http_status": resp.status, "data": data}
                except Exception:
                    return {"_http_status": resp.status, "_raw": text[:600]}
        except Exception as e:
            return {"_http_status": 0, "_error": str(e)}

    async def _request_smart(
        self, endpoint: str, payload: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Try POST JSON first; auto-switch Token/Bearer on 401/403 and GET on 404/405."""
        res = await self._post(endpoint, payload)
        status = res.get("_http_status", 0)
        if status in (401, 403) and self._token:
            alt_scheme = "Bearer" if self._auth_scheme == "Token" else "Token"
            prev_scheme = self._auth_scheme
            self._auth_scheme = alt_scheme
            retry = await self._post(endpoint, payload)
            if retry.get("_http_status", 0) in (200, 201):
                self.db.set_setting("superlive_auth_scheme", alt_scheme)
                return retry
            self._auth_scheme = prev_scheme
        if status in (404, 405):
            get_res = await self._get(endpoint, payload)
            if get_res.get("_http_status", 0) in (200, 201):
                return get_res
        return res

    @staticmethod
    def _deep_find_keys(obj: Any, target_keys: Tuple[str, ...]) -> Dict[str, Any]:
        """Recursively collect first non-empty values for any matching keys."""
        found: Dict[str, Any] = {}
        stack: List[Any] = [obj]
        seen = 0
        while stack and seen < 400:
            cur = stack.pop()
            seen += 1
            if isinstance(cur, dict):
                for k, v in cur.items():
                    kl = str(k).lower()
                    if kl in target_keys and kl not in found and v not in (None, "", [], {}):
                        found[kl] = v
                    if isinstance(v, (dict, list)):
                        stack.append(v)
            elif isinstance(cur, list):
                for item in cur[:50]:
                    if isinstance(item, (dict, list)):
                        stack.append(item)
        return found

    async def ensure_initialized(self, force: bool = False) -> Dict[str, Any]:
        if self._token and not force:
            return {"_cached": True}
        data = await self._request_smart(
            "initialize",
            {"device_id": self._device_id, "platform": "web", "language": "en"},
        )
        found = self._deep_find_keys(data, ("token", "access_token", "auth_token", "jwt", "session_token"))
        token = (
            found.get("access_token")
            or found.get("token")
            or found.get("auth_token")
            or found.get("jwt")
            or found.get("session_token")
        )
        if token:
            self._token = str(token)
            self.db.set_setting("superlive_token", self._token)
        return data

    async def email_signin(self, email: str, password: str) -> Tuple[bool, str]:
        data = await self._post(
            "signup/email_signin",
            {"email": email, "password": password, "device_id": self._device_id},
        )
        found = self._deep_find_keys(data, ("token", "access_token", "auth_token", "jwt"))
        token = found.get("access_token") or found.get("token") or found.get("auth_token") or found.get("jwt")
        if token:
            self._token = str(token)
            self.db.set_setting("superlive_token", self._token)
            return True, "ورود به اکانت SuperLive با موفقیت انجام شد."
        err = data.get("message") or data.get("error") or data.get("_error") or f"HTTP {data.get('_http_status')}"
        return False, f"خطا در ورود: {err}"

    @staticmethod
    def _extract_avatar(user_obj: Dict[str, Any]) -> str:
        img = user_obj.get("profile_image") or user_obj.get("avatar")
        if isinstance(img, dict):
            return str(img.get("url") or img.get("thumbnail_url") or "")
        return str(img or "")

    async def _search_by_shared_id(self, ident: str) -> Dict[str, Any]:
        """Resolve a SuperLive public shared_id (e.g. 59534891) into the full user object with real user_id."""
        for payload in (
            {"shared_id": str(ident)},
            {"search_text": str(ident)},
            {"query": str(ident), "keyword": str(ident)},
        ):
            res = await self._request_smart("user/profile_share_search", payload)
            if res.get("_http_status") not in (200, 201):
                continue
            u = self._extract_user_dict(res)
            if u and (u.get("user_id") or u.get("id")):
                return u
            for list_key in ("users", "items", "results", "data"):
                arr = res.get(list_key)
                if isinstance(arr, list):
                    for cand in arr:
                        if not isinstance(cand, dict):
                            continue
                        sub = cand.get("user") if isinstance(cand.get("user"), dict) else cand
                        if str(sub.get("shared_id") or "") == str(ident) or str(sub.get("user_id") or "") == str(ident):
                            return sub
                    if arr and isinstance(arr[0], dict):
                        sub0 = arr[0].get("user") if isinstance(arr[0].get("user"), dict) else arr[0]
                        if sub0.get("user_id") or sub0.get("id"):
                            return sub0
        return {}

    async def resolve_creator(self, raw_query: str) -> Tuple[str, str, str, str]:
        """Resolve any SuperLive user_id, public shared_id, username, or URL into (real_user_id, username, display_name, avatar)."""
        await self.ensure_initialized()
        ident = extract_creator_identifier(raw_query)
        if not ident:
            return "", "", "", ""

        # 1) Try direct users/profile (in case ident is already internal user_id)
        if ident.isdigit():
            prof = await self._request_smart("users/profile", {"user_id": str(ident)})
            user_obj = self._extract_user_dict(prof)
            if user_obj and not prof.get("error"):
                cid = str(user_obj.get("user_id") or user_obj.get("id") or ident)
                uname = str(user_obj.get("username") or user_obj.get("shared_id") or "")
                dname = str(user_obj.get("name") or user_obj.get("nickname") or uname or cid)
                return cid, uname, dname, self._extract_avatar(user_obj)

        # 2) Resolve via profile_share_search (translates public shared_id like 59534891 -> internal user_id)
        found_u = await self._search_by_shared_id(ident)
        if found_u:
            cid = str(found_u.get("user_id") or found_u.get("id") or ident)
            uname = str(found_u.get("username") or found_u.get("shared_id") or "")
            dname = str(found_u.get("name") or found_u.get("nickname") or uname or cid)
            return cid, uname, dname, self._extract_avatar(found_u)

        return ident, "", ident, ""

    @staticmethod
    def _extract_user_dict(payload: Any) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        for key in ("user", "profile", "data", "result"):
            val = payload.get(key)
            if isinstance(val, dict):
                if any(k in val for k in ("id", "user_id", "username", "name", "livestream_id", "livestream", "is_live")):
                    return val
                for sub in ("user", "profile"):
                    if isinstance(val.get(sub), dict):
                        return val[sub]
        if any(k in payload for k in ("id", "user_id", "username", "name", "livestream_id", "livestream", "is_live")):
            return payload
        return {}

    @staticmethod
    def _extract_direct_stream_url(obj: Any) -> str:
        if not isinstance(obj, dict):
            return ""
        skip_parent_keys = {
            "profile_image", "avatar", "thumbnail", "cover", "image",
            "badge", "badges", "gift", "gifts", "icon", "icons", "banner",
        }
        image_markers = (
            "cdn.spl-live.com/pp/", "/pp/", "/avatar", "/thumbnail",
            ".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", "_s", "_b",
        )
        for k in (
            "hls_url",
            "flv_url",
            "stream_url",
            "play_url",
            "rtmp_url",
            "m3u8_url",
            "pull_url",
            "playback_url",
        ):
            v = obj.get(k)
            if isinstance(v, str):
                vl = v.lower()
                if any(m in vl for m in image_markers):
                    continue
                if (
                    vl.startswith("rtmp://")
                    or vl.startswith("rtmps://")
                    or vl.startswith("agora://")
                    or (vl.startswith(("http://", "https://")) and any(ext in vl for ext in (".m3u8", ".flv")))
                ):
                    return v
        for k, v in obj.items():
            if str(k).lower() in skip_parent_keys:
                continue
            if isinstance(v, dict):
                found = SuperLiveClient._extract_direct_stream_url(v)
                if found:
                    return found
        return ""

    def _build_agora_uri(self, payload: Dict[str, Any], ls_id: str = "") -> str:
        """Construct agora://<app_id>/<channel>?token=<tok>&uid=<uid>&ls_id=<ls_id> from API response."""
        flat: Dict[str, Any] = {}
        agora_sig_token = ""
        stack: List[Any] = [payload]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                for k, v in cur.items():
                    if isinstance(v, (dict, list)):
                        stack.append(v)
                    else:
                        if isinstance(v, str) and len(v) >= 50 and v.startswith(("006", "007")) and not agora_sig_token:
                            agora_sig_token = v
                        if k not in flat and v not in (None, ""):
                            flat[k] = v
            elif isinstance(cur, list):
                for item in cur[:50]:
                    if isinstance(item, (dict, list)):
                        stack.append(item)
                    elif isinstance(item, str) and len(item) >= 50 and item.startswith(("006", "007")) and not agora_sig_token:
                        agora_sig_token = item

        channel = (
            flat.get("channel_name")
            or flat.get("channel_id")
            or flat.get("agora_channel")
            or flat.get("agora_channel_name")
            or flat.get("rtc_channel")
            or flat.get("channel")
            or ls_id
        )
        token = (
            agora_sig_token
            or flat.get("agora_token")
            or flat.get("agora_rtc_token")
            or flat.get("rtc_token")
            or flat.get("viewer_token")
            or flat.get("audience_token")
            or flat.get("subscriber_token")
            or flat.get("channel_token")
            or flat.get("stream_token")
            or flat.get("token")
            or ""
        )
        if not token:
            for k, v in flat.items():
                if "token" in str(k).lower() and isinstance(v, str) and len(v) >= 30:
                    token = v
                    break

        uid = (
            flat.get("agora_uid")
            or flat.get("agora_user_id")
            or flat.get("rtc_uid")
            or flat.get("rtc_id")
            or flat.get("uid")
            or self._rtc_id
            or "0"
        )
        app_id = flat.get("agora_app_id") or flat.get("app_id") or DEFAULT_AGORA_APP_ID

        if channel and (token or str(channel) != str(ls_id)):
            qs = urllib.parse.urlencode(
                {
                    "token": str(token),
                    "uid": str(uid),
                    "ls_id": str(ls_id or channel),
                }
            )
            return f"agora://{app_id}/{channel}?{qs}"
        return ""

    async def fetch_discover_map(self, max_pages: int = 4) -> Tuple[Dict[str, Dict[str, Any]], int, str]:
        """Fetch paginated /discover ONCE and index live streams by user_id, shared_id, and username."""
        await self.ensure_initialized()
        live_map: Dict[str, Dict[str, Any]] = {}
        first_http = 0
        first_sample = ""
        cursor: Any = None
        for page_idx in range(max_pages):
            disc_data = await self._request_smart("discover", {"next": cursor, "type": 0})
            if page_idx == 0:
                first_http = int(disc_data.get("_http_status") or 0)
                first_sample = json.dumps(disc_data, ensure_ascii=False)[:700]
            if not isinstance(disc_data, dict):
                break
            streams_list = (
                disc_data.get("items")
                or disc_data.get("livestreams")
                or disc_data.get("streams")
                or (disc_data.get("data") or {}).get("items")
                or (disc_data.get("data") or {}).get("livestreams")
                or (disc_data.get("data") if isinstance(disc_data.get("data"), list) else [])
                or []
            )
            for item in streams_list:
                if not isinstance(item, dict):
                    continue
                host = item.get("user") or item.get("host") or item.get("broadcaster") or item
                for key_val in (
                    host.get("user_id"),
                    host.get("id"),
                    host.get("shared_id"),
                    host.get("short_id"),
                    item.get("user_id"),
                    item.get("shared_id"),
                    str(host.get("username") or "").lower(),
                ):
                    if key_val:
                        live_map[str(key_val)] = item
            cursor = disc_data.get("next")
            if not cursor:
                break
        return live_map, first_http, first_sample

    async def check_creator_and_get_stream(
        self, creator_id: str, discover_map: Optional[Dict[str, Dict[str, Any]]] = None
    ) -> CreatorStreamInfo:
        """
        Check whether a SuperLive creator is currently live and retrieve a playable stream URI.
        Uses recursive field inspection + safe discover fallback + detailed raw_debug for /debug.
        """
        init_res = await self.ensure_initialized()
        info = CreatorStreamInfo(creator_id=str(creator_id))
        info.raw_debug["init_status"] = init_res.get("_http_status", "cached")
        info.raw_debug["has_token"] = bool(self._token)

        target_uid = str(creator_id)
        ls_id = ""
        is_live_flag = False
        prof_resp: Dict[str, Any] = {}
        user_obj: Dict[str, Any] = {}

        # 0) Fast path: check pre-fetched discover_map first (avoids 100x API spam!)
        if discover_map is not None:
            matched_item = discover_map.get(target_uid) or discover_map.get(target_uid.lower())
            if matched_item:
                host = matched_item.get("user") or matched_item.get("host") or matched_item.get("broadcaster") or matched_item
                ls_id = str(host.get("livestream_id") or matched_item.get("livestream_id") or matched_item.get("id") or "")
                real_uid = str(host.get("user_id") or host.get("id") or target_uid)
                info.creator_id = real_uid
                info.username = str(host.get("username") or host.get("shared_id") or "")
                info.display_name = str(host.get("name") or host.get("nickname") or info.username or real_uid)
                info.avatar_url = self._extract_avatar(host)
                info.title = str(matched_item.get("title") or "")
                is_live_flag = bool(ls_id)
                info.raw_debug["discover_cache_hit"] = True

        # 1) Query user profile only if not already matched in discover_map
        prof_resp = await self._request_smart("users/profile", {"user_id": target_uid})
        if prof_resp.get("_http_status") in (401, 403):
            await self.ensure_initialized(force=True)
            prof_resp = await self._request_smart("users/profile", {"user_id": target_uid})

        # If SuperLive says "There is no user found with given id" (code 9), creator_id is a public shared_id!
        err_obj = prof_resp.get("error") if isinstance(prof_resp.get("error"), dict) else {}
        if prof_resp.get("_http_status") == 400 and (err_obj.get("code") == 9 or "no user" in str(err_obj.get("message", "")).lower()):
            resolved_u = await self._search_by_shared_id(target_uid)
            if resolved_u and (resolved_u.get("user_id") or resolved_u.get("id")):
                target_uid = str(resolved_u.get("user_id") or resolved_u.get("id"))
                info.creator_id = target_uid
                info.raw_debug["resolved_from_shared_id"] = f"{creator_id} -> {target_uid}"
                prof_resp = await self._request_smart("users/profile", {"user_id": target_uid})

        info.raw_debug["profile_http"] = prof_resp.get("_http_status")
        info.raw_debug["profile_sample"] = json.dumps(prof_resp, ensure_ascii=False)[:900]

        user_obj = self._extract_user_dict(prof_resp)
        if user_obj:
            info.username = str(user_obj.get("username") or user_obj.get("shared_id") or "")
            info.display_name = str(user_obj.get("name") or user_obj.get("nickname") or info.username or target_uid)
            info.avatar_url = self._extract_avatar(user_obj)

        # Recursive search inside ENTIRE profile response for livestream identifiers
        deep = self._deep_find_keys(
            prof_resp,
            (
                "livestream_id",
                "live_id",
                "stream_id",
                "broadcast_id",
                "room_id",
                "channel_name",
                "agora_channel",
                "is_live",
                "is_streaming",
                "live_status",
                "online_status",
                "livestream",
                "current_livestream",
                "active_livestream",
            ),
        )
        ls_id = str(
            deep.get("livestream_id")
            or deep.get("live_id")
            or deep.get("stream_id")
            or deep.get("broadcast_id")
            or deep.get("room_id")
            or ""
        )
        for nested_key in ("livestream", "current_livestream", "active_livestream"):
            nested = deep.get(nested_key)
            if not ls_id and isinstance(nested, dict):
                ls_id = str(nested.get("id") or nested.get("livestream_id") or nested.get("live_id") or "")

        is_live_flag = bool(
            deep.get("is_live")
            or deep.get("is_streaming")
            or deep.get("live_status") in (1, True, "live", "online", "LIVE", "ONLINE")
            or ls_id
        )

        # 2) Safe discover fallback ONLY when profile didn't expose livestream_id (supports pagination + shared_id!)
        disc_data: Dict[str, Any] = {}
        if not ls_id and discover_map is None:
            cursor: Any = None
            for page_idx in range(4):
                disc_data = await self._request_smart("discover", {"next": cursor, "type": 0})
                if page_idx == 0:
                    info.raw_debug["discover_http"] = disc_data.get("_http_status")
                    info.raw_debug["discover_sample"] = json.dumps(disc_data, ensure_ascii=False)[:700]
                if not isinstance(disc_data, dict):
                    break
                streams_list = (
                    disc_data.get("items")
                    or disc_data.get("livestreams")
                    or disc_data.get("streams")
                    or (disc_data.get("data") or {}).get("items")
                    or (disc_data.get("data") or {}).get("livestreams")
                    or (disc_data.get("data") if isinstance(disc_data.get("data"), list) else [])
                    or []
                )
                matched = False
                for item in streams_list:
                    if not isinstance(item, dict):
                        continue
                    host = item.get("user") or item.get("host") or item.get("broadcaster") or item
                    ids = {
                        str(host.get("id") or ""),
                        str(host.get("user_id") or ""),
                        str(host.get("shared_id") or ""),
                        str(host.get("short_id") or ""),
                        str(item.get("user_id") or ""),
                        str(item.get("shared_id") or ""),
                        str(item.get("host_id") or ""),
                    }
                    names = {
                        str(host.get("username") or "").lower(),
                        str(host.get("unique_id") or "").lower(),
                    }
                    if str(creator_id) in ids or target_uid in ids or str(creator_id).lower() in names:
                        ls_id = str(
                            host.get("livestream_id")
                            or item.get("livestream_id")
                            or item.get("id")
                            or item.get("live_id")
                            or ""
                        )
                        real_uid = str(host.get("user_id") or host.get("id") or "")
                        if real_uid:
                            info.creator_id = real_uid
                        is_live_flag = bool(ls_id)
                        info.title = str(item.get("title") or "")
                        vc = item.get("viewer_count") or item.get("viewers") or 0
                        info.viewers = int(vc) if str(vc).isdigit() else 0
                        if not info.display_name:
                            info.display_name = str(host.get("name") or host.get("username") or creator_id)
                        matched = True
                        break
                if matched:
                    break
                cursor = disc_data.get("next")
                if not cursor:
                    break

        info.livestream_id = ls_id
        info.is_live = is_live_flag and bool(ls_id)

        if not info.is_live:
            return info

        # 3) Retrieve livestream details & enter room for Agora/HLS credentials
        ls_str = str(ls_id)
        ret_resp = await self._request_smart("livestream/retrieve", {"livestream_id": ls_str})
        ent_resp = await self._request_smart("livestream/enter", {"livestream_id": ls_str})

        info.raw_debug["retrieve_http"] = ret_resp.get("_http_status")
        slim_ret = {k: v for k, v in ret_resp.items() if k not in ("user", "target_bar", "top_gifters", "gifts")}
        info.raw_debug["retrieve_sample"] = json.dumps(slim_ret, ensure_ascii=False)[:1100]
        info.raw_debug["enter_http"] = ent_resp.get("_http_status")
        info.raw_debug["enter_sample"] = json.dumps(ent_resp, ensure_ascii=False)[:700]

        for candidate_resp in (ent_resp, ret_resp, prof_resp, user_obj):
            if not isinstance(candidate_resp, dict):
                continue
            if not info.title:
                info.title = str(
                    candidate_resp.get("title")
                    or (candidate_resp.get("livestream") or {}).get("title")
                    or ""
                )
            if not info.viewers:
                vc = (
                    candidate_resp.get("viewer_count")
                    or (candidate_resp.get("livestream") or {}).get("viewer_count")
                    or 0
                )
                if str(vc).isdigit():
                    info.viewers = int(vc)

            direct = self._extract_direct_stream_url(candidate_resp)
            if direct:
                info.stream_url = direct
                break

        if not info.stream_url:
            merged = {"retrieve": ret_resp, "enter": ent_resp, "profile": prof_resp}
            agora_uri = self._build_agora_uri(merged, ls_id=ls_id)
            if agora_uri:
                info.stream_url = agora_uri

        return info

# ==============================================================================
# MEDIA PROCESSING: FFMPEG MUXING (H.264 + PCM -> MP4), PROBE & CONTACT SHEET
# ==============================================================================

def probe_video_metadata(video_path: Path) -> Tuple[int, int, int]:
    """Return (duration_seconds, width, height) using ffprobe or ffmpeg fallback."""
    duration, width, height = 0, 720, 1280
    if FFPROBE_BIN:
        try:
            cmd = [
                FFPROBE_BIN,
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height:format=duration",
                "-of", "json",
                str(video_path),
            ]
            out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=15)
            data = json.loads(out.decode("utf-8", errors="ignore"))
            streams = data.get("streams") or []
            if streams:
                width = int(streams[0].get("width") or width)
                height = int(streams[0].get("height") or height)
            dur_str = (data.get("format") or {}).get("duration")
            if dur_str:
                duration = int(float(dur_str))
            return duration, width, height
        except Exception:
            pass
    if FFMPEG_BIN:
        try:
            res = subprocess.run(
                [FFMPEG_BIN, "-i", str(video_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=15,
            )
            stderr = res.stderr.decode("utf-8", errors="ignore")
            m_dur = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
            if m_dur:
                duration = int(m_dur.group(1)) * 3600 + int(m_dur.group(2)) * 60 + int(float(m_dur.group(3)))
            m_dim = re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})", stderr)
            if m_dim:
                width, height = int(m_dim.group(1)), int(m_dim.group(2))
        except Exception:
            pass
    return duration, width, height


def build_mux_command(
    ffmpeg_bin: str,
    h264_path: Path,
    pcm_path: Optional[Path],
    output_mp4: Path,
    fps: float = 25.0,
    sample_rate: int = 48000,
    channels: int = 1,
) -> List[str]:
    """
    Build the FFmpeg command to mux raw H.264 video + raw PCM s16le audio into a playable MP4.
    Solves the silent-video bug by properly including and AAC-encoding the PCM audio stream.
    """
    safe_fps = max(10.0, min(60.0, float(fps or 25.0)))
    has_video = h264_path.exists() and h264_path.stat().st_size > 1024
    has_audio = bool(pcm_path and pcm_path.exists() and pcm_path.stat().st_size > 1024)

    if has_video and has_audio and pcm_path is not None:
        return [
            ffmpeg_bin,
            "-y",
            "-fflags", "+genpts",
            "-r", f"{safe_fps:.2f}",
            "-f", "h264",
            "-i", str(h264_path),
            "-f", "s16le",
            "-ar", str(sample_rate),
            "-ac", str(channels),
            "-i", str(pcm_path),
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "128k",
            "-shortest",
            "-movflags", "+faststart",
            str(output_mp4),
        ]
    if has_video:
        return [
            ffmpeg_bin,
            "-y",
            "-fflags", "+genpts",
            "-r", f"{safe_fps:.2f}",
            "-f", "h264",
            "-i", str(h264_path),
            "-c:v", "copy",
            "-movflags", "+faststart",
            str(output_mp4),
        ]
    if has_audio and pcm_path is not None:
        return [
            ffmpeg_bin,
            "-y",
            "-f", "s16le",
            "-ar", str(sample_rate),
            "-ac", str(channels),
            "-i", str(pcm_path),
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            str(output_mp4),
        ]
    return []


def generate_thumbnail_and_contact_sheet(
    video_path: Path,
    thumb_path: Path,
    sheet_path: Path,
    duration_sec: int,
    make_sheet: bool = True,
) -> Tuple[Optional[Path], Optional[Path]]:
    """Generate a JPEG video cover thumbnail and a 4x3 StreamRecorder-style contact sheet."""
    if not FFMPEG_BIN or not video_path.exists():
        return None, None

    out_thumb: Optional[Path] = None
    out_sheet: Optional[Path] = None

    # 1) Cover thumbnail (Telegram video preview)
    seek_sec = min(max(1, duration_sec // 5), 10)
    try:
        subprocess.run(
            [
                FFMPEG_BIN, "-y",
                "-ss", str(seek_sec),
                "-i", str(video_path),
                "-vframes", "1",
                "-vf", "scale=320:-2",
                "-q:v", "4",
                str(thumb_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
        )
        if thumb_path.exists() and thumb_path.stat().st_size > 500:
            out_thumb = thumb_path
    except Exception:
        pass

    # 2) 4x3 Contact Sheet (12 preview frames across the full stream)
    if make_sheet and duration_sec >= 15:
        try:
            fps_val = max(0.0005, 12.0 / max(12.0, float(duration_sec)))
            vf = f"fps={fps_val:.6f},scale=320:-2,tile=4x3:padding=4:margin=4:color=0x111111"
            subprocess.run(
                [
                    FFMPEG_BIN, "-y",
                    "-i", str(video_path),
                    "-vf", vf,
                    "-frames:v", "1",
                    "-q:v", "3",
                    str(sheet_path),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=45,
                check=False,
            )
            if sheet_path.exists() and sheet_path.stat().st_size > 1000:
                out_sheet = sheet_path
        except Exception:
            pass

    return out_thumb, out_sheet


# ==============================================================================
# AGORA SINGLETON SERVICE & DUAL-TRACK (H.264 + PCM) RECEIVER
# ==============================================================================

class AgoraServiceManager:
    """Process-wide singleton for AgoraService so multiple streams record concurrently safely."""
    _lock = threading.Lock()
    _service: Any = None
    _app_id: str = ""

    @classmethod
    def get_service(cls, app_id: str) -> Any:
        if not AGORA_AVAILABLE:
            raise RuntimeError(f"agora-python-server-sdk نصب نیست: {AGORA_IMPORT_ERROR}")
        with cls._lock:
            if cls._service is None:
                cfg = AgoraServiceConfig()
                cfg.appid = app_id
                cfg.enable_audio_processor = 1
                cfg.enable_audio_device = 0
                cfg.enable_video = 1
                cfg.audio_scenario = AudioScenarioType.AUDIO_SCENARIO_CHORUS
                svc = AgoraService()
                svc.initialize(cfg)
                cls._service = svc
                cls._app_id = app_id
            return cls._service


class _VideoEncodedObserver(IVideoEncodedFrameObserver):  # type: ignore
    def __init__(self, receiver: "AgoraStreamReceiver"):
        super().__init__()
        self.receiver = receiver

    def on_encoded_video_frame(self, uid: Any, image_buffer: Any, length: int, video_encoded_frame_info: Any = None, *args: Any, **kwargs: Any) -> int:
        self.receiver.handle_video_frame(image_buffer, length)
        return 1


class _AudioPcmObserver(IAudioFrameObserver):  # type: ignore
    def __init__(self, receiver: "AgoraStreamReceiver"):
        super().__init__()
        self.receiver = receiver

    def on_playback_audio_frame_before_mixing(self, *args: Any, **kwargs: Any) -> int:
        audio_frame = kwargs.get("audio_frame")
        if audio_frame is None:
            for a in args:
                if hasattr(a, "buffer"):
                    audio_frame = a
                    break
        if audio_frame is not None:
            self.receiver.handle_audio_frame(audio_frame)
        return 1

    def on_playback_audio_frame(self, *args: Any, **kwargs: Any) -> int:
        return self.on_playback_audio_frame_before_mixing(*args, **kwargs)


class _ConnObserver(IRTCConnectionObserver):  # type: ignore
    def __init__(self, receiver: "AgoraStreamReceiver"):
        super().__init__()
        self.receiver = receiver

    def on_connected(self, *args: Any, **kwargs: Any) -> None:
        self.receiver.conn_events.append("connected")

    def on_disconnected(self, *args: Any, **kwargs: Any) -> None:
        reason = args[-1] if args else kwargs.get("reason", "?")
        self.receiver.conn_events.append(f"disconn({reason})")

    def on_connecting(self, *args: Any, **kwargs: Any) -> None:
        self.receiver.conn_events.append("connecting")

    def on_user_joined(self, *args: Any, **kwargs: Any) -> None:
        uid = args[-1] if args else kwargs.get("user_id", "?")
        self.receiver.conn_events.append(f"joined({uid})")

    def on_connection_failure(self, *args: Any, **kwargs: Any) -> None:
        reason = args[-1] if args else kwargs.get("reason", "?")
        self.receiver.conn_events.append(f"fail({reason})")


class AgoraStreamReceiver:
    """Captures raw H.264 video AND raw PCM audio from an Agora RTC channel."""
    def __init__(self, agora_uri: str, h264_path: Path, pcm_path: Path):
        parsed = urllib.parse.urlparse(agora_uri)
        qs = urllib.parse.parse_qs(parsed.query)
        self.app_id = parsed.netloc or DEFAULT_AGORA_APP_ID
        self.channel_name = parsed.path.lstrip("/")
        self.token = (qs.get("token") or [""])[0]
        self.uid = (qs.get("uid") or ["0"])[0]

        self.h264_path = h264_path
        self.pcm_path = pcm_path
        self._h264_fh = open(h264_path, "wb")
        self._pcm_fh = open(pcm_path, "wb")
        self._io_lock = threading.Lock()

        self.started_at = time.time()
        self.last_frame_at = time.time()
        self.video_frames = 0
        self.audio_frames = 0
        self.bytes_written = 0
        self.sample_rate = 48000
        self.channels = 1
        self._conn: Any = None
        self._closed = False
        self.conn_events: List[str] = []
        self.connect_rc: Any = None
        self.diag_flags: str = ""

    def handle_video_frame(self, image_buffer: Any, length: int) -> None:
        if self._closed or not image_buffer or length <= 0:
            return
        try:
            data = bytes(image_buffer[:length]) if not isinstance(image_buffer, (bytes, bytearray)) else image_buffer[:length]
            with self._io_lock:
                if not self._closed:
                    self._h264_fh.write(data)
                    self.video_frames += 1
                    self.bytes_written += len(data)
                    self.last_frame_at = time.time()
        except Exception:
            pass

    def handle_audio_frame(self, audio_frame: Any) -> None:
        if self._closed or audio_frame is None:
            return
        try:
            buf = getattr(audio_frame, "buffer", None)
            if not buf:
                return
            sr = getattr(audio_frame, "samples_per_sec", 0)
            ch = getattr(audio_frame, "channels", 0)
            if sr in (8000, 16000, 24000, 32000, 44100, 48000):
                self.sample_rate = int(sr)
            if ch in (1, 2):
                self.channels = int(ch)
            data = bytes(buf) if not isinstance(buf, (bytes, bytearray)) else buf
            with self._io_lock:
                if not self._closed:
                    self._pcm_fh.write(data)
                    self.audio_frames += 1
                    self.bytes_written += len(data)
                    self.last_frame_at = time.time()
        except Exception:
            pass

    @staticmethod
    def _build_publish_config(svc: Any) -> Any:
        """Build the publish_config object required by agora-python-server-sdk 2.3/2.4+."""
        import agora.rtc.agora_service as _ag_svc  # type: ignore
        import agora.rtc.agora_base as _ag_base  # type: ignore

        pub_cls = None
        # 1) Check type annotation on create_rtc_connection
        try:
            sig = inspect.signature(svc.create_rtc_connection)
            param = sig.parameters.get("publish_config")
            if param and param.annotation not in (inspect.Parameter.empty, Any, None):
                ann = param.annotation
                if isinstance(ann, type):
                    pub_cls = ann
                elif isinstance(ann, str):
                    pub_cls = getattr(_ag_svc, ann, None) or getattr(_ag_base, ann, None)
        except Exception:
            pass

        # 2) Fallback: search known class names across agora_service / agora_base / rtc_connection
        if pub_cls is None:
            mods = [_ag_svc, _ag_base]
            try:
                import agora.rtc.rtc_connection as _ag_conn  # type: ignore
                mods.append(_ag_conn)
            except Exception:
                pass
            for mod in mods:
                for cname in ("RTCConnPublishConfig", "PublishConfig", "PublishConfigure"):
                    cand = getattr(mod, cname, None)
                    if isinstance(cand, type):
                        pub_cls = cand
                        break
                if pub_cls is not None:
                    break

        if pub_cls is None:
            return None

        pub_cfg = pub_cls()
        # Audience recorder: do not publish outbound audio/video
        for attr in ("is_publish_audio", "is_publish_video", "publish_audio", "publish_video"):
            if hasattr(pub_cfg, attr):
                setattr(pub_cfg, attr, False)
        if hasattr(pub_cfg, "audio_scenario"):
            try:
                pub_cfg.audio_scenario = AudioScenarioType.AUDIO_SCENARIO_CHORUS
            except Exception:
                pass
        return pub_cfg

    def connect(self) -> None:
        svc = AgoraServiceManager.get_service(self.app_id)
        conn_cfg = RTCConnConfig(
            client_role_type=ClientRoleType.CLIENT_ROLE_AUDIENCE,
            channel_profile=ChannelProfileType.CHANNEL_PROFILE_LIVE_BROADCASTING,
        )
        for attr, val in (
            ("auto_subscribe_audio", 1),
            ("auto_subscribe_video", 1),
            ("enable_audio_recording_or_playout", 1),
        ):
            if hasattr(conn_cfg, attr):
                try:
                    setattr(conn_cfg, attr, val)
                except Exception:
                    pass
        # Support both SDK 2.2 (1 arg) and SDK 2.3/2.4+ (2 args: conn_cfg, publish_config)
        sig_params = inspect.signature(svc.create_rtc_connection).parameters
        if len(sig_params) >= 2:
            pub_cfg = self._build_publish_config(svc)
            self._conn = svc.create_rtc_connection(conn_cfg, pub_cfg)
        else:
            self._conn = svc.create_rtc_connection(conn_cfg)

        self._video_obs = _VideoEncodedObserver(self)
        self._audio_obs = _AudioPcmObserver(self)

        local_user = self._conn.get_local_user() if hasattr(self._conn, "get_local_user") else None
        if local_user is not None:
            if hasattr(local_user, "set_user_role"):
                local_user.set_user_role(ClientRoleType.CLIENT_ROLE_AUDIENCE)
            if hasattr(local_user, "subscribe_all_audio"):
                local_user.subscribe_all_audio()
            if hasattr(local_user, "subscribe_all_video"):
                try:
                    sub_opts = VideoSubscriptionOptions(
                        type=VideoStreamType.VIDEO_STREAM_HIGH,
                        encoded_frame_only=1,
                    )
                except TypeError:
                    sub_opts = VideoSubscriptionOptions()
                    if hasattr(sub_opts, "type"):
                        sub_opts.type = VideoStreamType.VIDEO_STREAM_HIGH
                    if hasattr(sub_opts, "encoded_frame_only"):
                        sub_opts.encoded_frame_only = 1
                    elif hasattr(sub_opts, "encodedFrameOnly"):
                        sub_opts.encodedFrameOnly = 1
                local_user.subscribe_all_video(sub_opts)

            if hasattr(local_user, "set_playback_audio_frame_before_mixing_parameters"):
                local_user.set_playback_audio_frame_before_mixing_parameters(self.channels, self.sample_rate)

            # Register video encoded frame observer (supports both 2.2 and 2.4 method names)
            if hasattr(local_user, "register_video_encoded_frame_observer"):
                local_user.register_video_encoded_frame_observer(self._video_obs)
            elif hasattr(local_user, "set_encoded_video_frame_observer"):
                local_user.set_encoded_video_frame_observer(self._video_obs)
            elif hasattr(self._conn, "register_video_encoded_frame_observer"):
                self._conn.register_video_encoded_frame_observer(self._video_obs)
            elif hasattr(self._conn, "set_encoded_video_frame_observer"):
                self._conn.set_encoded_video_frame_observer(self._video_obs)

            # Register audio frame observer (SDK 2.4+ requires enable_vad and vad_configure)
            reg_fn = (
                getattr(local_user, "register_audio_frame_observer", None)
                or getattr(self._conn, "register_audio_frame_observer", None)
            )
            if reg_fn is not None:
                n_params = len(inspect.signature(reg_fn).parameters)
                if n_params >= 3:
                    reg_fn(self._audio_obs, 0, None)
                elif n_params == 2:
                    reg_fn(self._audio_obs, 0)
                else:
                    reg_fn(self._audio_obs)
        else:
            if hasattr(self._conn, "subscribe_all_audio"):
                try:
                    self._conn.subscribe_all_audio()
                except Exception:
                    pass
            if hasattr(self._conn, "subscribe_all_video"):
                try:
                    sub_opts = VideoSubscriptionOptions()
                    if hasattr(sub_opts, "type"):
                        sub_opts.type = VideoStreamType.VIDEO_STREAM_HIGH
                    if hasattr(sub_opts, "encoded_frame_only"):
                        sub_opts.encoded_frame_only = 1
                    elif hasattr(sub_opts, "encodedFrameOnly"):
                        sub_opts.encodedFrameOnly = 1
                    self._conn.subscribe_all_video(sub_opts)
                except Exception:
                    pass
            reg_fn = getattr(self._conn, "register_audio_frame_observer", None)
            if reg_fn is not None:
                n_params = len(inspect.signature(reg_fn).parameters)
                if n_params >= 3:
                    reg_fn(self._audio_obs, 0, None)
                elif n_params == 2:
                    reg_fn(self._audio_obs, 0)
                else:
                    reg_fn(self._audio_obs)
            if hasattr(self._conn, "register_video_encoded_frame_observer"):
                self._conn.register_video_encoded_frame_observer(self._video_obs)
            elif hasattr(self._conn, "set_encoded_video_frame_observer"):
                self._conn.set_encoded_video_frame_observer(self._video_obs)

        self._conn_obs = _ConnObserver(self)
        if hasattr(self._conn, "register_observer"):
            try:
                self._conn.register_observer(self._conn_obs)
            except Exception as e:
                self.conn_events.append(f"obs_err:{e}")

        v_methods = [m for m in dir(self._conn) if "video" in m.lower() or "subscrib" in m.lower()][:6]
        self.diag_flags = f"lu={local_user is not None},vm={v_methods}"
        self.connect_rc = self._conn.connect(self.token, self.channel_name, str(self.uid))
        self.started_at = time.time()
        self.last_frame_at = time.time()

    def estimated_fps(self) -> float:
        elapsed = max(1.0, self.last_frame_at - self.started_at)
        if self.video_frames < 10:
            return 25.0
        return max(12.0, min(60.0, self.video_frames / elapsed))

    def close(self) -> None:
        with self._io_lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._h264_fh.flush()
                self._h264_fh.close()
            except Exception:
                pass
            try:
                self._pcm_fh.flush()
                self._pcm_fh.close()
            except Exception:
                pass
        if self._conn is not None:
            try:
                self._conn.disconnect()
                self._conn.release()
            except Exception:
                pass
            self._conn = None


# ==============================================================================
# RECORDING MANAGER (Unlimited Multi-Stream, Auto-Upload up to 2 GB via Telethon)
# ==============================================================================

@dataclass
class ActiveRecording:
    creator_id: str
    creator_name: str
    livestream_id: str
    stream_url: str
    db_rec_id: int
    chat_id: int
    started_at: float = field(default_factory=time.time)
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    status_text: str = "در حال ضبط..."
    bytes_written: int = 0
    part_number: int = 1
    task: Optional[asyncio.Task] = None


class RecordingManager:
    def __init__(self, bot: TelegramClient, db: Database, api: SuperLiveClient):
        self.bot = bot
        self.db = db
        self.api = api
        self.active: Dict[str, ActiveRecording] = {}
        self._fail_cooldown: Dict[str, float] = {}

    def max_concurrent(self) -> int:
        raw = self.db.get_setting("max_concurrent", str(DEFAULT_MAX_CONCURRENT))
        return int(raw) if raw.isdigit() else DEFAULT_MAX_CONCURRENT

    def is_recording(self, creator_id: str) -> bool:
        return str(creator_id) in self.active

    async def start_recording(
        self,
        stream_info: CreatorStreamInfo,
        chat_id: int,
        triggered_by: str = "watchlist",
    ) -> Tuple[bool, str]:
        cid = str(stream_info.creator_id)
        if cid in self.active:
            return False, f"استریم <b>{html.escape(stream_info.display_name or cid)}</b> همین الان در حال ضبط هست."
        if triggered_by == "watchlist":
            cd_until = self._fail_cooldown.get(cid, 0.0)
            if time.time() < cd_until:
                return False, "cooldown"

        limit = self.max_concurrent()
        if limit > 0 and len(self.active) >= limit:
            return False, f"سقف ضبط هم‌زمان ({limit} استریم) پر شده. از /settings می‌تونی بیشترش کنی یا 0 (نامحدود) بذاری."

        _, free_mb, _ = get_disk_stats()
        if free_mb < MIN_FREE_DISK_MB:
            return False, f"فضای خالی دیسک کمه ({free_mb:.0f} MB). برای جلوگیری از کرش سرور، ضبط جدید متوقف شد."

        if not stream_info.stream_url:
            return False, "لینک استریم یا اطلاعات کانال Agora برای این لایو پیدا نشد."

        if stream_info.stream_url.startswith("agora://") and not AGORA_AVAILABLE:
            return False, f"این لایو از نوع Agora هست ولی پکیج `agora-python-server-sdk` روی سرور نصب نیست."

        if not FFMPEG_BIN:
            return False, "ابزار FFmpeg روی سرور پیدا نشد (پکیج `imageio-ffmpeg` رو نصب کن)."

        cname = stream_info.display_name or stream_info.username or cid
        db_rec_id = self.db.create_recording_record(cid, cname, stream_info.livestream_id)
        rec = ActiveRecording(
            creator_id=cid,
            creator_name=cname,
            livestream_id=stream_info.livestream_id,
            stream_url=stream_info.stream_url,
            db_rec_id=db_rec_id,
            chat_id=chat_id,
        )
        self.active[cid] = rec
        rec.task = asyncio.create_task(self._run_recording_lifecycle(rec, triggered_by))
        return True, f"🔴 ضبط لایو <b>{html.escape(cname)}</b> (ID: <code>{cid}</code>) شروع شد!"

    async def stop_recording(self, creator_id: str) -> bool:
        rec = self.active.get(str(creator_id))
        if not rec:
            return False
        rec.status_text = "در حال توقف و آماده‌سازی فایل..."
        rec.stop_event.set()
        return True

    async def stop_all(self) -> int:
        count = 0
        for rec in list(self.active.values()):
            rec.stop_event.set()
            count += 1
        return count

    async def _run_recording_lifecycle(self, rec: ActiveRecording, triggered_by: str) -> None:
        notify_on = self.db.get_setting("notify_live", "1") == "1"
        status_msg = None
        if notify_on:
            try:
                badge = "👀 واچ‌لیست" if triggered_by == "watchlist" else "🎬 ضبط دستی"
                status_msg = await self.bot.send_message(
                    rec.chat_id,
                    (
                        f"🔴 <b>شروع ضبط لایو ({badge})</b>\n"
                        f"👤 استریمر: <b>{html.escape(rec.creator_name)}</b> (<code>{rec.creator_id}</code>)\n"
                        f"📡 پروتکل: <code>{'Agora RTC' if rec.stream_url.startswith('agora://') else 'HLS/FFmpeg'}</code>\n"
                        f"⏳ وضعیت: در حال ضبط پیوسته (تا سقف 2GB بدون تکه‌تکه شدن)..."
                    ),
                    parse_mode="html",
                    buttons=[[Button.inline("⏹ توقف و ارسال همین الان", data=f"stop:{rec.creator_id}")]],
                )
            except Exception as e:
                log.warning("Failed sending start notice: %s", e)

        work_dir = RECORDINGS_DIR / f"{sanitize_filename(rec.creator_name)}_{rec.creator_id}_{int(rec.started_at)}"
        work_dir.mkdir(parents=True, exist_ok=True)
        final_status = "completed"
        total_duration = 0
        total_size = 0
        last_msg_id = 0
        last_arch_chat = 0
        last_arch_msg = 0
        notes = ""

        try:
            while not rec.stop_event.is_set():
                part_mp4 = work_dir / f"{sanitize_filename(rec.creator_name)}_part{rec.part_number}.mp4"
                if rec.stream_url.startswith("agora://"):
                    captured_ok, reached_size_cap = await self._capture_agora_part(rec, work_dir, part_mp4)
                else:
                    captured_ok, reached_size_cap = await self._capture_ffmpeg_part(rec, part_mp4)

                if captured_ok and part_mp4.exists() and part_mp4.stat().st_size > 10_000:
                    dur, _, _ = await asyncio.to_thread(probe_video_metadata, part_mp4)
                    fsize = part_mp4.stat().st_size
                    total_duration += dur
                    total_size += fsize
                    msg_id, arch_chat, arch_msg = await self._upload_part_to_telegram(
                        rec, part_mp4, dur, status_msg
                    )
                    last_msg_id = msg_id or last_msg_id
                    last_arch_chat = arch_chat or last_arch_chat
                    last_arch_msg = arch_msg or last_arch_msg

                    # Cleanup uploaded part immediately if auto_delete enabled (protects 10 GiB disk)
                    if self.db.get_setting("auto_delete", "1") == "1":
                        try:
                            part_mp4.unlink(missing_ok=True)
                        except Exception:
                            pass

                if reached_size_cap and not rec.stop_event.is_set():
                    rec.part_number += 1
                    continue
                break

            if total_size == 0:
                final_status = "empty"
                self._fail_cooldown[rec.creator_id] = time.time() + 180.0
                notes = "هیچ فریم ویدیویی/صوتی دریافت نشد (ممکن است لایو بسته شده یا خصوصی باشد)."
                await self.bot.send_message(
                    rec.chat_id,
                    (
                        f"⚠️ ضبط <b>{html.escape(rec.creator_name)}</b> تمام شد ولی دیتایی دریافت نشد.\n"
                        f"📊 جزئیات فنی: <code>{html.escape(rec.status_text)}</code>"
                    ),
                    parse_mode="html",
                )
        except Exception as e:
            final_status = "error"
            self._fail_cooldown[rec.creator_id] = time.time() + 180.0
            notes = str(e)[:300]
            log.exception("Recording failed for %s", rec.creator_id)
            try:
                await self.bot.send_message(
                    rec.chat_id,
                    f"❌ خطا در ضبط <b>{html.escape(rec.creator_name)}</b>:\n<code>{html.escape(str(e)[:300])}</code>",
                    parse_mode="html",
                )
            except Exception:
                pass
        finally:
            self.db.finish_recording_record(
                rec.db_rec_id,
                status=final_status,
                duration_sec=total_duration,
                file_size_bytes=total_size,
                tg_chat_id=rec.chat_id,
                tg_message_id=last_msg_id,
                archive_chat_id=last_arch_chat,
                archive_message_id=last_arch_msg,
                notes=notes,
            )
            if final_status == "completed":
                self.db.update_watch_status(
                    rec.creator_id,
                    "offline",
                    display_name=rec.creator_name,
                    increment_recordings=True,
                )
            self.active.pop(rec.creator_id, None)
            if self.db.get_setting("auto_delete", "1") == "1":
                shutil.rmtree(work_dir, ignore_errors=True)
            if status_msg:
                try:
                    await status_msg.delete()
                except Exception:
                    pass

    async def _capture_ffmpeg_part(self, rec: ActiveRecording, output_mp4: Path) -> Tuple[bool, bool]:
        """Record HLS/FLV/RTMP directly with FFmpeg (-c copy, no re-encoding)."""
        cmd = [
            FFMPEG_BIN or "ffmpeg",
            "-y",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-i", rec.stream_url,
            "-c", "copy",
            "-movflags", "+faststart",
            str(output_mp4),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        reached_size_cap = False
        try:
            while proc.returncode is None:
                if rec.stop_event.is_set():
                    break
                if output_mp4.exists():
                    sz = output_mp4.stat().st_size
                    rec.bytes_written = sz
                    if sz >= MAX_SINGLE_FILE_BYTES:
                        reached_size_cap = True
                        break
                _, free_mb, _ = get_disk_stats()
                if free_mb < MIN_FREE_DISK_MB:
                    log.warning("Disk low (%.0f MB free) - finalizing %s", free_mb, rec.creator_id)
                    rec.stop_event.set()
                    break
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    continue
        finally:
            if proc.returncode is None:
                try:
                    if proc.stdin:
                        proc.stdin.write(b"q\n")
                        await proc.stdin.drain()
                    await asyncio.wait_for(proc.wait(), timeout=10.0)
                except Exception:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=5.0)
                    except Exception:
                        proc.kill()
        return output_mp4.exists() and output_mp4.stat().st_size > 10_000, reached_size_cap

    async def _capture_agora_part(
        self, rec: ActiveRecording, work_dir: Path, output_mp4: Path
    ) -> Tuple[bool, bool]:
        """Record Agora RTC stream (H.264 video + PCM audio) and mux cleanly into MP4."""
        h264_file = work_dir / f"raw_p{rec.part_number}.h264"
        pcm_file = work_dir / f"raw_p{rec.part_number}.pcm"
        receiver = AgoraStreamReceiver(rec.stream_url, h264_file, pcm_file)
        reached_size_cap = False

        await asyncio.to_thread(receiver.connect)
        try:
            while not rec.stop_event.is_set():
                await asyncio.sleep(2.0)
                rec.bytes_written = receiver.bytes_written

                # Auto-stop when broadcaster stops sending frames
                idle = time.time() - receiver.last_frame_at
                if idle >= AGORA_INACTIVITY_TIMEOUT:
                    log.info("Agora stream %s inactive for %.0fs, finishing.", rec.creator_id, idle)
                    break

                # Roll over before hitting Telegram's 2 GB MTProto cap
                if receiver.bytes_written >= MAX_SINGLE_FILE_BYTES:
                    reached_size_cap = True
                    break

                # Disk safety guard
                _, free_mb, _ = get_disk_stats()
                if free_mb < MIN_FREE_DISK_MB:
                    log.warning("Low disk (%.0f MB free), finalizing Agora stream %s", free_mb, rec.creator_id)
                    rec.stop_event.set()
                    break
        finally:
            fps = receiver.estimated_fps()
            sr = receiver.sample_rate
            ch = receiver.channels
            await asyncio.to_thread(receiver.close)

        rec.status_text = (
            f"v={receiver.video_frames}, a={receiver.audio_frames}, rc={receiver.connect_rc}, "
            f"ev={','.join(receiver.conn_events[:6])}, uid={receiver.uid}, tok={len(receiver.token)}, "
            f"{receiver.diag_flags}"
        )
        log.info("Agora capture ended for %s: %s", rec.creator_id, rec.status_text)
        mux_cmd = build_mux_command(
            FFMPEG_BIN or "ffmpeg",
            h264_file,
            pcm_file,
            output_mp4,
            fps=fps,
            sample_rate=sr,
            channels=ch,
        )
        if not mux_cmd:
            return False, False

        proc = await asyncio.create_subprocess_exec(
            *mux_cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr_bytes = await proc.communicate()
        if proc.returncode != 0:
            log.error("FFmpeg mux error: %s", stderr_bytes.decode("utf-8", errors="ignore")[-400:])

        # Clean up raw intermediate streams right away to save disk
        h264_file.unlink(missing_ok=True)
        pcm_file.unlink(missing_ok=True)

        return output_mp4.exists() and output_mp4.stat().st_size > 10_000, reached_size_cap

    async def _upload_part_to_telegram(
        self,
        rec: ActiveRecording,
        mp4_path: Path,
        duration_sec: int,
        status_msg: Any,
    ) -> Tuple[int, int, int]:
        """Upload up to 2 GB MP4 via Telethon MTProto with progress, cover thumb & contact sheet."""
        rec.status_text = "در حال آپلود به تلگرام..."
        fsize = mp4_path.stat().st_size
        _, width, height = await asyncio.to_thread(probe_video_metadata, mp4_path)

        make_sheet = self.db.get_setting("contact_sheet", "1") == "1"
        thumb_path = mp4_path.with_suffix(".thumb.jpg")
        sheet_path = mp4_path.with_suffix(".sheet.jpg")
        thumb_file, sheet_file = await asyncio.to_thread(
            generate_thumbnail_and_contact_sheet,
            mp4_path,
            thumb_path,
            sheet_path,
            duration_sec,
            make_sheet,
        )

        # Send Contact Sheet preview first (just like @superliverec_bot)
        if sheet_file and sheet_file.exists():
            try:
                await self.bot.send_file(
                    rec.chat_id,
                    str(sheet_file),
                    caption=(
                        f"🖼 <b>پیش‌نمایش ضبط (Contact Sheet)</b>\n"
                        f"👤 {html.escape(rec.creator_name)} (<code>{rec.creator_id}</code>)\n"
                        f"⏱ مدت: <code>{fmt_duration(duration_sec)}</code> | 💾 حجم: <code>{fmt_bytes(fsize)}</code>"
                    ),
                    parse_mode="html",
                )
            except Exception as e:
                log.warning("Contact sheet upload failed: %s", e)

        # Throttled progress callback
        last_update = [0.0]

        async def progress_cb(current: int, total: int) -> None:
            now = time.time()
            if status_msg and (now - last_update[0] >= 5.0 or current == total):
                last_update[0] = now
                pct = (current / max(1, total)) * 100.0
                try:
                    await status_msg.edit(
                        f"⬆️ <b>در حال آپلود ویدیو به تلگرام...</b>\n"
                        f"👤 {html.escape(rec.creator_name)} (پارت {rec.part_number})\n"
                        f"📊 پیشرفت: <code>{pct:.1f}%</code> ({fmt_bytes(current)} / {fmt_bytes(total)})",
                        parse_mode="html",
                    )
                except Exception:
                    pass

        date_str = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        part_label = f" | پارت {rec.part_number}" if rec.part_number > 1 else ""
        caption = (
            f"🎬 <b>{html.escape(rec.creator_name)}</b>{part_label}\n"
            f"🆔 آیدی: <code>{rec.creator_id}</code>\n"
            f"⏱ مدت: <code>{fmt_duration(duration_sec)}</code> | 💾 حجم: <code>{fmt_bytes(fsize)}</code>\n"
            f"📐 کیفیت: <code>{width}x{height}</code> | 📅 {date_str}"
        )

        attrs = [
            DocumentAttributeVideo(
                duration=max(1, duration_sec),
                w=width,
                h=height,
                supports_streaming=True,
            )
        ]

        sent = None
        for attempt in range(3):
            try:
                sent = await self.bot.send_file(
                    rec.chat_id,
                    str(mp4_path),
                    caption=caption,
                    parse_mode="html",
                    thumb=str(thumb_file) if thumb_file else None,
                    attributes=attrs,
                    supports_streaming=True,
                    progress_callback=progress_cb,
                )
                break
            except FloodWaitError as fw:
                wait_s = int(getattr(fw, "seconds", 10)) + 2
                log.warning("FloodWait %ss while uploading %s (attempt %d)", wait_s, rec.creator_id, attempt + 1)
                await asyncio.sleep(wait_s)

        # Optional: Forward to user's private Archive Channel
        arch_chat_raw = self.db.get_setting("archive_chat_id", "")
        arch_chat_id = 0
        arch_msg_id = 0
        if arch_chat_raw:
            try:
                arch_chat_id = int(arch_chat_raw)
                fwd = await self.bot.forward_messages(arch_chat_id, sent)
                if isinstance(fwd, list) and fwd:
                    arch_msg_id = fwd[0].id
                elif hasattr(fwd, "id"):
                    arch_msg_id = fwd.id
            except Exception as e:
                log.warning("Failed forwarding to archive channel %s: %s", arch_chat_raw, e)

        thumb_path.unlink(missing_ok=True)
        sheet_path.unlink(missing_ok=True)
        return getattr(sent, "id", 0), arch_chat_id, arch_msg_id


# ==============================================================================
# BACKGROUND WATCHLIST MONITOR LOOP
# ==============================================================================

class WatchlistMonitor:
    def __init__(self, db: Database, api: SuperLiveClient, rec_mgr: RecordingManager):
        self.db = db
        self.api = api
        self.rec_mgr = rec_mgr
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        log.info("Watchlist monitor started.")
        while not self._stop.is_set():
            try:
                interval = int(self.db.get_setting("poll_interval", str(DEFAULT_POLL_INTERVAL)))
                interval = max(10, min(300, interval))
                owner_id = self.db.get_owner_id()
                if owner_id:
                    items = self.db.list_watchlist()
                    discover_map: Optional[Dict[str, Dict[str, Any]]] = None
                    if items:
                        try:
                            discover_map, _, _ = await self.api.fetch_discover_map(max_pages=4)
                        except Exception as e:
                            log.warning("Batch discover fetch failed: %s", e)
                    for item in items:
                        if self._stop.is_set():
                            break
                        cid = item["creator_id"]
                        if not item["auto_record"] or self.rec_mgr.is_recording(cid):
                            continue
                        try:
                            info = await self.api.check_creator_and_get_stream(cid, discover_map=discover_map)
                            if info.is_live:
                                self.db.update_watch_status(
                                    cid, "live", display_name=info.display_name, username=info.username
                                )
                                ok, msg = await self.rec_mgr.start_recording(
                                    info, chat_id=owner_id, triggered_by="watchlist"
                                )
                                if ok:
                                    log.info("Auto-started recording for %s", cid)
                                else:
                                    log.debug("Skipped auto-record for %s: %s", cid, msg)
                            else:
                                self.db.update_watch_status(
                                    cid, "offline", display_name=info.display_name, username=info.username
                                )
                        except Exception as e:
                            log.warning("Poll error for creator %s: %s", cid, e)
                        await asyncio.sleep(1.2)
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.exception("Watchlist loop error: %s", e)
                await asyncio.sleep(15)


# ==============================================================================
# TELEGRAM BOT UI & HANDLERS (Telethon MTProto)
# ==============================================================================

class SuperLiveRecBot:
    def __init__(self):
        self.db = Database(DB_PATH)
        self.api = SuperLiveClient(self.db)
        self.bot = TelegramClient(str(SESSION_PATH), TG_API_ID, TG_API_HASH)
        self.rec_mgr = RecordingManager(self.bot, self.db, self.api)
        self.monitor = WatchlistMonitor(self.db, self.api, self.rec_mgr)
        self._awaiting_input: Dict[int, str] = {}

    def _is_owner(self, user_id: int) -> bool:
        owner = self.db.get_owner_id()
        if owner == 0:
            self.db.set_owner_id(user_id)
            return True
        return user_id == owner

    def _dashboard_text_and_buttons(self) -> Tuple[str, List[List[Any]]]:
        wl = self.db.list_watchlist()
        live_cnt = sum(1 for x in wl if x["last_status"] == "live")
        active_cnt = len(self.rec_mgr.active)
        total_recs = self.db.count_recordings()
        used_mb, free_mb, total_mb = get_disk_stats()
        ram_used, ram_total = get_ram_usage_mb()

        agora_badge = "✅ آماده" if AGORA_AVAILABLE else "❌ نصب نیست"
        ffmpeg_badge = "✅ آماده" if FFMPEG_BIN else "❌ نصب نیست"
        ram_line = f"{ram_used:.0f} / {ram_total:.0f} MB" if ram_total > 0 else f"{ram_used:.0f} MB"

        text = (
            "🎥 <b>SuperLiveRec Personal Unlimited</b>\n"
            "<i>الهام‌گرفته از @superliverec_bot و StreamRecorder.io — بدون محدودیت تعداد</i>\n\n"
            f"🔴 در حال ضبط الان: <b>{active_cnt}</b> استریم\n"
            f"👀 واچ‌لیست: <b>{len(wl)}</b> نفر (آنلاین: <b>{live_cnt}</b>)\n"
            f"📚 آرشیو ضبط‌شده‌ها: <b>{total_recs}</b> ویدیو\n"
            f"⬆️ سقف آپلود هر فایل: <b>تا 2 گیگابایت</b> (MTProto)\n\n"
            f"🖥 <b>وضعیت سرور:</b>\n"
            f"• رم: <code>{ram_line}</code> | دیسک خالی: <code>{free_mb/1024:.2f} / {total_mb/1024:.1f} GB</code>\n"
            f"• موتور Agora: {agora_badge} | FFmpeg: {ffmpeg_badge}\n\n"
            "💡 برای شروع کافیه آیدی عددی یا لینک پروفایل SuperLive رو همینجا بفرستی یا از دکمه‌ها استفاده کنی:"
        )
        buttons = [
            [
                Button.inline("➕ افزودن به واچ‌لیست", data="act:add_watch"),
                Button.inline("🎬 ضبط فوری (Rec)", data="act:manual_rec"),
            ],
            [
                Button.inline(f"📋 واچ‌لیست ({len(wl)})", data="page:watchlist:0"),
                Button.inline(f"🔴 در حال ضبط ({active_cnt})", data="page:active"),
            ],
            [
                Button.inline("📚 آرشیو ویدیوها", data="page:archive:0"),
                Button.inline("⚙️ تنظیمات", data="page:settings"),
            ],
            [Button.inline("🔄 بروزرسانی داشبورد", data="page:home")],
        ]
        return text, buttons

    def _register_handlers(self) -> None:
        @self.bot.on(events.NewMessage(pattern=r"^/(start|menu)$"))
        async def cmd_start(event: Any) -> None:
            if not self._is_owner(event.sender_id):
                await event.reply("⛔ این ربات شخصی هست و فقط برای مالک سرور فعاله.")
                return
            self._awaiting_input.pop(event.sender_id, None)
            text, buttons = self._dashboard_text_and_buttons()
            await event.reply(text, parse_mode="html", buttons=buttons)

        @self.bot.on(events.NewMessage(pattern=r"^/watch(?:\s+(.+))?$"))
        async def cmd_watch(event: Any) -> None:
            if not self._is_owner(event.sender_id):
                return
            arg = (event.pattern_match.group(1) or "").strip()
            if not arg:
                self._awaiting_input[event.sender_id] = "watch"
                await event.reply("➕ آیدی عددی، یوزرنیم یا لینک پروفایل SuperLive رو بفرست تا به واچ‌لیست اضافه بشه:")
                return
            await self._handle_add_watch(event, arg)

        @self.bot.on(events.NewMessage(pattern=r"^/unwatch\s+(.+)$"))
        async def cmd_unwatch(event: Any) -> None:
            if not self._is_owner(event.sender_id):
                return
            cid = extract_creator_identifier(event.pattern_match.group(1))
            if self.db.remove_watch(cid):
                await event.reply(f"🗑 استریمر <code>{html.escape(cid)}</code> از واچ‌لیست حذف شد.", parse_mode="html")
            else:
                await event.reply("این آیدی توی واچ‌لیست پیدا نشد.")

        @self.bot.on(events.NewMessage(pattern=r"^/rec(?:\s+(.+))?$"))
        async def cmd_rec(event: Any) -> None:
            if not self._is_owner(event.sender_id):
                return
            arg = (event.pattern_match.group(1) or "").strip()
            if not arg:
                self._awaiting_input[event.sender_id] = "rec"
                await event.reply("🎬 آیدی یا لینک استریمر SuperLive رو بفرست تا فوری ضبطش شروع بشه:")
                return
            await self._handle_manual_rec(event, arg)

        @self.bot.on(events.NewMessage(pattern=r"^/stop(?:\s+(.+))?$"))
        async def cmd_stop(event: Any) -> None:
            if not self._is_owner(event.sender_id):
                return
            arg = (event.pattern_match.group(1) or "").strip().lower()
            if arg == "all":
                cnt = await self.rec_mgr.stop_all()
                await event.reply(f"⏹ دستور توقف برای {cnt} ضبط فعال ارسال شد (فایل‌ها پس از آماده‌سازی ارسال می‌شن).")
                return
            if not arg:
                await self._send_active_view(event)
                return
            cid = extract_creator_identifier(arg)
            if await self.rec_mgr.stop_recording(cid):
                await event.reply(f"⏹ ضبط <code>{html.escape(cid)}</code> در حال توقف و ارسال ویدیوست...", parse_mode="html")
            else:
                await event.reply("این استریمر الان در حال ضبط نیست.")

        @self.bot.on(events.NewMessage(pattern=r"^/watchlist$"))
        async def cmd_watchlist(event: Any) -> None:
            if not self._is_owner(event.sender_id):
                return
            await self._send_watchlist_view(event, page=0)

        @self.bot.on(events.NewMessage(pattern=r"^/active$"))
        async def cmd_active(event: Any) -> None:
            if not self._is_owner(event.sender_id):
                return
            await self._send_active_view(event)

        @self.bot.on(events.NewMessage(pattern=r"^/(archive|history)$"))
        async def cmd_archive(event: Any) -> None:
            if not self._is_owner(event.sender_id):
                return
            await self._send_archive_view(event, page=0)

        @self.bot.on(events.NewMessage(pattern=r"^/check\s+(.+)$"))
        async def cmd_check(event: Any) -> None:
            if not self._is_owner(event.sender_id):
                return
            raw = event.pattern_match.group(1).strip()
            msg = await event.reply("🔍 در حال بررسی وضعیت استریمر...")
            cid, uname, dname, _ = await self.api.resolve_creator(raw)
            info = await self.api.check_creator_and_get_stream(cid or raw)
            status = "🔴 آنلاین (LIVE)" if info.is_live else "⚫ آفلاین"
            proto = "Agora RTC" if info.stream_url.startswith("agora://") else ("HLS/Direct" if info.stream_url else "نامشخص")
            await msg.edit(
                f"🔎 <b>نتیجه بررسی استریمر</b>\n\n"
                f"👤 نام: <b>{html.escape(info.display_name or dname or cid)}</b>\n"
                f"🆔 آیدی: <code>{html.escape(info.creator_id)}</code>\n"
                f"📡 وضعیت: {status}\n"
                f"🎞 آیدی لایو: <code>{html.escape(info.livestream_id or '-')}</code>\n"
                f"⚙️ پروتکل دریافت: <code>{proto}</code>",
                parse_mode="html",
                buttons=[
                    [
                        Button.inline("➕ افزودن به واچ‌لیست", data=f"qwatch:{info.creator_id}"),
                        Button.inline("🎬 ضبط فوری", data=f"qrec:{info.creator_id}"),
                    ]
                ],
            )

        @self.bot.on(events.NewMessage(pattern=r"^/debug\s+(.+)$"))
        async def cmd_debug(event: Any) -> None:
            if not self._is_owner(event.sender_id):
                return
            raw = event.pattern_match.group(1).strip()
            msg = await event.reply("🛠 در حال دریافت پاسخ خام سرور SuperLive...")
            cid, _, _, _ = await self.api.resolve_creator(raw)
            info = await self.api.check_creator_and_get_stream(cid or raw)
            d = info.raw_debug
            report = (
                f"🛠 <b>گزارش دیباگ API برای <code>{html.escape(info.creator_id)}</code></b>\n"
                f"• Init: <code>{d.get('init_status')}</code> | Token: <code>{d.get('has_token')}</code>\n"
                f"• Profile HTTP: <code>{d.get('profile_http')}</code>\n"
                f"• Discover HTTP: <code>{d.get('discover_http', '-')}</code>\n"
                f"• Retrieve/Enter HTTP: <code>{d.get('retrieve_http', '-')} / {d.get('enter_http', '-')}</code>\n"
                f"• Live: <code>{info.is_live}</code> | ls_id: <code>{html.escape(info.livestream_id or '-')}</code>\n"
                f"• Stream: <code>{html.escape((info.stream_url[:60] + '...') if len(info.stream_url) > 60 else (info.stream_url or '-'))}</code>\n\n"
                f"<b>Profile JSON:</b>\n<code>{html.escape(str(d.get('profile_sample', ''))[:1200])}</code>\n\n"
                f"<b>Retrieve/Enter JSON:</b>\n<code>{html.escape(str(d.get('retrieve_sample') or d.get('enter_sample') or d.get('discover_sample') or '')[:1200])}</code>"
            )
            await msg.edit(report[:4000], parse_mode="html")

        @self.bot.on(events.NewMessage(pattern=r"^/login\s+(\S+)\s+(.+)$"))
        async def cmd_login(event: Any) -> None:
            if not self._is_owner(event.sender_id):
                return
            email = event.pattern_match.group(1).strip()
            password = event.pattern_match.group(2).strip()
            ok, msg_text = await self.api.email_signin(email, password)
            try:
                await event.delete()
            except Exception:
                pass
            await event.respond(f"{'✅' if ok else '❌'} {msg_text}")

        @self.bot.on(events.NewMessage(pattern=r"^/settoken\s+(\S+)(?:\s+(\S+))?$"))
        async def cmd_settoken(event: Any) -> None:
            if not self._is_owner(event.sender_id):
                return
            tok = event.pattern_match.group(1).strip()
            dev = (event.pattern_match.group(2) or "").strip()
            self.api.set_credentials(tok, dev)
            try:
                await event.delete()
            except Exception:
                pass
            await event.respond(
                "✅ توکن و Device-ID با موفقیت ذخیره شد (پیام شما برای امنیت حذف شد).\n"
                "الان با <code>/debug آیدی_استریمر</code> یا <code>/rec آیدی_استریمر</code> تست کن!",
                parse_mode="html",
            )

        @self.bot.on(events.NewMessage(pattern=r"^/setarchive(?:\s+(-?\d+|off))?$"))
        async def cmd_setarchive(event: Any) -> None:
            if not self._is_owner(event.sender_id):
                return
            val = (event.pattern_match.group(1) or "").strip()
            if not val:
                cur = self.db.get_setting("archive_chat_id", "تنظیم نشده")
                await event.reply(
                    f"📦 کانال آرشیو فعلی: <code>{html.escape(cur)}</code>\n"
                    f"برای تنظیم: <code>/setarchive -1001234567890</code> (ربات باید ادمین کانال باشه)\n"
                    f"برای غیرفعال کردن: <code>/setarchive off</code>",
                    parse_mode="html",
                )
                return
            if val.lower() == "off":
                self.db.set_setting("archive_chat_id", "")
                await event.reply("✅ ارسال خودکار به کانال آرشیو غیرفعال شد.")
            else:
                self.db.set_setting("archive_chat_id", val)
                await event.reply(f"✅ کانال آرشیو روی <code>{html.escape(val)}</code> تنظیم شد.", parse_mode="html")

        @self.bot.on(events.NewMessage(pattern=r"^/settings$"))
        async def cmd_settings(event: Any) -> None:
            if not self._is_owner(event.sender_id):
                return
            await self._send_settings_view(event)

        @self.bot.on(events.NewMessage(func=lambda e: e.is_private and e.text and not e.text.startswith("/")))
        async def on_plain_text(event: Any) -> None:
            if not self._is_owner(event.sender_id):
                return
            mode = self._awaiting_input.pop(event.sender_id, None)
            raw = event.text.strip()
            if mode == "rec":
                await self._handle_manual_rec(event, raw)
            elif mode == "watch":
                await self._handle_add_watch(event, raw)
            else:
                # Smart direct-paste: if user pastes a SuperLive link or numeric ID directly
                if "superlive" in raw.lower() or "spl-live" in raw.lower() or re.fullmatch(r"\d{5,15}", raw):
                    cid = extract_creator_identifier(raw)
                    await event.reply(
                        f"آیدی <code>{html.escape(cid)}</code> تشخیص داده شد. چیکار کنم؟",
                        parse_mode="html",
                        buttons=[
                            [
                                Button.inline("➕ اضافه به واچ‌لیست", data=f"qwatch:{cid}"),
                                Button.inline("🎬 ضبط فوری", data=f"qrec:{cid}"),
                            ]
                        ],
                    )

        @self.bot.on(events.CallbackQuery())
        async def on_callback(event: Any) -> None:
            if not self._is_owner(event.sender_id):
                await event.answer("⛔ دسترسی ندارید", alert=True)
                return
            data = event.data.decode("utf-8", errors="ignore")
            try:
                if data == "page:home":
                    text, buttons = self._dashboard_text_and_buttons()
                    await event.edit(text, parse_mode="html", buttons=buttons)
                elif data == "act:add_watch":
                    self._awaiting_input[event.sender_id] = "watch"
                    await event.answer()
                    await event.respond("➕ آیدی عددی، یوزرنیم یا لینک پروفایل SuperLive رو بفرست:")
                elif data == "act:manual_rec":
                    self._awaiting_input[event.sender_id] = "rec"
                    await event.answer()
                    await event.respond("🎬 آیدی یا لینک استریمر رو برای ضبط فوری بفرست:")
                elif data.startswith("page:watchlist:"):
                    page = int(data.split(":")[2])
                    await self._send_watchlist_view(event, page=page, edit=True)
                elif data == "page:active":
                    await self._send_active_view(event, edit=True)
                elif data.startswith("page:archive:"):
                    page = int(data.split(":")[2])
                    await self._send_archive_view(event, page=page, edit=True)
                elif data == "page:settings":
                    await self._send_settings_view(event, edit=True)
                elif data.startswith("qwatch:"):
                    cid = data.split(":", 1)[1]
                    await event.answer("در حال افزودن...")
                    await self._handle_add_watch(event, cid)
                elif data.startswith("qrec:"):
                    cid = data.split(":", 1)[1]
                    await event.answer("در حال بررسی لایو...")
                    await self._handle_manual_rec(event, cid)
                elif data.startswith("toggle:"):
                    cid = data.split(":", 1)[1]
                    val = self.db.toggle_auto_record(cid)
                    await event.answer("ضبط خودکار روشن شد ✅" if val else "ضبط خودکار خاموش شد ⏸")
                    await self._send_watchlist_view(event, page=0, edit=True)
                elif data.startswith("rmwatch:"):
                    cid = data.split(":", 1)[1]
                    self.db.remove_watch(cid)
                    await event.answer("از واچ‌لیست حذف شد 🗑")
                    await self._send_watchlist_view(event, page=0, edit=True)
                elif data.startswith("stop:"):
                    cid = data.split(":", 1)[1]
                    ok = await self.rec_mgr.stop_recording(cid)
                    await event.answer("در حال توقف و ارسال ویدیو..." if ok else "ضبط فعالی پیدا نشد")
                    await self._send_active_view(event, edit=True)
                elif data.startswith("set:"):
                    await self._handle_setting_toggle(event, data.split(":", 1)[1])
            except MessageNotModifiedError:
                await event.answer()
            except Exception as e:
                log.warning("Callback error (%s): %s", data, e)
                await event.answer("خطا در اجرای دستور", alert=False)

    async def _handle_add_watch(self, event: Any, raw: str) -> None:
        wait_msg = await event.respond("⏳ در حال شناسایی استریمر در SuperLive...")
        cid, uname, dname, avatar = await self.api.resolve_creator(raw)
        if not cid:
            await wait_msg.edit("❌ نتونستم آیدی معتبری از این متن استخراج کنم.")
            return
        added = self.db.add_watch(cid, uname, dname, avatar)
        info = await self.api.check_creator_and_get_stream(cid)
        if info.is_live:
            self.db.update_watch_status(cid, "live", display_name=info.display_name, username=info.username)
            ok, rec_msg = await self.rec_mgr.start_recording(info, chat_id=event.chat_id, triggered_by="watchlist")
            await wait_msg.edit(
                f"{'✅ به واچ‌لیست اضافه شد' if added else '♻️ در واچ‌لیست بروزرسانی شد'}: "
                f"<b>{html.escape(info.display_name or dname or cid)}</b> (<code>{cid}</code>)\n"
                f"{rec_msg}",
                parse_mode="html",
            )
        else:
            await wait_msg.edit(
                f"{'✅ به واچ‌لیست اضافه شد' if added else '♻️ در واچ‌لیست بود (بروزرسانی شد)'}:\n"
                f"👤 <b>{html.escape(info.display_name or dname or cid)}</b> (<code>{cid}</code>)\n"
                f"⚫ وضعیت فعلی: آفلاین — به محض آنلاین شدن خودکار ضبط می‌شه.",
                parse_mode="html",
                buttons=[[Button.inline("📋 مشاهده واچ‌لیست", data="page:watchlist:0")]],
            )

    async def _handle_manual_rec(self, event: Any, raw: str) -> None:
        wait_msg = await event.respond("🔍 در حال بررسی آنلاین بودن استریمر...")
        cid, _, dname, _ = await self.api.resolve_creator(raw)
        info = await self.api.check_creator_and_get_stream(cid or raw)
        if not info.is_live:
            await wait_msg.edit(
                f"⚫ استریمر <b>{html.escape(info.display_name or dname or cid)}</b> الان آفلاینه.\n"
                f"می‌خوای به واچ‌لیست اضافه کنم تا هر وقت آنلاین شد خودکار ضبط بشه؟",
                parse_mode="html",
                buttons=[[Button.inline("➕ اضافه به واچ‌لیست", data=f"qwatch:{info.creator_id}")]],
            )
            return
        ok, msg_text = await self.rec_mgr.start_recording(info, chat_id=event.chat_id, triggered_by="manual")
        await wait_msg.edit(msg_text, parse_mode="html")

    async def _send_watchlist_view(self, event: Any, page: int = 0, edit: bool = False) -> None:
        items = self.db.list_watchlist()
        per_page = 6
        total_pages = max(1, (len(items) + per_page - 1) // per_page)
        page = max(0, min(page, total_pages - 1))
        chunk = items[page * per_page : (page + 1) * per_page]

        lines = [f"📋 <b>واچ‌لیست شخصی (نامحدود) — صفحه {page+1}/{total_pages}</b>\n"]
        buttons: List[List[Any]] = []
        if not chunk:
            lines.append("هنوز کسی توی واچ‌لیست نیست. با `/watch ID` یا دکمه پایین اضافه کن.")
        else:
            for item in chunk:
                cid = item["creator_id"]
                name = html.escape(item["display_name"] or cid)
                is_rec = self.rec_mgr.is_recording(cid)
                st = "⏺ در حال ضبط" if is_rec else ("🔴 آنلاین" if item["last_status"] == "live" else "⚫ آفلاین")
                auto = "✅" if item["auto_record"] else "⏸"
                lines.append(f"• <b>{name}</b> (<code>{cid}</code>) — {st} | خودکار: {auto} | ضبط‌ها: {item['total_recordings']}")
                buttons.append(
                    [
                        Button.inline(f"🎬 ضبط {item['display_name'][:12] or cid}", data=f"qrec:{cid}"),
                        Button.inline(f"{'⏸' if item['auto_record'] else '▶️'} خودکار", data=f"toggle:{cid}"),
                        Button.inline("🗑 حذف", data=f"rmwatch:{cid}"),
                    ]
                )

        nav: List[Any] = []
        if page > 0:
            nav.append(Button.inline("⬅️ قبلی", data=f"page:watchlist:{page-1}"))
        if page < total_pages - 1:
            nav.append(Button.inline("بعدی ➡️", data=f"page:watchlist:{page+1}"))
        if nav:
            buttons.append(nav)
        buttons.append([Button.inline("➕ افزودن جدید", data="act:add_watch"), Button.inline("🏠 منو", data="page:home")])

        text = "\n".join(lines)
        if edit:
            await event.edit(text, parse_mode="html", buttons=buttons)
        else:
            await event.reply(text, parse_mode="html", buttons=buttons)

    async def _send_active_view(self, event: Any, edit: bool = False) -> None:
        actives = list(self.rec_mgr.active.values())
        lines = [f"🔴 <b>ضبط‌های فعال الان ({len(actives)})</b>\n"]
        buttons: List[List[Any]] = []
        if not actives:
            lines.append("در حال حاضر هیچ استریمی در حال ضبط نیست.")
        else:
            for rec in actives:
                elapsed = fmt_duration(time.time() - rec.started_at)
                lines.append(
                    f"• <b>{html.escape(rec.creator_name)}</b> (<code>{rec.creator_id}</code>)\n"
                    f"  ⏱ {elapsed} | 💾 {fmt_bytes(rec.bytes_written)} | پارت {rec.part_number}\n"
                    f"  📌 {html.escape(rec.status_text)}"
                )
                buttons.append([Button.inline(f"⏹ توقف و ارسال {rec.creator_name[:16]}", data=f"stop:{rec.creator_id}")])

        buttons.append([Button.inline("🔄 بروزرسانی", data="page:active"), Button.inline("🏠 منو", data="page:home")])
        text = "\n".join(lines)
        if edit:
            await event.edit(text, parse_mode="html", buttons=buttons)
        else:
            await event.reply(text, parse_mode="html", buttons=buttons)

    async def _send_archive_view(self, event: Any, page: int = 0, edit: bool = False) -> None:
        per_page = 8
        rows = self.db.list_recent_recordings(limit=per_page, offset=page * per_page)
        lines = [f"📚 <b>آرشیو ضبط‌شده‌ها — صفحه {page+1}</b>\n"]
        if not rows:
            lines.append("هنوز ویدیویی در آرشیو ثبت نشده.")
        else:
            for r in rows:
                icon = "✅" if r["status"] == "completed" else ("🔴" if r["status"] == "recording" else "⚠️")
                dt = (r["started_at"] or "")[:16].replace("T", " ")
                lines.append(
                    f"{icon} <b>{html.escape(r['creator_name'] or r['creator_id'])}</b> — "
                    f"<code>{fmt_duration(r['duration_sec'])}</code> | <code>{fmt_bytes(r['file_size_bytes'])}</code>\n"
                    f"   📅 {dt} | وضعیت: <code>{r['status']}</code>"
                )
        nav: List[Any] = []
        if page > 0:
            nav.append(Button.inline("⬅️ قبلی", data=f"page:archive:{page-1}"))
        if len(rows) == per_page:
            nav.append(Button.inline("بعدی ➡️", data=f"page:archive:{page+1}"))
        buttons: List[List[Any]] = []
        if nav:
            buttons.append(nav)
        buttons.append([Button.inline("🏠 منو", data="page:home")])
        text = "\n".join(lines)
        if edit:
            await event.edit(text, parse_mode="html", buttons=buttons)
        else:
            await event.reply(text, parse_mode="html", buttons=buttons)

    async def _send_settings_view(self, event: Any, edit: bool = False) -> None:
        sheet = self.db.get_setting("contact_sheet", "1") == "1"
        notify = self.db.get_setting("notify_live", "1") == "1"
        auto_del = self.db.get_setting("auto_delete", "1") == "1"
        poll = self.db.get_setting("poll_interval", str(DEFAULT_POLL_INTERVAL))
        conc = self.db.get_setting("max_concurrent", str(DEFAULT_MAX_CONCURRENT))
        arch = self.db.get_setting("archive_chat_id", "تنظیم نشده")

        text = (
            "⚙️ <b>تنظیمات SuperLiveRec</b>\n\n"
            f"🖼 ارسال Contact Sheet (پیش‌نمایش ۴×۳): {'✅ روشن' if sheet else '❌ خاموش'}\n"
            f"🔔 اعلان شروع ضبط: {'✅ روشن' if notify else '❌ خاموش'}\n"
            f"🧹 حذف خودکار فایل از دیسک بعد از آپلود: {'✅ روشن' if auto_del else '❌ خاموش'}\n"
            f"⏱ بازه چک کردن واچ‌لیست: <code>{poll} ثانیه</code>\n"
            f"⚡ سقف ضبط هم‌زمان: <code>{'نامحدود' if conc == '0' else conc}</code>\n"
            f"📦 کانال آرشیو: <code>{html.escape(arch)}</code>\n\n"
            "• تنظیم کانال آرشیو: <code>/setarchive -100...</code>\n"
            "• لاگین اکانت SuperLive (اختیاری): <code>/login email password</code>"
        )
        buttons = [
            [
                Button.inline(f"🖼 Contact Sheet: {'ON' if sheet else 'OFF'}", data="set:sheet"),
                Button.inline(f"🔔 اعلان لایو: {'ON' if notify else 'OFF'}", data="set:notify"),
            ],
            [
                Button.inline(f"🧹 پاکسازی دیسک: {'ON' if auto_del else 'OFF'}", data="set:autodel"),
                Button.inline(f"⏱ بازه چک ({poll}s)", data="set:poll"),
            ],
            [
                Button.inline(f"⚡ هم‌زمان ({'∞' if conc == '0' else conc})", data="set:conc"),
                Button.inline("🏠 منو", data="page:home"),
            ],
        ]
        if edit:
            await event.edit(text, parse_mode="html", buttons=buttons)
        else:
            await event.reply(text, parse_mode="html", buttons=buttons)

    async def _handle_setting_toggle(self, event: Any, key: str) -> None:
        if key == "sheet":
            cur = self.db.get_setting("contact_sheet", "1")
            self.db.set_setting("contact_sheet", "0" if cur == "1" else "1")
        elif key == "notify":
            cur = self.db.get_setting("notify_live", "1")
            self.db.set_setting("notify_live", "0" if cur == "1" else "1")
        elif key == "autodel":
            cur = self.db.get_setting("auto_delete", "1")
            self.db.set_setting("auto_delete", "0" if cur == "1" else "1")
        elif key == "poll":
            opts = ["15", "25", "45", "60"]
            cur = self.db.get_setting("poll_interval", str(DEFAULT_POLL_INTERVAL))
            nxt = opts[(opts.index(cur) + 1) % len(opts)] if cur in opts else "25"
            self.db.set_setting("poll_interval", nxt)
        elif key == "conc":
            opts = ["4", "8", "15", "0"]
            cur = self.db.get_setting("max_concurrent", str(DEFAULT_MAX_CONCURRENT))
            nxt = opts[(opts.index(cur) + 1) % len(opts)] if cur in opts else "8"
            self.db.set_setting("max_concurrent", nxt)
        await event.answer("تنظیمات ذخیره شد ✅")
        await self._send_settings_view(event, edit=True)

    async def _start_health_server(self) -> Optional[web.AppRunner]:
        port_raw = os.environ.get("PORT", "").strip()
        if not port_raw or not port_raw.isdigit():
            return None
        port = int(port_raw)
        app = web.Application()

        async def handle_health(_request: Any) -> web.Response:
            _, free_mb, _ = get_disk_stats()
            return web.json_response(
                {
                    "status": "ok",
                    "service": "SuperLiveRec Personal Unlimited",
                    "active_recordings": len(self.rec_mgr.active),
                    "watchlist_count": len(self.db.list_watchlist()),
                    "free_disk_mb": round(free_mb, 1),
                    "agora_ready": AGORA_AVAILABLE,
                }
            )

        app.router.add_get("/", handle_health)
        app.router.add_get("/health", handle_health)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        log.info("Healthcheck HTTP server listening on port %d", port)
        return runner

    async def run(self) -> None:
        # Clean up stale interrupted recordings & orphan temp directories from prior restarts
        stale = self.db.mark_stale_recordings_interrupted()
        if stale:
            log.info("Marked %d stale recordings as interrupted.", stale)
        if self.db.get_setting("auto_delete", "1") == "1":
            for child in RECORDINGS_DIR.iterdir():
                try:
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink(missing_ok=True)
                except Exception:
                    pass

        self._register_handlers()
        health_runner = await self._start_health_server()
        log.info("Starting SuperLiveRec Personal Unlimited Bot (Telethon MTProto)...")
        await self.bot.start(bot_token=BOT_TOKEN)
        me = await self.bot.get_me()
        log.info("Logged in as @%s (id=%s) | FFmpeg=%s | Agora=%s", me.username, me.id, FFMPEG_BIN, AGORA_AVAILABLE)
        self.monitor.start()
        try:
            await self.bot.run_until_disconnected()
        finally:
            await self.monitor.stop()
            await self.rec_mgr.stop_all()
            await self.api.close()
            if health_runner:
                await health_runner.cleanup()


async def main() -> None:
    app = SuperLiveRecBot()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(app.bot.disconnect()))
        except NotImplementedError:
            pass
    await app.run()


if __name__ == "__main__":
    asyncio.run(main())
