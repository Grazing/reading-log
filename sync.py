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


def _local_off_shelf(on_shelf: set) -> list:
    """WeRead ids already stored locally but absent from the current shelf."""
    on_shelf = {str(item) for item in (on_shelf or set())}
    with store.connect() as conn:
        rows = conn.execute("SELECT id, title FROM books WHERE id LIKE 'weread:%'").fetchall()
    out = []
    for row in rows:
        book_id = str(row["id"] or "")
        raw_id = book_id.split(":", 1)[1].strip() if ":" in book_id else ""
        if not raw_id or raw_id in on_shelf:
            continue
        out.append({"id": raw_id, "title": row["title"] or "", "ts": 0})
    return out


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


def _apply_book_merges(shelf: list | None = None, only: set[str] | None = None) -> int:
    """Fold alias edition dailies into each canonical book (day union, same-day sum).

    The canonical last_open becomes the latest open among the canonical volume
    and every merged member that still has a last_open. Each volume's finish
    date stays on that volume. An unfinished series canonical is not given a
    finish date, so 读完日期 sort keeps treating the series as in progress.

    ``only`` limits the fold to merge groups that intersect that id set (recent
    sync). None keeps every group, which is the full-sync path.
    """
    merges = store.ensure_book_merges()
    if only is not None:
        wanted = {str(item) for item in only if item}
        merges = {
            canon: aliases
            for canon, aliases in merges.items()
            if {canon, *aliases} & wanted
        }
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
    _apply_book_series(only)
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


def _apply_book_series(only: set[str] | None = None) -> None:
    """Word count = sum of members. Progress = words read / that total.

    progressLead (杨可意/磨铁) is an early parallel edition. While any lead volume
    is incomplete, progress = lead words-read / member total. Once the lead set
    is finished, map onto members via afterLeadFloors (e.g. Motie → 自导入 1–3
    @100% and 部4 @≥86%), then add later volumes by their actual WeRead %.
    Reading time is folded separately by the merge.
    """
    wanted = {str(item) for item in only if item} if only is not None else None
    for canonical, spec in store.book_series().items():
        members = spec.get("members") or []
        if wanted is not None:
            leads = list(spec.get("progressLead") or [])
            if not ({str(canonical), *map(str, members), *map(str, leads)} & wanted):
                continue
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


# Recent shelf width. Full sync is not sliced; only a resolved "recent" scope is.
RECENT_SHELF_LIMIT = 50


def weread_sync_established(sync_at: int, has_weread_books: bool) -> bool:
    """This data directory has completed a WeRead sync.

    Uses the existing wereadSyncAt timestamp. A timestamp with no local WeRead
    books is still an empty library, so automatic sync stays on the full path.
    """
    try:
        stamp = int(sync_at or 0)
    except (TypeError, ValueError):
        stamp = 0
    return stamp > 0 and bool(has_weread_books)


def _weread_auto_synced() -> bool:
    stamp = store.weread_sync_at()
    has_books = False
    if stamp > 0:
        with store.connect() as conn:
            has_books = (
                conn.execute(
                    "SELECT 1 FROM books WHERE id LIKE 'weread:%' LIMIT 1"
                ).fetchone()
                is not None
            )
    return weread_sync_established(stamp, has_books)


def resolve_weread_scope(requested: str | None, *, synced: bool) -> str:
    """Return ``full`` or ``recent``.

    ``full`` is「全部同步」 and always keeps the whole shelf.
    ``recent`` is「同步最近」 and always slices, including the first sync.
    Anything else (``auto``, omitted) is an automatic sync: full until this
    data directory has a successful sync, then the same recent slice.
    The ``synced`` flag is computed on the server; callers cannot supply it.
    """
    mode = str(requested or "").strip().lower()
    if mode == "full":
        return "full"
    if mode == "recent":
        return "recent"
    if not synced:
        return "full"
    return "recent"


def shelf_slice_for_scope(books: list, scope: str) -> list:
    """Keep shelf order (readUpdateTime descending). Slice only for recent."""
    rows = list(books or [])
    if scope != "recent":
        return rows
    return rows[:RECENT_SHELF_LIMIT]


def pulls_off_shelf(scope: str) -> bool:
    """Notes, annual ranks, and the local off-shelf scan belong to a full sync."""
    return scope != "recent"


def _merge_group_in_scope(book_id: str, only: set[str]) -> bool:
    book_id = str(book_id or "")
    if not book_id:
        return False
    if book_id in only:
        return True
    for canon, aliases in store.book_merges().items():
        group = {canon, *aliases}
        if book_id in group and group & only:
            return True
    return False


def sync_weread(year: int | None = None, month: int | None = None, scope: str | None = None) -> dict:
    if not _weread_sync_guard.acquire(blocking=False):
        return {
            "ok": True,
            "inProgress": True,
            "skipped": True,
            "wereadSyncAt": store.weread_sync_at(),
        }
    try:
        return _sync_weread(year, month, scope)
    finally:
        _weread_sync_guard.release()


def _sync_weread(year: int | None = None, month: int | None = None, scope: str | None = None) -> dict:
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

    # Shelf stays readUpdateTime descending. Full keeps every row plus off-shelf
    # sources. Recent keeps the first RECENT_SHELF_LIMIT and does not go looking
    # for books outside that slice.
    scope = resolve_weread_scope(scope, synced=_weread_auto_synced())
    on_shelf = [b for b in shelf if _raw_id(b)]
    off_shelf = []
    if pulls_off_shelf(scope):
        on_ids = {_raw_id(b) for b in on_shelf}
        seen = set(on_ids)
        for stub in list(weread.books_beyond_shelf(on_ids)) + _local_off_shelf(on_ids):
            raw_id = _raw_id(stub)
            book_id = "weread:" + raw_id
            if not raw_id or raw_id in seen:
                continue
            if book_id in merge_aliases or store.is_excluded_book(book_id):
                continue
            if store.preserves_synced_days(book_id):
                continue
            seen.add(raw_id)
            off_shelf.append(stub)
    else:
        on_shelf = shelf_slice_for_scope(on_shelf, scope)
    candidates = on_shelf + off_shelf
    used = 0
    entries = 0
    errors = 0
    expired = False
    for book in candidates:
        raw_id = _raw_id(book)
        book_id = "weread:" + raw_id
        # Aliases are folded into the canonical book later; skip their own rows.
        # Protected books are skipped entirely, on or off the shelf.
        if book_id in merge_aliases or store.is_excluded_book(book_id):
            continue
        if store.preserves_synced_days(book_id):
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
            if extra.get("isbn"):
                meta["isbn"] = extra["isbn"]
            if extra.get("publisher"):
                meta["publisher"] = extra["publisher"]
            if extra.get("translator"):
                meta["translator"] = extra["translator"]
            if extra.get("price"):
                meta["price"] = extra["price"]
            if extra.get("publishYear"):
                meta["publishYear"] = extra["publishYear"]
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
            isbn = weread.pick_isbn(info, extra)
            if isbn:
                meta["isbn"] = isbn
            pub = weread.pick_publisher(info, extra)
            if pub:
                meta["publisher"] = pub
            tr = weread.pick_translator(info, extra)
            if tr:
                meta["translator"] = tr
            price = weread.pick_price(info, extra)
            if price:
                meta["price"] = price
            # Do NOT reuse param name `year` — that shadows sync (year, month)
            # and later "%04d-%02d" % (year, month) raises TypeError on str.
            publish_year = weread.pick_publish_year(info, extra)
            if publish_year:
                meta["publishYear"] = publish_year
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
            if scope == "recent":
                only_ids = {"weread:" + _raw_id(b) for b in candidates if _raw_id(b)}
                merged_entries = _apply_book_merges(on_shelf, only=only_ids)
                if _merge_group_in_scope(_PIANO_BOOK_ID, only_ids):
                    _apply_piano_teacher_progress()
                heal_ids = {bid for bid in only_ids if not store.preserves_synced_days(bid)}
                _heal_bare_hundred_progress(heal_ids)
            else:
                merged_entries = _apply_book_merges(shelf)
                _apply_piano_teacher_progress()
                _heal_bare_hundred_progress()
            entries += merged_entries
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
        "onShelf": len(on_shelf),
        "offShelf": len(off_shelf),
        "scope": scope,
    }
    if expired and used == 0:
        result["ok"] = False
        result["expired"] = True
        result["error"] = "网页登录已过期，请重新扫码后再同步按日明细"
    if result["ok"]:
        ts = weread._now()
        store.set_setting("wereadSyncAt", str(ts))
        if year and month:
            store.set_setting("wereadSyncMonth", "%04d-%02d" % (int(year), int(month)))
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


def _heal_bare_hundred_progress(only: set[str] | None = None) -> None:
    """Re-resolve WeRead books stuck at 100% with no finish date via chapter pointer.

    ``only`` limits the pass to those ids (recent sync). None scans every
    stuck WeRead book, which is the full-sync path.
    """
    with store.connect() as conn:
        rows = conn.execute(
            """
            SELECT id FROM books
            WHERE id LIKE 'weread:%'
              AND COALESCE(progress, 0) >= 100
              AND TRIM(COALESCE(finish_date, '')) = ''
              AND COALESCE(progress_custom, 0) = 0
            """
        ).fetchall()
    wanted = {str(item) for item in only if item} if only is not None else None
    for row in rows:
        book_id = str(row["id"] or "")
        if not book_id or book_id == _PIANO_BOOK_ID:
            continue
        if wanted is not None and book_id not in wanted:
            continue
        raw_id = book_id.split(":", 1)[1].strip()
        if not raw_id:
            continue
        try:
            pct = weread.fetch_reading_progress(raw_id)
        except weread.SessionExpired:
            raise
        except Exception:
            continue
        if pct is None or pct >= 100:
            continue
        try:
            store.set_book_progress(book_id, pct, custom=False)
        except ValueError:
            continue


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
