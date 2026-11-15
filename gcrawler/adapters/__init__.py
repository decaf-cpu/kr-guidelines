"""학회별 맞춤 파서.

범용 파서로 안 읽히는 게시판은 이 폴더에 `<학회id>.py` (하이픈은 밑줄로) 파일을 만들고
아래 함수 중 필요한 것만 정의하면 자동으로 사용된다.

    def parse_list(html: str, url: str, board: dict) -> list[Row]:
        '''목록 HTML → Row 리스트'''

    def page_url(board: dict, page: int) -> str | None:
        '''n쪽 목록 주소 (POST/AJAX 게시판 등 자동 감지가 안 될 때)'''

예시는 example_adapter.py 참고.
"""
from __future__ import annotations

import importlib
from types import ModuleType


def load(society_id: str) -> ModuleType | None:
    name = society_id.replace("-", "_")
    try:
        return importlib.import_module(f"{__name__}.{name}")
    except ModuleNotFoundError as e:
        if e.name and e.name.endswith(name):
            return None
        raise
