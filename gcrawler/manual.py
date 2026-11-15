"""config/manual.yaml: 자동 수집이 막힌 학회(robots.txt·방화벽 등)의 진료지침을 손으로 등록."""
from __future__ import annotations

import hashlib
import re

import yaml

from .filters import lang_of, tags_for
from .store import CONFIG

_DATE = re.compile(r"^(19|20)\d{2}(-\d{2}(-\d{2})?)?$")


def load_manual(societies: dict[str, dict]) -> tuple[list[dict], list[str]]:
    """(항목 목록, 오류 메시지 목록). 오류가 있는 항목은 건너뛴다."""
    path = CONFIG / "manual.yaml"
    if not path.exists():
        return [], []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    items, errors = [], []
    for i, e in enumerate(raw, 1):
        where = f"manual.yaml {i}번째 항목"
        if not isinstance(e, dict):
            errors.append(f"{where}: 형식 오류")
            continue
        soc, title, url = e.get("society"), str(e.get("title") or "").strip(), str(e.get("url") or "").strip()
        if soc not in societies:
            errors.append(f"{where}: 알 수 없는 학회 id '{soc}'")
            continue
        if not title or not url.startswith(("http://", "https://")):
            errors.append(f"{where}: title과 url(http로 시작)은 필수")
            continue
        date = str(e.get("date") or "").strip()
        if date and not _DATE.match(date):
            errors.append(f"{where}: date는 2026 / 2026-06 / 2026-06-12 형식")
            date = ""
        added = str(e.get("added") or "").strip() or None
        prec = {4: "year", 7: "month", 10: "day"}.get(len(date)) if date else None
        files = []
        for f in e.get("files") or []:
            if isinstance(f, str):
                f = {"url": f}
            fu = str(f.get("url") or "")
            if fu.startswith(("http://", "https://")):
                kind = (re.search(r"\.(pdf|hwp|hwpx|docx?|pptx?)(?:$|\?)", fu, re.I) or [None, "file"])[1].lower()
                files.append({"name": f.get("name") or title, "url": fu, "kind": kind})
        items.append(
            {
                "id": "m" + hashlib.sha1(f"{soc}|{url}|{title}".encode()).hexdigest()[:15],
                "society": soc,
                "title": title,
                "url": url,
                "board_name": "수동 등록",
                "date": date or None,
                "date_precision": prec,
                "date_source": "manual" if date else None,
                "first_seen": added,
                "access": "public",
                "lang": lang_of(title),
                "tags": tags_for(title),
                "attachments": files,
                "link_resolved": True,
                "manual": True,
                "backfill": added is None,
            }
        )
    return items, errors
