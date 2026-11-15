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


def _ok(y: int, m: int = 1, d: int = 1) -> bool:
    try:
        x = dt.date(y, m, d)
    except ValueError:
        return False
    return dt.date(1990, 1, 1) <= x <= dt.date.today()


def _from_pdf_bytes(b: bytes) -> tuple[str, str] | None:
    found = []
    for pat in _PDF_DATE:
        for m in pat.finditer(b):
            y = int(m.group(1))
            mo = int(m.group(2)) if m.group(2) else None
            d = int(m.group(3)) if m.group(3) else None
            if mo and d and _ok(y, mo, d):
                found.append((f"{y:04d}-{mo:02d}-{d:02d}", "day"))
            elif mo and _ok(y, mo):
                found.append((f"{y:04d}-{mo:02d}", "month"))
            elif _ok(y):
                found.append((f"{y:04d}", "year"))
    return min(found) if found else None  # 여러 개면 가장 이른 날짜(최초 작성)


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
    try:
        name = unquote(name)
        name = name.encode("latin-1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass
    return name


def file_date(fetcher: Fetcher, url: str, verify: bool = True) -> tuple[str, str, str] | None:
    """(날짜, 정밀도, 근거) 또는 None."""
    r = fetcher.get(url, verify=verify, stream=True, headers={"Range": f"bytes=-{CHUNK}"})
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
    tail = _read(r, CHUNK)
    is_pdf = "pdf" in ctype or name.lower().endswith(".pdf") or b"%PDF" in tail[:1024] or b"/Type" in tail
    if is_pdf:
        got = _from_pdf_bytes(tail)
        if not got and ranged:
            r2 = fetcher.get(url, verify=verify, stream=True, headers={"Range": f"bytes=0-{CHUNK - 1}"})
            if r2 is not None and r2.status_code < 400:
                got = _from_pdf_bytes(_read(r2, CHUNK))
        if got:
            return got[0], got[1], "pdf"
    y = year_from_title(re.sub(r"[_\-.]", " ", name))
    if y:
        return y[0], "year", "filename"
    if last_mod:
        try:
            d = parsedate_to_datetime(last_mod).date()
            if _ok(d.year, d.month, d.day):
                return d.isoformat(), "day", "lastmod"
        except (TypeError, ValueError):
            pass
    return None
