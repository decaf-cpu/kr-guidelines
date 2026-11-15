"""진단용: 학회 홈페이지·게시판 HTML을 그대로 저장해 파서 설정을 고칠 수 있게 한다.

  python -m gcrawler probe                # 수집 현황에서 '정상'이 아닌 학회 전부
  python -m gcrawler probe kasl ksn       # 지정한 학회만
결과: probe-out/<학회id>/ 아래 HTML, anchors.tsv, log.txt
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .fetch import Fetcher, decode
from .parse import clean
from .store import DATA, ROOT, load_json, load_societies

OUT = ROOT / "probe-out"
_META_REFRESH = re.compile(r"url\s*=\s*['\"]?([^'\"\s>]+)", re.I)
_JS_REDIRECT = re.compile(r"(?:location\.href|location\.replace|location)\s*[=(]\s*['\"]([^'\"]+)['\"]", re.I)


def _anchors(html: str, base: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    rows = []
    for a in soup.find_all("a"):
        img = a.find("img")
        rows.append(
            "\t".join(
                [
                    clean(a.get_text(" "))[:80],
                    clean(img.get("alt")) if img else "",
                    clean(a.get("title"))[:60],
                    urljoin(base, (a.get("href") or "").strip())[:300],
                    clean(a.get("onclick"))[:120],
                ]
            )
        )
    return rows


def _next_hops(html: str, base: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    hops = []
    m = soup.find("meta", attrs={"http-equiv": re.compile("refresh", re.I)})
    if m and _META_REFRESH.search(m.get("content", "")):
        hops.append(urljoin(base, _META_REFRESH.search(m["content"]).group(1)))
    for s in soup.find_all("script"):
        if s.string:
            for u in _JS_REDIRECT.findall(s.string)[:2]:
                hops.append(urljoin(base, u))
    for f in soup.find_all(["frame", "iframe"]):
        if f.get("src") and not f["src"].startswith(("about:", "javascript")):
            hops.append(urljoin(base, f["src"]))
    for a in soup.find_all("a"):
        if re.search(r"(사이트\s*맵|sitemap)", clean(a.get_text(" ")) + (a.get("title") or ""), re.I) and a.get("href"):
            hops.append(urljoin(base, a["href"]))
            break
    seen, out = set(), []
    for h in hops:
        if h not in seen and h.startswith("http"):
            seen.add(h)
            out.append(h)
    return out[:6]


def probe_one(f: Fetcher, soc: dict, extra_urls: list[str]) -> None:
    d = OUT / soc["id"]
    d.mkdir(parents=True, exist_ok=True)
    log = []
    queue = [("home", soc["homepage"], 0)] + [(f"board{i}", u, 1) for i, u in enumerate(extra_urls)]
    done = set()
    n = 0
    while queue and n < 12:
        name, url, depth = queue.pop(0)
        if url in done:
            continue
        done.add(url)
        n += 1
        r = f.get(url, verify=soc.get("verify_ssl", True))
        if r is None:
            log.append(f"{name}\t{url}\tFAILED (네트워크 오류/시간 초과/robots)")
            continue
        log.append(f"{name}\t{url}\tHTTP {r.status_code}\t→ {r.url}\t{r.headers.get('content-type','')}\t{len(r.content)}B")
        if "html" not in r.headers.get("content-type", "html").lower():
            continue
        html = decode(r)
        fname = f"{n:02d}_{name}.html"
        (d / fname).write_text(html, encoding="utf-8")
        (d / f"{n:02d}_{name}.anchors.tsv").write_text("text\talt\ttitle\thref\tonclick\n" + "\n".join(_anchors(html, r.url)), encoding="utf-8")
        if depth < 2:
            for i, h in enumerate(_next_hops(html, r.url)):
                queue.append((f"{name}-hop{i}", h, depth + 1))
    (d / "log.txt").write_text("\n".join(log) + "\n", encoding="utf-8")


def run(ids: list[str] | None = None) -> None:
    socs = {s["id"]: s for s in load_societies()}
    status = load_json(DATA / "status.json", {"societies": {}})["societies"]
    if not ids:
        ids = [i for i in socs if not status.get(i, {}).get("ok")]
    f = Fetcher(delay=1.0, respect_robots=False, retries=1, timeout=20)
    for i in ids:
        soc = socs[i]
        boards = [b["url"] for b in soc.get("boards", [])]
        boards += [b["url"] for b in status.get(i, {}).get("boards", []) if b.get("url") and b["url"] not in boards]
        print("probe", i, soc["homepage"], len(boards))
        try:
            probe_one(f, soc, boards[:4])
        except Exception as e:  # 진단 도구이므로 계속 진행
            (OUT / i).mkdir(parents=True, exist_ok=True)
            (OUT / i / "log.txt").write_text(f"ERROR {type(e).__name__}: {e}\n", encoding="utf-8")
    (OUT / "index.json").write_text(json.dumps(ids, ensure_ascii=False), encoding="utf-8")
