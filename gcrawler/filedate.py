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
