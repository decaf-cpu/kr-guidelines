"""수집 결과 저장소 (data/*.json). Git에 커밋되어 이력이 남는다."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import threading
import unicodedata
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CONFIG = ROOT / "config"


def now_iso() -> str:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).isoformat(timespec="seconds")


def today() -> str:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).date().isoformat()


def load_json(path: Path, default):
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=False)
        f.write("\n")
    tmp.replace(path)


def load_societies() -> list[dict]:
    with (CONFIG / "societies.yaml").open(encoding="utf-8") as f:
        socs = yaml.safe_load(f) or []
    ids = [s["id"] for s in socs]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise ValueError(f"societies.yaml에 중복 id: {dup}")
    for s in socs:
        s.setdefault("boards", [])
        s.setdefault("enabled", True)
        s.setdefault("verify_ssl", True)
    return socs


def load_taxonomy() -> dict:
    with (CONFIG / "taxonomy.yaml").open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def item_id(society: str, canon_url: str | None, title: str, board: str) -> str:
    key = f"{society}|{canon_url or board + '#' + title}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


class Store:
    def __init__(self):
        self.path = DATA / "guidelines.json"
        doc = load_json(self.path, {"items": []})
        self.items: dict[str, dict] = {}
        for it in doc.get("items", []):
            # backfill 표시가 생기기 전(2026-11-15 이전 수집분)은 모두 초기 수집
            it.setdefault("backfill", True)
            self._absorb(it)
        self.new_ids: set[str] = set()
        self._lock = threading.Lock()

    def _absorb(self, it: dict) -> None:
        """주소 정규화 규칙이 바뀌어도 같은 글이 두 번 저장되지 않게 id를 다시 계산해 합친다."""
        from .parse import canonical_url

        if it.get("title"):
            it["title"] = unicodedata.normalize("NFC", it["title"])
        canon = canonical_url(it["url"]) if it.get("url") and it.get("link_resolved", True) else None
        iid = item_id(it.get("society", ""), canon, it.get("title", ""), it.get("board", ""))
        it["id"] = iid
        old = self.items.get(iid)
        if old is None:
            self.items[iid] = it
            return
        keep, other = (old, it) if (old.get("first_seen") or "9") <= (it.get("first_seen") or "9") else (it, old)
        for k, v in other.items():
            keep.setdefault(k, v)
        if keep.get("access") == "unknown" and other.get("access") not in (None, "unknown"):
            keep["access"] = other["access"]
        self.items[iid] = keep

    def get(self, iid: str) -> dict | None:
        return self.items.get(iid)

    def upsert(self, iid: str, fields: dict) -> tuple[dict, bool]:
        t = today()
        with self._lock:
            it = self.items.get(iid)
            is_new = it is None
            if is_new:
                it = {"id": iid, "first_seen": t, "access": "unknown"}
                self.items[iid] = it
                self.new_ids.add(iid)
        for k, v in fields.items():
            if v is None or v == [] or v == "":
                continue
            it[k] = v
        it["last_seen"] = t
        return it, is_new

    def save(self) -> None:
        with self._lock:
            snapshot = list(self.items.values())
        items = sorted(snapshot, key=lambda x: (x.get("society", ""), x.get("first_seen", ""), x["id"]))
        save_json(self.path, {"updated": now_iso(), "count": len(items), "items": items})
