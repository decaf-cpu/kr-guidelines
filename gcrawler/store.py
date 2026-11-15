"""수집 결과 저장소 (data/*.json). Git에 커밋되어 이력이 남는다."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import threading
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
        self.items: dict[str, dict] = {it["id"]: it for it in doc.get("items", [])}
        self.new_ids: set[str] = set()
        self._lock = threading.Lock()

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
