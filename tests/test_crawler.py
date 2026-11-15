from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

import pytest
import requests

from gcrawler import crawl as crawl_mod
from gcrawler import store as store_mod
from gcrawler.discover import find_candidates
from gcrawler.fetch import Fetcher, decode
from gcrawler.filters import is_guideline
from gcrawler.parse import canonical_url, detect_page_param, extract_rows, find_date, year_from_title

FX = Path(__file__).parent / "fixtures"


def fx(name: str) -> bytes:
    return (FX / name).read_bytes()


def html(name: str, enc: str = "utf-8") -> str:
    return fx(name).decode(enc)


# ── 단위 테스트 ─────────────────────────────────
def test_dates():
    assert find_date("등록일 2025.06.12") == ("2025-06-12", "day")
    assert find_date("25-06-12") == ("2025-06-12", "day")
    assert find_date("2024년 5월 3일") == ("2024-05-03", "day")
    assert find_date("v1.2.3") is None
    assert year_from_title("Korean J Gastroenterol 2026; 86(4)") == ("2026", "year")
    assert year_from_title("제5판") is None


def test_filters():
    assert is_guideline("2025 만성 B형간염 진료 가이드라인")
    assert is_guideline("Seoul Consensus on Functional Constipation")
    assert not is_guideline("2026 춘계 학술대회 사전등록 안내")
    assert not is_guideline("진료지침 공청회 개최 안내")
    assert is_guideline("진료지침 공청회 자료 및 전문 공개")
    assert not is_guideline("회비 납부 안내", dedicated=True)
    assert is_guideline("강직척추염 치료", dedicated=True)


def test_canonical_url_drops_page():
    a = canonical_url("https://x.org/bbs2/index.php?code=guide2&page=1&number=5&mode=view&keyfield=&key=")
    b = canonical_url("https://x.org/bbs2/index.php?mode=view&number=5&code=guide2&page=2")
    assert a == b


def test_ksg_table_board():
    rows = extract_rows(html("ksg_guide2.html"), "https://gastrokorea.org/bbs2/index.php?code=guide2")
    assert len(rows) == 4
    r = rows[0]
    assert r.title.startswith("KASID Guidelines for Sexuality")
    assert "number=2539" in r.url
    assert r.attachments and "download.php" in r.attachments[0]["url"]
    assert len(rows[2].attachments) == 2
    page2 = detect_page_param(html("ksg_guide2.html"), "https://gastrokorea.org/bbs2/index.php?code=guide2")
    assert page2[0] == "page"


def test_gnuboard_euckr():
    resp = requests.Response()
    resp._content = fx("gnuboard_euckr.html")
    resp.headers["content-type"] = "text/html"
    text = decode(resp)
    assert "만성 B형간염" in text
    rows = extract_rows(text, "https://www.kasl.org/bbs/board.php?bo_table=guide")
    titles = [r.title for r in rows]
    assert "2025 만성 B형간염 진료 가이드라인" in titles
    # 잘린 제목은 title 속성의 전체 제목으로
    assert "대사이상 관련 지방간질환 진료 가이드라인 전문 및 요약본 공개" in titles
    hbv = next(r for r in rows if "B형간염" in r.title)
    assert hbv.date == "2025-06-12"
    assert rows[0].notice
    # 메뉴(header)의 링크는 행으로 잡히지 않아야 함
    assert "공지사항" not in titles


def test_li_board_with_js_links():
    cfg = {"js_link_template": "/data/guideView.do?idx={0}"}
    rows = extract_rows(html("li_board.html"), "https://www.rheum.or.kr/data/guide.do", cfg)
    assert len(rows) == 3
    assert rows[0].url == "https://www.rheum.or.kr/data/guideView.do?idx=312"
    assert rows[0].date == "2025-10-04"
    rows_nocfg = extract_rows(html("li_board.html"), "https://www.rheum.or.kr/data/guide.do")
    assert rows_nocfg[0].url is None  # 템플릿이 없으면 개별 주소 없음 → 게시판 주소로 연결


def test_static_pdf_page():
    rows = extract_rows(html("static_pdf.html"), "https://www.lipid.or.kr/guideline")
    assert len(rows) == 2
    assert any("제5판" in r.title for r in rows)
    assert all(r.url.endswith(".pdf") for r in rows)


def test_discovery():
    c = find_candidates(html("homepage.html"), "https://www.example.or.kr/", "https://www.example.or.kr/")
    urls = [x["url"] for x in c]
    assert urls[0] == "https://www.example.or.kr/bbs/list.php?code=guide"
    assert not any("foreign" in u or "manual" in u or "other-site" in u or "notice" in u for u in urls)


# ── 통합 테스트: 가짜 사이트로 crawl → build ──────────
def route_key(url: str) -> str:
    """페이지 번호는 남기고 빈 파라미터·순서만 정리 (canonical_url은 page를 지우므로 라우팅에 부적합)."""
    from urllib.parse import parse_qsl, urlencode
    p = urlparse(url)
    q = sorted((k, v) for k, v in parse_qsl(p.query) if v and k not in {"keyfield", "key", "gubun"})
    return f"{p.netloc.lower()}{p.path}?{urlencode(q)}"


class FakeFetcher(Fetcher):
    def __init__(self, routes: dict[str, tuple[int, bytes]]):
        super().__init__(delay=0, respect_robots=False, retries=0)
        self.routes = routes

    def _raw_get(self, url, verify, stream=False):
        self._wait(urlparse(url).netloc)
        key = route_key(url)
        r = requests.Response()
        r.url = url
        code, body = self.routes.get(key, (404, b"not found"))
        r.status_code = code
        r._content = body
        r._content_consumed = True
        r.headers["content-type"] = "application/pdf" if url.endswith(".pdf") or "download" in url else "text/html; charset=utf-8"
        return r


@pytest.fixture
def tmp_data(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "DATA", tmp_path)
    monkeypatch.setattr(crawl_mod, "DATA", tmp_path)
    return tmp_path


def test_crawl_end_to_end(tmp_data):
    base = "https://gastrokorea.org/bbs2/index.php"
    routes = {
        route_key(base + "?code=guide2"): (200, fx("ksg_guide2.html")),
        route_key(base + "?code=guide2&page=2&category="): (200, fx("ksg_guide2_p2.html")),
        route_key(base + "?code=guide2&page=2&number=2010&mode=view"): (200, fx("ksg_detail.html")),
        route_key(base + "?code=guide2&page=1&number=2539&mode=view"): (200, fx("login_detail.html")),
        route_key("https://www.example.or.kr/"): (200, fx("homepage.html")),
        route_key("https://www.example.or.kr/bbs/list.php?code=guide"): (200, fx("li_board.html")),
    }
    for n, pg in ((2538, 1), (2535, 1), (2395, 1), (2019, 2)):
        routes[route_key(f"{base}?code=guide2&page={pg}&number={n}&mode=view")] = (200, b"<html><body>view</body></html>")
    socs = [
        {"id": "ksg", "name": "대한소화기학회", "short": "소화기학회", "homepage": "https://gastrokorea.org",
         "group": "내과계", "specialty": "내과", "division": "소화기", "enabled": True, "verify_ssl": True,
         "boards": [{"url": base + "?code=guide2", "name": "진료지침", "dedicated": True}]},
        {"id": "kex", "name": "대한예시학회", "short": "예시학회", "homepage": "https://www.example.or.kr/",
         "group": "내과계", "specialty": "내과", "division": "류마티스", "enabled": True, "verify_ssl": True,
         "boards": []},
    ]
    res = crawl_mod.run(mode="full", max_pages=3, fetcher=FakeFetcher(routes), societies=socs)
    assert res["new"] == 9

    doc = json.loads((tmp_data / "guidelines.json").read_text(encoding="utf-8"))
    items = {it["title"]: it for it in doc["items"]}
    gerd = items["위식도역류질환 진료지침 개정안 2023"]
    assert gerd["date"] == "2023-09-14" and gerd["date_source"] == "detail"
    assert gerd["access"] == "public"
    sexuality = next(v for k, v in items.items() if "Sexuality" in k)
    assert sexuality["access"] == "members"
    uc = next(v for k, v in items.items() if "Ulcerative" in k)
    assert uc["date"] == "2026" and uc["date_precision"] == "year"

    status = json.loads((tmp_data / "status.json").read_text(encoding="utf-8"))
    assert status["societies"]["kex"]["boards"][0]["source"] == "discovered"
    assert status["societies"]["ksg"]["ok"]

    # 재실행: 신규 0건, first_seen 유지
    res2 = crawl_mod.run(mode="incremental", fetcher=FakeFetcher(routes), societies=socs)
    assert res2["new"] == 0


def test_per_host_delay_under_threads():
    import threading, time
    f = Fetcher(delay=0.2, respect_robots=False)
    stamps = {"a": [], "b": []}
    def hit(host):
        for _ in range(3):
            f._wait(host)
            stamps[host].append(time.monotonic())
    ts = [threading.Thread(target=hit, args=(h,)) for h in ("a", "a", "b")]
    t0 = time.monotonic()
    [t.start() for t in ts]; [t.join() for t in ts]
    a = sorted(stamps["a"])
    assert all(y - x >= 0.19 for x, y in zip(a, a[1:]))  # 같은 사이트: 간격 보장
    assert stamps["b"][0] - t0 < 0.1                    # 다른 사이트: 기다리지 않음
