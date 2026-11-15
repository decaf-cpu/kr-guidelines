"""목록·상세 페이지에 날짜가 없는 지침의 날짜를 파일에서 추정.

순서: PDF 안의 작성일(CreationDate/xmp:CreateDate) → 파일 이름의 연도 → 서버의 파일 수정일(Last-Modified).
파일 전체를 받지 않고 앞뒤 256KB만 읽는다.
"""
from __future__ import annotations

import datetime as dt
import re
from email.utils import parsedate_to_datetime
from urllib.parse import unquote

from .fetch import Fetcher
from .parse import year_from_title

_PDF_DATE = [
    re.compile(rb"/CreationDate\s*\(\s*D:\s*((?:19|20)\d{2})(\d{2})?(\d{2})?"),
    re.compile(rb"<xmp:CreateDate>\s*((?:19|20)\d{2})-(\d{2})-(\d{2})"),
    re.compile(rb"xmp:CreateDate=\"((?:19|20)\d{2})-(\d{2})-(\d{2})"),
]
CHUNK = 262144
FULL_LIMIT = 8 * 1024 * 1024  # Range를 무시하는 서버: 작성일을 찾을 때까지 최대 8MB


def _ok(y: int, m: int = 1, d: int = 1) -> bool:
    try:
        x = dt.date(y, m, d)
    except ValueError:
        return False
    return dt.date(1990, 1, 1) <= x <= dt.date.today()


def _from_pdf_bytes(b: bytes) -> tuple[str, str] | None:
    """문서 자체의 작성일. 한 파일 안에 날짜가 여럿이면(삽입된 그림·양식의 날짜 등)
    Info 사전의 /CreationDate 중 파일 뒤쪽(최종 저장 시 기록)의 것을, 없으면 메타데이터 날짜 중 가장 늦은 것."""
    def conv(m):
        y = int(m.group(1))
        mo = int(m.group(2)) if m.group(2) else None
        d = int(m.group(3)) if m.group(3) else None
        if mo and d and _ok(y, mo, d):
            return f"{y:04d}-{mo:02d}-{d:02d}", "day"
        if mo and _ok(y, mo):
            return f"{y:04d}-{mo:02d}", "month"
        if _ok(y):
            return f"{y:04d}", "year"
        return None

    info = [c for c in (conv(m) for m in _PDF_DATE[0].finditer(b)) if c]
    if info:
        return info[-1]
    xmp = [c for pat in _PDF_DATE[1:] for c in (conv(m) for m in pat.finditer(b)) if c]
    return max(xmp) if xmp else None


_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
# 학술지 인용 표기: "Allergy Asthma Respir Dis 2018;6:1-10", "2020 Nov;12(6):", "2019 Mar 5;34(9)"
_CITE = re.compile(rf"\b((?:19|20)\d{{2}})(?:\s+(?:{_MONTHS})[a-z]*\.?(?:\s+\d{{1,2}})?)?\s*;\s*\d{{1,3}}\s*[(:]", re.I)
# "Allergy Asthma Respir Dis 6(1):1-10, January 2018" / "Vol. 6, No. 1, January 2018"
_VOL = re.compile(rf"\b\d{{1,3}}\s*\(\s*\d{{1,2}}\s*\)\s*:\s*\d+\s*[-–]\s*\d+\s*,\s*(?:(?:{_MONTHS})[a-z]*\.?\s+)?((?:19|20)\d{{2}})", re.I)
_COPY = re.compile(r"(?:copyright|©|ⓒ)\s*(?:©\s*)?((?:19|20)\d{2})", re.I)
_ACCEPT = re.compile(rf"(?:accepted|게재\s*승인일?|채택일?)\s*[:：]?\s*(?:(?:{_MONTHS})[a-z]*\.?\s+\d{{1,2}},?\s+((?:19|20)\d{{2}})|((?:19|20)\d{{2}})\s*[.\-년/]\s*\d{{1,2}})", re.I)


def year_from_pdf_text(text: str) -> str | None:
    """PDF 첫 쪽에 찍힌 학술지 발행연도. 인용 표기 > Copyright 연도 > 게재 승인 연도 순.
    파일을 다시 저장해도 바뀌지 않으므로 파일 메타데이터 날짜가 믿을 수 없을 때 쓴다."""
    text = re.sub(r"\s+", " ", text or "")
    for pat in (_CITE, _VOL, _COPY):
        ys = [int(m.group(1)) for m in pat.finditer(text) if _ok(int(m.group(1)))]
        if ys:
            return str(max(set(ys), key=ys.count))
    ys = [int(m.group(1) or m.group(2)) for m in _ACCEPT.finditer(text)]
    ys = [y for y in ys if _ok(y)]
    return str(max(ys)) if ys else None


def text_year(fetcher: Fetcher, url: str, verify: bool = True, referer: str | None = None, limit: int = 20 * 1024 * 1024) -> str | None:
    """PDF를 한 번 내려받아 앞 두 쪽 본문에서 발행연도를 읽는다."""
    import io

    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    r = fetcher.get(url, verify=verify, stream=True, headers={"Referer": referer} if referer else {})
    if r is None or r.status_code >= 400 or "html" in r.headers.get("content-type", "").lower():
        if r is not None:
            r.close()
        return None
    data = _read(r, limit)
    if b"%PDF" not in data[:1024]:
        return None
    try:
        reader = PdfReader(io.BytesIO(data))
        text = " ".join((reader.pages[i].extract_text() or "") for i in range(min(2, len(reader.pages))))
    except Exception:
        return None
    return year_from_pdf_text(text)


_VARIANT = re.compile(r"요약본|요약|전체본|전문|본문|슬라이드|국문|영문|한글|korean|english|summary|full ?text|updated?|업데이트|수정본|\(.*?\)|[^0-9a-z가-힣]", re.I)


def file_value(it: dict) -> str | None:
    """항목이 파일에서 얻은 날짜(본 날짜이거나 일괄 등록일 대신 쓸 날짜)."""
    if (it.get("date_source") or "").startswith("file") and it.get("date"):
        return it["date"]
    fd = it.get("file_date")
    return fd[0] if fd else None


def unreliable_file_days(items) -> set[tuple[str, str]]:
    """한 학회에서 서로 다른 지침 2건 이상이 같은 파일 날짜를 가지면 그 날짜는 문서 날짜가 아니다
    (사이트 이전 때 파일을 한꺼번에 다시 저장했거나 같은 양식을 쓴 경우). 같은 지침의 요약본·전체본·
    국문/영문처럼 짝을 이루는 파일은 같은 날 만들 수 있으므로 한 건으로 친다."""
    fams: dict[tuple[str, str], set[str]] = {}
    for it in items:
        v = file_value(it)
        if v:
            fams.setdefault((it.get("society"), v), set()).add(_VARIANT.sub("", (it.get("title") or "").lower()))
    return {k for k, f in fams.items() if len(f) >= 2}


def _read(r, limit: int) -> bytes:
    try:
        buf = b""
        for chunk in r.iter_content(65536):
            buf += chunk
            if len(buf) >= limit:
                break
        return buf
    finally:
        r.close()


def _filename(r, url: str) -> str:
    cd = r.headers.get("content-disposition", "") if r is not None else ""
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd, re.I)
    name = m.group(1) if m else url.rsplit("/", 1)[-1]
    name = unquote(name)
    for enc in ("utf-8", "cp949"):
        try:
            return name.encode("latin-1").decode(enc)
        except (UnicodeDecodeError, UnicodeEncodeError):
            continue
    return name


def file_date(fetcher: Fetcher, url: str, verify: bool = True, referer: str | None = None) -> tuple[str, str, str] | None:
    """(날짜, 정밀도, 근거) 또는 None. referer: 게시판 주소(목록에서 클릭한 것과 같은 요청)."""
    base = {"Referer": referer} if referer else {}
    r = fetcher.get(url, verify=verify, stream=True, headers={**base, "Range": f"bytes=-{CHUNK}"})
    if r is None or r.status_code >= 400:
        if r is not None:
            r.close()
        return None
    ctype = r.headers.get("content-type", "").lower()
    if "html" in ctype:
        r.close()
        return None
    name = _filename(r, url)
    last_mod = r.headers.get("last-modified")
    ranged = r.status_code == 206
    if ranged:
        tail = _read(r, CHUNK)
    else:
        # Range를 무시하고 파일 전체를 보내는 서버: 문서 작성일은 보통 파일 끝에 있으므로 끝까지(최대 8MB) 읽는다
        tail = b""
        try:
            for chunk in r.iter_content(262144):
                tail += chunk
                if b"%PDF" not in tail[:1024] or len(tail) >= FULL_LIMIT:
                    break
        finally:
            r.close()
    is_pdf = "pdf" in ctype or name.lower().endswith(".pdf") or b"%PDF" in tail[:1024] or b"/Type" in tail
    if is_pdf:
        got = _from_pdf_bytes(tail)
        if not got and ranged:
            r2 = fetcher.get(url, verify=verify, stream=True, headers={**base, "Range": f"bytes=0-{CHUNK - 1}"})
            if r2 is not None and r2.status_code < 400:
                got = _from_pdf_bytes(_read(r2, CHUNK))
        if got:
            return got[0], got[1], "pdf"
    y = year_from_title(re.sub(r"[_\-.]", " ", name))
    if y:
        return y[0], y[1], "filename"
    if last_mod:
        try:
            d = parsedate_to_datetime(last_mod).date()
            # 요청할 때마다 파일을 새로 만드는 다운로드 주소는 수정일이 '오늘'로 나오므로 버린다
            if _ok(d.year, d.month, d.day) and (dt.date.today() - d).days > 3:
                return d.isoformat(), "day", "lastmod"
        except (TypeError, ValueError):
            pass
    return None
