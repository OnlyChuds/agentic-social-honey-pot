from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Self
from uuid import uuid4

import httpx

from src.api.limits import RateGovernor
from src.config import TargetApiConfig
from src.store.asset_cache import TeamAssetCache
from src.store.db import MAX_ASSETS_DEFAULT, AssetCapError, AssetRecord, Store

SleepFn = Callable[[float], Awaitable[None]]

MAX_ASSET_BYTES = 5 * 1024 * 1024
MAX_PAYLOAD_BYTES = 2 * 1024 * 1024
MAX_TURN_CHARS = 4000
SUPPORTED_ASSET_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "application/pdf",
}


class TargetAPIError(RuntimeError):
    """Base error for Target API failures."""

    def __init__(self, status_code: int, message: str, *, retryable: bool = False) -> None:
        super().__init__(f"Target API HTTP {status_code}: {message}")
        self.status_code = status_code
        self.detail = message
        self.retryable = retryable


class RateLimitError(TargetAPIError):
    """Raised when HTTP 429 retries are exhausted."""


class BudgetExhaustedError(TargetAPIError):
    """Raised on HTTP 409 (touch budget exhausted, session ended, or eval runs exhausted)."""


class ConversationEndedError(TargetAPIError):
    """Raised on HTTP 409 when a live conversation has already ended or hit its turn limit."""


class StoryTimeError(TargetAPIError):
    """Raised on HTTP 409 when ``sent_at`` regresses earlier than the session's ``story_time``."""


class SimulatorRejectionError(TargetAPIError):
    """Raised on HTTP 422 (rejected by simulator/persona model — do not retry blindly)."""


class PageNotRegisteredError(RuntimeError):
    """Raised when attempting to send a message referencing an unregistered landing page."""


class AssetLimitError(RuntimeError):
    """Raised when asset size, type, or 300-asset cap is violated."""


class TargetAPIClient:
    """Async httpx client for the Target API with idempotency, backoff retries, and invariants."""

    def __init__(
        self,
        base_url: str,
        team_key: str,
        *,
        rate_governor: RateGovernor | None = None,
        backoff_s: list[int] | None = None,
        timeout_s: float = 120.0,
        store: Store | None = None,
        max_assets: int = MAX_ASSETS_DEFAULT,
        asset_cache: TeamAssetCache | None = None,
        client: httpx.AsyncClient | None = None,
        sleep: SleepFn = asyncio.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.team_key = team_key
        self.governor = rate_governor or RateGovernor()
        self.backoff_s = list(backoff_s) if backoff_s is not None else [30, 60, 90]
        self.store = store
        self.max_assets = max_assets
        if asset_cache is not None:
            self.asset_cache: TeamAssetCache | None = asset_cache
        elif store is not None and str(store.db_path) != ":memory:":
            self.asset_cache = TeamAssetCache(store.db_path.parent / "team_assets.json")
        else:
            self.asset_cache = None
        self._sleep = sleep
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self.team_key}"},
            timeout=timeout_s,
        )
        # In-memory mirrors used when no Store is attached (or as fast cache)
        self._registered_pages: dict[str, set[str]] = {}
        self._uploaded_assets_by_sha: dict[str, AssetRecord] = {}
        self._channels_cache: list[dict[str, Any]] | None = None

    @classmethod
    def from_config(
        cls,
        cfg: TargetApiConfig,
        *,
        store: Store | None = None,
        client: httpx.AsyncClient | None = None,
        sleep: SleepFn = asyncio.sleep,
    ) -> TargetAPIClient:
        governor = RateGovernor(
            req_per_min=cfg.rate.req_per_min,
            max_parallel=cfg.rate.max_parallel,
            sleep=sleep,
        )
        return cls(
            base_url=cfg.base_url,
            team_key=cfg.team_key,
            rate_governor=governor,
            backoff_s=cfg.retry.backoff_s,
            store=store,
            client=client,
            sleep=sleep,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    def _auth_headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.team_key:
            headers["Authorization"] = f"Bearer {self.team_key}"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def is_page_registered(self, session_id: str, url: str) -> bool:
        if url in self._registered_pages.get(session_id, set()):
            return True
        if self.store is not None and self.store.is_page_registered(session_id, url):
            self._registered_pages.setdefault(session_id, set()).add(url)
            return True
        return False

    def _record_registered_page(
        self,
        *,
        session_id: str,
        page_id: str,
        url: str,
        title: str,
        fields: list[str],
    ) -> None:
        self._registered_pages.setdefault(session_id, set()).add(url)
        if self.store is not None:
            self.store.record_page(
                session_id=session_id,
                page_id=page_id,
                url=url,
                title=title,
                fields=fields,
            )

    @staticmethod
    def _extract_error_detail(resp: httpx.Response) -> str:
        try:
            data = resp.json()
            if isinstance(data, dict):
                return str(data.get("detail") or data.get("error") or data)
            return str(data)
        except ValueError:
            return resp.text or resp.reason_phrase

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
        mutating: bool = False,
        idempotency_key: str | None = None,
    ) -> Any:
        if json_body is not None:
            encoded_len = len(json.dumps(json_body, ensure_ascii=False).encode("utf-8"))
            if encoded_len > MAX_PAYLOAD_BYTES:
                raise TargetAPIError(
                    413,
                    f"Request payload ({encoded_len} bytes) exceeds 2 MB limit",
                )

        idem_key = idempotency_key or (str(uuid4()) if mutating else None)
        headers = self._auth_headers(idem_key)
        max_attempts = len(self.backoff_s) + 1

        for attempt in range(max_attempts):
            try:
                async with self.governor.acquire():
                    resp = await self._client.request(
                        method,
                        path,
                        json=json_body,
                        files=files,
                        headers=headers,
                    )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt >= len(self.backoff_s):
                    raise TargetAPIError(
                        503,
                        f"Network/timeout error after {attempt + 1} attempts: {exc}",
                        retryable=True,
                    ) from exc
                await self._sleep(float(self.backoff_s[attempt]))
                continue

            status = resp.status_code
            if 200 <= status < 300:
                if not resp.content:
                    return {}
                return resp.json()

            detail = self._extract_error_detail(resp)

            if status == 429:
                if attempt >= len(self.backoff_s):
                    raise RateLimitError(429, detail, retryable=True)
                retry_after_raw = resp.headers.get("Retry-After")
                delay = float(self.backoff_s[attempt])
                if retry_after_raw is not None:
                    try:
                        delay = max(delay, float(retry_after_raw))
                    except ValueError:
                        pass
                await self._sleep(delay)
                continue

            if status in {502, 503, 504}:
                if attempt >= len(self.backoff_s):
                    raise TargetAPIError(status, detail, retryable=True)
                await self._sleep(float(self.backoff_s[attempt]))
                continue

            if status == 422:
                raise SimulatorRejectionError(422, detail, retryable=False)

            if status == 409:
                lower = detail.lower()
                if any(
                    tok in lower
                    for tok in ("story_time", "sent_at", "earlier than", "backwards", "chronolog", "timestamp")
                ):
                    raise StoryTimeError(409, detail, retryable=False)
                if "conversation" in lower and any(
                    tok in lower for tok in ("ended", "closed", "max turns", "turn limit", "complete")
                ):
                    raise ConversationEndedError(409, detail, retryable=False)
                raise BudgetExhaustedError(409, detail, retryable=False)

            raise TargetAPIError(status, detail, retryable=False)

        raise TargetAPIError(503, "Retry loop exited unexpectedly", retryable=True)

    # --- 1. Scenarios ---

    async def get_scenarios(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/scenarios", mutating=False)
        if not isinstance(data, list):
            raise TargetAPIError(500, f"Expected list from /scenarios, got {type(data).__name__}")
        return data

    async def get_channels(self, *, force_refresh: bool = False) -> list[dict[str, Any]]:
        if self._channels_cache is not None and not force_refresh:
            return [dict(c) for c in self._channels_cache]
        data = await self._request("GET", "/channels", mutating=False)
        if isinstance(data, dict) and isinstance(data.get("channels"), list):
            channels = [dict(c) for c in data["channels"] if isinstance(c, dict)]
        elif isinstance(data, list):
            channels = [dict(c) for c in data if isinstance(c, dict)]
        else:
            channels = []
        self._channels_cache = channels
        return [dict(c) for c in channels]

    # --- 2. Sessions ---

    async def create_session(
        self,
        scenario_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        res = await self._request(
            "POST",
            "/sessions",
            json_body={"scenario_id": scenario_id},
            mutating=True,
            idempotency_key=idempotency_key,
        )
        return dict(res)

    async def get_session(self, session_id: str) -> dict[str, Any]:
        res = await self._request("GET", f"/sessions/{session_id}", mutating=False)
        return dict(res)

    async def list_sessions(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/sessions", mutating=False)
        if isinstance(data, dict) and isinstance(data.get("sessions"), list):
            return [dict(s) for s in data["sessions"] if isinstance(s, dict)]
        if isinstance(data, list):
            return data
        return []

    async def get_session_events(self, session_id: str) -> list[dict[str, Any]]:
        data = await self._request("GET", f"/sessions/{session_id}/events", mutating=False)
        if isinstance(data, dict) and isinstance(data.get("events"), list):
            return list(data["events"])
        if isinstance(data, list):
            return data
        return []

    async def end_session(
        self,
        session_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        res = await self._request(
            "POST",
            f"/sessions/{session_id}/end",
            mutating=True,
            idempotency_key=idempotency_key,
        )
        return dict(res)

    # --- 3. Messages ---

    async def send_message(
        self,
        session_id: str,
        *,
        to: str,
        channel: str,
        body: str,
        sender: dict[str, str] | None = None,
        subject: str | None = None,
        attachments: list[str | dict[str, Any]] | None = None,
        sent_at: str | None = None,
        required_page_url: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if required_page_url is not None and not self.is_page_registered(
            session_id, required_page_url
        ):
            raise PageNotRegisteredError(
                f"Landing page {required_page_url!r} must be registered in session "
                f"{session_id!r} before sending a message linking to it."
            )

        payload: dict[str, Any] = {
            "to": to,
            "channel": channel,
            "body": body,
        }
        if sender:
            payload["from"] = sender
        if subject is not None:
            payload["subject"] = subject
        if attachments:
            payload["attachments"] = attachments
        if sent_at is not None:
            payload["sent_at"] = sent_at

        res = await self._request(
            "POST",
            f"/sessions/{session_id}/messages",
            json_body=payload,
            mutating=True,
            idempotency_key=idempotency_key,
        )
        return dict(res)

    # --- 4. Assets ---

    async def upload_asset(
        self,
        source: str | Path | bytes,
        *,
        filename: str | None = None,
        content_type: str | None = None,
        idempotency_key: str | None = None,
    ) -> AssetRecord:
        if isinstance(source, (str, Path)):
            path = Path(source)
            raw = path.read_bytes()
            resolved_name = filename or path.name
            local_path_str = str(path)
        else:
            raw = source
            resolved_name = filename or "asset.png"
            local_path_str = resolved_name

        if len(raw) > MAX_ASSET_BYTES:
            raise AssetLimitError(
                f"Asset {resolved_name!r} is {len(raw)} bytes, exceeding 5 MB limit"
            )

        guessed_type = content_type or mimetypes.guess_type(resolved_name)[0] or "image/png"
        if guessed_type == "image/jpg":
            guessed_type = "image/jpeg"
        if guessed_type not in SUPPORTED_ASSET_TYPES:
            raise AssetLimitError(
                f"Unsupported asset content type {guessed_type!r}; "
                f"supported: {sorted(SUPPORTED_ASSET_TYPES)}"
            )

        digest = hashlib.sha256(raw).hexdigest()
        if self.store is not None:
            existing = self.store.get_asset_by_sha256(digest)
            if existing is not None:
                self._uploaded_assets_by_sha[digest] = existing
                if self.asset_cache is not None:
                    self.asset_cache.put(existing)
                return existing
        else:
            cached = self._uploaded_assets_by_sha.get(digest)
            if cached is not None:
                return cached

        if self.asset_cache is not None:
            team_cached = self.asset_cache.get(digest)
            if team_cached is not None:
                self._uploaded_assets_by_sha[digest] = team_cached
                if self.store is not None:
                    try:
                        self.store.record_asset(
                            asset_id=team_cached.asset_id,
                            local_path=local_path_str or team_cached.local_path,
                            sha256=digest,
                            content_type=team_cached.content_type or guessed_type,
                            size_bytes=team_cached.size_bytes or len(raw),
                        )
                    except AssetCapError:
                        pass
                return team_cached

        if self.store is not None:
            if self.store.remaining_asset_budget() <= 0:
                raise AssetLimitError(
                    f"Asset cap reached ({self.store.max_assets} stored assets)"
                )
        elif len(self._uploaded_assets_by_sha) >= self.max_assets:
            raise AssetLimitError(f"Asset cap reached ({self.max_assets} stored assets)")

        res = await self._request(
            "POST",
            "/assets",
            files={"file": (resolved_name, raw, guessed_type)},
            mutating=True,
            idempotency_key=idempotency_key,
        )
        asset_id = str(res.get("asset_id") or res.get("id") or "")
        if not asset_id:
            raise TargetAPIError(500, f"Upload response missing asset_id: {res}")

        if self.store is not None:
            try:
                record = self.store.record_asset(
                    asset_id=asset_id,
                    local_path=local_path_str,
                    sha256=digest,
                    content_type=guessed_type,
                    size_bytes=len(raw),
                )
            except AssetCapError as exc:
                raise AssetLimitError(str(exc)) from exc
        else:
            record = AssetRecord(
                asset_id=asset_id,
                local_path=local_path_str,
                sha256=digest,
                content_type=guessed_type,
                size_bytes=len(raw),
                created_at="",
            )

        self._uploaded_assets_by_sha[digest] = record
        if self.asset_cache is not None:
            self.asset_cache.put(record)
        return record

    # --- 5. Landing Pages ---

    async def register_page(
        self,
        session_id: str,
        *,
        url: str,
        title: str,
        html: str | None = None,
        text: str | None = None,
        images: list[str] | None = None,
        fields: list[str],
        force: bool = False,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        del force
        payload: dict[str, Any] = {
            "url": url,
            "title": title,
            "fields": fields,
        }
        if html is not None:
            payload["html"] = html
        if text is not None:
            payload["text"] = text
        if images:
            payload["images"] = list(images)
        res = await self._request(
            "POST",
            f"/sessions/{session_id}/pages",
            json_body=payload,
            mutating=True,
            idempotency_key=idempotency_key,
        )
        page_id = str(res.get("page_id") or res.get("id") or f"page_{uuid4().hex[:8]}")
        self._record_registered_page(
            session_id=session_id,
            page_id=page_id,
            url=url,
            title=title,
            fields=fields,
        )
        return dict(res)

    # --- 6. Live Conversations ---

    async def start_conversation(
        self,
        session_id: str,
        *,
        to: str,
        channel: str,
        sender: dict[str, str] | None = None,
        sent_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "to": to,
            "channel": channel,
        }
        if sender:
            payload["from"] = sender
        if sent_at is not None:
            payload["sent_at"] = sent_at
        res = await self._request(
            "POST",
            f"/sessions/{session_id}/conversations",
            json_body=payload,
            mutating=True,
            idempotency_key=idempotency_key,
        )
        return dict(res)

    async def send_conversation_turn(
        self,
        conversation_id: str,
        say: str,
        *,
        attachments: list[str | dict[str, Any]] | None = None,
        sent_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if len(say) > MAX_TURN_CHARS:
            raise ValueError(
                f"Conversation turn 'say' length ({len(say)}) exceeds {MAX_TURN_CHARS} char limit"
            )
        payload: dict[str, Any] = {"say": say}
        if attachments:
            payload["attachments"] = attachments
        if sent_at is not None:
            payload["sent_at"] = sent_at
        res = await self._request(
            "POST",
            f"/conversations/{conversation_id}/turns",
            json_body=payload,
            mutating=True,
            idempotency_key=idempotency_key,
        )
        return dict(res)

    async def end_conversation(
        self,
        conversation_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        res = await self._request(
            "POST",
            f"/conversations/{conversation_id}/end",
            mutating=True,
            idempotency_key=idempotency_key,
        )
        return dict(res)
