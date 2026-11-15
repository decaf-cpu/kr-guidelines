"""회원 전용 여부 판정. 회원 전용으로 판단되면 사이트에 싣지 않는다."""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from .fetch import Fetcher, decode
from .parse import parse_detail

LOGIN_TEXT = re.compile(
    r"(로그인\s*(후|이\s*필요|하셔야|해\s*주|하신\s*후|을\s*하셔야)|회원\s*(만|전용|에\s*한(해|하여))|"
    r"권한이\s*없|접근\s*권한|열람\s*권한|읽기\s*권한|정회원|평생회원만|"
    r"members?\s*only|please\s*log\s*in|login\s*required|permission\s*denied)",
    re.I,
)
LOGIN_URL = re.compile(r"(login|signin|sign_in|member/log|mem_login|/auth|[?&]url=%2F)", re.I)


def _html_says_login(html: str) -> bool:
    head = html[:60000]
    soup = BeautifulSoup(head, "lxml")
    # alert('로그인 후 이용해 주세요') 식 차단
    for s in soup.find_all("script"):
        if s.string and "alert" in s.string and LOGIN_TEXT.search(s.string):
            return True
    has_pw = soup.find("input", attrs={"type": "password"}) is not None
    body_text = soup.get_text(" ")
    return bool(LOGIN_TEXT.search(body_text)) and (has_pw or len(body_text) < 3000)


_REFRESH_URL = re.compile(r"<meta[^>]+http-equiv=[\"']?refresh[^>]+url\s*=\s*([^\"'>\s]+)", re.I)
_JS_LOC = re.compile(r"location(?:\.href|\.replace)?\s*[=(]\s*['\"]([^'\"]+)['\"]", re.I)


def page_requires_login(html: str) -> bool:
    """목록/상세 페이지가 본문 안에서 로그인 페이지로 보내거나 '회원만' 경고를 띄우는지."""
    head = html[:200000]
    soup = BeautifulSoup(head, "lxml")
    urls = []
    for m in soup.find_all("meta"):
        if "refresh" in (m.get("http-equiv") or "").lower():
            mm = re.search(r"url\s*=\s*['\"]?([^'\"\s>]+)", m.get("content") or "", re.I)
            if mm:
                urls.append(mm.group(1))
    for sc in soup.find_all("script"):
        if sc.string:
            urls += _JS_LOC.findall(sc.string)
    if any(LOGIN_URL.search(u) or re.search(r"member/\?sn=3", u) for u in urls):
        return True
    return _html_says_login(head)


def check_detail(fetcher: Fetcher, url: str, verify: bool = True, check_file: bool = True) -> dict:
    """상세 페이지를 열어 공개 여부·날짜·첨부를 확인."""
    r = fetcher.get(url, verify=verify)
    if r is None:
        return {"access": "unknown"}
    if r.status_code in (401, 403):
        return {"access": "members"}
    if r.status_code >= 400:
        return {"access": "unknown", "http": r.status_code}
    if LOGIN_URL.search(r.url) and not LOGIN_URL.search(url):
        return {"access": "members"}
    html = decode(r)
    if page_requires_login(html):
        return {"access": "members"}
    info = parse_detail(html, r.url)
    result = {"access": "public", "detail_date": info["date"], "attachments": info["attachments"],
              "pub_year": info.get("pub_year"), "doi": info.get("doi")}

    # 첨부파일만 회원 전용인 경우가 흔함 → 첫 첨부 하나만 확인
    if check_file and info["attachments"]:
        fr = fetcher.get(info["attachments"][0]["url"], verify=verify, stream=True)
        if fr is not None:
            try:
                ctype = fr.headers.get("content-type", "").lower()
                if fr.status_code in (401, 403) or LOGIN_URL.search(fr.url or ""):
                    result["access"] = "members"
                elif "text/html" in ctype:
                    chunk = next(fr.iter_content(40000), b"")
                    if LOGIN_TEXT.search(chunk.decode("utf-8", "ignore")) or LOGIN_TEXT.search(chunk.decode("cp949", "ignore")):
                        result["access"] = "members"
            finally:
                fr.close()
    return result
