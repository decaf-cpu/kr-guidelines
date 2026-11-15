"""범용 게시판 목록 파서.

학회마다 게시판 엔진(그누보드, XE/Rhymix, 자체 CMS 등)이 달라도
'행(row) = 제목 링크 1개 + 날짜/첨부' 라는 공통 구조를 이용해 게시물을 뽑는다.
특정 사이트가 안 읽히면 societies.yaml의 board 설정(row_selector 등)이나
gcrawler/adapters/<id>.py 로 덮어쓴다.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup, Tag

# ── 날짜 ─────────────────────────────────────────
_FULL_DATE = re.compile(r"(?<!\d)((?:19|20)\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})(?!\d)")
_SHORT_DATE = re.compile(r"(?<![\d.])(\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})(?![\d.])")
_MONTH = re.compile(r"(?<!\d)((?:19|20)\d{2})\s*[.\-/년]\s*(\d{1,2})\s*월?(?![\d.\-/])")
_YEAR = re.compile(r"(?<!\d)((?:19[89]|20[0-4])\d)(?!\d)")
_LABELED = re.compile(
    r"(작성일|등록일|게시일|작성\s*일자|등록\s*일자|게재일|date|posted)\s*[:：]?\s*"
    r"((?:19|20)\d{2}\s*[.\-/년]\s*\d{1,2}\s*[.\-/월]\s*\d{1,2})",
    re.I,
)


def _valid(y: int, m: int, d: int) -> dt.date | None:
    try:
        x = dt.date(y, m, d)
    except ValueError:
        return None
    if x > dt.date.today() + dt.timedelta(days=1) or x.year < 1980:
        return None
    return x


def find_date(text: str) -> tuple[str, str] | None:
    """텍스트에서 가장 그럴듯한 날짜. (ISO 문자열, 정밀도) 반환."""
    if not text:
        return None
    for m in _FULL_DATE.finditer(text):
        d = _valid(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            return d.isoformat(), "day"
    for m in _SHORT_DATE.finditer(text):
        d = _valid(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            return d.isoformat(), "day"
    return None


def find_month(text: str) -> tuple[str, str] | None:
    for m in _MONTH.finditer(text or ""):
        y, mo = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and _valid(y, mo, 1):
            return f"{y:04d}-{mo:02d}", "month"
    return None


def find_labeled_date(text: str) -> tuple[str, str] | None:
    m = _LABELED.search(text or "")
    return find_date(m.group(2)) if m else None


_COMPACT = re.compile(r"(?<!\d)((?:19|20)\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)")


def year_from_title(title: str) -> tuple[str, str] | None:
    m = _COMPACT.search(title or "")  # '권고안_20200211' 같은 날짜
    if m and _valid(int(m.group(1)), int(m.group(2)), int(m.group(3))):
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}", "day"
    this_year = dt.date.today().year
    years = [int(y) for y in _YEAR.findall(title or "") if int(y) <= this_year]
    if not years:
        return None
    return f"{max(years)}", "year"


# ── URL ──────────────────────────────────────────
_VOLATILE_PARAMS = {
    "page", "pg", "cpage", "pageno", "pageindex", "currentpage", "page_no", "pagenum",
    "nowpage", "gotopage", "curpage", "p", "keyfield", "key", "keyword", "searchword",
    "search_keyword", "searchtype", "sfl", "stx", "sst", "sod", "spt", "category_srl_search",
    "listorder", "sort", "order", "orderby", "s_word", "s_type",
}
PAGE_PARAM_CANDIDATES = [
    "page", "pg", "cpage", "pageNo", "pageIndex", "currentPage", "page_no", "pageNum",
    "nowPage", "gotoPage", "curPage", "p", "pagenum", "pageno",
]


def canonical_url(url: str) -> str:
    """페이지 번호·검색어 등 매번 바뀌는 파라미터를 지워 같은 글을 같은 주소로."""
    p = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if v != "" and k.lower() not in _VOLATILE_PARAMS]
    q.sort()
    netloc = p.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    scheme = "https" if p.scheme.lower() in ("http", "https") else p.scheme.lower()
    return urlunparse((scheme, netloc, p.path or "/", "", urlencode(q, doseq=True), ""))


def set_query_param(url: str, key: str, value: str) -> str:
    p = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k != key]
    q.append((key, value))
    return urlunparse(p._replace(query=urlencode(q, doseq=True)))


def same_site(a: str, b: str) -> bool:
    def base(host: str) -> str:
        host = host.lower().split(":")[0]
        if host.startswith("www."):
            host = host[4:]
        parts = host.split(".")
        if len(parts) >= 3 and parts[-1] == "kr" and parts[-2] in {"or", "co", "ac", "go", "re", "ne", "pe"}:
            return ".".join(parts[-3:])
        return ".".join(parts[-2:])

    return base(urlparse(a).netloc) == base(urlparse(b).netloc)


# ── 첨부 ─────────────────────────────────────────
_ATTACH_EXT = re.compile(r"\.(pdf|hwp|hwpx|docx?|pptx?|xlsx?|zip)(?:$|[?#&])", re.I)
_ATTACH_HINT = re.compile(r"(download|filedown|file_down|fileDown|down\.php|download\.php|getfile|attach)", re.I)


def is_attachment(a: Tag, href: str) -> bool:
    if _ATTACH_EXT.search(href) or _ATTACH_HINT.search(href):
        return True
    img = a.find("img")
    if img is not None and re.search(r"(pdf|hwp|file|down|첨부)", (img.get("alt", "") + img.get("src", "")), re.I):
        return True
    return False


def attachment_kind(name: str, href: str) -> str:
    m = _ATTACH_EXT.search(name or "") or _ATTACH_EXT.search(href or "")
    if m:
        return m.group(1).lower()
    return "file"


# ── 행 파싱 ──────────────────────────────────────
_NAV_CLASS = re.compile(
    r"(gnb|lnb|snb|tnb|menu|nav|footer|header|util|quick|breadcrumb|location|paging|pagination|"
    r"sitemap|top_?menu|side|banner|family|popup|skip)",
    re.I,
)
_NAV_WORDS = {
    "이전", "다음", "목록", "처음", "마지막", "더보기", "more", "prev", "next", "first", "last",
    "home", "로그인", "회원가입", "검색", "글쓰기", "답변", "수정", "삭제",
}
_WS = re.compile(r"\s+")


def clean(text: str | None) -> str:
    return _WS.sub(" ", text or "").strip()


def _in_nav(el: Tag) -> bool:
    for parent in [el, *el.parents]:
        if not isinstance(parent, Tag):
            continue
        if parent.name in {"nav", "header", "footer"}:
            return True
        ident = " ".join(parent.get("class", [])) + " " + (parent.get("id") or "")
        if ident.strip() and _NAV_CLASS.search(ident):
            return True
    return False


_JS_ARG = re.compile(r"""\(\s*['"]?([\w\-]+)['"]?""")


def _resolve_href(a: Tag, base_url: str, js_template: str | None) -> str | None:
    href = (a.get("href") or "").strip()
    onclick = a.get("onclick") or ""
    if href and not href.lower().startswith(("javascript", "#")):
        return urljoin(base_url, href)
    if js_template:
        m = _JS_ARG.search(href if href.lower().startswith("javascript") else onclick)
        if m:
            return urljoin(base_url, js_template.format(m.group(1)))
    return None


@dataclass
class Row:
    title: str
    url: str | None
    date: str | None = None
    date_precision: str | None = None
    attachments: list[dict] = field(default_factory=list)
    notice: bool = False  # 상단 고정 공지
    foreign: bool = False  # '카테고리' 칸 등에 외국 학회명(ESC·AHA 등)이 적힌 행


def _attachments_in(el: Tag, base_url: str) -> list[dict]:
    out, seen = [], set()
    for a in el.find_all("a"):
        href = (a.get("href") or "").strip()
        if not href or href.lower().startswith(("javascript", "#")):
            continue
        if not is_attachment(a, href):
            continue
        u = urljoin(base_url, href)
        if u in seen:
            continue
        seen.add(u)
        name = clean(a.get_text(" ")) or clean(a.get("title")) or u.rsplit("/", 1)[-1]
        out.append({"name": name[:200], "url": u, "kind": attachment_kind(name, u)})
    return out


def parse_row(el: Tag, base_url: str, cfg: dict | None = None) -> Row | None:
    cfg = cfg or {}
    js_template = cfg.get("js_link_template")
    if cfg.get("title_selector"):
        anchors = el.select(cfg["title_selector"])
    else:
        anchors = el.find_all("a")
    best = None
    for a in anchors:
        href = (a.get("href") or "").strip()
        if href and not href.lower().startswith(("javascript", "#")) and is_attachment(a, href):
            continue
        if GENERIC_LINK.match(clean(a.get_text(" ")) or "x"):
            continue
        t = clean(a.get_text(" ")) or clean(a.get("title"))
        if len(t) < 4 or t.lower() in _NAV_WORDS:
            continue
        if best is None or len(t) > len(best[1]):
            best = (a, t)
    if best is None:
        return None
    a, title = best
    if re.search(r"(회원만|회원\s*전용|로그인\s*후)", (a.get("onclick") or "") + " " + (a.get("href") or "")):
        return None
    if re.search(r"[?&](sort|order|orderby)=", a.get("href") or "", re.I) and len(title) < 15:
        return None  # 표 머리글의 정렬 링크
    tit = a.find(class_=re.compile(r"^(tit|title|subject|subj|txt_tit|tit_txt|name)$", re.I))
    if tit is not None and len(clean(tit.get_text(" "))) >= 4:
        title = clean(tit.get_text(" "))
    if title.lower().startswith(("http://", "https://", "www.")):
        return None
    # 제목이 잘린 경우(… 처리) title 속성의 전체 제목 사용
    full = clean(a.get("title"))
    if full and len(full) > len(title) and full.rstrip(".…").startswith(title.rstrip(".…")[:10]):
        title = full
    url = _resolve_href(a, base_url, js_template)

    if cfg.get("date_selector"):
        d_el = el.select_one(cfg["date_selector"])
        date = find_date(d_el.get_text(" ")) if d_el else None
    else:
        rest = clean(el.get_text(" ")).replace(title, " ")
        date = find_date(rest) or find_month(rest)
        if date is None:
            # '년도' 칸처럼 연도만 적힌 셀
            for cell in el.find_all(["td", "span", "em", "dd", "div"], recursive=True):
                t = clean(cell.get_text(" "))
                if re.fullmatch(r"(19[89]\d|20[0-4]\d)", t) and int(t) <= dt.date.today().year:
                    date = (t, "year")
                    break

    row_text = clean(el.get_text(" "))
    from .filters import FOREIGN
    foreign = any(
        FOREIGN.fullmatch(clean(c.get_text(" "))) for c in el.find_all(["td", "span", "em", "dd"]) if c.find("a") is None
    )
    notice = bool(re.match(r"^(공지|notice|\[공지\])", row_text, re.I)) or "notice" in " ".join(el.get("class", [])).lower()
    return Row(
        title=title[:300],
        url=url,
        date=date[0] if date else None,
        date_precision=date[1] if date else None,
        attachments=_attachments_in(el, base_url),
        notice=notice,
        foreign=foreign,
    )


def _dedupe(rows: list[Row]) -> list[Row]:
    out, seen = [], set()
    for r in rows:
        key = (r.title, r.url)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


POST_URL = re.compile(r"(mode=view|wr_id=|[?&]num(ber)?=\d|/view[/.?]|view\.(php|html|asp|do)|[?&]idx=\d|document_srl=|[?&]seq=\d|[?&]no=\d|[?&]bbsidx=|[?&]nttId=)", re.I)


def _sibling_groups(soup: BeautifulSoup, min_size: int = 3) -> list[list[Tag]]:
    groups = []
    for parent in soup.find_all(True):
        if parent.name in {"html", "head", "select", "script", "style"} or _in_nav(parent):
            continue
        kids = [c for c in parent.find_all(recursive=False) if isinstance(c, Tag)]
        if len(kids) < min_size:
            continue
        by_tag: dict[str, list[Tag]] = {}
        for c in kids:
            if c.find("a") is not None or c.name == "a":
                by_tag.setdefault(c.name, []).append(c)
        for tag, els in by_tag.items():
            if tag != "a" and len(els) >= min_size:
                groups.append(els)
    return groups[:200]


def extract_rows(html: str, base_url: str, cfg: dict | None = None) -> list[Row]:
    cfg = cfg or {}
    soup = BeautifulSoup(html, "lxml")
    for bad in soup(["script", "style", "noscript"]):
        bad.decompose()

    if cfg.get("row_selector"):
        rows = [parse_row(el, base_url, cfg) for el in soup.select(cfg["row_selector"])]
        return _dedupe([r for r in rows if r])

    candidate_sets = {
        "tr": [el for el in soup.find_all("tr") if not _in_nav(el)],
        "dl": [el for el in soup.find_all("dl") if not _in_nav(el) and not el.find("dl")],
        "li": [el for el in soup.find_all("li") if not _in_nav(el) and not el.find("li")],
        "div": [
            el
            for el in soup.find_all(["div", "dl", "article"], class_=re.compile(r"(list|item|row|bbs|board|post|subject)", re.I))
            if not _in_nav(el)
        ],
    }
    # 같은 부모 아래 같은 태그가 3번 이상 반복되고 각자 링크를 가진 묶음 = 게시판 행일 가능성
    for i, group in enumerate(_sibling_groups(soup)):
        candidate_sets[f"sib{i}"] = group
    best_rows: list[Row] = []
    best_score = 0.0
    for els in candidate_sets.values():
        rows = _dedupe([r for r in (parse_row(el, base_url, cfg) for el in els) if r])
        if not rows:
            continue
        evidence = sum(1 for r in rows if r.date or r.attachments or (r.url and POST_URL.search(r.url)))
        score = evidence + 0.05 * len(rows)
        # 근거(날짜·첨부)가 하나도 없는 집합은 메뉴일 가능성이 높아 감점
        if evidence == 0:
            score = 0.01 * len(rows)
        if score > best_score:
            best_score, best_rows = score, rows
    if best_rows and best_score >= 2:
        return best_rows
    return extract_links_fallback(soup, base_url)


GENERIC_LINK = re.compile(
    r"^\s*(\[?\s*(전문|간략본|요약본|요약|국문|영문|한글|원문|본문|첨부|첨부\s*파일|파일|pdf|hwp|ppt|자료|자세히|상세|더|원문\s*링크|doi)?\s*"
    r"(보기|다운로드|다운|download|열기|바로\s*가기|view|link|click|icon|아이콘)?\s*\]?\s*)+$",
    re.I,
)


def _context_title(a: Tag) -> str:
    """'전문보기' 같은 링크면 가까운 블록의 제목다운 글자를 찾는다."""
    own = clean(a.get_text(" "))
    for parent in a.parents:
        if not isinstance(parent, Tag) or parent.name in {"body", "html"}:
            break
        # 같은 블록 안의 제목 요소 우선
        head = parent.find(["h1", "h2", "h3", "h4", "h5", "h6", "strong", "dt", "th", "b"])
        if head is not None and len(clean(head.get_text(" "))) >= 4 and not GENERIC_LINK.match(clean(head.get_text(" "))):
            return clean(head.get_text(" "))
        text = clean(parent.get_text(" "))
        # 블록 안 다른 링크 문구(전문보기·간략본보기 등)를 지운 나머지
        for x in parent.find_all("a"):
            t = clean(x.get_text(" "))
            if t and GENERIC_LINK.match(t):
                text = text.replace(t, " ")
        text = clean(text)
        if len(text) >= 6:
            return text[:200] if len(text) <= 200 else ""
        if len(text) > 200:
            break
    # 앞쪽 형제 요소의 글자
    prev = a.find_previous(string=lambda x: x and len(x.strip()) >= 6)
    return clean(prev)[:200] if prev else own


def extract_links_fallback(soup: BeautifulSoup, base_url: str) -> list[Row]:
    """게시판이 아닌 정적 페이지(지침 PDF 링크 나열)용: 본문 첨부 링크를 항목으로.
    같은 제목에 딸린 여러 파일(전문·요약본 등)은 한 항목의 첨부로 묶는다."""
    by_title: dict[str, Row] = {}
    for a in soup.find_all("a"):
        href = (a.get("href") or "").strip()
        if not href or href.lower().startswith(("javascript", "#")) or _in_nav(a):
            continue
        if not is_attachment(a, href):
            continue
        label = clean(a.get_text(" ")) or clean(a.get("title"))
        img = a.find("img")
        if not label and img is not None:
            label = clean(img.get("alt"))
        title = label
        if len(title) < 4 or GENERIC_LINK.match(title):
            # 링크 글자가 '다운로드'뿐이면 링크·그림에 붙은 alt/title 속성의 이름을 먼저 쓴다
            attr = [clean(a.get("alt")), clean(a.get("title"))] + [clean(i.get("alt")) for i in a.find_all("img")]
            attr = [t for t in attr if len(t) >= 4 and not GENERIC_LINK.match(t)]
            title = attr[0] if attr else _context_title(a)
        if len(title) < 4:
            continue
        u = urljoin(base_url, href)
        att = {"name": label or title[:200], "url": u, "kind": attachment_kind(label + " " + title, u)}
        row = by_title.get(title)
        if row is None:
            date = find_date(title)
            by_title[title] = Row(
                title=title[:300], url=u,
                date=date[0] if date else None, date_precision=date[1] if date else None,
                attachments=[att],
            )
        elif all(x["url"] != u for x in row.attachments):
            row.attachments.append(att)
    return list(by_title.values())


# ── 페이지 이동 ──────────────────────────────────
OFFSET_PARAMS = re.compile(r"^(offset|start|startrow|start_row|from|first|pos|row|startnum|start_num|limit_start)$", re.I)


def detect_page_param(html: str, base_url: str, forced: str | None = None) -> tuple | None:
    """'2' 링크를 찾아 (페이지 파라미터 이름, 2쪽 URL, 간격)을 돌려준다.
    간격이 None이면 페이지 번호 방식, 숫자면 offset 방식(2쪽 = offset 간격)."""
    soup = BeautifulSoup(html, "lxml")
    for a in soup.find_all("a", href=True):
        if clean(a.get_text()) != "2":
            continue
        href = a["href"].strip()
        if href.lower().startswith(("javascript", "#")):
            continue
        u = urljoin(base_url, href)
        q = dict(parse_qsl(urlparse(u).query, keep_blank_values=True))
        if forced and q.get(forced) == "2":
            return forced, u, None
        for name in PAGE_PARAM_CANDIDATES:
            for k, v in q.items():
                if k.lower() == name.lower() and v == "2":
                    return k, u, None
        for k, v in q.items():
            if OFFSET_PARAMS.match(k) and v.isdigit() and int(v) > 1:
                return k, u, int(v)
    if forced:
        return forced, set_query_param(base_url, forced, "2"), None
    return None


def page_urls(first_url: str, page2: tuple | None, max_pages: int) -> list[str]:
    urls = [first_url]
    if not page2 or max_pages <= 1:
        return urls
    param, template = page2[0], page2[1]
    step = page2[2] if len(page2) > 2 else None
    for n in range(2, max_pages + 1):
        urls.append(set_query_param(template, param, str(step * (n - 1) if step else n)))
    return urls


# ── 상세 페이지 ──────────────────────────────────
_YEAR_LABEL = re.compile(r"^(?:발행|발간|게재|출판|개발|제정|개정)?\s*(?:연도|년도)$|^(?:publication\s*)?year$", re.I)
_DOI = re.compile(r"(?:https?://(?:dx\.)?doi\.org/|\bdoi:\s*)(10\.\d{4,9}/[^\s\"'<>]+)", re.I)


def labeled_year(soup) -> str | None:
    """'연도 | 2017'처럼 항목명 옆 칸에 적힌 발행연도 (dt/dd, th/td, 나란한 div)."""
    this_year = dt.date.today().year
    for el in soup.find_all(["dt", "th", "div", "span", "label", "strong", "p"]):
        if len(el.get_text(strip=True)) > 12 or not _YEAR_LABEL.match(clean(el.get_text(" "))):
            continue
        sib = el.find_next_sibling()
        if sib is None:
            continue
        m = re.fullmatch(r"\s*((?:19|20)\d{2})\s*년?\s*", sib.get_text(" "))
        if m and 1990 <= int(m.group(1)) <= this_year:
            return m.group(1)
    return None


def parse_detail(html: str, base_url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    for bad in soup(["script", "style", "noscript"]):
        bad.decompose()
    text = clean(soup.get_text(" "))
    date = find_labeled_date(text)
    pub_year = labeled_year(soup)
    doi = None
    for a in soup.find_all("a", href=True):
        m = _DOI.search(a["href"])
        if m and not _in_nav(a):
            doi = "https://doi.org/" + m.group(1).rstrip(".,;)")
            break
    attachments = []
    seen = set()
    for a in soup.find_all("a"):
        href = (a.get("href") or "").strip()
        if not href or href.lower().startswith(("javascript", "#")) or _in_nav(a):
            continue
        if not is_attachment(a, href):
            continue
        u = urljoin(base_url, href)
        if u in seen:
            continue
        seen.add(u)
        name = clean(a.get_text(" ")) or clean(a.get("title")) or u.rsplit("/", 1)[-1]
        attachments.append({"name": name[:200], "url": u, "kind": attachment_kind(name, u)})
    return {"date": date, "attachments": attachments[:20], "pub_year": pub_year, "doi": doi}
