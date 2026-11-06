#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地阅读记录（SQLite）。"""

from __future__ import annotations

import contextvars
import csv
import io
import json
import re
import sqlite3
import struct
import uuid
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Card / 日历 dates follow Asia/Shanghai (same as WeRead sync).
TZ = timezone(timedelta(hours=8))

try:
    from pypinyin import lazy_pinyin
except ImportError:  # optional; fallback keeps API usable without the package
    lazy_pinyin = None

DATA_DIR = Path.home() / ".reading-log"
DB_PATH = DATA_DIR / "reading.db"
COVERS_DIR = DATA_DIR / "covers"
COVER_FILE_RE = re.compile(r"^[A-Za-z0-9._-]+\.png$")

BAR_PRESETS = (
    "#eac1b7",
    "#f0d8b5",
    "#f9f3a0",
    "#dfeab7",
    "#d4e9cc",
    "#cce9d6",
    "#cce9e9",
    "#d3ddec",
    "#dfd8f2",
)
# Pastel bar style from design presets (#eac1b7): only H comes from the cover.
BAR_STYLE_S = 10
BAR_STYLE_B = 95
# Title ink from bar hue (mirrors static bookNameColor).
TITLE_STYLE_S = 7
TITLE_STYLE_B = 61
# H display fallback ≈ #eac1b7; S/B match BAR_STYLE / TITLE_STYLE.
# fromCover mirrors today's new-book coloring (cover H, fixed S/B).
_DEFAULT_COLOR_DEFAULTS = {
    "bar": {
        "h": {"value": 12, "fromCover": True},
        "s": {"value": BAR_STYLE_S, "fromCover": False},
        "b": {"value": BAR_STYLE_B, "fromCover": False},
    },
    "title": {
        "h": {"value": 12, "fromCover": True},
        "s": {"value": TITLE_STYLE_S, "fromCover": False},
        "b": {"value": TITLE_STYLE_B, "fromCover": False},
    },
}
DEFAULT_TITLE_COLOR = "#f7f7f5"
CAL_START = date(2026, 1, 1)
# API returns up to this many books per day; the UI clips further with barRows.
MAX_BOOKS_PER_DAY = 8


def _parse_hex_rgb(color: str | None) -> tuple[int, int, int] | None:
    raw = str(color or "").strip()
    if len(raw) == 7 and raw.startswith("#") and all(ch in "0123456789abcdefABCDEF" for ch in raw[1:]):
        return int(raw[1:3], 16), int(raw[3:5], 16), int(raw[5:7], 16)
    return None


def _hex_to_hsb(hex_color: str) -> tuple[int, int, int]:
    rgb = _parse_hex_rgb(hex_color)
    if not rgb:
        return 0, 0, 100
    r, g, b = rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0
    mx, mn = max(r, g, b), min(r, g, b)
    d = mx - mn
    h = 0.0
    if d > 0:
        if mx == r:
            h = ((g - b) / d) % 6
        elif mx == g:
            h = (b - r) / d + 2
        else:
            h = (r - g) / d + 4
        h *= 60
        if h < 0:
            h += 360
    s = 0.0 if mx == 0 else (d / mx) * 100
    return int(round(h)), int(round(s)), int(round(mx * 100))


def _hsb_to_hex(h: float, s: float, v: float) -> str:
    h = ((float(h) % 360) + 360) % 360
    s = max(0.0, min(100.0, float(s))) / 100.0
    v = max(0.0, min(100.0, float(v))) / 100.0
    c = v * s
    x = c * (1 - abs((h / 60) % 2 - 1))
    m = v - c
    if h < 60:
        r, g, b = c, x, 0.0
    elif h < 120:
        r, g, b = x, c, 0.0
    elif h < 180:
        r, g, b = 0.0, c, x
    elif h < 240:
        r, g, b = 0.0, x, c
    elif h < 300:
        r, g, b = x, 0.0, c
    else:
        r, g, b = c, 0.0, x

    def to(n: float) -> str:
        return format(int(round((n + m) * 255)), "02x")

    return "#" + to(r) + to(g) + to(b)


def _clamp_channel(name: str, value) -> int:
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        n = 0
    if name == "h":
        return n % 360
    return max(0, min(100, n))


def _normalize_color_group(raw, fallback: dict) -> dict:
    src = raw if isinstance(raw, dict) else {}
    out = {}
    for key in ("h", "s", "b"):
        fb = fallback[key]
        ch = src.get(key) if isinstance(src.get(key), dict) else {}
        out[key] = {
            "value": _clamp_channel(key, ch.get("value", fb["value"])),
            "fromCover": bool(ch["fromCover"]) if "fromCover" in ch else bool(fb["fromCover"]),
        }
    return out


def color_defaults() -> dict:
    """HSB defaults for new-book bar / title colors (settings-backed)."""
    raw = get_setting("colorDefaults", "")
    parsed = {}
    if raw:
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    return {
        "bar": _normalize_color_group(parsed.get("bar"), _DEFAULT_COLOR_DEFAULTS["bar"]),
        "title": _normalize_color_group(parsed.get("title"), _DEFAULT_COLOR_DEFAULTS["title"]),
    }


def set_color_defaults(payload) -> dict:
    groups = payload if isinstance(payload, dict) else {}
    out = {
        "bar": _normalize_color_group(groups.get("bar"), _DEFAULT_COLOR_DEFAULTS["bar"]),
        "title": _normalize_color_group(groups.get("title"), _DEFAULT_COLOR_DEFAULTS["title"]),
    }
    set_setting("colorDefaults", json.dumps(out, ensure_ascii=False))
    return out


def _parse_font_offset(raw) -> float:
    if raw is None or raw is False:
        return 0.0
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return 0.0
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if n != n:  # NaN
        return 0.0
    return max(-200.0, min(200.0, n))


def ui_font() -> dict:
    """UI font family + per-script vertical offsets (settings-backed)."""
    raw = get_setting("uiFont", "")
    parsed = {}
    if raw:
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    family = str(parsed.get("family") or "").strip()
    if len(family) > 120:
        family = family[:120]
    return {
        "family": family,
        "offsetCjk": _parse_font_offset(parsed.get("offsetCjk")),
        "offsetLatin": _parse_font_offset(parsed.get("offsetLatin")),
        "offsetDigit": _parse_font_offset(parsed.get("offsetDigit")),
    }


def set_ui_font(payload) -> dict:
    src = payload if isinstance(payload, dict) else {}
    family = str(src.get("family") or "").strip()
    if len(family) > 120:
        family = family[:120]
    out = {
        "family": family,
        "offsetCjk": _parse_font_offset(src.get("offsetCjk")),
        "offsetLatin": _parse_font_offset(src.get("offsetLatin")),
        "offsetDigit": _parse_font_offset(src.get("offsetDigit")),
    }
    set_setting("uiFont", json.dumps(out, ensure_ascii=False))
    return out


def _group_needs_cover(group: dict) -> bool:
    return any(bool((group.get(k) or {}).get("fromCover")) for k in ("h", "s", "b"))


def _resolve_hsb(group: dict, cover_hsb: tuple[int, int, int] | None, h_fallback: int | None = None) -> tuple[int, int, int]:
    """Compose H/S/B from settings; cover channels use cover_hsb when present."""
    h_cfg, s_cfg, b_cfg = group["h"], group["s"], group["b"]
    if h_cfg["fromCover"] and cover_hsb is not None:
        h = cover_hsb[0]
    elif h_cfg["fromCover"] and h_fallback is not None:
        h = int(h_fallback) % 360
    else:
        h = h_cfg["value"]
    if s_cfg["fromCover"] and cover_hsb is not None:
        s = cover_hsb[1]
    else:
        s = s_cfg["value"]
    if b_cfg["fromCover"] and cover_hsb is not None:
        b = cover_hsb[2]
    else:
        b = b_cfg["value"]
    return h % 360, max(0, min(100, s)), max(0, min(100, b))


def default_title_color(bar_color: str | None = None, cover_hsb: tuple[int, int, int] | None = None) -> str:
    """Muted title ink — H from cover/bar when configured; S/B from settings."""
    cfg = color_defaults()["title"]
    bar = bar_color if _parse_hex_rgb(bar_color) else "#eac1b7"
    bar_h, _, _ = _hex_to_hsb(bar)
    h, s, b = _resolve_hsb(cfg, cover_hsb, h_fallback=bar_h)
    return _hsb_to_hex(h, s, b)


def title_color_for(stored: str | None = None, bar_color: str | None = None) -> str:
    hex_color = (stored or "").strip()
    # Sentinel near-paper white: resolve to the same muted ink the UI paints.
    if hex_color and hex_color.lower() != DEFAULT_TITLE_COLOR.lower():
        return hex_color
    return default_title_color(bar_color)


def _hash01(seed: str) -> float:
    h = 2166136261
    for ch in str(seed):
        h = ((h ^ ord(ch)) * 16777619) & 0xFFFFFFFF
        if h >= 2**31:
            h -= 2**32
    return (abs(h) % 10000) / 9999


def default_bar_color(book_id: str) -> str:
    idx = int(_hash01(book_id) * len(BAR_PRESETS)) % len(BAR_PRESETS)
    return BAR_PRESETS[idx]


def bar_color_from_hue(h: float) -> str:
    """Muted pastel bar: cover supplies H; S/B from settings (defaults BAR_STYLE)."""
    cfg = color_defaults()["bar"]
    return _hsb_to_hex(h, cfg["s"]["value"], cfg["b"]["value"])


def dominant_hsb_from_image_bytes(data: bytes) -> tuple[int, int, int] | None:
    """Weighted H/S/B of non-paper / non-gray cover pixels (circular mean hue)."""
    if not data:
        return None
    try:
        from math import atan2, cos, degrees, radians, sin
        from io import BytesIO

        from PIL import Image
    except Exception:
        return None
    try:
        img = Image.open(BytesIO(data))
        img = img.convert("RGBA")
        img.thumbnail((64, 64), Image.Resampling.BOX)
        pixels = list(img.getdata())
    except Exception:
        return None

    sum_sin = 0.0
    sum_cos = 0.0
    sum_sat = 0.0
    sum_bri = 0.0
    weight_sum = 0.0
    for r, g, b, a in pixels:
        if a < 200:
            continue
        # Near-paper / white / washed backgrounds
        if r >= 245 and g >= 245 and b >= 240:
            continue
        mx = max(r, g, b)
        mn = min(r, g, b)
        if mx < 28:  # near black
            continue
        chroma = mx - mn
        if chroma < 18:  # near gray
            continue
        rf, gf, bf = r / 255.0, g / 255.0, b / 255.0
        mx_f, mn_f = max(rf, gf, bf), min(rf, gf, bf)
        d = mx_f - mn_f
        if d <= 0:
            continue
        if mx_f == rf:
            h = ((gf - bf) / d) % 6
        elif mx_f == gf:
            h = (bf - rf) / d + 2
        else:
            h = (rf - gf) / d + 4
        h *= 60
        if h < 0:
            h += 360
        sat = d / mx_f
        if sat < 0.12:
            continue
        # Prefer vivid midtones over washed pastels on the cover itself
        weight = sat * (0.35 + 0.65 * mx_f)
        rad = radians(h)
        sum_sin += sin(rad) * weight
        sum_cos += cos(rad) * weight
        sum_sat += sat * 100.0 * weight
        sum_bri += mx_f * 100.0 * weight
        weight_sum += weight

    if weight_sum <= 0:
        return None
    hue = int(round(degrees(atan2(sum_sin, sum_cos)))) % 360
    sat_out = int(round(sum_sat / weight_sum))
    bri_out = int(round(sum_bri / weight_sum))
    return hue, max(0, min(100, sat_out)), max(0, min(100, bri_out))


def dominant_hue_from_image_bytes(data: bytes) -> int | None:
    """Average hue of non-paper / non-gray cover pixels (circular mean, sat-weighted)."""
    hsb = dominant_hsb_from_image_bytes(data)
    return None if hsb is None else hsb[0]


def _fetch_cover_image_bytes(url: str) -> bytes | None:
    text = str(url or "").strip()
    if not text.startswith(("http://", "https://")):
        return None
    try:
        from urllib.parse import urlparse
        from urllib.request import Request, urlopen

        host = (urlparse(text).hostname or "").lower()
        if any(
            host == suffix or host.endswith("." + suffix)
            for suffix in ("zhangyue01.com", "zhangyuecdn.com", "zhangyue.com", "ireader.com")
        ):
            referer = "https://pc.ireader.com/"
        else:
            referer = "https://weread.qq.com/"
        req = Request(
            text,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": referer,
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            },
        )
        with urlopen(req, timeout=8) as resp:
            body = resp.read()
        return body if body else None
    except Exception:
        return None


def _cover_bytes_for_url(cover_url: str | None) -> bytes | None:
    url = str(cover_url or "").strip()
    if not url:
        return None
    try:
        import weread

        url = weread.upgrade_cover_url(url) or url
    except Exception:
        pass
    return _fetch_cover_image_bytes(url)


def dominant_hsb_from_cover_url(cover_url: str | None) -> tuple[int, int, int] | None:
    """Fetch cover and return dominant H/S/B, or None if unavailable."""
    data = _cover_bytes_for_url(cover_url)
    if not data:
        return None
    return dominant_hsb_from_image_bytes(data)


def dominant_hue_from_cover_url(cover_url: str | None) -> int | None:
    """Fetch cover and return dominant hue, or None if unavailable."""
    hsb = dominant_hsb_from_cover_url(cover_url)
    return None if hsb is None else hsb[0]


def new_book_colors(book_id: str, cover_url: str | None = None) -> tuple[str, str]:
    """Compose bar + title colors for a newly inserted book from settings + cover."""
    bar_cfg = color_defaults()["bar"]
    title_cfg = color_defaults()["title"]
    need_cover = _group_needs_cover(bar_cfg) or _group_needs_cover(title_cfg)
    cover_hsb = dominant_hsb_from_cover_url(cover_url) if need_cover else None
    # Cover-H with no sample: keep hash preset variety (legacy).
    if bar_cfg["h"]["fromCover"] and cover_hsb is None:
        bar = default_bar_color(book_id)
    else:
        h, s, b = _resolve_hsb(bar_cfg, cover_hsb)
        bar = _hsb_to_hex(h, s, b)
    title = default_title_color(bar, cover_hsb)
    return bar, title


def bar_color_for_new_book(book_id: str, cover_url: str | None = None) -> str:
    """New books: pastel bar from settings + cover when possible; else hash preset."""
    return new_book_colors(book_id, cover_url)[0]


def bar_color_for(book_id: str, stored: str | None = None) -> str:
    raw = str(stored or "").strip()
    if len(raw) == 7 and raw.startswith("#") and all(ch in "0123456789abcdefABCDEF" for ch in raw[1:]):
        return raw
    return default_bar_color(book_id)


def reassign_preset_colors() -> int:
    """把现有书的横条颜色重新分配到九色预设，尽量均匀。"""
    import random

    with connect() as conn:
        ids = [row["id"] for row in conn.execute("SELECT id FROM books")]
        if not ids:
            return 0
        rng = random.Random()
        order = list(ids)
        rng.shuffle(order)
        palette = list(BAR_PRESETS)
        rng.shuffle(palette)
        for i, book_id in enumerate(order):
            conn.execute(
                "UPDATE books SET bar_color = ? WHERE id = ?",
                (palette[i % len(palette)], book_id),
            )
        return len(order)


def ensure_bar_palette() -> int:
    if get_setting("barPalette") == "v9":
        return 0
    n = reassign_preset_colors()
    set_setting("barPalette", "v9")
    return n


def is_finished(progress) -> bool:
    try:
        return int(progress or 0) >= 100
    except (TypeError, ValueError):
        return False


def _book_words_per_second(conn, book_id: str, book: dict) -> float | None:
    """Average reading speed in words/second for estimating period word count.

    Finished books: total word count / time spent through the finish date.
    Unfinished books: (progress × word count) / lifetime reading time.
    """
    try:
        word_count = int(book.get("word_count") or 0)
    except (TypeError, ValueError):
        word_count = 0
    if word_count <= 0:
        return None
    finish = str(book.get("finish_date") or "").strip()[:10]
    if len(finish) < 10:
        finish = ""
    finished = is_finished(book.get("progress")) or bool(finish)
    if finished:
        if finish:
            row = conn.execute(
                "SELECT COALESCE(SUM(seconds), 0) FROM entries WHERE book_id = ? AND day <= ?",
                (book_id, finish),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COALESCE(SUM(seconds), 0) FROM entries WHERE book_id = ?",
                (book_id,),
            ).fetchone()
        seconds = int(row[0] or 0)
        if seconds <= 0:
            return None
        return word_count / seconds
    try:
        progress = max(0, min(100, int(book.get("progress") or 0)))
    except (TypeError, ValueError):
        progress = 0
    words_read = word_count * (progress / 100.0)
    if words_read <= 0:
        return None
    row = conn.execute(
        "SELECT COALESCE(SUM(seconds), 0) FROM entries WHERE book_id = ?",
        (book_id,),
    ).fetchone()
    seconds = int(row[0] or 0)
    if seconds <= 0:
        return None
    return words_read / seconds


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS books (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            source TEXT NOT NULL,
            bar_color TEXT,
            title_color TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id TEXT NOT NULL,
            day TEXT NOT NULL,
            seconds INTEGER NOT NULL,
            source TEXT NOT NULL,
            seconds_custom INTEGER NOT NULL DEFAULT 0,
            UNIQUE(book_id, day, source)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    cols = {row[1] for row in conn.execute("PRAGMA table_info(books)")}
    for name, spec in (
        ("cover", "TEXT"),
        ("progress", "INTEGER"),
        ("word_count", "INTEGER"),
        ("last_open", "INTEGER"),
        ("author", "TEXT"),
        ("title_custom", "INTEGER"),
        ("finish_date", "TEXT"),
        ("finish_date_custom", "INTEGER"),
        ("progress_custom", "INTEGER"),
        ("cover_custom", "INTEGER"),
        ("author_custom", "INTEGER"),
        ("word_count_custom", "INTEGER"),
        ("isbn", "TEXT"),
        ("publisher", "TEXT"),
        ("translator", "TEXT"),
        ("price", "TEXT"),
        ("publish_year", "TEXT"),
    ):
        if name not in cols:
            conn.execute("ALTER TABLE books ADD COLUMN %s %s" % (name, spec))
    entry_cols = {row[1] for row in conn.execute("PRAGMA table_info(entries)")}
    if "seconds_custom" not in entry_cols:
        conn.execute("ALTER TABLE entries ADD COLUMN seconds_custom INTEGER NOT NULL DEFAULT 0")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS entry_tombstones (
            book_id TEXT NOT NULL,
            day TEXT NOT NULL,
            PRIMARY KEY (book_id, day)
        )
        """
    )
    return conn


# Wallpaper chrome and 调色/色调. Books, records, covers, and per-book colors stay
# on the unscoped keys. A device that has never saved its own copy reads the
# legacy shared value once, then writes only `mac:` / `b7:` and diverges.
DEVICE_STYLE_KEYS = frozenset({
    "wallpaperMonth",
    "plannerOrient",
    "portraitRecRows",
    "barRows",
    "plannerPad",
    "calScroll",
    "calStats",
    "calRecords",
    "calSplit",
    "uiTheme",
    "uiTonePresets",
    "uiGrade",
    "uiGradePresets",
    "lastView",
    "recentOffset",
})
_device_var: contextvars.ContextVar[str] = contextvars.ContextVar("reading_log_device", default="mac")


def normalize_device(value) -> str:
    raw = str(value or "").strip().lower()
    if raw in ("1", "b7", "phone", "android"):
        return "b7"
    return "mac"


def bind_device(value):
    return _device_var.set(normalize_device(value))


def reset_device(token) -> None:
    _device_var.reset(token)


def current_device() -> str:
    return _device_var.get()


def _device_setting_key(key: str) -> str | None:
    if key not in DEVICE_STYLE_KEYS:
        return None
    return current_device() + ":" + key


def get_setting(key: str, default: str = "") -> str:
    scoped = _device_setting_key(key)
    with connect() as conn:
        if scoped:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (scoped,)).fetchone()
            if row is not None:
                return row["value"]
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    store_key = _device_setting_key(key) or key
    with connect() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (store_key, str(value)),
        )


def min_minutes() -> int:
    try:
        return max(0, int(get_setting("minMinutes", "3")))
    except ValueError:
        return 3


def set_min_minutes(n: int) -> int:
    n = max(0, min(600, int(n)))
    set_setting("minMinutes", str(n))
    return n


def min_book_total_minutes() -> int:
    try:
        return max(0, int(get_setting("minBookTotalMinutes", "0")))
    except ValueError:
        return 0


def set_min_book_total_minutes(n: int) -> int:
    n = max(0, min(600, int(n)))
    set_setting("minBookTotalMinutes", str(n))
    return n


def sync_interval_minutes() -> int:
    return _parse_sync_interval(get_setting("syncIntervalMinutes", "0"))


def set_sync_interval_minutes(n) -> int:
    n = _parse_sync_interval(n)
    set_setting("syncIntervalMinutes", str(n))
    return n


def _parse_sync_interval(n) -> int:
    if n is None or n is False:
        return 0
    if isinstance(n, str):
        n = n.strip()
        if not n:
            return 0
    try:
        n = int(float(n))
    except (TypeError, ValueError):
        return 0
    return max(0, min(10080, n))


def weread_sync_at() -> int:
    try:
        return max(0, int(get_setting("wereadSyncAt", "0") or 0))
    except ValueError:
        return 0


def _normalize_sync_time(raw) -> str | None:
    text = str(raw or "").strip()
    if len(text) >= 5 and text[2] != ":":
        return None
    parts = text.split(":")
    if len(parts) < 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return None
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return None
    return "%02d:%02d" % (hour, minute)


def sync_times() -> list[str]:
    raw = get_setting("syncTimes", "")
    data = []
    if raw:
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = [part.strip() for part in raw.split(",") if part.strip()]
        if isinstance(parsed, list):
            data = parsed
        elif isinstance(parsed, str):
            data = [parsed]
    return _clean_sync_times(data)


def set_sync_times(value) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            value = []
        else:
            try:
                value = json.loads(text)
            except Exception:
                value = [part.strip() for part in text.split(",") if part.strip()]
    if not isinstance(value, list):
        value = []
    times = _clean_sync_times(value)
    set_setting("syncTimes", json.dumps(times, ensure_ascii=False))
    return times


def _clean_sync_times(value) -> list[str]:
    out: list[str] = []
    for item in value or []:
        hm = _normalize_sync_time(item)
        if hm and hm not in out:
            out.append(hm)
    out.sort()
    return out


def short_lifetime_book_ids(conn, threshold_seconds: int) -> set[str]:
    if threshold_seconds <= 0:
        return set()
    rows = conn.execute(
        "SELECT book_id, SUM(seconds) AS seconds FROM entries GROUP BY book_id"
    ).fetchall()
    skip = {row["book_id"] for row in rows if int(row["seconds"] or 0) < threshold_seconds}
    known = {row["id"] for row in conn.execute("SELECT id FROM books")}
    with_time = {row["book_id"] for row in rows if int(row["seconds"] or 0) > 0}
    # Synced stubs with no reading time stay hidden; manual shelf books may be empty on purpose.
    manual_ids = {
        row["id"]
        for row in conn.execute("SELECT id FROM books WHERE source = 'manual'")
    }
    skip |= (known - with_time) - manual_ids
    return skip


# CSS filter % for brightness/contrast (100 = identity). UI shows display 0–100
# where 50 = neutral (display = filter - 50); client remaps at the UI boundary.
UI_FILTER_DEFAULT = 100
UI_FILTER_MIN = 50
UI_FILTER_MAX = 150


def _ui_filter_pct(src, key, fallback=UI_FILTER_DEFAULT):
    if not isinstance(src, dict) or src.get(key) is None or src.get(key) == "":
        return fallback
    try:
        v = int(round(float(src.get(key))))
    except (TypeError, ValueError):
        return fallback
    return max(UI_FILTER_MIN, min(UI_FILTER_MAX, v))


def ui_theme(setting_key: str = "uiTheme") -> dict:
    raw = get_setting(setting_key, "")
    try:
        obj = json.loads(raw) if raw else {}
    except Exception:
        obj = {}
    if not isinstance(obj, dict):
        obj = {}
    def _num(key):
        try:
            if obj.get(key) is None or obj.get(key) == "":
                return None
            return int(obj.get(key))
        except (TypeError, ValueError):
            return None
    return {
        "h": _num("h"),
        "s": _num("s"),
        "v": _num("v"),
        "invert": bool(obj.get("invert")),
        "brightness": _ui_filter_pct(obj, "brightness"),
        "contrast": _ui_filter_pct(obj, "contrast"),
    }


def set_ui_theme(body: dict | None, setting_key: str = "uiTheme") -> dict:
    body = body or {}
    current = ui_theme(setting_key)
    theme = {
        "h": current["h"] if body.get("h") is None else int(body.get("h")),
        "s": current["s"] if body.get("s") is None else int(body.get("s")),
        "v": current["v"] if body.get("v") is None else int(body.get("v")),
        "invert": bool(body.get("invert")) if "invert" in body else bool(current["invert"]),
        "brightness": _ui_filter_pct(body, "brightness", current["brightness"]),
        "contrast": _ui_filter_pct(body, "contrast", current["contrast"]),
    }
    set_setting(setting_key, json.dumps(theme, ensure_ascii=False))
    return theme


def ui_grade() -> dict:
    return ui_theme("uiGrade")


def set_ui_grade(body: dict | None) -> dict:
    return set_ui_theme(body, "uiGrade")


UI_TONE_PRESETS_MAX = 24


def _normalize_ui_tone_preset(item) -> dict | None:
    if not isinstance(item, dict):
        return None
    try:
        h = int(round(float(item.get("h"))))
        s = int(round(float(item.get("s"))))
        v = int(round(float(item.get("v"))))
    except (TypeError, ValueError):
        return None
    h = max(0, min(360, h))
    s = max(0, min(100, s))
    v = max(0, min(100, v))
    pid = str(item.get("id") or "").strip()
    if not pid:
        pid = "p%d" % (abs(hash((h, s, v, item.get("invert"), item.get("brightness"), item.get("contrast")))) % (10**12),)
    return {
        "id": pid[:48],
        "h": h,
        "s": s,
        "v": v,
        "invert": bool(item.get("invert")),
        "brightness": _ui_filter_pct(item, "brightness"),
        "contrast": _ui_filter_pct(item, "contrast"),
    }


def ui_tone_presets(setting_key: str = "uiTonePresets") -> dict:
    raw = get_setting(setting_key, "")
    try:
        obj = json.loads(raw) if raw else None
    except Exception:
        obj = None
    items: list[dict] = []
    if isinstance(obj, dict) and isinstance(obj.get("items"), list):
        src = obj.get("items")
        saved = True
    elif isinstance(obj, list):
        src = obj
        saved = True
    else:
        src = []
        saved = bool(raw)
    for item in src:
        norm = _normalize_ui_tone_preset(item)
        if norm:
            items.append(norm)
        if len(items) >= UI_TONE_PRESETS_MAX:
            break
    return {"saved": saved, "items": items}


def set_ui_tone_presets(body, setting_key: str = "uiTonePresets") -> dict:
    if isinstance(body, dict) and isinstance(body.get("items"), list):
        src = body.get("items")
    elif isinstance(body, list):
        src = body
    else:
        src = []
    items: list[dict] = []
    seen: set[str] = set()
    for item in src:
        norm = _normalize_ui_tone_preset(item)
        if not norm or norm["id"] in seen:
            continue
        seen.add(norm["id"])
        items.append(norm)
        if len(items) >= UI_TONE_PRESETS_MAX:
            break
    payload = {"items": items}
    set_setting(setting_key, json.dumps(payload, ensure_ascii=False))
    return {"saved": True, "items": items}


def ui_grade_presets() -> dict:
    return ui_tone_presets("uiGradePresets")


def set_ui_grade_presets(body) -> dict:
    return set_ui_tone_presets(body, "uiGradePresets")


# Pad values are % of wallpaper width (left/right) or height (top/bottom), 0–50.
PAD_PCT_MAX = 50
PAD_X_DEFAULT = 40  # legacy CSS px (pre-v4)
PAD_B_DEFAULT = 56  # legacy CSS px (pre-v4)
PAD_MAX = 160  # legacy CSS px clamp (pre-v4)
LANDSCAPE_PAD_W = 3840
LANDSCAPE_PAD_H = 2160
PORTRAIT_PAD_W = 2160
PORTRAIT_PAD_H = 2880


def _pad_defaults(orient: str = "landscape") -> dict:
    if orient == "track":
        # Track card margins (independent of wallpaper landscape/portrait).
        return {
            "left": 2,
            "right": 2,
            "top": 2,
            "bottom": 2,
            "linkLR": True,
            "linkTB": True,
        }
    if orient == "portrait":
        # ≈40/2160, 230/2880, 16/2880 from prior fixed portrait insets.
        return {
            "left": 2,
            "right": 2,
            "top": 8,
            "bottom": 1,
            "linkLR": True,
            "linkTB": True,
        }
    # ≈40/3840, 80/2160, 56/2160 from prior landscape insets.
    return {
        "left": 1,
        "right": 1,
        "top": 4,
        "bottom": 3,
        "linkLR": True,
        "linkTB": True,
    }


def _clamp_pad(n, fallback: int) -> int:
    """Legacy px clamp (0–160). Kept for v1–v3 migrations."""
    try:
        v = int(round(float(n)))
    except (TypeError, ValueError):
        return fallback
    return max(0, min(PAD_MAX, v))


def _clamp_pad_pct(n, fallback: int) -> int:
    try:
        v = int(round(float(n)))
    except (TypeError, ValueError):
        return fallback
    return max(0, min(PAD_PCT_MAX, v))


def _parse_pad_bool(value, default: bool = True) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in ("1", "true", "on", "yes"):
        return True
    if s in ("0", "false", "off", "no"):
        return False
    return default


def _px_to_pct(px, dim: int, fallback_pct: int) -> int:
    try:
        return _clamp_pad_pct(round(float(px) / float(dim) * 100.0), fallback_pct)
    except (TypeError, ValueError, ZeroDivisionError):
        return fallback_pct


def _pad_xy(src: dict | None, fallback_x: int = PAD_X_DEFAULT, fallback_b: int = PAD_B_DEFAULT) -> dict:
    src = src if isinstance(src, dict) else {}
    has_x = src.get("x") is not None and src.get("x") != ""
    has_b = src.get("b") is not None and src.get("b") != ""
    return {
        "x": _clamp_pad(src.get("x"), fallback_x) if has_x else fallback_x,
        "b": _clamp_pad(src.get("b"), fallback_b) if has_b else fallback_b,
    }


def _pad_slot(src: dict | None, orient: str = "landscape") -> dict:
    """Normalize a pad slot to {left,right,top,bottom,linkLR,linkTB} as %."""
    fb = _pad_defaults(orient)
    src = src if isinstance(src, dict) else {}
    # Already %-based (v4+), or partial update with new keys.
    has_pct = any(
        src.get(k) is not None and src.get(k) != ""
        for k in ("left", "right", "top", "bottom")
    )
    if has_pct:
        left = _clamp_pad_pct(src.get("left"), fb["left"]) if src.get("left") not in (None, "") else fb["left"]
        right = _clamp_pad_pct(src.get("right"), fb["right"]) if src.get("right") not in (None, "") else fb["right"]
        top = _clamp_pad_pct(src.get("top"), fb["top"]) if src.get("top") not in (None, "") else fb["top"]
        bottom = _clamp_pad_pct(src.get("bottom"), fb["bottom"]) if src.get("bottom") not in (None, "") else fb["bottom"]
        # If only legacy x/b also present without left/right, prefer left/right already set.
        return {
            "left": left,
            "right": right,
            "top": top,
            "bottom": bottom,
            "linkLR": _parse_pad_bool(src.get("linkLR"), True),
            "linkTB": _parse_pad_bool(src.get("linkTB"), True),
        }
    # Legacy {x,b} CSS px → % of design wallpaper for this orient.
    w = PORTRAIT_PAD_W if orient == "portrait" else LANDSCAPE_PAD_W
    h = PORTRAIT_PAD_H if orient == "portrait" else LANDSCAPE_PAD_H
    xy = _pad_xy(src)
    left = right = _px_to_pct(xy["x"], w, fb["left"])
    bottom = _px_to_pct(xy["b"], h, fb["bottom"])
    return {
        "left": left,
        "right": right,
        "top": fb["top"],
        "bottom": bottom,
        "linkLR": True,
        "linkTB": True,
    }


def _merge_pad_slot(target: dict, src: dict | None, orient: str) -> dict:
    out = dict(target)
    src = src if isinstance(src, dict) else {}
    if not src:
        return out
    # Prefer explicit % keys; fall back to legacy x/b px→%.
    if any(src.get(k) not in (None, "") for k in ("left", "right", "top", "bottom", "linkLR", "linkTB")):
        if src.get("left") not in (None, ""):
            out["left"] = _clamp_pad_pct(src.get("left"), out["left"])
        if src.get("right") not in (None, ""):
            out["right"] = _clamp_pad_pct(src.get("right"), out["right"])
        if src.get("top") not in (None, ""):
            out["top"] = _clamp_pad_pct(src.get("top"), out["top"])
        if src.get("bottom") not in (None, ""):
            out["bottom"] = _clamp_pad_pct(src.get("bottom"), out["bottom"])
        if "linkLR" in src:
            out["linkLR"] = _parse_pad_bool(src.get("linkLR"), out.get("linkLR", True))
        if "linkTB" in src:
            out["linkTB"] = _parse_pad_bool(src.get("linkTB"), out.get("linkTB", True))
        return out
    if src.get("x") not in (None, "") or src.get("b") not in (None, ""):
        migrated = _pad_slot(src, orient)
        if src.get("x") not in (None, ""):
            out["left"] = migrated["left"]
            out["right"] = migrated["right"]
        if src.get("b") not in (None, ""):
            out["bottom"] = migrated["bottom"]
    return out


def _parse_pad_orient(value, default: str = "landscape") -> str:
    raw = str(value or "").strip().lower()
    if raw in ("track", "追踪"):
        return "track"
    if raw in ("portrait", "竖版", "vertical"):
        return "portrait"
    if raw in ("landscape", "横版", "horizontal"):
        return "landscape"
    return default


def planner_pad() -> dict:
    raw = get_setting("plannerPad", "")
    try:
        obj = json.loads(raw) if raw else {}
    except Exception:
        obj = {}
    if not isinstance(obj, dict):
        obj = {}
    ver = int(obj.get("v") or 0)
    # Design base moved 1920→3840: persisted CSS pads authored at the old scale need ×2 once.
    if raw and ver < 2:
        def _migrate_pad(key, fallback):
            if obj.get(key) in (None, ""):
                return fallback
            try:
                return _clamp_pad(float(obj[key]) * 2, fallback)
            except (TypeError, ValueError):
                return fallback
        obj = {
            "x": _migrate_pad("x", PAD_X_DEFAULT),
            "b": _migrate_pad("b", PAD_B_DEFAULT),
            "v": 2,
        }
        set_setting("plannerPad", json.dumps(obj, ensure_ascii=False))
        ver = 2
    # v3: landscape / portrait pads remembered separately.
    if raw and ver < 3:
        shared = _pad_xy(obj)
        obj = {
            "v": 3,
            "landscape": dict(shared),
            "portrait": dict(shared),
        }
        set_setting("plannerPad", json.dumps(obj, ensure_ascii=False))
        ver = 3
    # v4: % insets + left/right/top/bottom + link flags.
    if raw and ver < 4:
        if ver >= 3:
            land_xy = _pad_xy(obj.get("landscape") if isinstance(obj.get("landscape"), dict) else obj)
            port_xy = _pad_xy(obj.get("portrait") if isinstance(obj.get("portrait"), dict) else obj)
        else:
            shared = _pad_xy(obj)
            land_xy = dict(shared)
            port_xy = dict(shared)
        obj = {
            "v": 4,
            "landscape": _pad_slot(land_xy, "landscape"),
            "portrait": _pad_slot(port_xy, "portrait"),
        }
        set_setting("plannerPad", json.dumps(obj, ensure_ascii=False))
        ver = 4
    # v5: track mode pads independent of wallpaper landscape/portrait.
    if raw and ver < 5:
        if ver >= 4:
            landscape = _pad_slot(obj.get("landscape") if isinstance(obj.get("landscape"), dict) else None, "landscape")
            portrait = _pad_slot(obj.get("portrait") if isinstance(obj.get("portrait"), dict) else None, "portrait")
        elif ver >= 3:
            landscape = _pad_slot(obj.get("landscape") if isinstance(obj.get("landscape"), dict) else obj, "landscape")
            portrait = _pad_slot(obj.get("portrait") if isinstance(obj.get("portrait"), dict) else obj, "portrait")
        else:
            shared = _pad_slot(obj, "landscape")
            landscape = dict(shared)
            portrait = _pad_slot(obj, "portrait")
        track = _pad_slot(obj.get("track") if isinstance(obj.get("track"), dict) else None, "track")
        obj = {
            "v": 5,
            "landscape": landscape,
            "portrait": portrait,
            "track": track,
        }
        set_setting("plannerPad", json.dumps(obj, ensure_ascii=False))
        ver = 5
    if ver >= 5:
        landscape = _pad_slot(obj.get("landscape") if isinstance(obj.get("landscape"), dict) else None, "landscape")
        portrait = _pad_slot(obj.get("portrait") if isinstance(obj.get("portrait"), dict) else None, "portrait")
        track = _pad_slot(obj.get("track") if isinstance(obj.get("track"), dict) else None, "track")
    elif ver >= 4:
        landscape = _pad_slot(obj.get("landscape") if isinstance(obj.get("landscape"), dict) else None, "landscape")
        portrait = _pad_slot(obj.get("portrait") if isinstance(obj.get("portrait"), dict) else None, "portrait")
        track = _pad_defaults("track")
    elif ver >= 3:
        landscape = _pad_slot(obj.get("landscape") if isinstance(obj.get("landscape"), dict) else obj, "landscape")
        portrait = _pad_slot(obj.get("portrait") if isinstance(obj.get("portrait"), dict) else obj, "portrait")
        track = _pad_defaults("track")
    else:
        shared = _pad_slot(obj, "landscape")
        landscape = dict(shared)
        portrait = _pad_slot(obj, "portrait")
        track = _pad_defaults("track")
    return {
        "landscape": landscape,
        "portrait": portrait,
        "track": track,
        # Legacy top-level aliases = landscape left/bottom (%).
        "x": landscape["left"],
        "b": landscape["bottom"],
        "left": landscape["left"],
        "right": landscape["right"],
        "top": landscape["top"],
        "bottom": landscape["bottom"],
        "saved": bool(raw),
    }


def set_planner_pad(body: dict | None) -> dict:
    body = body or {}
    current = planner_pad()
    landscape = dict(current["landscape"])
    portrait = dict(current["portrait"])
    track = dict(current["track"])

    if (
        isinstance(body.get("landscape"), dict)
        or isinstance(body.get("portrait"), dict)
        or isinstance(body.get("track"), dict)
    ):
        if isinstance(body.get("landscape"), dict):
            landscape = _merge_pad_slot(landscape, body["landscape"], "landscape")
        if isinstance(body.get("portrait"), dict):
            portrait = _merge_pad_slot(portrait, body["portrait"], "portrait")
        if isinstance(body.get("track"), dict):
            track = _merge_pad_slot(track, body["track"], "track")
    else:
        orient = _parse_pad_orient(body.get("orient"), "landscape")
        if orient == "track":
            track = _merge_pad_slot(track, body, "track")
        elif orient == "portrait":
            portrait = _merge_pad_slot(portrait, body, "portrait")
        else:
            landscape = _merge_pad_slot(landscape, body, "landscape")

    pad = {
        "v": 5,
        "landscape": landscape,
        "portrait": portrait,
        "track": track,
    }
    set_setting("plannerPad", json.dumps(pad, ensure_ascii=False))
    return planner_pad()


def _parse_cal_scroll(value, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in ("1", "true", "on", "yes"):
        return True
    if s in ("0", "false", "off", "no"):
        return False
    return default


def cal_scroll() -> dict:
    raw = get_setting("calScroll", "")
    if raw == "":
        return {"on": True, "saved": False}
    return {"on": _parse_cal_scroll(raw, True), "saved": True}


def set_cal_scroll(value) -> dict:
    if isinstance(value, dict):
        value = value.get("on")
    on = _parse_cal_scroll(value, True)
    set_setting("calScroll", "1" if on else "0")
    return cal_scroll()


TRACK_TITLE_DEFAULT = "READING"
TRACK_TITLE_MAX = 48


def track_title() -> dict:
    raw = str(get_setting("trackTitle", "") or "").strip()
    if not raw:
        return {"title": TRACK_TITLE_DEFAULT, "saved": False}
    title = raw[:TRACK_TITLE_MAX]
    return {"title": title or TRACK_TITLE_DEFAULT, "saved": True}


def set_track_title(value) -> dict:
    if isinstance(value, dict):
        value = value.get("title")
    title = str(value or "").strip()[:TRACK_TITLE_MAX]
    if not title:
        title = TRACK_TITLE_DEFAULT
    set_setting("trackTitle", title)
    return {"title": title, "saved": True}


_CAL_SIDE_SLOTS = ("landscape", "portrait", "track")


def _cal_side_map(raw: str, default_on: bool) -> dict:
    """Parse per-slot map; legacy "0"/"1" seeds all slots."""
    base = {k: default_on for k in _CAL_SIDE_SLOTS}
    if raw == "":
        return {**base, "on": default_on, "saved": False}
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict) and any(k in obj for k in _CAL_SIDE_SLOTS):
            out = {
                k: _parse_cal_scroll(obj[k], default_on) if k in obj else default_on
                for k in _CAL_SIDE_SLOTS
            }
            active = obj.get("on")
            if active is None:
                active = out.get("landscape", default_on)
            out["on"] = _parse_cal_scroll(active, default_on)
            out["saved"] = True
            return out
    except Exception:
        pass
    on = _parse_cal_scroll(raw, default_on)
    return {**{k: on for k in _CAL_SIDE_SLOTS}, "on": on, "saved": True}


def _set_cal_side_map(setting_key: str, value, default_on: bool) -> dict:
    cur = _cal_side_map(get_setting(setting_key, ""), default_on)
    slots = {k: cur[k] for k in _CAL_SIDE_SLOTS}
    active = cur["on"]
    if isinstance(value, dict):
        has_slots = any(k in value for k in _CAL_SIDE_SLOTS)
        slot = str(value.get("slot") or "").strip().lower()
        for k in _CAL_SIDE_SLOTS:
            if k in value:
                slots[k] = _parse_cal_scroll(value[k], slots[k])
        if value.get("on") is not None:
            active = _parse_cal_scroll(value.get("on"), active)
            if slot in _CAL_SIDE_SLOTS:
                slots[slot] = active
            elif not has_slots:
                # Legacy {on: bool} with no per-slot fields → seed all slots.
                slots = {k: active for k in _CAL_SIDE_SLOTS}
        elif slot in _CAL_SIDE_SLOTS and slot in value:
            active = slots[slot]
    else:
        active = _parse_cal_scroll(value, default_on)
        slots = {k: active for k in _CAL_SIDE_SLOTS}
    payload = {**slots, "on": active}
    set_setting(setting_key, json.dumps(payload, ensure_ascii=False))
    return {**payload, "saved": True}


def _parse_cal_stats_step(value, default: int = 0) -> int:
    """柱状图分级分钟：0=关闭；旧布尔 true→50、false→0。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return 50 if value else 0
    if isinstance(value, (int, float)):
        try:
            return max(0, int(value))
        except Exception:
            return default
    s = str(value).strip().lower()
    if s in ("1", "true", "on", "yes"):
        return 50
    if s in ("0", "false", "off", "no", ""):
        return 0
    try:
        return max(0, int(float(s)))
    except Exception:
        return default


def _cal_stats_step_map(raw: str, default_step: int = 0) -> dict:
    base = {k: default_step for k in _CAL_SIDE_SLOTS}
    if raw == "":
        return {**base, "on": default_step > 0, "step": default_step, "saved": False}
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict) and any(k in obj for k in _CAL_SIDE_SLOTS):
            out = {
                k: _parse_cal_stats_step(obj[k], default_step) if k in obj else default_step
                for k in _CAL_SIDE_SLOTS
            }
            active = out.get("landscape", default_step)
            if obj.get("step") is not None:
                active = _parse_cal_stats_step(obj.get("step"), active)
            elif obj.get("on") is not None and not any(k in obj for k in _CAL_SIDE_SLOTS):
                active = _parse_cal_stats_step(obj.get("on"), active)
            out["on"] = active > 0
            out["step"] = active
            out["saved"] = True
            return out
    except Exception:
        pass
    step = _parse_cal_stats_step(raw, default_step)
    return {**{k: step for k in _CAL_SIDE_SLOTS}, "on": step > 0, "step": step, "saved": True}


def _set_cal_stats_step_map(value, default_step: int = 0) -> dict:
    cur = _cal_stats_step_map(get_setting("calStats", ""), default_step)
    slots = {k: int(cur[k]) for k in _CAL_SIDE_SLOTS}
    active = int(cur.get("step") or slots.get("landscape", default_step))
    if isinstance(value, dict):
        has_slots = any(k in value for k in _CAL_SIDE_SLOTS)
        slot = str(value.get("slot") or "").strip().lower()
        for k in _CAL_SIDE_SLOTS:
            if k in value:
                slots[k] = _parse_cal_stats_step(value[k], slots[k])
        step_val = value.get("step")
        if step_val is None and value.get("on") is not None:
            step_val = value.get("on")
        if step_val is not None:
            step = _parse_cal_stats_step(step_val, 0)
            if slot in _CAL_SIDE_SLOTS:
                slots[slot] = step
                active = step
            elif not has_slots:
                slots = {k: step for k in _CAL_SIDE_SLOTS}
                active = step
            else:
                active = step
        elif slot in _CAL_SIDE_SLOTS:
            active = slots[slot]
    else:
        step = _parse_cal_stats_step(value, default_step)
        slots = {k: step for k in _CAL_SIDE_SLOTS}
        active = step
    payload = {**slots, "on": active > 0, "step": active}
    set_setting("calStats", json.dumps(payload, ensure_ascii=False))
    return {**payload, "saved": True}


def cal_stats() -> dict:
    return _cal_stats_step_map(get_setting("calStats", ""), 0)


def set_cal_stats(value) -> dict:
    return _set_cal_stats_step_map(value, 0)


def cal_records() -> dict:
    return _cal_side_map(get_setting("calRecords", ""), True)


def set_cal_records(value) -> dict:
    return _set_cal_side_map("calRecords", value, True)


def cal_split() -> dict:
    """Per-slot 拆分：landscape / portrait / track（旧全局 0/1 会种到三槽）。"""
    return _cal_side_map(get_setting("calSplit", ""), False)


def set_cal_split(value) -> dict:
    return _set_cal_side_map("calSplit", value, False)


def _parse_planner_orient(value, default: str = "landscape") -> str:
    raw = str(value or "").strip().lower()
    if raw in ("portrait", "竖版", "vertical"):
        return "portrait"
    if raw in ("landscape", "横版", "horizontal"):
        return "landscape"
    return default


def planner_orient() -> dict:
    raw = get_setting("plannerOrient", "")
    if raw == "":
        return {"orient": "landscape", "saved": False}
    return {"orient": _parse_planner_orient(raw, "landscape"), "saved": True}


def set_planner_orient(value) -> dict:
    if isinstance(value, dict):
        value = value.get("orient") or value.get("value")
    orient = _parse_planner_orient(value, "landscape")
    set_setting("plannerOrient", orient)
    return planner_orient()


PORTRAIT_REC_ROWS_DEFAULT = 5
PORTRAIT_REC_ROWS_MIN = 3
PORTRAIT_REC_ROWS_MAX = 20


def _parse_portrait_rec_rows(value, default: int = PORTRAIT_REC_ROWS_DEFAULT) -> int:
    if isinstance(value, dict):
        value = value.get("rows", value.get("value", value.get("n")))
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(PORTRAIT_REC_ROWS_MIN, min(PORTRAIT_REC_ROWS_MAX, n))


def portrait_rec_rows() -> dict:
    raw = get_setting("portraitRecRows", "")
    if raw == "":
        return {"rows": PORTRAIT_REC_ROWS_DEFAULT, "saved": False}
    return {"rows": _parse_portrait_rec_rows(raw), "saved": True}


def set_portrait_rec_rows(value) -> dict:
    if isinstance(value, dict):
        value = value.get("rows", value.get("value", value.get("n")))
    rows = _parse_portrait_rec_rows(value, PORTRAIT_REC_ROWS_DEFAULT)
    set_setting("portraitRecRows", str(rows))
    return portrait_rec_rows()


BAR_ROWS_DEFAULT = 4
BAR_ROWS_MIN = 1
BAR_ROWS_MAX = 8
_BAR_ROWS_VIEWS = ("landscape", "portrait", "track")


def _parse_bar_rows_n(value, default: int = BAR_ROWS_DEFAULT) -> int:
    if isinstance(value, dict):
        value = value.get("rows", value.get("value", value.get("n")))
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(BAR_ROWS_MIN, min(BAR_ROWS_MAX, n))


def _bar_rows_defaults() -> dict:
    return {k: BAR_ROWS_DEFAULT for k in _BAR_ROWS_VIEWS}


def bar_rows() -> dict:
    raw = get_setting("barRows", "")
    out = _bar_rows_defaults()
    saved = False
    if raw != "":
        try:
            obj = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            obj = None
        if isinstance(obj, dict):
            saved = True
            for key in _BAR_ROWS_VIEWS:
                if key in obj:
                    out[key] = _parse_bar_rows_n(obj.get(key), BAR_ROWS_DEFAULT)
        else:
            # Legacy single integer → all views.
            n = _parse_bar_rows_n(raw, BAR_ROWS_DEFAULT)
            out = {k: n for k in _BAR_ROWS_VIEWS}
            saved = True
    out["saved"] = saved
    return out


def set_bar_rows(value) -> dict:
    cur = bar_rows()
    body = value if isinstance(value, dict) else {}
    next_rows = {k: cur[k] for k in _BAR_ROWS_VIEWS}
    if isinstance(value, dict):
        for key in _BAR_ROWS_VIEWS:
            if key in body:
                next_rows[key] = _parse_bar_rows_n(body.get(key), next_rows[key])
        # Allow {rows: N} / bare number to set the active view only when tagged.
        if "rows" in body and body.get("view") in _BAR_ROWS_VIEWS:
            next_rows[body["view"]] = _parse_bar_rows_n(body.get("rows"), next_rows[body["view"]])
    elif value is not None and value != "":
        n = _parse_bar_rows_n(value, BAR_ROWS_DEFAULT)
        next_rows = {k: n for k in _BAR_ROWS_VIEWS}
    set_setting("barRows", json.dumps(next_rows, ensure_ascii=False))
    return bar_rows()


def _parse_cal_kind(raw, fallback: str = "month") -> str:
    kind = str(raw or "").strip().lower()
    if kind in ("month", "recent"):
        return kind
    return fallback if fallback in ("month", "recent") else "month"


def last_view() -> dict:
    raw = get_setting("lastView", "")
    try:
        obj = json.loads(raw) if raw else {}
    except Exception:
        obj = {}
    if not isinstance(obj, dict) or not obj:
        return {}
    today = date.today()
    year, month = clamp_month(today.year, today.month)
    mode = str(obj.get("mode") or "month").strip().lower()
    if mode not in ("month", "recent", "books", "track"):
        mode = "month"
    # Explicit calKind preferred; else infer only from calendar modes (not track/books).
    if mode in ("month", "recent"):
        cal_kind = _parse_cal_kind(obj.get("calKind"), mode)
    else:
        cal_kind = _parse_cal_kind(obj.get("calKind"), "month")
    try:
        year, month = clamp_month(int(obj.get("year") or year), int(obj.get("month") or month))
    except Exception:
        pass
    return {
        "mode": mode,
        "year": year,
        "month": month,
        "recentOffset": recent_offset(),
        "calKind": cal_kind,
    }


def set_last_view(body: dict | None) -> dict:
    body = body or {}
    current = last_view()
    today = date.today()
    fallback_year, fallback_month = clamp_month(today.year, today.month)
    mode = str(body.get("mode") or current.get("mode") or "month").strip().lower()
    if mode not in ("month", "recent", "books", "track"):
        mode = "month"
    if body.get("calKind") is not None:
        cal_kind = _parse_cal_kind(body.get("calKind"), current.get("calKind") or "month")
    elif mode in ("month", "recent"):
        cal_kind = mode
    else:
        cal_kind = _parse_cal_kind(current.get("calKind"), "month")
    try:
        year = int(body["year"]) if body.get("year") is not None else int(current.get("year") or fallback_year)
        month = int(body["month"]) if body.get("month") is not None else int(current.get("month") or fallback_month)
        year, month = clamp_month(year, month)
    except Exception:
        year, month = fallback_year, fallback_month
    if body.get("recentOffset") is not None:
        off = set_recent_offset(body.get("recentOffset"))
    else:
        off = recent_offset()
    view = {
        "mode": mode,
        "year": year,
        "month": month,
        "recentOffset": off,
        "calKind": cal_kind,
    }
    set_setting("lastView", json.dumps(view, ensure_ascii=False))
    return view


def rename_book(book_id: str, title: str) -> dict:
    title = str(title or "").strip()
    if not title:
        raise ValueError("请填写书名")
    if not book_id:
        raise ValueError("找不到这本书")
    with connect() as conn:
        row = conn.execute("SELECT id FROM books WHERE id = ?", (book_id,)).fetchone()
        if not row:
            raise ValueError("找不到这本书")
        conn.execute(
            "UPDATE books SET title = ?, title_custom = 1 WHERE id = ?",
            (title, book_id),
        )
        row = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    return {key: row[key] for key in row.keys()}


def create_manual_book(title: str) -> dict:
    """Create a local (non-WeRead) shelf book with empty cover; id uses manual: prefix."""
    title = str(title or "").strip()
    if not title:
        raise ValueError("请填写书名")
    book_id = "manual:" + uuid.uuid4().hex[:10]
    bar, title_col = new_book_colors(book_id)
    with connect() as conn:
        # Prefer attaching to an existing same-title shelf copy.
        found = conn.execute(
            """
            SELECT id FROM books
            WHERE lower(title) = ?
            ORDER BY CASE source WHEN 'weread' THEN 0 WHEN 'manual' THEN 1 ELSE 2 END, id
            LIMIT 1
            """,
            (title.lower(),),
        ).fetchone()
        if found:
            row = conn.execute("SELECT * FROM books WHERE id = ?", (found["id"],)).fetchone()
            return _book_row_public(row)
        conn.execute(
            "INSERT INTO books(id, title, source, bar_color, title_color, title_custom) VALUES(?, ?, ?, ?, ?, 1)",
            (book_id, title, "manual", bar, title_col),
        )
        row = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    return _book_row_public(row)


def _book_row_public(row) -> dict:
    """Minimal book dict for create/update API responses."""
    item = {key: row[key] for key in row.keys()}
    progress = item.get("progress")
    stored_finish = str(item.get("finish_date") or "").strip()[:10]
    if stored_finish and len(stored_finish) < 10:
        stored_finish = ""
    try:
        progress_custom = int(item.get("progress_custom") or 0) == 1
    except (TypeError, ValueError):
        progress_custom = False
    return {
        "id": item["id"],
        "title": item.get("title") or "",
        "source": item.get("source") or "manual",
        "cover": _cover_for_payload(item.get("cover") or ""),
        "progress": progress,
        "progressCustom": progress_custom,
        "finished": is_finished(progress),
        "wordCount": int(item.get("word_count") or 0),
        "author": item.get("author") or "",
        "finishDate": stored_finish or None,
        "barColor": bar_color_for(item["id"], item.get("bar_color")),
        "titleColor": title_color_for(item.get("title_color"), item.get("bar_color")),
        "seconds": 0,
        "minutes": 0,
        "readDays": 0,
        "lastOpen": int(item.get("last_open") or 0),
    }


def set_book_author(book_id: str, author: str) -> dict:
    book_id = str(book_id or "").strip()
    if not book_id:
        raise ValueError("找不到这本书")
    author = str(author or "").strip()
    # Drop a trailing 著 so we don't double it on display.
    if author.endswith("著"):
        author = author[:-1].rstrip()
    with connect() as conn:
        row = conn.execute("SELECT id FROM books WHERE id = ?", (book_id,)).fetchone()
        if not row:
            raise ValueError("找不到这本书")
        conn.execute(
            "UPDATE books SET author = ?, author_custom = 1 WHERE id = ?",
            (author, book_id),
        )
        row = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    return _book_row_public(row)


def parse_word_count(raw) -> int:
    if isinstance(raw, bool) or raw is None:
        raise ValueError("字数不对")
    if isinstance(raw, (int, float)):
        n = int(round(float(raw)))
    else:
        text = str(raw).strip().replace(",", "").replace(" ", "")
        if not text or text in ("—", "-"):
            raise ValueError("请填写字数")
        wan = False
        if text.endswith("万"):
            wan = True
            text = text[:-1]
        try:
            n = float(text)
        except (TypeError, ValueError):
            raise ValueError("字数不对")
        if wan:
            n = n * 10000
        n = int(round(n))
    if n < 0:
        raise ValueError("字数不对")
    return n


def set_book_word_count(book_id: str, raw) -> dict:
    book_id = str(book_id or "").strip()
    if not book_id:
        raise ValueError("找不到这本书")
    word_count = parse_word_count(raw)
    with connect() as conn:
        row = conn.execute("SELECT id FROM books WHERE id = ?", (book_id,)).fetchone()
        if not row:
            raise ValueError("找不到这本书")
        conn.execute(
            "UPDATE books SET word_count = ?, word_count_custom = 1 WHERE id = ?",
            (word_count, book_id),
        )
        row = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    return _book_row_public(row)


def apply_synced_book_field(book_id: str, field: str, value) -> dict:
    """Write one WeRead-synced field and clear its manual lock."""
    book_id = str(book_id or "").strip()
    field = str(field or "").strip()
    if not book_id:
        raise ValueError("找不到这本书")
    with connect() as conn:
        row = conn.execute("SELECT id FROM books WHERE id = ?", (book_id,)).fetchone()
        if not row:
            raise ValueError("找不到这本书")
        if field == "title":
            title = str(value or "").strip()
            if not title:
                raise ValueError("没有读到书名")
            conn.execute(
                "UPDATE books SET title = ?, title_custom = 0 WHERE id = ?",
                (title, book_id),
            )
        elif field == "author":
            author = str(value or "").strip()
            if author.endswith("著"):
                author = author[:-1].rstrip()
            conn.execute(
                "UPDATE books SET author = ?, author_custom = 0 WHERE id = ?",
                (author, book_id),
            )
        elif field in ("wordCount", "word_count"):
            try:
                wc = int(value or 0)
            except (TypeError, ValueError):
                wc = 0
            if wc <= 0:
                raise ValueError("没有读到字数")
            conn.execute(
                "UPDATE books SET word_count = ?, word_count_custom = 0 WHERE id = ?",
                (wc, book_id),
            )
        else:
            raise ValueError("不支持同步该字段")
        row = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    return _book_row_public(row)


def parse_finish_date(raw: str) -> str:
    s = str(raw or "").strip()
    if not s:
        raise ValueError("请填写读完日期")
    s = s.replace("/", "-").replace(".", "-")
    parts = [p for p in s.split("-") if p != ""]
    try:
        if len(parts) == 3:
            year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
        elif len(parts) == 2:
            year, month, day = date.today().year, int(parts[0]), int(parts[1])
        else:
            raise ValueError("日期格式不对")
        if year < 1000 or year > 9999:
            raise ValueError("日期格式不对")
        return date(year, month, day).isoformat()
    except ValueError as exc:
        msg = str(exc)
        if msg in ("请填写读完日期", "日期格式不对"):
            raise
        raise ValueError("日期不对") from exc


def parse_progress(raw) -> int:
    if isinstance(raw, bool) or raw is None:
        raise ValueError("进度请填 0–100")
    if isinstance(raw, (int, float)):
        n = int(round(float(raw)))
    else:
        text = str(raw).strip().replace("%", "").strip()
        if not text:
            raise ValueError("请填写阅读进度")
        try:
            n = int(round(float(text)))
        except (TypeError, ValueError):
            raise ValueError("进度请填 0–100")
    if n < 0 or n > 100:
        raise ValueError("进度请填 0–100")
    return n


def _progress_is_custom(row) -> bool:
    try:
        return int(row["progress_custom"] or 0) == 1
    except (KeyError, IndexError, TypeError, ValueError):
        return False


def book_progress_public(row) -> dict:
    finish = str(row["finish_date"] or "").strip()[:10]
    if len(finish) < 10:
        finish = ""
    progress = row["progress"]
    return {
        "id": row["id"],
        "progress": progress,
        "progressCustom": _progress_is_custom(row),
        "finished": is_finished(progress),
        "finishDate": finish or None,
    }


def set_book_progress(book_id: str, raw, custom: bool = True) -> dict:
    """Write progress. custom=True locks it against WeRead/iReader sync."""
    book_id = str(book_id or "").strip()
    if not book_id:
        raise ValueError("找不到这本书")
    progress = parse_progress(raw)
    with connect() as conn:
        row = conn.execute("SELECT id FROM books WHERE id = ?", (book_id,)).fetchone()
        if not row:
            raise ValueError("找不到这本书")
        conn.execute(
            "UPDATE books SET progress = ?, progress_custom = ? WHERE id = ?",
            (progress, 1 if custom else 0, book_id),
        )
        row = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    return book_progress_public(row)


def set_book_finish_date(book_id: str, raw: str) -> dict:
    book_id = str(book_id or "").strip()
    if not book_id:
        raise ValueError("找不到这本书")
    finish_date = parse_finish_date(raw)
    with connect() as conn:
        row = conn.execute("SELECT id, progress FROM books WHERE id = ?", (book_id,)).fetchone()
        if not row:
            raise ValueError("找不到这本书")
        if not is_finished(row["progress"]):
            raise ValueError("阅读进度未到 100%")
        conn.execute(
            "UPDATE books SET finish_date = ?, finish_date_custom = 1, progress = 100 WHERE id = ?",
            (finish_date, book_id),
        )
        row = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    return {key: row[key] for key in row.keys()}


def clamp_month(year: int, month: int) -> tuple[int, int]:
    d = date(int(year), int(month), 1)
    if d < CAL_START:
        return CAL_START.year, CAL_START.month
    return d.year, d.month


def wallpaper_target() -> dict:
    raw = (get_setting("wallpaperMonth", "current") or "current").strip()
    today = date.today()
    key = raw.lower()
    if key in ("recent", "prev-recent"):
        # 壁纸周期与界面正在看的 recentOffset 无关。
        # 本周期 = 当前 30 天窗口（offset 0），上一周期 = 上一窗口（offset 1）。
        offset = 1 if key == "prev-recent" else 0
        offset = clamp_recent_offset(offset, today)
        _start, window_end = recent_window(today, offset=offset)
        year, month = clamp_month(window_end.year, window_end.month)
        return {
            "mode": key,
            "year": year,
            "month": month,
            "value": key,
            "offset": offset,
        }
    if key == "current":
        year, month = clamp_month(today.year, today.month)
        return {"mode": "current", "year": year, "month": month, "value": "current", "offset": 0}
    try:
        year_s, month_s = raw.split("-", 1)
        year, month = clamp_month(int(year_s), int(month_s))
        value = "%04d-%02d" % (year, month)
        return {"mode": "fixed", "year": year, "month": month, "value": value, "offset": 0}
    except Exception:
        year, month = clamp_month(today.year, today.month)
        return {"mode": "current", "year": year, "month": month, "value": "current", "offset": 0}


def set_wallpaper_month(value: str) -> dict:
    raw = (value or "current").strip().lower()
    if raw in ("current", "recent", "prev-recent"):
        set_setting("wallpaperMonth", raw)
        return wallpaper_target()
    year_s, month_s = raw.split("-", 1)
    year, month = clamp_month(int(year_s), int(month_s))
    set_setting("wallpaperMonth", "%04d-%02d" % (year, month))
    return wallpaper_target()


def upsert_book(book_id: str, title: str, source: str, bar_color: str | None = None) -> None:
    with connect() as conn:
        row = conn.execute("SELECT id, bar_color, title_color FROM books WHERE id = ?", (book_id,)).fetchone()
        if row:
            conn.execute(
                "UPDATE books SET title = CASE WHEN COALESCE(title_custom, 0) = 1 THEN title ELSE ? END, source = ? WHERE id = ?",
                (title, source, book_id),
            )
            return
        if bar_color:
            bar = bar_color
            title_col = default_title_color(bar)
        else:
            bar, title_col = new_book_colors(book_id)
        conn.execute(
            "INSERT INTO books(id, title, source, bar_color, title_color) VALUES(?, ?, ?, ?, ?)",
            (
                book_id,
                title,
                source,
                bar,
                title_col,
            ),
        )


def replace_source_range(source: str, start: date, end: date, rows: list[dict]) -> int:
    start_iso = start.isoformat()
    end_iso = end.isoformat()
    with connect() as conn:
        conn.execute(
            """
            DELETE FROM entries
            WHERE source = ? AND day >= ? AND day <= ?
              AND COALESCE(seconds_custom, 0) = 0
            """,
            (source, start_iso, end_iso),
        )
        count = 0
        for row in rows:
            book_id = str(row["id"])
            title = str(row["title"] or "").strip()
            day = str(row["day"])
            seconds = int(row.get("seconds") or 0)
            if not book_id or not title or seconds <= 0:
                continue
            existing = conn.execute("SELECT id FROM books WHERE id = ?", (book_id,)).fetchone()
            if existing:
                conn.execute(
                    "UPDATE books SET title = CASE WHEN COALESCE(title_custom, 0) = 1 THEN title ELSE ? END WHERE id = ?",
                    (title, book_id),
                )
            else:
                bar, title_col = new_book_colors(book_id)
                conn.execute(
                    "INSERT INTO books(id, title, source, bar_color, title_color) VALUES(?, ?, ?, ?, ?)",
                    (book_id, title, source, bar, title_col),
                )
            if _day_blocked(conn, book_id, day):
                continue
            conn.execute(
                """
                INSERT INTO entries(book_id, day, seconds, source) VALUES(?, ?, ?, ?)
                ON CONFLICT(book_id, day, source) DO UPDATE SET
                    seconds = CASE WHEN COALESCE(entries.seconds_custom, 0) = 1
                              THEN entries.seconds ELSE excluded.seconds END
                """,
                (book_id, day, seconds, source),
            )
            count += 1
        return count


def add_entry(day: str, title: str, minutes: float, source: str) -> dict:
    title = str(title or "").strip()
    if not title:
        raise ValueError("请填写书名")
    date.fromisoformat(day)
    seconds = max(1, int(round(float(minutes) * 60)))
    source = source if source in ("manual", "ireader") else "manual"
    slug = title.lower()
    book_id = "%s:%s" % (source, slug)
    with connect() as conn:
        # Attach to the shelf copy of the same title so a later WeRead sync
        # cannot leave the import on a second, empty book.
        found = conn.execute(
            """
            SELECT id, source FROM books
            WHERE lower(title) = ?
            ORDER BY CASE source WHEN 'weread' THEN 0 WHEN ? THEN 1 ELSE 2 END, id
            LIMIT 1
            """,
            (slug, source),
        ).fetchone()
        if found:
            book_id = found["id"]
            entry_source = found["source"] if found["source"] in ("weread", "ireader", "manual") else source
            conn.execute("UPDATE books SET title = CASE WHEN COALESCE(title_custom, 0) = 1 THEN title ELSE ? END WHERE id = ?", (title, book_id))
        else:
            # Missing titles become local manual books (same as 记录 → 添加书名).
            entry_source = source
            book_id = "manual:" + uuid.uuid4().hex[:10]
            bar, title_col = new_book_colors(book_id)
            conn.execute(
                "INSERT INTO books(id, title, source, bar_color, title_color, title_custom) VALUES(?, ?, ?, ?, ?, 1)",
                (book_id, title, "manual", bar, title_col),
            )
        # Lock the day. Adding onto an existing WeRead total keeps both (合并).
        conn.execute(
            """
            INSERT INTO entries(book_id, day, seconds, source, seconds_custom)
            VALUES(?, ?, ?, ?, 1)
            ON CONFLICT(book_id, day, source) DO UPDATE SET
                seconds = entries.seconds + excluded.seconds,
                seconds_custom = 1
            """,
            (book_id, day, seconds, entry_source),
        )
        last_open = int(datetime.fromisoformat(day).timestamp())
        conn.execute(
            "UPDATE books SET last_open = CASE WHEN COALESCE(last_open, 0) > ? THEN last_open ELSE ? END WHERE id = ?",
            (last_open, last_open, book_id),
        )
        row = conn.execute(
            "SELECT books.id, books.title, books.source, books.bar_color, books.title_color, entries.seconds "
            "FROM entries JOIN books ON books.id = entries.book_id "
            "WHERE entries.book_id = ? AND entries.day = ? AND entries.source = ?",
            (book_id, day, entry_source),
        ).fetchone()
    return dict(row)


def import_csv(text: str, source: str = "ireader") -> int:
    source = source if source in ("manual", "ireader") else "ireader"
    sample = text.lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(sample))
    if not reader.fieldnames:
        raise ValueError("CSV 需要表头：日期,书名,分钟")
    fields = {name.strip().lower(): name for name in reader.fieldnames if name}
    day_key = fields.get("日期") or fields.get("date") or fields.get("day")
    title_key = fields.get("书名") or fields.get("title") or fields.get("book")
    min_key = fields.get("分钟") or fields.get("minutes") or fields.get("mins") or fields.get("时长")
    if not day_key or not title_key or not min_key:
        raise ValueError("CSV 表头需要包含：日期, 书名, 分钟（或 date, title, minutes）")
    n = 0
    for row in reader:
        day_raw = str(row.get(day_key) or "").strip().replace("/", "-")
        title = str(row.get(title_key) or "").strip()
        minutes = str(row.get(min_key) or "").strip()
        if not day_raw or not title or not minutes:
            continue
        day_raw = day_raw[:10]
        date.fromisoformat(day_raw)
        add_entry(day_raw, title, float(minutes), source)
        n += 1
    return n


def cover_file_name(book_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(book_id or "").strip())
    if not safe or not COVER_FILE_RE.match(safe + ".png"):
        raise ValueError("找不到这本书")
    return safe + ".png"


def is_local_cover_url(url: str) -> bool:
    path = str(url or "").strip().split("?", 1)[0]
    return path.startswith("/api/covers/") and bool(COVER_FILE_RE.match(path.rsplit("/", 1)[-1]))


def set_book_cover(book_id: str, cover: str, custom: bool = True) -> dict:
    cover = str(cover or "").strip()
    if not book_id:
        raise ValueError("找不到这本书")
    if not cover:
        raise ValueError("请上传封面")
    with connect() as conn:
        row = conn.execute("SELECT id FROM books WHERE id = ?", (book_id,)).fetchone()
        if not row:
            raise ValueError("找不到这本书")
        conn.execute(
            "UPDATE books SET cover = ?, cover_custom = ? WHERE id = ?",
            (cover, 1 if custom else 0, book_id),
        )
        row = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    return dict(row)


def _png_pixel_size(data: bytes) -> tuple[int, int] | None:
    raw = bytes(data or b"")
    if len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    try:
        width, height = struct.unpack(">II", raw[16:24])
    except Exception:
        return None
    return int(width), int(height)


def save_custom_cover_png(book_id: str, png_bytes: bytes) -> dict:
    """Persist a cropped PNG under ~/.reading-log/covers and point books.cover at it."""
    if not book_id:
        raise ValueError("找不到这本书")
    data = bytes(png_bytes or b"")
    if len(data) < 32 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("不是 PNG 图片")
    if len(data) > 8 * 1024 * 1024:
        raise ValueError("图片太大")
    # Guard against accidental 3×4 / thumbnail writes that look like a missing cover.
    size = _png_pixel_size(data)
    if not size or size[0] < 48 or size[1] < 64 or len(data) < 1024:
        raise ValueError("封面图片无效或过小")
    with connect() as conn:
        row = conn.execute("SELECT id FROM books WHERE id = ?", (book_id,)).fetchone()
        if not row:
            raise ValueError("找不到这本书")
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    name = cover_file_name(book_id)
    dest = COVERS_DIR / name
    dest.write_bytes(data)
    stamp = int(datetime.now().timestamp())
    cover_url = "/api/covers/%s?v=%s" % (name, stamp)
    return set_book_cover(book_id, cover_url, custom=True)


def set_book_colors(book_id: str, bar_color: str | None = None, title_color: str | None = None) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
        if not row:
            raise ValueError("找不到这本书")
        bar = bar_color if bar_color else row["bar_color"]
        title = title_color if title_color else row["title_color"]
        if bar_color == "default":
            bar = default_bar_color(book_id)
        elif bar_color:
            bar = bar_color_for(book_id, bar_color)
        if title_color == "default":
            title = default_title_color(bar)
        else:
            title = title_color_for(title, bar)
        conn.execute("UPDATE books SET bar_color = ?, title_color = ? WHERE id = ?", (bar, title, book_id))
        row = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    return dict(row)


def _remember_deleted_day(conn, book_id: str, day: str) -> None:
    book_id = str(book_id or "").strip()
    day = str(day or "").strip()[:10]
    if not book_id or not day:
        return
    conn.execute(
        "INSERT INTO entry_tombstones(book_id, day) VALUES(?, ?) ON CONFLICT(book_id, day) DO NOTHING",
        (book_id, day),
    )


def _forget_deleted_day(conn, book_id: str, day: str) -> None:
    conn.execute(
        "DELETE FROM entry_tombstones WHERE book_id = ? AND day = ?",
        (str(book_id or "").strip(), str(day or "").strip()[:10]),
    )


def delete_entry(book_id: str, day: str) -> None:
    book_id = str(book_id or "").strip()
    day = str(day or "").strip()[:10]
    if not book_id or not day:
        return
    with connect() as conn:
        _remember_deleted_day(conn, book_id, day)
        conn.execute("DELETE FROM entries WHERE book_id = ? AND day = ?", (book_id, day))


def excluded_book_ids() -> set[str]:
    raw = get_setting("excludedBooks", "")
    try:
        obj = json.loads(raw) if raw else []
    except Exception:
        obj = []
    if not isinstance(obj, list):
        return set()
    return {str(x).strip() for x in obj if str(x).strip()}


def exclude_book(book_id: str) -> None:
    book_id = str(book_id or "").strip()
    if not book_id:
        return
    ids = excluded_book_ids()
    if book_id in ids:
        return
    ids.add(book_id)
    set_setting("excludedBooks", json.dumps(sorted(ids), ensure_ascii=False))


def is_excluded_book(book_id: str) -> bool:
    return str(book_id or "").strip() in excluded_book_ids()


def book_visibility_map() -> dict[str, dict]:
    """Books hidden from 月历 / 卡片 / 票根. Missing ids stay visible in all three."""
    raw = get_setting("bookVisibility", "")
    try:
        obj = json.loads(raw) if raw else {}
    except Exception:
        obj = {}
    if not isinstance(obj, dict):
        return {}
    out: dict[str, dict] = {}
    for key, flags in obj.items():
        bid = str(key or "").strip()
        if not bid or not isinstance(flags, dict):
            continue
        cal = bool(flags.get("cal", True))
        card = bool(flags.get("card", True))
        ticket = bool(flags.get("ticket", True))
        if cal and card and ticket:
            continue
        out[bid] = {"cal": cal, "card": card, "ticket": ticket}
    return out


def hidden_book_ids(mode: str) -> set[str]:
    m = str(mode or "")
    if m == "card":
        key = "card"
    elif m == "ticket":
        key = "ticket"
    else:
        key = "cal"
    return {bid for bid, flags in book_visibility_map().items() if not flags.get(key, True)}


def set_book_visibility(body) -> dict[str, dict]:
    incoming = body.get("books") if isinstance(body, dict) and isinstance(body.get("books"), dict) else body
    if not isinstance(incoming, dict):
        incoming = {}
    current = book_visibility_map()
    for key, flags in incoming.items():
        bid = str(key or "").strip()
        if not bid or bid == "books" or not isinstance(flags, dict):
            continue
        cal = bool(flags.get("cal", True))
        card = bool(flags.get("card", True))
        ticket = bool(flags.get("ticket", True))
        if cal and card and ticket:
            current.pop(bid, None)
        else:
            current[bid] = {"cal": cal, "card": card, "ticket": ticket}
    set_setting("bookVisibility", json.dumps(current, ensure_ascii=False))
    return current


# Canonical ← aliases. Aliases stay excluded from the UI; their daily seconds are
# folded into the canonical book on each WeRead sync (day-set union; same-day sum).
# WeRead only exposes per-day totals (no session start/end), so same-day “interval
# union” is approximated by summing edition totals (equals true union when the
# reading blocks do not overlap).
# 全知读者视角 01 is the shelf row everything else folds into.
_ORV_CANONICAL = "weread:CB_E216NB6MrC2B76d75A67eAcP"
_ORV_VOLUMES = [
    _ORV_CANONICAL,
    "weread:CB_Dtn6ND6MrC2B76d75A9Iu91k",  # 02
    "weread:CB_Epd6NF6MrC2B76d75ADB1BwU",  # 03
    "weread:CB_EhP6NH6MrC2B76d75A9E809d",  # 04
    "weread:CB_EZB6NJ6MrC2B76d75AEYI3mY",  # 05
    "weread:CB_EQx6NL6MrC2B76d75AFnjGAO",  # 06
    "weread:CB_B6X6NN6MrC2B76d75A7oTD6g",  # 07
    "weread:CB_AyJ6NP6MrC2B76d75ADtZH1T",  # 08
    "weread:CB_Aq56NR6MrC2B76d75A0gL4SY",  # 09
    "weread:CB_Ahr6NT6MrC2B76d75AFAF3Qc",  # 10
    "weread:CB_Bdh6NV6MrC2B76d75AFSMGZq",  # 11
    "weread:CB_BVT6NX6MrC2B76d75AAoZFqi",  # 12
]
# 磨铁出版三部（杨可意译；ISBN 出版社为国际文化出版公司 / 国文润华）。
# 正文到原作 E34（约自导入版第四部 86%）。时长并入正本，不计入总字数。
# 最后打开时间必须计入。这三本是隐藏别名，同步要单独刷新它们的 last_open。
_ORV_YANG = [
    "weread:3300052619",  # 全知读者视角
    "weread:3300101329",  # 全知读者视角.2
    "weread:3300138805",  # 全知读者视角.3
]

_DEFAULT_BOOK_MERGES = {
    # 鋼琴教師（繁体潘勋）← 钢琴教师_简体（潘勋）+ 钢琴教师（宁瑛）
    "weread:CB_64i24y27dDdf77175ACIS9b7": [
        "weread:CB_6Cw24w27dDdf77175AC71DBW",
        "weread:3300069473",
    ],
    # 全知读者视角 ← 自导入 02–12 + 磨铁三部（时长累计；字数仍只算自导入 12 部）
    _ORV_CANONICAL: _ORV_VOLUMES[1:] + list(_ORV_YANG),
}

# Series whose word count is the sum of members.
# Progress is words-read / that total.
# progressLead (磨铁/杨可意) covers early story only; while incomplete, progress
# uses lead words-read / member total. Once lead is finished, map onto members:
# afterLeadFloors[i] floors member i's % (Motie ends at E34 ≈ 自导入 vol4 86%),
# then later volumes use their actual WeRead progress. Never max(lead, members)
# and never drop to raw 自导入 % alone (that cliffs ~3% after Motie 100%).
_DEFAULT_BOOK_SERIES = {
    _ORV_CANONICAL: {
        "title": "全知读者视角",
        "members": list(_ORV_VOLUMES),
        "progressLead": list(_ORV_YANG),
        # Motie finished ⇒ 自导入 1–3 视为读完，部4 至少 86%；其后按实际进度。
        "afterLeadFloors": [100, 100, 100, 86],
    },
}


def book_merges() -> dict[str, list[str]]:
    """Return {canonical_book_id: [alias_book_id, ...]}."""
    raw = get_setting("bookMerges", "")
    try:
        obj = json.loads(raw) if raw else {}
    except Exception:
        obj = {}
    if not isinstance(obj, dict) or not obj:
        obj = dict(_DEFAULT_BOOK_MERGES)
    out: dict[str, list[str]] = {}
    for canon, aliases in obj.items():
        c = str(canon or "").strip()
        if not c:
            continue
        al = []
        if isinstance(aliases, list):
            for a in aliases:
                s = str(a or "").strip()
                if s and s != c and s not in al:
                    al.append(s)
        out[c] = al
    return out


def ensure_book_merges() -> dict[str, list[str]]:
    """Persist merges (including new defaults) and exclude aliases so they stay hidden."""
    merges = book_merges()
    changed = not get_setting("bookMerges", "")
    for canon, aliases in _DEFAULT_BOOK_MERGES.items():
        cur = merges.setdefault(canon, [])
        for alias in aliases:
            if alias != canon and alias not in cur:
                cur.append(alias)
                changed = True
    if changed:
        set_setting("bookMerges", json.dumps(merges, ensure_ascii=False))
    for canon, aliases in merges.items():
        for alias in aliases:
            exclude_book(alias)
    return merges


def book_series() -> dict[str, dict]:
    """Return {canonical_book_id: {title, members, progressLead?, afterLeadFloors?}}."""
    out: dict[str, dict] = {}
    for canon, spec in _DEFAULT_BOOK_SERIES.items():
        item = {"title": spec["title"], "members": list(spec["members"])}
        lead = list(spec.get("progressLead") or [])
        if lead:
            item["progressLead"] = lead
        floors = list(spec.get("afterLeadFloors") or [])
        if floors:
            item["afterLeadFloors"] = [max(0, min(100, int(x))) for x in floors]
        out[canon] = item
    return out


def set_series_book_stats(book_id: str, title: str, word_count: int, progress: int) -> None:
    """Lock the combined title, word count, and progress so per-volume sync cannot split them."""
    book_id = str(book_id or "").strip()
    if not book_id:
        return
    word_count = max(0, int(word_count))
    progress = max(0, min(100, int(progress)))
    title = str(title or "").strip()
    with connect() as conn:
        conn.execute(
            """
            UPDATE books
            SET title = CASE WHEN ? != '' THEN ? ELSE title END,
                title_custom = CASE WHEN ? != '' THEN 1 ELSE title_custom END,
                word_count = ?,
                progress = ?,
                progress_custom = 1
            WHERE id = ?
            """,
            (title, title, title, word_count, progress, book_id),
        )


def merge_alias_ids() -> set[str]:
    ids: set[str] = set()
    for aliases in book_merges().values():
        ids.update(aliases)
    return ids


def is_merge_alias(book_id: str) -> bool:
    return str(book_id or "").strip() in merge_alias_ids()


def is_merge_canonical(book_id: str) -> bool:
    return str(book_id or "").strip() in book_merges()


def _positive_stamp(value) -> int:
    """Unix seconds, or 0 when missing. Millisecond stamps are scaled down."""
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        return 0
    if n > 10**12:
        n //= 1000
    return n if n > 0 else 0


def merged_member_ids(canonical_id: str) -> list[str]:
    """Canonical id plus aliases. Empty when this book is not a merge target."""
    canonical_id = str(canonical_id or "").strip()
    if not canonical_id:
        return []
    aliases = book_merges().get(canonical_id)
    if aliases is None:
        return []
    ids = [canonical_id]
    for alias in aliases:
        s = str(alias or "").strip()
        if s and s not in ids:
            ids.append(s)
    return ids


def _max_open_stamp(conn, book_ids: list[str], incoming=None) -> int | None:
    """Latest positive last_open among ids and an optional incoming stamp.

    Zero and missing values are ignored. None means every candidate is missing,
    so callers must leave the stored canonical last_open unchanged.
    """
    stamps: list[int] = []
    inc = _positive_stamp(incoming)
    if inc:
        stamps.append(inc)
    for bid in book_ids:
        row = conn.execute("SELECT last_open FROM books WHERE id = ?", (bid,)).fetchone()
        if not row:
            continue
        n = _positive_stamp(row["last_open"])
        if n:
            stamps.append(n)
    return max(stamps) if stamps else None


def set_last_open_if_newer(book_id: str, stamp: int) -> None:
    """Raise one book's last_open. Hidden merge aliases are updated too.

    Does not create a row, and does not touch minutes, progress, or word count.
    """
    book_id = str(book_id or "").strip()
    stamp = _positive_stamp(stamp)
    if not book_id or not stamp:
        return
    with connect() as conn:
        conn.execute(
            """
            UPDATE books
            SET last_open = ?
            WHERE id = ? AND COALESCE(last_open, 0) < ?
            """,
            (stamp, book_id, stamp),
        )


def _stamp_shanghai_day(ts: int) -> str:
    """YYYY-MM-DD in Asia/Shanghai for a unix stamp (seconds or ms)."""
    n = int(ts or 0)
    if n <= 0:
        return ""
    if n > 10**12:
        n = n // 1000
    return datetime.fromtimestamp(n, TZ).date().isoformat()


def _day_to_stamp(day: str) -> int:
    """Midnight local stamp for an ISO day (matches existing manual/import writes)."""
    return int(datetime.fromisoformat(str(day)[:10]).timestamp())


def effective_last_open(stored_last_open, last_day: str | None) -> int:
    """Prefer the latest day with real reading entries over a shelf updateTime.

    WeRead often bumps updateTime / readUpdateTime when a book is merely
    opened or touched, with no new reading seconds. Card「日期」and 最近打开
    sort should follow the last day that actually has entries.
    """
    day = str(last_day or "").strip()[:10]
    if day:
        try:
            date.fromisoformat(day)
        except ValueError:
            day = ""
    stored = int(stored_last_open or 0)
    if not day:
        return stored if stored > 0 else 0
    if stored > 0:
        try:
            if _stamp_shanghai_day(stored) == day:
                return stored
        except (OSError, OverflowError, ValueError):
            pass
    try:
        return _day_to_stamp(day)
    except ValueError:
        return stored if stored > 0 else 0


def latest_entry_day(book_id: str, conn=None) -> str:
    """Latest YYYY-MM-DD with seconds > 0 for this book, or ''."""
    book_id = str(book_id or "").strip()
    if not book_id:
        return ""

    def _query(c) -> str:
        row = c.execute(
            """
            SELECT MAX(day) AS last_day FROM entries
            WHERE book_id = ? AND COALESCE(seconds, 0) > 0
            """,
            (book_id,),
        ).fetchone()
        return str((row["last_day"] if row else None) or "")[:10]

    if conn is not None:
        return _query(conn)
    with connect() as c:
        return _query(c)


def reconcile_last_open_from_entries(book_id: str) -> int | None:
    """Write last_open from the latest reading-entry day when shelf time disagrees."""
    book_id = str(book_id or "").strip()
    if not book_id:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT last_open FROM books WHERE id = ?", (book_id,)
        ).fetchone()
        if not row:
            return None
        last_day = latest_entry_day(book_id, conn)
        if not last_day:
            return _positive_stamp(row["last_open"])
        stored = int(row["last_open"] or 0)
        stamp = effective_last_open(stored, last_day)
        if stamp and stamp != stored:
            conn.execute(
                "UPDATE books SET last_open = ? WHERE id = ?",
                (stamp, book_id),
            )
        return stamp or None


def reconcile_all_last_opens_from_entries() -> int:
    """Clamp every book whose last_open day has no reading entries. Returns fixes."""
    fixed = 0
    with connect() as conn:
        rows = conn.execute("SELECT id, last_open FROM books").fetchall()
        for row in rows:
            book_id = row["id"]
            last_day = latest_entry_day(book_id, conn)
            if not last_day:
                continue
            stored = int(row["last_open"] or 0)
            stamp = effective_last_open(stored, last_day)
            if stamp and stamp != stored:
                conn.execute(
                    "UPDATE books SET last_open = ? WHERE id = ?",
                    (stamp, book_id),
                )
                fixed += 1
    return fixed


def apply_merged_last_open(canonical_id: str) -> int | None:
    """Point a merged book's last_open at the latest open among its volumes.

    Does not write 0, and does not touch finish dates, progress, or word count.
    After taking the shelf max, clamp to the canonical's latest reading-entry day
    so a volume's updateTime bump without seconds cannot float the series card.
    """
    ids = merged_member_ids(canonical_id)
    if not ids:
        return None
    with connect() as conn:
        stamp = _max_open_stamp(conn, ids)
        if not stamp:
            return None
        conn.execute(
            "UPDATE books SET last_open = ? WHERE id = ?",
            (stamp, ids[0]),
        )
    return reconcile_last_open_from_entries(ids[0])


def _finish_day(raw) -> str:
    day = str(raw or "").strip()[:10]
    if len(day) < 10 or day[4] != "-" or day[7] != "-":
        return ""
    try:
        date.fromisoformat(day)
    except ValueError:
        return ""
    return day


def note_merged_finish_date(book_id: str, raw: str) -> None:
    """Keep one volume's finish date if the incoming day is later.

    Does not change progress or finish_date_custom. An unfinished series
    canonical stays blank: a finish date there is treated as read-through
    for word estimates, and the card would look finished. A single volume
    being finished does not supply a finish date for the series card.
    """
    book_id = str(book_id or "").strip()
    day = _finish_day(raw)
    if not book_id or not day:
        return
    series_canonical = book_id in book_series()
    with connect() as conn:
        row = conn.execute(
            "SELECT finish_date, finish_date_custom, progress FROM books WHERE id = ?",
            (book_id,),
        ).fetchone()
        if not row:
            return
        try:
            custom = int(row["finish_date_custom"] or 0) == 1
        except (TypeError, ValueError):
            custom = False
        stored = _finish_day(row["finish_date"])
        if custom and stored:
            return
        if series_canonical and not is_finished(row["progress"]):
            return
        if stored and stored >= day:
            return
        conn.execute("UPDATE books SET finish_date = ? WHERE id = ?", (day, book_id))


def replace_merged_book_days(book_id: str, source: str, rows: list[dict]) -> int:
    """Write merged daily rows. User-locked (seconds_custom) days are left untouched."""
    book_id = str(book_id or "").strip()
    source = str(source or "").strip() or "weread"
    if not book_id:
        return 0
    with connect() as conn:
        conn.execute(
            """
            DELETE FROM entries
            WHERE book_id = ? AND source = ? AND COALESCE(seconds_custom, 0) = 0
            """,
            (book_id, source),
        )
        count = 0
        for row in rows:
            day = str(row.get("day") or "")
            seconds = int(row.get("seconds") or 0)
            if not day or seconds <= 0:
                continue
            if _day_is_custom(conn, book_id, day):
                continue
            if _day_is_tombstoned(conn, book_id, day):
                continue
            conn.execute(
                """
                INSERT INTO entries(book_id, day, seconds, source, seconds_custom)
                VALUES(?, ?, ?, ?, 0)
                ON CONFLICT(book_id, day, source) DO UPDATE SET
                    seconds = CASE WHEN COALESCE(entries.seconds_custom, 0) = 1
                              THEN entries.seconds ELSE excluded.seconds END,
                    seconds_custom = entries.seconds_custom
                """,
                (book_id, day, seconds, source),
            )
            count += 1
        return count


def set_custom_day_seconds(book_id: str, day: str, seconds: int, source: str = "weread") -> None:
    """Lock one day to an explicit duration (survives sync / alias merge)."""
    book_id = str(book_id or "").strip()
    day = str(day or "").strip()
    source = str(source or "").strip() or "weread"
    seconds = max(0, int(seconds))
    if not book_id or not day:
        return
    with connect() as conn:
        conn.execute(
            "DELETE FROM entry_tombstones WHERE book_id = ? AND day = ?",
            (book_id, day),
        )
        if seconds <= 0:
            conn.execute(
                "DELETE FROM entries WHERE book_id = ? AND day = ? AND source = ?",
                (book_id, day, source),
            )
            return
        conn.execute(
            """
            INSERT INTO entries(book_id, day, seconds, source, seconds_custom)
            VALUES(?, ?, ?, ?, 1)
            ON CONFLICT(book_id, day, source) DO UPDATE SET
                seconds = excluded.seconds,
                seconds_custom = 1
            """,
            (book_id, day, seconds, source),
        )


def delete_book(book_id: str) -> bool:
    book_id = str(book_id or "").strip()
    if not book_id:
        return False
    with connect() as conn:
        row = conn.execute("SELECT id FROM books WHERE id = ?", (book_id,)).fetchone()
        if not row:
            exclude_book(book_id)
            return False
        conn.execute("DELETE FROM entries WHERE book_id = ?", (book_id,))
        conn.execute("DELETE FROM books WHERE id = ?", (book_id,))
    exclude_book(book_id)
    return True


def delete_books_named(title: str) -> list[str]:
    title = str(title or "").strip()
    if not title:
        return []
    with connect() as conn:
        rows = conn.execute("SELECT id FROM books WHERE title = ?", (title,)).fetchall()
    deleted = []
    for row in rows:
        book_id = row["id"]
        if delete_book(book_id):
            deleted.append(book_id)
    return deleted


def month_grid(year: int, month: int) -> list[date]:
    """Month calendar days padded to Monday–Sunday weeks."""
    first = date(year, month, 1)
    # date.weekday(): Mon=0 … Sun=6 → Monday-first column index.
    lead = first.weekday()
    start = first - timedelta(days=lead)
    last_day = monthrange(year, month)[1]
    last = date(year, month, last_day)
    trail = 6 - last.weekday()
    end = last + timedelta(days=trail)
    days = []
    cur = start
    while cur <= end:
        days.append(cur)
        cur += timedelta(days=1)
    return days


RECENT_DAYS = 30
# How many not-yet-started future periods the planner may show (offset -1, -2, …).
RECENT_FUTURE_PERIODS = 2


def recent_max_offset(today: date | None = None) -> int:
    today = today or date.today()
    if today < CAL_START:
        return 0
    return (today - CAL_START).days // RECENT_DAYS


def recent_min_offset(today: date | None = None) -> int:
    """Most-negative offset = furthest future period placeholder."""
    return -int(RECENT_FUTURE_PERIODS)


def clamp_recent_offset(offset, today: date | None = None) -> int:
    today = today or date.today()
    try:
        n = int(offset)
    except (TypeError, ValueError):
        n = 0
    min_off = recent_min_offset(today)
    max_off = recent_max_offset(today)
    if n < min_off:
        n = min_off
    if n > max_off:
        n = max_off
    return n


def recent_offset() -> int:
    raw = get_setting("recentOffset", "")
    if raw == "":
        try:
            obj = json.loads(get_setting("lastView", "") or "{}")
        except Exception:
            obj = {}
        if isinstance(obj, dict) and obj.get("recentOffset") is not None:
            raw = obj.get("recentOffset")
    return clamp_recent_offset(raw)


def set_recent_offset(offset) -> int:
    n = clamp_recent_offset(offset)
    set_setting("recentOffset", str(n))
    return n


def books_scroll() -> int:
    try:
        return max(0, int(get_setting("booksScroll", "0") or 0))
    except (TypeError, ValueError):
        return 0


def set_books_scroll(n) -> int:
    try:
        n = int(float(n))
    except (TypeError, ValueError):
        n = 0
    n = max(0, min(n, 5000000))
    set_setting("booksScroll", str(n))
    return n


def parse_recent_end(value) -> date | None:
    if isinstance(value, date):
        return value
    raw = str(value or "").strip()[:10]
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except Exception:
        return None


def recent_window(
    today: date | None = None,
    offset: int | None = None,
    end: date | None = None,
) -> tuple[date, date]:
    today = today or date.today()
    end_date = end if isinstance(end, date) else None
    if offset is None and end_date is not None:
        if end_date > today:
            end_date = today
        if end_date < CAL_START:
            end_date = CAL_START
        start = end_date - timedelta(days=RECENT_DAYS - 1)
        if start < CAL_START:
            start = CAL_START
        if end_date < start:
            end_date = start
        return start, end_date
    if offset is None:
        offset = recent_offset()
    offset = clamp_recent_offset(offset, today)
    window_end = today - timedelta(days=RECENT_DAYS * offset)
    window_start = window_end - timedelta(days=RECENT_DAYS - 1)
    if window_start < CAL_START:
        window_start = CAL_START
    if window_end < CAL_START:
        window_end = CAL_START
    if window_end < window_start:
        window_end = window_start
    return window_start, window_end


def recent_grid(
    today: date | None = None,
    offset: int | None = None,
    end: date | None = None,
) -> list[date]:
    start_in, end = recent_window(today, offset=offset, end=end)
    # Pad to Monday–Sunday weeks (date.weekday(): Mon=0 … Sun=6).
    lead = start_in.weekday()
    start = start_in - timedelta(days=lead)
    trail = 6 - end.weekday()
    last = end + timedelta(days=trail)
    days = []
    cur = start
    while cur <= last:
        days.append(cur)
        cur += timedelta(days=1)
    return days


def _planner_payload(days: list[date], in_pred, avg_div: int, extra: dict) -> dict:
    start = days[0].isoformat()
    end = days[-1].isoformat()
    threshold = min_minutes() * 60
    book_total_threshold = min_book_total_minutes() * 60
    with connect() as conn:
        books = {row["id"]: dict(row) for row in conn.execute("SELECT * FROM books")}
        rows = conn.execute(
            """
            SELECT book_id, day, SUM(seconds) AS seconds,
                   MAX(COALESCE(seconds_custom, 0)) AS seconds_custom
            FROM entries
            WHERE day >= ? AND day <= ?
            GROUP BY book_id, day
            """,
            (start, end),
        ).fetchall()
        skip_books = short_lifetime_book_ids(conn, book_total_threshold)
    hidden_cal = hidden_book_ids("cal")
    by_day: dict[str, list] = {}
    for row in rows:
        seconds = int(row["seconds"] or 0)
        locked = int(row["seconds_custom"] or 0) == 1
        if seconds < threshold and not locked:
            continue
        if row["book_id"] in skip_books or row["book_id"] in hidden_cal:
            continue
        book = books.get(row["book_id"]) or {}
        by_day.setdefault(row["day"], []).append(
            {
                "id": row["book_id"],
                "title": book.get("title") or "",
                "seconds": seconds,
                "source": book.get("source") or "",
                "barColor": bar_color_for(row["book_id"], book.get("bar_color")),
                "titleColor": title_color_for(book.get("title_color"), book.get("bar_color")),
                "lastOpen": int(book.get("last_open") or 0),
            }
        )
    period_secs: dict[str, int] = {}
    cells = []
    total_seconds = 0
    for d in days:
        iso = d.isoformat()
        books_today = sorted(
            by_day.get(iso, []),
            key=lambda b: (-int(b["seconds"]), b["title"]),
        )
        shown = books_today[:MAX_BOOKS_PER_DAY]
        total = sum(int(b["seconds"]) for b in books_today)
        inside = bool(in_pred(d))
        if inside:
            total_seconds += total
            for item in books_today:
                period_secs[item["id"]] = period_secs.get(item["id"], 0) + int(item["seconds"])
        cells.append(
            {
                "date": iso,
                "day": d.day,
                "inMonth": inside,
                "week": d.isocalendar()[1],
                "seconds": total,
                "books": shown,
            }
        )
    records = []
    for book_id, seconds in period_secs.items():
        book = books.get(book_id) or {}
        records.append(
            {
                "id": book_id,
                "title": book.get("title") or "",
                "seconds": seconds,
                "finished": is_finished(book.get("progress")),
                "barColor": bar_color_for(book_id, book.get("bar_color")),
            }
        )
    records.sort(key=lambda r: (-int(r["seconds"]), r["title"]))
    total_minutes = int(total_seconds) // 60
    avg_minutes = int(round(total_minutes / max(1, int(avg_div))))
    total_words = 0.0
    with connect() as conn:
        for book_id, seconds in period_secs.items():
            rate = _book_words_per_second(conn, book_id, books.get(book_id) or {})
            if rate is None or seconds <= 0:
                continue
            total_words += rate * seconds
    extra.update(
        {
            "minMinutes": min_minutes(),
            "minBookTotalMinutes": min_book_total_minutes(),
            "days": cells,
            "records": records,
            "totalMinutes": total_minutes,
            "avgMinutes": avg_minutes,
            "totalWords": int(round(total_words)),
        }
    )
    return extra


def month_elapsed_days(year: int, month: int, today: date | None = None) -> int:
    """Days of this month that have already happened, including today.

    A past month counts every day. A future month has none.
    """
    today = today or date.today()
    length = monthrange(year, month)[1]
    if (year, month) < (today.year, today.month):
        return length
    if (year, month) > (today.year, today.month):
        return 0
    return min(length, today.day)


def month_payload(year: int, month: int) -> dict:
    days = month_grid(year, month)
    return _planner_payload(
        days,
        lambda d, m=month: d.month == m,
        month_elapsed_days(year, month),
        {
            "kind": "month",
            "year": year,
            "month": month,
            "sideNum": month,
            "sideMonth": month,
            "sideYear": year,
        },
    )


def recent_payload(
    today: date | None = None,
    offset: int | None = None,
    end: date | str | None = None,
) -> dict:
    today = today or date.today()
    end_date = parse_recent_end(end) if not isinstance(end, date) else end
    used_offset = offset
    if used_offset is not None:
        used_offset = clamp_recent_offset(used_offset, today)
        end_date = None
    start_in, window_end = recent_window(today, offset=used_offset, end=end_date)
    days = recent_grid(today, offset=used_offset, end=end_date)
    if used_offset is None:
        used_offset = clamp_recent_offset((today - window_end).days // RECENT_DAYS, today)
    span = (window_end - start_in).days + 1
    return _planner_payload(
        days,
        lambda d, a=start_in, b=window_end: a <= d <= b,
        span,
        {
            "kind": "recent",
            "year": window_end.year,
            "month": window_end.month,
            "sideNum": window_end.day,
            "sideMonth": window_end.month,
            "sideYear": window_end.year,
            "from": start_in.isoformat(),
            "to": window_end.isoformat(),
            "offset": used_offset,
            "maxOffset": recent_max_offset(today),
            "minOffset": recent_min_offset(today),
        },
    )


def range_grid(start_in: date, end: date) -> list[date]:
    """Date range padded to Monday–Sunday weeks."""
    lead = start_in.weekday()
    start = start_in - timedelta(days=lead)
    trail = 6 - end.weekday()
    last = end + timedelta(days=trail)
    days = []
    cur = start
    while cur <= last:
        days.append(cur)
        cur += timedelta(days=1)
    return days


def range_payload(start: date | str, end: date | str) -> dict:
    """Custom date-range planner payload in recent (period) style."""
    start_in = parse_recent_end(start) if not isinstance(start, date) else start
    window_end = parse_recent_end(end) if not isinstance(end, date) else end
    if start_in is None or window_end is None:
        raise ValueError("起止日期无效")
    if window_end < start_in:
        raise ValueError("开始日期不能晚于结束日期")
    if start_in < CAL_START:
        start_in = CAL_START
    if window_end < CAL_START:
        window_end = CAL_START
    if window_end < start_in:
        window_end = start_in
    # Cap extreme ranges so PNG export stays manageable (~2 years of weeks).
    max_span = 366 * 2
    if (window_end - start_in).days + 1 > max_span:
        raise ValueError("日期范围过长（最多约两年）")
    days = range_grid(start_in, window_end)
    span = (window_end - start_in).days + 1
    return _planner_payload(
        days,
        lambda d, a=start_in, b=window_end: a <= d <= b,
        span,
        {
            "kind": "recent",
            "year": window_end.year,
            "month": window_end.month,
            "sideNum": window_end.day,
            "sideMonth": window_end.month,
            "sideYear": window_end.year,
            "from": start_in.isoformat(),
            "to": window_end.isoformat(),
            "offset": 0,
            "maxOffset": recent_max_offset(),
        },
    )


def upsert_book_full(book_id: str, title: str, source: str, meta: dict | None = None) -> None:
    meta = meta or {}
    if is_excluded_book(book_id):
        return
    # Merged shelf rows must not keep only this volume's open time.
    member_ids = merged_member_ids(book_id)
    with connect() as conn:
        row = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
        cover = meta.get("cover")
        progress = meta.get("progress")
        word_count = meta.get("wordCount")
        last_open = meta.get("lastOpen")
        if member_ids:
            last_open = _max_open_stamp(conn, member_ids, last_open)
        author = (meta.get("author") or "").strip() or None
        finish_date = str(meta.get("finishDate") or "").strip()[:10] or None
        isbn = str(meta.get("isbn") or "").strip() or None
        publisher = str(meta.get("publisher") or "").strip() or None
        translator = str(meta.get("translator") or "").strip() or None
        price = str(meta.get("price") or "").strip() or None
        publish_year = str(meta.get("publishYear") or meta.get("publish_year") or "").strip()[:4] or None
        if publish_year and not publish_year.isdigit():
            publish_year = None
        if row:
            stored_finish = str(row["finish_date"] or "").strip()[:10]
            if stored_finish and len(stored_finish) < 10:
                stored_finish = ""
            try:
                custom_finish = int(row["finish_date_custom"] or 0) == 1
            except (KeyError, IndexError, TypeError, ValueError):
                custom_finish = False
            # A hand-set date stays. A custom flag with a blank date is not a choice,
            # so WeRead/iReader may fill it and the flag is cleared below.
            if custom_finish and not stored_finish:
                custom_finish = False
            # Custom / already-stored 读完日期 must not be overwritten by WeRead/iReader
            # (same idea as title_custom, and the 9.24 overwrite guard).
            # progress_custom keeps a hand-edited 阅读进度; sync skips it the same way.
            # cover_custom keeps a locally uploaded/cropped cover.
            if custom_finish or stored_finish:
                finish_date = stored_finish or None
            incoming_finish = None if (custom_finish or stored_finish) else finish_date
            # Series totals are written by set_series_book_stats. A single volume's
            # word count (e.g. 全知读者视角01 ≈ 20.3万) must not replace the sum.
            if book_id in book_series():
                word_count = None
                # One volume's finish date must not stamp an unfinished series.
                # That would mark it read-through. The sort reads member dates.
                if not is_finished(row["progress"]):
                    incoming_finish = None
            try:
                custom_author = int(row["author_custom"] or 0) == 1
            except (KeyError, IndexError, TypeError, ValueError):
                custom_author = False
            try:
                custom_words = int(row["word_count_custom"] or 0) == 1
            except (KeyError, IndexError, TypeError, ValueError):
                custom_words = False
            if custom_author:
                author = None
            if custom_words:
                word_count = None
            # WeRead/iReader sometimes send progress=100 with no finish date while the
            # cursor is still mid-book. Refuse to persist that bare 100 (keeps prior %).
            effective_finish = incoming_finish or stored_finish or None
            if progress is not None:
                try:
                    progress_n = int(progress)
                except (TypeError, ValueError):
                    progress_n = None
                if progress_n is not None and progress_n >= 100 and not effective_finish:
                    progress = None
            conn.execute(
                """
                UPDATE books SET title = CASE WHEN COALESCE(title_custom, 0) = 1 THEN title ELSE ? END, source = ?,
                    cover = CASE WHEN COALESCE(cover_custom, 0) = 1 THEN cover ELSE COALESCE(?, cover) END,
                    progress = CASE WHEN COALESCE(progress_custom, 0) = 1 THEN progress ELSE COALESCE(?, progress) END,
                    word_count = COALESCE(?, word_count),
                    last_open = COALESCE(?, last_open),
                    author = COALESCE(?, author),
                    finish_date = CASE
                        WHEN COALESCE(finish_date_custom, 0) = 1 AND TRIM(COALESCE(finish_date, '')) != '' THEN finish_date
                        ELSE COALESCE(?, finish_date) END,
                    finish_date_custom = CASE
                        WHEN COALESCE(finish_date_custom, 0) = 1 AND TRIM(COALESCE(finish_date, '')) = '' THEN 0
                        ELSE finish_date_custom END,
                    isbn = COALESCE(?, isbn),
                    publisher = COALESCE(?, publisher),
                    translator = COALESCE(?, translator),
                    price = COALESCE(?, price),
                    publish_year = COALESCE(?, publish_year)
                WHERE id = ?
                """,
                (
                    title or row["title"],
                    source,
                    cover or None,
                    progress if progress is not None else None,
                    word_count if word_count is not None else None,
                    last_open if last_open else None,
                    author,
                    incoming_finish,
                    isbn,
                    publisher,
                    translator,
                    price,
                    publish_year,
                    book_id,
                ),
            )
            return
        # New book only: settings-driven bar/title (cover channels when configured).
        bar, title_col = new_book_colors(book_id, cover)
        insert_progress = progress
        if insert_progress is not None:
            try:
                insert_n = int(insert_progress)
            except (TypeError, ValueError):
                insert_n = None
            if insert_n is not None and insert_n >= 100 and not finish_date:
                insert_progress = None
        conn.execute(
            """
            INSERT INTO books(id, title, source, bar_color, title_color, cover, progress, word_count,
                last_open, author, finish_date, isbn, publisher, translator, price, publish_year)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                book_id,
                title,
                source,
                bar,
                title_col,
                cover or "",
                insert_progress,
                word_count,
                last_open or 0,
                author or "",
                finish_date or "",
                isbn or "",
                publisher or "",
                translator or "",
                price or "",
                publish_year or "",
            ),
        )


def _day_is_custom(conn, book_id: str, day: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM entries
        WHERE book_id = ? AND day = ?
          AND (COALESCE(seconds_custom, 0) = 1 OR source = 'manual')
        LIMIT 1
        """,
        (book_id, day),
    ).fetchone()
    return row is not None


def book_has_manual_days(book_id: str) -> bool:
    """True if the user edited/split any day for this book (or added manual rows)."""
    with connect() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM entries
            WHERE book_id = ?
              AND (COALESCE(seconds_custom, 0) = 1 OR source = 'manual')
            LIMIT 1
            """,
            (book_id,),
        ).fetchone()
        return row is not None


def _day_is_tombstoned(conn, book_id: str, day: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM entry_tombstones WHERE book_id = ? AND day = ?",
        (book_id, day),
    ).fetchone()
    return row is not None


def _day_blocked(conn, book_id: str, day: str) -> bool:
    return _day_is_custom(conn, book_id, day) or _day_is_tombstoned(conn, book_id, day)


def replace_book_days(book_id: str, source: str, rows: list[dict]) -> int:
    """Refresh synced days for one book. User-edited rows (seconds_custom) stay."""
    with connect() as conn:
        conn.execute(
            """
            DELETE FROM entries
            WHERE book_id = ? AND source = ? AND COALESCE(seconds_custom, 0) = 0
            """,
            (book_id, source),
        )
        count = 0
        for row in rows:
            day = str(row.get("day") or "")
            seconds = int(row.get("seconds") or 0)
            if not day or seconds <= 0:
                continue
            if _day_blocked(conn, book_id, day):
                continue
            conn.execute(
                """
                INSERT INTO entries(book_id, day, seconds, source) VALUES(?, ?, ?, ?)
                ON CONFLICT(book_id, day, source) DO UPDATE SET
                    seconds = CASE WHEN COALESCE(entries.seconds_custom, 0) = 1
                              THEN entries.seconds ELSE excluded.seconds END
                """,
                (book_id, day, seconds, source),
            )
            count += 1
    # Shelf updateTime may have been written first; align last_open to real days.
    reconcile_last_open_from_entries(book_id)
    return count


def _log_day(raw: str) -> str:
    day = str(raw or "").strip()[:10].replace("/", "-")
    try:
        date.fromisoformat(day)
    except ValueError:
        raise ValueError("请填写日期")
    return day


def _parse_minutes_number(raw) -> int:
    text = str(raw if raw is not None else "").strip()
    if not text:
        raise ValueError("请填写时长")
    try:
        return int(round(float(text)))
    except (TypeError, ValueError):
        raise ValueError("请填写时长")


def _log_minutes(raw) -> int:
    n = _parse_minutes_number(raw)
    if n < 1:
        raise ValueError("时长至少 1 分钟")
    if n > 24 * 60:
        raise ValueError("单日时长不能超过 24 小时")
    return n


def _entry_payload(row) -> dict:
    seconds = int(row["seconds"] or 0)
    try:
        custom = int(row["seconds_custom"] or 0) == 1
    except (KeyError, IndexError, TypeError, ValueError):
        custom = False
    return {
        "id": int(row["id"]),
        "bookId": row["book_id"],
        "title": row["title"] or "",
        "date": row["day"],
        "seconds": seconds,
        "minutes": seconds // 60,
        "source": row["source"] or "",
        "custom": custom,
    }


_ENTRY_SELECT = """
SELECT entries.id, entries.book_id, entries.day, entries.seconds, entries.source,
       COALESCE(entries.seconds_custom, 0) AS seconds_custom,
       books.title AS title
FROM entries JOIN books ON books.id = entries.book_id
"""


# Leading/trailing book marks & quotes — ignored for pinyin sort keys.
_TITLE_DECOR_RE = re.compile(r'[\s《》〈〉「」『』【】\[\]（）()""“”‘’\'"]+')


def _title_sort_key(title: str) -> str:
    """Pinyin-ish sort key: strip 《》/quotes, then lazy_pinyin (Latin/digits unchanged)."""
    raw = (title or "").strip()
    core = _TITLE_DECOR_RE.sub("", raw)
    if not core:
        core = raw
    if lazy_pinyin is not None:
        return "".join(lazy_pinyin(core)).casefold()
    return core.casefold()


def _sort_book_options_by_title(options: list[dict]) -> None:
    """Sort book picker options by pinyin (via pypinyin), not macOS locale collation."""
    options.sort(key=lambda item: (_title_sort_key(item.get("title") or ""), item.get("id") or ""))


def list_reading_log() -> dict:
    hidden = excluded_book_ids()
    with connect() as conn:
        rows = conn.execute(_ENTRY_SELECT).fetchall()
        books = conn.execute("SELECT id, title FROM books").fetchall()
    entries = [_entry_payload(row) for row in rows]
    entries.sort(key=lambda item: (item["date"], item["title"], item["bookId"], item["id"]))
    options = []
    for row in books:
        book_id = str(row["id"])
        if book_id in hidden:
            continue
        options.append({"id": book_id, "title": row["title"] or ""})
    # First-letter / pinyin order for 记录 mode manual-add book picker (#logBook).
    _sort_book_options_by_title(options)
    return {"entries": entries, "books": options}


def _load_entry(conn, entry_id: int) -> dict:
    row = conn.execute(_ENTRY_SELECT + " WHERE entries.id = ?", (entry_id,)).fetchone()
    if not row:
        raise ValueError("找不到这条记录")
    return _entry_payload(row)


def update_reading_entry(entry_id: int, day: str, book_id: str, minutes) -> dict:
    day = _log_day(day)
    book_id = str(book_id or "").strip()
    if not book_id:
        raise ValueError("请选择书")
    minutes_n = _parse_minutes_number(minutes)
    with connect() as conn:
        row = conn.execute("SELECT * FROM entries WHERE id = ?", (int(entry_id),)).fetchone()
        if not row:
            raise ValueError("找不到这条记录")
        book = conn.execute("SELECT id FROM books WHERE id = ?", (book_id,)).fetchone()
        if not book:
            raise ValueError("找不到这本书")
        current_seconds = int(row["seconds"] or 0)
        current_minutes = current_seconds // 60
        same_slot = book_id == row["book_id"] and day == row["day"]
        if same_slot and minutes_n == current_minutes:
            return _load_entry(conn, int(row["id"]))
        if minutes_n < 1:
            raise ValueError("时长至少 1 分钟")
        if minutes_n > 24 * 60:
            raise ValueError("单日时长不能超过 24 小时")
        seconds = current_seconds if minutes_n == current_minutes else minutes_n * 60
        clash = conn.execute(
            "SELECT id FROM entries WHERE book_id = ? AND day = ? AND id != ?",
            (book_id, day, int(row["id"])),
        ).fetchone()
        _forget_deleted_day(conn, book_id, day)
        if clash:
            # Same book already has that day → add minutes into it, drop this row.
            conn.execute(
                """
                UPDATE entries
                SET seconds = seconds + ?, seconds_custom = 1
                WHERE id = ?
                """,
                (seconds, int(clash["id"])),
            )
            old_book = row["book_id"]
            old_day = row["day"]
            conn.execute("DELETE FROM entries WHERE id = ?", (int(row["id"]),))
            left = conn.execute(
                "SELECT 1 FROM entries WHERE book_id = ? AND day = ? LIMIT 1",
                (old_book, old_day),
            ).fetchone()
            if not left and (old_book != book_id or old_day != day):
                _remember_deleted_day(conn, old_book, old_day)
            return _load_entry(conn, int(clash["id"]))
        try:
            conn.execute(
                """
                UPDATE entries
                SET book_id = ?, day = ?, seconds = ?, seconds_custom = 1
                WHERE id = ?
                """,
                (book_id, day, seconds, int(row["id"])),
            )
        except sqlite3.IntegrityError:
            # Race / multi-source unique: fall back to merge.
            clash2 = conn.execute(
                "SELECT id FROM entries WHERE book_id = ? AND day = ? AND id != ?",
                (book_id, day, int(row["id"])),
            ).fetchone()
            if not clash2:
                raise ValueError("这一天已经有这本书的记录")
            conn.execute(
                """
                UPDATE entries
                SET seconds = seconds + ?, seconds_custom = 1
                WHERE id = ?
                """,
                (seconds, int(clash2["id"])),
            )
            conn.execute("DELETE FROM entries WHERE id = ?", (int(row["id"]),))
            return _load_entry(conn, int(clash2["id"]))
        old_book = row["book_id"]
        old_day = row["day"]
        if old_book != book_id or old_day != day:
            left = conn.execute(
                "SELECT 1 FROM entries WHERE book_id = ? AND day = ? LIMIT 1",
                (old_book, old_day),
            ).fetchone()
            if not left:
                _remember_deleted_day(conn, old_book, old_day)
        return _load_entry(conn, int(row["id"]))


def add_reading_entry(book_id: str, day: str, minutes) -> dict:
    day = _log_day(day)
    book_id = str(book_id or "").strip()
    if not book_id:
        raise ValueError("请选择书")
    seconds = _log_minutes(minutes) * 60
    with connect() as conn:
        book = conn.execute("SELECT id FROM books WHERE id = ?", (book_id,)).fetchone()
        if not book:
            raise ValueError("找不到这本书")
        existing = conn.execute(
            "SELECT id FROM entries WHERE book_id = ? AND day = ?",
            (book_id, day),
        ).fetchone()
        _forget_deleted_day(conn, book_id, day)
        if existing:
            conn.execute(
                """
                UPDATE entries
                SET seconds = seconds + ?, seconds_custom = 1
                WHERE id = ?
                """,
                (seconds, int(existing["id"])),
            )
            return _load_entry(conn, int(existing["id"]))
        try:
            cur = conn.execute(
                """
                INSERT INTO entries(book_id, day, seconds, source, seconds_custom)
                VALUES(?, ?, ?, 'manual', 1)
                """,
                (book_id, day, seconds),
            )
        except sqlite3.IntegrityError:
            existing2 = conn.execute(
                "SELECT id FROM entries WHERE book_id = ? AND day = ?",
                (book_id, day),
            ).fetchone()
            if not existing2:
                raise ValueError("这一天已经有这本书的记录")
            conn.execute(
                """
                UPDATE entries
                SET seconds = seconds + ?, seconds_custom = 1
                WHERE id = ?
                """,
                (seconds, int(existing2["id"])),
            )
            return _load_entry(conn, int(existing2["id"]))
        return _load_entry(conn, int(cur.lastrowid))


def delete_reading_entry(entry_id: int) -> None:
    with connect() as conn:
        row = conn.execute(
            "SELECT id, book_id, day FROM entries WHERE id = ?",
            (int(entry_id),),
        ).fetchone()
        if not row:
            raise ValueError("找不到这条记录")
        _remember_deleted_day(conn, row["book_id"], row["day"])
        conn.execute(
            "DELETE FROM entries WHERE book_id = ? AND day = ?",
            (row["book_id"], row["day"]),
        )


def update_book_reading_day(book_id: str, from_day: str, to_day: str, minutes) -> dict:
    """Edit a card detail day-bar (book+day aggregate). Merge minutes if to_day already exists."""
    book_id = str(book_id or "").strip()
    if not book_id:
        raise ValueError("请选择书")
    from_day = _log_day(from_day)
    to_day = _log_day(to_day)
    minutes_n = _parse_minutes_number(minutes)
    if minutes_n < 1:
        raise ValueError("时长至少 1 分钟")
    if minutes_n > 24 * 60:
        raise ValueError("单日时长不能超过 24 小时")
    seconds = minutes_n * 60
    with connect() as conn:
        book = conn.execute("SELECT id FROM books WHERE id = ?", (book_id,)).fetchone()
        if not book:
            raise ValueError("找不到这本书")
        src = conn.execute(
            "SELECT id FROM entries WHERE book_id = ? AND day = ? ORDER BY id",
            (book_id, from_day),
        ).fetchall()
        if not src:
            raise ValueError("找不到这条记录")
        keep_id = int(src[0]["id"])
        if from_day == to_day:
            conn.execute(
                """
                UPDATE entries
                SET seconds = ?, seconds_custom = 1
                WHERE id = ?
                """,
                (seconds, keep_id),
            )
            conn.execute(
                "DELETE FROM entries WHERE book_id = ? AND day = ? AND id != ?",
                (book_id, from_day, keep_id),
            )
            return _load_entry(conn, keep_id)
        dest = conn.execute(
            "SELECT id FROM entries WHERE book_id = ? AND day = ? ORDER BY id",
            (book_id, to_day),
        ).fetchall()
        _forget_deleted_day(conn, book_id, to_day)
        if dest:
            dest_id = int(dest[0]["id"])
            conn.execute(
                """
                UPDATE entries
                SET seconds = seconds + ?, seconds_custom = 1
                WHERE id = ?
                """,
                (seconds, dest_id),
            )
            conn.execute(
                "DELETE FROM entries WHERE book_id = ? AND day = ?",
                (book_id, from_day),
            )
            _remember_deleted_day(conn, book_id, from_day)
            return _load_entry(conn, dest_id)
        conn.execute(
            """
            UPDATE entries
            SET day = ?, seconds = ?, seconds_custom = 1
            WHERE id = ?
            """,
            (to_day, seconds, keep_id),
        )
        conn.execute(
            "DELETE FROM entries WHERE book_id = ? AND day = ?",
            (book_id, from_day),
        )
        _remember_deleted_day(conn, book_id, from_day)
        return _load_entry(conn, keep_id)


def delete_book_reading_day(book_id: str, day: str) -> None:
    book_id = str(book_id or "").strip()
    day = _log_day(day)
    if not book_id:
        raise ValueError("请选择书")
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM entries WHERE book_id = ? AND day = ? LIMIT 1",
            (book_id, day),
        ).fetchone()
        if not row:
            raise ValueError("找不到这条记录")
        _remember_deleted_day(conn, book_id, day)
        conn.execute("DELETE FROM entries WHERE book_id = ? AND day = ?", (book_id, day))


def _cover_for_payload(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    if is_local_cover_url(raw):
        return raw
    try:
        import weread
        return weread.upgrade_cover_url(raw) or raw
    except Exception:
        return raw


def books_payload() -> dict:
    threshold = min_minutes() * 60
    book_total_threshold = min_book_total_minutes() * 60
    hidden = excluded_book_ids()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT books.*,
                COALESCE(SUM(CASE WHEN entries.seconds >= ? THEN entries.seconds ELSE 0 END), 0) AS seconds,
                COUNT(DISTINCT CASE WHEN entries.seconds >= ? THEN entries.day END) AS read_days,
                MIN(CASE WHEN entries.seconds >= ? THEN entries.day END) AS first_day,
                MAX(CASE WHEN entries.seconds > 0 THEN entries.day END) AS last_day
            FROM books
            LEFT JOIN entries ON entries.book_id = books.id
            GROUP BY books.id
            """,
            (threshold, threshold, threshold),
        ).fetchall()
        skip_books = short_lifetime_book_ids(conn, book_total_threshold)
    vis_map = book_visibility_map()
    books = []
    for row in rows:
        item = dict(row)
        if item.get("id") in hidden or item.get("id") in skip_books:
            continue
        first_day = str(item.get("first_day") or "")[:10]
        if first_day and len(first_day) < 10:
            first_day = ""
        last_day = str(item.get("last_day") or "")[:10]
        if last_day and len(last_day) < 10:
            last_day = ""
        # Prefer last day with real reading seconds over WeRead shelf updateTime.
        last_open = effective_last_open(item.get("last_open"), last_day)
        seconds = int(item.get("seconds") or 0)
        days = int(item.get("read_days") or 0)
        progress = item.get("progress")
        # Hide empty synced stubs; keep manually created shelf books even with no entries yet.
        if seconds <= 0 and days <= 0 and str(item.get("source") or "") != "manual":
            continue
        stored_finish = str(item.get("finish_date") or "").strip()[:10]
        if stored_finish and len(stored_finish) < 10:
            stored_finish = ""
        # Keep a stored finish date even below 100%. The card shows it only at 100%.
        # A member volume's finish date is not copied here: an unfinished series
        # stays unfinished in 读完日期 sort and falls back to last open.
        finish_date = stored_finish or None
        finished = is_finished(progress)
        try:
            progress_custom = int(item.get("progress_custom") or 0) == 1
        except (TypeError, ValueError):
            progress_custom = False
        vis = vis_map.get(item["id"]) or {}
        books.append(
            {
                "id": item["id"],
                "title": item["title"],
                "source": item["source"],
                "cover": _cover_for_payload(item.get("cover") or ""),
                "progress": progress,
                "progressCustom": progress_custom,
                "showCal": vis.get("cal", True),
                "showCard": vis.get("card", True),
                "showTicket": vis.get("ticket", True),
                "finished": finished,
                "wordCount": int(item.get("word_count") or 0),
                "author": item.get("author") or "",
                "isbn": item.get("isbn") or "",
                "publisher": item.get("publisher") or "",
                "translator": item.get("translator") or "",
                "price": item.get("price") or "",
                "publishYear": item.get("publish_year") or "",
                "seconds": seconds,
                "minutes": seconds // 60,
                "readDays": days,
                "firstDay": first_day or None,
                "lastDay": last_day or None,
                "lastOpen": last_open,
                "lastOpened": last_open,
                "finishDate": finish_date,
                "barColor": bar_color_for(item["id"], item.get("bar_color")),
                "titleColor": title_color_for(item.get("title_color"), item.get("bar_color")),
            }
        )
    books.sort(key=lambda b: (-int(b["lastOpen"] or 0), b["title"]))
    return {
        "minMinutes": min_minutes(),
        "minBookTotalMinutes": min_book_total_minutes(),
        "books": books,
    }


def book_detail(book_id: str) -> dict:
    threshold = min_minutes() * 60
    with connect() as conn:
        book = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
        if not book:
            raise ValueError("找不到这本书")
        rows = conn.execute(
            """
            SELECT day, SUM(seconds) AS seconds FROM entries
            WHERE book_id = ? AND seconds >= ?
            GROUP BY day ORDER BY day
            """,
            (book_id, threshold),
        ).fetchall()
    months: dict[str, dict] = {}
    for row in rows:
        day = date.fromisoformat(row["day"])
        key = "%04d-%02d" % (day.year, day.month)
        bucket = months.setdefault(
            key, {"year": day.year, "month": day.month, "seconds": 0, "days": []}
        )
        secs = int(row["seconds"] or 0)
        bucket["seconds"] += secs
        bucket["days"].append({"day": day.day, "date": row["day"], "seconds": secs})
    for bucket in months.values():
        bucket["days"].sort(key=lambda item: item["date"], reverse=True)
    title_color = book["title_color"] if "title_color" in book.keys() else None
    bar = bar_color_for(book["id"], book["bar_color"] if "bar_color" in book.keys() else None)
    return {
        "id": book["id"],
        "title": book["title"],
        "barColor": bar,
        "titleColor": title_color_for(title_color, bar),
        "months": [months[k] for k in sorted(months.keys(), reverse=True)],
    }
