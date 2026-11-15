"""맞춤 파서 예시 (실제로 쓰이지 않음).

사용법: 이 파일을 복사해 `<학회id>.py`로 저장하고 내용을 고친다.
예) 대한OO학회 id가 'ksxx'이면 gcrawler/adapters/ksxx.py
"""
from __future__ import annotations

from urllib.parse import urljoin

from bs4 import BeautifulSoup

from gcrawler.parse import Row, clean, find_date


def parse_list(html: str, url: str, board: dict) -> list[Row]:
    soup = BeautifulSoup(html, "lxml")
    rows = []
    for el in soup.select("ul.board_list > li"):
        a = el.select_one("a.subject")
        if not a:
            continue
        d = find_date(clean(el.select_one(".date").get_text())) if el.select_one(".date") else None
        rows.append(
            Row(
                title=clean(a.get_text()),
                url=urljoin(url, a["href"]),
                date=d[0] if d else None,
                date_precision=d[1] if d else None,
            )
        )
    return rows


def page_url(board: dict, page: int) -> str | None:
    return f"{board['url']}&pageIndex={page}"
