#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""书历本地服务。由「书历.app」以子进程拉起；也可以单独跑。"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen

# Windows embeddable Python uses a ._pth file and does not put the script
# directory on sys.path. Insert it before the local imports.
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ireader
import store
import sync
import weread

store.ensure_bar_palette()

STATIC = ROOT / "static"
HOST = "127.0.0.1"
# Phone on the same Wi-Fi reaches the Mac. Localhost checks still use HOST.
BIND_HOST = os.environ.get("READING_LOG_BIND", "0.0.0.0")
PORT = 18766
TZ = timezone(timedelta(hours=8))
COVER_CACHE: dict[str, tuple[bytes, str]] = {}
COVER_CACHE_MAX = 80
COVER_HOSTS = ("weread.qq.com", "myqcloud.com", "qpic.cn", "qcloud.com")
EXPORT_NAME_RE = re.compile(r"[^\w\u4e00-\u9fff.\- ]+", re.UNICODE)
MAX_EXPORT_PNG = 25 * 1024 * 1024
MAX_WALLPAPER_PNG = 80 * 1024 * 1024
MAX_COVER_PNG = 8 * 1024 * 1024


def settings_public() -> dict:
    wall = store.wallpaper_target()
    return {
        "ok": True,
        "minMinutes": store.min_minutes(),
        "minBookTotalMinutes": store.min_book_total_minutes(),
        "wallpaperYear": wall["year"],
        "wallpaperMonth": wall["month"],
        "wallpaperMode": wall["mode"],
        "wallpaperValue": wall["value"],
        "wallpaperOffset": int(wall.get("offset") or 0),
        "uiTheme": store.ui_theme(),
        "uiTonePresets": store.ui_tone_presets(),
        "uiGrade": store.ui_grade(),
        "uiGradePresets": store.ui_grade_presets(),
        "lastView": store.last_view(),
        "plannerPad": store.planner_pad(),
        "calScroll": store.cal_scroll(),
        "calStats": store.cal_stats(),
        "calRecords": store.cal_records(),
        "calSplit": store.cal_split(),
        "plannerOrient": store.planner_orient(),
        "portraitRecRows": store.portrait_rec_rows(),
        "barRows": store.bar_rows(),
        "recentOffset": store.recent_offset(),
        "booksScroll": store.books_scroll(),
        "syncIntervalMinutes": store.sync_interval_minutes(),
        "syncTimes": store.sync_times(),
        "wereadSyncAt": store.weread_sync_at(),
        "colorDefaults": store.color_defaults(),
        "uiFont": store.ui_font(),
        "trackTitle": store.track_title(),
        "device": store.current_device(),
        "imageSaveDir": store.image_save_dir(),
    }


def inject_planner_pad(html: bytes) -> bytes:
    pad = store.planner_pad()
    land = pad.get("landscape") or store._pad_defaults("landscape")
    port = pad.get("portrait") or store._pad_defaults("portrait")
    saved = bool(pad.get("saved"))
    track = pad.get("track") or store._pad_defaults("track")
    seed = (
        "window.__PLANNER_PAD__="
        + json.dumps(
            {
                "landscape": land,
                "portrait": port,
                "track": track,
                "left": land.get("left"),
                "right": land.get("right"),
                "top": land.get("top"),
                "bottom": land.get("bottom"),
                "saved": saved,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + ";"
    ).encode("utf-8")
    marker = b"window.__PLANNER_PAD__=null;"
    if marker in html:
        return html.replace(marker, seed, 1)
    snippet = b"<script>" + seed + b"</script>"
    if b"</head>" in html:
        return html.replace(b"</head>", snippet + b"</head>", 1)
    return snippet + html


def inject_ui_font(html: bytes) -> bytes:
    seed = (
        "window.__UI_FONT__="
        + json.dumps(store.ui_font(), ensure_ascii=False, separators=(",", ":"))
        + ";"
    ).encode("utf-8")
    marker = b"window.__UI_FONT__=null;"
    if marker in html:
        return html.replace(marker, seed, 1)
    snippet = b"<script>" + seed + b"</script>"
    if b"</head>" in html:
        return html.replace(b"</head>", snippet + b"</head>", 1)
    return snippet + html


def _add_font_name(names: set[str], raw) -> None:
    text = str(raw or "").strip()
    if not text or text.startswith(".") or text.startswith("/"):
        return
    if "/" in text or "\\" in text:
        return
    # Skip private / placeholder families CoreText sometimes returns.
    if text in ("-", "?", "\xa0") or set(text) <= set("-?. "):
        return
    if "\ufffd" in text or text.count("?") >= max(3, len(text) // 2):
        return
    names.add(text)


def _list_fonts_macos_coretext(names: set[str]) -> None:
    """Fast family list via CoreText (preferred on Darwin)."""
    import ctypes
    from ctypes import c_bool, c_long, c_uint32, c_void_p

    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    ct = ctypes.CDLL("/System/Library/Frameworks/CoreText.framework/CoreText")
    cf.CFArrayGetCount.argtypes = [c_void_p]
    cf.CFArrayGetCount.restype = c_long
    cf.CFArrayGetValueAtIndex.argtypes = [c_void_p, c_long]
    cf.CFArrayGetValueAtIndex.restype = c_void_p
    cf.CFStringGetCString.argtypes = [c_void_p, c_void_p, c_long, c_uint32]
    cf.CFStringGetCString.restype = c_bool
    cf.CFRelease.argtypes = [c_void_p]
    ct.CTFontManagerCopyAvailableFontFamilyNames.restype = c_void_p
    arr = ct.CTFontManagerCopyAvailableFontFamilyNames()
    if not arr:
        return
    try:
        buf = ctypes.create_string_buffer(1024)
        k_utf8 = 0x08000100
        count = int(cf.CFArrayGetCount(arr))
        for i in range(count):
            s = cf.CFArrayGetValueAtIndex(arr, i)
            if not s:
                continue
            if cf.CFStringGetCString(s, buf, 1024, k_utf8):
                _add_font_name(names, buf.value.decode("utf-8", "replace"))
    finally:
        cf.CFRelease(arr)


def _list_fonts_macos(names: set[str]) -> None:
    try:
        _list_fonts_macos_coretext(names)
    except Exception:
        pass
    if names:
        return
    # Slower fallback when CoreText bridging fails.
    try:
        out = subprocess.check_output(
            ["system_profiler", "SPFontsDataType", "-json"],
            stderr=subprocess.DEVNULL,
            timeout=90,
        )
        data = json.loads(out.decode("utf-8", "replace"))
        rows = data.get("SPFontsDataType") if isinstance(data, dict) else None
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                faces = row.get("typefaces")
                if isinstance(faces, list):
                    for face in faces:
                        if isinstance(face, dict):
                            _add_font_name(names, face.get("family"))
                _add_font_name(names, row.get("family"))
    except Exception:
        pass
    if names:
        return
    roots = [
        Path("/System/Library/Fonts"),
        Path("/Library/Fonts"),
        Path.home() / "Library" / "Fonts",
    ]
    for root in roots:
        try:
            if not root.is_dir():
                continue
            for path in root.rglob("*"):
                try:
                    if not path.is_file() or path.name.startswith("."):
                        continue
                    if path.suffix.lower() not in (".ttf", ".otf", ".ttc", ".dfont"):
                        continue
                    _add_font_name(names, path.stem)
                except Exception:
                    continue
        except Exception:
            continue


def _list_fonts_windows(names: set[str]) -> None:
    ps = (
        "Add-Type -AssemblyName System.Drawing; "
        "(New-Object System.Drawing.Text.InstalledFontCollection).Families "
        "| ForEach-Object { $_.Name }"
    )
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", ps],
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
        for line in out.decode("utf-8", "replace").splitlines():
            _add_font_name(names, line)
    except Exception:
        pass
    if names:
        return
    # Registry fallback (font display names / file keys).
    try:
        import winreg  # type: ignore

        for hive, sub in (
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
        ):
            try:
                key = winreg.OpenKey(hive, sub)
            except OSError:
                continue
            try:
                i = 0
                while True:
                    try:
                        name, _val, _typ = winreg.EnumValue(key, i)
                    except OSError:
                        break
                    i += 1
                    clean = re.sub(r"\s*\((?:TrueType|OpenType|All res)\)\s*$", "", str(name or ""), flags=re.I)
                    _add_font_name(names, clean)
            finally:
                winreg.CloseKey(key)
    except Exception:
        pass


def _list_fonts_fontconfig(names: set[str]) -> None:
    try:
        out = subprocess.check_output(
            ["fc-list", ":", "family"],
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        for line in out.decode("utf-8", "replace").splitlines():
            for part in line.split(","):
                _add_font_name(names, part)
    except Exception:
        pass


_FONT_LIST_CACHE: list[str] | None = None


def list_system_fonts() -> list[str]:
    global _FONT_LIST_CACHE
    if _FONT_LIST_CACHE is not None:
        return _FONT_LIST_CACHE
    names: set[str] = set()
    try:
        if sys.platform == "darwin":
            _list_fonts_macos(names)
        elif sys.platform == "win32":
            _list_fonts_windows(names)
        else:
            _list_fonts_fontconfig(names)
    except Exception:
        pass
    _FONT_LIST_CACHE = sorted(names, key=lambda s: s.casefold())
    return _FONT_LIST_CACHE


def allowed_cover_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    return bool(host) and any(host == suffix or host.endswith("." + suffix) for suffix in COVER_HOSTS)


def cover_referer(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if any(host == suffix or host.endswith("." + suffix) for suffix in ("zhangyue01.com", "zhangyuecdn.com", "zhangyue.com", "ireader.com")):
        return "https://pc.ireader.com/"
    return "https://weread.qq.com/"


def fetch_cover_bytes(url: str) -> tuple[bytes, str]:
    cached = COVER_CACHE.get(url)
    if cached:
        return cached
    req = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": cover_referer(url),
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        },
    )
    with weread.open_url(req, timeout=12) as resp:
        body = resp.read()
        ctype = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
    if not ctype.startswith("image/"):
        ctype = "image/jpeg"
    if len(COVER_CACHE) >= COVER_CACHE_MAX:
        COVER_CACHE.pop(next(iter(COVER_CACHE)))
    COVER_CACHE[url] = (body, ctype)
    return body, ctype


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        try:
            msg = fmt % args
        except Exception:
            msg = " ".join([str(fmt)] + [str(a) for a in args])
        sys.stderr.write("[%s] %s\n" % (datetime.now().strftime("%H:%M:%S"), msg))

    def _send(self, code: int, body: bytes, content_type: str, cache: str = "no-store"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, raw, "application/json; charset=utf-8")

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        payload = self.rfile.read(length) if length else b""
        if not payload:
            return {}
        try:
            data = json.loads(payload.decode("utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _request_device(self, parsed, body) -> str:
        """mac vs b7. Query/body win; an Android client with no flag is the phone."""
        qs = parse_qs(parsed.query or "")
        raw = ""
        if qs.get("device"):
            raw = qs["device"][0]
        elif isinstance(body, dict) and body.get("device") not in (None, ""):
            raw = body.get("device")
        if str(raw or "").strip():
            return store.normalize_device(raw)
        ua = self.headers.get("User-Agent") or ""
        if "Android" in ua or "Dalvik" in ua:
            return "b7"
        return "mac"

    def do_GET(self):
        parsed = urlparse(self.path)
        token = store.bind_device(self._request_device(parsed, None))
        try:
            self._do_GET(parsed)
        finally:
            store.reset_device(token)

    def _do_GET(self, parsed):
        if parsed.path == "/api/status":
            payload = {"ok": True}
            try:
                wall = store.wallpaper_target()
                min_book = store.min_book_total_minutes() if hasattr(store, "min_book_total_minutes") else 0
                payload.update(
                    {
                        "weread": weread.status(),
                        "minMinutes": store.min_minutes(),
                        "minBookTotalMinutes": min_book,
                        "dbPath": str(store.DB_PATH),
                        "calStart": {"year": store.CAL_START.year, "month": store.CAL_START.month},
                        "wallpaperYear": wall["year"],
                        "wallpaperMonth": wall["month"],
                        "wallpaperMode": wall["mode"],
                        "wallpaperValue": wall["value"],
                        "wallpaperOffset": int(wall.get("offset") or 0),
                        "uiTheme": store.ui_theme(),
                        "uiTonePresets": store.ui_tone_presets(),
                        "uiGrade": store.ui_grade(),
                        "uiGradePresets": store.ui_grade_presets(),
                        "lastView": store.last_view(),
                        "plannerPad": store.planner_pad(),
                        "calScroll": store.cal_scroll(),
                        "calStats": store.cal_stats(),
                        "calRecords": store.cal_records(),
                        "calSplit": store.cal_split(),
                        "plannerOrient": store.planner_orient(),
                        "portraitRecRows": store.portrait_rec_rows(),
                        "barRows": store.bar_rows(),
                        "recentOffset": store.recent_offset(),
                        "booksScroll": store.books_scroll(),
                        "syncIntervalMinutes": store.sync_interval_minutes(),
                        "syncTimes": store.sync_times(),
                        "wereadSyncAt": store.weread_sync_at(),
                        "colorDefaults": store.color_defaults(),
                        "uiFont": store.ui_font(),
                        "trackTitle": store.track_title(),
                        "device": store.current_device(),
                        "imageSaveDir": store.image_save_dir(),
                    }
                )
            except Exception as exc:
                payload["ok"] = False
                payload["error"] = str(exc)
            self._json(200, payload)
            return
        if parsed.path == "/api/fonts":
            try:
                fonts = list_system_fonts()
            except Exception:
                fonts = []
            self._json(200, {"ok": True, "fonts": fonts})
            return
        if parsed.path == "/api/month":
            qs = parse_qs(parsed.query)
            now = datetime.now(TZ)
            year = int((qs.get("year") or [now.year])[0])
            month = int((qs.get("month") or [now.month])[0])
            self._json(200, store.month_payload(year, month))
            return
        if parsed.path == "/api/recent":
            qs = parse_qs(parsed.query)
            offset = None
            end = None
            if "offset" in qs:
                try:
                    offset = int((qs.get("offset") or ["0"])[0])
                except Exception:
                    offset = 0
            elif "end" in qs:
                end = (qs.get("end") or [""])[0]
            self._json(200, store.recent_payload(offset=offset, end=end))
            return
        if parsed.path == "/api/range":
            qs = parse_qs(parsed.query)
            start = (qs.get("from") or [""])[0]
            end = (qs.get("to") or [""])[0]
            try:
                self._json(200, store.range_payload(start, end))
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            return
        if parsed.path == "/api/books":
            self._json(200, store.books_payload())
            return
        if parsed.path == "/api/books/detail":
            qs = parse_qs(parsed.query)
            book_id = (qs.get("id") or [""])[0]
            try:
                self._json(200, store.book_detail(book_id))
            except Exception as exc:
                self._json(400, {"error": str(exc)})
            return
        if parsed.path == "/api/reading-log":
            self._json(200, store.list_reading_log())
            return
        if parsed.path == "/api/cover":
            qs = parse_qs(parsed.query)
            raw = unquote((qs.get("u") or [""])[0].strip())
            raw = weread.upgrade_cover_url(raw) or raw
            if not allowed_cover_url(raw):
                self._send(400, b"bad cover", "text/plain")
                return
            try:
                body, ctype = fetch_cover_bytes(raw)
                self._send(200, body, ctype, cache="public, max-age=86400")
            except Exception:
                self._send(404, b"no cover", "text/plain")
            return
        if parsed.path.startswith("/api/covers/"):
            name = unquote(parsed.path[len("/api/covers/"):].strip("/"))
            if "/" in name or "\\" in name or not store.COVER_FILE_RE.match(name):
                self._send(400, b"bad cover", "text/plain")
                return
            root = store.COVERS_DIR.resolve()
            path = (store.COVERS_DIR / name).resolve()
            if path.parent != root or not path.is_file():
                self._send(404, b"no cover", "text/plain")
                return
            self._send(200, path.read_bytes(), "image/png", cache="public, max-age=86400")
            return
        if parsed.path == "/api/login/status":
            self._json(200, weread.status())
            return
        if parsed.path == "/api/login/qr/poll":
            qs = parse_qs(parsed.query)
            uid = (qs.get("uid") or [""])[0]
            otp = (qs.get("otp") or [""])[0]
            try:
                self._json(200, weread.qr_poll(uid, otp))
            except Exception as exc:
                self._json(500, {"error": str(exc)})
            return
        if parsed.path == "/api/login/ireader/status":
            self._json(200, ireader.status())
            return
        if parsed.path == "/api/login/ireader/qr/poll":
            qs = parse_qs(parsed.query)
            uid = (qs.get("uid") or [""])[0]
            try:
                self._json(200, ireader.qr_poll(uid))
            except Exception as exc:
                self._json(500, {"error": str(exc)})
            return
        if parsed.path == "/api/stamp":
            self._json(200, data_stamp())
            return
        if parsed.path == "/api/win/chrome":
            self._json(200, win_chrome_state())
            return
        if parsed.path == "/api/wallpaper/progress":
            qs = parse_qs(parsed.query)
            step = (qs.get("step") or [""])[0].strip()
            log_wallpaper_progress(step)
            self._json(200, {"ok": True})
            return
        if parsed.path == "/api/wallpaper/png":
            # Hibreak lock path: Android GETs a ready PNG (no device WebView).
            # device=b7 (default for Android UA) generates B7 size/mode only —
            # does not replace the Mac desktop wallpaper capture.
            qs = parse_qs(parsed.query)
            fresh = (qs.get("fresh") or ["0"])[0].strip().lower() in ("1", "true", "yes")
            device = store.current_device()
            width = _qs_int(qs, "width", 0) or _qs_int(qs, "w", 0) or _qs_int(qs, "canvasWidth", 0)
            height = _qs_int(qs, "height", 0) or _qs_int(qs, "h", 0) or _qs_int(qs, "canvasHeight", 0)
            mag = _qs_int(qs, "mag", 0) or _qs_int(qs, "scale", 0) or _qs_int(qs, "canvasScale", 0)
            orient = ((qs.get("orient") or qs.get("orientation") or [""])[0] or "").strip()
            try:
                data, meta = load_wallpaper_png(
                    fresh=fresh,
                    device=device,
                    width=width,
                    height=height,
                    mag=mag,
                    orient=orient,
                )
            except Exception as exc:
                self._json(404, {"ok": False, "error": str(exc)})
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-ReadingLog-Wallpaper-Path", meta.get("path") or "")
            self.send_header("X-ReadingLog-Wallpaper-Fresh", "1" if meta.get("fresh") else "0")
            self.send_header("X-ReadingLog-Wallpaper-Bytes", str(len(data)))
            self.send_header("X-ReadingLog-Wallpaper-Device", meta.get("device") or device)
            self.end_headers()
            self.wfile.write(data)
            return
        if parsed.path == "/api/settings":
            self._json(200, settings_public())
            return

        rel = parsed.path.lstrip("/") or "index.html"
        target = (STATIC / rel).resolve()
        if STATIC not in target.parents and target != STATIC / "index.html":
            self._send(403, b"forbidden", "text/plain")
            return
        if not target.exists() or not target.is_file():
            target = STATIC / "index.html"
        if not target.exists():
            self._send(404, b"not found", "text/plain")
            return
        types = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".png": "image/png",
            ".svg": "image/svg+xml",
        }
        body = target.read_bytes()
        if target.name == "index.html":
            body = inject_planner_pad(body)
            body = inject_ui_font(body)
        self._send(200, body, types.get(target.suffix.lower(), "application/octet-stream"))

    def do_POST(self):
        parsed = urlparse(self.path)
        body = self._read_json()
        token = store.bind_device(self._request_device(parsed, body))
        try:
            self._do_POST(parsed, body)
        finally:
            store.reset_device(token)

    def _do_POST(self, parsed, body):
        try:
            if parsed.path == "/api/login/qr/start":
                self._json(200, weread.qr_start())
                return
            if parsed.path == "/api/login/cookies":
                session = weread.save_cookies_from_paste(body.get("cookie") or "")
                self._json(200, {"ok": True, **weread.status(), "name": session.get("name") or ""})
                return
            if parsed.path == "/api/login/logout":
                weread.clear_session()
                self._json(200, {"ok": True, **weread.status()})
                return
            if parsed.path == "/api/login/ireader/qr/start":
                self._json(200, ireader.qr_start())
                return
            if parsed.path == "/api/login/ireader/cookies":
                session = ireader.save_cookies_from_paste(body.get("cookie") or "")
                self._json(200, {"ok": True, **ireader.status(), "name": session.get("name") or ""})
                return
            if parsed.path == "/api/login/ireader/logout":
                ireader.clear_session()
                self._json(200, {"ok": True, **ireader.status()})
                return
            if parsed.path == "/api/sync/weread":
                now = datetime.now(TZ)
                year = int(body.get("year") or now.year)
                month = int(body.get("month") or now.month)
                self._json(200, sync.sync_weread(year, month))
                return
            if parsed.path == "/api/sync/ireader":
                self._json(200, sync.sync_ireader())
                return
            if parsed.path == "/api/pick-folder":
                self._json(200, pick_save_folder())
                return
            if parsed.path == "/api/settings":
                if "imageSaveDir" in body:
                    store.set_image_save_dir(normalize_image_save_dir(body.get("imageSaveDir")))
                if "minMinutes" in body:
                    store.set_min_minutes(int(body.get("minMinutes") or 3))
                if "minBookTotalMinutes" in body:
                    store.set_min_book_total_minutes(int(body.get("minBookTotalMinutes") or 0))
                if "wallpaperMonth" in body:
                    store.set_wallpaper_month(str(body.get("wallpaperMonth") or "current"))
                if "uiTheme" in body:
                    store.set_ui_theme(body.get("uiTheme") or {})
                if "uiTonePresets" in body:
                    store.set_ui_tone_presets(body.get("uiTonePresets"))
                if "uiGrade" in body:
                    store.set_ui_grade(body.get("uiGrade") or {})
                if "uiGradePresets" in body:
                    store.set_ui_grade_presets(body.get("uiGradePresets"))
                if "lastView" in body:
                    last = body.get("lastView")
                    store.set_last_view(last if isinstance(last, dict) else {})
                if "recentOffset" in body:
                    store.set_recent_offset(body.get("recentOffset"))
                if "booksScroll" in body:
                    store.set_books_scroll(body.get("booksScroll"))
                if "plannerPad" in body:
                    pad = body.get("plannerPad")
                    store.set_planner_pad(pad if isinstance(pad, dict) else {})
                if "calScroll" in body:
                    store.set_cal_scroll(body.get("calScroll"))
                if "calStats" in body:
                    store.set_cal_stats(body.get("calStats"))
                if "calRecords" in body:
                    store.set_cal_records(body.get("calRecords"))
                if "calSplit" in body:
                    store.set_cal_split(body.get("calSplit"))
                if "plannerOrient" in body:
                    store.set_planner_orient(body.get("plannerOrient"))
                if "portraitRecRows" in body:
                    store.set_portrait_rec_rows(body.get("portraitRecRows"))
                if "barRows" in body:
                    store.set_bar_rows(body.get("barRows"))
                if "syncIntervalMinutes" in body:
                    store.set_sync_interval_minutes(body.get("syncIntervalMinutes"))
                if "syncTimes" in body:
                    store.set_sync_times(body.get("syncTimes"))
                if "bookVisibility" in body:
                    store.set_book_visibility(body.get("bookVisibility") or {})
                if "colorDefaults" in body:
                    store.set_color_defaults(body.get("colorDefaults"))
                if "uiFont" in body:
                    store.set_ui_font(body.get("uiFont"))
                if "trackTitle" in body:
                    store.set_track_title(body.get("trackTitle"))
                self._json(200, settings_public())
                return
            if parsed.path == "/api/export/png":
                self._json(200, save_export_png(str(body.get("filename") or ""), str(body.get("pngBase64") or "")))
                return
            if parsed.path == "/api/wallpaper/capture":
                self._json(200, save_wallpaper_capture(str(body.get("pngBase64") or "")))
                return
            if parsed.path == "/api/entries":
                item = store.add_entry(
                    str(body.get("date") or ""),
                    str(body.get("title") or ""),
                    float(body.get("minutes") or 0),
                    str(body.get("source") or "manual"),
                )
                self._json(200, {"ok": True, "entry": item})
                return
            if parsed.path == "/api/reading-log/update":
                item = store.update_reading_entry(
                    int(body.get("id") or 0),
                    str(body.get("date") or ""),
                    str(body.get("bookId") or body.get("id_book") or ""),
                    body.get("minutes"),
                )
                self._json(200, {"ok": True, "entry": item})
                return
            if parsed.path == "/api/reading-log/add":
                item = store.add_reading_entry(
                    str(body.get("bookId") or ""),
                    str(body.get("date") or ""),
                    body.get("minutes"),
                )
                self._json(200, {"ok": True, "entry": item})
                return
            if parsed.path == "/api/reading-log/delete":
                store.delete_reading_entry(int(body.get("id") or 0))
                self._json(200, {"ok": True})
                return
            if parsed.path == "/api/reading-log/day/update":
                item = store.update_book_reading_day(
                    str(body.get("bookId") or ""),
                    str(body.get("fromDate") or body.get("from") or ""),
                    str(body.get("date") or body.get("toDate") or ""),
                    body.get("minutes"),
                )
                self._json(200, {"ok": True, "entry": item})
                return
            if parsed.path == "/api/reading-log/day/delete":
                store.delete_book_reading_day(
                    str(body.get("bookId") or ""),
                    str(body.get("date") or ""),
                )
                self._json(200, {"ok": True})
                return
            if parsed.path == "/api/import":
                text = str(body.get("csv") or "")
                source = str(body.get("source") or "ireader")
                n = store.import_csv(text, source)
                self._json(200, {"ok": True, "count": n})
                return
            if parsed.path == "/api/books/color":
                book = store.set_book_colors(
                    str(body.get("id") or ""),
                    body.get("barColor"),
                    body.get("titleColor"),
                )
                self._json(200, {"ok": True, "book": book})
                return
            if parsed.path == "/api/books/cover":
                data = _decode_png_b64(str(body.get("pngBase64") or ""), MAX_COVER_PNG)
                book = store.save_custom_cover_png(str(body.get("id") or ""), data)
                self._json(
                    200,
                    {
                        "ok": True,
                        "cover": book.get("cover") or "",
                        "book": {
                            "id": book.get("id"),
                            "title": book.get("title"),
                            "cover": book.get("cover") or "",
                            "barColor": book.get("bar_color"),
                            "titleColor": book.get("title_color"),
                        },
                    },
                )
                return
            if parsed.path == "/api/books/create":
                book = store.create_manual_book(str(body.get("title") or ""))
                self._json(200, {"ok": True, "book": book})
                return
            if parsed.path == "/api/books/rename":
                book = store.rename_book(str(body.get("id") or ""), str(body.get("title") or ""))
                self._json(200, {"ok": True, "book": book})
                return
            if parsed.path == "/api/books/author":
                book = store.set_book_author(str(body.get("id") or ""), str(body.get("author") or ""))
                self._json(200, {"ok": True, "book": book})
                return
            if parsed.path == "/api/books/word-count":
                book = store.set_book_word_count(str(body.get("id") or ""), body.get("wordCount") if "wordCount" in body else body.get("word_count"))
                self._json(200, {"ok": True, "book": book})
                return
            if parsed.path == "/api/books/finish-date":
                book = store.set_book_finish_date(str(body.get("id") or ""), str(body.get("finishDate") or body.get("date") or ""))
                self._json(200, {"ok": True, "book": book})
                return
            if parsed.path == "/api/books/progress":
                book = store.set_book_progress(str(body.get("id") or ""), body.get("progress"), custom=True)
                self._json(200, {"ok": True, "progress": book.get("progress"), "book": book})
                return
            if parsed.path == "/api/books/progress/sync":
                result = sync.sync_book_progress(str(body.get("id") or ""))
                if not result.get("ok"):
                    self._json(400, {"error": result.get("error") or "同步阅读进度失败"})
                    return
                self._json(200, result)
                return
            if parsed.path == "/api/books/meta/sync":
                result = sync.sync_book_meta_field(
                    str(body.get("id") or ""),
                    str(body.get("field") or ""),
                )
                if not result.get("ok"):
                    self._json(400, {"error": result.get("error") or "同步失败"})
                    return
                self._json(200, result)
                return
            if parsed.path == "/api/entries/delete":
                store.delete_entry(str(body.get("id") or ""), str(body.get("date") or ""))
                self._json(200, {"ok": True})
                return
            if parsed.path == "/api/wallpaper/nudge":
                self._json(200, nudge_wallpaper_app())
                return
            if parsed.path == "/api/wallpaper/apply":
                self._json(200, apply_desktop_wallpaper(str(body.get("displayId") or "")))
                return
            if parsed.path == "/api/win/chrome":
                self._json(200, update_win_chrome(body if isinstance(body, dict) else {}))
                return
        except Exception as exc:
            self._json(400, {"error": str(exc)})
            return
        self._send(404, b"not found", "text/plain")


def data_stamp() -> dict:
    wall = store.wallpaper_target()
    wall_value = wall.get("value") or wall.get("mode")
    if wall_value in ("recent", "prev-recent") or wall.get("mode") in ("recent", "prev-recent"):
        payload = store.recent_payload(offset=int(wall.get("offset") or 0))
    else:
        payload = store.month_payload(wall["year"], wall["month"])
    pad = store.planner_pad()
    # Style fields are the bound device's copy, so a phone wallpaper change
    # does not move the Mac fingerprint and the reverse.
    style = {
        "uiTheme": store.ui_theme(),
        "uiGrade": store.ui_grade(),
        "plannerPad": {
            "landscape": pad.get("landscape") or store._pad_defaults("landscape"),
            "portrait": pad.get("portrait") or store._pad_defaults("portrait"),
            "track": pad.get("track") or store._pad_defaults("track"),
        },
        "calScroll": store.cal_scroll(),
        "calStats": store.cal_stats(),
        "calRecords": store.cal_records(),
        "calSplit": store.cal_split(),
        "plannerOrient": store.planner_orient().get("orient") or "landscape",
        "portraitRecRows": store.portrait_rec_rows().get("rows") or store.PORTRAIT_REC_ROWS_DEFAULT,
        "barRows": {k: store.bar_rows().get(k) for k in ("landscape", "portrait", "track")},
        "wallpaperValue": wall.get("value") or "current",
        "wallpaperOffset": int(wall.get("offset") or 0),
    }
    raw = json.dumps(
        {"payload": payload, "style": style},
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    now = datetime.now(TZ)
    return {
        "fingerprint": hashlib.sha256(raw).hexdigest(),
        "device": store.current_device(),
        "year": wall["year"],
        "month": wall["month"],
        "day": now.day,
        "wallpaperMode": wall["mode"],
        "wallpaperValue": wall["value"],
        "wallpaperOffset": int(wall.get("offset") or 0),
    }


def wallpaper_app_path() -> Path | None:
    candidates = (
        Path("/Applications/书历.app"),
        ROOT / "dist" / "书历.app",
        Path("/Applications/阅读记录.app"),
        ROOT / "dist" / "阅读记录.app",
    )
    for path in candidates:
        if path.exists():
            return path
    return None


def downloads_dir() -> Path:
    return Path.home() / "Downloads"


def normalize_image_save_dir(value) -> str:
    """Empty keeps the default Downloads folder. Otherwise store an absolute directory."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.home() / path
    try:
        path = path.resolve()
    except Exception as exc:
        raise ValueError("无法使用保存路径") from exc
    if path.exists() and not path.is_dir():
        raise ValueError("保存路径不是文件夹")
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError("无法创建保存路径") from exc
    if not path.is_dir():
        raise ValueError("保存路径不是文件夹")
    return str(path)


def image_save_folder() -> Path:
    raw = store.image_save_dir()
    if not raw:
        path = downloads_dir()
        path.mkdir(parents=True, exist_ok=True)
        return path
    return Path(normalize_image_save_dir(raw))


def export_location_label(folder: Path) -> str:
    try:
        if folder.resolve() == downloads_dir().resolve():
            return "下载"
    except Exception:
        pass
    return str(folder)


def pick_save_folder() -> dict:
    """Native folder panel. Cancel is not an error."""
    if sys.platform == "darwin":
        return _pick_save_folder_macos()
    if sys.platform == "win32":
        return _pick_save_folder_windows()
    return {"ok": False, "error": "当前系统无法打开文件夹选择"}


def _pick_save_folder_macos() -> dict:
    script = 'POSIX path of (choose folder with prompt "选择保存路径")'
    try:
        out = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "选择文件夹超时"}
    except OSError as exc:
        return {"ok": False, "error": str(exc) or "无法打开文件夹选择"}
    if out.returncode != 0:
        err = (out.stderr or "").strip()
        if "User canceled" in err or "(-128)" in err:
            return {"ok": False, "cancelled": True}
        return {"ok": False, "error": err or "无法选择文件夹"}
    path = (out.stdout or "").strip().rstrip("/")
    if not path:
        return {"ok": False, "cancelled": True}
    return {"ok": True, "path": path}


def _win_shuli_hwnd():
    """HWND of the WebView window, so the folder dialog opens in front of it."""
    import ctypes

    user32 = ctypes.windll.user32
    get_text = ctypes.WINFUNCTYPE(
        ctypes.c_int, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int
    )(("GetWindowTextW", user32))
    visible = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p)(("IsWindowVisible", user32))
    found = ctypes.c_void_p()

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def visit(hwnd, _lparam):
        if not visible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(32)
        get_text(hwnd, buf, 32)
        if buf.value == "书历":
            found.value = hwnd
            return False
        return True

    enum = ctypes.WINFUNCTYPE(
        ctypes.c_bool,
        ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p),
        ctypes.c_void_p,
    )(("EnumWindows", user32))
    enum(visit, None)
    return found.value or None


def _win_folder_owner():
    """Topmost popup in this process, so the dialog opens in front of WebView2."""
    import ctypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    create = ctypes.WINFUNCTYPE(
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_ulong,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    )(("CreateWindowExW", user32))
    module = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_wchar_p)(("GetModuleHandleW", kernel32))
    foreground = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p)(("SetForegroundWindow", user32))
    get_fg = ctypes.WINFUNCTYPE(ctypes.c_void_p)(("GetForegroundWindow", user32))
    get_tid = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p)(("GetWindowThreadProcessId", user32))
    get_cur = ctypes.WINFUNCTYPE(ctypes.c_ulong)(("GetCurrentThreadId", kernel32))
    attach = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_bool)(("AttachThreadInput", user32))
    hwnd = create(
        0x00000008 | 0x00000080,  # WS_EX_TOPMOST | WS_EX_TOOLWINDOW
        "Static",
        "选择保存路径",
        0x80000000,  # WS_POPUP
        0, 0, 0, 0,
        None, None, module(None), None,
    )
    if not hwnd:
        return None
    fg = get_fg()
    if fg:
        fg_tid = get_tid(fg, None)
        cur = get_cur()
        if fg_tid and fg_tid != cur:
            attach(cur, fg_tid, True)
            foreground(hwnd)
            attach(cur, fg_tid, False)
        else:
            foreground(hwnd)
    return hwnd


def _pick_save_folder_windows() -> dict:
    """IFileDialog folder picker. The server is a hidden pythonw process."""
    import ctypes
    import uuid

    ole32 = ctypes.windll.ole32
    HR_CANCEL = 0x800704C7

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_ulong),
            ("Data2", ctypes.c_ushort),
            ("Data3", ctypes.c_ushort),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    def make_guid(text: str) -> GUID:
        parsed = uuid.UUID(text)
        item = GUID()
        item.Data1 = parsed.time_low
        item.Data2 = parsed.time_mid
        item.Data3 = parsed.time_hi_version
        for i, byte in enumerate(parsed.bytes[8:]):
            item.Data4[i] = byte
        return item

    def com_call(this, index, restype, *argtypes):
        proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
        vtbl = ctypes.cast(this, ctypes.POINTER(ctypes.c_void_p))[0]
        addr = ctypes.cast(vtbl, ctypes.POINTER(ctypes.c_void_p))[index]
        return proto(addr)

    def failed(hr) -> bool:
        return (int(hr) & 0xFFFFFFFF) >= 0x80000000

    def cancelled(hr) -> bool:
        return (int(hr) & 0xFFFFFFFF) == HR_CANCEL

    co_init = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_ulong)(("CoInitializeEx", ole32))
    co_create = ctypes.WINFUNCTYPE(
        ctypes.c_long,
        ctypes.POINTER(GUID),
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(GUID),
        ctypes.POINTER(ctypes.c_void_p),
    )(("CoCreateInstance", ole32))
    co_free = ctypes.WINFUNCTYPE(None, ctypes.c_void_p)(("CoTaskMemFree", ole32))
    co_uninit = ctypes.WINFUNCTYPE(None)(("CoUninitialize", ole32))

    hr_init = co_init(None, 2)  # COINIT_APARTMENTTHREADED
    # S_FALSE (1) means this thread was already initialized.
    if failed(hr_init) and (int(hr_init) & 0xFFFFFFFF) != 0x80010106:
        return {"ok": False, "error": "无法打开文件夹选择"}
    dialog = ctypes.c_void_p()
    item = ctypes.c_void_p()
    popup = None
    destroy_popup = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p)(("DestroyWindow", ctypes.windll.user32))
    try:
        clsid = make_guid("DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7")
        iid = make_guid("D57C7288-D4AD-4768-BE02-9D969532D960")
        created = co_create(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(dialog))
        if failed(created) or not dialog.value:
            return {"ok": False, "error": "无法打开文件夹选择"}
        opts = ctypes.c_ulong()
        if not failed(com_call(dialog, 10, ctypes.c_long, ctypes.POINTER(ctypes.c_ulong))(dialog, ctypes.byref(opts))):
            # FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST
            opts.value |= 0x20 | 0x40 | 0x800
            com_call(dialog, 9, ctypes.c_long, ctypes.c_ulong)(dialog, opts.value)
        com_call(dialog, 17, ctypes.c_long, ctypes.c_wchar_p)(dialog, "选择保存路径")
        popup = _win_folder_owner()
        owner = popup or _win_shuli_hwnd()
        shown = com_call(dialog, 3, ctypes.c_long, ctypes.c_void_p)(dialog, owner)
        if cancelled(shown):
            return {"ok": False, "cancelled": True}
        if failed(shown) and owner:
            shown = com_call(dialog, 3, ctypes.c_long, ctypes.c_void_p)(dialog, None)
        if cancelled(shown):
            return {"ok": False, "cancelled": True}
        if failed(shown):
            return {"ok": False, "error": "无法打开文件夹选择"}
        got = com_call(dialog, 20, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))(dialog, ctypes.byref(item))
        if failed(got) or not item.value:
            return {"ok": False, "error": "无法选择文件夹"}
        name = ctypes.c_void_p()
        named = com_call(item, 5, ctypes.c_long, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p))(
            item, 0x80058000, ctypes.byref(name)
        )
        if failed(named) or not name.value:
            return {"ok": False, "error": "无法选择文件夹"}
        try:
            path = ctypes.wstring_at(name.value).strip()
        finally:
            co_free(name)
        if not path:
            return {"ok": False, "cancelled": True}
        return {"ok": True, "path": path}
    except Exception:
        return {"ok": False, "error": "无法打开文件夹选择"}
    finally:
        if item.value:
            com_call(item, 2, ctypes.c_ulong)(item)
        if dialog.value:
            com_call(dialog, 2, ctypes.c_ulong)(dialog)
        if popup:
            destroy_popup(popup)
            shuli = _win_shuli_hwnd()
            if shuli:
                ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p)(
                    ("SetForegroundWindow", ctypes.windll.user32)
                )(shuli)
        if not failed(hr_init):
            co_uninit()


def unique_export_path(directory: Path, name: str) -> Path:
    dest = directory / name
    if not dest.exists():
        return dest
    stem = dest.stem
    suffix = dest.suffix
    i = 2
    while True:
        cand = directory / ("%s-%s%s" % (stem, i, suffix))
        if not cand.exists():
            return cand
        i += 1


def sanitize_export_name(name: str, fallback: str = "书历.png") -> str:
    name = os.path.basename(str(name or "")).strip()
    name = EXPORT_NAME_RE.sub("", name)
    if not name.lower().endswith(".png"):
        name = (name.rstrip(".") or fallback[:-4]) + ".png"
    stem = name[:-4].strip(" .")
    if not stem:
        return fallback
    return stem[:60] + ".png"


def _decode_png_b64(png_b64: str, max_bytes: int) -> bytes:
    raw = (png_b64 or "").strip()
    if "," in raw and raw[:5].lower() == "data:":
        raw = raw.split(",", 1)[1]
    raw = "".join(raw.split())
    try:
        data = base64.b64decode(raw, validate=False)
    except Exception as exc:
        raise ValueError("无法解码图片") from exc
    if not data or len(data) < 32:
        raise ValueError("图片为空")
    if len(data) > max_bytes:
        raise ValueError("图片太大")
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("不是 PNG 图片")
    return data


def save_export_png(filename: str, png_b64: str) -> dict:
    data = _decode_png_b64(png_b64, MAX_EXPORT_PNG)
    folder = image_save_folder()
    folder.mkdir(parents=True, exist_ok=True)
    dest = unique_export_path(folder, sanitize_export_name(filename))
    dest.write_bytes(data)
    return {
        "ok": True,
        "path": str(dest),
        "filename": dest.name,
        "bytes": len(data),
        "locationLabel": export_location_label(folder),
    }


def log_wallpaper_progress(step: str) -> None:
    """Append JS capture stages to wallpaper.log so hangs show the last step."""
    msg = (step or "").strip() or "(empty)"
    try:
        store.DATA_DIR.mkdir(parents=True, exist_ok=True)
        path = store.DATA_DIR / "wallpaper.log"
        line = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + " js " + msg + "\n"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        pass
    print("[wallpaper] " + msg, flush=True)


# B7 书历竖版设计默认（与 index.html PORTRAIT_PIXEL_* 一致）。
# Lock PNG must use app wall settings from the phone — never Hibreak wm size.
B7_WALL_WIDTH = 2160
B7_WALL_HEIGHT = 2880
B7_WALL_MAG = 1


def _qs_int(qs: dict, key: str, default: int = 0) -> int:
    try:
        raw = (qs.get(key) or [""])[0]
        n = int(float(str(raw).strip()))
        return n
    except Exception:
        return default


def save_wallpaper_capture(png_b64: str) -> dict:
    """Write capture for the Mac app wallpaper path (avoid huge JS→Swift base64 IPC)."""
    data = _decode_png_b64(png_b64, MAX_WALLPAPER_PNG)
    store.DATA_DIR.mkdir(parents=True, exist_ok=True)
    # B7 lock / device=b7 offscreen capture must not overwrite Mac desktop PNG.
    name = "wallpaper-b7-capture.png" if store.current_device() == "b7" else "wallpaper-capture.png"
    dest = store.DATA_DIR / name
    dest.write_bytes(data)
    log_wallpaper_progress("capture-saved %s %sb device=%s" % (dest.name, len(data), store.current_device()))
    return {"ok": True, "path": str(dest), "filename": dest.name, "bytes": len(data)}


def _wallpaper_png_candidates(device: str = "mac") -> list:
    """Mac desktop vs B7 lock PNG files (kept separate)."""
    if store.normalize_device(device) == "b7":
        return [
            store.DATA_DIR / "wallpaper-b7-capture.png",
            store.DATA_DIR / "wallpaper-b7.png",
        ]
    return [
        store.DATA_DIR / "wallpaper-capture.png",
        store.DATA_DIR / "wallpaper.png",
    ]


def _read_best_wallpaper_png(device: str = "mac") -> tuple[bytes, Path]:
    best = None
    best_mtime = -1.0
    for path in _wallpaper_png_candidates(device):
        try:
            if not path.is_file() or path.stat().st_size < 32:
                continue
            mtime = path.stat().st_mtime
            if mtime >= best_mtime:
                best = path
                best_mtime = mtime
        except Exception:
            continue
    if best is None:
        if store.normalize_device(device) == "b7":
            raise FileNotFoundError("还没有 B7 壁纸 PNG（请先锁屏刷新或 fresh=1）")
        raise FileNotFoundError("还没有壁纸 PNG（请先在 Mac 上生成一次）")
    data = best.read_bytes()
    if len(data) < 32 or data[0] != 0x89 or data[1] != 0x50:
        raise ValueError("壁纸文件不是 PNG")
    return data, best


def load_wallpaper_png(
    fresh: bool = False,
    wait_seconds: float = 55.0,
    device: str = "b7",
    width: int = 0,
    height: int = 0,
    mag: int = 0,
    orient: str = "",
) -> tuple[bytes, dict]:
    """
    Serve a calendar PNG for Hibreak lock refresh (no Android WebView).

    device=b7 (lock path): nudge wallpaper-b7.nudge so Mac renders B7 size/mode
    into wallpaper-b7-capture.png without changing the Mac desktop wallpaper.

    device=mac: legacy path — wallpaper.nudge → desktop capture files.
    """
    device = store.normalize_device(device)
    capture = _wallpaper_png_candidates(device)[0]
    before_mtime = -1.0
    before_size = -1
    if capture.is_file():
        try:
            st = capture.stat()
            before_mtime = st.st_mtime
            before_size = st.st_size
        except Exception:
            pass

    refreshed = False
    if fresh:
        try:
            if device == "b7":
                nudge_b7_wallpaper(
                    width=width,
                    height=height,
                    mag=mag,
                    orient=orient,
                )
                log_wallpaper_progress(
                    "png-api b7 nudge fresh=1 %sx%s mag=%s orient=%s"
                    % (width or B7_WALL_WIDTH, height or B7_WALL_HEIGHT, mag or B7_WALL_MAG, orient or "-")
                )
            else:
                nudge_wallpaper_app()
                log_wallpaper_progress("png-api nudge fresh=1 device=mac")
        except Exception as exc:
            log_wallpaper_progress("png-api nudge failed: %s" % exc)
        deadline = time.time() + max(3.0, float(wait_seconds or 55.0))
        watch = _wallpaper_png_candidates(device)
        while time.time() < deadline:
            try:
                for path in watch:
                    if not path.is_file():
                        continue
                    st = path.stat()
                    if st.st_size < 32:
                        continue
                    # Any candidate newer/larger than the pre-nudge capture counts.
                    if st.st_mtime > before_mtime + 0.05 or (
                        path == capture and st.st_size != before_size
                    ):
                        time.sleep(0.15)
                        refreshed = True
                        break
                if refreshed:
                    break
            except Exception:
                pass
            time.sleep(0.4)

    data, path = _read_best_wallpaper_png(device)
    log_wallpaper_progress(
        "png-api serve %s %sb device=%s fresh=%s refreshed=%s"
        % (path.name, len(data), device, int(bool(fresh)), int(refreshed))
    )
    return data, {
        "ok": True,
        "path": str(path),
        "bytes": len(data),
        "fresh": bool(fresh),
        "refreshed": refreshed,
        "device": device,
    }


def _enable_windows_dpi() -> None:
    if sys.platform != "win32":
        return
    import ctypes
    try:
        user32 = ctypes.windll.user32
        user32.SetProcessDpiAwarenessContext.restype = ctypes.c_bool
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


_enable_windows_dpi()


def _win_chrome_path() -> Path:
    return store.DATA_DIR / "win-chrome.json"


def load_win_chrome() -> dict:
    try:
        data = json.loads(_win_chrome_path().read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_win_chrome(data: dict) -> None:
    store.DATA_DIR.mkdir(parents=True, exist_ok=True)
    _win_chrome_path().write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def reading_revision() -> str:
    db = store.DATA_DIR / "reading.db"
    try:
        st = db.stat()
    except OSError:
        return "0"
    return "%s:%s" % (getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)), st.st_size)


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def launcher_exe() -> str:
    env = os.environ.get("READING_LOG_EXE") or ""
    if env and os.path.isfile(env):
        return env
    cand = ROOT.parent / "书历.exe"
    if cand.is_file():
        return str(cand)
    return env


def login_item_enabled() -> bool:
    if sys.platform != "win32":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            val, _ = winreg.QueryValueEx(key, "书历")
            return bool(str(val or "").strip())
    except OSError:
        return False


def set_login_item(enabled: bool) -> None:
    if sys.platform != "win32":
        raise ValueError("开机启动只在 Windows 上可用")
    import winreg
    exe = launcher_exe()
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
        if enabled:
            if not exe or not os.path.isfile(exe):
                raise ValueError("找不到书历程序，无法设置开机启动")
            winreg.SetValueEx(key, "书历", 0, winreg.REG_SZ, '"%s"' % exe)
        else:
            try:
                winreg.DeleteValue(key, "书历")
            except OSError:
                pass


def _norm_dev(path: str) -> str:
    text = str(path or "").strip().upper().replace("/", "\\")
    for prefix in ("\\\\?\\", "\\\\.\\"):
        if text.startswith(prefix):
            return text[len(prefix):]
    return text


def _display_friendly_names() -> dict:
    """Map a normalized device path to the monitor's friendly name. Windows only."""
    if sys.platform != "win32":
        return {}
    import ctypes
    from ctypes import wintypes

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

    class SOURCE(ctypes.Structure):
        _fields_ = [
            ("adapterId", LUID),
            ("id", wintypes.UINT),
            ("modeInfoIdx", wintypes.UINT),
            ("statusFlags", wintypes.UINT),
        ]

    class TARGET(ctypes.Structure):
        _fields_ = [
            ("adapterId", LUID),
            ("id", wintypes.UINT),
            ("modeInfoIdx", wintypes.UINT),
            ("outputTechnology", wintypes.UINT),
            ("rotation", wintypes.UINT),
            ("scaling", wintypes.UINT),
            ("refreshNum", wintypes.UINT),
            ("refreshDen", wintypes.UINT),
            ("scanLineOrdering", wintypes.UINT),
            ("targetAvailable", wintypes.BOOL),
            ("statusFlags", wintypes.UINT),
        ]

    class PATH_INFO(ctypes.Structure):
        _fields_ = [("sourceInfo", SOURCE), ("targetInfo", TARGET), ("flags", wintypes.UINT)]

    class MODE_INFO(ctypes.Structure):
        _fields_ = [
            ("infoType", wintypes.UINT),
            ("id", wintypes.UINT),
            ("adapterId", LUID),
            ("mode", ctypes.c_byte * 128),
        ]

    class HEADER(ctypes.Structure):
        _fields_ = [
            ("type", wintypes.UINT),
            ("size", wintypes.UINT),
            ("adapterId", LUID),
            ("id", wintypes.UINT),
        ]

    class TARGET_NAME(ctypes.Structure):
        _fields_ = [
            ("header", HEADER),
            ("flags", wintypes.UINT),
            ("outputTechnology", wintypes.UINT),
            ("edidManufactureId", wintypes.WORD),
            ("edidProductCodeId", wintypes.WORD),
            ("connectorInstance", wintypes.UINT),
            ("monitorFriendlyDeviceName", wintypes.WCHAR * 64),
            ("monitorDevicePath", wintypes.WCHAR * 128),
        ]

    user32 = ctypes.windll.user32
    get_sizes = ctypes.WINFUNCTYPE(
        ctypes.c_long, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)
    )(("GetDisplayConfigBufferSizes", user32))
    query = ctypes.WINFUNCTYPE(
        ctypes.c_long,
        ctypes.c_uint,
        ctypes.POINTER(ctypes.c_uint),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint),
        ctypes.c_void_p,
        ctypes.c_void_p,
    )(("QueryDisplayConfig", user32))
    get_info = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(("DisplayConfigGetDeviceInfo", user32))
    names = {}
    try:
        path_count = ctypes.c_uint(0)
        mode_count = ctypes.c_uint(0)
        if get_sizes(0x2, ctypes.byref(path_count), ctypes.byref(mode_count)) != 0:
            return {}
        if path_count.value == 0 or path_count.value > 32 or mode_count.value > 64:
            return {}
        paths = (PATH_INFO * path_count.value)()
        modes = (MODE_INFO * max(1, mode_count.value))()
        if query(0x2, ctypes.byref(path_count), ctypes.byref(paths), ctypes.byref(mode_count), ctypes.byref(modes), None) != 0:
            return {}
        for i in range(path_count.value):
            info = TARGET_NAME()
            info.header.type = 2
            info.header.size = ctypes.sizeof(TARGET_NAME)
            info.header.adapterId = paths[i].targetInfo.adapterId
            info.header.id = paths[i].targetInfo.id
            if get_info(ctypes.byref(info)) != 0:
                continue
            friendly = str(info.monitorFriendlyDeviceName or "").strip()
            device = str(info.monitorDevicePath or "").strip()
            if friendly and device:
                names[_norm_dev(device)] = friendly
    except Exception:
        return {}
    return names


def _com_release(this) -> None:
    import ctypes
    if not this:
        return
    vtbl = ctypes.cast(this, ctypes.POINTER(ctypes.c_void_p))[0]
    addr = ctypes.cast(vtbl, ctypes.POINTER(ctypes.c_void_p))[2]
    ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(addr)(this)


def _wallpaper_interface():
    """Return (interface pointer, uninit callable) for IDesktopWallpaper."""
    import ctypes
    import uuid

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_ulong),
            ("Data2", ctypes.c_ushort),
            ("Data3", ctypes.c_ushort),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    def make_guid(text: str) -> GUID:
        parsed = uuid.UUID(text)
        item = GUID()
        item.Data1 = parsed.time_low
        item.Data2 = parsed.time_mid
        item.Data3 = parsed.time_hi_version
        for i, byte in enumerate(parsed.bytes[8:]):
            item.Data4[i] = byte
        return item

    ole32 = ctypes.windll.ole32
    co_init = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_ulong)(("CoInitializeEx", ole32))
    co_create = ctypes.WINFUNCTYPE(
        ctypes.c_long,
        ctypes.POINTER(GUID),
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(GUID),
        ctypes.POINTER(ctypes.c_void_p),
    )(("CoCreateInstance", ole32))
    co_uninit = ctypes.WINFUNCTYPE(None)(("CoUninitialize", ole32))
    hr = co_init(None, 0)
    own = hr in (0, 1)
    clsid = make_guid("C2CF3110-460E-4FC1-B9D0-8A1C0C9CC4BD")
    iid = make_guid("B92B56A9-8B55-4E14-9A89-0199BBB6F93B")
    punk = ctypes.c_void_p()
    created = co_create(ctypes.byref(clsid), None, 23, ctypes.byref(iid), ctypes.byref(punk))
    if created < 0 or not punk.value:
        if own:
            co_uninit()
        raise OSError("无法读取显示器（%s）" % hex(created & 0xFFFFFFFF))

    def uninit():
        _com_release(punk.value)
        if own:
            co_uninit()

    return punk.value, uninit


def _vtbl(this, index, proto):
    import ctypes
    vtbl = ctypes.cast(this, ctypes.POINTER(ctypes.c_void_p))[0]
    addr = ctypes.cast(vtbl, ctypes.POINTER(ctypes.c_void_p))[index]
    return proto(addr)


def list_windows_displays() -> list:
    if sys.platform != "win32":
        return []
    import ctypes

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    this = None
    uninit = None
    try:
        this, uninit = _wallpaper_interface()
        count = ctypes.c_uint(0)
        hr = _vtbl(
            this, 6, ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint))
        )(this, ctypes.byref(count))
        if hr < 0:
            raise OSError("无法列出显示器")
        names = _display_friendly_names()
        free_mem = ctypes.windll.ole32.CoTaskMemFree
        free_mem.argtypes = [ctypes.c_void_p]
        free_mem.restype = None
        displays = []
        for i in range(min(int(count.value), 16)):
            slot = ctypes.c_void_p()
            hr = _vtbl(
                this,
                5,
                ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)),
            )(this, i, ctypes.byref(slot))
            if hr < 0 or not slot.value:
                continue
            device = ctypes.wstring_at(slot.value)
            free_mem(slot)
            rect = RECT()
            hr = _vtbl(
                this,
                7,
                ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.POINTER(RECT)),
            )(this, device, ctypes.byref(rect))
            width = max(0, int(rect.right - rect.left)) if hr >= 0 else 0
            height = max(0, int(rect.bottom - rect.top)) if hr >= 0 else 0
            primary = hr >= 0 and rect.left == 0 and rect.top == 0
            friendly = names.get(_norm_dev(device)) or ("显示器 %d" % (len(displays) + 1))
            shown = friendly + ("（主屏幕）" if primary else "")
            label = shown if width <= 0 or height <= 0 else "%s  %d×%d" % (shown, int(width), int(height))
            displays.append({
                "id": device,
                "name": friendly,
                "label": label,
                "width": width,
                "height": height,
                "primary": primary,
            })
        return displays
    finally:
        if uninit:
            uninit()


def resolve_display_id(displays: list, saved: dict) -> str:
    ids = [str(item.get("id") or "") for item in displays if item.get("id")]
    want = str(saved.get("displayId") or "")
    if want and want in ids:
        return want
    name = str(saved.get("displayName") or "").strip()
    if name:
        for item in displays:
            if str(item.get("name") or "") == name and item.get("id"):
                return str(item["id"])
    for item in displays:
        if item.get("primary") and item.get("id"):
            return str(item["id"])
    return ids[0] if ids else ""


def win_chrome_state() -> dict:
    saved = load_win_chrome()
    rev = reading_revision()
    if sys.platform != "win32":
        return {
            "ok": True,
            "windows": False,
            "displays": [],
            "displayId": "",
            "autoUpdate": False,
            "login": False,
            "rev": rev,
        }
    try:
        displays = list_windows_displays()
    except Exception as exc:
        log_wallpaper_progress("displays " + str(exc))
        displays = []
    chosen = resolve_display_id(displays, saved)
    auto = saved.get("autoUpdate", True)
    return {
        "ok": True,
        "windows": True,
        "displays": displays,
        "displayId": chosen,
        "autoUpdate": _as_bool(auto) if "autoUpdate" in saved else True,
        "login": login_item_enabled(),
        "rev": rev,
    }


def update_win_chrome(body: dict) -> dict:
    if sys.platform != "win32":
        raise ValueError("显示器设置只在 Windows 上可用")
    saved = load_win_chrome()
    if "displayId" in body:
        display_id = str(body.get("displayId") or "").replace("\x00", "").strip()
        if len(display_id) > 512:
            display_id = ""
        saved["displayId"] = display_id
    if "displayName" in body:
        saved["displayName"] = str(body.get("displayName") or "").strip()[:120]
    if "autoUpdate" in body:
        saved["autoUpdate"] = _as_bool(body.get("autoUpdate"))
    if "login" in body:
        set_login_item(_as_bool(body.get("login")))
    save_win_chrome(saved)
    return win_chrome_state()


def _set_wallpaper_on_monitor(path: str, monitor_id: str) -> None:
    import ctypes
    this, uninit = _wallpaper_interface()
    try:
        hr = _vtbl(
            this, 10, ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_uint)
        )(this, 4)
        if hr < 0:
            log_wallpaper_progress("wallpaper position %s" % hex(hr & 0xFFFFFFFF))
        hr = _vtbl(
            this, 3, ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p)
        )(this, monitor_id or None, path)
        if hr < 0:
            raise OSError("系统没有把壁纸放到所选显示器（%s）" % hex(hr & 0xFFFFFFFF))
    finally:
        uninit()


def _open_wallpaper_app() -> Path | None:
    app = wallpaper_app_path()
    if app is not None:
        subprocess.Popen(["/usr/bin/open", "-g", "-a", str(app)], close_fds=True)
    return app


def nudge_wallpaper_app() -> dict:
    """Ask the Mac app to re-render and apply the desktop wallpaper (Mac size/mode)."""
    store.DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = store.DATA_DIR / "wallpaper.nudge"
    path.write_text(str(time.time()), encoding="utf-8")
    app = _open_wallpaper_app()
    return {"ok": True, "nudge": str(path), "app": str(app) if app else ""}


def nudge_b7_wallpaper(
    width: int = 0,
    height: int = 0,
    mag: int = 0,
    orient: str = "",
) -> dict:
    """
    Ask the Mac app to render a B7 lock PNG only (no desktop setDesktopImage).
    Writes wallpaper-b7.nudge JSON; Mac watches that file separately from wallpaper.nudge.
    """
    store.DATA_DIR.mkdir(parents=True, exist_ok=True)
    w = int(width or 0)
    h = int(height or 0)
    m = int(mag or 0)
    if w < 320:
        w = B7_WALL_WIDTH
    if h < 200:
        h = B7_WALL_HEIGHT
    if m < 1 or m > 4:
        m = B7_WALL_MAG
    o = str(orient or "").strip().lower()
    if o in ("竖版", "vertical"):
        o = "portrait"
    elif o in ("横版", "horizontal"):
        o = "landscape"
    elif o not in ("portrait", "landscape"):
        o = ""
    payload = {
        "device": "b7",
        "width": w,
        "height": h,
        "mag": m,
        "orient": o,
        "ts": time.time(),
    }
    path = store.DATA_DIR / "wallpaper-b7.nudge"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    app = _open_wallpaper_app()
    return {"ok": True, "nudge": str(path), "app": str(app) if app else "", **payload}


def apply_desktop_wallpaper(monitor_id: str = "") -> dict:
    """Set the last captured PNG as the desktop wallpaper on the chosen display."""
    src = store.DATA_DIR / "wallpaper-capture.png"
    if not src.is_file() or src.stat().st_size < 32:
        raise ValueError("还没有壁纸图片")
    if sys.platform != "win32":
        return nudge_wallpaper_app()
    dest = store.DATA_DIR / "wallpaper.bmp"
    try:
        from PIL import Image
        img = Image.open(src).convert("RGB")
        img.save(dest, "BMP")
        wall = dest
    except Exception:
        wall = src
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop", 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "WallpaperStyle", 0, winreg.REG_SZ, "10")
            winreg.SetValueEx(key, "TileWallpaper", 0, winreg.REG_SZ, "0")
    except Exception:
        pass
    chosen = str(monitor_id or "").replace("\x00", "").strip()
    if not chosen:
        chosen = str(load_win_chrome().get("displayId") or "").strip()
    if chosen:
        _set_wallpaper_on_monitor(str(wall), chosen)
        log_wallpaper_progress("applied %s display" % wall.name)
        return {"ok": True, "path": str(wall), "displayId": chosen}
    import ctypes
    flags = 0x01 | 0x02
    spi = ctypes.windll.user32.SystemParametersInfoW
    spi.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_uint]
    spi.restype = ctypes.c_bool
    ok = spi(20, 0, str(wall), flags)
    if not ok:
        raise OSError("系统没有接受这张壁纸")
    log_wallpaper_progress("applied %s" % wall)
    return {"ok": True, "path": str(wall), "displayId": ""}


def app_url() -> str:
    return "http://%s:%s/" % (HOST, PORT)


def lan_hosts() -> list[str]:
    import socket

    found: list[str] = []

    def add(ip: str) -> None:
        if not ip or ip.startswith("127.") or ip in found:
            return
        found.append(ip)

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            add(sock.getsockname()[0])
        finally:
            sock.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(info[4][0])
    except Exception:
        pass
    return found


def publish_lan_urls() -> list[str]:
    lines = [app_url()]
    for ip in lan_hosts():
        lines.append("http://%s:%s/" % (ip, PORT))
    try:
        path = store.DATA_DIR / "lan-url.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass
    return lines


def owned_by_mac_app() -> bool:
    if "--owned" in sys.argv:
        return True
    return os.environ.get("READING_LOG_OWNED") == "1"


def skip_browser() -> bool:
    return owned_by_mac_app() or os.environ.get("READING_LOG_NO_BROWSER") == "1"


def existing_app_alive() -> bool:
    try:
        with urlopen(app_url() + "api/status", timeout=1) as resp:
            return resp.status == 200
    except Exception:
        return False


def launched_under_cursor() -> bool:
    pid = os.getppid()
    for _ in range(16):
        if pid <= 1:
            return False
        try:
            out = subprocess.check_output(["ps", "-o", "ppid=,command=", "-p", str(pid)], text=True).strip()
        except Exception:
            return False
        if not out:
            return False
        parts = out.split(None, 1)
        if len(parts) < 2:
            return False
        try:
            pid = int(parts[0])
        except ValueError:
            return False
        cmd = parts[1]
        if "/Cursor.app/" in cmd or "Cursor Helper" in cmd:
            return True
    return False


def handoff_to_terminal() -> None:
    """Cursor 子进程关聊天就会被 SIGTERM；交给双击同样的 start.command。"""
    script = ROOT / "start.command"
    subprocess.Popen(["open", "-a", "Terminal", str(script)], close_fds=True)
    print("已交给「终端」启动，关掉 Cursor 也不会退出。")


def bind_http_server():
    ThreadingHTTPServer.allow_reuse_address = True
    last = None
    for _ in range(40):
        try:
            return ThreadingHTTPServer((BIND_HOST, PORT), Handler)
        except OSError as exc:
            last = exc
            if existing_app_alive():
                return None
            time.sleep(0.25)
    sys.exit("无法绑定 %s:%s：%s" % (BIND_HOST, PORT, last))


def main():
    if not (STATIC / "index.html").exists():
        sys.exit("缺少 static/index.html")
    store.connect().close()
    url = app_url()
    owned = owned_by_mac_app()
    if existing_app_alive():
        print("书历已在运行：%s" % url, flush=True)
        if not skip_browser():
            webbrowser.open(url)
        return
    if not owned and launched_under_cursor():
        handoff_to_terminal()
        return
    server = bind_http_server()
    if server is None:
        print("书历已在运行：%s" % url, flush=True)
        if not skip_browser():
            webbrowser.open(url)
        return
    print("书历已启动：%s" % url, flush=True)
    for line in publish_lan_urls():
        if line != url:
            print("手机可打开：%s" % line, flush=True)
    if not owned:
        print("关闭这个窗口即可退出。", flush=True)
        if not skip_browser():
            webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
