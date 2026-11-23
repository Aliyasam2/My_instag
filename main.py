python
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
import base64
import datetime
import struct
import zlib
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

# Set BOT_TOKEN in the server environment; never commit a live token.
BOT_TOKEN = os.environ.get(
    "BOT_TOKEN",
    "8837565058:AAFDtM7wJRI2AOgV67dPDdK-206STorQ1Ew",
).strip()

# Telethon MTProto credentials (needed for 2 GB uploads without 50 MB Bot API limit)
# You can set your own from https://my.telegram.org via TG_API_ID / TG_API_HASH env vars
TG_API_ID = int(os.environ.get("TG_API_ID", "2040"))
TG_API_HASH = os.environ.get("TG_API_HASH", "b18441a1ff607e10a989891a5462e627").strip()

# Lock bot to the owner's Telegram numeric user ID. Override via OWNER_ID if needed.
OWNER_ID = int(os.environ.get("OWNER_ID", "404470349"))

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
VIDEO_STALL_TIMEOUT = int(os.environ.get("VIDEO_STALL_TIMEOUT", "12"))
# Watchlist check interval (seconds)
DEFAULT_POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "25"))
# Resource safeguard; actual concurrency is also limited by available SuperLive accounts.
DEFAULT_MAX_CONCURRENT = max(1, int(os.environ.get("MAX_CONCURRENT_RECORDINGS", "2")))

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
    # Direct livestream page link: https://superlive.chat/livestream/155964343
    ls_match = re.search(r"/livestream/(\d{4,16})", text)
    if ls_match:
        return f"ls:{ls_match.group(1)}"
    if text.lower().startswith("ls:") and text[3:].isdigit():
        return text.lower()
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


try:
    from zoneinfo import ZoneInfo  # type: ignore
    LOCAL_TZ: Any = ZoneInfo(os.environ.get("TZ_NAME", "Asia/Tehran"))
except Exception:
    LOCAL_TZ = datetime.timezone.utc

_BG_TASKS: set = set()


def bg_task(coro: Any) -> "asyncio.Task":
    t = asyncio.create_task(coro)
    _BG_TASKS.add(t)
    t.add_done_callback(_BG_TASKS.discard)
    return t


def schedule_delete(msg: Any, delay: float) -> None:
    """Delete a transient Telegram message after `delay` seconds (keeps the chat tidy)."""
    if msg is None:
        return

    async def _d() -> None:
        try:
            await asyncio.sleep(delay)
            await msg.delete()
        except Exception:
            pass

    bg_task(_d())


def local_now_str(fmt: str = "%Y-%m-%d · %H:%M") -> str:
    return datetime.datetime.now(LOCAL_TZ).strftime(fmt)


def iso_to_local(iso: str, fmt: str = "%m-%d %H:%M") -> str:
    if not iso:
        return "-"
    try:
        dt = datetime.datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(LOCAL_TZ).strftime(fmt)
    except Exception:
        return iso[:16].replace("T", " ")


def is_h264_keyframe(data: bytes) -> bool:
    """True if an Annex-B H.264 access unit contains an IDR slice or SPS."""
    try:
        n = len(data)
        i = data.find(b"\x00\x00\x01")
        checks = 0
        while 0 <= i < n - 3 and checks < 64:
            t = data[i + 3] & 0x1F
            if t in (5, 7):
                return True
            if t == 1:
                return False
            i = data.find(b"\x00\x00\x01", i + 3)
            checks += 1
    except Exception:
        pass
    return False


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r"[^\w\-.]+", "_", name.strip())
    return cleaned[:48] or "creator"


def decode_agora_token_007(token: str) -> Dict[str, Any]:
    """Extract app_id, channel and uid embedded in an Agora AccessToken2 (007...)."""
    if not isinstance(token, str) or not token.startswith("007"):
        return {}
    try:
        raw = token[3:]
        raw += "=" * (-len(raw) % 4)
        buf = zlib.decompress(base64.b64decode(raw))
        pos = 0

        def u16() -> int:
            nonlocal pos
            v = struct.unpack_from("<H", buf, pos)[0]
            pos += 2
            return v

        def u32() -> int:
            nonlocal pos
            v = struct.unpack_from("<I", buf, pos)[0]
            pos += 4
            return v

        def rstr() -> bytes:
            nonlocal pos
            n = u16()
            v = buf[pos:pos + n]
            pos += n
            return v

        rstr()  # signature
        out: Dict[str, Any] = {"app_id": rstr().decode("utf-8", "ignore")}
        out["issue_ts"] = u32()
        out["expire"] = u32()
        u32()  # salt
        for _ in range(u16()):
            stype = u16()
            for _ in range(u16()):
                u16()
                u32()
            if stype == 1:  # RTC service
                out["channel"] = rstr().decode("utf-8", "ignore")
                out["uid"] = rstr().decode("utf-8", "ignore")
                break
            # Non-RTC services: RTM(2)/Streaming etc. carry one string (user_id) in most versions
            rstr()
        return out
    except Exception as e:
        return {"error": str(e)}


# ==============================================================================
# DATABASE LAYER (SQLite — Watchlist, Archive, Settings, SuperLive Session)
# ==============================================================================

class Database:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        # Single persistent connection (the old code leaked one connection per query)
        if getattr(self, "_db", None) is None:
            self._db = sqlite3.connect(str(self.path), check_same_thread=False)
            self._db.row_factory = sqlite3.Row
        return self._db

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

                CREATE TABLE IF NOT EXISTS clips (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rec_id INTEGER DEFAULT 0,
                    creator_id TEXT NOT NULL,
                    creator_name TEXT DEFAULT '',
                    part_no INTEGER DEFAULT 1,
                    created_at TEXT NOT NULL,
                    duration_sec INTEGER DEFAULT 0,
                    size_bytes INTEGER DEFAULT 0,
                    chat_id INTEGER DEFAULT 0,
                    msg_id INTEGER DEFAULT 0,
                    sheet_msg_id INTEGER DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_clips_creator ON clips (creator_id, id);
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

    # --- Clips (every uploaded video part) ---
    def add_clip(self, rec_id: int, creator_id: str, creator_name: str, part_no: int,
                 duration_sec: int, size_bytes: int, chat_id: int, msg_id: int, sheet_msg_id: int = 0) -> None:
        now = datetime.datetime.utcnow().isoformat()
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO clips (rec_id, creator_id, creator_name, part_no, created_at, duration_sec, "
                "size_bytes, chat_id, msg_id, sheet_msg_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (rec_id, creator_id, creator_name, part_no, now, duration_sec, size_bytes, chat_id, msg_id, sheet_msg_id),
            )

    def list_clips(self, creator_id: str = "", limit: int = 8, offset: int = 0) -> List[Dict[str, Any]]:
        with self._lock, self._conn() as conn:
            if creator_id:
                rows = conn.execute(
                    "SELECT * FROM clips WHERE creator_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
                    (creator_id, limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM clips ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
                ).fetchall()
            return [dict(r) for r in rows]

    def clip_stats(self, creator_id: str = "") -> Tuple[int, int, int]:
        with self._lock, self._conn() as conn:
            q = "SELECT COUNT(*) c, COALESCE(SUM(duration_sec),0) d, COALESCE(SUM(size_bytes),0) s FROM clips"
            row = conn.execute(q + (" WHERE creator_id = ?" if creator_id else ""),
                               ((creator_id,) if creator_id else ())).fetchone()
            return int(row["c"]), int(row["d"]), int(row["s"])

    def get_clip(self, clip_id: int) -> Optional[Dict[str, Any]]:
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
            return dict(row) if row else None

    def migrate_watch(self, old_id: str, new_id: str) -> None:
        if not new_id or old_id == new_id:
            return
        with self._lock, self._conn() as conn:
            if conn.execute("SELECT 1 FROM watchlist WHERE creator_id = ?", (new_id,)).fetchone():
                conn.execute("DELETE FROM watchlist WHERE creator_id = ?", (old_id,))
            else:
                conn.execute("UPDATE watchlist SET creator_id = ? WHERE creator_id = ?", (new_id, old_id))

    def increment_recordings(self, creator_id: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("UPDATE watchlist SET total_recordings = total_recordings + 1 WHERE creator_id = ?", (creator_id,))

    def clip_creators(self, limit: int = 10, offset: int = 0) -> List[Dict[str, Any]]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT creator_id, MAX(creator_name) AS creator_name, COUNT(*) AS cnt, "
                "SUM(duration_sec) AS dur, SUM(size_bytes) AS size, MAX(id) AS last_id "
                "FROM clips GROUP BY creator_id ORDER BY last_id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return [dict(r) for r in rows]

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


class AccountPool:
    """Several SuperLive accounts; each one can watch exactly ONE livestream at a time."""
    def __init__(self, db: "Database"):
        self.db = db
        raw = db.get_setting("account_slots", "0")
        slots = sorted({int(x) for x in raw.split(",") if x.strip().isdigit()} | {0})
        self.clients: Dict[int, "SuperLiveClient"] = {n: SuperLiveClient(db, n) for n in slots}

    def _save(self) -> None:
        self.db.set_setting("account_slots", ",".join(str(n) for n in sorted(self.clients)))

    @property
    def primary(self) -> "SuperLiveClient":
        return self.clients[0]

    def add(self, token: str, device_id: str = "", label: str = "") -> "SuperLiveClient":
        n = 1
        while n in self.clients:
            n += 1
        c = SuperLiveClient(self.db, n)
        c.set_credentials(token, device_id)
        if label:
            c.label = label
            self.db.set_setting(c._k("superlive_label"), label)
        self.clients[n] = c
        self._save()
        return c

    def new_empty(self) -> "SuperLiveClient":
        n = 1
        while n in self.clients:
            n += 1
        c = SuperLiveClient(self.db, n)
        self.clients[n] = c
        self._save()
        return c

    def remove(self, slot: int) -> bool:
        if slot == 0 or slot not in self.clients:
            return False
        c = self.clients.pop(slot)
        c.wipe()
        self._save()
        bg_task(c.close())
        return True

    def usable(self) -> List["SuperLiveClient"]:
        return [c for _, c in sorted(self.clients.items()) if c.has_token()]

    async def close(self) -> None:
        for c in self.clients.values():
            await c.close()


class SuperLiveClient:
    def __init__(self, db: Database, slot: int = 0):
        self.db = db
        self.slot = slot
        self._session: Optional[aiohttp.ClientSession] = None
        env_tok = os.environ.get("SUPERLIVE_TOKEN", "").strip() if slot == 0 else ""
        env_dev = os.environ.get("SUPERLIVE_DEVICE_ID", "").strip() if slot == 0 else ""
        self._token: str = env_tok or db.get_setting(self._k("superlive_token"), "")
        self._device_id: str = env_dev or db.get_setting(self._k("superlive_device_id"), "") or uuid.uuid4().hex
        self._auth_scheme: str = db.get_setting(self._k("superlive_auth_scheme"), "Token")
        self._rtc_id: str = (
            (os.environ.get("SUPERLIVE_RTC_ID", "").strip() if slot == 0 else "")
            or db.get_setting(self._k("superlive_rtc_id"), "")
            or str(random.randint(1000000000, 4200000000))
        )
        if not db.get_setting(self._k("superlive_device_id"), ""):
            db.set_setting(self._k("superlive_device_id"), self._device_id)
        if not db.get_setting(self._k("superlive_rtc_id"), ""):
            db.set_setting(self._k("superlive_rtc_id"), self._rtc_id)
        self.label: str = db.get_setting(self._k("superlive_label"), "") or f"اکانت {slot + 1}"

    def _k(self, key: str) -> str:
        return key if self.slot == 0 else f"acc{self.slot}_{key}"

    def has_token(self) -> bool:
        return bool(self._token)

    def wipe(self) -> None:
        for k in ("superlive_token", "superlive_device_id", "superlive_auth_scheme", "superlive_rtc_id", "superlive_label"):
            self.db.set_setting(self._k(k), "")

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
        self.db.set_setting(self._k("superlive_token"), self._token)
        if device_id.strip():
            self._device_id = device_id.strip()
            self.db.set_setting(self._k("superlive_device_id"), self._device_id)

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
                self.db.set_setting(self._k("superlive_auth_scheme"), alt_scheme)
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
            self.db.set_setting(self._k("superlive_token"), self._token)
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
            self.db.set_setting(self._k("superlive_token"), self._token)
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

        # 0) Livestream link -> ask SuperLive who owns this livestream
        if ident.startswith("ls:"):
            ls_id = ident[3:]
            ret = await self._request_smart("livestream/retrieve", {"livestream_id": ls_id})
            owner = self._deep_find_keys(ret, ("livestream_owner_id",)).get("livestream_owner_id")
            u = ret.get("user") if isinstance(ret.get("user"), dict) else {}
            if not u:
                lsobj = ret.get("livestream") if isinstance(ret.get("livestream"), dict) else {}
                u = lsobj.get("user") if isinstance(lsobj.get("user"), dict) else {}
            cid = str(owner or u.get("user_id") or u.get("id") or "")
            if cid:
                uname = str(u.get("username") or u.get("shared_id") or "")
                dname = str(u.get("name") or uname or cid)
                return cid, uname, dname, self._extract_avatar(u) if u else ""
            log.warning("Could not resolve livestream %s owner: %s", ls_id, json.dumps(ret, ensure_ascii=False)[:300])
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

    def _build_agora_uri(
        self,
        payload: Dict[str, Any],
        ls_id: str = "",
        debug_out: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Construct agora://<app_id>/<channel>?token=<tok>&uid=<uid>&ls_id=<ls_id>
        Safely distinguishes Agora RTC (video/audio) tokens from RTM (chat) tokens,
        and extracts the embedded 32-hex App ID from 006... tokens automatically.
        """
        flat: Dict[str, Any] = {}
        rtc_tokens: List[Tuple[str, str]] = []
        any_tokens: List[Tuple[str, str]] = []
        agora_key_summary: List[str] = []

        stack: List[Tuple[str, Any]] = [("", payload)]
        while stack:
            prefix, cur = stack.pop()
            if isinstance(cur, dict):
                for k, v in cur.items():
                    kl = str(k).lower()
                    full_k = f"{prefix}.{k}" if prefix else str(k)
                    if isinstance(v, (dict, list)):
                        stack.append((full_k, v))
                    else:
                        if any(w in kl for w in ("agora", "rtc", "rtm", "channel", "token", "stream", "uid")):
                            v_preview = f"len={len(v)}:{v[:6]}.." if isinstance(v, str) and len(v) > 24 else str(v)
                            agora_key_summary.append(f"{k}={v_preview}")
                        if isinstance(v, str) and len(v) >= 50 and v.startswith(("006", "007")):
                            if "rtm" not in kl and "chat" not in kl and "msg" not in kl:
                                rtc_tokens.append((kl, v))
                            any_tokens.append((kl, v))
                        if kl not in flat and v not in (None, ""):
                            flat[kl] = v
            elif isinstance(cur, list):
                for idx, item in enumerate(cur[:40]):
                    if isinstance(item, (dict, list)):
                        stack.append((f"{prefix}[{idx}]", item))
                    elif isinstance(item, str) and len(item) >= 50 and item.startswith(("006", "007")):
                        any_tokens.append((prefix.lower(), item))

        if debug_out is not None:
            debug_out["agora_keys"] = ", ".join(agora_key_summary[:18])

        # Prefer explicit RTC/livestream channel keys (avoid chat/rtm channels)
        channel = (
            flat.get("agora_channel_name")
            or flat.get("agora_channel")
            or flat.get("rtc_channel_name")
            or flat.get("rtc_channel")
            or flat.get("channel_name")
            or flat.get("channel_id")
            or flat.get("channel")
            or ls_id
        )

        # Pick RTC token (strictly preferring non-RTM tokens)
        token = ""
        for pref_k in (
            "agora_channel_token",
            "agora_token",
            "agora_rtc_token",
            "rtc_token",
            "viewer_token",
            "audience_token",
            "subscriber_token",
            "channel_token",
            "stream_token",
        ):
            val = flat.get(pref_k)
            if isinstance(val, str) and len(val) >= 30:
                token = val
                break
        if not token and rtc_tokens:
            token = rtc_tokens[0][1]
        if not token and any_tokens:
            token = any_tokens[0][1]
        if not token:
            for k, v in flat.items():
                if "token" in k and "rtm" not in k and isinstance(v, str) and len(v) >= 30:
                    token = v
                    break

        # Auto-extract exact 32-hex Agora App ID from 006... tokens if present
        app_id = flat.get("agora_app_id") or flat.get("app_id") or DEFAULT_AGORA_APP_ID
        if isinstance(token, str) and token.startswith("006") and len(token) >= 35:
            cand_app = token[3:35]
            if re.fullmatch(r"[0-9a-fA-F]{32}", cand_app):
                app_id = cand_app

        # Build prioritized candidate UIDs (explicit API uid, self._rtc_id, "0")
        explicit_uid = (
            flat.get("agora_id")
            or flat.get("agora_uid")
            or flat.get("agora_user_id")
            or flat.get("rtc_uid")
            or flat.get("viewer_uid")
            or flat.get("rtc_id")
            or ""
        )

        # AccessToken2 (007) embeds the exact app_id / channel / uid it was issued for -> trust it
        tok_info = decode_agora_token_007(token)
        if debug_out is not None:
            debug_out["token_info"] = str({k: v for k, v in tok_info.items()})
        if tok_info.get("app_id") and re.fullmatch(r"[0-9a-fA-F]{32}", str(tok_info["app_id"])):
            app_id = tok_info["app_id"]
        if tok_info.get("channel"):
            channel = tok_info["channel"]
        if "uid" in tok_info:
            # empty uid in token = any uid accepted
            explicit_uid = tok_info["uid"] or explicit_uid

        uid_list: List[str] = []
        for u in (str(explicit_uid), str(self._rtc_id), "0"):
            u = u.strip()
            if u and u not in uid_list:
                uid_list.append(u)

        # Also store any alternate non-RTM token just in case
        alt_tokens = [t for _, t in rtc_tokens if t != token]

        if channel and (token or str(channel) != str(ls_id)):
            qs_dict = {
                "token": str(token),
                "uid": uid_list[0] if uid_list else "0",
                "uids": ",".join(uid_list),
                "ls_id": str(ls_id or channel),
            }
            if alt_tokens:
                qs_dict["alt_token"] = alt_tokens[0]
            qs = urllib.parse.urlencode(qs_dict)
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
        self, creator_id: str, discover_map: Optional[Dict[str, Dict[str, Any]]] = None, light: bool = False
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
        cache_ls_id = ls_id
        cache_name = info.display_name
        if info.creator_id and info.creator_id != target_uid:
            target_uid = info.creator_id  # discover gave us the real internal user_id

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
        # SuperLive may ignore user_id and return the logged-in account's own profile -> discard it
        if user_obj and str(user_obj.get("user_id") or "") not in ("", target_uid, str(creator_id)) \
                and str(user_obj.get("shared_id") or "") != str(creator_id):
            info.raw_debug["profile_mismatch"] = f"got {user_obj.get('user_id')} for {target_uid}"
            user_obj = {}
            prof_resp = {"_http_status": prof_resp.get("_http_status"), "_ignored": "own profile"}
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
            or cache_ls_id
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
            for page_idx in range(8):
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

        if not info.is_live or light:
            # light=True: only liveness; does NOT enter the room (entering would kick this
            # account out of whatever livestream it is currently recording)
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

        owner = self._deep_find_keys(ret_resp, ("livestream_owner_id",)).get("livestream_owner_id")
        if owner:
            info.creator_id = str(owner)
        if cache_name and not info.display_name:
            info.display_name = cache_name
        if isinstance(ret_resp, dict) and isinstance(ret_resp.get("user"), dict):
            ru = ret_resp["user"]
            if ru.get("name"):
                info.display_name = str(ru["name"])
            if ru.get("user_id"):
                info.creator_id = str(ru["user_id"])

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
            agora_uri = self._build_agora_uri(merged, ls_id=ls_id, debug_out=info.raw_debug)
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
            "-af", "aresample=async=1",
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
    if make_sheet and duration_sec >= 3:
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

    def on_encoded_video_frame(self, *args: Any, **kwargs: Any) -> int:
        # SDK versions differ: (uid, buf, len, info) or (observer, uid, buf, len, info).
        # Find the bytes-like buffer and the int length that follows it, whatever the order.
        try:
            buf = kwargs.get("image_buffer")
            length = kwargs.get("length")
            if buf is None:
                vals = list(args)
                for i, a in enumerate(vals):
                    if isinstance(a, (bytes, bytearray, memoryview)) or (
                        hasattr(a, "__len__") and not isinstance(a, str) and hasattr(a, "__getitem__") and len(a) > 16
                    ):
                        buf = a
                        nxt = vals[i + 1] if i + 1 < len(vals) else None
                        length = nxt if isinstance(nxt, int) and nxt > 0 else len(a)
                        break
            if buf is not None:
                self.receiver.handle_video_frame(buf, int(length or len(buf)))
            elif not self.receiver.cb_error:
                self.receiver.cb_error = "video_cb_args=" + ",".join(type(a).__name__ for a in args)
        except Exception as e:
            if not self.receiver.cb_error:
                self.receiver.cb_error = f"video_cb:{e}"
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
        self.receiver.event_times.append((time.time(), "drop"))

    def on_connecting(self, *args: Any, **kwargs: Any) -> None:
        self.receiver.conn_events.append("connecting")

    def on_user_joined(self, *args: Any, **kwargs: Any) -> None:
        uid = args[-1] if args else kwargs.get("user_id", "?")
        self.receiver.conn_events.append(f"joined({uid})")

    def on_connection_failure(self, *args: Any, **kwargs: Any) -> None:
        reason = args[-1] if args else kwargs.get("reason", "?")
        self.receiver.conn_events.append(f"fail({reason})")
        self.receiver.event_times.append((time.time(), "drop"))


class AgoraStreamReceiver:
    """Captures raw H.264 video AND raw PCM audio from an Agora RTC channel."""
    def __init__(self, agora_uri: str, h264_path: Path, pcm_path: Path):
        parsed = urllib.parse.urlparse(agora_uri)
        qs = urllib.parse.parse_qs(parsed.query)
        self.app_id = parsed.netloc or DEFAULT_AGORA_APP_ID
        self.channel_name = parsed.path.lstrip("/")
        self.token = (qs.get("token") or [""])[0]
        self.alt_token = (qs.get("alt_token") or [""])[0]
        self.uid = (qs.get("uid") or ["0"])[0]
        raw_uids = (qs.get("uids") or [self.uid])[0]
        self.uid_candidates: List[str] = [u.strip() for u in raw_uids.split(",") if u.strip()] or [self.uid, "0"]
        if "0" not in self.uid_candidates:
            self.uid_candidates.append("0")

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
        self.cb_error: str = ""
        # Per-segment counters (a segment = one uploaded file; connection stays open across segments)
        self.seg_video_frames = 0
        self.seg_audio_frames = 0
        self.seg_bytes = 0
        self.seg_started_at = time.time()
        self._need_keyframe = True
        self.last_video_at = 0.0
        self.event_times: List[Tuple[float, str]] = []

    def handle_video_frame(self, image_buffer: Any, length: int) -> None:
        if self._closed or image_buffer is None or not isinstance(length, int) or length <= 0:
            return
        try:
            data = bytes(image_buffer[:length]) if not isinstance(image_buffer, (bytes, bytearray)) else bytes(image_buffer[:length])
            with self._io_lock:
                if self._closed:
                    return
                self.last_frame_at = time.time()
                if self._need_keyframe:
                    # Each segment must start on a keyframe, otherwise the file starts black/garbled
                    if not is_h264_keyframe(data):
                        return
                    self._need_keyframe = False
                    self.seg_started_at = time.time()
                self.last_video_at = time.time()
                self._h264_fh.write(data)
                self.video_frames += 1
                self.seg_video_frames += 1
                self.bytes_written += len(data)
                self.seg_bytes += len(data)
        except Exception as e:
            if not self.cb_error:
                self.cb_error = f"video_write:{e}"

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
                if self._closed:
                    return
                # The SDK keeps firing playback callbacks with SILENCE after the connection drops,
                # so audio is only written while video is actually arriving (A/V stay the same length).
                if self._need_keyframe or time.time() - self.last_video_at > 3.0:
                    return
                self._pcm_fh.write(data)
                self.audio_frames += 1
                self.seg_audio_frames += 1
                self.bytes_written += len(data)
                self.seg_bytes += len(data)
        except Exception:
            pass

    def rotate(self, new_h264: Path, new_pcm: Path) -> Dict[str, Any]:
        """Close current segment files and continue writing into new ones WITHOUT disconnecting."""
        with self._io_lock:
            old = {
                "h264": self.h264_path, "pcm": self.pcm_path,
                "video_frames": self.seg_video_frames, "audio_frames": self.seg_audio_frames,
                "elapsed": max(1.0, time.time() - self.seg_started_at),
                "sample_rate": self.sample_rate, "channels": self.channels,
            }
            for fh in (self._h264_fh, self._pcm_fh):
                try:
                    fh.flush()
                    fh.close()
                except Exception:
                    pass
            self.h264_path, self.pcm_path = new_h264, new_pcm
            self._h264_fh = open(new_h264, "wb")
            self._pcm_fh = open(new_pcm, "wb")
            self.seg_video_frames = self.seg_audio_frames = self.seg_bytes = 0
            self.seg_started_at = time.time()
            self._need_keyframe = True
        return old

    def segment_snapshot(self) -> Dict[str, Any]:
        with self._io_lock:
            return {
                "h264": self.h264_path, "pcm": self.pcm_path,
                "video_frames": self.seg_video_frames, "audio_frames": self.seg_audio_frames,
                "elapsed": max(1.0, time.time() - self.seg_started_at),
                "sample_rate": self.sample_rate, "channels": self.channels,
            }

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
    triggered_by: str = "watchlist"
    started_at: float = field(default_factory=time.time)
    seg_started_at: float = field(default_factory=time.time)
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    split_event: asyncio.Event = field(default_factory=asyncio.Event)
    status_text: str = "در حال اتصال..."
    bytes_written: int = 0
    part_number: int = 1
    task: Optional[asyncio.Task] = None
    agora_diag: str = ""
    manual_stop: bool = False
    uploads_pending: int = 0
    upload_status: str = ""
    uploaded_parts: int = 0
    uploaded_duration: int = 0
    uploaded_bytes: int = 0
    connected: bool = False
    client: Any = None


FAILED_DIR = BASE_DIR / "failed_uploads"
FAILED_DIR.mkdir(parents=True, exist_ok=True)


class RecordingManager:
    def __init__(self, bot: TelegramClient, db: Database, pool: "AccountPool"):
        self.bot = bot
        self.db = db
        self.pool = pool
        self.api = pool.primary
        self.busy: Dict[int, str] = {}  # account slot -> creator_id
        self.active: Dict[str, ActiveRecording] = {}
        self._fail_cooldown: Dict[str, float] = {}
        # livestream_ids the user stopped manually -> don't auto-restart the same live
        self.manually_stopped_ls: Dict[str, str] = {}
        self._upload_sem = asyncio.Semaphore(int(os.environ.get("MAX_PARALLEL_UPLOADS", "3")))
        self.on_change: Any = None  # callback to refresh UI panels

    def _changed(self) -> None:
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass

    def is_recording(self, creator_id: str) -> bool:
        return str(creator_id) in self.active

    def accounts(self) -> List[SuperLiveClient]:
        return self.pool.usable() or [self.pool.primary]

    def free_account(self) -> Optional[SuperLiveClient]:
        if len(self.active) >= self.max_concurrent():
            return None
        for c in self.accounts():
            if c.slot not in self.busy:
                return c
        return None

    def max_concurrent(self) -> int:
        return min(len(self.accounts()), DEFAULT_MAX_CONCURRENT)

    def has_capacity(self) -> bool:
        return self.free_account() is not None

    def split_seconds(self) -> int:
        raw = self.db.get_setting("split_minutes", "0")
        return int(raw) * 60 if raw.isdigit() else 0

    def part_cap_bytes(self) -> int:
        raw = self.db.get_setting("part_mb", str(MAX_SINGLE_FILE_BYTES // (1024 * 1024)))
        mb = int(raw) if raw.isdigit() else 1900
        return max(50, min(1950, mb)) * 1024 * 1024

    async def start_recording(
        self,
        stream_info: CreatorStreamInfo,
        chat_id: int,
        triggered_by: str = "watchlist",
    ) -> Tuple[bool, str]:
        cid = str(stream_info.creator_id)
        if cid in self.active:
            return False, f"⏺ <b>{html.escape(stream_info.display_name or cid)}</b> همین الان در حال ضبطه."
        if triggered_by == "watchlist":
            if time.time() < self._fail_cooldown.get(cid, 0.0):
                return False, "cooldown"
            if stream_info.livestream_id and self.manually_stopped_ls.get(cid) == stream_info.livestream_id:
                return False, "manually stopped"
        else:
            self.manually_stopped_ls.pop(cid, None)

        client = self.free_account()
        if client is None:
            return False, (f"⚠️ همه {self.max_concurrent()} اکانت SuperLive مشغول ضبطن. "
                           f"از ⚙️ تنظیمات › 👥 اکانتها یه اکانت دیگه اضافه کن.")
        # Reserve the account, then get a fresh token issued for THIS account
        self.busy[client.slot] = cid
        try:
            fresh = await client.check_creator_and_get_stream(cid)
        except Exception as e:
            fresh = None
            log.warning("Fresh stream fetch failed (%s): %s", client.label, e)
        if not fresh or not fresh.is_live or not fresh.stream_url:
            self.busy.pop(client.slot, None)
            if not fresh:
                return False, "⚠️ ارتباط با SuperLive برقرار نشد."
            if not fresh.is_live:
                return False, "⚫ این استریمر الان لایو نیست."
            d = fresh.raw_debug
            reason = ""
            for k in ("enter_sample", "retrieve_sample"):
                m = re.search(r'"disallowed_reason":\s*("?[^,}]*"?)', str(d.get(k, "")))
                if m and m.group(1) not in ("null", '""'):
                    reason = m.group(1).strip('"')
                    break
            if "private" in reason or "private_livestream" in str(d.get("agora_keys", "")):
                return False, ("🔒 این لایو الان <b>خصوصی (Private / پولی)</b> شده و سرور فقط به کسایی که "
                               "پرداخت کردن اجازه ورود میده. وقتی دوباره عمومی بشه میشه ضبطش کرد.")
            return False, (
                "⚠️ لایو روشنه ولی اطلاعات اتصال (Agora) داده نشد.\n"
                f"• Retrieve/Enter: <code>{d.get('retrieve_http', '-')}/{d.get('enter_http', '-')}</code>"
                + (f"\n• دلیل سرور: <code>{html.escape(reason)}</code>" if reason else "")
                + f"\n• کلیدها: <code>{html.escape(str(d.get('agora_keys', '-'))[:250])}</code>"
                + f"\n• اکانت: <code>{html.escape(client.label)}</code>"
            )
        fresh.display_name = fresh.display_name or stream_info.display_name
        stream_info = fresh
        cid = str(stream_info.creator_id or cid)
        if cid in self.active:
            self.busy.pop(client.slot, None)
            return False, "⏺ همین الان در حال ضبطه."
        self.busy[client.slot] = cid
        _, free_mb, _ = get_disk_stats()
        err = ""
        if free_mb < MIN_FREE_DISK_MB:
            err = f"💽 فضای خالی دیسک کمه ({free_mb:.0f} MB)؛ ضبط جدید شروع نشد."
        elif stream_info.stream_url.startswith("agora://") and not AGORA_AVAILABLE:
            err = "پکیج agora-python-server-sdk روی سرور نصب نیست."
        elif not FFMPEG_BIN:
            err = "FFmpeg روی سرور پیدا نشد."
        if err:
            self.busy.pop(client.slot, None)
            return False, err

        cname = stream_info.display_name or stream_info.username or cid
        db_rec_id = self.db.create_recording_record(cid, cname, stream_info.livestream_id)
        rec = ActiveRecording(
            creator_id=cid,
            creator_name=cname,
            livestream_id=stream_info.livestream_id,
            stream_url=stream_info.stream_url,
            db_rec_id=db_rec_id,
            chat_id=chat_id,
            triggered_by=triggered_by,
        )
        rec.client = client
        self.active[cid] = rec
        rec.task = asyncio.create_task(self._run_recording_lifecycle(rec))
        self._changed()
        return True, f"⏺ ضبط <b>{html.escape(cname)}</b> با {html.escape(client.label)} شروع شد."

    async def stop_recording(self, creator_id: str) -> bool:
        rec = self.active.get(str(creator_id))
        if not rec:
            return False
        rec.manual_stop = True
        if rec.livestream_id:
            self.manually_stopped_ls[rec.creator_id] = rec.livestream_id
        rec.status_text = "در حال پایان ضبط و ارسال..."
        rec.stop_event.set()
        self._changed()
        return True

    async def split_recording(self, creator_id: str) -> bool:
        """Send everything recorded so far, keep recording the rest seamlessly."""
        rec = self.active.get(str(creator_id))
        if not rec:
            return False
        rec.split_event.set()
        return True

    async def stop_all(self) -> int:
        count = 0
        for rec in list(self.active.values()):
            rec.stop_event.set()
            count += 1
        return count

    # ------------------------------------------------------------------ lifecycle
    async def _run_recording_lifecycle(self, rec: ActiveRecording) -> None:
        work_dir = RECORDINGS_DIR / f"{sanitize_filename(rec.creator_name)}_{rec.creator_id}_{int(rec.started_at)}"
        work_dir.mkdir(parents=True, exist_ok=True)
        upload_lock = asyncio.Lock()
        upload_tasks: List[asyncio.Task] = []
        upload_failures: List[str] = []
        final_status = "completed"
        notes = ""

        def enqueue(coro_factory: Any) -> None:
            rec.uploads_pending += 1

            async def _job() -> None:
                try:
                    async with upload_lock:  # keeps parts in order
                        await coro_factory()
                except Exception as e:
                    upload_failures.append(str(e)[:250])
                    log.exception("Upload job failed for %s: %s", rec.creator_id, e)
                finally:
                    rec.uploads_pending -= 1
                    self._changed()

            upload_tasks.append(asyncio.create_task(_job()))

        try:
            restarts = 0
            while not rec.stop_event.is_set():
                parts_before = rec.part_number
                if rec.stream_url.startswith("agora://"):
                    await self._agora_session(rec, work_dir, enqueue)
                else:
                    await self._ffmpeg_session(rec, work_dir, enqueue)
                if rec.stop_event.is_set():
                    break
                if rec.part_number > parts_before:
                    restarts = 0
                # Stream went quiet: is the creator really offline or was it a network hiccup?
                try:
                    info = await (rec.client or self.api).check_creator_and_get_stream(rec.creator_id)
                except Exception:
                    info = None
                if info and info.is_live and info.stream_url and restarts < 10:
                    restarts += 1
                    rec.stream_url = info.stream_url
                    rec.livestream_id = info.livestream_id or rec.livestream_id
                    rec.status_text = f"اتصال مجدد ({restarts})..."
                    log.info("Reconnecting %s (attempt %d)", rec.creator_id, restarts)
                    await asyncio.sleep(min(10, 2 + restarts))
                    continue
                break
        except Exception as e:
            final_status = "error"
            notes = str(e)[:300]
            self._fail_cooldown[rec.creator_id] = time.time() + 180.0
            log.exception("Recording failed for %s", rec.creator_id)
        finally:
            rec.status_text = "در حال ارسال پارتهای باقیمانده..."
            self._changed()
            if upload_tasks:
                await asyncio.gather(*upload_tasks, return_exceptions=True)

            if rec.uploaded_parts == 0 and final_status == "completed":
                final_status = "empty"
                self._fail_cooldown[rec.creator_id] = time.time() + 180.0
                notes = rec.agora_diag or "no data"
                log.info("Empty recording for %s (%s): %s", rec.creator_id, rec.triggered_by, notes)
                if rec.triggered_by == "manual" and not rec.manual_stop:
                    try:
                        m = await self.bot.send_message(
                            rec.chat_id,
                            f"⚠️ ضبط <b>{html.escape(rec.creator_name)}</b> ممکن نشد (سرور اجازه اتصال نداد).",
                            parse_mode="html",
                        )
                        schedule_delete(m, 15)
                    except Exception:
                        pass
            elif rec.uploaded_parts > 0:
                self.db.increment_recordings(rec.creator_id)
                try:
                    m = await self.bot.send_message(
                        rec.chat_id,
                        f"✅ ضبط <b>{html.escape(rec.creator_name)}</b> تموم شد · "
                        f"{rec.uploaded_parts} پارت · {fmt_duration(rec.uploaded_duration)} · {fmt_bytes(rec.uploaded_bytes)}",
                        parse_mode="html",
                    )
                    schedule_delete(m, self.notify_ttl())
                except Exception:
                    pass

            if upload_failures:
                final_status = "upload_failed" if rec.uploaded_parts == 0 else "partial_upload"
                notes = (notes + " | " if notes else "") + "Upload failed; local files retained: " + "; ".join(upload_failures[:3])
                log.error("Retaining local recording files after upload failure: %s", work_dir)

            self.db.finish_recording_record(
                rec.db_rec_id,
                status=final_status,
                duration_sec=rec.uploaded_duration,
                file_size_bytes=rec.uploaded_bytes,
                tg_chat_id=rec.chat_id,
                notes=notes,
            )
            self.db.update_watch_status(rec.creator_id, "offline", display_name=rec.creator_name)
            self.active.pop(rec.creator_id, None)
            if rec.client is not None and self.busy.get(rec.client.slot) == rec.creator_id:
                self.busy.pop(rec.client.slot, None)
            if not upload_failures:
                shutil.rmtree(work_dir, ignore_errors=True)
            self._changed()

    def notify_ttl(self) -> int:
        raw = self.db.get_setting("notify_ttl", "60")
        return int(raw) if raw.isdigit() else 60

    def _should_split(self, rec: ActiveRecording, seg_bytes: int) -> bool:
        if rec.split_event.is_set():
            rec.split_event.clear()
            return True
        if seg_bytes >= self.part_cap_bytes():
            return True
        sp = self.split_seconds()
        if sp > 0 and time.time() - rec.seg_started_at >= sp:
            return True
        return False

    # ------------------------------------------------------------------ FFmpeg (HLS/FLV/RTMP)
    async def _ffmpeg_session(self, rec: ActiveRecording, work_dir: Path, enqueue: Any) -> None:
        while True:
            part_no = rec.part_number
            out = work_dir / f"{sanitize_filename(rec.creator_name)}_part{part_no}.mp4"
            cmd = [FFMPEG_BIN or "ffmpeg", "-y", "-loglevel", "error"]
            if rec.stream_url.lower().startswith(("http://", "https://")):
                cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
            # fragmented MP4: playable even if ffmpeg is killed, no extra rewrite at the end
            cmd += ["-i", rec.stream_url, "-c", "copy", "-movflags", "+frag_keyframe+empty_moov+default_base_moof", str(out)]
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            rec.seg_started_at = time.time()
            rec.connected = True
            rec.status_text = "در حال ضبط"
            self._changed()
            split = False
            try:
                while proc.returncode is None:
                    if rec.stop_event.is_set():
                        break
                    sz = out.stat().st_size if out.exists() else 0
                    rec.bytes_written = sz
                    if self._should_split(rec, sz):
                        split = True
                        break
                    _, free_mb, _ = get_disk_stats()
                    if free_mb < MIN_FREE_DISK_MB:
                        rec.stop_event.set()
                        break
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass
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
            if out.exists() and out.stat().st_size > 10_000:
                enqueue(lambda p=out, n=part_no: self._upload_job(rec, p, n))
                rec.part_number += 1
            if split and not rec.stop_event.is_set():
                continue
            return

    # ------------------------------------------------------------------ Agora RTC
    async def _agora_session(self, rec: ActiveRecording, work_dir: Path, enqueue: Any) -> None:
        def seg_paths(n: int) -> Tuple[Path, Path]:
            return work_dir / f"raw_p{n}.h264", work_dir / f"raw_p{n}.pcm"

        h264_file, pcm_file = seg_paths(rec.part_number)
        receiver = AgoraStreamReceiver(rec.stream_url, h264_file, pcm_file)

        # Try candidate UIDs / tokens quickly if Agora returns fail(8) (INVALID_TOKEN / UID mismatch)
        attempts: List[Tuple[str, str]] = [(receiver.token, u) for u in receiver.uid_candidates]
        if receiver.alt_token:
            attempts.extend((receiver.alt_token, u) for u in receiver.uid_candidates)

        connected_ok = False
        for idx, (tok_try, uid_try) in enumerate(attempts):
            if rec.stop_event.is_set():
                break
            receiver.token = tok_try
            receiver.uid = uid_try
            receiver.conn_events.clear()
            await asyncio.to_thread(receiver.connect)
            # Wait up to 4.5s for connected or fail(8)
            for _ in range(9):
                await asyncio.sleep(0.5)
                if "connected" in receiver.conn_events or receiver.video_frames > 0 or receiver.audio_frames > 0:
                    connected_ok = True
                    break
                if any(ev.startswith("fail(") or ev.startswith("disconn(") for ev in receiver.conn_events):
                    break
            if connected_ok or idx == len(attempts) - 1:
                break
            # Disconnect failed attempt cleanly before trying next UID/token
            if receiver._conn is not None:
                try:
                    await asyncio.to_thread(receiver._conn.disconnect)
                    await asyncio.to_thread(receiver._conn.release)
                except Exception:
                    pass
                receiver._conn = None

        rec.connected = connected_ok
        rec.status_text = "در حال ضبط" if connected_ok else "اتصال ناموفق"
        rec.seg_started_at = time.time()
        self._changed()
        last_seg: Dict[str, Any] = {}
        session_start = time.time()
        last_enter = time.time()
        receiver.event_times.clear()
        try:
            while not rec.stop_event.is_set():
                await asyncio.sleep(1.0)
                if connected_ok and receiver.event_times:
                    log.warning("Agora connection dropped for %s: %s", rec.creator_id, receiver.conn_events[-3:])
                    rec.status_text = "اتصال قطع شد، اتصال مجدد..."
                    break
                if not connected_ok:
                    log.warning("Agora never connected for %s: %s", rec.creator_id, receiver.conn_events[-4:])
                    rec.status_text = "اتصال برقرار نشد"
                    break
                if receiver.video_frames == 0 and time.time() - session_start >= 25:
                    log.warning("No video at all for %s after 25s: %s", rec.creator_id, receiver.conn_events[-4:])
                    rec.status_text = "تصویری دریافت نشد"
                    break
                ref = receiver.last_video_at or session_start
                if connected_ok and time.time() - ref >= VIDEO_STALL_TIMEOUT:
                    log.warning("Video stalled for %s (%.0fs), reconnecting", rec.creator_id, time.time() - ref)
                    rec.status_text = "تصویر قطع شد، اتصال مجدد..."
                    break

                rec.bytes_written = receiver.seg_bytes
                if receiver.seg_video_frames == 1:
                    rec.seg_started_at = receiver.seg_started_at
                if not connected_ok and any(ev.startswith("fail(") for ev in receiver.conn_events) and receiver.video_frames == 0:
                    break
                _, free_mb, _ = get_disk_stats()
                if free_mb < MIN_FREE_DISK_MB:
                    rec.stop_event.set()
                    break
                if self._should_split(rec, receiver.seg_bytes) and receiver.seg_video_frames > 0:
                    part_no = rec.part_number
                    rec.part_number += 1
                    nh, npcm = seg_paths(rec.part_number)
                    old = await asyncio.to_thread(receiver.rotate, nh, npcm)
                    rec.seg_started_at = time.time()
                    rec.bytes_written = 0
                    enqueue(lambda o=old, n=part_no: self._mux_and_upload(rec, o, work_dir, n))
                    self._changed()
        finally:
            last_seg = receiver.segment_snapshot()
            await asyncio.to_thread(receiver.close)

        rec.agora_diag = (
            f"v={receiver.video_frames}, a={receiver.audio_frames}, rc={receiver.connect_rc}, "
            f"ev={','.join(receiver.conn_events[:6])}, uid={receiver.uid}, cb_err={receiver.cb_error or '-'}"
        )
        log.info("Agora session ended for %s: %s", rec.creator_id, rec.agora_diag)
        if last_seg.get("video_frames", 0) > 0:
            part_no = rec.part_number
            rec.part_number += 1
            enqueue(lambda o=last_seg, n=part_no: self._mux_and_upload(rec, o, work_dir, n))
        else:
            for k in ("h264", "pcm"):
                try:
                    Path(last_seg.get(k)).unlink(missing_ok=True)
                except Exception:
                    pass

    async def _mux_and_upload(self, rec: ActiveRecording, seg: Dict[str, Any], work_dir: Path, part_no: int) -> None:
        h264: Path = seg["h264"]
        pcm: Path = seg["pcm"]
        out = work_dir / f"{sanitize_filename(rec.creator_name)}_part{part_no}.mp4"
        fps = seg["video_frames"] / max(1.0, seg["elapsed"])
        rec.upload_status = f"پارت {part_no}: آمادهسازی فایل..."
        cmd = build_mux_command(FFMPEG_BIN or "ffmpeg", h264, pcm, out, fps=fps,
                                sample_rate=seg["sample_rate"], channels=seg["channels"])
        if cmd:
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            _, err = await proc.communicate()
            if proc.returncode != 0:
                log.error("Mux error: %s", err.decode("utf-8", "ignore")[-400:])
        h264.unlink(missing_ok=True)
        pcm.unlink(missing_ok=True)
        if out.exists() and out.stat().st_size > 10_000:
            await self._upload_job(rec, out, part_no)

    # ------------------------------------------------------------------ upload
    async def _upload_job(self, rec: ActiveRecording, mp4_path: Path, part_no: int) -> None:
        async with self._upload_sem:
            dur, width, height = await asyncio.to_thread(probe_video_metadata, mp4_path)
            fsize = mp4_path.stat().st_size
            thumb_path = mp4_path.with_suffix(".thumb.jpg")
            sheet_path = mp4_path.with_suffix(".sheet.jpg")
            thumb_file, sheet_file = await asyncio.to_thread(
                generate_thumbnail_and_contact_sheet, mp4_path, thumb_path, sheet_path, dur, True
            )
            if not sheet_file and thumb_file:
                sheet_file = thumb_file  # very short clip: use the cover as preview

            caption = (
                f"🎬 <b>{html.escape(rec.creator_name)}</b>  ·  پارت {part_no}\n"
                f"⏱ <code>{fmt_duration(dur)}</code>   💾 <code>{fmt_bytes(fsize)}</code>   📐 <code>{width}×{height}</code>\n"
                f"📅 {local_now_str()}   🆔 <code>{rec.creator_id}</code>\n"
                f"#u{rec.creator_id}"
            )
            last_update = [0.0]

            async def progress_cb(current: int, total: int) -> None:
                now = time.time()
                if now - last_update[0] >= 3.0 or current == total:
                    last_update[0] = now
                    rec.upload_status = f"پارت {part_no}: آپلود {current * 100 / max(1, total):.0f}%"

            sheet_msg = None
            if sheet_file and sheet_file.exists():
                try:
                    sheet_msg = await self.bot.send_file(
                        rec.chat_id, str(sheet_file),
                        caption=f"🖼 پیشنمایش · <b>{html.escape(rec.creator_name)}</b> · پارت {part_no}",
                        parse_mode="html",
                    )
                except Exception as e:
                    log.warning("Preview upload failed: %s", e)

            attrs = [DocumentAttributeVideo(duration=max(1, dur), w=width, h=height, supports_streaming=True)]
            sent = None
            for attempt in range(5):
                try:
                    sent = await self.bot.send_file(
                        rec.chat_id, str(mp4_path), caption=caption, parse_mode="html",
                        thumb=str(thumb_file) if thumb_file else None, attributes=attrs,
                        supports_streaming=True, progress_callback=progress_cb,
                        reply_to=sheet_msg.id if sheet_msg else None,
                    )
                    break
                except FloodWaitError as fw:
                    await asyncio.sleep(int(getattr(fw, "seconds", 10)) + 2)
                except Exception as e:
                    log.warning("Upload error %s part %d (try %d): %s", rec.creator_id, part_no, attempt + 1, e)
                    await asyncio.sleep(5 * (attempt + 1))

            thumb_path.unlink(missing_ok=True)
            sheet_path.unlink(missing_ok=True)

            if sent is None:
                dest = FAILED_DIR / mp4_path.name
                try:
                    shutil.move(str(mp4_path), str(dest))
                except Exception:
                    dest = mp4_path
                rec.upload_status = f"پارت {part_no}: آپلود ناموفق"
                try:
                    await self.bot.send_message(
                        rec.chat_id,
                        f"❌ آپلود پارت {part_no} از <b>{html.escape(rec.creator_name)}</b> ناموفق بود.\n"
                        f"فایل روی سرور نگه داشته شد: <code>{html.escape(str(dest))}</code>",
                        parse_mode="html",
                    )
                except Exception:
                    pass
                # Surface failure to lifecycle; retain this file for recovery.
                raise RuntimeError(f"Telegram upload failed for part {part_no}; retained at {dest}")

            # Telegram confirmed the send. Persist metadata before deleting the local copy.
            self.db.add_clip(rec.db_rec_id, rec.creator_id, rec.creator_name, part_no, dur, fsize,
                             rec.chat_id, sent.id, sheet_msg.id if sheet_msg else 0)
            mp4_path.unlink(missing_ok=True)
            rec.uploaded_parts += 1
            rec.uploaded_duration += dur
            rec.uploaded_bytes += fsize
            rec.upload_status = f"پارت {part_no}: ارسال شد ✅"

            arch_raw = self.db.get_setting("archive_chat_id", "")
            if arch_raw:
                try:
                    msgs = [m for m in (sheet_msg, sent) if m]
                    await self.bot.forward_messages(int(arch_raw), msgs)
                except Exception as e:
                    log.warning("Archive forward failed: %s", e)


# ==============================================================================
# BACKGROUND WATCHLIST MONITOR LOOP
# ==============================================================================

class WatchlistMonitor:
    def __init__(self, db: Database, api: SuperLiveClient, rec_mgr: RecordingManager, bot: TelegramClient):
        self.db = db
        self.api = api
        self.rec_mgr = rec_mgr
        self.bot = bot
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._notified_ls: Dict[str, str] = {}  # creator_id -> livestream_id already announced

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()

    async def _announce_live(self, owner_id: int, info: CreatorStreamInfo, started: bool) -> None:
        key = info.livestream_id or "live"
        if self._notified_ls.get(info.creator_id) == key:
            return
        if not started and not info.stream_url:
            # quick joinability probe with a free account (doesn't disturb busy ones)
            probe = self.rec_mgr.free_account()
            if probe is not None:
                try:
                    full = await probe.check_creator_and_get_stream(info.creator_id)
                    if not full.stream_url:
                        return
                except Exception:
                    return
        self._notified_ls[info.creator_id] = key
        if self.db.get_setting("notify_live", "1") != "1":
            return
        row = [Button.inline("👤 صفحه", data=f"p:{info.creator_id}:0")]
        if not started:
            row.insert(0, Button.inline("🎬 ضبط فوری", data=f"qr:{info.creator_id}"))
        try:
            m = await self.bot.send_message(
                owner_id,
                f"🔴 <b>{html.escape(info.display_name or info.creator_id)}</b> لایو شد"
                + (" · ⏺ ضبط خودکار شروع شد" if started else ""),
                parse_mode="html",
                buttons=[row],
            )
            ttl = self.rec_mgr.notify_ttl()
            if ttl > 0:
                schedule_delete(m, ttl)
        except Exception as e:
            log.warning("Live notice failed: %s", e)

    async def _loop(self) -> None:
        log.info("Watchlist monitor started.")
        while not self._stop.is_set():
            try:
                interval = int(self.db.get_setting("poll_interval", str(DEFAULT_POLL_INTERVAL)) or DEFAULT_POLL_INTERVAL)
                interval = max(10, min(300, interval))
                owner_id = self.db.get_owner_id()
                items = self.db.list_watchlist() if owner_id else []
                discover_map: Optional[Dict[str, Dict[str, Any]]] = None
                if items:
                    try:
                        discover_map, _, _ = await self.api.fetch_discover_map(max_pages=8)
                    except Exception as e:
                        log.warning("Batch discover fetch failed: %s", e)
                for item in items:
                    if self._stop.is_set():
                        break
                    cid = item["creator_id"]
                    if self.rec_mgr.is_recording(cid):
                        continue
                    try:
                        info = await self.api.check_creator_and_get_stream(cid, discover_map=discover_map, light=True)
                        if info.creator_id and info.creator_id != cid and info.creator_id.isdigit():
                            log.info("Watchlist id %s -> real user_id %s", cid, info.creator_id)
                            self.db.migrate_watch(cid, info.creator_id)
                            if self.rec_mgr.is_recording(info.creator_id):
                                continue
                            cid = info.creator_id
                        if info.is_live:
                            self.db.update_watch_status(cid, "live", display_name=info.display_name, username=info.username)
                            started = False
                            if item["auto_record"] and self.rec_mgr.has_capacity():
                                ok, msg = await self.rec_mgr.start_recording(info, chat_id=owner_id, triggered_by="watchlist")
                                started = ok
                                if not ok:
                                    log.debug("Skip auto-record %s: %s", cid, msg)
                            await self._announce_live(owner_id, info, started)
                        else:
                            self._notified_ls.pop(cid, None)
                            self.rec_mgr.manually_stopped_ls.pop(cid, None)
                            self.db.update_watch_status(cid, "offline", display_name=info.display_name, username=info.username)
                    except Exception as e:
                        log.warning("Poll error for creator %s: %s", cid, e)
                    await asyncio.sleep(0.8)
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.exception("Watchlist loop error: %s", e)
                await asyncio.sleep(15)


# ==============================================================================
# TELEGRAM BOT UI & HANDLERS (Telethon MTProto)
# ==============================================================================

KB_WATCH = "📋 واچلیست"
KB_ACTIVE = "⏺ در حال ضبط"
KB_ADD = "➕ افزودن استریمر"
KB_REC = "🎬 ضبط فوری"
KB_ARCHIVE = "📚 آرشیو"
KB_SETTINGS = "⚙️ تنظیمات"
KB_HOME = "🏠 داشبورد"
KB_ALL = {KB_WATCH, KB_ACTIVE, KB_ADD, KB_REC, KB_ARCHIVE, KB_SETTINGS, KB_HOME}
SEP = "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄"


def _kb_button(label: str) -> Any:
    kwargs: Dict[str, Any] = {"resize": True}
    try:
        if "persistent" in inspect.signature(Button.text).parameters:
            kwargs["persistent"] = True
    except Exception:
        pass
    return Button.text(label, **kwargs)


def main_keyboard() -> Any:
    from telethon.tl.types import KeyboardButton, KeyboardButtonRow, ReplyKeyboardMarkup
    rows = [[KB_WATCH, KB_ACTIVE], [KB_ADD, KB_REC], [KB_ARCHIVE, KB_SETTINGS], [KB_HOME]]
    trows = [KeyboardButtonRow([KeyboardButton(t) for t in r]) for r in rows]
    kw: Dict[str, Any] = {"rows": trows, "resize": True}
    try:
        params = inspect.signature(ReplyKeyboardMarkup.__init__).parameters
        if "persistent" in params:
            kw["persistent"] = True
        if "placeholder" in params:
            kw["placeholder"] = "آیدی یا لینک رو بفرست…"
    except Exception:
        pass
    return ReplyKeyboardMarkup(**kw)


def short(name: str, n: int = 18) -> str:
    name = (name or "").strip()
    return name if len(name) <= n else name[: n - 1] + "…"


class SuperLiveRecBot:
    def __init__(self):
        self.db = Database(DB_PATH)
        self.pool = AccountPool(self.db)
        self.api = self.pool.primary
        self.bot = TelegramClient(str(SESSION_PATH), TG_API_ID, TG_API_HASH)
        self.rec_mgr = RecordingManager(self.bot, self.db, self.pool)
        self.monitor = WatchlistMonitor(self.db, self.api, self.rec_mgr, self.bot)
        self._awaiting_input: Dict[int, str] = {}
        self._panel: Dict[int, Any] = {}           # chat_id -> current panel message
        self._panel_view: Dict[int, Tuple[str, tuple]] = {}  # chat_id -> (view name, args) for auto refresh
        self._panel_until: Dict[int, float] = {}
        self._panel_last: Dict[int, str] = {}

    def _is_owner(self, user_id: int) -> bool:
        owner = self.db.get_owner_id()
        if owner == 0:
            self.db.set_owner_id(user_id)
            return True
        return user_id == owner

    # ------------------------------------------------------------------ rendering
    def _status_icon(self, cid: str, last_status: str) -> str:
        if self.rec_mgr.is_recording(cid):
            return "⏺"
        return "🔴" if last_status == "live" else "⚫"

    def render(self, view: str, *args: Any) -> Tuple[str, List[List[Any]]]:
        return getattr(self, f"_v_{view}")(*args)

    def _v_home(self) -> Tuple[str, List[List[Any]]]:
        wl = self.db.list_watchlist()
        live_cnt = sum(1 for x in wl if x["last_status"] == "live")
        active = list(self.rec_mgr.active.values())
        cnt, dur, size = self.db.clip_stats()
        used_mb, free_mb, total_mb = get_disk_stats()
        ram_used, ram_total = get_ram_usage_mb()
        ram = f"{ram_used/1024:.1f}/{ram_total/1024:.1f} GB" if ram_total else f"{ram_used:.0f} MB"
        lines = [
            "🎥 <b>SuperLive Recorder</b>",
            SEP,
            f"⏺ در حال ضبط: <b>{len(active)}</b>",
            f"👀 واچلیست: <b>{len(wl)}</b>   ·   🔴 آنلاین: <b>{live_cnt}</b>",
            f"📚 آرشیو: <b>{cnt}</b> ویدیو   ·   <b>{fmt_duration(dur)}</b>   ·   <b>{fmt_bytes(size)}</b>",
            SEP,
            f"💽 دیسک آزاد: <code>{free_mb/1024:.1f}/{total_mb/1024:.1f} GB</code>   🧠 رم: <code>{ram}</code>",
            f"⚙️ Agora {'✅' if AGORA_AVAILABLE else '❌'}   ·   FFmpeg {'✅' if FFMPEG_BIN else '❌'}",
        ]
        if active:
            lines.append(SEP)
            for r in active[:6]:
                lines.append(f"⏺ <b>{html.escape(short(r.creator_name, 22))}</b> · <code>{fmt_duration(time.time() - r.started_at)}</code>")
        lines.append("\n<i>آیدی، لینک پروفایل یا لینک لایو رو هر وقت خواستی همینجا بفرست.</i>")
        buttons = [
            [Button.inline(f"📋 واچلیست ({len(wl)})", data="w:0"), Button.inline(f"⏺ ضبطها ({len(active)})", data="a")],
            [Button.inline("📚 آرشیو", data="ar:0"), Button.inline("⚙️ تنظیمات", data="s")],
            [Button.inline("🔄 بروزرسانی", data="h")],
        ]
        return "\n".join(lines), buttons

    def _v_watchlist(self, page: int = 0) -> Tuple[str, List[List[Any]]]:
        items = self.db.list_watchlist()
        # recording first, then live, then offline
        items.sort(key=lambda x: (0 if self.rec_mgr.is_recording(x["creator_id"]) else (1 if x["last_status"] == "live" else 2)))
        per = 8
        pages = max(1, (len(items) + per - 1) // per)
        page = max(0, min(page, pages - 1))
        chunk = items[page * per:(page + 1) * per]
        rec_n = sum(1 for x in items if self.rec_mgr.is_recording(x["creator_id"]))
        live_n = sum(1 for x in items if x["last_status"] == "live")
        lines = [
            f"📋 <b>واچلیست</b>  ·  {len(items)} نفر",
            f"⏺ {rec_n} در حال ضبط   🔴 {live_n} آنلاین   ⚫ {len(items) - live_n} آفلاین",
            SEP,
        ]
        if not chunk:
            lines.append("هنوز کسی اضافه نشده. از دکمه «➕ افزودن استریمر» استفاده کن.")
        else:
            lines.append("<i>روی هر نفر بزن تا جزئیات و ضبطهاش رو ببینی.</i>")
        buttons: List[List[Any]] = []
        for it in chunk:
            cid = it["creator_id"]
            icon = self._status_icon(cid, it["last_status"])
            extra = ""
            r = self.rec_mgr.active.get(cid)
            if r:
                extra = f" · {fmt_duration(time.time() - r.started_at)}"
            auto = "" if it["auto_record"] else " ⏸"
            buttons.append([Button.inline(f"{icon} {short(it['display_name'] or cid)}{extra}{auto}", data=f"p:{cid}:0")])
        nav: List[Any] = []
        if page > 0:
            nav.append(Button.inline("‹ قبلی", data=f"w:{page-1}"))
        if pages > 1:
            nav.append(Button.inline(f"{page+1}/{pages}", data=f"w:{page}"))
        if page < pages - 1:
            nav.append(Button.inline("بعدی ›", data=f"w:{page+1}"))
        if nav:
            buttons.append(nav)
        buttons.append([Button.inline("➕ افزودن", data="add"), Button.inline("🔄", data=f"w:{page}"), Button.inline("🏠", data="h")])
        return "\n".join(lines), buttons

    def _v_person(self, cid: str, page: int = 0) -> Tuple[str, List[List[Any]]]:
        w = self.db.get_watch(cid) or {}
        r = self.rec_mgr.active.get(cid)
        name = w.get("display_name") or (r.creator_name if r else "") or cid
        cnt, dur, size = self.db.clip_stats(cid)
        if r:
            st = "⏺ در حال ضبط"
        elif w.get("last_status") == "live":
            st = "🔴 آنلاین"
        else:
            st = "⚫ آفلاین"
        lines = [
            f"👤 <b>{html.escape(name)}</b>",
            f"🆔 <code>{cid}</code>" + (f"   @{html.escape(w['username'])}" if w.get("username") else ""),
            SEP,
            f"📡 وضعیت: {st}",
        ]
        if w:
            lines.append(f"🤖 ضبط خودکار: {'✅ روشن' if w.get('auto_record') else '⏸ خاموش'}")
            if w.get("last_seen_live"):
                lines.append(f"🕘 آخرین لایو: <code>{iso_to_local(w['last_seen_live'])}</code>")
        else:
            lines.append("<i>این استریمر توی واچلیست نیست.</i>")
        if r:
            lines += [
                SEP,
                f"⏱ کل ضبط: <code>{fmt_duration(time.time() - r.started_at)}</code>   ·   پارت فعلی: <code>{fmt_duration(time.time() - r.seg_started_at)}</code>",
                f"💾 پارت فعلی: <code>{fmt_bytes(r.bytes_written)}</code>   ·   ارسالشده: <b>{r.uploaded_parts}</b> پارت",
            ]
            if r.upload_status:
                lines.append(f"📤 {html.escape(r.upload_status)}")
        lines += [SEP, f"📚 ضبطها: <b>{cnt}</b> ویدیو · <b>{fmt_duration(dur)}</b> · <b>{fmt_bytes(size)}</b>"]

        per = 6
        clips = self.db.list_clips(cid, limit=per, offset=page * per)
        buttons: List[List[Any]] = []
        if r:
            buttons.append([Button.inline("📤 ارسال تا اینجا", data=f"sp:{cid}"), Button.inline("⏹ توقف ضبط", data=f"st:{cid}")])
        else:
            buttons.append([Button.inline("🎬 ضبط فوری", data=f"qr:{cid}")])
        if w:
            buttons.append([
                Button.inline("⏸ خاموش کردن خودکار" if w.get("auto_record") else "▶️ روشن کردن خودکار", data=f"tg:{cid}"),
                Button.inline("🗑 حذف", data=f"rmq:{cid}"),
            ])
        else:
            buttons.append([Button.inline("➕ افزودن به واچلیست", data=f"qw:{cid}")])
        for c in clips:
            buttons.append([Button.inline(
                f"▶️ {iso_to_local(c['created_at'])} · پ{c['part_no']} · {fmt_duration(c['duration_sec'])} · {fmt_bytes(c['size_bytes'])}",
                data=f"c:{c['id']}",
            )])
        nav: List[Any] = []
        if page > 0:
            nav.append(Button.inline("‹ جدیدترها", data=f"p:{cid}:{page-1}"))
        if (page + 1) * per < cnt:
            nav.append(Button.inline("قدیمیترها ›", data=f"p:{cid}:{page+1}"))
        if nav:
            buttons.append(nav)
        buttons.append([Button.inline("‹ واچلیست", data="w:0"), Button.inline("🔄", data=f"p:{cid}:{page}"), Button.inline("🏠", data="h")])
        return "\n".join(lines), buttons

    def _v_active(self) -> Tuple[str, List[List[Any]]]:
        actives = list(self.rec_mgr.active.values())
        lines = [f"⏺ <b>ضبطهای فعال</b>  ·  {len(actives)}", SEP]
        buttons: List[List[Any]] = []
        if not actives:
            lines.append("الان چیزی در حال ضبط نیست.")
        for r in actives:
            conn = "🟢" if r.connected else "🟡"
            lines.append(
                f"{conn} <b>{html.escape(short(r.creator_name, 24))}</b>\n"
                f"   ⏱ <code>{fmt_duration(time.time() - r.started_at)}</code> کل  ·  "
                f"<code>{fmt_duration(time.time() - r.seg_started_at)}</code> پارت {r.part_number}  ·  💾 <code>{fmt_bytes(r.bytes_written)}</code>\n"
                f"   👥 {html.escape(r.client.label if r.client else '-')}  ·  🎞 <code>{r.livestream_id}</code>  ·  📌 {html.escape(r.status_text)}\n"
                f"   📤 {r.uploaded_parts} پارت ارسال شده" + (f"  ·  {html.escape(r.upload_status)}" if r.upload_status else "")
            )
            buttons.append([
                Button.inline(f"👤 {short(r.creator_name, 12)}", data=f"p:{r.creator_id}:0"),
                Button.inline("📤 ارسال", data=f"sp:{r.creator_id}"),
                Button.inline("⏹ توقف", data=f"st:{r.creator_id}"),
            ])
        if actives:
            lines.append(f"\n<i>«📤 ارسال» = هرچی تا الان ضبط شده فرستاده میشه و ضبط بدون وقفه ادامه پیدا میکنه.</i>")
            buttons.append([Button.inline("⏹ توقف همه", data="stallq")])
        buttons.append([Button.inline("🔄 بروزرسانی", data="a"), Button.inline("🏠", data="h")])
        return "\n".join(lines), buttons

    def _v_archive(self, page: int = 0) -> Tuple[str, List[List[Any]]]:
        per = 8
        rows = self.db.clip_creators(limit=per + 1, offset=page * per)
        more = len(rows) > per
        rows = rows[:per]
        cnt, dur, size = self.db.clip_stats()
        lines = [f"📚 <b>آرشیو</b>  ·  {cnt} ویدیو · {fmt_duration(dur)} · {fmt_bytes(size)}", SEP]
        if not rows:
            lines.append("هنوز ویدیویی ضبط نشده.")
        else:
            lines.append("<i>روی هر استریمر بزن تا ویدیوهاش رو مرتب ببینی.</i>")
        buttons: List[List[Any]] = []
        for r in rows:
            buttons.append([Button.inline(
                f"👤 {short(r['creator_name'] or r['creator_id'])} · {r['cnt']} ویدیو · {fmt_duration(r['dur'] or 0)}",
                data=f"p:{r['creator_id']}:0",
            )])
        nav: List[Any] = []
        if page > 0:
            nav.append(Button.inline("‹ قبلی", data=f"ar:{page-1}"))
        if more:
            nav.append(Button.inline("بعدی ›", data=f"ar:{page+1}"))
        if nav:
            buttons.append(nav)
        buttons.append([Button.inline("🏠", data="h")])
        return "\n".join(lines), buttons

    def _v_accounts(self) -> Tuple[str, List[List[Any]]]:
        lines = ["👥 <b>اکانتهای SuperLive</b>", SEP,
                 "<i>هر اکانت همزمان فقط یه لایو رو میتونه ضبط کنه. برای ضبط همزمان N نفر، N اکانت لازمه.</i>", ""]
        buttons: List[List[Any]] = []
        for slot, c in sorted(self.pool.clients.items()):
            busy = self.rec_mgr.busy.get(slot)
            if not c.has_token():
                st = "⚪ بدون توکن"
            elif busy:
                r = self.rec_mgr.active.get(busy)
                if r:
                    st = (f"⏺ در حال ضبط <b>{html.escape(short(r.creator_name, 18))}</b>\n"
                          f"     🎞 لایو: <a href=\"https://superlive.chat/livestream/{r.livestream_id}\">{r.livestream_id}</a>"
                          f"  ·  ⏱ <code>{fmt_duration(time.time() - r.started_at)}</code>"
                          f"  ·  {'🟢 تصویر میرسه' if r.connected else '🟡 در حال اتصال'}")
                else:
                    st = "⏳ در حال آمادهسازی..."
            else:
                st = "🟢 آزاد"
            lines.append(f"• <b>{html.escape(c.label)}</b>{' (اصلی)' if slot == 0 else ''} — {st}")
            row = [Button.inline(f"🔑 {short(c.label, 10)}", data=f"acct:{slot}")]
            if busy and busy in self.rec_mgr.active:
                row.insert(0, Button.inline("👤 ضبط", data=f"p:{busy}:0"))
            if slot != 0:
                row.append(Button.inline("🗑", data=f"accrmq:{slot}"))
            buttons.append(row)
        lines += ["", SEP,
                  "➕ <b>افزودن اکانت:</b>",
                  "<code>/addacc ایمیل رمز</code>   یا   <code>/addacc token توکن [device_id]</code>",
                  "🔑 دکمهی هر اکانت = تنظیم دوباره توکن همون اکانت"]
        buttons.append([Button.inline("🔄", data="acc"), Button.inline("‹ تنظیمات", data="s"), Button.inline("🏠", data="h")])
        return "\n".join(lines), buttons

    def _v_settings(self) -> Tuple[str, List[List[Any]]]:
        g = self.db.get_setting
        notify = g("notify_live", "1") == "1"
        ttl = g("notify_ttl", "60")
        split = g("split_minutes", "0")
        part = g("part_mb", "1900")
        poll = g("poll_interval", str(DEFAULT_POLL_INTERVAL))
        accs = self.rec_mgr.accounts()
        conc_txt = f"{len(accs)} (به تعداد اکانتها)"
        arch = g("archive_chat_id", "") or "خاموش"
        ttl_txt = "هیچوقت" if ttl == "0" else f"{ttl} ثانیه"
        split_txt = "خاموش" if split == "0" else f"هر {split} دقیقه"
        text = "\n".join([
            "⚙️ <b>تنظیمات</b>",
            SEP,
            f"🔔 اعلان لایو شدن: <b>{'روشن' if notify else 'خاموش'}</b>",
            f"🧹 حذف خودکار اعلانها بعد از: <b>{ttl_txt}</b>",
            f"✂️ ارسال خودکار تکهتکه: <b>{split_txt}</b>",
            f"📦 حداکثر حجم هر پارت: <b>{part} MB</b>",
            f"⏱ بازه چک واچلیست: <b>{poll} ثانیه</b>",
            f"🗄 کانال آرشیو: <code>{html.escape(arch)}</code>",
            SEP,
            f"⚡ ضبط همزمان: <b>{conc_txt}</b>",
            "• واچلیست: <b>نامحدود</b>",
            "• پیشنمایش برای همه ویدیوها: <b>همیشه</b>",
            "• حذف فایل از سرور بعد از ارسال: <b>همیشه</b>",
            "\n<i>کانال آرشیو:</i> <code>/setarchive -100…</code>  ·  <code>/setarchive off</code>",
        ])
        buttons = [
            [Button.inline(f"🔔 اعلان: {'ON' if notify else 'OFF'}", data="set:notify"), Button.inline(f"🧹 حذف: {ttl_txt}", data="set:ttl")],
            [Button.inline(f"✂️ تکهتکه: {split_txt}", data="set:split"), Button.inline(f"📦 {part}MB", data="set:part")],
            [Button.inline(f"⏱ چک: {poll}s", data="set:poll"), Button.inline(f"👥 اکانتها ({len(accs)})", data="acc")],
            [Button.inline("🏠", data="h")],
        ]
        return text, buttons

    # ------------------------------------------------------------------ panel handling
    async def show(self, event: Any, view: str, *args: Any, edit: bool = False, live: bool = True) -> None:
        """Show a view in a single 'panel' message per chat (old panels are removed to keep the chat clean)."""
        text, buttons = self.render(view, *args)
        chat_id = event.chat_id
        if edit:
            try:
                await event.edit(text, parse_mode="html", buttons=buttons)
                msg = await event.get_message()
                self._panel[chat_id] = msg
            except MessageNotModifiedError:
                pass
            except Exception as e:
                log.warning("Panel edit failed (%s), sending new", e)
                msg = await self.bot.send_message(chat_id, text, parse_mode="html", buttons=buttons, link_preview=False)
                self._panel[chat_id] = msg
        else:
            old = self._panel.get(chat_id)
            msg = await self.bot.send_message(chat_id, text, parse_mode="html", buttons=buttons, link_preview=False)
            self._panel[chat_id] = msg
            if old is not None:
                try:
                    await old.delete()
                except Exception:
                    pass
        self._panel_view[chat_id] = (view, args)
        self._panel_last[chat_id] = text
        self._panel_until[chat_id] = time.time() + (900 if live else 0)

    async def _panel_refresher(self) -> None:
        """Auto-refresh the open panel (live timers / statuses) every 10 s for 15 minutes."""
        while True:
            await asyncio.sleep(10)
            for chat_id, msg in list(self._panel.items()):
                try:
                    if time.time() > self._panel_until.get(chat_id, 0):
                        continue
                    view, args = self._panel_view.get(chat_id, ("home", ()))
                    if view not in ("home", "watchlist", "person", "active", "accounts"):
                        continue
                    text, buttons = self.render(view, *args)
                    if text == self._panel_last.get(chat_id):
                        continue
                    await msg.edit(text, parse_mode="html", buttons=buttons)
                    self._panel_last[chat_id] = text
                except MessageNotModifiedError:
                    pass
                except FloodWaitError as fw:
                    await asyncio.sleep(int(getattr(fw, "seconds", 10)))
                except Exception:
                    self._panel_until[chat_id] = 0

    async def _ensure_keyboard(self, chat_id: int, force: bool = False) -> None:
        """The message that carries the reply keyboard must NOT be deleted, or Telegram hides the keyboard."""
        old_id = self.db.get_setting(f"kb_msg_{chat_id}", "")
        if old_id and not force:
            return
        try:
            markup = main_keyboard()
            m = await self.bot.send_message(chat_id, "⌨️ منوی ربات پایین صفحهست.", buttons=markup)
        except Exception as e:
            log.warning("Persistent keyboard failed (%s), using simple keyboard", e)
            rows = [[KB_WATCH, KB_ACTIVE], [KB_ADD, KB_REC], [KB_ARCHIVE, KB_SETTINGS], [KB_HOME]]
            m = await self.bot.send_message(chat_id, "⌨️ منوی ربات پایین صفحهست.",
                                            buttons=[[Button.text(t, resize=True) for t in r] for r in rows])
        self.db.set_setting(f"kb_msg_{chat_id}", str(m.id))
        if old_id.isdigit():
            try:
                await self.bot.delete_messages(chat_id, [int(old_id)])
            except Exception:
                pass

    async def toast(self, event: Any, text: str, ttl: int = 8, **kw: Any) -> Any:
        m = await self.bot.send_message(event.chat_id, text, parse_mode="html", **kw)
        schedule_delete(m, ttl)
        return m

    async def _delete_user_msg(self, event: Any) -> None:
        try:
            await event.delete()
        except Exception:
            pass

    # ------------------------------------------------------------------ handlers
    def _register_handlers(self) -> None:
        @self.bot.on(events.NewMessage(pattern=r"^/(start|menu)(@\w+)?(\s.*)?$"))
        async def cmd_start(event: Any) -> None:
            if not self._is_owner(event.sender_id):
                await event.reply("⛔ این ربات شخصیه.")
                return
            self._awaiting_input.pop(event.sender_id, None)
            await self._delete_user_msg(event)
            # (re)attach the persistent bottom keyboard
            try:
                await self.show(event, "home")
            except Exception as e:
                log.exception("Home panel failed: %s", e)
                await event.respond(f"⚠️ خطا در نمایش منو: <code>{html.escape(str(e)[:300])}</code>", parse_mode="html")
            try:
                await self._ensure_keyboard(event.chat_id, force=True)
            except Exception as e:
                log.exception("Keyboard failed: %s", e)

        @self.bot.on(events.NewMessage(func=lambda e: e.is_private and (e.raw_text or "") in KB_ALL))
        async def on_keyboard(event: Any) -> None:
            if not self._is_owner(event.sender_id):
                return
            t = event.raw_text
            self._awaiting_input.pop(event.sender_id, None)
            await self._delete_user_msg(event)
            if t == KB_WATCH:
                await self.show(event, "watchlist", 0)
            elif t == KB_ACTIVE:
                await self.show(event, "active")
            elif t == KB_ARCHIVE:
                await self.show(event, "archive", 0)
            elif t == KB_SETTINGS:
                await self.show(event, "settings")
            elif t == KB_HOME:
                await self.show(event, "home")
            elif t == KB_ADD:
                self._awaiting_input[event.sender_id] = "watch"
                await self.toast(event, "➕ آیدی، لینک پروفایل یا لینک لایو استریمر رو بفرست:", ttl=60)
            elif t == KB_REC:
                self._awaiting_input[event.sender_id] = "rec"
                await self.toast(event, "🎬 آیدی، لینک پروفایل یا لینک لایو رو بفرست تا فوری ضبط بشه:", ttl=60)

        @self.bot.on(events.NewMessage(pattern=r"^/watch(?:\s+(.+))?$"))
        async def cmd_watch(event: Any) -> None:
            if not self._is_owner(event.sender_id):
                return
            arg = (event.pattern_match.group(1) or "").strip()
            await self._delete_user_msg(event)
            if not arg:
                self._awaiting_input[event.sender_id] = "watch"
                await self.toast(event, "➕ آیدی یا لینک رو بفرست:", ttl=60)
                return
            await self._handle_add_watch(event, arg)

        @self.bot.on(events.NewMessage(pattern=r"^/rec(?:\s+(.+))?$"))
        async def cmd_rec(event: Any) -> None:
            if not self._is_owner(event.sender_id):
                return
            arg = (event.pattern_match.group(1) or "").strip()
            await self._delete_user_msg(event)
            if not arg:
                self._awaiting_input[event.sender_id] = "rec"
                await self.toast(event, "🎬 آیدی یا لینک رو بفرست:", ttl=60)
                return
            await self._handle_manual_rec(event, arg)

        @self.bot.on(events.NewMessage(pattern=r"^/stop(?:\s+(.+))?$"))
        async def cmd_stop(event: Any) -> None:
            if not self._is_owner(event.sender_id):
                return
            arg = (event.pattern_match.group(1) or "").strip().lower()
            await self._delete_user_msg(event)
            if arg == "all":
                for r in list(self.rec_mgr.active.values()):
                    await self.rec_mgr.stop_recording(r.creator_id)
            elif arg:
                await self.rec_mgr.stop_recording(extract_creator_identifier(arg))
            await self.show(event, "active")

        @self.bot.on(events.NewMessage(pattern=r"^/check\s+(.+)$"))
        async def cmd_check(event: Any) -> None:
            if not self._is_owner(event.sender_id):
                return
            raw = event.pattern_match.group(1).strip()
            msg = await event.reply("🔍 در حال بررسی وضعیت استریمر...")
            await self._delete_user_msg(event)
            cid, uname, dname, _ = await self.api.resolve_creator(raw)
            if not cid:
                await msg.edit("❌ صاحب این لایو/آیدی پیدا نشد (شاید لایو تموم شده).")
                return
            info = await self.api.check_creator_and_get_stream(cid)
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
                        Button.inline("➕ افزودن به واچ‌لیست", data=f"qw:{info.creator_id}"),
                        Button.inline("🎬 ضبط فوری", data=f"qr:{info.creator_id}"),
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
            if not cid:
                await msg.edit("❌ صاحب این لایو/آیدی پیدا نشد (شاید لایو تموم شده).")
                return
            info = await self.api.check_creator_and_get_stream(cid)
            d = info.raw_debug
            report = (
                f"🛠 <b>گزارش دیباگ API برای <code>{html.escape(info.creator_id)}</code></b>\n"
                f"• Init: <code>{d.get('init_status')}</code> | Token: <code>{d.get('has_token')}</code>\n"
                f"• Profile HTTP: <code>{d.get('profile_http')}</code>\n"
                f"• Discover HTTP: <code>{d.get('discover_http', '-')}</code>\n"
                f"• Retrieve/Enter HTTP: <code>{d.get('retrieve_http', '-')} / {d.get('enter_http', '-')}</code>\n"
                f"• Live: <code>{info.is_live}</code> | ls_id: <code>{html.escape(info.livestream_id or '-')}</code>\n"
                f"• Stream: <code>{html.escape((info.stream_url[:60] + '...') if len(info.stream_url) > 60 else (info.stream_url or '-'))}</code>\n"
                f"• Token Info: <code>{html.escape(str(d.get('token_info', '-')))}</code>\n"
                f"• Agora Keys: <code>{html.escape(str(d.get('agora_keys', '-'))[:600])}</code>\n\n"
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

        @self.bot.on(events.NewMessage(pattern=r"^/guesttest\s+(.+)$"))
        async def cmd_guesttest(event: Any) -> None:
            """Test: can a brand-new GUEST device (no login) get an Agora token and receive video?"""
            if not self._is_owner(event.sender_id):
                return
            raw = event.pattern_match.group(1).strip()
            await self._delete_user_msg(event)
            msg = await event.respond("🧪 تست مهمان: ساخت دستگاه جدید...")
            slot = 90000 + random.randint(0, 9999)
            guest = SuperLiveClient(self.db, slot)
            guest._token = ""  # make sure no token is reused
            lines = ["🧪 <b>نتیجه تست مهمان</b>", SEP, f"📱 device: <code>{guest._device_id[:12]}…</code>"]
            work = RECORDINGS_DIR / f"guesttest_{slot}"
            try:
                init = await guest.ensure_initialized(force=True)
                guid = str(init.get("guid") or "")
                if not guest.has_token() and guid:
                    # SuperLive web guests are identified by the "guid" returned from /initialize
                    guest._token = guid
                    lines.append(f"ℹ️ توکن نداد ولی <code>guid</code> داد؛ از همون به عنوان توکن مهمان استفاده میشه.")
                lines.append(f"1️⃣ initialize: HTTP <code>{init.get('_http_status')}</code> · توکن مهمان: <b>{'✅ گرفت' if guest.has_token() else '❌ نگرفت'}</b>")
                if not guest.has_token():
                    lines.append(f"<code>{html.escape(json.dumps(init, ensure_ascii=False)[:500])}</code>")
                # Auth probe: which scheme (if any) does the server accept for this guest?
                probe = await guest._post("users/me", {})
                lines.append(f"   🔐 users/me با {guest._auth_scheme}: HTTP <code>{probe.get('_http_status')}</code>")

                cid, _, dname, _ = await self.api.resolve_creator(raw)  # resolve with main account (read-only)
                if not cid:
                    lines.append("❌ استریمر/لایو پیدا نشد.")
                    raise RuntimeError("stop")
                main_info = await self.api.check_creator_and_get_stream(cid, light=True)
                info = await guest.check_creator_and_get_stream(cid)
                if not info.is_live and main_info.is_live and main_info.livestream_id:
                    # guest couldn't see the profile; force the livestream id from the main account
                    lines.append(f"ℹ️ اکانت اصلی لایو رو دید (<code>{main_info.livestream_id}</code>)؛ مهمان مستقیم وارد همون لایو میشه.")
                    ls = main_info.livestream_id
                    ret = await guest._request_smart("livestream/retrieve", {"livestream_id": ls})
                    ent = await guest._request_smart("livestream/enter", {"livestream_id": ls})
                    info.is_live = True
                    info.livestream_id = ls
                    info.raw_debug["retrieve_http"] = ret.get("_http_status")
                    info.raw_debug["enter_http"] = ent.get("_http_status")
                    info.raw_debug["enter_sample"] = json.dumps(ent, ensure_ascii=False)[:700]
                    info.raw_debug["retrieve_sample"] = json.dumps(ret, ensure_ascii=False)[:700]
                    info.stream_url = guest._extract_direct_stream_url(ent) or guest._extract_direct_stream_url(ret) or \
                        guest._build_agora_uri({"retrieve": ret, "enter": ent}, ls_id=ls, debug_out=info.raw_debug)
                elif not main_info.is_live:
                    lines.append("⚠️ اکانت اصلی هم این استریمر رو آفلاین دید؛ تست رو موقع لایو بودنش تکرار کن.")
                d = info.raw_debug
                lines.append(f"2️⃣ استریمر: <b>{html.escape(info.display_name or dname or cid)}</b> · لایو: <b>{'✅' if info.is_live else '❌'}</b> · ls: <code>{info.livestream_id or '-'}</code>")
                lines.append(f"3️⃣ retrieve/enter: <code>{d.get('retrieve_http', '-')}/{d.get('enter_http', '-')}</code> · توکن Agora: <b>{'✅' if info.stream_url.startswith('agora://') else ('✅ (HLS)' if info.stream_url else '❌')}</b>")
                if d.get("token_info"):
                    lines.append(f"   🔑 <code>{html.escape(str(d['token_info'])[:200])}</code>")
                if not info.stream_url:
                    m = re.search(r'"disallowed_reason":\s*("?[^,}]*"?)', str(d.get("enter_sample", "")) + str(d.get("retrieve_sample", "")))
                    if m:
                        lines.append(f"   دلیل: <code>{html.escape(m.group(1))}</code>")
                    lines.append(f"   <code>{html.escape(str(d.get('enter_sample', ''))[:400])}</code>")
                    raise RuntimeError("stop")

                if info.stream_url.startswith("agora://") and AGORA_AVAILABLE:
                    await msg.edit("\n".join(lines + ["", "4️⃣ اتصال آزمایشی به Agora (۱۵ ثانیه)..."]), parse_mode="html")
                    work.mkdir(parents=True, exist_ok=True)
                    rcv = AgoraStreamReceiver(info.stream_url, work / "t.h264", work / "t.pcm")
                    try:
                        await asyncio.to_thread(rcv.connect)
                        await asyncio.sleep(15)
                    finally:
                        await asyncio.to_thread(rcv.close)
                    ok = rcv.video_frames > 0
                    lines.append(
                        f"4️⃣ Agora: <b>{'✅ تصویر رسید' if ok else '❌ تصویر نرسید'}</b> · "
                        f"v=<code>{rcv.video_frames}</code> a=<code>{rcv.audio_frames}</code> uid=<code>{rcv.uid}</code>\n"
                        f"   ev: <code>{html.escape(','.join(rcv.conn_events[:6]))}</code>"
                    )
                    lines += [SEP, "✅ <b>مهمان جواب میده</b> — میشه ضبط بدون اکانت رو ساخت." if ok
                              else "❌ <b>مهمان توکن گرفت ولی تصویر نیومد</b> — خروجی رو بفرست."]
                else:
                    lines += [SEP, "✅ مهمان لینک استریم گرفت."]
            except RuntimeError:
                lines += [SEP, "❌ <b>مهمان کافی نیست</b> — باید با اکانت لاگینشده ضبط کرد."]
            except Exception as e:
                lines.append(f"⚠️ خطا: <code>{html.escape(str(e)[:300])}</code>")
            finally:
                shutil.rmtree(work, ignore_errors=True)
                guest.wipe()
                await guest.close()
            await msg.edit("\n".join(lines)[:4000], parse_mode="html", link_preview=False)

        @self.bot.on(events.NewMessage(pattern=r"^/addacc\s+(\S+)\s+(\S+)(?:\s+(\S+))?$"))
        async def cmd_addacc(event: Any) -> None:
            if not self._is_owner(event.sender_id):
                return
            a1, a2, a3 = event.pattern_match.group(1), event.pattern_match.group(2), event.pattern_match.group(3) or ""
            await self._delete_user_msg(event)
            if a1.lower() == "token":
                c = self.pool.add(a2, a3)
                await self.toast(event, f"✅ {html.escape(c.label)} با توکن اضافه شد.", ttl=8)
            else:
                c = self.pool.new_empty()
                ok, msg_text = await c.email_signin(a1, a2)
                if not ok:
                    self.pool.remove(c.slot)
                    await self.toast(event, f"❌ {html.escape(msg_text)}", ttl=15)
                    return
                c.label = a1.split("@")[0]
                self.db.set_setting(c._k("superlive_label"), c.label)
                await self.toast(event, f"✅ اکانت {html.escape(c.label)} اضافه شد.", ttl=8)
            await self.show(event, "accounts")

        @self.bot.on(events.NewMessage(pattern=r"^/(settings|watchlist|active|archive|history|accounts)$"))
        async def cmd_views(event: Any) -> None:
            if not self._is_owner(event.sender_id):
                return
            name = event.pattern_match.group(1)
            await self._delete_user_msg(event)
            view = {"history": "archive"}.get(name, name)
            await self.show(event, view, *((0,) if view in ("watchlist", "archive") else ()))

        @self.bot.on(events.NewMessage(func=lambda e: e.is_private and e.raw_text and not e.raw_text.startswith("/") and e.raw_text not in KB_ALL))
        async def on_plain_text(event: Any) -> None:
            if not self._is_owner(event.sender_id):
                return
            mode = self._awaiting_input.pop(event.sender_id, None)
            raw = event.raw_text.strip()
            if mode and mode.startswith("acctok:"):
                await self._delete_user_msg(event)
                slot = int(mode.split(":")[1])
                c = self.pool.clients.get(slot)
                if c:
                    bits = raw.split()
                    c.set_credentials(bits[0], bits[1] if len(bits) > 1 else "")
                    await self.toast(event, f"✅ توکن {html.escape(c.label)} ذخیره شد.", ttl=6)
                await self.show(event, "accounts")
                return
            if mode == "rec":
                await self._delete_user_msg(event)
                await self._handle_manual_rec(event, raw)
            elif mode == "watch":
                await self._delete_user_msg(event)
                await self._handle_add_watch(event, raw)
            elif "superlive" in raw.lower() or "spl-live" in raw.lower() or "sprlv" in raw.lower() or re.fullmatch(r"\d{5,15}", raw):
                await self._delete_user_msg(event)
                m = await self.toast(event, "🔍 در حال شناسایی...", ttl=30)
                cid, _, dname, _ = await self.api.resolve_creator(raw)
                try:
                    await m.delete()
                except Exception:
                    pass
                if not cid:
                    await self.toast(event, "❌ استریمری با این آیدی/لینک پیدا نشد.")
                    return
                await self.show(event, "person", cid, 0)

        @self.bot.on(events.CallbackQuery())
        async def on_callback(event: Any) -> None:
            if not self._is_owner(event.sender_id):
                await event.answer("⛔", alert=True)
                return
            data = event.data.decode("utf-8", errors="ignore")
            try:
                parts = data.split(":")
                head = parts[0]
                if head == "h":
                    await self.show(event, "home", edit=True)
                elif head == "w":
                    await self.show(event, "watchlist", int(parts[1]), edit=True)
                elif head == "a":
                    await self.show(event, "active", edit=True)
                elif head == "ar":
                    await self.show(event, "archive", int(parts[1]), edit=True)
                elif head == "s":
                    await self.show(event, "settings", edit=True)
                elif head == "p":
                    await self.show(event, "person", parts[1], int(parts[2]) if len(parts) > 2 else 0, edit=True)
                elif head == "add":
                    self._awaiting_input[event.sender_id] = "watch"
                    await event.answer()
                    await self.toast(event, "➕ آیدی، لینک پروفایل یا لینک لایو رو بفرست:", ttl=60)
                    return
                elif head == "qw":
                    await event.answer("در حال افزودن...")
                    await self._handle_add_watch(event, parts[1], panel_edit=True)
                elif head == "qr":
                    await event.answer("در حال بررسی لایو...")
                    await self._handle_manual_rec(event, parts[1], panel_edit=True)
                elif head == "tg":
                    val = self.db.toggle_auto_record(parts[1])
                    await event.answer("ضبط خودکار روشن ✅" if val else "ضبط خودکار خاموش ⏸")
                    await self.show(event, "person", parts[1], 0, edit=True)
                    return
                elif head == "rmq":
                    await event.edit(
                        "🗑 این استریمر از واچلیست حذف بشه؟\n<i>ویدیوهای ضبطشده حذف نمیشن.</i>",
                        parse_mode="html",
                        buttons=[[Button.inline("✅ بله، حذف کن", data=f"rm:{parts[1]}"), Button.inline("‹ انصراف", data=f"p:{parts[1]}:0")]],
                    )
                elif head == "rm":
                    self.db.remove_watch(parts[1])
                    await event.answer("حذف شد 🗑")
                    await self.show(event, "watchlist", 0, edit=True)
                    return
                elif head == "sp":
                    ok = await self.rec_mgr.split_recording(parts[1])
                    await event.answer("📤 تا اینجا ارسال میشه؛ ضبط ادامه داره" if ok else "ضبط فعالی نیست", alert=False)
                    return
                elif head == "st":
                    ok = await self.rec_mgr.stop_recording(parts[1])
                    await event.answer("⏹ ضبط متوقف شد؛ فایل در حال ارسال" if ok else "ضبط فعالی نیست")
                    await asyncio.sleep(0.5)
                    await self.show(event, self._panel_view.get(event.chat_id, ("active", ()))[0] if self._panel_view.get(event.chat_id, ("",))[0] in ("person", "active") else "active",
                                    *self._panel_view.get(event.chat_id, ("active", ()))[1] if self._panel_view.get(event.chat_id, ("",))[0] in ("person", "active") else (), edit=True)
                    return
                elif head == "stallq":
                    await event.edit("⏹ همه ضبطها متوقف بشن؟", buttons=[[Button.inline("✅ بله", data="stall"), Button.inline("‹ انصراف", data="a")]])
                elif head == "stall":
                    for r in list(self.rec_mgr.active.values()):
                        await self.rec_mgr.stop_recording(r.creator_id)
                    await event.answer("همه متوقف شدن")
                    await self.show(event, "active", edit=True)
                    return
                elif head == "c":
                    clip = self.db.get_clip(int(parts[1]))
                    if not clip:
                        await event.answer("پیدا نشد", alert=True)
                        return
                    try:
                        ids = [i for i in (clip["sheet_msg_id"], clip["msg_id"]) if i]
                        await self.bot.forward_messages(event.chat_id, ids, clip["chat_id"])
                        await event.answer("👇 ارسال شد")
                    except Exception:
                        await event.answer("این ویدیو دیگه در دسترس نیست (شاید پاک شده)", alert=True)
                    return
                elif head == "acc":
                    await self.show(event, "accounts", edit=True)
                elif head == "acct":
                    self._awaiting_input[event.sender_id] = f"acctok:{parts[1]}"
                    await event.answer()
                    await self.toast(event, "🔑 توکن جدید این اکانت رو بفرست (اختیاری: بعدش با فاصله device_id):", ttl=90)
                    return
                elif head == "accrmq":
                    await event.edit("🗑 این اکانت حذف بشه؟", buttons=[[Button.inline("✅ بله", data=f"accrm:{parts[1]}"), Button.inline("‹ انصراف", data="acc")]])
                elif head == "accrm":
                    slot = int(parts[1])
                    if slot in self.rec_mgr.busy:
                        await event.answer("این اکانت الان در حال ضبطه", alert=True)
                        return
                    self.pool.remove(slot)
                    await event.answer("حذف شد")
                    await self.show(event, "accounts", edit=True)
                    return
                elif head == "set":
                    await self._handle_setting_toggle(event, parts[1])
                    return
                else:
                    await event.answer()
                    return
                await event.answer()
            except MessageNotModifiedError:
                await event.answer()
            except Exception as e:
                log.warning("Callback error (%s): %s", data, e)
                try:
                    await event.answer("خطا", alert=False)
                except Exception:
                    pass

    # ------------------------------------------------------------------ actions
    async def _handle_add_watch(self, event: Any, raw: str, panel_edit: bool = False) -> None:
        wait = await self.toast(event, "⏳ در حال شناسایی استریمر...", ttl=30)
        cid, uname, dname, avatar = await self.api.resolve_creator(raw)
        try:
            await wait.delete()
        except Exception:
            pass
        if not cid:
            await self.toast(event, "❌ استریمری با این آیدی/لینک پیدا نشد.")
            return
        added = self.db.add_watch(cid, uname, dname, avatar)
        info = await self.api.check_creator_and_get_stream(cid, light=True)
        if info.display_name:
            self.db.update_watch_status(cid, "live" if info.is_live else "offline", display_name=info.display_name, username=info.username)
        if info.is_live:
            ok, _ = await self.rec_mgr.start_recording(info, chat_id=event.chat_id, triggered_by="watchlist")
            self.monitor._notified_ls[info.creator_id] = info.livestream_id or "live"
        await self.toast(event, ("✅ به واچلیست اضافه شد" if added else "♻️ از قبل توی واچلیست بود") +
                         (" · ⏺ لایوه، ضبط شروع شد" if info.is_live else ""), ttl=6)
        await self.show(event, "person", info.creator_id or cid, 0, edit=panel_edit)

    async def _handle_manual_rec(self, event: Any, raw: str, panel_edit: bool = False) -> None:
        wait = await self.toast(event, "🔍 در حال بررسی لایو...", ttl=30)
        cid, _, dname, _ = await self.api.resolve_creator(raw)
        if not cid:
            try:
                await wait.delete()
            except Exception:
                pass
            await self.toast(event, "❌ استریمری با این آیدی/لینک پیدا نشد.")
            return
        info = await self.api.check_creator_and_get_stream(cid, light=True)
        try:
            await wait.delete()
        except Exception:
            pass
        if not info.is_live:
            await self.toast(event, f"⚫ <b>{html.escape(info.display_name or dname or cid)}</b> الان آفلاینه.", ttl=8)
        else:
            ok, msg_text = await self.rec_mgr.start_recording(info, chat_id=event.chat_id, triggered_by="manual")
            await self.toast(event, msg_text, ttl=6 if ok else 60)
        await self.show(event, "person", info.creator_id or cid, 0, edit=panel_edit)

    async def _handle_setting_toggle(self, event: Any, key: str) -> None:
        g, st = self.db.get_setting, self.db.set_setting

        def cycle(name: str, opts: List[str], default: str) -> None:
            cur = g(name, default)
            st(name, opts[(opts.index(cur) + 1) % len(opts)] if cur in opts else opts[0])

        if key == "notify":
            st("notify_live", "0" if g("notify_live", "1") == "1" else "1")
        elif key == "ttl":
            cycle("notify_ttl", ["30", "60", "300", "0"], "60")
        elif key == "split":
            cycle("split_minutes", ["0", "10", "20", "30", "60"], "0")
        elif key == "part":
            cycle("part_mb", ["500", "1000", "1500", "1900"], "1900")
        elif key == "poll":
            cycle("poll_interval", ["15", "25", "45", "60"], str(DEFAULT_POLL_INTERVAL))

        await event.answer("ذخیره شد ✅")
        await self.show(event, "settings", edit=True)

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
        if not BOT_TOKEN:
            raise RuntimeError("BOT_TOKEN is missing. Set it as an environment variable before starting the bot.")
        stale = self.db.mark_stale_recordings_interrupted()
        if stale:
            log.info("Marked %d stale recordings as interrupted.", stale)
        # Preserve interrupted/failed recordings for recovery; successfully sent MP4 parts are deleted immediately.
        self._register_handlers()
        health_runner = await self._start_health_server()
        await self.bot.start(bot_token=BOT_TOKEN)
        me = await self.bot.get_me()
        log.info("Logged in as @%s | FFmpeg=%s | Agora=%s", me.username, FFMPEG_BIN, AGORA_AVAILABLE)
        try:
            from telethon.tl.functions.bots import SetBotCommandsRequest
            from telethon.tl.types import BotCommand, BotCommandScopeDefault
            await self.bot(SetBotCommandsRequest(scope=BotCommandScopeDefault(), lang_code="", commands=[
                BotCommand("start", "داشبورد و منوی پایین"),
                BotCommand("rec", "ضبط فوری با آیدی یا لینک"),
                BotCommand("watch", "افزودن به واچلیست"),
                BotCommand("check", "بررسی وضعیت استریمر"),
                BotCommand("debug", "گزارش فنی API"),
            ]))
        except Exception as e:
            log.debug("set commands failed: %s", e)
        owner = self.db.get_owner_id()
        if owner:
            try:
                await self._ensure_keyboard(owner, force=True)
            except Exception as e:
                log.debug("keyboard restore failed: %s", e)
        self.monitor.start()
        bg_task(self._panel_refresher())
        try:
            await self.bot.run_until_disconnected()
        finally:
            await self.monitor.stop()
            await self.rec_mgr.stop_all()
            tasks = [r.task for r in list(self.rec_mgr.active.values()) if r.task]
            if tasks:
                log.info("Waiting for %d recordings to finalize...", len(tasks))
                await asyncio.wait(tasks, timeout=900)
            await self.pool.close()
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
