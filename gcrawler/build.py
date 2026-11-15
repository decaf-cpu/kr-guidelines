"""data/*.json + config → 정적 사이트(_site/) 빌드."""
from __future__ import annotations

import datetime as dt
import re
import shutil
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

from collections import Counter

from .manual import load_manual
from .parse import year_from_title
from .store import DATA, ROOT, load_json, load_societies, load_taxonomy, save_json

SITE_SRC = ROOT / "site"
OUT = ROOT / "_site"
_NORM = re.compile(r"[^0-9a-z가-힣]+")


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
        if it.get("date_precision") == "day":
            by_soc.setdefault(it.get("society"), Counter())[it["date"]] += 1
    bulk = {
        (soc, d)
        for soc, c in by_soc.items()
        for d, n in c.items()
        if n >= 5 and n >= 0.25 * totals[soc]
    }
    out = []
    for it in items:
        it = dict(it)
        d, prec = it.get("date"), it.get("date_precision")
        ty = year_from_title(it.get("title", ""))
        if d and prec == "day" and (it.get("society"), d) in bulk:
            it["posted"] = d
            if ty:
                it.update(date=ty[0], date_precision="year", date_source="title")
            else:
                it["date_source"] = "bulk"
        elif d and ty and abs(int(d[:4]) - int(ty[0])) >= 2:
            it["posted"] = d
            it.update(date=ty[0], date_precision="year", date_source="title")
        out.append(it)
    return out


def merge_items(items: list[dict], socs: dict[str, dict]) -> list[dict]:
    items = fix_dates(items)
    groups: dict[str, dict] = {}
    for it in items:
        if it.get("access") == "members":
            continue
        soc = socs.get(it.get("society"))
        if soc is None or (not soc.get("enabled", True) and not it.get("manual")):
            continue
        key = norm_title(it["title"])
        src = {
            "society": it["society"],
            "url": it.get("url"),
            "board_name": it.get("board_name"),
            "attachments": it.get("attachments", [])[:6],
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
                "posted": it.get("posted"),
                "first_seen": it.get("first_seen"),
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
        g["tags"] = sorted(set(g["tags"]) | set(it.get("tags", [])))
    out = list(groups.values())
    for g in out:
        ss = [socs[s["society"]] for s in g["sources"]]
        g["groups"] = sorted({s["group"] for s in ss})
        g["divisions"] = sorted({s["division"] for s in ss})
        g["societies"] = [s["id"] for s in ss]
    out.sort(key=lambda g: ((g.get("date") or g.get("first_seen") or ""), g["title"]), reverse=True)
    return out


def write_feed(items: list[dict], socs: dict[str, dict], base_url: str, path: Path) -> None:
    latest = sorted(items, key=lambda g: g.get("first_seen") or "", reverse=True)[:60]
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
    items = merge_items(doc.get("items", []) + manual, socs)
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
                {k: s.get(k) for k in ("id", "name", "short", "homepage", "group", "specialty", "division", "enabled", "note", "manual")}
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
