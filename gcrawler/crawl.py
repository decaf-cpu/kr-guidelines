"""학회별 수집 실행."""
from __future__ import annotations

import datetime as dt
import logging
import re
from urllib.parse import unquote
from concurrent.futures import ThreadPoolExecutor

from . import adapters
from .access import check_detail, page_requires_login
from .discover import discover
from .fetch import Fetcher
from .filters import KOREAN_ORG, is_guideline, lang_of, tags_for
from .parse import (
    Row,
    canonical_url,
    detect_page_param,
    extract_rows,
    find_month,
    page_urls,
    year_from_title,
)
from .store import DATA, Store, item_id, load_json, now_iso, save_json, today

log = logging.getLogger(__name__)

PRECISION_RANK = {"day": 3, "month": 2, "year": 1, None: 0}
REDISCOVER_DAYS = 7


def _better_date(old: tuple | None, new: tuple | None) -> bool:
    if not new or not new[0]:
        return False
    if not old or not old[0]:
        return True
    return PRECISION_RANK.get(new[1], 0) > PRECISION_RANK.get(old[1], 0)


def _boards_for(fetcher: Fetcher, soc: dict, discovered: dict, rediscover: bool) -> tuple[list[dict], dict | None]:
    if soc.get("boards"):
        return [dict(b, source="config") for b in soc["boards"]], None
    cache = discovered.get(soc["id"])
    fresh = False
    if cache and not rediscover and cache.get("ok") and cache.get("homepage") == soc["homepage"]:
        try:
            age = (dt.date.fromisoformat(today()) - dt.date.fromisoformat(cache["checked"])).days
            fresh = age < REDISCOVER_DAYS
        except (KeyError, ValueError):
            fresh = False
    if not fresh:
        res = discover(fetcher, soc["homepage"], verify=soc.get("verify_ssl", True))
        cache = {"checked": today(), "homepage": soc["homepage"], **res}
        discovered[soc["id"]] = cache
    boards = [
        {"url": c["url"], "name": c["label"], "dedicated": True, "source": "discovered"}
        for c in cache.get("candidates", [])
    ]
    return boards, cache


def crawl_board(fetcher: Fetcher, soc: dict, board: dict, store: Store, mode: str, max_pages: int,
                detail_budget: list[int], adapter) -> dict:
    verify = soc.get("verify_ssl", True)
    result = {"url": board["url"], "name": board.get("name"), "source": board.get("source"),
              "pages": 0, "rows": 0, "matched": 0, "new": 0, "members_only": 0, "error": None}

    first_html, resp = fetcher.get_html(board["url"], verify=verify)
    if first_html is None:
        code = resp.status_code if resp is not None else None
        result["error"] = f"목록 접속 실패 (HTTP {code})" if code else "목록 접속 실패"
        return result

    if page_requires_login(first_html):
        result["error"] = "회원 전용 게시판 (로그인 필요)"
        return result

    if adapter is not None and hasattr(adapter, "page_url"):
        urls = [board["url"]] + [u for n in range(2, max_pages + 1) if (u := adapter.page_url(board, n))]
    else:
        page2 = detect_page_param(first_html, resp.url, board.get("page_param"))
        urls = page_urls(resp.url, page2, max_pages)

    prev_titles: set[str] | None = None
    for idx, url in enumerate(urls):
        if idx == 0:
            html, base = first_html, resp.url
        else:
            html, r = fetcher.get_html(url, verify=verify)
            if html is None:
                break
            base = r.url
        if adapter is not None and hasattr(adapter, "parse_list"):
            rows: list[Row] = adapter.parse_list(html, base, board)
        else:
            rows = extract_rows(html, base, board)
        result["pages"] += 1
        if not rows:
            break
        titles = {r.title for r in rows if not r.notice}
        if prev_titles is not None and titles and titles == prev_titles:
            break  # 페이지 번호를 무시하고 같은 목록을 주는 게시판
        prev_titles = titles
        result["rows"] += len(rows)

        new_here = 0
        for row in rows:
            if not is_guideline(row.title, board.get("dedicated", False)):
                continue
            if row.foreign and not KOREAN_ORG.search(row.title):
                continue
            result["matched"] += 1
            canon = canonical_url(row.url) if row.url else None
            iid = item_id(soc["id"], canon, row.title, board["url"])
            existing = store.get(iid)
            fields = {
                "society": soc["id"],
                "board": board["url"],
                "board_name": board.get("name"),
                "title": row.title,
                "url": row.url or base,
                "link_resolved": bool(row.url),
                "lang": lang_of(row.title),
                "tags": tags_for(row.title),
            }
            if row.attachments:
                fields["attachments"] = row.attachments
            old_date = (existing.get("date"), existing.get("date_precision")) if existing else None
            if _better_date(old_date, (row.date, row.date_precision)):
                fields.update(date=row.date, date_precision=row.date_precision, date_source="list")
            it, is_new = store.upsert(iid, fields)
            if is_new:
                new_here += 1
                result["new"] += 1

            # 상세 확인: 새 글이거나 아직 공개 여부를 모르는 글만, 학회당 횟수 제한
            if row.url and it.get("access") == "unknown" and detail_budget[0] > 0:
                detail_budget[0] -= 1
                info = check_detail(fetcher, row.url, verify=verify)
                it["access"] = info.get("access", "unknown")
                it["checked"] = today()
                if info.get("attachments") and not it.get("attachments"):
                    it["attachments"] = info["attachments"]
                cur = (it.get("date"), it.get("date_precision"))
                if _better_date(cur, info.get("detail_date")):
                    it["date"], it["date_precision"] = info["detail_date"]
                    it["date_source"] = "detail"
            elif not row.url and it.get("access") == "unknown":
                it["access"] = "public"  # 목록은 공개, 개별 주소만 못 얻은 경우
            if it.get("access") == "members":
                result["members_only"] += 1

            cur = (it.get("date"), it.get("date_precision"))
            if not cur[0]:
                y = find_month(row.title) or year_from_title(row.title)
                if not y and row.url:
                    # 'https://.../2026_guidelines.pdf'처럼 파일명에 적힌 연도
                    y = year_from_title(re.sub(r"[_\-.]", " ", unquote(row.url.rsplit("/", 1)[-1])))
                if y:
                    it["date"], it["date_precision"], it["date_source"] = y[0], y[1], "title"

        if mode == "incremental" and idx >= 1 and new_here == 0:
            break
    return result


def run(society_ids: list[str] | None = None, mode: str = "incremental", max_pages: int | None = None,
        detail_per_society: int = 30, rediscover: bool = False, fetcher: Fetcher | None = None,
        societies: list[dict] | None = None, workers: int = 6) -> dict:
    from .store import load_societies

    socs = societies if societies is not None else load_societies()
    if society_ids:
        socs = [s for s in socs if s["id"] in society_ids]
    fetcher = fetcher or Fetcher()
    max_pages = max_pages or (30 if mode == "full" else 3)

    store = Store()
    status_path = DATA / "status.json"
    status = load_json(status_path, {"societies": {}})
    discovered = load_json(DATA / "discovered.json", {})

    def one(soc: dict) -> None:
        log.info("▶ %s (%s)", soc["name"], soc["id"])
        rec = {"id": soc["id"], "run_at": now_iso(), "boards": [], "error": None}
        try:
            boards, disc = _boards_for(fetcher, soc, discovered, rediscover)
            if disc is not None:
                rec["discovery"] = {k: disc.get(k) for k in ("checked", "ok", "error")}
            if not boards:
                rec["error"] = (disc or {}).get("error") or "게시판 없음"
            adapter = adapters.load(soc["id"])
            budget = [detail_per_society]
            for b in boards:
                try:
                    rec["boards"].append(crawl_board(fetcher, soc, b, store, mode, max_pages, budget, adapter))
                except Exception as e:  # 한 게시판 오류가 전체를 멈추지 않게
                    log.exception("게시판 오류 %s", b["url"])
                    rec["boards"].append({"url": b["url"], "error": f"{type(e).__name__}: {e}"})
        except Exception as e:
            log.exception("학회 오류 %s", soc["id"])
            rec["error"] = f"{type(e).__name__}: {e}"
        matched = sum(b.get("matched", 0) for b in rec["boards"])
        rec["ok"] = rec["error"] is None and matched > 0 and not all(b.get("error") for b in rec["boards"])
        if rec["error"] is None and matched == 0 and rec["boards"]:
            rec["error"] = "게시판은 열렸으나 진료지침 항목 0건 (파서 설정 확인 필요)"
        status["societies"][soc["id"]] = rec
        log.info("  %s → 일치 %d건, 신규 %d건", soc["id"], matched, sum(b.get("new", 0) for b in rec["boards"]))

    targets = [s for s in socs if s.get("enabled", True)]
    # 학회마다 사이트가 다르므로 여러 학회를 동시에 수집 (같은 사이트 간격은 Fetcher가 보장)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        list(ex.map(one, targets))

    status["updated"] = now_iso()
    status["requests"] = fetcher.request_count
    store.save()
    save_json(status_path, status)
    save_json(DATA / "discovered.json", discovered)
    return {"new": len(store.new_ids), "total": len(store.items), "requests": fetcher.request_count}
