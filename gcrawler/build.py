"""data/*.json + config → 정적 사이트(_site/) 빌드."""
from __future__ import annotations

import datetime as dt
import re
import unicodedata
import shutil
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

from collections import Counter

from .filedate import drop_shared_links, file_value, unreliable_file_days
from .filters import is_guideline
from .manual import load_manual
from .parse import GENERIC_LINK, year_from_title
from .store import DATA, ROOT, load_json, load_societies, load_taxonomy, save_json

SITE_SRC = ROOT / "site"
OUT = ROOT / "_site"
_NORM = re.compile(r"[^0-9a-z가-힣]+")
_FILE_URL = re.compile(r"(\.(pdf|hwp|hwpx|docx?|pptx?|xlsx?|zip)(?:$|[?#&])|download|filedown|file_down|fileDownload|down\.php|getfile)", re.I)


def norm_title(t: str) -> str:
    return _NORM.sub("", unicodedata.normalize("NFC", t or "").lower())


# ── 학회 간 같은 지침 찾기 ─────────────────────────
# 같은 지침을 여러 학회가 올리면 제목이 조금씩 다르다:
#   '2018 심방세동 치료 가이드라인' / '2018 대한부정맥학회 심방세동 치료 가이드라인'
#   '2022 고혈압 진료지침' / '[대한고혈압학회] 2022년 제5판 고혈압 진료지침'
#   '대한내과학회 COVID-19 백신 접종 권고안' / '대한내과학회 코로나19 백신 접종 권고안'
#   목록에서 잘린 제목 '…(NOAC) 사...' / '…비타민K비의존성 경구'
_SOC_NAME = re.compile(r"(대한|한국)[가-힣·\s]{1,20}?(학회|의사회|협회|연구회)|[가-힣]{2,12}학회|질병관리(본부|청)")
_COVID = re.compile(r"(covid\s*-?\s*19|코로나\s*19|코로나\s*바?이러스\s*감염증\s*-?\s*19|신종\s*코로나\s*바이러스\s*감염증)", re.I)
_EDITION = re.compile(r"(제\s*\d+\s*판|\d+\s*판|\(?\d+(st|nd|rd|th)\s*ed(ition)?\.?\)?|개정판)", re.I)
_TRUNC = re.compile(r"(\.\.\.|…)\s*$")


def fuzzy_key(t: str) -> str:
    """연도·학회명·판수·표기 차이를 지운 제목 열쇠 (연도는 따로 비교)."""
    t = unicodedata.normalize("NFC", t or "")
    if _TRUNC.search(t):
        t = re.sub(r"\S*$", "", _TRUNC.sub("", t))  # 잘린 제목은 끝의 잘린 낱말까지 버림
    t = _COVID.sub(" covid19 ", t)
    t = _SOC_NAME.sub(" ", t)
    t = _EDITION.sub(" ", t)
    t = re.sub(r"(19|20)\d{2}\s*년?\s*도?", " ", t)
    t = re.sub(r"지침서", "지침", t)
    return norm_title(t)


def _year_of(g: dict) -> str | None:
    ty = year_from_title(g.get("title", ""))
    if ty:
        return ty[0][:4]
    return (g.get("date") or "")[:4] or None


def _same_guideline(a: dict, b: dict) -> bool:
    if set(a["societies"]) & set(b["societies"]):
        return False  # 한 학회 안의 비슷한 제목은 판·요약본 등 서로 다른 문서일 수 있음
    ya, yb = _year_of(a), _year_of(b)
    if not ya or ya != yb:
        return False
    ka, kb = fuzzy_key(a["title"]), fuzzy_key(b["title"])
    if len(ka) < 6 or len(kb) < 6:
        return False
    if ka == kb:
        return True
    short, long_ = sorted((ka, kb), key=len)
    # 목록에서 잘린 제목: 짧은 쪽이 긴 쪽의 앞부분이고 충분히 길 때만
    return len(short) >= 15 and long_.startswith(short)


def _display_title(titles: list[str]) -> str:
    # 내용이 가장 긴 제목, 같으면 잘리지 않은 쪽, 그다음 군더더기(학회명·판수)가 적은 짧은 쪽
    return max(titles, key=lambda t: (len(fuzzy_key(t)), not _TRUNC.search(t), -len(t)))


def _absorb_group(target: dict, g: dict) -> None:
    for s in g["sources"]:
        if not any(t["society"] == s["society"] and t["url"] == s["url"] for t in target["sources"]):
            target["sources"].append(s)
    target["_titles"] = target["_titles"] + g["_titles"]
    target["title"] = _display_title(target["_titles"])
    rank = {"day": 3, "month": 2, "year": 1}
    if rank.get(g.get("date_precision"), 0) > rank.get(target.get("date_precision"), 0):
        target.update({k: g.get(k) for k in ("date", "date_precision", "date_source", "date_ref", "posted")})
    if (g.get("first_seen") or "9999") < (target.get("first_seen") or "9999"):
        target["first_seen"] = g["first_seen"]
    target["backfill"] = target["backfill"] and g["backfill"]
    target["tags"] = sorted(set(target["tags"]) | set(g["tags"]))
    target["societies"] = list(dict.fromkeys(target["societies"] + g["societies"]))
    target["groups"] = sorted(set(target["groups"]) | set(g["groups"]))
    target["divisions"] = sorted(set(target["divisions"]) | set(g["divisions"]))


def _settle_year(g: dict) -> None:
    """학회마다 날짜가 다르면(게시일·재게시일) 제목에 적힌 발행연도를 따른다."""
    ys = {y[0][:4] for t in g["_titles"] if (y := year_from_title(t))}
    if len(ys) == 1 and g.get("date") and g["date"][:4] not in ys and g.get("date_source") != "lookup":
        y = ys.pop()
        if g["date"][:4] > y:
            g["posted"] = g["date"]
        g.update(date=y, date_precision="year", date_source="title")


def merge_across_societies(groups: list[dict], same_as: list[list[dict]] | None = None) -> list[dict]:
    """학회마다 제목이 조금 다른 같은 지침을 한 항목으로 합친다.
    1) 자동: 다른 학회 + 같은 연도 + fuzzy_key가 같거나 잘린 제목의 앞부분
    2) 수동: config/duplicates.yaml에 적은 묶음 (학회 id + 제목 일부)"""
    for g in groups:
        g.setdefault("_titles", [g["title"]])
    out: list[dict] = []
    for g in groups:
        target = next((o for o in out if _same_guideline(o, g)), None)
        if target is None:
            out.append(g)
        else:
            _absorb_group(target, g)
    for bundle in same_as or []:
        found: list[dict] = []
        for m in bundle:
            for o in out:
                if m.get("society") in o["societies"] and any(str(m.get("title", "")) in t for t in o["_titles"]):
                    if all(o is not f for f in found):
                        found.append(o)
                    break
        keep = found[0]["title"] if found else None  # 수동 묶음은 처음 적은 학회의 제목을 쓴다
        for o in found[1:]:
            _absorb_group(found[0], o)
            out = [x for x in out if x is not o]
        if keep:
            found[0]["title"] = keep
    for g in out:
        if len(g["societies"]) > 1:
            _settle_year(g)
        g.pop("_titles", None)
    return out


def fix_dates(items: list[dict]) -> list[dict]:
    """게시일이 발행 시점을 반영하지 못하는 경우를 바로잡는다.
    - 일괄 등록: 한 학회에서 같은 날짜가 5건 이상·25% 이상이면 사이트 이전 등으로 한꺼번에 올린 날짜로 간주
    - 옛 지침 재게시: 제목 연도가 게시 연도보다 2년 이상 이르면 제목 연도를 발행연도로
    원래 게시일은 posted에 남긴다."""
    items = [dict(it, attachments=[dict(a) for a in it.get("attachments", []) or []]) for it in items]
    drop_shared_links(items)
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
    bulk_pairs = bulk
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
        # 상세 페이지의 '연도' 칸: 학회가 직접 적은 발행연도. 날짜가 없거나 일괄 등록일이거나 그보다 늦은 게시일이면 이것을 쓴다
        py = it.get("pub_year")
        if py and (not it.get("date") or (it.get("society"), it.get("date")) in bulk_pairs or it["date"][:4] != py):
            if it.get("date") and it["date"][:4] > py and (it.get("society"), it["date"]) not in bulk_pairs:
                it["posted"] = it["date"]
            it.update(date=py, date_precision="year", date_source="detail-year")
        d, prec = it.get("date"), it.get("date_precision")
        ty = year_from_title(it.get("title", ""))
        if d and prec == "day" and (it.get("society"), d) in bulk:
            # 일괄 등록일(사이트 이전일)은 실제 게시일이 아니므로 '게시'로 표시하지 않는다
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


def load_duplicates() -> list[list[dict]]:
    import yaml

    p = ROOT / "config" / "duplicates.yaml"
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or []) if p.exists() else []


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
                boards: dict[str, str] | None = None, same_as: list[list[dict]] | None = None) -> list[dict]:
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
        if it.get("doi") and all(f.get("url") != it["doi"] for f in files):
            files.insert(0, {"name": "원문 (DOI)", "url": it["doi"], "kind": "원문"})
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
    out = merge_across_societies(out, same_as)
    out.sort(key=lambda g: ((g.get("date") or g.get("first_seen") or ""), g["title"]), reverse=True)
    return out


def published_within_year(g: dict, today: dt.date | None = None) -> bool:
    """발간일이 최근 1년 안인지. 연도·월만 아는 항목은 그 기간의 첫날로 따져 확실한 것만 (사이트 NEW 규칙과 같음)."""
    d = g.get("date") or ""
    m = re.match(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?", d)
    if not m:
        return False
    start = dt.date(int(m.group(1)), int(m.group(2) or 1), int(m.group(3) or 1))
    today = today or dt.date.today()
    try:
        cutoff = today.replace(year=today.year - 1)
    except ValueError:  # 2월 29일
        cutoff = today.replace(year=today.year - 1, day=28)
    return start >= cutoff


def write_feed(items: list[dict], socs: dict[str, dict], base_url: str, path: Path) -> None:
    # 새로 올라온 글(초기 수집분 제외) 중 발간 1년 이내만
    fresh = (g for g in items if not g.get("backfill") and published_within_year(g))
    latest = sorted(fresh, key=lambda g: g.get("first_seen") or "", reverse=True)[:60]
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
    # 수동 등록에 added(등록일)가 없으면 수집일을 모르는 것으로 둔다 (빌드할 때마다 '오늘'이 되면 NEW가 계속 붙음)
    boards = society_boards(socs_list, status, load_json(DATA / "discovered.json", {}))
    items = merge_items(doc.get("items", []) + manual, socs, load_overrides(), boards, load_duplicates())
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
