"""홈페이지 메뉴에서 진료지침 게시판 후보를 찾는다 (boards가 비어 있는 학회용)."""
from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .fetch import Fetcher
from .parse import canonical_url, clean, is_attachment, same_site

MENU_KEY = re.compile(r"(진료\s*지침|진료\s*가이드\s*라인|임상\s*진료\s*지침|가이드\s*라인|guideline|권고\s*안|치료\s*지침|지침)", re.I)
NEGATIVE = re.compile(r"(국외|해외|외국|국제|international|global|links?|사이트|관련\s*기관|개발\s*매뉴얼|개발\s*방법|위원회|윤리|투고|저자|author|편집|심사|규정|평가|인증\s*제도)", re.I)
HUB = re.compile(r"(자료실|자료|학술|정보|간행물|발간|publication|출판|진료|교육|사이트\s*맵|sitemap|전체\s*메뉴|"
                 r"전문가|의료인|회원용|회원\s*홈|professional|for\s*professionals|kor\s*회원|국문)", re.I)
_JS_CALL = re.compile(r"^javascript:\s*([A-Za-z_$][\w$]*)\s*\(\s*\)\s*;?\s*$", re.I)
PORTAL = re.compile(r"(전문가|의료인|회원용|professional|kor\s*회원|국문\s*홈)", re.I)
_META_REFRESH = re.compile(r"url\s*=\s*['\"]?([^'\"\s>]+)", re.I)
_JS_REDIRECT = re.compile(r"(?:location\.href|location\.replace|window\.location|document\.location|location)\s*[=(]\s*['\"]([^'\"]+)['\"]", re.I)


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


def _label(a) -> str:
    label = clean(a.get_text(" ")) or clean(a.get("title"))
    if not label:
        img = a.find("img")
        if img is not None:
            label = clean(img.get("alt"))
    return label


def redirects(html: str, page_url: str) -> list[str]:
    """첫 화면이 meta refresh·JS 이동·프레임으로 진짜 홈페이지를 감싸는 경우."""
    soup = BeautifulSoup(html, "lxml")
    out = []
    m = soup.find("meta", attrs={"http-equiv": re.compile("refresh", re.I)})
    if m and _META_REFRESH.search(m.get("content", "")):
        out.append(urljoin(page_url, _META_REFRESH.search(m["content"]).group(1)))
    if len(soup.get_text(" ").strip()) < 400:
        for s in soup.find_all("script"):
            if s.string:
                out += [urljoin(page_url, u) for u in _JS_REDIRECT.findall(s.string)[:2]]
    for f in soup.find_all(["frame", "iframe"]):
        src = (f.get("src") or "").strip()
        if src and not src.startswith(("about:", "javascript")) and f.name == "frame":
            out.append(urljoin(page_url, src))
    return [u for u in dict.fromkeys(out) if u.startswith("http")]


def hub_links(html: str, page_url: str, homepage: str, limit: int = 4, js_map: dict | None = None) -> list[str]:
    """진료지침 메뉴가 안 보일 때 둘러볼 만한 상위 메뉴(자료실·학술·사이트맵 등)."""
    soup = BeautifulSoup(html, "lxml")
    scored = []
    for a in soup.find_all("a", href=True):
        label = _label(a)
        href = a["href"].strip()
        m = _JS_CALL.match(href)
        if m and js_map and m.group(1) in js_map:
            href = js_map[m.group(1)]
        if not label or len(label) > 20 or href.lower().startswith(("javascript", "#", "mailto")):
            continue
        if not HUB.search(label) or NEGATIVE.search(label):
            continue
        u = urljoin(page_url, href)
        if not same_site(u, homepage) or is_attachment(a, href):
            continue
        if PORTAL.search(label):
            s = 4
        elif re.search(r"(사이트\s*맵|sitemap|전체\s*메뉴)", label, re.I):
            s = 3
        elif re.search(r"(자료|진료)", label):
            s = 2
        else:
            s = 1
        scored.append((s, u))
    scored.sort(key=lambda x: -x[0])
    return list(dict.fromkeys(u for _, u in scored))[:limit]


def js_functions(fetcher: Fetcher, html: str, page_url: str, homepage: str, verify: bool) -> dict[str, str]:
    """javascript:data03() 같은 메뉴 링크를 실제 주소로 바꾸기 위해
    페이지 안·같은 사이트 스크립트에서 function 이름() { location.href='...' }를 찾는다."""
    soup = BeautifulSoup(html, "lxml")
    sources = [s.string or "" for s in soup.find_all("script") if not s.get("src")]
    srcs = [urljoin(page_url, s["src"]) for s in soup.find_all("script", src=True)]
    srcs = [u for u in srcs if same_site(u, homepage) and not re.search(r"(jquery|bootstrap|slick|swiper|aos|gtag|analytics)", u, re.I)]
    for u in srcs[:6]:
        r = fetcher.get(u, verify=verify)
        if r is not None and r.status_code < 400:
            sources.append(r.content.decode("utf-8", "ignore") + "\n" + r.content.decode("cp949", "ignore"))
    out: dict[str, str] = {}
    pat = re.compile(
        r"(?:function\s+([A-Za-z_$][\w$]*)\s*\([^)]*\)|([A-Za-z_$][\w$]*)\s*=\s*function\s*\([^)]*\))\s*\{"
        r"[^{}]{0,300}?(?:location(?:\.href)?|window\.open)\s*(?:=|\()\s*['\"]([^'\"]+)['\"]",
        re.S,
    )
    for src in sources:
        for m in pat.finditer(src):
            name = m.group(1) or m.group(2)
            out.setdefault(name, urljoin(page_url, m.group(3)))
    return out


def find_candidates(html: str, page_url: str, homepage: str, js_map: dict[str, str] | None = None) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    found: dict[str, dict] = {}
    for a in soup.find_all("a", href=True):
        label = _label(a)
        if not label or len(label) > 30 or not MENU_KEY.search(label):
            continue
        href = a["href"].strip()
        m = _JS_CALL.match(href)
        if m and js_map and m.group(1) in js_map:
            href = js_map[m.group(1)]
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
        return {"ok": False, "error": f"홈페이지 접속 실패 (HTTP {code})" if code else "홈페이지 접속 실패 (해외 접속 차단·주소 오류 가능)", "candidates": []}
    pages = [(html, resp.url)]
    # 1) 감싸는 첫 화면(meta refresh·JS 이동·frameset) 따라가기
    for u in redirects(html, resp.url)[:2]:
        h, r = fetcher.get_html(u, verify=verify)
        if h:
            pages.append((h, r.url))
    cands: dict[str, dict] = {}

    def add(found):
        for c in found:
            key = canonical_url(c["url"])
            if key not in cands or cands[key]["score"] < c["score"]:
                cands[key] = c

    js_cache: dict[str, dict] = {}

    def js_of(h: str, u: str) -> dict:
        if u not in js_cache:
            js_cache[u] = js_functions(fetcher, h, u, homepage, verify) if "javascript:" in h else {}
        return js_cache[u]

    def scan(h: str, u: str) -> None:
        found = find_candidates(h, u, homepage)
        if not found and "javascript:" in h:
            found = find_candidates(h, u, homepage, js_of(h, u))
        add(found)

    for h, u in pages:
        scan(h, u)
    # 2) 없으면 포털형 첫 화면의 '전문가 홈페이지', 자료실·학술·사이트맵 같은 메뉴를 두 단계까지 열어 보기
    seen = {canonical_url(u) for _, u in pages}
    frontier = pages
    budget = 8
    for _depth in range(2):
        if cands or budget <= 0:
            break
        hubs = []
        for h, u in frontier:
            hubs += hub_links(h, u, homepage, js_map=js_of(h, u))
        nxt = []
        for u in dict.fromkeys(hubs):
            if budget <= 0 or canonical_url(u) in seen:
                continue
            seen.add(canonical_url(u))
            budget -= 1
            h, r = fetcher.get_html(u, verify=verify)
            if h:
                nxt.append((h, r.url))
                scan(h, r.url)
        frontier = nxt
    if not cands:
        return {"ok": False, "error": "메뉴에서 진료지침 링크를 찾지 못함 (수동 설정 필요)", "candidates": []}
    ranked = sorted(cands.values(), key=lambda x: -x["score"])
    return {"ok": True, "candidates": ranked[:limit]}
