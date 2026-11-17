"""대한비만학회 — 전문가 홈페이지 '비만 진료지침' 창(popup/obesity_cpg.html).

표 한 행이 파일 하나다 (예: '비만진료지침 2022 개정 8판 (JOMES)', '… (요약본)').
같은 판의 행은 한 지침으로 묶고, 괄호 안 표기를 파일 이름으로 쓴다.
robots.txt는 /board/·/*.pdf$만 막고 /popup/·/inc/는 허용한다 (2026-11-17 확인).
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from gcrawler.parse import Row, clean

_PAREN = re.compile(r"\s*[(（]([^)）]*)[)）]\s*$")


def parse_list(html: str, url: str, board: dict) -> list[Row]:
    soup = BeautifulSoup(html, "lxml")
    groups: dict[str, Row] = {}
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        a = tr.find("a", href=re.compile(r"download", re.I))
        if len(tds) < 2 or not a:
            continue
        year = clean(tds[0].get_text(" "))
        full = clean(tds[1].get_text(" "))
        m = _PAREN.search(full)
        title = clean(_PAREN.sub("", full)) if m else full
        label = m.group(1).strip() if m else "본문"
        if not title:
            continue
        f = {"name": label, "url": urljoin(url, a["href"]), "kind": "pdf"}
        row = groups.get(title)
        if row is None:
            y = year if re.fullmatch(r"(19|20)\d\d", year) else None
            row = groups[title] = Row(title=title, url=f["url"], date=y, date_precision="year" if y else None)
        row.attachments.append(f)
    # 전문 > JOMES 게재본 > 그 밖 순으로 대표 파일
    order = lambda x: (0 if "전문" in x["name"] else 1 if "JOMES" in x["name"].upper() else 2)
    for row in groups.values():
        row.attachments.sort(key=order)
        row.url = row.attachments[0]["url"]
    return list(groups.values())


def page_url(board: dict, page: int) -> str | None:
    return None  # 한 쪽짜리 표
