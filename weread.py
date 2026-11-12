#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""微信读书网页登录会话（用户自己的账号：扫码或粘贴 Cookie）。"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import ProxyHandler, Request, build_opener, getproxies

TZ = timezone(timedelta(hours=8))
DATA_DIR = Path.home() / ".reading-log"
SESSION_PATH = DATA_DIR / "session.json"
LEGACY_SESSION = Path.home() / ".weread-calendar" / "session.json"
WEB_ORIGIN = "https://weread.qq.com"
API_ORIGIN = "https://i.weread.qq.com"
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
# weread2notion-pro 用 App UA 才能拿到 readDetail.data 里的按日 readDate。
APP_UA = (
    "WeRead/8.2.5 WRBrand/other Dalvik/2.1.0 "
    "(Linux; U; Android 12; Pixel Build/SP1A.210812.016)"
)
APP_HEADERS = {
    "User-Agent": APP_UA,
    "baseapi": "32",
    "appver": "8.2.5.10163885",
    "basever": "8.2.5.10163885",
    "osver": "12",
}

_qr_state: dict = {}
# Official WeRead CDN size tokens: s_≈70px, t6_≈250px, t9_≈428×616 (largest that exists).
_COVER_SIZE_RE = re.compile(r"/(s|t[5-8])_([^/]+)$")


def upgrade_cover_url(url: str) -> str:
    """Rewrite small WeRead thumbs to t9_ (largest official CDN size)."""
    text = str(url or "").strip()
    if not text:
        return ""
    return _COVER_SIZE_RE.sub(r"/t9_\2", text)


class SessionExpired(RuntimeError):
    pass


def _now() -> int:
    return int(time.time())


def parse_cookie_string(raw: str) -> dict:
    cookies = {}
    text = (raw or "").strip()
    if not text:
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


def cookie_header(cookies: dict) -> str:
    return "; ".join("%s=%s" % (k, v) for k, v in cookies.items() if v)


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
    if not SESSION_PATH.exists() and LEGACY_SESSION.exists():
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            SESSION_PATH.write_bytes(LEGACY_SESSION.read_bytes())
            try:
                SESSION_PATH.chmod(0o600)
            except OSError:
                pass
        except OSError:
            pass
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
        "vid": str(data.get("vid") or ""),
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
    return bool(cookies.get("wr_skey") and cookies.get("wr_vid"))


def status() -> dict:
    data = load_session()
    ok = has_session(data)
    return {
        "loggedIn": ok,
        "name": data.get("name") or "",
        "vid": str(data.get("vid") or ""),
        "updatedAt": data.get("updated_at") or 0,
        "sessionPath": str(SESSION_PATH),
    }


def _connection_refused(exc: BaseException) -> bool:
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if getattr(cur, "errno", None) in (61, 111):
            return True
        if "connection refused" in str(cur).lower():
            return True
        nxt = getattr(cur, "reason", None)
        cur = nxt if isinstance(nxt, BaseException) and nxt is not cur else None
    return False


def _fresh_request(req: Request) -> Request:
    """Copy a request. urllib's proxy handler rewrites req.host in place, so a
    failed proxy attempt must not be reused for the direct retry."""
    headers = {}
    headers.update(getattr(req, "unredirected_hdrs", {}) or {})
    headers.update(getattr(req, "headers", {}) or {})
    return Request(
        req.full_url,
        data=req.data,
        headers=headers,
        origin_req_host=getattr(req, "origin_req_host", None),
        unverifiable=bool(getattr(req, "unverifiable", False)),
        method=getattr(req, "method", None),
    )


def open_url(req: Request, timeout: int = 20):
    """Open outbound HTTP without sticking to a dead process-wide proxy.

    urllib snapshots system proxies on the first urlopen of a process. This
    server stays up for days; if that proxy later exits, every later request
    fails with connection refused. Read proxies per call, and if the proxy
    itself refuses, try once with a direct connection.
    """
    proxies = getproxies()
    try:
        return build_opener(ProxyHandler(dict(proxies))).open(_fresh_request(req), timeout=timeout)
    except URLError as exc:
        if not proxies or not _connection_refused(exc):
            raise
        return build_opener(ProxyHandler({})).open(_fresh_request(req), timeout=timeout)


def _request(
    url: str,
    cookies: dict,
    params: Optional[dict] = None,
    data: Optional[bytes] = None,
    headers: Optional[dict] = None,
    timeout: int = 20,
    method: Optional[str] = None,
) -> tuple[int, dict, bytes, dict]:
    if params:
        sep = "&" if "?" in url else "?"
        url = url + sep + urlencode(params)
    hdrs = {
        "User-Agent": BROWSER_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept-Encoding": "identity",
        "Origin": WEB_ORIGIN,
        "Referer": WEB_ORIGIN + "/",
    }
    if headers:
        hdrs.update(headers)
    cookie = cookie_header(cookies)
    if cookie:
        hdrs["Cookie"] = cookie
    if method is None:
        method = "POST" if data is not None else "GET"
    req = Request(url, data=data, headers=hdrs, method=method)
    try:
        with open_url(req, timeout=timeout) as resp:
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
        raise RuntimeError("微信读书网页接口返回无法解析")
    if not isinstance(data, dict):
        return {}
    return data


def http_json(
    url: str,
    cookies: dict,
    params: Optional[dict] = None,
    payload: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: int = 20,
    app_api: bool = False,
) -> tuple[dict, dict]:
    extra = dict(headers or {})
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        extra["Content-Type"] = "application/json;charset=UTF-8"
    if app_api:
        extra.update(APP_HEADERS)
    code, _hdrs, body, merged = _request(
        url, cookies, params=params, data=data, headers=extra, timeout=timeout
    )
    parsed = _decode_json(body)
    err = int(parsed.get("errcode") or parsed.get("errCode") or 0)
    if code == 401 or err in (-2010, -2012):
        raise SessionExpired("网页登录已过期，请重新扫码或粘贴 Cookie")
    if code >= 400:
        raise RuntimeError(parsed.get("errmsg") or parsed.get("errMsg") or "微信读书网页接口 HTTP %s" % code)
    if err:
        raise RuntimeError(parsed.get("errmsg") or parsed.get("errMsg") or "微信读书网页接口错误")
    return parsed, merged


def refresh_cookies(data: Optional[dict] = None) -> dict:
    data = dict(data or load_session())
    cookies = dict(data.get("cookies") or {})
    if not cookies:
        return data
    try:
        _code, _headers, _body, merged = _request(
            WEB_ORIGIN + "/",
            cookies,
            timeout=15,
            headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
        )
        cookies = merged
    except (URLError, TimeoutError, OSError):
        pass
    try:
        payload = json.dumps({"rq": "%2Fweb%2Fbook%2Fread"}).encode("utf-8")
        _code, _headers, _body, merged = _request(
            WEB_ORIGIN + "/web/login/renewal",
            cookies,
            data=payload,
            headers={"Content-Type": "application/json;charset=UTF-8"},
            timeout=15,
        )
        cookies = merged
    except (URLError, TimeoutError, OSError, SessionExpired):
        pass
    data["cookies"] = cookies
    data["updated_at"] = _now()
    if has_session(data):
        save_session(data)
    return data


def _fill_profile(data: dict) -> dict:
    cookies = dict(data.get("cookies") or {})
    vid = str(cookies.get("wr_vid") or data.get("vid") or "")
    if not vid:
        return data
    try:
        info, merged = http_json(
            WEB_ORIGIN + "/web/user",
            cookies,
            params={"userVid": vid},
            timeout=15,
        )
        cookies = merged
        name = info.get("name") or info.get("user") or cookies.get("wr_name") or ""
        if name:
            data["name"] = name
    except Exception:
        pass
    data["cookies"] = cookies
    data["vid"] = vid
    data["updated_at"] = _now()
    save_session(data)
    return data


def save_cookies_from_paste(raw: str) -> dict:
    cookies = parse_cookie_string(raw)
    if not cookies.get("wr_skey") or not cookies.get("wr_vid"):
        raise RuntimeError("Cookie 里需要 wr_vid 和 wr_skey。请从 weread.qq.com 复制完整 Cookie。")
    data = {"cookies": cookies, "vid": cookies.get("wr_vid") or "", "name": cookies.get("wr_name") or ""}
    data = refresh_cookies(data)
    return _fill_profile(data)


def qr_start() -> dict:
    cookies: dict = {}
    uid = ""
    flow = "auth"
    try:
        parsed, cookies = http_json(
            WEB_ORIGIN + "/api/auth/getLoginUid",
            cookies,
            headers={"Referer": WEB_ORIGIN + "/r/weread-skills"},
            timeout=15,
        )
        uid = str(parsed.get("uid") or "")
    except Exception:
        uid = ""
    if not uid:
        flow = "web"
        parsed, cookies = http_json(
            WEB_ORIGIN + "/web/login/getuid",
            {},
            payload={},
            timeout=15,
        )
        uid = str(parsed.get("uid") or "")
        cookies = {**(cookies or {})}
    if not uid:
        raise RuntimeError("无法获取登录二维码，请改用粘贴 Cookie")
    confirm = WEB_ORIGIN + "/web/confirm?uid=" + quote(uid)
    if flow == "web":
        confirm = WEB_ORIGIN + "/web/confirm?pf=2&uid=" + quote(uid)
    _qr_state[uid] = {"cookies": cookies, "flow": flow, "started": _now()}
    return {"uid": uid, "confirmUrl": confirm, "flow": flow}


def _complete_auth(result: dict, cookies: dict) -> dict:
    vid = str(result.get("webLoginVid") or result.get("vid") or "")
    skey = str(result.get("accessToken") or result.get("skey") or "")
    refresh = str(result.get("refreshToken") or "")
    if not vid or not skey:
        raise RuntimeError("扫码结果缺少登录凭证")
    cookies = dict(cookies)
    cookies["wr_vid"] = vid
    cookies["wr_skey"] = skey
    cookies["wr_ql"] = cookies.get("wr_ql") or "0"
    if refresh:
        cookies["wr_rt"] = refresh
    data = {"cookies": cookies, "vid": vid, "name": result.get("name") or ""}
    return _fill_profile(refresh_cookies(data))


def _complete_web(result: dict, cookies: dict) -> dict:
    parsed, cookies = http_json(
        WEB_ORIGIN + "/web/login/weblogin",
        cookies,
        payload=result,
        timeout=20,
    )
    merged = dict(result)
    merged.update(parsed or {})
    parsed, cookies = http_json(
        WEB_ORIGIN + "/web/login/session/init",
        cookies,
        payload=merged,
        timeout=20,
    )
    cookies = dict(cookies)
    vid = str(cookies.get("wr_vid") or merged.get("vid") or parsed.get("vid") or "")
    data = {"cookies": cookies, "vid": vid, "name": merged.get("name") or parsed.get("name") or ""}
    return _fill_profile(refresh_cookies(data))


def qr_poll(uid: str, otp: str = "") -> dict:
    uid = (uid or "").strip()
    state = _qr_state.get(uid) or {}
    if not uid or not state:
        raise RuntimeError("二维码已失效，请重新获取")
    if _now() - int(state.get("started") or 0) > 300:
        _qr_state.pop(uid, None)
        return {"status": "expired"}
    cookies = dict(state.get("cookies") or {})
    flow = state.get("flow") or "auth"
    if flow == "auth":
        params = {"uid": uid, "otp": otp or ""}
        url = WEB_ORIGIN + "/api/auth/getLoginInfo"
        headers = {"Referer": WEB_ORIGIN + "/r/weread-skills"}
    else:
        params = {"uid": uid}
        url = WEB_ORIGIN + "/web/login/getinfo"
        headers = {"Referer": WEB_ORIGIN + "/"}
    try:
        parsed, cookies = http_json(url, cookies, params=params, headers=headers, timeout=12)
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
    if parsed.get("succeed") is True or parsed.get("accessToken") or (
        parsed.get("vid") and (parsed.get("skey") or parsed.get("accessToken"))
    ):
        _qr_state.pop(uid, None)
        if flow == "auth":
            session = _complete_auth(parsed, cookies)
        else:
            session = _complete_web(parsed, cookies)
        return {"status": "ok", "name": session.get("name") or "", "vid": session.get("vid") or ""}
    logic = str(parsed.get("logicCode") or parsed.get("errcode") or "")
    if logic in {"NEED_OTP", "OTP_NOT_MATCH"}:
        return {"status": "need_otp", "logicCode": logic}
    if logic in {"LOGIN_TIMEOUT", "OTP_EXPIRED"}:
        _qr_state.pop(uid, None)
        return {"status": "expired"}
    if not parsed or parsed.get("code") in (None, -1, "-1"):
        return {"status": "pending"}
    return {"status": "pending", "rawHint": logic}


def _with_refresh(fn):
    data = load_session()
    if not has_session(data):
        raise SessionExpired("尚未网页登录")
    try:
        return fn(data)
    except SessionExpired:
        data = refresh_cookies(data)
        if not has_session(data):
            raise
        try:
            return fn(data)
        except SessionExpired:
            raise SessionExpired("网页登录已过期，请重新扫码或粘贴 Cookie")


def web_get(path: str, params: Optional[dict] = None, app_api: bool = False) -> dict:
    def _do(data: dict) -> dict:
        cookies = dict(data.get("cookies") or {})
        urls = []
        if path.startswith("http"):
            urls = [path]
        else:
            urls = [WEB_ORIGIN + "/web" + path, API_ORIGIN + path]
        last_err = None
        for url in urls:
            try:
                parsed, merged = http_json(
                    url, cookies, params=params, timeout=25, app_api=app_api
                )
                if merged != cookies:
                    data["cookies"] = merged
                    data["updated_at"] = _now()
                    save_session(data)
                return parsed
            except SessionExpired:
                raise
            except Exception as exc:
                last_err = exc
                continue
        raise last_err or RuntimeError("网页接口请求失败")

    return _with_refresh(_do)


def book_readinfo(book_id: str) -> dict:
    params = {
        "bookId": book_id,
        "readingDetail": 1,
        "readingBookIndex": 1,
        "finishedDate": 1,
        "finishedBookIndex": 1,
        "readingBookCount": 1,
        "finishedBookCount": 1,
        "noteCount": 1,
    }
    return web_get("/book/readinfo", params=params, app_api=True)


def parse_daily_seconds(read_info: dict) -> dict:
    """返回 {YYYY-MM-DD: seconds}。没有 readDate 的条目直接丢掉，不编造。"""
    detail = read_info.get("readDetail") or {}
    rows = detail.get("data") or []
    out = {}
    if not isinstance(rows, list):
        return out
    for item in rows:
        if not isinstance(item, dict):
            continue
        ts = item.get("readDate") or item.get("dateTimestamp") or item.get("date")
        seconds = item.get("readTime") or item.get("readingTime") or 0
        try:
            ts = int(ts)
            seconds = int(seconds or 0)
        except (TypeError, ValueError):
            continue
        if ts <= 0 or seconds <= 0:
            continue
        if ts > 10**12:
            ts = ts // 1000
        day = datetime.fromtimestamp(ts, TZ).date().isoformat()
        out[day] = out.get(day, 0) + seconds
    return out


def fallback_daily_seconds(read_info: dict, last_open: int = 0) -> dict:
    """WeRead 对部分书返回 showDetail=0、不给 readDetail，但有总 readingTime。

    totalReadDay==1 时，这一天是最近一次阅读进度（last_open / readUpdateTime），
    不是 readingBookDate。后者只是「标记在读」的时间，可以早于真正开读很久
    （例如 8 月标记、10 月才读的 11 分钟会被整段记到 8 月）。
    totalReadDay>1 时仍记在 readingBookDate：没有按日明细就没法拆开，
    也不能每次点开都把多日总量堆到今天。
    一旦 showDetail=1 有了真实按日数据，同步会改走 parse_daily_seconds。
    调用方若发现该书已有用户手动拆分（seconds_custom / manual），应跳过本函数并清掉非自定义的 weread 残行。
    """
    if not isinstance(read_info, dict):
        return {}
    try:
        if int(read_info.get("showDetail") or 0) == 1:
            return {}
    except (TypeError, ValueError):
        pass
    if read_info.get("readDetail"):
        return {}
    try:
        seconds = int(read_info.get("readingTime") or 0)
    except (TypeError, ValueError):
        return {}
    if seconds <= 0:
        return {}
    try:
        total_days = int(read_info.get("totalReadDay") or 0)
    except (TypeError, ValueError):
        total_days = 0
    ts = 0
    if total_days == 1:
        try:
            ts = int(last_open or 0)
        except (TypeError, ValueError):
            ts = 0
    if ts <= 0:
        # Multi-day (or no last-open): keep the stable mark-as-reading date.
        for key in ("readingBookDate", "updateTime"):
            try:
                ts = int(read_info.get(key) or 0)
            except (TypeError, ValueError):
                ts = 0
            if ts > 0:
                break
    if ts <= 0:
        try:
            ts = int(last_open or 0)
        except (TypeError, ValueError):
            ts = 0
    if ts <= 0:
        ts = _now()
    if ts > 10**12:
        ts = ts // 1000
    day = datetime.fromtimestamp(ts, TZ).date().isoformat()
    return {day: seconds}


def monthly_read_times(year: int, month: int) -> Optional[dict]:
    """网页 /readdata/detail 若有按日总额则返回 {iso: seconds}，否则 None。"""
    base = int(datetime(year, month, 1, tzinfo=TZ).timestamp())
    try:
        data = web_get(
            "/readdata/detail",
            params={"mode": "monthly", "baseTime": base},
            app_api=True,
        )
    except Exception:
        return None
    times = data.get("readTimes") or data.get("dailyReadTimes") or {}
    if not isinstance(times, dict) or not times:
        return None
    out = {}
    for key, value in times.items():
        try:
            ts = int(key)
            if ts > 10**12:
                ts = ts // 1000
            day = datetime.fromtimestamp(ts, TZ).date().isoformat()
            out[day] = int(value or 0)
        except (TypeError, ValueError):
            continue
    return out or None


def load_api_key() -> str:
    key = os.environ.get("WEREAD_API_KEY", "").strip()
    env_file = Path.home() / ".weread.env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:]
            if line.startswith("WEREAD_API_KEY="):
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
                if value:
                    key = value
    return key


def call_agent(payload: dict) -> dict:
    import json as _json

    key = load_api_key()
    if not key:
        raise RuntimeError("未找到 WEREAD_API_KEY")
    body = dict(payload)
    body["skill_version"] = "1.0.4"
    req = Request(
        "https://i.weread.qq.com/api/agent/gateway",
        data=_json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer %s" % key, "Content-Type": "application/json"},
        method="POST",
    )
    with open_url(req, timeout=25) as resp:
        data = _json.loads(resp.read().decode("utf-8"))
    if data.get("errcode"):
        raise RuntimeError(data.get("errmsg") or "微信读书接口错误")
    return data


def pick_author(*objs) -> str:
    keys = ("author", "authorName", "authors", "writer")
    for obj in objs:
        if not isinstance(obj, dict):
            continue
        stack = [obj]
        nested = obj.get("book")
        if isinstance(nested, dict):
            stack.append(nested)
        nested = obj.get("bookInfo")
        if isinstance(nested, dict):
            stack.append(nested)
        for item in stack:
            for key in keys:
                raw = item.get(key)
                if isinstance(raw, list):
                    names = [str(x).strip() for x in raw if str(x).strip()]
                    if names:
                        return " ".join(names)[:80]
                s = str(raw or "").strip()
                if s:
                    return s[:80]
    return ""


def pick_word_count(*objs) -> int:
    keys = ("wordCount", "totalWords", "totalWordCount", "words", "word_count", "total_words")
    for obj in objs:
        if not isinstance(obj, dict):
            continue
        stack = [obj]
        nested = obj.get("book")
        if isinstance(nested, dict):
            stack.append(nested)
        nested = obj.get("bookInfo")
        if isinstance(nested, dict):
            stack.append(nested)
        for item in stack:
            for key in keys:
                raw = item.get(key)
                if raw is None or raw == "":
                    continue
                try:
                    n = int(raw)
                except (TypeError, ValueError):
                    continue
                if n > 0:
                    return n
    return 0


def _meta_stack(*objs) -> list:
    stack = []
    for obj in objs:
        if not isinstance(obj, dict):
            continue
        stack.append(obj)
        nested = obj.get("book")
        if isinstance(nested, dict):
            stack.append(nested)
        nested = obj.get("bookInfo")
        if isinstance(nested, dict):
            stack.append(nested)
    return stack


def pick_isbn(*objs) -> str:
    for item in _meta_stack(*objs):
        for key in ("isbn", "ISBN", "isbn13", "isbn10"):
            s = str(item.get(key) or "").strip()
            if s and s.lower() not in ("none", "null"):
                return s[:32]
    return ""


def pick_publisher(*objs) -> str:
    for item in _meta_stack(*objs):
        for key in ("publisher", "press", "publisherName", "publishHouse", "publishingHouse"):
            s = str(item.get(key) or "").strip()
            if s:
                return s[:80]
        # WeRead digital editions often leave publisher blank; copyright party is the imprint.
        copy = item.get("copyrightInfo")
        if isinstance(copy, dict):
            s = str(copy.get("name") or "").strip()
            if s:
                return s[:80]
    return ""


def pick_translator(*objs) -> str:
    for item in _meta_stack(*objs):
        seg = item.get("translatorSeg") or item.get("translator_seg")
        if isinstance(seg, list) and seg:
            names = []
            for part in seg:
                if isinstance(part, dict):
                    w = str(part.get("words") or part.get("name") or "").strip()
                else:
                    w = str(part or "").strip()
                if w:
                    names.append(w)
            if names:
                return " ".join(names)[:80]
        for key in ("translator", "translators", "translatorName"):
            raw = item.get(key)
            if isinstance(raw, list):
                names = [str(x).strip() for x in raw if str(x).strip()]
                if names:
                    return " ".join(names)[:80]
            s = str(raw or "").strip()
            if s:
                return s[:80]
    return ""


def pick_price(*objs) -> str:
    """Display price in yuan, e.g. '70.00'. Prefer price, then publishPrice, then centPrice."""
    for item in _meta_stack(*objs):
        for key in ("price", "publishPrice", "originalPrice"):
            raw = item.get(key)
            if raw is None or raw == "":
                continue
            try:
                n = float(raw)
            except (TypeError, ValueError):
                continue
            if n > 0:
                return "%.2f" % n
        cent = item.get("centPrice")
        if cent is None or cent == "":
            continue
        try:
            c = int(cent)
        except (TypeError, ValueError):
            continue
        if c > 0:
            return "%.2f" % (c / 100.0)
    return ""


def pick_publish_year(*objs) -> str:
    for item in _meta_stack(*objs):
        for key in ("publishTime", "publish_time", "publishDate", "publish_date"):
            raw = item.get(key)
            if raw is None or raw == "":
                continue
            s = str(raw).strip()
            if len(s) >= 4 and s[:4].isdigit():
                y = int(s[:4])
                if 1000 <= y <= 2100:
                    return str(y)
            iso = _iso_date_from_value(raw)
            if iso:
                return iso[:4]
    return ""


def _iso_date_from_value(raw) -> str:
    if raw is None or raw == "":
        return ""
    if isinstance(raw, str):
        s = raw.strip()
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            return s[:10]
        if s.isdigit():
            raw = s
        else:
            return ""
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    if 19900101 <= n <= 21001231:
        s = "%08d" % n
        return "%s-%s-%s" % (s[:4], s[4:6], s[6:8])
    if n > 10**12:
        n = n // 1000
    try:
        return datetime.fromtimestamp(n, TZ).date().isoformat()
    except Exception:
        return ""


def pick_finish_date(*objs) -> str:
    keys = (
        "finishedDate",
        "finishDate",
        "finishedTime",
        "finishTime",
        "completeTime",
        "finish_date",
    )
    for obj in objs:
        if not isinstance(obj, dict):
            continue
        stack = [obj]
        nested = obj.get("book")
        if isinstance(nested, dict):
            stack.append(nested)
        nested = obj.get("bookInfo")
        if isinstance(nested, dict):
            stack.append(nested)
        for item in stack:
            for key in keys:
                day = _iso_date_from_value(item.get(key))
                if day:
                    return day
    return ""


def _progress_int(payload) -> Optional[int]:
    if not isinstance(payload, dict):
        return None
    book = payload.get("book") if isinstance(payload.get("book"), dict) else {}
    raw = book.get("progress")
    if raw is None:
        raw = payload.get("progress")
    if raw is None or raw == "":
        return None
    try:
        n = int(round(float(raw)))
    except (TypeError, ValueError):
        return None
    if n < 0:
        return 0
    if n > 100:
        return 100
    return n


def _has_finish_time(book: dict) -> bool:
    """True when WeRead marks a real finish (not a bare progress=100)."""
    if not isinstance(book, dict):
        return False
    for key in ("finishTime", "finishedDate", "finishDate", "finishedTime"):
        raw = book.get(key)
        if raw in (None, "", 0, "0"):
            continue
        return True
    return False


_chapterinfo_cache: dict[str, tuple[float, list]] = {}
_CHAPTERINFO_TTL_SEC = 3600.0


def fetch_chapterinfo(book_id: str) -> list:
    """Chapter list with wordCount. Empty when neither session nor agent works."""
    book_id = str(book_id or "").strip()
    if not book_id:
        return []
    cached = _chapterinfo_cache.get(book_id)
    if cached and (_now() - cached[0]) < _CHAPTERINFO_TTL_SEC and cached[1]:
        return cached[1]
    data = {}
    if load_api_key():
        try:
            data = call_agent({"api_name": "/book/chapterinfo", "bookId": book_id})
        except Exception:
            data = {}
    if not data and has_session():
        for path, params in (
            ("/book/chapterInfos", {"bookIds": json.dumps([book_id])}),
            ("/book/chapterinfos", {"bookIds": book_id}),
        ):
            try:
                data = web_get(path, params=params, app_api=True)
                if data:
                    break
            except Exception:
                data = {}
    chapters = data.get("chapters") if isinstance(data, dict) else None
    if not (isinstance(chapters, list) and chapters):
        # Some payloads nest under data[] / updated[].
        chapters = None
        for key in ("data", "updated"):
            block = data.get(key) if isinstance(data, dict) else None
            if not isinstance(block, list):
                continue
            for item in block:
                if not isinstance(item, dict):
                    continue
                nested = item.get("chapters")
                if isinstance(nested, list) and nested:
                    chapters = nested
                    break
            if chapters:
                break
    if isinstance(chapters, list) and chapters:
        _chapterinfo_cache[book_id] = (float(_now()), chapters)
        return chapters
    return list(cached[1]) if cached and cached[1] else []


def estimate_progress_from_pointer(
    book_id: str,
    chapter_idx: int,
    chapter_offset: int,
    chapters: Optional[list] = None,
) -> Optional[int]:
    """(words before chapter + chapterOffset) / total words → 0–100."""
    book_id = str(book_id or "").strip()
    if not book_id:
        return None
    try:
        idx = int(chapter_idx)
        offset = int(chapter_offset)
    except (TypeError, ValueError):
        return None
    if idx <= 0:
        return None
    rows = chapters if isinstance(chapters, list) and chapters else fetch_chapterinfo(book_id)
    if not rows:
        return None
    total = 0
    base = 0
    found = False
    cur_words = 0
    for ch in rows:
        if not isinstance(ch, dict):
            continue
        try:
            words = int(ch.get("wordCount") or 0)
        except (TypeError, ValueError):
            words = 0
        if words < 0:
            words = 0
        total += words
        try:
            cidx = int(ch.get("chapterIdx") or 0)
        except (TypeError, ValueError):
            continue
        if cidx < idx:
            base += words
        elif cidx == idx:
            found = True
            cur_words = words
    if not found or total <= 0:
        return None
    read = base + max(0, min(offset, cur_words if cur_words > 0 else offset))
    pct = int(round(read / total * 100))
    return max(0, min(100, pct))


def resolve_reading_progress(book_id: str, payload) -> Optional[int]:
    """Progress 0–100.

    WeRead sometimes reports progress=100 with no finishTime while the chapter
    cursor is still mid-book. Never treat bare 100 as finished: prefer the
    chapter-pointer estimate, and if that fails return None so sync keeps the
    previous local value instead of flipping to 100%.
    """
    book = payload.get("book") if isinstance(payload, dict) and isinstance(payload.get("book"), dict) else {}
    n = _progress_int(payload)
    if _has_finish_time(book):
        if n is None:
            return 100
        return max(0, min(100, n))

    idx = 0
    offset = 0
    try:
        idx = int(book.get("chapterIdx") or 0)
        offset = int(book.get("chapterOffset") or 0)
    except (TypeError, ValueError):
        idx, offset = 0, 0

    estimated = None
    if idx > 0:
        estimated = estimate_progress_from_pointer(book_id, idx, offset)

    if estimated is not None:
        # Without finish evidence, never surface 100% (would mark 读完).
        return 99 if estimated >= 100 else estimated

    # No usable pointer estimate: keep prior progress rather than trusting bare 100.
    if n is None or n >= 100:
        return None
    return n


def _fetch_getprogress(book_id: str) -> dict:
    book_id = str(book_id or "").strip()
    if not book_id:
        return {}
    if has_session():
        try:
            data = web_get("/book/getprogress", params={"bookId": book_id}, app_api=True)
            if isinstance(data, dict) and data:
                return data
        except SessionExpired:
            if not load_api_key():
                raise
        except Exception:
            pass
    if load_api_key():
        try:
            data = call_agent({"api_name": "/book/getprogress", "bookId": book_id})
            if isinstance(data, dict):
                return data
        except Exception:
            return {}
    if not has_session():
        raise SessionExpired("尚未登录微信读书")
    return {}


def fetch_reading_progress(book_id: str) -> Optional[int]:
    """One book's WeRead progress, 0–100. None if the response has no progress."""
    book_id = str(book_id or "").strip()
    if not book_id:
        return None
    payload = _fetch_getprogress(book_id)
    if not payload:
        return None
    return resolve_reading_progress(book_id, payload)


def fetch_book_meta(book_id: str) -> dict:
    info = {}
    if has_session():
        try:
            info = web_get("/book/info", params={"bookId": str(book_id)}, app_api=True)
        except Exception:
            info = {}
    if not info and load_api_key():
        try:
            info = call_agent({"api_name": "/book/info", "bookId": str(book_id)})
        except Exception:
            info = {}
    prog = {}
    try:
        prog = _fetch_getprogress(str(book_id))
    except SessionExpired:
        raise
    except Exception:
        prog = {}
    book = prog.get("book") or {}
    nested = info.get("book") if isinstance(info.get("book"), dict) else {}
    book_info = info.get("bookInfo") if isinstance(info.get("bookInfo"), dict) else {}
    cover = upgrade_cover_url(
        info.get("cover") or nested.get("cover") or book_info.get("cover") or ""
    )
    progress = resolve_reading_progress(str(book_id), prog) if prog else None
    finish_date = pick_finish_date(info, nested, book_info, book, prog)
    # False 100 without finishTime must not stamp a 读完日期.
    if progress is not None and progress < 100:
        finish_date = ""
    last_open = 0
    try:
        last_open = int(book.get("updateTime") or info.get("updateTime") or 0)
    except (TypeError, ValueError):
        last_open = 0
    return {
        "title": info.get("title") or nested.get("title") or book_info.get("title") or "",
        "cover": cover,
        "wordCount": pick_word_count(info, nested, book_info, book),
        "author": pick_author(info, nested, book_info, book),
        "isbn": pick_isbn(info, nested, book_info, book),
        "publisher": pick_publisher(info, nested, book_info, book),
        "translator": pick_translator(info, nested, book_info, book),
        "price": pick_price(info, nested, book_info, book),
        "publishYear": pick_publish_year(info, nested, book_info, book),
        "progress": progress,
        "lastOpen": last_open,
        "finishDate": finish_date,
        "readingSeconds": int(book.get("recordReadingTime") or 0),
    }


def parse_shelf(data: dict) -> list:
    out = []
    seen = set()
    for book in data.get("books") or []:
        book_id = str(book.get("bookId") or "")
        title = book.get("title") or ""
        if not book_id or not title or book_id in seen:
            continue
        seen.add(book_id)
        out.append(
            {
                "id": book_id,
                "title": title,
                "ts": int(book.get("readUpdateTime") or 0),
                "cover": upgrade_cover_url(book.get("cover") or ""),
                "wordCount": pick_word_count(book),
                "author": pick_author(book),
            }
        )
    for album in data.get("albums") or []:
        info = album.get("albumInfo") or {}
        extra = album.get("albumInfoExtra") or {}
        album_id = str(info.get("albumId") or "")
        title = info.get("name") or ""
        if not album_id or not title or album_id in seen:
            continue
        seen.add(album_id)
        out.append(
            {
                "id": album_id,
                "title": title,
                "ts": int(extra.get("lectureReadUpdateTime") or info.get("updateTime") or 0),
                "cover": upgrade_cover_url(extra.get("cover") or info.get("cover") or ""),
            }
        )
    out.sort(key=lambda b: b.get("ts") or 0, reverse=True)
    return out


def _note_stub(item: dict) -> Optional[dict]:
    book = item.get("book") if isinstance(item.get("book"), dict) else {}
    book_id = str(item.get("bookId") or book.get("bookId") or "").strip()
    if not book_id:
        return None
    try:
        ts = int(item.get("sort") or 0)
    except (TypeError, ValueError):
        ts = 0
    return {
        "id": book_id,
        "title": book.get("title") or item.get("title") or "",
        "cover": upgrade_cover_url(book.get("cover") or ""),
        "author": book.get("author") or "",
        "ts": ts,
    }


def _longest_stub(item: dict) -> Optional[dict]:
    book = item.get("book") if isinstance(item.get("book"), dict) else {}
    album = item.get("albumInfo") if isinstance(item.get("albumInfo"), dict) else {}
    book_id = str(book.get("bookId") or album.get("albumId") or "").strip()
    if not book_id:
        return None
    return {
        "id": book_id,
        "title": book.get("title") or album.get("name") or "",
        "cover": upgrade_cover_url(book.get("cover") or album.get("cover") or ""),
        "author": book.get("author") or album.get("authorName") or "",
        "ts": 0,
    }


def books_beyond_shelf(on_shelf: set) -> list:
    """Books WeRead can list that are not on the current shelf.

    Documented sources only: /user/notebooks (every book with notes) and
    /readdata/detail readLongest (top 10 overall and for recent years).
    There is no API for the full 读过 / 读完 list.
    """
    if not load_api_key():
        return []
    on_shelf = {str(item) for item in (on_shelf or set())}
    found: dict[str, dict] = {}

    def add(stub: Optional[dict]) -> None:
        if not stub:
            return
        book_id = str(stub.get("id") or "")
        if not book_id or book_id in on_shelf or book_id in found:
            return
        found[book_id] = stub

    last = None
    for _ in range(40):
        payload = {"api_name": "/user/notebooks", "count": 100}
        if last is not None:
            payload["lastSort"] = last
        try:
            page = call_agent(payload)
        except Exception:
            break
        books = page.get("books") or []
        for item in books:
            if isinstance(item, dict):
                add(_note_stub(item))
        if not books or not int(page.get("hasMore") or 0):
            break
        sort = books[-1].get("sort") if isinstance(books[-1], dict) else None
        if sort is None or sort == last:
            break
        last = sort

    now = datetime.now(TZ)
    queries = [("overall", 0)]
    for year in range(now.year, now.year - 3, -1):
        queries.append(("annually", int(datetime(year, 6, 1, tzinfo=TZ).timestamp())))
    for mode, base in queries:
        try:
            data = call_agent({"api_name": "/readdata/detail", "mode": mode, "baseTime": base})
        except Exception:
            continue
        for item in data.get("readLongest") or []:
            if isinstance(item, dict):
                add(_longest_stub(item))
    return list(found.values())


def shelf_books() -> list:
    try:
        return parse_shelf(web_get("/shelf/sync", app_api=True))
    except SessionExpired:
        pass
    except Exception:
        pass
    if load_api_key():
        return parse_shelf(call_agent({"api_name": "/shelf/sync"}))
    raise SessionExpired("网页登录已过期，请重新扫码；或配置 WEREAD_API_KEY 以读取书架")
