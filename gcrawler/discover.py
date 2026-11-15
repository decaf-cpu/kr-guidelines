"""홈페이지 메뉴에서 진료지침 게시판 후보를 찾는다 (boards가 비어 있는 학회용)."""
from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .fetch import Fetcher
from .parse import canonical_url, clean, is_attachment, same_site

MENU_KEY = re.compile(r"(진료\s*지침|진료\s*가이드\s*라인|임상\s*진료\s*지침|가이드\s*라인|guideline|권고\s*안|치료\s*지침|지침)", re.I)
NEGATIVE = re.compile(r"(국외|해외|외국|international|global|links?|사이트|관련\s*기관|개발\s*매뉴얼|개발\s*방법|위원회\s*소개|윤리|투고|저자|author|편집|심사|규정)", re.I)


def score_label(label: str) -> int:
    s = 0
    if re.search(r"(진료\s*지침|진료\s*가이드\s*라인|임상\s*진료\s*지침)", label):
        s += 5
    elif re.search(r"(가이드\s*라인|guideline)", label, re.I):
        s += 4
    elif re.search(r"지침", label):
        s += 3
    elif re.search(r"권고", label):
        s += 2
    if NEGATIVE.search(label):
        s -= 6
    return s


def find_candidates(html: str, page_url: str, homepage: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    found: dict[str, dict] = {}
    for a in soup.find_all("a", href=True):
        label = clean(a.get_text(" ")) or clean(a.get("title"))
        if not label or len(label) > 30 or not MENU_KEY.search(label):
            continue
        href = a["href"].strip()
        if href.lower().startswith(("javascript", "#", "mailto")):
            continue
        if is_attachment(a, href):
            continue
        url = urljoin(page_url, href)
        if not same_site(url, homepage):
            continue
        s = score_label(label)
        if s <= 0:
            continue
        key = canonical_url(url)
        if key not in found or found[key]["score"] < s:
            found[key] = {"url": url, "label": label, "score": s}
    return sorted(found.values(), key=lambda x: -x["score"])


def discover(fetcher: Fetcher, homepage: str, verify: bool = True, limit: int = 3) -> dict:
    html, resp = fetcher.get_html(homepage, verify=verify)
    if html is None:
        code = resp.status_code if resp is not None else None
        return {"ok": False, "error": f"홈페이지 접속 실패 (HTTP {code})" if code else "홈페이지 접속 실패", "candidates": []}
    cands = find_candidates(html, resp.url, homepage)
    if not cands:
        return {"ok": False, "error": "메뉴에서 진료지침 링크를 찾지 못함 (수동 설정 필요)", "candidates": []}
    return {"ok": True, "candidates": cands[:limit]}
