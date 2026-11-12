#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""掌阅 PC 网页登录（pc.ireader.com 同一套公开接口：APP 扫码）。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request

DATA_DIR = Path.home() / ".reading-log"
SESSION_PATH = DATA_DIR / "ireader-session.json"
WEB_ORIGIN = "https://pc.ireader.com"
API_ORIGIN = "https://api-pc.zhangyue.com"
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
# 与 pc.ireader.com 页面 window.__NUXT__.config 一致。
CLIENT_PARAMS = {
    "p29": "zye5b814",
    "p2": "104583",
    "p3": "101010010",
    "p4": "501603",
    "p33": "com.chaozh.iReaderFree",
}
SUCCESS = 0
WAIT_LOGIN = 30059
LOGIN_EXPIRED = 600001
QR_PAYLOAD_PREFIX = "web_login:"

_qr_state: dict = {}


class SessionExpired(RuntimeError):
    pass


def _now() -> int:
    return int(time.time())


def parse_cookie_string(raw: str) -> dict:
    cookies = {}
    text = (raw or "").strip()
    if not text:
        return cookies
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            obj = {}
        if isinstance(obj, dict):
            for key in ("usr", "zysid", "name", "nick", "avatar", "session_id"):
                val = obj.get(key)
                if val:
                    cookies[str(key)] = str(val)
            if cookies.get("session_id") and not cookies.get("zysid"):
                cookies["zysid"] = cookies["session_id"]
            return cookies
    for part in text.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key.lower() in {"path", "domain", "expires", "max-age", "secure", "httponly", "samesite"}:
            continue
        if key:
            cookies[key] = value
    return cookies


def _merge_set_cookie(cookies: dict, headers) -> None:
    raw_list = []
    if hasattr(headers, "get_all"):
        raw_list = headers.get_all("Set-Cookie") or []
    elif headers.get("Set-Cookie"):
        raw_list = [headers.get("Set-Cookie")]
    for item in raw_list:
        first = (item or "").split(";", 1)[0]
        if "=" not in first:
            continue
        key, value = first.split("=", 1)
        key = key.strip()
        if key:
            cookies[key] = value.strip()


def load_session() -> dict:
    if not SESSION_PATH.exists():
        return {}
    try:
        data = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    cookies = data.get("cookies") or {}
    if not isinstance(cookies, dict):
        cookies = {}
    data["cookies"] = {str(k): str(v) for k, v in cookies.items() if k and v}
    return data


def save_session(data: dict) -> None:
    SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "cookies": data.get("cookies") or {},
        "name": data.get("name") or "",
        "usr": str(data.get("usr") or ""),
        "zysid": str(data.get("zysid") or ""),
        "avatar": data.get("avatar") or "",
        "updated_at": int(data.get("updated_at") or _now()),
    }
    SESSION_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    try:
        SESSION_PATH.chmod(0o600)
    except OSError:
        pass


def clear_session() -> None:
    global _qr_state
    _qr_state = {}
    if SESSION_PATH.exists():
        SESSION_PATH.unlink()


def has_session(data: Optional[dict] = None) -> bool:
    data = data if data is not None else load_session()
    cookies = data.get("cookies") or {}
    usr = str(data.get("usr") or cookies.get("usr") or "")
    zysid = str(data.get("zysid") or cookies.get("zysid") or "")
    return bool(usr and zysid)


def status() -> dict:
    data = load_session()
    ok = has_session(data)
    return {
        "loggedIn": ok,
        "name": data.get("name") or "",
        "usr": str(data.get("usr") or ""),
        "updatedAt": data.get("updated_at") or 0,
        "sessionPath": str(SESSION_PATH),
        "dailyMinutes": False,
        "hint": "登录后可同步云书架；网页没有按日阅读分钟接口，日历请用 CSV。",
    }


def _request(
    path: str,
    cookies: dict,
    params: Optional[dict] = None,
    data: Optional[bytes] = None,
    headers: Optional[dict] = None,
    timeout: int = 20,
    method: Optional[str] = None,
) -> tuple[int, dict, bytes, dict]:
    query = dict(CLIENT_PARAMS)
    if cookies.get("usr"):
        query["usr"] = cookies["usr"]
    if cookies.get("zysid"):
        query["zysid"] = cookies["zysid"]
    if params:
        query.update({k: v for k, v in params.items() if v is not None})
    url = path if path.startswith("http") else API_ORIGIN + path
    sep = "&" if "?" in url else "?"
    url = url + sep + urlencode(query)
    hdrs = {
        "User-Agent": BROWSER_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept-Encoding": "identity",
        "Origin": WEB_ORIGIN,
        "Referer": WEB_ORIGIN + "/login",
    }
    if headers:
        hdrs.update(headers)
    if method is None:
        method = "POST" if data is not None else "GET"
    req = Request(url, data=data, headers=hdrs, method=method)
    try:
        import weread

        with weread.open_url(req, timeout=timeout) as resp:
            body = resp.read()
            merged = dict(cookies)
            _merge_set_cookie(merged, resp.headers)
            return resp.status, dict(resp.headers), body, merged
    except HTTPError as exc:
        body = b""
        try:
            body = exc.read() or b""
        except Exception:
            pass
        merged = dict(cookies)
        if exc.headers:
            _merge_set_cookie(merged, exc.headers)
        return exc.code, dict(exc.headers or {}), body, merged


def _decode_json(body: bytes) -> dict:
    if not body:
        return {}
    text = body.decode("utf-8", errors="replace")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise RuntimeError("掌阅网页接口返回无法解析")
    if not isinstance(data, dict):
        return {}
    return data


def api_call(
    path: str,
    cookies: Optional[dict] = None,
    params: Optional[dict] = None,
    payload: Optional[dict] = None,
    timeout: int = 20,
) -> tuple[dict, dict]:
    cookies = dict(cookies or {})
    extra = {}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        extra["Content-Type"] = "application/json;charset=UTF-8"
    code, _hdrs, body, merged = _request(
        path, cookies, params=params, data=data, headers=extra, timeout=timeout
    )
    parsed = _decode_json(body)
    err = parsed.get("code")
    try:
        err = int(err)
    except (TypeError, ValueError):
        err = SUCCESS if parsed.get("body") is not None else -1
    if code == 401 or err == LOGIN_EXPIRED:
        raise SessionExpired("掌阅登录已过期，请重新扫码")
    if code >= 400 and not parsed:
        raise RuntimeError(parsed.get("error_msg") or parsed.get("msg") or "掌阅网页接口 HTTP %s" % code)
    return parsed, merged


def _unwrap_qr_code(body) -> str:
    if body is None:
        return ""
    if isinstance(body, dict):
        body = body.get("code") or body.get("qrcode") or body.get("qrCode") or ""
    text = str(body).strip()
    return text.replace('"', "").replace("'", "")


def qr_start() -> dict:
    parsed, cookies = api_call("/bookstore/open/user/qrcode/get", {}, timeout=15)
    err = int(parsed.get("code") or 0)
    if err != SUCCESS:
        raise RuntimeError(parsed.get("msg") or "无法获取掌阅登录二维码")
    uid = _unwrap_qr_code(parsed.get("body"))
    if not uid:
        raise RuntimeError("无法获取掌阅登录二维码，请改用粘贴 usr / zysid")
    _qr_state[uid] = {"cookies": cookies, "started": _now()}
    return {"uid": uid, "confirmUrl": QR_PAYLOAD_PREFIX + uid}


def _complete_login(body: dict, cookies: dict) -> dict:
    if not isinstance(body, dict):
        raise RuntimeError("扫码结果缺少登录凭证")
    usr = str(body.get("name") or body.get("usr") or "")
    nick = str(body.get("nick") or "")
    zysid = str(body.get("session_id") or body.get("zysid") or "")
    avatar = str(body.get("avatar") or "")
    if not usr or not zysid:
        raise RuntimeError("扫码结果缺少 usr / session_id")
    merged = dict(cookies)
    merged["usr"] = usr
    merged["zysid"] = zysid
    data = {
        "cookies": merged,
        "usr": usr,
        "zysid": zysid,
        "name": nick or usr,
        "avatar": avatar,
        "updated_at": _now(),
    }
    save_session(data)
    return _fill_profile(data)


def qr_poll(uid: str) -> dict:
    uid = (uid or "").strip()
    state = _qr_state.get(uid) or {}
    if not uid or not state:
        raise RuntimeError("二维码已失效，请重新获取")
    if _now() - int(state.get("started") or 0) > 240:
        _qr_state.pop(uid, None)
        return {"status": "expired"}
    cookies = dict(state.get("cookies") or {})
    try:
        parsed, cookies = api_call(
            "/bookstore/open/user/qrcode/result",
            cookies,
            params={"code": uid},
            timeout=12,
        )
    except SessionExpired:
        raise
    except RuntimeError as exc:
        msg = str(exc)
        if "timed out" in msg.lower() or "timeout" in msg.lower():
            return {"status": "pending"}
        raise
    except (URLError, TimeoutError, OSError):
        return {"status": "pending"}
    state["cookies"] = cookies
    _qr_state[uid] = state
    err = parsed.get("code")
    try:
        err = int(err)
    except (TypeError, ValueError):
        err = -1
    if err == SUCCESS and isinstance(parsed.get("body"), dict):
        _qr_state.pop(uid, None)
        session = _complete_login(parsed.get("body") or {}, cookies)
        return {"status": "ok", "name": session.get("name") or "", "usr": session.get("usr") or ""}
    if err == WAIT_LOGIN:
        return {"status": "pending"}
    _qr_state.pop(uid, None)
    return {"status": "expired", "rawHint": parsed.get("msg") or str(err)}


def _fill_profile(data: dict) -> dict:
    cookies = dict(data.get("cookies") or {})
    try:
        parsed, merged = api_call("/bookstore/open/user/info", cookies, timeout=15)
        cookies = merged
        body = parsed.get("body") if int(parsed.get("code") or 0) == SUCCESS else None
        if isinstance(body, dict):
            name = body.get("nick") or body.get("name") or data.get("name") or ""
            if name:
                data["name"] = name
            if body.get("avatar"):
                data["avatar"] = body.get("avatar")
            if body.get("usr") or body.get("name"):
                data["usr"] = str(body.get("usr") or body.get("name") or data.get("usr") or "")
    except Exception:
        pass
    data["cookies"] = cookies
    data["updated_at"] = _now()
    save_session(data)
    return data


def save_cookies_from_paste(raw: str) -> dict:
    cookies = parse_cookie_string(raw)
    usr = cookies.get("usr") or cookies.get("name") or ""
    zysid = cookies.get("zysid") or cookies.get("session_id") or ""
    if not usr or not zysid:
        raise RuntimeError("需要 usr 和 zysid。可从 pc.ireader.com 登录后的 Cookie / 本地存储复制。")
    cookies["usr"] = usr
    cookies["zysid"] = zysid
    data = {
        "cookies": cookies,
        "usr": usr,
        "zysid": zysid,
        "name": cookies.get("nick") or usr,
        "avatar": cookies.get("avatar") or "",
    }
    return _fill_profile(data)


def _auth_cookies() -> dict:
    data = load_session()
    if not has_session(data):
        raise SessionExpired("尚未登录掌阅")
    cookies = dict(data.get("cookies") or {})
    cookies["usr"] = str(data.get("usr") or cookies.get("usr") or "")
    cookies["zysid"] = str(data.get("zysid") or cookies.get("zysid") or "")
    return cookies


def _body_or_raise(parsed: dict) -> object:
    err = parsed.get("code")
    try:
        err = int(err)
    except (TypeError, ValueError):
        err = -1
    if err == LOGIN_EXPIRED:
        raise SessionExpired("掌阅登录已过期，请重新扫码")
    if err != SUCCESS:
        raise RuntimeError(parsed.get("msg") or parsed.get("error_msg") or "掌阅接口错误 %s" % err)
    return parsed.get("body")


def _as_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("data", "list", "books", "records"):
            item = value.get(key)
            if isinstance(item, list):
                return item
    return []


def _book_id_of(item: dict) -> str:
    if item.get("is_local_book") and item.get("book_file_id"):
        return str(item.get("book_file_id"))
    return str(item.get("book_id") or item.get("_book_id") or item.get("bookId") or "")


def parse_shelf(payload) -> list:
    rows = _as_list(payload)
    out = []
    seen = set()
    for item in rows:
        if not isinstance(item, dict):
            continue
        try:
            dtype = int(item.get("dtype") if item.get("dtype") is not None else 0)
        except (TypeError, ValueError):
            dtype = 0
        if dtype == 1:
            continue
        book_id = _book_id_of(item)
        title = str(item.get("book_name") or item.get("bookName") or item.get("title") or item.get("name") or "").strip()
        if not book_id or not title or book_id in seen:
            continue
        seen.add(book_id)
        cover = str(item.get("icon") or item.get("cover") or item.get("pic") or "")
        last_open = 0
        for key in ("order", "update_time", "updateTime", "last_read_time", "read_time"):
            try:
                last_open = int(item.get(key) or 0)
            except (TypeError, ValueError):
                last_open = 0
            if last_open:
                break
        out.append({"id": book_id, "title": title, "cover": cover, "ts": last_open, "raw": item})
    return out[:120]


def shelf_books() -> list:
    parsed, _merged = api_call("/bookstore/open/cloud/bookshelf/list", _auth_cookies(), timeout=25)
    body = _body_or_raise(parsed)
    return parse_shelf(body)


def _index_by_id(rows: list) -> dict:
    out = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        book_id = _book_id_of(item) or str(item.get("bookId") or "")
        if book_id:
            out[book_id] = item
    return out


def book_details(book_ids: list[str]) -> dict:
    ids = [str(x) for x in book_ids if x][:40]
    if not ids:
        return {}
    try:
        parsed, _merged = api_call(
            "/bookstore/open/book/details",
            _auth_cookies(),
            params={"book_id": ",".join(ids)},
            timeout=20,
        )
        body = _body_or_raise(parsed)
    except SessionExpired:
        raise
    except Exception:
        return {}
    return _index_by_id(_as_list(body) if not isinstance(body, dict) else _as_list(body) or [body])


def book_progress(book_ids: list[str]) -> dict:
    ids = [str(x) for x in book_ids if x][:40]
    if not ids:
        return {}
    try:
        parsed, _merged = api_call(
            "/bookstore/open/cloud/book/getProgress",
            _auth_cookies(),
            params={"book_id": ",".join(ids)},
            timeout=20,
        )
        body = _body_or_raise(parsed)
    except SessionExpired:
        raise
    except Exception:
        return {}
    rows = _as_list(body)
    if isinstance(body, dict) and not rows:
        maybe = {k: v for k, v in body.items() if isinstance(v, dict)}
        if maybe:
            return {str(k): v for k, v in maybe.items()}
    return _index_by_id(rows)


def _progress_percent(item: dict) -> Optional[int]:
    for key in ("progress", "percent", "read_progress", "offset_percent"):
        raw = item.get(key)
        if raw is None or raw == "":
            continue
        try:
            n = float(raw)
        except (TypeError, ValueError):
            continue
        if 0 < n <= 1:
            n *= 100
        n = int(round(n))
        if 0 <= n <= 100:
            return n
    return None


def _last_open(item: dict) -> int:
    for key in ("update_time", "updateTime", "last_read_time", "read_time", "time_stamp", "timestamp"):
        try:
            n = int(item.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if n > 10**12:
            n //= 1000
        if n > 0:
            return n
    return 0


def fetch_book_meta(book_id: str, extra: Optional[dict] = None) -> dict:
    extra = extra or {}
    details = book_details([book_id]).get(book_id) or {}
    progress = book_progress([book_id]).get(book_id) or {}
    cover = (
        extra.get("cover")
        or details.get("icon")
        or details.get("cover")
        or details.get("pic")
        or ""
    )
    title = (
        extra.get("title")
        or details.get("book_name")
        or details.get("bookName")
        or details.get("title")
        or ""
    )
    return {
        "title": title,
        "cover": cover,
        "progress": _progress_percent(progress) if progress else _progress_percent(details),
        "lastOpen": _last_open(progress) or _last_open(details) or int(extra.get("ts") or 0),
        "wordCount": 0,
    }
