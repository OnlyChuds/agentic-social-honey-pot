from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from src.store.db import AssetRecord


class TeamAssetCache:
    """Team-wide persistent SHA-256 -> AssetRecord cache protecting the 300-asset cap across runs."""

    def __init__(self, path: Path = Path("data/team_assets.json")) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def _read_raw(self) -> dict[str, dict[str, Any]]:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): dict(v) for k, v in data.items() if isinstance(v, dict)}
        except (OSError, json.JSONDecodeError, ValueError):
            pass
        return {}

    def _write_raw(self, payload: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    @staticmethod
    def _to_record(sha256: str, item: dict[str, Any]) -> AssetRecord | None:
        asset_id = str(item.get("asset_id") or "").strip()
        if not asset_id:
            return None
        return AssetRecord(
            asset_id=asset_id,
            local_path=str(item.get("local_path") or ""),
            sha256=str(item.get("sha256") or sha256),
            content_type=str(item.get("content_type") or "image/png"),
            size_bytes=int(item.get("size_bytes") or 0),
            created_at=str(item.get("created_at") or ""),
        )

    def get(self, sha256: str) -> AssetRecord | None:
        key = sha256.strip().lower()
        if not key:
            return None
        with self._lock:
            raw = self._read_raw()
            item = raw.get(key)
            if item is None:
                return None
            return self._to_record(key, item)

    def put(self, record: AssetRecord) -> None:
        key = record.sha256.strip().lower()
        if not key or not record.asset_id.strip():
            return
        with self._lock:
            raw = self._read_raw()
            raw[key] = {
                "asset_id": record.asset_id,
                "local_path": record.local_path,
                "sha256": key,
                "content_type": record.content_type,
                "size_bytes": int(record.size_bytes),
                "created_at": record.created_at,
            }
            try:
                self._write_raw(raw)
            except OSError:
                return

    def count(self) -> int:
        with self._lock:
            return len(self._read_raw())

    def all_records(self) -> list[AssetRecord]:
        with self._lock:
            raw = self._read_raw()
            out: list[AssetRecord] = []
            for sha, item in raw.items():
                rec = self._to_record(sha, item)
                if rec is not None:
                    out.append(rec)
            return out
