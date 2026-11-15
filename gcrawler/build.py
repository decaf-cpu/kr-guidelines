"""data/*.json + config → 정적 사이트(_site/) 빌드."""
from __future__ import annotations

import datetime as dt
import re
import shutil
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

from collections import Counter

from .filedate import file_value, unreliable_file_days
from .filters import is_guideline
from .manual import load_manual
from .parse import GENERIC_LINK, year_from_title
from .store import DATA, ROOT, load_json, load_societies, load_taxonomy, save_json

SITE_SRC = ROOT / "site"
OUT = ROOT / "_site"
_NORM = re.compile(r"[^0-9a-z가-힣]+")
_FILE_URL = re.compile(r"(\.(pdf|hwp|hwpx|docx?|pptx?|xlsx?|zip)(?:$|[?#&])|download|filedown|file_down|fileDownload|down\.php|getfile)", re.I)


def norm_title(t: str) -> str:
    return _NORM.sub("", (t or "").lower())


def fix_dates(items: list[dict]) -> list[dict]:
    """게시일이 발행 시점을 반영하지 못하는 경우를 바로잡는다.
    - 일괄 등록: 한 학회에서 같은 날짜가 5건 이상·25% 이상이면 사이트 이전 등으로 한꺼번에 올린 날짜로 간주
    - 옛 지침 재게시: 제목 연도가 게시 연도보다 2년 이상 이르면 제목 연도를 발행연도로
    원래 게시일은 posted에 남긴다."""
    by_soc: dict[str, Counter] = {}
    totals: Counter = Counter()
    for it in items:
        totals[it.get("society")] += 1
        if it.get("date_precision") == "day" and it.get("date"):
            by_soc.setdefault(it.get("society"), Counter())[it["date"]] += 1
    bulk = {
        (soc, d)
        for soc, c in by_soc.items()
        for d, n in c.items()
        if n >= 5 and n >= 0.25 * totals[soc]
    }
    # 서로 다른 지침이 같은 파일 날짜를 가지면 문서 날짜가 아니라 양식·재저장 날짜 → 버리고 PDF 본문의 발행연도를 쓴다
    bad_file = unreliable_file_days(items)
    out = []
    for it in items:
        it = dict(it)
        if it.get("date_source") == "file-lastmod" and it.get("date") and it.get("file_checked"):
            try:
                if (dt.date.fromisoformat(it["file_checked"]) - dt.date.fromisoformat(it["date"])).days <= 3:
                    it.update(date=None, date_precision=None, date_source=None)
            except ValueError:
                pass
        file_bad = (it.get("society"), file_value(it)) in bad_file
        if file_bad:
            it.pop("file_date", None)
            if (it.get("date_source") or "").startswith("file"):
                it.update(date=None, date_precision=None, date_source=None)
        if not it.get("date") and it.get("text_year"):
            it.update(date=it["text_year"], date_precision="year", date_source="file-text")
        d, prec = it.get("date"), it.get("date_precision")
        ty = year_from_title(it.get("title", ""))
        if d and prec == "day" and (it.get("society"), d) in bulk:
            it["posted"] = d
            fd = it.get("file_date")
            if ty:
                it.update(date=ty[0], date_precision=ty[1], date_source="title")
            elif it.get("text_year"):
                it.update(date=it["text_year"], date_precision="year", date_source="file-text")
            elif fd and fd[0] != d:
                it.update(date=fd[0], date_precision=fd[1], date_source=fd[2])
            else:
                it["date_source"] = "bulk"
        elif d and ty and abs(int(d[:4]) - int(ty[0][:4])) >= 2:
            if int(d[:4]) > int(ty[0][:4]):
                it["posted"] = d  # 옛 지침을 나중에 올린 경우만 게시일 표시 (지침보다 이른 게시일은 잘못된 날짜)
            it.update(date=ty[0], date_precision=ty[1], date_source="title")
        if it.get("posted") and it.get("date") and it["posted"][:4] < it["date"][:4]:
            it.pop("posted")  # 지침 연도보다 이른 게시일은 사이트 이전 등으로 생긴 잘못된 날짜
        out.append(it)
    return out


def society_boards(socs_list: list[dict], status: dict, discovered: dict) -> dict[str, str]:
    """학회별 진료지침 게시판 주소: 설정한 게시판 → 실제 수집된 게시판 → 자동 탐색 후보 → 홈페이지."""
    out = {}
    for s in socs_list:
        url = None
        if s.get("boards"):
            url = s["boards"][0]["url"]
        if not url:
            rec = (status.get("societies") or {}).get(s["id"]) or {}
            ok = [b["url"] for b in rec.get("boards", []) if not b.get("error") and b.get("matched")]
            url = ok[0] if ok else None
        if not url:
            cands = (discovered.get(s["id"]) or {}).get("candidates") or []
            url = cands[0]["url"] if cands else None
        out[s["id"]] = url or s.get("homepage")
    return out


def load_overrides() -> list[dict]:
    import yaml

    p = ROOT / "config" / "overrides.yaml"
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or []) if p.exists() else []


def apply_overrides(items: list[dict], overrides: list[dict]) -> list[dict]:
    """문헌으로 확인한 발행연도(config/overrides.yaml)를 덮어쓴다."""
    out = []
    for it in items:
        for o in overrides:
            if o.get("society") == it.get("society") and str(o.get("title", "")) in (it.get("title") or ""):
                d = str(o["date"])
                it = dict(it, date=d, date_precision={4: "year", 7: "month"}.get(len(d), "day"), date_source="lookup", date_ref=o.get("source"))
                it.pop("posted", None)
                break
        out.append(it)
    return out


def merge_items(items: list[dict], socs: dict[str, dict], overrides: list[dict] | None = None,
                boards: dict[str, str] | None = None) -> list[dict]:
    boards = boards or {}
    items = apply_overrides(fix_dates(items), overrides or [])
    groups: dict[str, dict] = {}
    for it in items:
        if it.get("access") == "members":
            continue
        # 예전 규칙으로 들어온 항목도 현재 규칙으로 다시 거른다
        if not it.get("manual") and (not is_guideline(it.get("title", ""), dedicated=True) or GENERIC_LINK.match(it.get("title", ""))):
            continue
        soc = socs.get(it.get("society"))
        if soc is None or (not soc.get("enabled", True) and not it.get("manual")):
            continue
        key = norm_title(it["title"])
        url = it.get("url")
        files = list(it.get("attachments", []))
        if url and not it.get("manual") and _FILE_URL.search(url) and it.get("board"):
            # 제목 링크가 파일 자체면: 제목은 학회 게시 페이지로, 파일은 첨부로
            if all(f.get("url") != url for f in files):
                files.insert(0, {"name": it["title"], "url": url, "kind": (re.search(r"\.(pdf|hwp|hwpx|docx?|pptx?)", url, re.I) or [None, "file"])[1].lower()})
            url = it["board"]
        main = boards.get(it["society"])
        ib = it.get("board")
        # 학회명 링크: 그 지침이 실린 진료지침 게시판 (같은 게시판의 분류·쪽 주소면 게시판 첫 화면)
        board = main if not ib or (main and ib.startswith(main.split("#")[0])) else ib
        src = {
            "society": it["society"],
            "url": url,
            "board": board or (soc.get("homepage") if soc else None),
            "board_name": it.get("board_name"),
            "attachments": files[:6],
            "link_resolved": it.get("link_resolved", True),
            "manual": bool(it.get("manual")),
        }
        g = groups.get(key)
        if g is None:
            g = {
                "id": it["id"],
                "title": it["title"],
                "date": it.get("date"),
                "date_precision": it.get("date_precision"),
                "date_source": it.get("date_source"),
                "date_ref": it.get("date_ref"),
                "posted": it.get("posted"),
                "first_seen": it.get("first_seen"),
                "backfill": bool(it.get("backfill", True)),
                "lang": it.get("lang"),
                "tags": list(it.get("tags", [])),
                "access": it.get("access"),
                "sources": [src],
            }
            groups[key] = g
            continue
        if not any(s["society"] == src["society"] and s["url"] == src["url"] for s in g["sources"]):
            g["sources"].append(src)
        rank = {"day": 3, "month": 2, "year": 1}
        if rank.get(it.get("date_precision"), 0) > rank.get(g.get("date_precision"), 0):
            g.update(date=it.get("date"), date_precision=it.get("date_precision"), date_source=it.get("date_source"))
        if (it.get("first_seen") or "9999") < (g.get("first_seen") or "9999"):
            g["first_seen"] = it["first_seen"]
        g["backfill"] = g["backfill"] and bool(it.get("backfill", True))
        g["tags"] = sorted(set(g["tags"]) | set(it.get("tags", [])))
    out = list(groups.values())
    for g in out:
        ss = [socs[s["society"]] for s in g["sources"]]
        ss = list({s["id"]: s for s in ss}.values())
        g["groups"] = sorted({s["group"] for s in ss})
        g["divisions"] = sorted({s["division"] for s in ss})
        g["societies"] = list(dict.fromkeys(s["id"] for s in ss))
    out.sort(key=lambda g: ((g.get("date") or g.get("first_seen") or ""), g["title"]), reverse=True)
    return out


def write_feed(items: list[dict], socs: dict[str, dict], base_url: str, path: Path) -> None:
    latest = sorted((g for g in items if not g.get("backfill")), key=lambda g: g.get("first_seen") or "", reverse=True)[:60]
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0"><channel>',
        "<title>국내 학회 진료지침 신규 수집</title>",
        f"<link>{escape(base_url)}</link>",
        "<description>대한의학회 회원학회 게시판에서 새로 수집된 진료지침</description>",
        "<language>ko</language>",
    ]
    for g in latest:
        names = ", ".join(socs[s]["short"] for s in g["societies"] if s in socs)
        when = dt.datetime.fromisoformat((g.get("first_seen") or "2000-01-01") + "T09:00:00+09:00")
        link = g["sources"][0]["url"] or base_url
        parts += [
            "<item>",
            f"<title>{escape(g['title'])}</title>",
            f"<link>{escape(link)}</link>",
            f"<guid isPermaLink=\"false\">{g['id']}</guid>",
            f"<category>{escape(', '.join(g['divisions']))}</category>",
            f"<description>{escape(names)} · {escape(g.get('date') or '날짜 미상')}</description>",
            f"<pubDate>{format_datetime(when)}</pubDate>",
            "</item>",
        ]
    parts.append("</channel></rss>")
    path.write_text("\n".join(parts), encoding="utf-8")


def build(base_url: str = "./") -> dict:
    socs_list = load_societies()
    socs = {s["id"]: s for s in socs_list}
    doc = load_json(DATA / "guidelines.json", {"items": []})
    status = load_json(DATA / "status.json", {"societies": {}})
    manual, manual_errors = load_manual(socs)
    for m in manual:
        m["first_seen"] = m["first_seen"] or (doc.get("updated") or "")[:10] or None
    boards = society_boards(socs_list, status, load_json(DATA / "discovered.json", {}))
    items = merge_items(doc.get("items", []) + manual, socs, load_overrides(), boards)
    hidden = sum(1 for it in doc.get("items", []) if it.get("access") == "members")

    if OUT.exists():
        shutil.rmtree(OUT)
    shutil.copytree(SITE_SRC, OUT)
    (OUT / "data").mkdir(exist_ok=True)

    save_json(OUT / "data" / "guidelines.json", {"updated": doc.get("updated"), "items": items})
    save_json(
        OUT / "data" / "societies.json",
        {
            "taxonomy": load_taxonomy(),
            "societies": [
                {**{k: s.get(k) for k in ("id", "name", "short", "homepage", "group", "specialty", "division", "enabled", "note", "manual")},
                 "board": boards.get(s["id"]) if s.get("enabled", True) or s.get("manual") else s.get("homepage")}
                for s in socs_list
            ],
        },
    )
    save_json(OUT / "data" / "status.json", {**status, "hidden_members_only": hidden, "manual_errors": manual_errors})
    write_feed(items, socs, base_url, OUT / "feed.xml")
    (OUT / ".nojekyll").write_text("")
    for err in manual_errors:
        print("⚠", err)
    return {"items": len(items), "hidden_members_only": hidden, "manual": len(manual)}
