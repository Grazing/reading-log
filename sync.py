#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从微信读书拉取书架、进度和按日阅读，写入本地库。"""

from __future__ import annotations

import threading

import ireader
import store
import weread

_weread_sync_guard = threading.Lock()


def _raw_id(book: dict) -> str:
    return str(book.get("id") or "")


def _book_daily_snapshot(raw_id: str, last_open: int = 0) -> tuple[dict[str, int], str]:
    """Fetch {day: seconds} and a finish date. Prefer readDetail, else fallback.

    The finish date is whatever WeRead reports for this volume (often blank).
    Minutes are not derived from it.
    """
    info = weread.book_readinfo(raw_id)
    daily = weread.parse_daily_seconds(info)
    if not daily:
        daily = weread.fallback_daily_seconds(info, int(last_open or 0)) or {}
    cleaned = {str(d): int(s) for d, s in daily.items() if int(s) > 0}
    return cleaned, weread.pick_finish_date(info)


def _book_daily_seconds(raw_id: str, last_open: int = 0) -> dict[str, int]:
    """Fetch one WeRead book's {day: seconds}; prefer readDetail, else fallback."""
    daily, _finish = _book_daily_snapshot(raw_id, last_open)
    return daily


def _refresh_merged_last_opens(shelf: list) -> None:
    """Copy shelf read-update times onto merge members, including hidden aliases.

    The per-book sync skips aliases, so their stored last_open would stay stale
    and the canonical max would miss a later open (磨铁三部). Minutes and word
    counts are left alone.
    """
    stamps: dict[str, int] = {}
    for book in shelf or []:
        raw = str(book.get("id") or "").strip()
        if not raw:
            continue
        try:
            ts = int(book.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0
        if ts > 0:
            stamps["weread:" + raw] = ts
    if not stamps:
        return
    for canon, aliases in store.book_merges().items():
        for book_id in [canon] + list(aliases):
            ts = stamps.get(book_id)
            if ts:
                store.set_last_open_if_newer(book_id, ts)


def _apply_book_merges(shelf: list | None = None) -> int:
    """Fold alias edition dailies into each canonical book (day union, same-day sum).

    The canonical last_open becomes the latest open among the canonical volume
    and every merged member that still has a last_open. Each volume's finish
    date stays on that volume. An unfinished series canonical is not given a
    finish date, so 读完日期 sort keeps treating the series as in progress.
    """
    merges = store.ensure_book_merges()
    _refresh_merged_last_opens(shelf or [])
    wrote = 0
    for canonical, aliases in merges.items():
        store.apply_merged_last_open(canonical)
        if not canonical.startswith("weread:"):
            continue
        members = [canonical] + list(aliases)
        acc: dict[str, int] = {}
        for book_id in members:
            raw = book_id.split(":", 1)[1].strip()
            if not raw:
                continue
            try:
                daily, finish = _book_daily_snapshot(raw)
            except weread.SessionExpired:
                raise
            except Exception:
                continue
            if finish:
                store.note_merged_finish_date(book_id, finish)
            for day, seconds in daily.items():
                acc[day] = acc.get(day, 0) + int(seconds)
        rows = [{"day": d, "seconds": s} for d, s in sorted(acc.items()) if s > 0]
        wrote += store.replace_merged_book_days(canonical, "weread", rows)
        # Keep alias rows out of the calendar (canonical already has the fold).
        for alias in aliases:
            store.replace_book_days(alias, "weread", [])
    _apply_book_series()
    return wrote


def _volume_read(book_id: str) -> tuple[int, int] | None:
    """Return (word count, progress 0–100). None when WeRead meta is unusable."""
    raw = str(book_id).split(":", 1)[-1].strip()
    if not raw:
        return None
    try:
        meta = weread.fetch_book_meta(raw)
    except weread.SessionExpired:
        raise
    except Exception:
        return None
    words = int(meta.get("wordCount") or 0)
    progress = meta.get("progress")
    if words <= 0 or progress is None:
        return None
    try:
        progress = int(progress)
    except (TypeError, ValueError):
        return None
    return words, max(0, min(100, progress))


def _apply_book_series() -> None:
    """Word count = sum of members. Progress = words read / that total.

    progressLead (杨可意/磨铁) is an early parallel edition. While any lead volume
    is incomplete, progress = lead words-read / member total. Once the lead set
    is finished, map onto members via afterLeadFloors (e.g. Motie → 自导入 1–3
    @100% and 部4 @≥86%), then add later volumes by their actual WeRead %.
    Reading time is folded separately by the merge.
    """
    for canonical, spec in store.book_series().items():
        members = spec.get("members") or []
        volumes: list[tuple[int, int]] = []
        complete = True
        for book_id in members:
            got = _volume_read(book_id)
            if not got:
                complete = False
                break
            volumes.append(got)
        if not complete or not volumes:
            continue
        total_words = sum(words for words, _progress in volumes)
        if total_words <= 0:
            continue

        read_words = sum(words * (progress / 100.0) for words, progress in volumes)
        lead_ids = spec.get("progressLead") or []
        if lead_ids:
            lead_read = 0.0
            lead_done = True
            for book_id in lead_ids:
                got = _volume_read(book_id)
                if not got:
                    lead_read = -1
                    break
                words, progress = got
                lead_read += words * (progress / 100.0)
                if progress < 100:
                    lead_done = False
            if lead_read < 0:
                continue
            if lead_done:
                floors = list(spec.get("afterLeadFloors") or [])
                read_words = 0.0
                for i, (words, progress) in enumerate(volumes):
                    floor = int(floors[i]) if i < len(floors) else 0
                    floor = max(0, min(100, floor))
                    effective = max(progress, floor)
                    read_words += words * (effective / 100.0)
            else:
                # Motie still in progress: scale lead words against the 12-vol total.
                read_words = lead_read

        pct = int(round(read_words / total_words * 100))
        store.set_series_book_stats(canonical, spec.get("title") or "", total_words, pct)


def sync_weread(year: int | None = None, month: int | None = None) -> dict:
    if not _weread_sync_guard.acquire(blocking=False):
        return {
            "ok": True,
            "inProgress": True,
            "skipped": True,
            "wereadSyncAt": store.weread_sync_at(),
        }
    try:
        return _sync_weread(year, month)
    finally:
        _weread_sync_guard.release()


def _sync_weread(year: int | None = None, month: int | None = None) -> dict:
    if not weread.has_session() and not weread.load_api_key():
        return {"ok": False, "error": "尚未登录微信读书"}
    try:
        if weread.has_session():
            weread.refresh_cookies()
        shelf = weread.shelf_books()
    except weread.SessionExpired as exc:
        return {"ok": False, "error": str(exc), "expired": True}
    except Exception as exc:
        return {"ok": False, "error": "读取书架失败：%s" % exc}

    store.ensure_book_merges()
    merge_aliases = store.merge_alias_ids()
    merge_canonicals = set(store.book_merges().keys())

    candidates = [b for b in shelf if _raw_id(b)][:50]
    used = 0
    entries = 0
    errors = 0
    expired = False
    for book in candidates:
        raw_id = _raw_id(book)
        book_id = "weread:" + raw_id
        # Aliases are folded into the canonical book later; skip their own rows.
        if book_id in merge_aliases or store.is_excluded_book(book_id):
            continue
        meta = {
            "cover": book.get("cover") or "",
            "lastOpen": int(book.get("ts") or 0),
            "wordCount": weread.pick_word_count(book) or None,
            "author": weread.pick_author(book) or None,
        }
        extra = {}
        try:
            extra = weread.fetch_book_meta(raw_id)
            if extra.get("cover"):
                meta["cover"] = extra["cover"]
            if extra.get("wordCount"):
                meta["wordCount"] = extra["wordCount"]
            if extra.get("author"):
                meta["author"] = extra["author"]
            if extra.get("progress") is not None:
                meta["progress"] = extra["progress"]
            if extra.get("lastOpen"):
                meta["lastOpen"] = extra["lastOpen"]
            if extra.get("finishDate"):
                meta["finishDate"] = extra["finishDate"]
            title = extra.get("title") or book.get("title") or ""
        except Exception:
            title = book.get("title") or ""
        store.upsert_book_full(book_id, title, "weread", meta)

        # Canonical merge targets: metadata only here; days written in _apply_book_merges.
        if book_id in merge_canonicals:
            used += 1
            continue

        daily = {}
        try:
            info = weread.book_readinfo(raw_id)
            daily = weread.parse_daily_seconds(info)
            if not daily:
                # WeRead 没给按日明细时：若用户已手动拆分，只清掉非自定义残行，
                # 绝不再用 fallback 把总时长重新堆到 last_open / 同一天（会双计）。
                if store.book_has_manual_days(book_id):
                    entries += store.replace_book_days(book_id, "weread", [])
                    used += 1
                    title = ((info.get("bookInfo") or {}).get("title") or title)
                    store.upsert_book_full(book_id, title, "weread", meta)
                    # updateTime can bump without new seconds; keep last_open on real days.
                    store.reconcile_last_open_from_entries(book_id)
                    continue
                daily = weread.fallback_daily_seconds(
                    info, int(meta.get("lastOpen") or 0)
                )
            title = ((info.get("bookInfo") or {}).get("title") or title)
            cover = ((info.get("bookInfo") or {}).get("cover") or meta.get("cover") or "")
            if cover:
                meta["cover"] = cover
            wc = weread.pick_word_count(info, extra)
            if wc:
                meta["wordCount"] = wc
            auth = weread.pick_author(info, extra)
            if auth:
                meta["author"] = auth
            fd = weread.pick_finish_date(info, extra)
            if fd:
                meta["finishDate"] = fd
            store.upsert_book_full(book_id, title, "weread", meta)
        except weread.SessionExpired:
            expired = True
            break
        except Exception:
            errors += 1
            continue
        if not daily:
            # Still clamp: shelf/meta lastOpen may be today's touch without seconds.
            store.reconcile_last_open_from_entries(book_id)
            continue
        used += 1
        rows = [{"day": day_iso, "seconds": int(seconds)} for day_iso, seconds in daily.items()]
        entries += store.replace_book_days(book_id, "weread", rows)

    merged_entries = 0
    if not expired:
        try:
            merged_entries = _apply_book_merges(shelf)
            entries += merged_entries
            _apply_piano_teacher_progress()
            # Catch any remaining shelf-only last_open bumps (incl. books skipped above).
            store.reconcile_all_last_opens_from_entries()
        except weread.SessionExpired:
            expired = True
        except Exception:
            errors += 1

    ok = (not expired) or used > 0 or merged_entries > 0
    result = {
        "ok": ok,
        "books": used,
        "entries": entries,
        "mergedEntries": merged_entries,
        "skipped": errors,
        "candidates": len(candidates),
    }
    if expired and used == 0:
        result["ok"] = False
        result["expired"] = True
        result["error"] = "网页登录已过期，请重新扫码后再同步按日明细"
    if result["ok"]:
        ts = weread._now()
        store.set_setting("wereadSyncAt", str(ts))
        if year and month:
            store.set_setting("wereadSyncMonth", "%04d-%02d" % (year, month))
        result["wereadSyncAt"] = ts
    else:
        result["wereadSyncAt"] = store.weread_sync_at()
    return result


# 繁体潘勋《鋼琴教師》：微信读书因误标读完把 progress 写成 100。
# 进度 = (该章开头字数 + 章内偏移) / 总字数。
_PIANO_BOOK_ID = "weread:CB_64i24y27dDdf77175ACIS9b7"
_PIANO_CHAPTER_BASE = {
    6: 70800,
    7: 167500,  # 16.75万
}


def piano_teacher_progress() -> int | None:
    """Progress from chapter offset. None when the cursor is not in a known chapter."""
    raw_id = _PIANO_BOOK_ID.split(":", 1)[1]
    try:
        prog = weread.web_get("/book/getprogress", params={"bookId": raw_id}, app_api=True)
    except weread.SessionExpired:
        raise
    except Exception:
        return None
    book = prog.get("book") if isinstance(prog.get("book"), dict) else {}
    try:
        idx = int(book.get("chapterIdx") or 0)
        offset = int(book.get("chapterOffset") or 0)
    except (TypeError, ValueError):
        return None
    base = _PIANO_CHAPTER_BASE.get(idx)
    if base is None:
        return None
    total = 0
    try:
        info = weread.web_get("/book/info", params={"bookId": raw_id}, app_api=True)
        total = int(info.get("totalWords") or info.get("wordCount") or 0)
    except Exception:
        total = 0
    if total <= 0:
        total = 176970
    read = base + max(0, offset)
    pct = int(round(read / total * 100))
    return max(0, min(100, pct))


def _apply_piano_teacher_progress() -> None:
    pct = piano_teacher_progress()
    if pct is None:
        return
    store.set_book_progress(_PIANO_BOOK_ID, pct, custom=True)


def sync_book_meta_field(book_id: str, field: str) -> dict:
    """Pull one metadata field (title / author / wordCount) from WeRead and clear its lock."""
    book_id = str(book_id or "").strip()
    field = str(field or "").strip()
    if field not in ("title", "author", "wordCount", "word_count"):
        return {"ok": False, "error": "不支持同步该字段"}
    if field == "word_count":
        field = "wordCount"
    if not book_id.startswith("weread:"):
        return {"ok": False, "error": "这本书不能从微信读书同步"}
    raw_id = book_id.split(":", 1)[1].strip()
    if not raw_id:
        return {"ok": False, "error": "找不到这本书"}
    if not weread.has_session() and not weread.load_api_key():
        return {"ok": False, "error": "尚未登录微信读书"}
    try:
        meta = weread.fetch_book_meta(raw_id)
    except weread.SessionExpired:
        return {"ok": False, "error": "网页登录已过期，请重新登录"}
    except Exception:
        return {"ok": False, "error": "同步失败"}
    value = meta.get(field)
    try:
        book = store.apply_synced_book_field(book_id, field, value)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    out = {"ok": True, "field": field, "book": book}
    if field == "title":
        out["title"] = book.get("title")
    elif field == "author":
        out["author"] = book.get("author")
    else:
        out["wordCount"] = book.get("wordCount")
    return out


def sync_book_progress(book_id: str) -> dict:
    """Pull one book's WeRead progress and clear the manual lock. Does not wipe on failure."""
    book_id = str(book_id or "").strip()
    if not book_id.startswith("weread:"):
        return {"ok": False, "error": "这本书不能同步微信读书进度"}
    raw_id = book_id.split(":", 1)[1].strip()
    if not raw_id:
        return {"ok": False, "error": "找不到这本书"}
    if book_id == _PIANO_BOOK_ID:
        try:
            pct = piano_teacher_progress()
        except weread.SessionExpired:
            return {"ok": False, "error": "网页登录已过期，请重新登录"}
        except Exception:
            return {"ok": False, "error": "同步阅读进度失败"}
        if pct is None:
            return {"ok": False, "error": "当前不在第 6 或第 7 章，无法按章内偏移计算进度"}
        try:
            book = store.set_book_progress(book_id, pct, custom=True)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "progress": book.get("progress"), "book": book}
    if not weread.has_session() and not weread.load_api_key():
        return {"ok": False, "error": "尚未登录微信读书"}
    try:
        progress = weread.fetch_reading_progress(raw_id)
    except weread.SessionExpired:
        return {"ok": False, "error": "网页登录已过期，请重新登录"}
    except Exception:
        return {"ok": False, "error": "同步阅读进度失败"}
    if progress is None:
        return {"ok": False, "error": "没有读到阅读进度"}
    try:
        book = store.set_book_progress(book_id, progress, custom=False)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "progress": book.get("progress"), "book": book}


def sync_ireader() -> dict:
    if not ireader.has_session():
        return {"ok": False, "error": "尚未登录掌阅"}
    try:
        shelf = ireader.shelf_books()
    except ireader.SessionExpired as exc:
        return {"ok": False, "error": str(exc), "expired": True}
    except Exception as exc:
        return {"ok": False, "error": "读取掌阅书架失败：%s" % exc}

    candidates = [b for b in shelf if _raw_id(b)][:80]
    used = 0
    errors = 0
    ids = [_raw_id(b) for b in candidates]
    details = {}
    progress = {}
    try:
        details = ireader.book_details(ids)
        progress = ireader.book_progress(ids)
    except ireader.SessionExpired as exc:
        return {"ok": False, "error": str(exc), "expired": True}
    except Exception:
        details, progress = {}, {}

    for book in candidates:
        raw_id = _raw_id(book)
        book_id = "ireader:" + raw_id
        if store.is_excluded_book(book_id):
            continue
        extra = details.get(raw_id) or {}
        prog = progress.get(raw_id) or {}
        title = (
            (extra.get("book_name") or extra.get("bookName") or extra.get("title") or "")
            or book.get("title")
            or ""
        )
        cover = (
            extra.get("icon")
            or extra.get("cover")
            or extra.get("pic")
            or book.get("cover")
            or ""
        )
        meta = {
            "cover": cover,
            "lastOpen": ireader._last_open(prog) or ireader._last_open(extra) or int(book.get("ts") or 0),
            "progress": ireader._progress_percent(prog) if prog else ireader._progress_percent(extra),
        }
        if not title:
            errors += 1
            continue
        try:
            store.upsert_book_full(book_id, title, "ireader", meta)
            # Same rule as WeRead: don't let shelf touch time beat real entry days.
            store.reconcile_last_open_from_entries(book_id)
            used += 1
        except Exception:
            errors += 1

    store.set_setting("ireaderSyncAt", str(ireader._now()))
    return {
        "ok": used > 0 or not errors,
        "books": used,
        "entries": 0,
        "skipped": errors,
        "candidates": len(candidates),
        "dailySync": False,
        "note": "已同步云书架。掌阅网页没有按日阅读分钟，日历请继续用 CSV 或手动添加。",
    }
