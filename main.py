#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SuperLiveRec Personal Unlimited Pro (v2.1)
==========================================
Personal SuperLive livestream recorder & watchlist bot for Telegram.
All known bugs fixed. Complete & runnable.
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
from telethon import Button, TelegramClient, events
from telethon.errors import FloodWaitError
try:
    from telethon.errors import MessageNotModifiedError
except ImportError:
    from telethon.errors.rpcerrorlist import MessageNotModifiedError
from telethon.tl.types import DocumentAttributeVideo

# ==============================================================================
# CONFIGURATION
# ==============================================================================

def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()

# Bot token is REQUIRED — no hardcoded fallback.
BOT_TOKEN = _env("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("8837565058:AAExoYiFvTB7aq8aZHPENBu_qaWIxlgV6pM")

TG_API_ID = int(_env("TG_API_ID", "2040") or "2040")
TG_API_HASH = _env("TG_API_HASH", "b18441a1ff607e10a989891a5462e627")

OWNER_ID = int(_env("OWNER_ID", "0") or "0")

SUPERLIVE_API_BASE = _env("SUPERLIVE_API_BASE", "https://api.spl-live.com/api/web").rstrip("/")
DEFAULT_AGORA_APP_ID = _env("AGORA_APP_ID", "dca16fbf522b4c6f96ecc88721800310")

BASE_DIR = Path(_env("DATA_DIR", "./superliverec_data")).resolve()
RECORDINGS_DIR = BASE_DIR / "recordings"
DB_PATH = BASE_DIR / "superliverec.db"
SESSION_PATH = BASE_DIR / "superliverec_bot_session"

BASE_DIR.mkdir(parents=True, exist_ok=True)
RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
FAILED_DIR = BASE_DIR / "failed_uploads"
FAILED_DIR.mkdir(parents=True, exist_ok=True)

MAX_SINGLE_FILE_BYTES = int(_env("MAX_FILE_MB", "1900")) * 1024 * 1024
MIN_FREE_DISK_MB = int(_env("MIN_FREE_DISK_MB", "500"))
AGORA_INACTIVITY_TIMEOUT = int(_env("AGORA_INACTIVITY_TIMEOUT", "45"))
VIDEO_STALL_TIMEOUT = int(_env("VIDEO_STALL_TIMEOUT", "12"))
DEFAULT_POLL_INTERVAL = int(_env("POLL_INTERVAL", "25"))
DEFAULT_MAX_CONCURRENT = max(1, int(_env("MAX_CONCURRENT_RECORDINGS", "2")))
MAX_PARALLEL_UPLOADS = int(_env("MAX_PARALLEL_UPLOADS", "3"))

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
# OPTIONAL DEPENDENCIES
# ==============================================================================

def find_ffmpeg() -> Optional[str]:
    b = shutil.which("ffmpeg")
    if b:
        return b
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
        AudioScenarioType, ChannelProfileType, ClientRoleType,
        VideoStreamType, VideoSubscriptionOptions,
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
except Exception as _e:
    AGORA_IMPORT_ERROR = str(_e)
    IAudioFrameObserver = object  # type: ignore
    IVideoEncodedFrameObserver = object  # type: ignore
    IRTCConnectionObserver = object  # type: ignore

# ==============================================================================
# HELPERS
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
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def get_disk_stats(path: Path = BASE_DIR) -> Tuple[float, float, float]:
    u = shutil.disk_usage(str(path))
    mb = 1024 * 1024
    return u.used / mb, u.free / mb, u.total / mb


def get_ram_usage_mb() -> Tuple[float, float]:
    try:
        cur = Path("/sys/fs/cgroup/memory.current")
        mx = Path("/sys/fs/cgroup/memory.max")
        if cur.exists() and mx.exists():
            used = int(cur.read_text().strip()) / 1048576
            raw = mx.read_text().strip()
            if raw.isdigit():
                return used, int(raw) / 1048576
    except Exception:
        pass
    try:
        txt = Path("/proc/meminfo").read_text()
        vals: Dict[str, int] = {}
        for line in txt.splitlines():
            p = line.split()
            if len(p) >= 2:
                vals[p[0].rstrip(":")] = int(p[1])
        t = vals.get("MemTotal", 0) / 1024.0
        a = vals.get("MemAvailable", vals.get("MemFree", 0)) / 1024.0
        return t - a, t
    except Exception:
        return 0.0, 0.0


def extract_creator_identifier(raw_input: str) -> str:
    text = raw_input.strip()
    if not text:
        return ""
    m = re.search(r"/livestream/(\d{4,16})", text)
    if m:
        return f"ls:{m.group(1)}"
    if text.lower().startswith("ls:") and text[3:].isdigit():
        return text.lower()
    um = re.search(r"https?://[^\s]+", text)
    if um:
        url = um.group(0)
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        for key in ("user_id", "uid", "id", "creator_id", "host_id", "profile_id"):
            if key in qs and qs[key]:
                return qs[key][0].strip()
        segs = [s for s in parsed.path.split("/") if s]
        for s in reversed(segs):
            if s.isdigit():
                return s
        if segs:
            return segs[-1].lstrip("@").strip()
    if re.fullmatch(r"\d{4,16}", text):
        return text
    return text.lstrip("@").strip()


try:
    from zoneinfo import ZoneInfo  # type: ignore
    LOCAL_TZ: Any = ZoneInfo(_env("TZ_NAME", "Asia/Tehran"))
except Exception:
    LOCAL_TZ = datetime.timezone.utc

_BG_TASKS: set = set()


def bg_task(coro: Any) -> "asyncio.Task":
    t = asyncio.create_task(coro)
    _BG_TASKS.add(t)
    t.add_done_callback(_BG_TASKS.discard)
    return t


def schedule_delete(msg: Any, delay: float) -> None:
    if msg is None:
        return
    async def _d():
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
    """Extract app_id/channel/uid from an Agora AccessToken2 (007...)."""
    if not isinstance(token, str) or not token.startswith("007"):
        return {}
    try:
        raw = token[3:]
        raw += "=" * (-len(raw) % 4)
        buf = zlib.decompress(base64.b64decode(raw))

        def need(n: int) -> None:
            if pos + n > len(buf):
                raise ValueError("token truncated")

        pos = 0

        def u16() -> int:
            nonlocal pos
            need(2)
            v = struct.unpack_from("<H", buf, pos)[0]
            pos += 2
            return v

        def u32() -> int:
            nonlocal pos
            need(4)
            v = struct.unpack_from("<I", buf, pos)[0]
            pos += 4
            return v

        def rstr() -> bytes:
            nonlocal pos
            n = u16()
            need(n)
            v = buf[pos:pos + n]
            pos += n
            return v

        rstr()
        out: Dict[str, Any] = {"app_id": rstr().decode("utf-8", "ignore")}
        out["issue_ts"] = u32()
        out["expire"] = u32()
        u32()
        svc_count = u16()
        for _ in range(svc_count):
            if pos + 2 > len(buf):
                break
            stype = u16()
            priv_count = u16()
            for _ in range(priv_count):
                u16()
                u32()
            if stype == 1:
                out["channel"] = rstr().decode("utf-8", "ignore")
                out["uid"] = rstr().decode("utf-8", "ignore")
                break
            rstr()
        return out
    except Exception as e:
        return {"error": str(e)}


# ==============================================================================
# DATABASE
# ==============================================================================

class Database:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._db: Optional[sqlite3.Connection] = None
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        if self._db is None:
            self._db = sqlite3.connect(str(self.path), check_same_thread=False)
            self._db.row_factory = sqlite3.Row
        return self._db

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                try:
                    self._db.close()
                except Exception:
                    pass
                self._db = None

    def _init_db(self) -> None:
        with self._lock, self._conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
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

    def get_setting(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

    def get_owner_id(self) -> int:
        if OWNER_ID > 0:
            return OWNER_ID
        v = self.get_setting("owner_id", "0")
        return int(v) if v.isdigit() else 0

    def set_owner_id(self, uid: int) -> None:
        self.set_setting("owner_id", str(uid))

    def add_watch(self, creator_id: str, username: str = "", display_name: str = "", avatar_url: str = "") -> bool:
        now = datetime.datetime.utcnow().isoformat()
        with self._lock, self._conn() as c:
            row = c.execute("SELECT creator_id FROM watchlist WHERE creator_id = ?", (creator_id,)).fetchone()
            if row:
                c.execute(
                    "UPDATE watchlist SET username = COALESCE(NULLIF(?, ''), username), "
                    "display_name = COALESCE(NULLIF(?, ''), display_name), "
                    "avatar_url = COALESCE(NULLIF(?, ''), avatar_url), auto_record = 1 "
                    "WHERE creator_id = ?",
                    (username, display_name, avatar_url, creator_id),
                )
                return False
            c.execute(
                "INSERT INTO watchlist (creator_id, username, display_name, avatar_url, added_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (creator_id, username, display_name or username or creator_id, avatar_url, now),
            )
            return True

    def remove_watch(self, creator_id: str) -> bool:
        with self._lock, self._conn() as c:
            return c.execute("DELETE FROM watchlist WHERE creator_id = ?", (creator_id,)).rowcount > 0

    def toggle_auto_record(self, creator_id: str) -> Optional[int]:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT auto_record FROM watchlist WHERE creator_id = ?", (creator_id,)).fetchone()
            if not row:
                return None
            nv = 0 if row["auto_record"] else 1
            c.execute("UPDATE watchlist SET auto_record = ? WHERE creator_id = ?", (nv, creator_id))
            return nv

    def list_watchlist(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn().execute(
                "SELECT * FROM watchlist ORDER BY (last_status = 'live') DESC, added_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_watch(self, creator_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn().execute("SELECT * FROM watchlist WHERE creator_id = ?", (creator_id,)).fetchone()
            return dict(row) if row else None

    def update_watch_status(self, creator_id: str, status: str, display_name: str = "", username: str = "",
                            increment_recordings: bool = False) -> None:
        now = datetime.datetime.utcnow().isoformat()
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE watchlist SET last_status = ?, "
                "last_seen_live = CASE WHEN ? = 'live' THEN ? ELSE last_seen_live END, "
                "display_name = COALESCE(NULLIF(?, ''), display_name), "
                "username = COALESCE(NULLIF(?, ''), username), "
                "total_recordings = total_recordings + ? WHERE creator_id = ?",
                (status, status, now, display_name, username, 1 if increment_recordings else 0, creator_id),
            )

    def create_recording_record(self, creator_id: str, creator_name: str, livestream_id: str = "") -> int:
        now = datetime.datetime.utcnow().isoformat()
        with self._lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO recordings (creator_id, creator_name, livestream_id, started_at, status) "
                "VALUES (?, ?, ?, ?, 'recording')",
                (creator_id, creator_name, livestream_id, now),
            )
            return int(cur.lastrowid)

    def finish_recording_record(self, rec_id: int, status: str, duration_sec: int, file_size_bytes: int,
                                tg_chat_id: int = 0, tg_message_id: int = 0, archive_chat_id: int = 0,
                                archive_message_id: int = 0, notes: str = "") -> None:
        now = datetime.datetime.utcnow().isoformat()
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE recordings SET ended_at = ?, duration_sec = ?, file_size_bytes = ?, status = ?, "
                "tg_chat_id = ?, tg_message_id = ?, archive_chat_id = ?, archive_message_id = ?, notes = ? "
                "WHERE id = ?",
                (now, duration_sec, file_size_bytes, status, tg_chat_id, tg_message_id,
                 archive_chat_id, archive_message_id, notes, rec_id),
            )

    def list_recent_recordings(self, limit: int = 15, offset: int = 0) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn().execute(
                "SELECT * FROM recordings ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
            return [dict(r) for r in rows]

    def count_recordings(self) -> int:
        with self._lock:
            row = self._conn().execute(
                "SELECT COUNT(*) AS c FROM recordings WHERE status = 'completed'"
            ).fetchone()
            return int(row["c"]) if row else 0

    def add_clip(self, rec_id: int, creator_id: str, creator_name: str, part_no: int,
                 duration_sec: int, size_bytes: int, chat_id: int, msg_id: int, sheet_msg_id: int = 0) -> None:
        now = datetime.datetime.utcnow().isoformat()
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO clips (rec_id, creator_id, creator_name, part_no, created_at, duration_sec, "
                "size_bytes, chat_id, msg_id, sheet_msg_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (rec_id, creator_id, creator_name, part_no, now, duration_sec, size_bytes,
                 chat_id, msg_id, sheet_msg_id),
            )

    def list_clips(self, creator_id: str = "", limit: int = 8, offset: int = 0) -> List[Dict[str, Any]]:
        with self._lock:
            if creator_id:
                rows = self._conn().execute(
                    "SELECT * FROM clips WHERE creator_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
                    (creator_id, limit, offset),
                ).fetchall()
            else:
                rows = self._conn().execute(
                    "SELECT * FROM clips ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
                ).fetchall()
            return [dict(r) for r in rows]

    def clip_stats(self, creator_id: str = "") -> Tuple[int, int, int]:
        with self._lock:
            q = "SELECT COUNT(*) c, COALESCE(SUM(duration_sec),0) d, COALESCE(SUM(size_bytes),0) s FROM clips"
            if creator_id:
                row = self._conn().execute(q + " WHERE creator_id = ?", (creator_id,)).fetchone()
            else:
                row = self._conn().execute(q).fetchone()
            return int(row["c"]), int(row["d"]), int(row["s"])

    def get_clip(self, clip_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn().execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
            return dict(row) if row else None

    def migrate_watch(self, old_id: str, new_id: str) -> None:
        if not new_id or old_id == new_id:
            return
        with self._lock, self._conn() as c:
            if c.execute("SELECT 1 FROM watchlist WHERE creator_id = ?", (new_id,)).fetchone():
                c.execute("DELETE FROM watchlist WHERE creator_id = ?", (old_id,))
            else:
                c.execute("UPDATE watchlist SET creator_id = ? WHERE creator_id = ?", (new_id, old_id))

    def increment_recordings(self, creator_id: str) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE watchlist SET total_recordings = total_recordings + 1 WHERE creator_id = ?",
                (creator_id,),
            )

    def mark_stale_recordings_interrupted(self) -> int:
        now = datetime.datetime.utcnow().isoformat()
        with self._lock, self._conn() as c:
            cur = c.execute(
                "UPDATE recordings SET status = 'interrupted', ended_at = ?, "
                "notes = 'Server restarted during capture' WHERE status = 'recording'",
                (now,),
            )
            return cur.rowcount

# ==============================================================================
# SUPERLIVE API CLIENT
# ==============================================================================

@dataclass
class CreatorStreamInfo:
    creator_id: str
    username: str = ""
    display_name: str = ""
    avatar_url: str = ""
    is_live: bool = False
    livestream_id: str = ""
    stream_url: str = ""
    title: str = ""
    viewers: int = 0
    raw_debug: Dict[str, Any] = field(default_factory=dict)


class AccountPool:
    def __init__(self, db: Database):
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
        env_tok = _env("SUPERLIVE_TOKEN") if slot == 0 else ""
        env_dev = _env("SUPERLIVE_DEVICE_ID") if slot == 0 else ""
        self._token: str = env_tok or db.get_setting(self._k("superlive_token"), "")
        self._device_id: str = env_dev or db.get_setting(self._k("superlive_device_id"), "") or uuid.uuid4().hex
        self._auth_scheme: str = db.get_setting(self._k("superlive_auth_scheme"), "Token")
        self._rtc_id: str = (
            (_env("SUPERLIVE_RTC_ID") if slot == 0 else "")
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
        for k in ("superlive_token", "superlive_device_id", "superlive_auth_scheme",
                  "superlive_rtc_id", "superlive_label"):
            self.db.set_setting(self._k(k), "")

    def _client_params(self, source_url: str = "https://superlive.chat/") -> Dict[str, Any]:
        return {
            "os_type": "web", "app_build": "5.25.23", "app": "superlive",
            "build_code": "1526-2991979-prod", "app_language": "en",
            "device_language": "en", "device_preferred_languages": ["en-US", "en"],
            "source_url": source_url, "session_source_url": "https://superlive.chat/",
            "referrer": "", "incognito": False, "rtc_id": self._rtc_id,
            "web_type": 1, "display_density": "2.75",
            "display_resolution_width": 393, "display_resolution_height": 873,
        }

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=25),
                cookie_jar=aiohttp.CookieJar(unsafe=True),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _headers(self) -> Dict[str, str]:
        h = {
            "User-Agent": ("Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36"),
            "Accept": "application/json", "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json", "Device-Id": self._device_id,
            "Origin": "https://superlive.chat", "Referer": "https://superlive.chat/",
        }
        if self._token:
            h["Authorization"] = f"{self._auth_scheme} {self._token}"
        return h

    def set_credentials(self, token: str, device_id: str = "") -> None:
        self._token = token.strip()
        self.db.set_setting(self._k("superlive_token"), self._token)
        if device_id.strip():
            self._device_id = device_id.strip()
            self.db.set_setting(self._k("superlive_device_id"), self._device_id)

    async def _post(self, endpoint: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{SUPERLIVE_API_BASE}/{endpoint.lstrip('/')}"
        body = dict(payload or {})
        body.setdefault("client_params", self._client_params())
        try:
            sess = await self._get_session()
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
        try:
            sess = await self._get_session()
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

    async def _request_smart(self, endpoint: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        res = await self._post(endpoint, payload)
        status = res.get("_http_status", 0)
        if status in (401, 403) and self._token:
            alt = "Bearer" if self._auth_scheme == "Token" else "Token"
            prev = self._auth_scheme
            self._auth_scheme = alt
            retry = await self._post(endpoint, payload)
            if retry.get("_http_status", 0) in (200, 201):
                self.db.set_setting(self._k("superlive_auth_scheme"), alt)
                return retry
            self._auth_scheme = prev
        if status in (404, 405):
            gr = await self._get(endpoint, payload)
            if gr.get("_http_status", 0) in (200, 201):
                return gr
        return res

    @staticmethod
    def _deep_find_keys(obj: Any, keys: Tuple[str, ...]) -> Dict[str, Any]:
        found: Dict[str, Any] = {}
        stack: List[Any] = [obj]
        seen = 0
        while stack and seen < 400:
            cur = stack.pop()
            seen += 1
            if isinstance(cur, dict):
                for k, v in cur.items():
                    kl = str(k).lower()
                    if kl in keys and kl not in found and v not in (None, "", [], {}):
                        found[kl] = v
                    if isinstance(v, (dict, list)):
                        stack.append(v)
            elif isinstance(cur, list):
                for it in cur[:50]:
                    if isinstance(it, (dict, list)):
                        stack.append(it)
        return found

    async def ensure_initialized(self, force: bool = False) -> Dict[str, Any]:
        if self._token and not force:
            return {"_cached": True}
        data = await self._request_smart(
            "initialize",
            {"device_id": self._device_id, "platform": "web", "language": "en"},
        )
        found = self._deep_find_keys(data, ("token", "access_token", "auth_token", "jwt", "session_token"))
        token = found.get("access_token") or found.get("token") or found.get("auth_token") \
            or found.get("jwt") or found.get("session_token")
        if token:
            self._token = str(token)
            self.db.set_setting(self._k("superlive_token"), self._token)
        return data

    @staticmethod
    def _extract_avatar(u: Dict[str, Any]) -> str:
        img = u.get("profile_image") or u.get("avatar")
        if isinstance(img, dict):
            return str(img.get("url") or img.get("thumbnail_url") or "")
        return str(img or "")

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
        skip = {"profile_image", "avatar", "thumbnail", "cover", "image",
                "badge", "badges", "gift", "gifts", "icon", "icons", "banner"}
        img_mark = ("cdn.spl-live.com/pp/", "/pp/", "/avatar", "/thumbnail",
                    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", "_s", "_b")
        for k in ("hls_url", "flv_url", "stream_url", "play_url", "rtmp_url",
                  "m3u8_url", "pull_url", "playback_url"):
            v = obj.get(k)
            if isinstance(v, str):
                vl = v.lower()
                if any(m in vl for m in img_mark):
                    continue
                if (vl.startswith(("rtmp://", "rtmps://", "agora://"))
                        or (vl.startswith(("http://", "https://"))
                            and any(e in vl for e in (".m3u8", ".flv")))):
                    return v
        for k, v in obj.items():
            if str(k).lower() in skip:
                continue
            if isinstance(v, dict):
                r = SuperLiveClient._extract_direct_stream_url(v)
                if r:
                    return r
        return ""

    async def _search_by_shared_id(self, ident: str) -> Dict[str, Any]:
        for payload in ({"shared_id": str(ident)}, {"search_text": str(ident)},
                        {"query": str(ident), "keyword": str(ident)}):
            res = await self._request_smart("user/profile_share_search", payload)
            if res.get("_http_status") not in (200, 201):
                continue
            u = self._extract_user_dict(res)
            if u and (u.get("user_id") or u.get("id")):
                return u
            for lk in ("users", "items", "results", "data"):
                arr = res.get(lk)
                if isinstance(arr, list):
                    for cand in arr:
                        if not isinstance(cand, dict):
                            continue
                        sub = cand.get("user") if isinstance(cand.get("user"), dict) else cand
                        if str(sub.get("shared_id") or "") == str(ident) \
                                or str(sub.get("user_id") or "") == str(ident):
                            return sub
                    if arr and isinstance(arr[0], dict):
                        s0 = arr[0].get("user") if isinstance(arr[0].get("user"), dict) else arr[0]
                        if s0.get("user_id") or s0.get("id"):
                            return s0
        return {}

    async def resolve_creator(self, raw_query: str) -> Tuple[str, str, str, str]:
        await self.ensure_initialized()
        ident = extract_creator_identifier(raw_query)
        if not ident:
            return "", "", "", ""

        if ident.startswith("ls:"):
            ls_id = ident[3:]
            ret = await self._request_smart("livestream/retrieve", {"livestream_id": ls_id})
            owner = self._deep_find_keys(ret, ("livestream_owner_id",)).get("livestream_owner_id")
            u = ret.get("user") if isinstance(ret.get("user"), dict) else {}
            if not u:
                lo = ret.get("livestream") if isinstance(ret.get("livestream"), dict) else {}
                u = lo.get("user") if isinstance(lo.get("user"), dict) else {}
            cid = str(owner or u.get("user_id") or u.get("id") or "")
            if cid:
                un = str(u.get("username") or u.get("shared_id") or "")
                dn = str(u.get("name") or un or cid)
                return cid, un, dn, self._extract_avatar(u) if u else ""
            return "", "", "", ""

        if ident.isdigit():
            prof = await self._request_smart("users/profile", {"user_id": str(ident)})
            uo = self._extract_user_dict(prof)
            if uo and not prof.get("error"):
                cid = str(uo.get("user_id") or uo.get("id") or ident)
                un = str(uo.get("username") or uo.get("shared_id") or "")
                dn = str(uo.get("name") or uo.get("nickname") or un or cid)
                return cid, un, dn, self._extract_avatar(uo)

        fu = await self._search_by_shared_id(ident)
        if fu:
            cid = str(fu.get("user_id") or fu.get("id") or ident)
            un = str(fu.get("username") or fu.get("shared_id") or "")
            dn = str(fu.get("name") or fu.get("nickname") or un or cid)
            return cid, un, dn, self._extract_avatar(fu)

        return ident, "", ident, ""

    def _build_agora_uri(self, payload: Dict[str, Any], ls_id: str = "",
                         debug_out: Optional[Dict[str, Any]] = None) -> str:
        flat: Dict[str, Any] = {}
        rtc_tokens: List[Tuple[str, str]] = []
        any_tokens: List[Tuple[str, str]] = []
        summary: List[str] = []

        stack: List[Tuple[str, Any]] = [("", payload)]
        while stack:
            prefix, cur = stack.pop()
            if isinstance(cur, dict):
                for k, v in cur.items():
                    kl = str(k).lower()
                    fk = f"{prefix}.{k}" if prefix else str(k)
                    if isinstance(v, (dict, list)):
                        stack.append((fk, v))
                    else:
                        if any(w in kl for w in ("agora", "rtc", "rtm", "channel", "token", "stream", "uid")):
                            pv = f"len={len(v)}:{v[:6]}.." if isinstance(v, str) and len(v) > 24 else str(v)
                            summary.append(f"{k}={pv}")
                        if isinstance(v, str) and len(v) >= 50 and v.startswith(("006", "007")):
                            if "rtm" not in kl and "chat" not in kl and "msg" not in kl:
                                rtc_tokens.append((kl, v))
                            any_tokens.append((kl, v))
                        if kl not in flat and v not in (None, ""):
                            flat[kl] = v
            elif isinstance(cur, list):
                for idx, it in enumerate(cur[:40]):
                    if isinstance(it, (dict, list)):
                        stack.append((f"{prefix}[{idx}]", it))
                    elif isinstance(it, str) and len(it) >= 50 and it.startswith(("006", "007")):
                        any_tokens.append((prefix.lower(), it))

        if debug_out is not None:
            debug_out["agora_keys"] = ", ".join(summary[:18])

        channel = (
            flat.get("agora_channel_name") or flat.get("agora_channel")
            or flat.get("rtc_channel_name") or flat.get("rtc_channel")
            or flat.get("channel_name") or flat.get("channel_id")
            or flat.get("channel") or ls_id
        )
        token = ""
        for pk in ("agora_channel_token", "agora_token", "agora_rtc_token", "rtc_token",
                   "viewer_token", "audience_token", "subscriber_token", "channel_token", "stream_token"):
            v = flat.get(pk)
            if isinstance(v, str) and len(v) >= 30:
                token = v
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

        app_id = flat.get("agora_app_id") or flat.get("app_id") or DEFAULT_AGORA_APP_ID
        if isinstance(token, str) and token.startswith("006") and len(token) >= 35:
            cand = token[3:35]
            if re.fullmatch(r"[0-9a-fA-F]{32}", cand):
                app_id = cand

        explicit_uid = (flat.get("agora_id") or flat.get("agora_uid") or flat.get("agora_user_id")
                        or flat.get("rtc_uid") or flat.get("viewer_uid") or flat.get("rtc_id") or "")

        ti = decode_agora_token_007(token)
        if debug_out is not None:
            debug_out["token_info"] = str({k: v for k, v in ti.items()})
        if ti.get("app_id") and re.fullmatch(r"[0-9a-fA-F]{32}", str(ti["app_id"])):
            app_id = ti["app_id"]
        if ti.get("channel"):
            channel = ti["channel"]
        if "uid" in ti:
            explicit_uid = ti["uid"] or explicit_uid

        uids: List[str] = []
        for u in (str(explicit_uid), str(self._rtc_id), "0"):
            u = u.strip()
            if u and u not in uids:
                uids.append(u)

        alt = [t for _, t in rtc_tokens if t != token]

        if channel and (token or str(channel) != str(ls_id)):
            qs = {
                "token": str(token), "uid": uids[0] if uids else "0",
                "uids": ",".join(uids), "ls_id": str(ls_id or channel),
            }
            if alt:
                qs["alt_token"] = alt[0]
            return f"agora://{app_id}/{channel}?{urllib.parse.urlencode(qs)}"
        return ""

    async def fetch_discover_map(self, max_pages: int = 4) -> Tuple[Dict[str, Dict[str, Any]], int, str]:
        await self.ensure_initialized()
        live_map: Dict[str, Dict[str, Any]] = {}
        first_http = 0
        first_sample = ""
        cursor: Any = None
        for page_idx in range(max_pages):
            dd = await self._request_smart("discover", {"next": cursor, "type": 0})
            if page_idx == 0:
                first_http = int(dd.get("_http_status") or 0)
                first_sample = json.dumps(dd, ensure_ascii=False)[:700]
            if not isinstance(dd, dict):
                break
            items = (
                dd.get("items") or dd.get("livestreams") or dd.get("streams")
                or (dd.get("data") or {}).get("items")
                or (dd.get("data") or {}).get("livestreams")
                or (dd.get("data") if isinstance(dd.get("data"), list) else [])
                or []
            )
            for it in items:
                if not isinstance(it, dict):
                    continue
                host = it.get("user") or it.get("host") or it.get("broadcaster") or it
                for kv in (host.get("user_id"), host.get("id"), host.get("shared_id"),
                           host.get("short_id"), it.get("user_id"), it.get("shared_id"),
                           str(host.get("username") or "").lower()):
                    if kv:
                        live_map[str(kv)] = it
            cursor = dd.get("next")
            if not cursor:
                break
        return live_map, first_http, first_sample

    async def check_creator_and_get_stream(
        self, creator_id: str,
        discover_map: Optional[Dict[str, Dict[str, Any]]] = None,
        light: bool = False,
    ) -> CreatorStreamInfo:
        init_res = await self.ensure_initialized()
        info = CreatorStreamInfo(creator_id=str(creator_id))
        info.raw_debug["init_status"] = init_res.get("_http_status", "cached")
        info.raw_debug["has_token"] = bool(self._token)

        target_uid = str(creator_id)
        ls_id = ""
        is_live_flag = False
        prof_resp: Dict[str, Any] = {}
        user_obj: Dict[str, Any] = {}

        if discover_map is not None:
            mi = discover_map.get(target_uid) or discover_map.get(target_uid.lower())
            if mi:
                host = mi.get("user") or mi.get("host") or mi.get("broadcaster") or mi
                ls_id = str(host.get("livestream_id") or mi.get("livestream_id") or mi.get("id") or "")
                real_uid = str(host.get("user_id") or host.get("id") or target_uid)
                info.creator_id = real_uid
                info.username = str(host.get("username") or host.get("shared_id") or "")
                info.display_name = str(host.get("name") or host.get("nickname") or info.username or real_uid)
                info.avatar_url = self._extract_avatar(host)
                info.title = str(mi.get("title") or "")
                is_live_flag = bool(ls_id)
                info.raw_debug["discover_cache_hit"] = True
        cache_ls_id = ls_id
        cache_name = info.display_name
        if info.creator_id and info.creator_id != target_uid:
            target_uid = info.creator_id

        prof_resp = await self._request_smart("users/profile", {"user_id": target_uid})
        if prof_resp.get("_http_status") in (401, 403):
            await self.ensure_initialized(force=True)
            prof_resp = await self._request_smart("users/profile", {"user_id": target_uid})

        err_obj = prof_resp.get("error") if isinstance(prof_resp.get("error"), dict) else {}
        if prof_resp.get("_http_status") == 400 and (
            err_obj.get("code") == 9 or "no user" in str(err_obj.get("message", "")).lower()
        ):
            ru = await self._search_by_shared_id(target_uid)
            if ru and (ru.get("user_id") or ru.get("id")):
                target_uid = str(ru.get("user_id") or ru.get("id"))
                info.creator_id = target_uid
                info.raw_debug["resolved_from_shared_id"] = f"{creator_id} -> {target_uid}"
                prof_resp = await self._request_smart("users/profile", {"user_id": target_uid})

        info.raw_debug["profile_http"] = prof_resp.get("_http_status")
        info.raw_debug["profile_sample"] = json.dumps(prof_resp, ensure_ascii=False)[:900]

        user_obj = self._extract_user_dict(prof_resp)
        if user_obj and str(user_obj.get("user_id") or "") not in ("", target_uid, str(creator_id)) \
                and str(user_obj.get("shared_id") or "") != str(creator_id):
            info.raw_debug["profile_mismatch"] = f"got {user_obj.get('user_id')} for {target_uid}"
            user_obj = {}
            prof_resp = {"_http_status": prof_resp.get("_http_status"), "_ignored": "own profile"}
        if user_obj:
            info.username = str(user_obj.get("username") or user_obj.get("shared_id") or "")
            info.display_name = str(user_obj.get("name") or user_obj.get("nickname") or info.username or target_uid)
            info.avatar_url = self._extract_avatar(user_obj)

        deep = self._deep_find_keys(prof_resp, (
            "livestream_id", "live_id", "stream_id", "broadcast_id", "room_id",
            "channel_name", "agora_channel", "is_live", "is_streaming", "live_status",
            "online_status", "livestream", "current_livestream", "active_livestream",
        ))
        ls_id = str(deep.get("livestream_id") or deep.get("live_id") or deep.get("stream_id")
                    or deep.get("broadcast_id") or deep.get("room_id") or cache_ls_id or "")
        for nk in ("livestream", "current_livestream", "active_livestream"):
            nested = deep.get(nk)
            if not ls_id and isinstance(nested, dict):
                ls_id = str(nested.get("id") or nested.get("livestream_id") or nested.get("live_id") or "")

        is_live_flag = bool(
            deep.get("is_live") or deep.get("is_streaming")
            or deep.get("live_status") in (1, True, "live", "online", "LIVE", "ONLINE") or ls_id
        )

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
                items = (
                    disc_data.get("items") or disc_data.get("livestreams") or disc_data.get("streams")
                    or (disc_data.get("data") or {}).get("items")
                    or (disc_data.get("data") or {}).get("livestreams")
                    or (disc_data.get("data") if isinstance(disc_data.get("data"), list) else [])
                    or []
                )
                matched = False
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    host = it.get("user") or it.get("host") or it.get("broadcaster") or it
                    ids = {str(host.get("id") or ""), str(host.get("user_id") or ""),
                           str(host.get("shared_id") or ""), str(host.get("short_id") or ""),
                           str(it.get("user_id") or ""), str(it.get("shared_id") or ""),
                           str(it.get("host_id") or "")}
                    names = {str(host.get("username") or "").lower(),
                             str(host.get("unique_id") or "").lower()}
                    if str(creator_id) in ids or target_uid in ids or str(creator_id).lower() in names:
                        ls_id = str(host.get("livestream_id") or it.get("livestream_id")
                                    or it.get("id") or it.get("live_id") or "")
                        real_uid = str(host.get("user_id") or host.get("id") or "")
                        if real_uid:
                            info.creator_id = real_uid
                        is_live_flag = bool(ls_id)
                        info.title = str(it.get("title") or "")
                        vc = it.get("viewer_count") or it.get("viewers") or 0
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
            return info

        ls_str = str(ls_id)
        ret_resp = await self._request_smart("livestream/retrieve", {"livestream_id": ls_str})
        ent_resp = await self._request_smart("livestream/enter", {"livestream_id": ls_str})

        info.raw_debug["retrieve_http"] = ret_resp.get("_http_status")
        slim = {k: v for k, v in ret_resp.items() if k not in ("user", "target_bar", "top_gifters", "gifts")}
        info.raw_debug["retrieve_sample"] = json.dumps(slim, ensure_ascii=False)[:1100]
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

        for cr in (ent_resp, ret_resp, prof_resp, user_obj):
            if not isinstance(cr, dict):
                continue
            if not info.title:
                info.title = str(cr.get("title")
                                 or (cr.get("livestream") or {}).get("title") or "")
            if not info.viewers:
                vc = cr.get("viewer_count") or (cr.get("livestream") or {}).get("viewer_count") or 0
                if str(vc).isdigit():
                    info.viewers = int(vc)
            direct = self._extract_direct_stream_url(cr)
            if direct:
                info.stream_url = direct
                break

        if not info.stream_url:
            merged = {"retrieve": ret_resp, "enter": ent_resp, "profile": prof_resp}
            uri = self._build_agora_uri(merged, ls_id=ls_id, debug_out=info.raw_debug)
            if uri:
                info.stream_url = uri

        return info

# ==============================================================================
# MEDIA PROCESSING
# ==============================================================================

def probe_video_metadata(video_path: Path) -> Tuple[int, int, int]:
    duration, width, height = 0, 720, 1280
    if FFPROBE_BIN:
        try:
            cmd = [FFPROBE_BIN, "-v", "error", "-select_streams", "v:0",
                   "-show_entries", "stream=width,height:format=duration",
                   "-of", "json", str(video_path)]
            out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=15)
            data = json.loads(out.decode("utf-8", "ignore"))
            streams = data.get("streams") or []
            if streams:
                width = int(streams[0].get("width") or width)
                height = int(streams[0].get("height") or height)
            ds = (data.get("format") or {}).get("duration")
            if ds:
                duration = int(float(ds))
            return duration, width, height
        except Exception:
            pass
    if FFMPEG_BIN:
        try:
            res = subprocess.run([FFMPEG_BIN, "-i", str(video_path)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
            se = res.stderr.decode("utf-8", "ignore")
            m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", se)
            if m:
                duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(float(m.group(3)))
            m2 = re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})", se)
            if m2:
                width, height = int(m2.group(1)), int(m2.group(2))
        except Exception:
            pass
    return duration, width, height


def build_mux_command(ffmpeg_bin: str, h264: Path, pcm: Optional[Path], out: Path,
                      fps: float = 25.0, sample_rate: int = 48000, channels: int = 1) -> List[str]:
    sf = max(10.0, min(60.0, float(fps or 25.0)))
    has_v = h264.exists() and h264.stat().st_size > 1024
    has_a = bool(pcm and pcm.exists() and pcm.stat().st_size > 1024)

    if has_v and has_a and pcm is not None:
        return [ffmpeg_bin, "-y", "-fflags", "+genpts", "-r", f"{sf:.2f}", "-f", "h264",
                "-i", str(h264), "-f", "s16le", "-ar", str(sample_rate), "-ac", str(channels),
                "-i", str(pcm), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
                "-c:a", "aac", "-b:a", "128k", "-af", "aresample=async=1",
                "-movflags", "+faststart", str(out)]
    if has_v:
        return [ffmpeg_bin, "-y", "-fflags", "+genpts", "-r", f"{sf:.2f}", "-f", "h264",
                "-i", str(h264), "-c:v", "copy", "-movflags", "+faststart", str(out)]
    if has_a and pcm is not None:
        return [ffmpeg_bin, "-y", "-f", "s16le", "-ar", str(sample_rate), "-ac", str(channels),
                "-i", str(pcm), "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart", str(out)]
    return []


def generate_thumbnail_and_contact_sheet(video_path: Path, thumb_path: Path, sheet_path: Path,
                                         duration_sec: int, make_sheet: bool = True
                                         ) -> Tuple[Optional[Path], Optional[Path]]:
    if not FFMPEG_BIN or not video_path.exists():
        return None, None
    out_thumb: Optional[Path] = None
    out_sheet: Optional[Path] = None
    seek = min(max(1, duration_sec // 5), 10)
    try:
        subprocess.run([FFMPEG_BIN, "-y", "-ss", str(seek), "-i", str(video_path),
                        "-vframes", "1", "-vf", "scale=320:-2", "-q:v", "4", str(thumb_path)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20, check=False)
        if thumb_path.exists() and thumb_path.stat().st_size > 500:
            out_thumb = thumb_path
    except Exception:
        pass
    if make_sheet and duration_sec >= 3:
        try:
            fps_val = max(0.0005, 12.0 / max(12.0, float(duration_sec)))
            vf = f"fps={fps_val:.6f},scale=320:-2,tile=4x3:padding=4:margin=4:color=0x111111"
            subprocess.run([FFMPEG_BIN, "-y", "-i", str(video_path), "-vf", vf,
                            "-frames:v", "1", "-q:v", "3", str(sheet_path)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=45, check=False)
            if sheet_path.exists() and sheet_path.stat().st_size > 1000:
                out_sheet = sheet_path
        except Exception:
            pass
    return out_thumb, out_sheet

# ==============================================================================
# AGORA SINGLETON & RECEIVER
# ==============================================================================

class AgoraServiceManager:
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
        try:
            buf = kwargs.get("image_buffer")
            length = kwargs.get("length")
            if buf is None:
                vals = list(args)
                for i, a in enumerate(vals):
                    if isinstance(a, (bytes, bytearray, memoryview)) or (
                        hasattr(a, "__len__") and not isinstance(a, str) and len(a) > 16
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
        af = kwargs.get("audio_frame")
        if af is None:
            for a in args:
                if hasattr(a, "buffer"):
                    af = a
                    break
        if af is not None:
            self.receiver.handle_audio_frame(af)
        return 1

    def on_playback_audio_frame(self, *args: Any, **kwargs: Any) -> int:
        return self.on_playback_audio_frame_before_mixing(*args, **kwargs)


class _ConnObserver(IRTCConnectionObserver):  # type: ignore
    def __init__(self, receiver: "AgoraStreamReceiver"):
        super().__init__()
        self.receiver = receiver

    def on_connected(self, *a, **k):
        self.receiver.conn_events.append("connected")

    def on_disconnected(self, *a, **k):
        r = a[-1] if a else k.get("reason", "?")
        self.receiver.conn_events.append(f"disconn({r})")
        self.receiver.event_times.append((time.time(), "drop"))

    def on_connecting(self, *a, **k):
        self.receiver.conn_events.append("connecting")

    def on_user_joined(self, *a, **k):
        u = a[-1] if a else k.get("user_id", "?")
        self.receiver.conn_events.append(f"joined({u})")

    def on_connection_failure(self, *a, **k):
        r = a[-1] if a else k.get("reason", "?")
        self.receiver.conn_events.append(f"fail({r})")
        self.receiver.event_times.append((time.time(), "drop"))


class AgoraStreamReceiver:
    def __init__(self, agora_uri: str, h264_path: Path, pcm_path: Path):
        p = urllib.parse.urlparse(agora_uri)
        qs = urllib.parse.parse_qs(p.query)
        self.app_id = p.netloc or DEFAULT_AGORA_APP_ID
        self.channel_name = p.path.lstrip("/")
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
            data = bytes(image_buffer[:length]) if not isinstance(image_buffer, (bytes, bytearray)) \
                else bytes(image_buffer[:length])
            with self._io_lock:
                if self._closed:
                    return
                self.last_frame_at = time.time()
                if self._need_keyframe:
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
        import agora.rtc.agora_service as _svc  # type: ignore
        import agora.rtc.agora_base as _base  # type: ignore
        pub_cls = None
        try:
            sig = inspect.signature(svc.create_rtc_connection)
            prm = sig.parameters.get("publish_config")
            if prm and prm.annotation not in (inspect.Parameter.empty, Any, None):
                ann = prm.annotation
                if isinstance(ann, type):
                    pub_cls = ann
                elif isinstance(ann, str):
                    pub_cls = getattr(_svc, ann, None) or getattr(_base, ann, None)
        except Exception:
            pass
        if pub_cls is None:
            mods = [_svc, _base]
            try:
                import agora.rtc.rtc_connection as _conn  # type: ignore
                mods.append(_conn)
            except Exception:
                pass
            for mod in mods:
                for cn in ("RTCConnPublishConfig", "PublishConfig", "PublishConfigure"):
                    cand = getattr(mod, cn, None)
                    if isinstance(cand, type):
                        pub_cls = cand
                        break
                if pub_cls is not None:
                    break
        if pub_cls is None:
            return None
        cfg = pub_cls()
        for a in ("is_publish_audio", "is_publish_video", "publish_audio", "publish_video"):
            if hasattr(cfg, a):
                setattr(cfg, a, False)
        if hasattr(cfg, "audio_scenario"):
            try:
                cfg.audio_scenario = AudioScenarioType.AUDIO_SCENARIO_CHORUS
            except Exception:
                pass
        return cfg

    def connect(self) -> None:
        svc = AgoraServiceManager.get_service(self.app_id)
        cc = RTCConnConfig(
            client_role_type=ClientRoleType.CLIENT_ROLE_AUDIENCE,
            channel_profile=ChannelProfileType.CHANNEL_PROFILE_LIVE_BROADCASTING,
        )
        for a, v in (("auto_subscribe_audio", 1), ("auto_subscribe_video", 1),
                     ("enable_audio_recording_or_playout", 1)):
            if hasattr(cc, a):
                try:
                    setattr(cc, a, v)
                except Exception:
                    pass
        params = inspect.signature(svc.create_rtc_connection).parameters
        if len(params) >= 2:
            self._conn = svc.create_rtc_connection(cc, self._build_publish_config(svc))
        else:
            self._conn = svc.create_rtc_connection(cc)

        self._video_obs = _VideoEncodedObserver(self)
        self._audio_obs = _AudioPcmObserver(self)

        lu = self._conn.get_local_user() if hasattr(self._conn, "get_local_user") else None
        if lu is not None:
            if hasattr(lu, "set_user_role"):
                lu.set_user_role(ClientRoleType.CLIENT_ROLE_AUDIENCE)
            if hasattr(lu, "subscribe_all_audio"):
                lu.subscribe_all_audio()
            if hasattr(lu, "subscribe_all_video"):
                try:
                    so = VideoSubscriptionOptions(
                        type=VideoStreamType.VIDEO_STREAM_HIGH, encoded_frame_only=1)
                except TypeError:
                    so = VideoSubscriptionOptions()
                    if hasattr(so, "type"):
                        so.type = VideoStreamType.VIDEO_STREAM_HIGH
                    if hasattr(so, "encoded_frame_only"):
                        so.encoded_frame_only = 1
                    elif hasattr(so, "encodedFrameOnly"):
                        so.encodedFrameOnly = 1
                lu.subscribe_all_video(so)
            if hasattr(lu, "set_playback_audio_frame_before_mixing_parameters"):
                lu.set_playback_audio_frame_before_mixing_parameters(self.channels, self.sample_rate)
            if hasattr(lu, "register_video_encoded_frame_observer"):
                lu.register_video_encoded_frame_observer(self._video_obs)
            elif hasattr(lu, "set_encoded_video_frame_observer"):
                lu.set_encoded_video_frame_observer(self._video_obs)
            elif hasattr(self._conn, "register_video_encoded_frame_observer"):
                self._conn.register_video_encoded_frame_observer(self._video_obs)
            elif hasattr(self._conn, "set_encoded_video_frame_observer"):
                self._conn.set_encoded_video_frame_observer(self._video_obs)

            reg = (getattr(lu, "register_audio_frame_observer", None)
                   or getattr(self._conn, "register_audio_frame_observer", None))
            if reg is not None:
                n = len(inspect.signature(reg).parameters)
                if n >= 3:
                    reg(self._audio_obs, 0, None)
                elif n == 2:
                    reg(self._audio_obs, 0)
                else:
                    reg(self._audio_obs)
        else:
            if hasattr(self._conn, "subscribe_all_audio"):
                try:
                    self._conn.subscribe_all_audio()
                except Exception:
                    pass
            if hasattr(self._conn, "subscribe_all_video"):
                try:
                    so = VideoSubscriptionOptions()
                    if hasattr(so, "type"):
                        so.type = VideoStreamType.VIDEO_STREAM_HIGH
                    if hasattr(so, "encoded_frame_only"):
                        so.encoded_frame_only = 1
                    elif hasattr(so, "encodedFrameOnly"):
                        so.encodedFrameOnly = 1
                    self._conn.subscribe_all_video(so)
                except Exception:
                    pass
            reg = getattr(self._conn, "register_audio_frame_observer", None)
            if reg is not None:
                n = len(inspect.signature(reg).parameters)
                if n >= 3:
                    reg(self._audio_obs, 0, None)
                elif n == 2:
                    reg(self._audio_obs, 0)
                else:
                    reg(self._audio_obs)
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
        self.diag_flags = f"lu={lu is not None},vm={v_methods}"
        self.connect_rc = self._conn.connect(self.token, self.channel_name, str(self.uid))
        self.started_at = time.time()
        self.last_frame_at = time.time()

    def close(self) -> None:
        with self._io_lock:
            if self._closed:
                return
            self._closed = True
            for fh in (self._h264_fh, self._pcm_fh):
                try:
                    fh.flush()
                    fh.close()
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
# RECORDING MANAGER
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


class RecordingManager:
    def __init__(self, bot: TelegramClient, db: Database, pool: AccountPool):
        self.bot = bot
        self.db = db
        self.pool = pool
        self.api = pool.primary
        self.busy: Dict[int, str] = {}
        self.active: Dict[str, ActiveRecording] = {}
        self._fail_cooldown: Dict[str, float] = {}
        self.manually_stopped_ls: Dict[str, str] = {}
        self._upload_sem = asyncio.Semaphore(MAX_PARALLEL_UPLOADS)
        self._start_lock = asyncio.Lock()
        self.on_change: Any = None

    def _changed(self) -> None:
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                log.debug("on_change failed", exc_info=True)

    def is_recording(self, creator_id: str) -> bool:
        return str(creator_id) in self.active

    def accounts(self) -> List[SuperLiveClient]:
        return self.pool.usable() or [self.pool.primary]

    def free_account(self) -> Optional[SuperLiveClient]:
        if len(self.active) >= self.max_concurrent():
            return None
        for c in self.accounts():
            if c.has_token() and c.slot not in self.busy:
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

    def notify_ttl(self) -> int:
        raw = self.db.get_setting("notify_ttl", "60")
        return int(raw) if raw.isdigit() else 60

    async def start_recording(self, info: CreatorStreamInfo, chat_id: int,
                              triggered_by: str = "watchlist") -> Tuple[bool, str]:
        cid = str(info.creator_id)
        if cid in self.active:
            return False, f"⏺ <b>{html.escape(info.display_name or cid)}</b> همین الان در حال ضبطه."
        if triggered_by == "watchlist":
            if time.time() < self._fail_cooldown.get(cid, 0.0):
                return False, "cooldown"
            if info.livestream_id and self.manually_stopped_ls.get(cid) == info.livestream_id:
                return False, "manually stopped"
        else:
            self.manually_stopped_ls.pop(cid, None)

        async with self._start_lock:
            client = self.free_account()
            if client is None:
                return False, (f"⚠️ همه {self.max_concurrent()} اکانت مشغول ضبطن. "
                               f"از ⚙️ تنظیمات › 👥 اکانت‌ها یه اکانت دیگه اضافه کن.")
            self.busy[client.slot] = cid

        try:
            try:
                fresh = await client.check_creator_and_get_stream(cid)
            except Exception as e:
                fresh = None
                log.warning("Fresh stream fetch failed (%s): %s", client.label, e)
            if not fresh or not fresh.is_live or not fresh.stream_url:
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
                if "private" in reason:
                    return False, ("🔒 این لایو الان <b>خصوصی</b> شده و سرور فقط به کسایی که "
                                   "پرداخت کردن اجازه ورود میده.")
                return False, (
                    "⚠️ لایو روشنه ولی اطلاعات اتصال (Agora) داده نشد.\n"
                    f"• Retrieve/Enter: <code>{d.get('retrieve_http', '-')}/{d.get('enter_http', '-')}</code>"
                    + (f"\n• دلیل سرور: <code>{html.escape(reason)}</code>" if reason else "")
                    + f"\n• کلیدها: <code>{html.escape(str(d.get('agora_keys', '-'))[:250])}</code>"
                    + f"\n• اکانت: <code>{html.escape(client.label)}</code>"
                )
            fresh.display_name = fresh.display_name or info.display_name
            info = fresh
            cid = str(info.creator_id or cid)
            if cid in self.active:
                return False, "⏺ همین الان در حال ضبطه."
            self.busy[client.slot] = cid

            _, free_mb, _ = get_disk_stats()
            err = ""
            if free_mb < MIN_FREE_DISK_MB:
                err = f"💽 فضای خالی دیسک کمه ({free_mb:.0f} MB)؛ ضبط جدید شروع نشد."
            elif info.stream_url.startswith("agora://") and not AGORA_AVAILABLE:
                err = "پکیج agora-python-server-sdk روی سرور نصب نیست."
            elif not FFMPEG_BIN:
                err = "FFmpeg روی سرور پیدا نشد."
            if err:
                return False, err

            cname = info.display_name or info.username or cid
            db_rec_id = self.db.create_recording_record(cid, cname, info.livestream_id)
            rec = ActiveRecording(
                creator_id=cid, creator_name=cname,
                livestream_id=info.livestream_id, stream_url=info.stream_url,
                db_rec_id=db_rec_id, chat_id=chat_id, triggered_by=triggered_by,
            )
            rec.client = client
            self.active[cid] = rec
            rec.task = asyncio.create_task(self._run_recording_lifecycle(rec))
            self._changed()
            return True, f"⏺ ضبط <b>{html.escape(cname)}</b> با {html.escape(client.label)} شروع شد."
        finally:
            # Release account if recording never registered (error path or race)
            if self.busy.get(client.slot) == cid and cid not in self.active:
                self.busy.pop(client.slot, None)

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
        rec = self.active.get(str(creator_id))
        if not rec:
            return False
        rec.split_event.set()
        return True

    async def stop_all(self) -> int:
        n = 0
        for rec in list(self.active.values()):
            rec.stop_event.set()
            n += 1
        return n

    async def _run_recording_lifecycle(self, rec: ActiveRecording) -> None:
        work_dir = RECORDINGS_DIR / f"{sanitize_filename(rec.creator_name)}_{rec.creator_id}_{int(rec.started_at)}"
        work_dir.mkdir(parents=True, exist_ok=True)
        upload_lock = asyncio.Lock()
        upload_tasks: set = set()
        upload_failures: List[str] = []
        final_status = "completed"
        notes = ""

        def enqueue(coro_factory: Any) -> None:
            rec.uploads_pending += 1

            async def _job() -> None:
                try:
                    async with upload_lock:
                        await coro_factory()
                except Exception as e:
                    upload_failures.append(str(e)[:250])
                    log.exception("Upload job failed for %s: %s", rec.creator_id, e)
                finally:
                    rec.uploads_pending -= 1
                    self._changed()

            t = asyncio.create_task(_job())
            upload_tasks.add(t)
            t.add_done_callback(upload_tasks.discard)

        try:
            restarts = 0
            while not rec.stop_event.is_set():
                parts_before = rec.part_number
                try:
                    if rec.stream_url.startswith("agora://"):
                        await self._agora_session(rec, work_dir, enqueue)
                    else:
                        await self._ffmpeg_session(rec, work_dir, enqueue)
                except Exception as e:
                    log.exception("Session error for %s: %s", rec.creator_id, e)
                if rec.stop_event.is_set():
                    break
                if rec.part_number > parts_before:
                    restarts = 0
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
            try:
                rec.status_text = "در حال ارسال پارت‌های باقیمانده..."
                self._changed()
            except Exception:
                pass

            try:
                while upload_tasks:
                    await asyncio.gather(*list(upload_tasks), return_exceptions=True)
            except Exception as e:
                log.warning("Awaiting uploads failed: %s", e)

            try:
                if rec.uploaded_parts == 0 and final_status == "completed":
                    final_status = "empty"
                    self._fail_cooldown[rec.creator_id] = time.time() + 180.0
                    notes = rec.agora_diag or "no data"
                    log.info("Empty recording for %s (%s): %s",
                             rec.creator_id, rec.triggered_by, notes)
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
                    try:
                        self.db.increment_recordings(rec.creator_id)
                    except Exception:
                        pass
                    try:
                        m = await self.bot.send_message(
                            rec.chat_id,
                            f"✅ ضبط <b>{html.escape(rec.creator_name)}</b> تموم شد · "
                            f"{rec.uploaded_parts} پارت · {fmt_duration(rec.uploaded_duration)} · "
                            f"{fmt_bytes(rec.uploaded_bytes)}",
                            parse_mode="html",
                        )
                        schedule_delete(m, self.notify_ttl())
                    except Exception:
                        pass
            except Exception as e:
                log.warning("Post-lifecycle notification failed: %s", e)

            try:
                if upload_failures:
                    final_status = "upload_failed" if rec.uploaded_parts == 0 else "partial_upload"
                    notes = (notes + " | " if notes else "") + \
                        "Upload failed; local files retained: " + "; ".join(upload_failures[:3])
                    log.error("Retaining local files after upload failure: %s", work_dir)
            except Exception:
                pass

            try:
                self.db.finish_recording_record(
                    rec.db_rec_id, status=final_status,
                    duration_sec=rec.uploaded_duration, file_size_bytes=rec.uploaded_bytes,
                    tg_chat_id=rec.chat_id, notes=notes,
                )
            except Exception:
                log.exception("Failed to finalize recording record")

            try:
                self.db.update_watch_status(rec.creator_id, "offline", display_name=rec.creator_name)
            except Exception:
                pass

            try:
                self.active.pop(rec.creator_id, None)
            except Exception:
                pass
            try:
                if rec.client is not None and self.busy.get(rec.client.slot) == rec.creator_id:
                    self.busy.pop(rec.client.slot, None)
            except Exception:
                pass
            try:
                if not upload_failures:
                    shutil.rmtree(work_dir, ignore_errors=True)
            except Exception:
                pass
            self._changed()

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

    async def _ffmpeg_session(self, rec: ActiveRecording, work_dir: Path, enqueue: Any) -> None:
        while True:
            part_no = rec.part_number
            out = work_dir / f"{sanitize_filename(rec.creator_name)}_part{part_no}.mp4"
            cmd = [FFMPEG_BIN or "ffmpeg", "-y", "-loglevel", "error"]
            if rec.stream_url.lower().startswith(("http://", "https://")):
                cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
            cmd += ["-i", rec.stream_url, "-c", "copy",
                    "-movflags", "+frag_keyframe+empty_moov+default_base_moof", str(out)]
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
                        try:
                            proc.terminate()
                            await asyncio.wait_for(proc.wait(), timeout=5.0)
                        except Exception:
                            proc.kill()
            if out.exists() and out.stat().st_size > 10_000:
                enqueue(lambda p=out, n=part_no: self._upload_job(rec, p, n))
                rec.part_number += 1
            if split and not rec.stop_event.is_set():
                continue
            return

    async def _agora_session(self, rec: ActiveRecording, work_dir: Path, enqueue: Any) -> None:
        def seg_paths(n: int) -> Tuple[Path, Path]:
            return work_dir / f"raw_p{n}.h264", work_dir / f"raw_p{n}.pcm"

        h264_file, pcm_file = seg_paths(rec.part_number)
        receiver = AgoraStreamReceiver(rec.stream_url, h264_file, pcm_file)

        attempts: List[Tuple[str, str]] = [(receiver.token, u) for u in receiver.uid_candidates]
        if receiver.alt_token:
            attempts.extend((receiver.alt_token, u) for u in receiver.uid_candidates)

        connected_ok = False
        last_attempt = False
        for idx, (tok, uid) in enumerate(attempts):
            if rec.stop_event.is_set():
                break
            receiver.token = tok
            receiver.uid = uid
            receiver.conn_events.clear()
            try:
                await asyncio.to_thread(receiver.connect)
            except Exception as e:
                log.warning("Agora connect error (attempt %d): %s", idx, e)
                continue
            for _ in range(9):
                await asyncio.sleep(0.5)
                if "connected" in receiver.conn_events or receiver.video_frames > 0 or receiver.audio_frames > 0:
                    connected_ok = True
                    break
                if any(ev.startswith("fail(") or ev.startswith("disconn(") for ev in receiver.conn_events):
                    break
            last_attempt = idx == len(attempts) - 1
            if connected_ok or last_attempt:
                break
            # Clean disconnect between attempts
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
        session_start = time.time()
        receiver.event_times.clear()
        try:
            while not rec.stop_event.is_set():
                await asyncio.sleep(1.0)
                if connected_ok and receiver.event_times:
                    log.warning("Agora drop for %s: %s", rec.creator_id, receiver.conn_events[-3:])
                    rec.status_text = "اتصال قطع شد، اتصال مجدد..."
                    break
                if not connected_ok:
                    log.warning("Agora never connected for %s: %s",
                                rec.creator_id, receiver.conn_events[-4:])
                    rec.status_text = "اتصال برقرار نشد"
                    break
                if receiver.video_frames == 0 and time.time() - session_start >= 25:
                    log.warning("No video for %s after 25s: %s",
                                rec.creator_id, receiver.conn_events[-4:])
                    rec.status_text = "تصویری دریافت نشد"
                    break
                ref = receiver.last_video_at or session_start
                if connected_ok and time.time() - ref >= VIDEO_STALL_TIMEOUT:
                    log.warning("Video stalled for %s (%.0fs), reconnecting",
                                rec.creator_id, time.time() - ref)
                    rec.status_text = "تصویر قطع شد، اتصال مجدد..."
                    break
                rec.bytes_written = receiver.seg_bytes
                if receiver.seg_video_frames == 1:
                    rec.seg_started_at = receiver.seg_started_at
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
            try:
                await asyncio.to_thread(receiver.close)
            except Exception:
                pass

        rec.agora_diag = (
            f"v={receiver.video_frames}, a={receiver.audio_frames}, rc={receiver.connect_rc}, "
            f"ev={','.join(receiver.conn_events[:6])}, uid={receiver.uid}, "
            f"cb_err={receiver.cb_error or '-'}"
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

    async def _mux_and_upload(self, rec: ActiveRecording, seg: Dict[str, Any],
                              work_dir: Path, part_no: int) -> None:
        h264: Path = seg["h264"]
        pcm: Path = seg["pcm"]
        out = work_dir / f"{sanitize_filename(rec.creator_name)}_part{part_no}.mp4"
        fps = seg["video_frames"] / max(1.0, seg["elapsed"])
        rec.upload_status = f"پارت {part_no}: آماده‌سازی فایل..."
        cmd = build_mux_command(FFMPEG_BIN or "ffmpeg", h264, pcm, out, fps=fps,
                                sample_rate=seg["sample_rate"], channels=seg["channels"])
        mux_ok = False
        if cmd:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
                _, err = await proc.communicate()
                if proc.returncode == 0 and out.exists() and out.stat().st_size > 10_000:
                    mux_ok = True
                else:
                    log.error("Mux error for %s part %d: %s",
                              rec.creator_id, part_no,
                              (err or b"").decode("utf-8", "ignore")[-400:])
            except Exception as e:
                log.error("Mux exception for %s part %d: %s", rec.creator_id, part_no, e)

        if mux_ok:
            # Only delete raw files AFTER successful mux
            h264.unlink(missing_ok=True)
            pcm.unlink(missing_ok=True)
            await self._upload_job(rec, out, part_no)
        else:
            # Keep raw files for recovery
            log.error("Retaining raw H264/PCM for %s part %d (mux failed)", rec.creator_id, part_no)
            rec.upload_status = f"پارت {part_no}: mux ناموفق (فایل خام نگه داشته شد)"

    async def _upload_job(self, rec: ActiveRecording, mp4_path: Path, part_no: int) -> None:
        async with self._upload_sem:
            dur, width, height = await asyncio.to_thread(probe_video_metadata, mp4_path)
            fsize = mp4_path.stat().st_size
            thumb_path = mp4_path.with_suffix(".thumb.jpg")
            sheet_path = mp4_path.with_suffix(".sheet.jpg")
            thumb_file, sheet_file = await asyncio.to_thread(
                generate_thumbnail_and_contact_sheet, mp4_path, thumb_path, sheet_path, dur, True)
            if not sheet_file and thumb_file:
                sheet_file = thumb_file

            caption = (
                f"🎬 <b>{html.escape(rec.creator_name)}</b>  ·  پارت {part_no}\n"
                f"⏱ <code>{fmt_duration(dur)}</code>   💾 <code>{fmt_bytes(fsize)}</code>   "
                f"📐 <code>{width}×{height}</code>\n"
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
                        caption=f"🖼 پیش‌نمایش · <b>{html.escape(rec.creator_name)}</b> · پارت {part_no}",
                        parse_mode="html")
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
                        reply_to=sheet_msg.id if sheet_msg else None)
                    break
                except FloodWaitError as fw:
                    await asyncio.sleep(int(getattr(fw, "seconds", 10)) + 2)
                except Exception as e:
                    log.warning("Upload error %s part %d (try %d): %s",
                                rec.creator_id, part_no, attempt + 1, e)
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
                        parse_mode="html")
                except Exception:
                    pass
                raise RuntimeError(f"Telegram upload failed for part {part_no}; retained at {dest}")

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
# WATCHLIST MONITOR
# ==============================================================================

class WatchlistMonitor:
    def __init__(self, db: Database, api: SuperLiveClient, rec_mgr: RecordingManager,
                 bot: TelegramClient):
        self.db = db
        self.api = api
        self.rec_mgr = rec_mgr
        self.bot = bot
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._notified_ls: Dict[str, str] = {}

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except Exception:
                pass

    async def _announce_live(self, owner_id: int, info: CreatorStreamInfo, started: bool) -> None:
        key = info.livestream_id or "live"
        if self._notified_ls.get(info.creator_id) == key:
            return
        if not started and not info.stream_url:
            # Probe joinability with a reserved free account (avoids disrupting active recordings)
            probe = None
            async with self.rec_mgr._start_lock:
                probe = self.rec_mgr.free_account()
                if probe is not None:
                    self.rec_mgr.busy[probe.slot] = "__probe__"
            if probe is not None:
                try:
                    full = await probe.check_creator_and_get_stream(info.creator_id)
                    if not full.stream_url:
                        return
                except Exception:
                    return
                finally:
                    if self.rec_mgr.busy.get(probe.slot) == "__probe__":
                        self.rec_mgr.busy.pop(probe.slot, None)
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
                parse_mode="html", buttons=[row])
            ttl = self.rec_mgr.notify_ttl()
            if ttl > 0:
                schedule_delete(m, ttl)
        except Exception as e:
            log.warning("Live notice failed: %s", e)

    async def _loop(self) -> None:
        log.info("Watchlist monitor started.")
        while not self._stop.is_set():
            try:
                interval = int(self.db.get_setting("poll_interval", str(DEFAULT_POLL_INTERVAL))
                               or DEFAULT_POLL_INTERVAL)
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
                        info = await self.api.check_creator_and_get_stream(
                            cid, discover_map=discover_map, light=True)
                        if info.creator_id and info.creator_id != cid and info.creator_id.isdigit():
                            log.info("Watchlist id %s -> real user_id %s", cid, info.creator_id)
                            self.db.migrate_watch(cid, info.creator_id)
                            if self.rec_mgr.is_recording(info.creator_id):
                                continue
                            cid = info.creator_id
                        if info.is_live:
                            self.db.update_watch_status(cid, "live",
                                                        display_name=info.display_name,
                                                        username=info.username)
                            started = False
                            if item["auto_record"] and self.rec_mgr.has_capacity():
                                ok, msg = await self.rec_mgr.start_recording(
                                    info, chat_id=owner_id, triggered_by="watchlist")
                                started = ok
                                if not ok:
                                    log.debug("Skip auto-record %s: %s", cid, msg)
                            await self._announce_live(owner_id, info, started)
                        else:
                            self._notified_ls.pop(cid, None)
                            self.rec_mgr.manually_stopped_ls.pop(cid, None)
                            self.db.update_watch_status(cid, "offline",
                                                        display_name=info.display_name,
                                                        username=info.username)
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
# BOT UI
# ==============================================================================

KB_WATCH = "📋 واچ‌لیست"
KB_ACTIVE = "⏺ در حال ضبط"
KB_ADD = "➕ افزودن استریمر"
KB_REC = "🎬 ضبط فوری"
KB_ARCHIVE = "📚 آرشیو"
KB_SETTINGS = "⚙️ تنظیمات"
KB_HOME = "🏠 داشبورد"
KB_ALL = {KB_WATCH, KB_ACTIVE, KB_ADD, KB_REC, KB_ARCHIVE, KB_SETTINGS, KB_HOME}
SEP = "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄"


def main_keyboard() -> Any:
    from telethon.tl.types import KeyboardButton, KeyboardButtonRow, ReplyKeyboardMarkup
    rows = [[KB_WATCH, KB_ACTIVE], [KB_ADD, KB_REC],
            [KB_ARCHIVE, KB_SETTINGS], [KB_HOME]]
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
        self._panel: Dict[int, Any] = {}
        self._panel_view: Dict[int, Tuple[str, tuple]] = {}
        self._panel_last: Dict[int, str] = {}

    def _is_owner(self, user_id: int) -> bool:
        owner = self.db.get_owner_id()
        if owner == 0:
            self.db.set_owner_id(user_id)
            return True
        return user_id == owner

    def _status_icon(self, cid: str, last_status: str) -> str:
        if self.rec_mgr.is_recording(cid):
            return "⏺"
        return "🔴" if last_status == "live" else "⚫"

    # --------------------------------------------------------------- views
    def render(self, view: str, *args: Any) -> Tuple[str, List[List[Any]]]:
        return getattr(self, f"_v_{view}")(*args)

    def _v_home(self) -> Tuple[str, List[List[Any]]]:
        wl = self.db.list_watchlist()
        live_cnt = sum(1 for x in wl if x["last_status"] == "live")
        active = list(self.rec_mgr.active.values())
        cnt, dur, size = self.db.clip_stats()
        _, free_mb, total_mb = get_disk_stats()
        ram_used, ram_total = get_ram_usage_mb()
        ram = f"{ram_used/1024:.1f}/{ram_total/1024:.1f} GB" if ram_total else f"{ram_used:.0f} MB"
        lines = [
            "🎥 <b>SuperLive Recorder</b>",
            SEP,
            f"⏺ در حال ضبط: <b>{len(active)}</b>",
            f"👀 واچ‌لیست: <b>{len(wl)}</b>   ·   🔴 آنلاین: <b>{live_cnt}</b>",
            f"📚 آرشیو: <b>{cnt}</b> ویدیو   ·   <b>{fmt_duration(dur)}</b>   ·   <b>{fmt_bytes(size)}</b>",
            SEP,
            f"💽 دیسک آزاد: <code>{free_mb/1024:.1f}/{total_mb/1024:.1f} GB</code>   "
            f"🧠 رم: <code>{ram}</code>",
            f"⚙️ Agora {'✅' if AGORA_AVAILABLE else '❌'}   ·   "
            f"FFmpeg {'✅' if FFMPEG_BIN else '❌'}",
        ]
        if active:
            lines.append(SEP)
            for r in active[:6]:
                lines.append(f"⏺ <b>{html.escape(short(r.creator_name, 22))}</b> · "
                             f"<code>{fmt_duration(time.time() - r.started_at)}</code>")
        lines.append("\n<i>آیدی، لینک پروفایل یا لینک لایو رو هر وقت خواستی همینجا بفرست.</i>")
        buttons = [
            [Button.inline(f"📋 واچ‌لیست ({len(wl)})", data="w:0"),
             Button.inline(f"⏺ ضبط‌ها ({len(active)})", data="a")],
            [Button.inline("📚 آرشیو", data="ar:0"),
             Button.inline("⚙️ تنظیمات", data="s")],
            [Button.inline("🔄 بروزرسانی", data="h")],
        ]
        return "\n".join(lines), buttons

    def _v_watchlist(self, page: int = 0) -> Tuple[str, List[List[Any]]]:
        items = self.db.list_watchlist()
        items.sort(key=lambda x: (0 if self.rec_mgr.is_recording(x["creator_id"])
                                   else (1 if x["last_status"] == "live" else 2)))
        per = 8
        pages = max(1, (len(items) + per - 1) // per)
        page = max(0, min(page, pages - 1))
        chunk = items[page * per:(page + 1) * per]
        rec_n = sum(1 for x in items if self.rec_mgr.is_recording(x["creator_id"]))
        live_n = sum(1 for x in items if x["last_status"] == "live")
        lines = [
            f"📋 <b>واچ‌لیست</b>  ·  {len(items)} نفر",
            f"⏺ {rec_n} در حال ضبط   🔴 {live_n} آنلاین   ⚫ {len(items)-live_n} آفلاین",
            SEP,
        ]
        if not chunk:
            lines.append("هنوز کسی اضافه نشده. از «➕ افزودن استریمر» استفاده کن.")
        else:
            lines.append("<i>روی هر نفر بزن تا جزئیات و ضبط‌هاش رو ببینی.</i>")
        buttons: List[List[Any]] = []
        for it in chunk:
            cid = it["creator_id"]
            icon = self._status_icon(cid, it["last_status"])
            extra = ""
            r = self.rec_mgr.active.get(cid)
            if r:
                extra = f" · {fmt_duration(time.time() - r.started_at)}"
            auto = "" if it["auto_record"] else " ⏸"
            buttons.append([Button.inline(
                f"{icon} {short(it['display_name'] or cid)}{extra}{auto}",
                data=f"p:{cid}:0")])
        nav: List[Any] = []
        if page > 0:
            nav.append(Button.inline("‹ قبلی", data=f"w:{page-1}"))
        if pages > 1:
            nav.append(Button.inline(f"{page+1}/{pages}", data=f"w:{page}"))
        if page < pages - 1:
            nav.append(Button.inline("بعدی ›", data=f"w:{page+1}"))
        if nav:
            buttons.append(nav)
        buttons.append([Button.inline("➕ افزودن", data="add"),
                        Button.inline("🔄", data=f"w:{page}"),
                        Button.inline("🏠", data="h")])
        return "\n".join(lines), buttons

    def _v_active(self) -> Tuple[str, List[List[Any]]]:
        active = list(self.rec_mgr.active.values())
        lines = [f"⏺ <b>در حال ضبط</b> · {len(active)}", SEP]
        buttons: List[List[Any]] = []
        if not active:
            lines.append("هیچ ضبطی فعال نیست.")
        for r in active:
            dur = fmt_duration(time.time() - r.started_at)
            lines.append(
                f"⏺ <b>{html.escape(short(r.creator_name, 22))}</b>\n"
                f"   ⏱ {dur} · 💾 {fmt_bytes(r.bytes_written)} · پارت {r.part_number}\n"
                f"   {html.escape(r.status_text)}"
                + (f"\n   📤 {html.escape(r.upload_status)}" if r.upload_status else "")
            )
            buttons.append([Button.inline(f"👤 {short(r.creator_name, 18)} · {dur}",
                                          data=f"p:{r.creator_id}:0")])
        buttons.append([Button.inline("🛑 توقف همه", data="sa"),
                        Button.inline("🔄", data="a"),
                        Button.inline("🏠", data="h")])
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
            f"🆔 <code>{cid}</code>"
            + (f"   @{html.escape(w['username'])}" if w.get("username") else ""),
            SEP,
            f"📡 وضعیت: {st}",
        ]
        if w:
            lines.append(f"🤖 ضبط خودکار: {'✅ روشن' if w.get('auto_record') else '⏸ خاموش'}")
            if w.get("last_seen_live"):
                lines.append(f"🕘 آخرین لایو: <code>{iso_to_local(w['last_seen_live'])}</code>")
        else:
            lines.append("<i>این استریمر توی واچ‌لیست نیست.</i>")
        if r:
            lines += [
                SEP,
                f"⏱ کل ضبط: <code>{fmt_duration(time.time() - r.started_at)}</code>",
                f"📦 پارت فعلی: <code>{fmt_duration(time.time() - r.seg_started_at)}</code>",
                f"💾 حجم پارت: <code>{fmt_bytes(r.bytes_written)}</code>",
                f"📤 پارت‌های ارسالی: {r.uploaded_parts}",
            ]
            if r.upload_status:
                lines.append(f"📶 {html.escape(r.upload_status)}")
        if cnt:
            lines += [SEP, f"📚 آرشیو: {cnt} ویدیو · {fmt_duration(dur)} · {fmt_bytes(size)}"]
        buttons: List[List[Any]] = []
        if r:
            buttons.append([
                Button.inline("⏹ توقف", data=f"st:{cid}"),
                Button.inline("✂️ تقسیم الان", data=f"sp:{cid}"),
            ])
        else:
            buttons.append([Button.inline("🎬 ضبط فوری", data=f"qr:{cid}")])
        if w:
            buttons.append([
                Button.inline("🤖 خاموش کردن ضبط خودکار" if w.get("auto_record")
                              else "✅ روشن کردن ضبط خودکار", data=f"ar2:{cid}"),
                Button.inline("🗑 حذف", data=f"rm:{cid}"),
            ])
        buttons.append([
            Button.inline("🔄 بروزرسانی", data=f"p:{cid}:{page}"),
            Button.inline("📋 واچ‌لیست", data="w:0"),
            Button.inline("🏠", data="h"),
        ])
        return "\n".join(lines), buttons

    def _v_archive(self, page: int = 0) -> Tuple[str, List[List[Any]]]:
        per = 8
        total = self.db.count_recordings()
        recs = self.db.list_recent_recordings(limit=per, offset=page * per)
        cnt, dur, size = self.db.clip_stats()
        lines = [
            f"📚 <b>آرشیو</b>",
            f"{cnt} کلیپ · {fmt_duration(dur)} · {fmt_bytes(size)}",
            SEP,
        ]
        if not recs:
            lines.append("هنوز ضبطی آرشیو نشده.")
        buttons: List[List[Any]] = []
        icons = {"completed": "✅", "interrupted": "⚠️", "empty": "🕳",
                 "upload_failed": "❌", "partial_upload": "⚠️",
                 "recording": "⏺", "error": "❌"}
        for r in recs:
            icon = icons.get(r["status"], "•")
            lines.append(
                f"{icon} {html.escape(short(r['creator_name'] or r['creator_id'], 22))} · "
                f"{fmt_duration(r['duration_sec'])} · {fmt_bytes(r['file_size_bytes'])}"
            )
            buttons.append([Button.inline(
                f"{icon} {short(r['creator_name'] or r['creator_id'], 18)} · "
                f"{fmt_duration(r['duration_sec'])}",
                data=f"p:{r['creator_id']}:0")])
        nav: List[Any] = []
        if page > 0:
            nav.append(Button.inline("‹", data=f"ar:{page-1}"))
        if total > (page + 1) * per:
            nav.append(Button.inline("›", data=f"ar:{page+1}"))
        if nav:
            buttons.append(nav)
        buttons.append([Button.inline("🔄", data=f"ar:{page}"), Button.inline("🏠", data="h")])
        return "\n".join(lines), buttons

    def _v_settings(self) -> Tuple[str, List[List[Any]]]:
        split_m = int(self.db.get_setting("split_minutes", "0") or 0)
        part_mb = self.rec_mgr.part_cap_bytes() // (1024 * 1024)
        poll = self.db.get_setting("poll_interval", str(DEFAULT_POLL_INTERVAL))
        max_c = self.rec_mgr.max_concurrent()
        nttl = self.db.get_setting("notify_ttl", "60")
        arch = self.db.get_setting("archive_chat_id", "")
        lines = [
            "⚙️ <b>تنظیمات</b>",
            SEP,
            f"⏱ تقسیم خودکار: {split_m} دقیقه" if split_m > 0 else "⏱ تقسیم خودکار: خاموش",
            f"💾 سقف هر پارت: {part_mb} MB",
            f"🔄 بازه چک واچ‌لیست: {poll} ثانیه",
            f"👥 حداکثر ضبط همزمان: {max_c}",
            f"🕘 خودحذفی اعلان‌ها: {nttl} ثانیه",
            f"📦 کانال آرشیو: {arch or 'تنظیم نشده'}",
            f"👥 اکانت‌ها: {len(self.pool.clients)} (فعال: {len(self.pool.usable())})",
            SEP,
            "روی هر گزینه بزن تا تغییر کنی.",
        ]
        buttons = [
            [Button.inline("⏱ تقسیم خودکار", data="sett:split_minutes"),
             Button.inline("💾 سقف پارت", data="sett:part_mb")],
            [Button.inline("🔄 بازه چک", data="sett:poll_interval"),
             Button.inline("🕘 خودحذفی", data="sett:notify_ttl")],
            [Button.inline("📦 کانال آرشیو", data="sett:archive_chat_id"),
             Button.inline("👥 اکانت‌ها", data="acc")],
            [Button.inline("🔄 بروزرسانی", data="s"), Button.inline("🏠", data="h")],
        ]
        return "\n".join(lines), buttons

    def _v_accounts(self) -> Tuple[str, List[List[Any]]]:
        lines = ["👥 <b>اکانت‌های SuperLive</b>", SEP]
        buttons: List[List[Any]] = []
        for slot, c in sorted(self.pool.clients.items()):
            status = "🟢 آماده" if c.has_token() else "⚪ بدون توکن"
            busy = "⏺ مشغول" if slot in self.rec_mgr.busy else "💤 آزاد"
            lines.append(f"<b>{slot}</b>. {html.escape(c.label)} · {status} · {busy}")
            if slot > 0:
                buttons.append([Button.inline(f"🗑 حذف اکانت {slot}", data=f"accrm:{slot}")])
        buttons.append([Button.inline("➕ افزودن اکانت", data="accadd"),
                        Button.inline("🔄", data="acc"),
                        Button.inline("🏠", data="h")])
        return "\n".join(lines), buttons

    # --------------------------------------------------------------- panel
    async def _refresh_panel(self, chat_id: int) -> None:
        vi = self._panel_view.get(chat_id)
        msg = self._panel.get(chat_id)
        if not vi or msg is None:
            return
        view, args = vi
        try:
            text, buttons = self.render(view, *args)
        except Exception as e:
            log.warning("Render failed for %s/%s: %s", chat_id, view, e)
            return
        if text == self._panel_last.get(chat_id):
            return
        try:
            await msg.edit(text, buttons=buttons, parse_mode="html")
            self._panel_last[chat_id] = text
        except MessageNotModifiedError:
            self._panel_last[chat_id] = text
        except Exception as e:
            log.debug("Panel edit failed: %s", e)
            try:
                await msg.delete()
            except Exception:
                pass
            self._panel.pop(chat_id, None)

    async def _refresh_all_panels(self) -> None:
        for chat_id in list(self._panel.keys()):
            try:
                await self._refresh_panel(chat_id)
            except Exception:
                pass

    def _request_panel_refresh(self) -> None:
        try:
            bg_task(self._refresh_all_panels())
        except RuntimeError:
            pass

    async def _show_panel(self, event: Any, view: str, *args: Any) -> None:
        chat_id = getattr(event, "chat_id", None) or event
        try:
            text, buttons = self.render(view, *args)
        except Exception as e:
            log.exception("Render failed: %s", e)
            return
        self._panel_view[chat_id] = (view, args)
        self._panel_last[chat_id] = text
        msg = self._panel.get(chat_id)
        if msg is not None:
            try:
                await msg.edit(text, buttons=buttons, parse_mode="html")
                return
            except MessageNotModifiedError:
                return
            except Exception:
                pass
        try:
            msg = await self.bot.send_message(chat_id, text, buttons=buttons, parse_mode="html")
            self._panel[chat_id] = msg
        except Exception as e:
            log.warning("send_panel failed: %s", e)

    # --------------------------------------------------------------- handlers
    def _register_handlers(self) -> None:
        bot = self.bot

        @bot.on(events.NewMessage(pattern=r"^/start"))
        async def cmd_start(ev):
            if not self._is_owner(ev.sender_id):
                return
            await ev.respond(
                "👋 به <b>SuperLive Recorder</b> خوش اومدی!\n"
                "از منوی پایین یا دکمه‌های زیر استفاده کن.",
                parse_mode="html", buttons=main_keyboard())
            await self._show_panel(ev, "home")

        @bot.on(events.NewMessage(pattern=r"^/debug"))
        async def cmd_debug(ev):
            if not self._is_owner(ev.sender_id):
                return
            parts = (ev.message.text or "").split(maxsplit=1)
            if len(parts) < 2:
                await ev.respond("Usage: <code>/debug &lt;id-or-url&gt;</code>", parse_mode="html")
                return
            target = parts[1].strip()
            await ev.respond("🔎 در حال دیباگ...")
            try:
                cid, _, _, _ = await self.api.resolve_creator(target)
                info = await self.api.check_creator_and_get_stream(cid)
                dbg = json.dumps(info.raw_debug, ensure_ascii=False, indent=2)
                msg = (f"<b>Debug</b> for <code>{html.escape(cid)}</code>\n"
                       f"live={info.is_live} ls={info.livestream_id}\n"
                       f"url={(info.stream_url or '-')[:80]}\n"
                       f"<pre>{html.escape(dbg[:3000])}</pre>")
                await ev.respond(msg, parse_mode="html")
            except Exception as e:
                await ev.respond(f"❌ {e}")

        @bot.on(events.CallbackQuery)
        async def on_cb(ev):
            if not self._is_owner(ev.sender_id):
                await ev.answer("⛔️", alert=False)
                return
            try:
                data = ev.data.decode("utf-8", "ignore") if isinstance(ev.data, bytes) else str(ev.data or "")
            except Exception:
                data = ""
            try:
                await self._handle_cb(ev, data)
            except Exception:
                log.exception("Callback failed: %s", data)
            try:
                await ev.answer()
            except Exception:
                pass

        @bot.on(events.NewMessage)
        async def on_msg(ev):
            if not self._is_owner(ev.sender_id) or ev.out:
                return
            text = (ev.message.text or "").strip()
            if not text or text.startswith("/"):
                return
            state = self._awaiting_input.get(ev.chat_id)
            if state:
                await self._handle_input(ev, state, text)
                return
            if text == KB_HOME:
                await self._show_panel(ev, "home")
            elif text == KB_WATCH:
                await self._show_panel(ev, "watchlist", 0)
            elif text == KB_ACTIVE:
                await self._show_panel(ev, "active")
            elif text == KB_ARCHIVE:
                await self._show_panel(ev, "archive", 0)
            elif text == KB_SETTINGS:
                await self._show_panel(ev, "settings")
            elif text == KB_ADD:
                self._awaiting_input[ev.chat_id] = "add"
                await ev.respond("🆔 آیدی، لینک پروفایل یا لینک لایو SuperLive رو بفرست:")
            elif text == KB_REC:
                self._awaiting_input[ev.chat_id] = "qr"
                await ev.respond("🎬 آیدی یا لینک لایو برای ضبط فوری رو بفرست:")

    async def _handle_cb(self, ev, data: str) -> None:
        if data == "h":
            await self._show_panel(ev, "home")
        elif data.startswith("w:"):
            try:
                await self._show_panel(ev, "watchlist", int(data[2:]))
            except ValueError:
                await self._show_panel(ev, "watchlist", 0)
        elif data == "a":
            await self._show_panel(ev, "active")
        elif data.startswith("ar:"):
            try:
                await self._show_panel(ev, "archive", int(data[3:]))
            except ValueError:
                await self._show_panel(ev, "archive", 0)
        elif data == "s":
            await self._show_panel(ev, "settings")
        elif data == "acc":
            await self._show_panel(ev, "accounts")
        elif data.startswith("p:"):
            parts = data[2:].split(":")
            cid = parts[0]
            page = 0
            if len(parts) > 1 and parts[1].isdigit():
                page = int(parts[1])
            await self._show_panel(ev, "person", cid, page)
        elif data.startswith("qr:"):
            await self._do_quick_record(ev, data[3:])
        elif data.startswith("st:"):
            cid = data[3:]
            if await self.rec_mgr.stop_recording(cid):
                await ev.respond("⏹ در حال توقف...")
            await self._show_panel(ev, "person", cid, 0)
        elif data.startswith("sp:"):
            cid = data[3:]
            if await self.rec_mgr.split_recording(cid):
                await ev.respond("✂️ پارت فعلی ارسال می‌شود، ضبط ادامه دارد.")
        elif data.startswith("rm:"):
            cid = data[3:]
            if self.db.remove_watch(cid):
                await ev.respond(f"🗑 حذف شد: <code>{html.escape(cid)}</code>", parse_mode="html")
            await self._show_panel(ev, "watchlist", 0)
        elif data.startswith("ar2:"):
            cid = data[4:]
            nv = self.db.toggle_auto_record(cid)
            if nv is not None:
                await ev.respond(f"🤖 ضبط خودکار: {'✅ روشن' if nv else '⏸ خاموش'}")
            await self._show_panel(ev, "person", cid, 0)
        elif data == "sa":
            n = await self.rec_mgr.stop_all()
            await ev.respond(f"⏹ {n} ضبط در حال توقف...")
            await self._show_panel(ev, "active")
        elif data == "add":
            self._awaiting_input[ev.chat_id] = "add"
            await ev.respond("🆔 آیدی، لینک پروفایل یا لینک لایو SuperLive رو بفرست:")
        elif data == "accadd":
            self._awaiting_input[ev.chat_id] = "accadd"
            await ev.respond("🔑 توکن SuperLive اکانت رو بفرست\n"
                             "می‌تونی بعد از توکن، device_id رو هم با فاصله بفرستی.")
        elif data.startswith("accrm:"):
            try:
                slot = int(data[6:])
            except ValueError:
                return
            if self.pool.remove(slot):
                await ev.respond(f"🗑 اکانت {slot} حذف شد.")
            await self._show_panel(ev, "accounts")
        elif data.startswith("sett:"):
            await self._prompt_setting(ev, data[5:])

    async def _do_quick_record(self, ev, cid: str) -> None:
        if self.rec_mgr.is_recording(cid):
            await ev.respond("⏺ همین الان در حال ضبطه.")
            return
        if not self.rec_mgr.has_capacity():
            await ev.respond("⚠️ ظرفیت ضبط پر است.")
            return
        await ev.respond("🔎 در حال بررسی و شروع ضبط...")
        try:
            info = await self.api.check_creator_and_get_stream(cid)
            if not info.is_live or not info.stream_url:
                await ev.respond("⚫ لایو نیست یا استریم در دسترس نیست.")
                return
            ok, msg = await self.rec_mgr.start_recording(
                info, chat_id=ev.chat_id, triggered_by="manual")
            await ev.respond(msg if not ok else f"✅ {msg}", parse_mode="html")
            await self._show_panel(ev, "person", info.creator_id, 0)
        except Exception as e:
            await ev.respond(f"❌ خطا: {e}")

    async def _prompt_setting(self, ev, key: str) -> None:
        prompts = {
            "split_minutes": "⏱ تقسیم خودکار به دقیقه (0 = خاموش):",
            "part_mb": "💾 سقف حجم هر پارت به MB (50-1950):",
            "poll_interval": "🔄 بازه چک واچ‌لیست به ثانیه (10-300):",
            "notify_ttl": "🕘 خودحذفی اعلان‌ها به ثانیه (0 = بدون حذف):",
            "archive_chat_id": "📦 آیدی عددی کانال آرشیو (0 = خاموش):",
        }
        self._awaiting_input[ev.chat_id] = f"set:{key}"
        await ev.respond(prompts.get(key, f"مقدار {key}:"))

    async def _handle_input(self, ev, state: str, text: str) -> None:
        self._awaiting_input.pop(ev.chat_id, None)
        try:
            if state == "add":
                await ev.respond("🔎 در حال جستجو...")
                cid, uname, dname, avatar = await self.api.resolve_creator(text)
                if not cid:
                    await ev.respond("❌ پیدا نشد.")
                    return
                is_new = self.db.add_watch(cid, username=uname,
                                           display_name=dname, avatar_url=avatar)
                await ev.respond(
                    f"{'✅ اضافه شد' if is_new else '♻️ بروزرسانی شد'}: "
                    f"<b>{html.escape(dname or cid)}</b>\n🆔 <code>{cid}</code>",
                    parse_mode="html")
                await self._show_panel(ev, "person", cid, 0)
            elif state == "qr":
                await ev.respond("🎬 در حال آماده‌سازی...")
                cid, _, _, _ = await self.api.resolve_creator(text)
                if not cid:
                    await ev.respond("❌ پیدا نشد.")
                    return
                await self._do_quick_record(ev, cid)
            elif state == "accadd":
                parts = text.split()
                token = parts[0] if parts else ""
                device = parts[1] if len(parts) > 1 else ""
                if not token or len(token) < 20:
                    await ev.respond("❌ توکن نامعتبر.")
                    return
                c = self.pool.add(token, device_id=device)
                await ev.respond(f"✅ اکانت {c.slot} اضافه شد.")
                await self._show_panel(ev, "accounts")
            elif state.startswith("set:"):
                key = state[4:]
                if key in ("split_minutes", "poll_interval", "notify_ttl", "part_mb", "archive_chat_id"):
                    if not text.lstrip("-").isdigit() and key != "archive_chat_id":
                        await ev.respond("❌ عدد نامعتبر.")
                        return
                self.db.set_setting(key, text)
                await ev.respond(f"✅ ذخیره شد: <code>{key} = {html.escape(text)}</code>",
                                 parse_mode="html")
                await self._show_panel(ev, "settings")
        except Exception as e:
            log.exception("Input handling failed")
            await ev.respond(f"❌ خطا: {e}")

    # --------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        await self.bot.start(bot_token=BOT_TOKEN)
        me = await self.bot.get_me()
        log.info("Bot @%s started", me.username)
        self.db.mark_stale_recordings_interrupted()
        self._register_handlers()
        self.rec_mgr.on_change = self._request_panel_refresh
        self.monitor.start()
        owner = self.db.get_owner_id()
        if owner:
            try:
                await self.bot.send_message(
                    owner,
                    "🚀 <b>SuperLive Recorder</b> روشن شد.",
                    parse_mode="html", buttons=main_keyboard())
            except Exception:
                pass

    async def shutdown(self) -> None:
        log.info("Shutting down...")
        try:
            await self.monitor.stop()
        except Exception:
            pass
        try:
            await self.rec_mgr.stop_all()
        except Exception:
            pass
        # Give active tasks a moment
        try:
            await asyncio.sleep(2.0)
        except Exception:
            pass
        try:
            await self.pool.close()
        except Exception:
            pass
        try:
            self.db.close()
        except Exception:
            pass
        try:
            await self.bot.disconnect()
        except Exception:
            pass


async def _async_main() -> None:
    bot = SuperLiveRecBot()
    shutdown = asyncio.Event()

    def _sig(*_):
        shutdown.set()

    try:
        loop = asyncio.get_running_loop()
        for s in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(s, _sig)
            except NotImplementedError:
                pass
    except Exception:
        pass

    try:
        await bot.start()
        await shutdown.wait()
    finally:
        await bot.shutdown()


def main() -> None:
    try:
        asyncio.run(_async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
