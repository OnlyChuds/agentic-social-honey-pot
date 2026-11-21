from __future__ import annotations

from dataclasses import dataclass, field
from email.parser import BytesParser
from email.policy import default as email_default_policy
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

from tests.mock_api.personas import PersonaKind, evaluate_persona_reaction

MAX_MOCK_ASSETS = 300
MAX_MOCK_ASSET_BYTES = 5 * 1024 * 1024
MAX_MOCK_TURN_CHARS = 4000
ALLOWED_ASSET_TYPES = {"image/png", "image/jpeg", "image/webp", "application/pdf"}


class CreateSessionReq(BaseModel):
    scenario_id: str


class SendMessageReq(BaseModel):
    to: str
    channel: str
    body: str
    subject: str | None = None
    attachments: list[Any] | None = None


class RegisterPageReq(BaseModel):
    url: str
    title: str
    html: str
    fields: list[str]


class StartConversationReq(BaseModel):
    to: str
    channel: str


class ConversationTurnReq(BaseModel):
    say: str


@dataclass
class MockSession:
    session_id: str
    scenario_id: str
    touch_budget: int
    touches_used: int = 0
    ended: bool = False
    pages: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class MockConversation:
    conversation_id: str
    session_id: str
    to: str
    channel: str
    ended: bool = False
    turns: list[str] = field(default_factory=list)


@dataclass
class MockTargetState:
    expected_token: str = "test-team-key"
    persona_by_target: dict[str, PersonaKind] = field(
        default_factory=lambda: {
            "julius": "eager",
            "tom": "cautious",
            "robert": "suspicious",
            "silent_target": "silent",
        }
    )
    default_persona: PersonaKind = "eager"
    practice_touch_budget: int = 30
    eval_touch_budget: int = 10
    eval_max_sessions: int = 3
    sessions: dict[str, MockSession] = field(default_factory=dict)
    conversations: dict[str, MockConversation] = field(default_factory=dict)
    assets: dict[str, dict[str, Any]] = field(default_factory=dict)
    idempotency_cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Fault injection queue: list of HTTP status codes popped on mutating message/session requests
    fault_queue: list[int] = field(default_factory=list)
    # Observability counters for tests
    received_idempotency_keys: list[str] = field(default_factory=list)

    def scenarios(self) -> list[dict[str, Any]]:
        return [
            {
                "id": "practice",
                "kind": "practice",
                "company": "revel8",
                "targets": [
                    {"id": "julius", "name": "Julius Muth", "role": "Co-Founder & Co-CEO (GTM)"},
                    {"id": "tom", "name": "Tom Müller", "role": "Co-Founder & Co-CEO (Product)"},
                    {"id": "robert", "name": "Robert Seilbeck", "role": "Co-Founder & CTO"},
                    {"id": "silent_target", "name": "Alex Quiet", "role": "VP Operations"},
                ],
                "objectives": [
                    {"id": "credentials", "text": "Submit credentials on registered page"},
                    {"id": "disclosure", "text": "Disclose non-public internal facts"},
                    {"id": "action", "text": "Open declared action artifact"},
                ],
                "touch_budget": self.practice_touch_budget,
                "max_sessions": None,
                "open": True,
            },
            {
                "id": "eval",
                "kind": "eval",
                "company": "EvalCorp",
                "targets": [
                    {"id": "eval_target", "name": "Eval Persona", "role": "Engineering Lead"},
                ],
                "objectives": [
                    {"id": "credentials", "text": "Submit credentials on registered page"},
                    {"id": "disclosure", "text": "Disclose non-public internal facts"},
                    {"id": "action", "text": "Open declared action artifact"},
                ],
                "touch_budget": self.eval_touch_budget,
                "max_sessions": self.eval_max_sessions,
                "open": True,
            },
        ]


def create_mock_app(state: MockTargetState | None = None) -> tuple[FastAPI, MockTargetState]:
    st = state or MockTargetState()
    app = FastAPI(title="Mock Target API (Offline Testbed)")

    def _check_auth(authorization: str | None) -> None:
        if not st.expected_token:
            return
        if authorization != f"Bearer {st.expected_token}":
            raise HTTPException(status_code=401, detail="Invalid bearer token")

    def _maybe_inject_fault() -> None:
        if st.fault_queue:
            code = st.fault_queue.pop(0)
            raise HTTPException(status_code=code, detail=f"Injected mock fault HTTP {code}")

    @app.get("/v1/scenarios")
    async def get_scenarios(authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        _check_auth(authorization)
        return st.scenarios()

    @app.post("/v1/sessions")
    async def create_session(
        req: CreateSessionReq,
        authorization: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        _check_auth(authorization)
        if idempotency_key:
            st.received_idempotency_keys.append(idempotency_key)
            if idempotency_key in st.idempotency_cache:
                return st.idempotency_cache[idempotency_key]

        if req.scenario_id == "eval":
            eval_count = sum(1 for s in st.sessions.values() if s.scenario_id == "eval")
            if eval_count >= st.eval_max_sessions:
                raise HTTPException(status_code=409, detail="Max eval sessions reached")
            budget = st.eval_touch_budget
        elif req.scenario_id == "practice":
            budget = st.practice_touch_budget
        else:
            raise HTTPException(status_code=400, detail=f"Unknown scenario_id: {req.scenario_id}")

        sid = f"sid_{uuid4().hex[:8]}"
        st.sessions[sid] = MockSession(session_id=sid, scenario_id=req.scenario_id, touch_budget=budget)
        out = {
            "session_id": sid,
            "scenario_id": req.scenario_id,
            "touch_budget": budget,
            "touches_used": 0,
            "ended": False,
        }
        if idempotency_key:
            st.idempotency_cache[idempotency_key] = out
        return out

    @app.get("/v1/sessions")
    async def list_sessions(authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        _check_auth(authorization)
        return [
            {
                "session_id": s.session_id,
                "scenario_id": s.scenario_id,
                "touch_budget": s.touch_budget,
                "touches_used": s.touches_used,
                "ended": s.ended,
            }
            for s in st.sessions.values()
        ]

    @app.get("/v1/sessions/{sid}")
    async def get_session(
        sid: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _check_auth(authorization)
        sess = st.sessions.get(sid)
        if sess is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return {
            "session_id": sess.session_id,
            "scenario_id": sess.scenario_id,
            "touch_budget": sess.touch_budget,
            "touches_used": sess.touches_used,
            "ended": sess.ended,
        }

    @app.get("/v1/sessions/{sid}/events")
    async def get_session_events(
        sid: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _check_auth(authorization)
        sess = st.sessions.get(sid)
        if sess is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return {"events": list(sess.events)}

    @app.post("/v1/sessions/{sid}/end")
    async def end_session(
        sid: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _check_auth(authorization)
        sess = st.sessions.get(sid)
        if sess is None:
            raise HTTPException(status_code=404, detail="Session not found")
        sess.ended = True
        return {"session_id": sid, "ended": True}

    @app.post("/v1/sessions/{sid}/pages")
    async def register_page(
        sid: str,
        req: RegisterPageReq,
        authorization: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        _check_auth(authorization)
        sess = st.sessions.get(sid)
        if sess is None:
            raise HTTPException(status_code=404, detail="Session not found")
        if idempotency_key and idempotency_key in st.idempotency_cache:
            return st.idempotency_cache[idempotency_key]

        page_id = f"page_{uuid4().hex[:8]}"
        page_rec = {
            "page_id": page_id,
            "url": req.url,
            "title": req.title,
            "fields": list(req.fields),
        }
        sess.pages.append(page_rec)
        if idempotency_key:
            st.idempotency_cache[idempotency_key] = page_rec
        return page_rec

    @app.post("/v1/sessions/{sid}/messages")
    async def send_message(
        sid: str,
        req: SendMessageReq,
        authorization: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        _check_auth(authorization)
        if idempotency_key:
            st.received_idempotency_keys.append(idempotency_key)
        _maybe_inject_fault()

        sess = st.sessions.get(sid)
        if sess is None:
            raise HTTPException(status_code=404, detail="Session not found")
        if idempotency_key and idempotency_key in st.idempotency_cache:
            return st.idempotency_cache[idempotency_key]
        if sess.ended:
            raise HTTPException(status_code=409, detail="Session already ended")
        if sess.touches_used >= sess.touch_budget:
            raise HTTPException(status_code=409, detail="Touch budget exhausted")

        sess.touches_used += 1
        sess.messages.append(req.model_dump())

        persona = st.persona_by_target.get(req.to, st.default_persona)
        events = evaluate_persona_reaction(
            persona=persona,
            channel=req.channel,
            body=req.body,
            touch_number=sess.touches_used,
            registered_pages=sess.pages,
            attachments=req.attachments or [],
        )
        sess.events.extend(events)

        out = {
            "message_id": f"msg_{uuid4().hex[:8]}",
            "touches_used": sess.touches_used,
            "touches_remaining": sess.touch_budget - sess.touches_used,
            "events": events,
        }
        if idempotency_key:
            st.idempotency_cache[idempotency_key] = out
        return out

    @app.post("/v1/assets")
    async def upload_asset(
        request: Request,
        authorization: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        _check_auth(authorization)
        if idempotency_key and idempotency_key in st.idempotency_cache:
            return st.idempotency_cache[idempotency_key]
        if len(st.assets) >= MAX_MOCK_ASSETS:
            raise HTTPException(status_code=409, detail="Asset storage limit reached")

        body_bytes = await request.body()
        req_ct = request.headers.get("content-type", "")
        filename = "asset"
        ctype = "application/octet-stream"
        raw = body_bytes

        if "multipart/form-data" in req_ct:
            mime_bytes = f"Content-Type: {req_ct}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body_bytes
            parsed = BytesParser(policy=email_default_policy).parsebytes(mime_bytes)
            for part in parsed.iter_parts():
                part_filename = part.get_filename()
                if part_filename or part.get_param("name", header="content-disposition") == "file":
                    filename = part_filename or filename
                    ctype = part.get_content_type() or ctype
                    payload = part.get_payload(decode=True)
                    raw = payload if isinstance(payload, bytes) else b""
                    break
        else:
            ctype = req_ct.split(";", 1)[0].strip() or ctype

        if len(raw) > MAX_MOCK_ASSET_BYTES:
            raise HTTPException(status_code=413, detail="Asset exceeds 5 MB")
        if ctype not in ALLOWED_ASSET_TYPES:
            raise HTTPException(status_code=415, detail=f"Unsupported content type: {ctype}")

        asset_id = f"a_{len(st.assets) + 1}"
        rec = {"asset_id": asset_id, "filename": filename, "size": len(raw)}
        st.assets[asset_id] = rec
        if idempotency_key:
            st.idempotency_cache[idempotency_key] = rec
        return rec

    @app.post("/v1/sessions/{sid}/conversations")
    async def start_conversation(
        sid: str,
        req: StartConversationReq,
        authorization: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        _check_auth(authorization)
        sess = st.sessions.get(sid)
        if sess is None:
            raise HTTPException(status_code=404, detail="Session not found")
        if idempotency_key and idempotency_key in st.idempotency_cache:
            return st.idempotency_cache[idempotency_key]
        if sess.ended or sess.touches_used >= sess.touch_budget:
            raise HTTPException(status_code=409, detail="Touch budget exhausted or session ended")

        sess.touches_used += 1
        cid = f"cid_{uuid4().hex[:8]}"
        st.conversations[cid] = MockConversation(
            conversation_id=cid,
            session_id=sid,
            to=req.to,
            channel=req.channel,
        )
        out = {"conversation_id": cid, "touches_used": sess.touches_used}
        if idempotency_key:
            st.idempotency_cache[idempotency_key] = out
        return out

    @app.post("/v1/conversations/{cid}/turns")
    async def send_turn(
        cid: str,
        req: ConversationTurnReq,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _check_auth(authorization)
        conv = st.conversations.get(cid)
        if conv is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        if conv.ended:
            raise HTTPException(status_code=409, detail="Conversation already ended")
        if len(req.say) > MAX_MOCK_TURN_CHARS:
            raise HTTPException(status_code=413, detail="Turn text exceeds 4000 chars")

        conv.turns.append(req.say)
        sess = st.sessions[conv.session_id]
        persona = st.persona_by_target.get(conv.to, st.default_persona)
        events = evaluate_persona_reaction(
            persona=persona,
            channel=conv.channel,
            body=req.say,
            touch_number=len(conv.turns),
            registered_pages=sess.pages,
            attachments=[],
        )
        sess.events.extend(events)
        return {"conversation_id": cid, "events": events}

    @app.post("/v1/conversations/{cid}/end")
    async def end_conversation(
        cid: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _check_auth(authorization)
        conv = st.conversations.get(cid)
        if conv is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        conv.ended = True
        return {"conversation_id": cid, "ended": True}

    return app, st
