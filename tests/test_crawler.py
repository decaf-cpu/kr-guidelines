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


def test_generic_link_titles_and_merge():
    from gcrawler.parse import extract_rows
    html_ = """<html><body><div class="content"><ul>
    <li><strong>한국 위암 진료 가이드라인 2024</strong> <a href="/file/gastric2024.pdf">전문보기</a> <a href="/file/gastric2024_s.pdf">간략본보기</a></li>
    <li><strong>간외 담관암 진료 가이드라인</strong> <a href="/file/bile.pdf">전문보기</a></li>
    </ul></div></body></html>"""
    rows = extract_rows(html_, "https://www.ksmo.or.kr/Pages/Publication/publication03.aspx")
    titles = {r.title: r for r in rows}
    assert "한국 위암 진료 가이드라인 2024" in titles
    assert len(titles["한국 위암 진료 가이드라인 2024"].attachments) == 2
    assert "간외 담관암 진료 가이드라인" in titles


def test_fix_dates():
    from gcrawler.build import fix_dates
    items = [{"society": "x", "title": f"지침 {i}", "date": "2020-02-01", "date_precision": "day"} for i in range(6)]
    items.append({"society": "x", "title": "주사용 조영제 지침(2016년 제2판)", "date": "2020-02-01", "date_precision": "day"})
    items.append({"society": "y", "title": "2000년 제1판 고혈압 진료지침", "date": "2026-05-08", "date_precision": "day"})
    items.append({"society": "y", "title": "2026년 제6판 고혈압 진료지침", "date": "2026-05-22", "date_precision": "day"})
    out = {it["title"]: it for it in fix_dates(items)}
    assert out["지침 0"]["date_source"] == "bulk"
    assert out["주사용 조영제 지침(2016년 제2판)"]["date"] == "2016"
    assert out["2000년 제1판 고혈압 진료지침"]["date"] == "2000"
    assert out["2026년 제6판 고혈압 진료지침"]["date"] == "2026-05-22"


def test_discovery_img_alt_and_frames():
    from gcrawler.discover import find_candidates, redirects
    h = '<ul class="depth2"><li><a href="/sub0401.php"><img src="m.gif" alt="진료지침"></a></li></ul>'
    assert find_candidates(h, "https://www.a.or.kr/main.php", "https://www.a.or.kr/")[0]["url"] == "https://www.a.or.kr/sub0401.php"
    fs = '<html><frameset><frame src="/main/index.php"></frameset></html>'
    assert redirects(fs, "https://www.a.or.kr/") == ["https://www.a.or.kr/main/index.php"]
    mr = '<html><head><meta http-equiv="refresh" content="0; url=/kor/"></head></html>'
    assert redirects(mr, "https://www.a.or.kr/") == ["https://www.a.or.kr/kor/"]


def test_card_list_month_dates_and_tit():
    html_ = """<div class="contents"><ul class="guideline">
    <li><a href="/bbs/?number=6713&mode=view&code=guide"><span class="thumb"><img src="a.png"></span>
      <span class="tit">2026 대한간학회 만성 B형간염 진료 가이드라인</span><span class="info">2026년 6월 개정</span></a></li>
    <li><a href="/bbs/?number=6193&mode=view&code=guide"><span class="tit">2025 대한간학회 대사이상지방간질환 진료 가이드라인</span><span class="info">2025년 5월 개정</span></a></li>
    <li><a href="/bbs/?number=6192&mode=view&code=guide"><span class="tit">2025 대한간학회 C형간염 진료 가이드라인</span><span class="info">2025년 5월 개정</span></a></li>
    </ul></div>"""
    rows = extract_rows(html_, "https://www.kasl.org/bbs/?code=guide")
    assert len(rows) == 3
    assert rows[0].title == "2026 대한간학회 만성 B형간염 진료 가이드라인"
    assert rows[0].date == "2026-06" and rows[0].date_precision == "month"


def test_year_column_and_foreign_rows():
    html_ = """<table><thead><tr><th>번호</th><th>년도</th><th>카테고리</th><th>제목</th></tr></thead><tbody>
    <tr><td>177</td><td>2026</td><td>ESC</td><td><a href="/bbs/index.php?code=guide&number=100448&mode=view">Fifth Universal Definition of Myocardial Infarction</a></td></tr>
    <tr><td>176</td><td>2025</td><td>KSC</td><td><a href="/bbs/index.php?code=guide&number=100300&mode=view">2025 대한심장학회 심근경색 진료지침</a></td></tr>
    <tr><td>175</td><td>2024</td><td>AHA</td><td><a href="/bbs/index.php?code=guide&number=100200&mode=view">2024 AHA Guideline for X</a></td></tr>
    </tbody></table>"""
    rows = extract_rows(html_, "https://www.circulation.or.kr/bbs/?code=guide")
    assert len(rows) == 3
    assert rows[1].date == "2025" and rows[1].date_precision == "year"
    assert rows[0].foreign and not rows[1].foreign
    assert not is_guideline("2024 AHA Guideline for X")
    assert is_guideline("2025 대한심장학회 심근경색 진료지침")


def test_download_icon_titles():
    html_ = """<div class="guide"><div class="guide-box"><p><span>염증성 장질환에서 thiopurine 사용: 대한장연구학회 합의문 (2015, 한글판)</span></p>
    <div class="btn-wrap"><a href="/assets/file/g07.pdf">Download <span>icon</span></a></div></div>
    <div class="guide-box"><p><span>COVID-19 유행 시기의 성인 염증성 장질환 임상 진료지침 (2021)</span></p>
    <div class="btn-wrap"><a href="/assets/file/g09.pdf">Download <span>icon</span></a></div></div>
    <div class="guide-box"><p><span>궤양성 대장염 진료 가이드라인 (2022)</span></p>
    <div class="btn-wrap"><a href="/assets/file/g10.pdf">Download <span>icon</span></a></div></div></div>"""
    rows = extract_rows(html_, "https://www.kasid.org/academic/education/guide/01")
    titles = [r.title for r in rows]
    assert "염증성 장질환에서 thiopurine 사용: 대한장연구학회 합의문 (2015, 한글판)" in titles
    assert len(rows) == 3


def test_manual_entries(tmp_path, monkeypatch):
    from gcrawler import manual as manual_mod
    (tmp_path / "manual.yaml").write_text("""
- society: kcr
  title: 2025 한국 류마티스관절염 진료지침
  url: https://example.org/ra.pdf
  date: 2025-10
  added: 2026-11-15
  files: [https://example.org/ra.pdf]
- society: nope
  title: x
  url: https://example.org/x
- society: kcr
  title: 날짜 오류
  url: https://example.org/y
  date: 25년
""", encoding="utf-8")
    monkeypatch.setattr(manual_mod, "CONFIG", tmp_path)
    socs = {"kcr": {"id": "kcr", "enabled": False, "manual": True, "group": "내과계", "division": "류마티스", "short": "류마티스학회"}}
    items, errors = manual_mod.load_manual(socs)
    assert len(items) == 2 and len(errors) == 2
    assert items[0]["date_precision"] == "month" and items[0]["attachments"][0]["kind"] == "pdf"
    from gcrawler.build import merge_items
    merged = merge_items(items, socs)
    assert any(g["title"] == "2025 한국 류마티스관절염 진료지침" and g["sources"][0]["manual"] for g in merged)


def test_pdf_creation_date():
    from gcrawler.filedate import _from_pdf_bytes
    b = b"%PDF-1.6 ... /CreationDate (D:20200211093000+09'00') /ModDate (D:20210101) ..."
    assert _from_pdf_bytes(b) == ("2020-02-11", "day")
    assert _from_pdf_bytes(b"<xmp:CreateDate>2019-05-03T10:00:00</xmp:CreateDate>") == ("2019-05-03", "day")
    assert _from_pdf_bytes(b"no dates here") is None


def test_backfill_then_new(tmp_data):
    base = "https://gastrokorea.org/bbs2/index.php"
    routes = {route_key(base + "?code=guide2"): (200, fx("ksg_guide2_p2.html"))}
    socs = [{"id": "ksg", "name": "대한소화기학회", "short": "소화기학회", "homepage": "https://gastrokorea.org",
             "group": "내과계", "specialty": "내과", "division": "소화기", "enabled": True, "verify_ssl": True,
             "boards": [{"url": base + "?code=guide2", "name": "진료지침", "dedicated": True}]}]
    crawl_mod.run(mode="incremental", max_pages=1, fetcher=FakeFetcher(routes), societies=socs, detail_per_society=0)
    doc = json.loads((tmp_data / "guidelines.json").read_text(encoding="utf-8"))
    assert all(it["backfill"] for it in doc["items"])  # 처음 수집한 게시판의 기존 글은 NEW 아님
    routes[route_key(base + "?code=guide2")] = (200, fx("ksg_guide2.html"))
    crawl_mod.run(mode="incremental", max_pages=1, fetcher=FakeFetcher(routes), societies=socs, detail_per_society=0)
    doc = json.loads((tmp_data / "guidelines.json").read_text(encoding="utf-8"))
    fresh = [it for it in doc["items"] if not it["backfill"]]
    assert len(fresh) == 4  # 이후 새로 올라온 글만 NEW 대상


def test_filedate_without_range_support():
    from gcrawler.filedate import file_date
    pdf = b"%PDF-1.4\n" + b"x" * 700000 + b"\n/Info << /CreationDate (D:20190305120000) >>\n%%EOF"

    class F(Fetcher):
        def __init__(self):
            super().__init__(delay=0, respect_robots=False, retries=0)

        def _raw_get(self, url, verify, stream=False, headers=None):
            r = requests.Response()
            r.url, r.status_code, r._content, r._content_consumed = url, 200, pdf, True
            r.headers["content-type"] = "application/octet-stream"
            return r

    assert file_date(F(), "https://x.or.kr/download.php?num=1") == ("2019-03-05", "day", "pdf")


def test_admin_titles_excluded():
    assert not is_guideline("개발 규정 부록 다운로드", dedicated=True)


def test_title_links_to_board_when_url_is_file():
    from gcrawler.build import merge_items
    socs = {"ksmo": {"id": "ksmo", "enabled": True, "group": "내과계", "division": "종양", "short": "종양내과학회"}}
    items = [{"id": "a", "society": "ksmo", "title": "한국 췌장암 진료 가이드라인", "url": "https://www.ksmo.or.kr/file/book1-07.pdf",
              "board": "https://www.ksmo.or.kr/Pages/Publication/publication03.aspx", "attachments": [], "access": "public"},
             {"id": "b", "society": "ksmo", "title": "2024 위암 진료 가이드라인", "url": "https://www.ksmo.or.kr/bbs/view.php?no=3",
              "board": "https://www.ksmo.or.kr/bbs/list.php", "attachments": [], "access": "public"}]
    g = {x["title"]: x for x in merge_items(items, socs)}
    s = g["한국 췌장암 진료 가이드라인"]["sources"][0]
    assert s["url"].endswith("publication03.aspx") and s["attachments"][0]["url"].endswith("book1-07.pdf")
    assert g["2024 위암 진료 가이드라인"]["sources"][0]["url"].endswith("view.php?no=3")


def test_foreign_society_names():
    assert not is_guideline("The North American Neuroendocrine Tumor Society Consensus Guidelines for Surveillance", dedicated=True)
    assert is_guideline("한국 췌장암 진료 가이드라인 (2021)", dedicated=True)


def test_pdf_date_prefers_document_info_over_embedded():
    from gcrawler.filedate import _from_pdf_bytes
    b = (b"%PDF <xmp:CreateDate>2019-03-27T10:00:00</xmp:CreateDate> /CreationDate (D:20190327) "
         b"... trailer /Info << /CreationDate (D:20230612100000) >>")
    assert _from_pdf_bytes(b) == ("2023-06-12", "day")


def test_repeated_file_dates_are_dropped():
    from gcrawler.build import fix_dates
    items = [{"society": "ksid", "title": f"지침 {i}", "date": "2019-03-27", "date_precision": "day", "date_source": "file-pdf"} for i in range(2)]
    items.append({"society": "ksid", "title": "다른 지침", "date": "2021-05-12", "date_precision": "day", "date_source": "file-pdf"})
    out = {it["title"]: it for it in fix_dates(items)}
    assert out["지침 0"]["date"] is None and out["다른 지침"]["date"] == "2021-05-12"


def test_firewall_block_stops_host():
    class F(Fetcher):
        def __init__(self):
            super().__init__(delay=0, respect_robots=False, retries=0)
            self.calls = 0

        def _raw_get(self, url, verify, stream=False, headers=None):
            self.calls += 1
            r = requests.Response()
            r.url, r.status_code, r._content_consumed = url, 200, True
            r._content = b"<br><center><h2>The request / response that are contrary to Web firewall security policies has been blocked.</h2></center>"
            r.headers["content-type"] = "text/html"
            r.headers["content-length"] = str(len(r._content))
            return r

    f = F()
    assert f.get("https://www.ksid.or.kr/a") is None
    assert f.get("https://www.ksid.or.kr/b") is None
    assert f.calls == 1  # 차단 뒤에는 더 요청하지 않음


def test_shared_file_date_across_different_guidelines_is_dropped():
    from gcrawler.build import fix_dates
    base = {"society": "s", "date": "2020-02-01", "date_precision": "day", "date_source": "detail"}
    items = [dict(base, id=str(i), title=f"지침 {chr(65 + i)}", file_date=["2025-12-01", "day", "file-pdf"]) for i in range(6)]
    items[0]["text_year"] = "2018"
    # 같은 지침의 요약본·전체본은 같은 날 만들 수 있다
    items += [
        dict(base, id="p1", title="만성기침 진료지침", file_date=["2018-07-09", "day", "file-pdf"]),
        dict(base, id="p2", title="만성기침 진료지침 요약본", file_date=["2018-07-09", "day", "file-pdf"]),
    ]
    out = {it["id"]: it for it in fix_dates(items)}
    assert out["0"]["date"] == "2018" and out["0"]["date_source"] == "file-text"
    assert out["1"]["date_source"] == "bulk"
    assert out["p1"]["date"] == "2018-07-09" and out["p2"]["date"] == "2018-07-09"


def test_year_from_pdf_text():
    from gcrawler.filedate import year_from_pdf_text
    assert year_from_pdf_text("Allergy Asthma Respir Dis 2018;6:1-10 Copyright © 2017") == "2018"
    assert year_from_pdf_text("Allergy Asthma Immunol Res. 2020 Nov;12(6):1000") == "2020"
    assert year_from_pdf_text("Copyright ⓒ 2015 대한천식알레르기학회") == "2015"
    assert year_from_pdf_text("no year here") is None


def test_override_sets_lookup_date():
    from gcrawler.build import apply_overrides
    items = [{"society": "s", "title": "Guidelines for X in Korea", "date": None}]
    out = apply_overrides(items, [{"society": "s", "title": "Guidelines for X", "date": "2023", "source": "PMID 1"}])
    assert out[0]["date"] == "2023" and out[0]["date_precision"] == "year" and out[0]["date_source"] == "lookup"


def test_shared_attachment_is_site_chrome():
    from gcrawler.filedate import drop_shared_links
    banner = {"name": "벤토린 안내.pdf", "url": "https://x/banner.pdf"}
    items = [{"society": "s", "title": f"지침 {i}", "attachments": [dict(banner)], "file_date": ["2025-12-01", "day", "file-pdf"],
              "file_checked_v": 4} for i in range(3)]
    assert drop_shared_links(items) == 3
    assert all(not it["attachments"] and "file_date" not in it and "file_checked_v" not in it for it in items)


def test_detail_year_label_and_doi():
    from gcrawler.parse import parse_detail
    html = """<div class="write-wrap"><ul><li><div class="form-tit">연도</div><div class="form-con"> 2017 </div></li>
    <li><div class="form-tit">링크</div><div class="form-con"><a href="https://doi.org/10.4168/aard.2017.5.4.185">doi</a></div></li></ul></div>"""
    r = parse_detail(html, "https://x/view/1")
    assert r["pub_year"] == "2017" and r["doi"] == "https://doi.org/10.4168/aard.2017.5.4.185"


def test_pub_year_replaces_bulk_date():
    from gcrawler.build import fix_dates
    items = [{"id": str(i), "society": "s", "title": f"지침 {chr(65 + i)}", "date": "2020-02-01", "date_precision": "day",
              "date_source": "detail"} for i in range(6)]
    items[0]["pub_year"] = "2017"
    out = {it["id"]: it for it in fix_dates(items)}
    assert out["0"]["date"] == "2017" and out["0"]["date_source"] == "detail-year" and not out["0"].get("posted")


def test_generic_download_link_uses_alt_title():
    from gcrawler.parse import extract_rows
    html = "<ul>" + "".join(
        f'<li><a alt="{t}" href="/func/download.php?path=x{i}&filename=y{i}"><img src="t.jpg"><span>다운로드</span></a></li>'
        for i, t in enumerate(["소아천식 진료 가이드라인", "2007 부비동염 진료 가이드라인"])) + "</ul>"
    rows = extract_rows(html, "https://x.or.kr/community/guide.php", {"dedicated": True})
    assert [r.title for r in rows] == ["소아천식 진료 가이드라인", "2007 부비동염 진료 가이드라인"]


def test_trailing_view_more_removed():
    from gcrawler.parse import Row, strip_trailing_link_words
    rows = strip_trailing_link_words([Row(title="Fact sheet 임신 중 타이레놀과 자폐증 논란 VIEW MORE", url="u")])
    assert rows[0].title == "Fact sheet 임신 중 타이레놀과 자폐증 논란"


def test_drop_params_keeps_item_id():
    from gcrawler.crawl import drop_query_params
    from gcrawler.parse import canonical_url

    old = "https://www.circulation.or.kr/bbs/index.php?code=guide&category=&gubun=&page=3&number=8309&mode=view"
    new = "https://www.circulation.or.kr/bbs/index.php?code=guide&category=X&gubun=&page=1&number=8309&mode=view&keyfield=&key="
    assert canonical_url(drop_query_params(new, ["category"])) == canonical_url(old)


def test_label_year_from_category_label():
    from bs4 import BeautifulSoup
    from gcrawler.parse import parse_row

    html = """<li><a href="#n"><div class="img"><img alt="x"></div><div class="txt">
      <span class="cate">COVID-19 진료지침 2024</span>
      <strong class="tit">만성 코로나19 증후군(Long COVID) 임상진료지침 권고안</strong>
      <p>대한감염학회</p></div></a>
      <a href="/rang_board/inc/download.php?nocount=1&code=ncov_guide_do&num=6749">다운로드</a></li>"""
    el = BeautifulSoup(html, "lxml").li
    base = "https://www.ksid.or.kr/rang_board/list.html?code=ncov_guide_do"
    row = parse_row(el, base, {"label_year": True})
    assert row is not None and row.label_year == "2024"
    assert parse_row(el, base, {}).label_year is None  # 설정한 게시판에서만
