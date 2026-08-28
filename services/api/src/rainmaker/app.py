"""The Rainmaker API.

Four surfaces, one process:

    /api/research     prospect enrichment (the browser agent)
    /api/sync         the CRDT relay: POST to append, WS to stream
    /api/deals        materialised pipeline views, derived from the op log
    /api/calls        the live call: WS for the conversation, GET for which engines are loaded

The console never talks to /api/deals to WRITE. Every mutation goes through the op log so the
disconnected path and the connected path are the same path — a write that only works when connected is
a write the design would have to special-case, and special cases are where
divergence lives.
"""

from __future__ import annotations

import os as _os

# BEFORE ANY MODEL LIBRARY IS IMPORTED. This process ends up with torch (the model and the
# lip-sync generator) and onnxruntime (the voice) in it, and on Windows both ship their own copy
# of the Intel OpenMP runtime. The second one to initialise aborts the process:
#
#     OMP: Error #15: Initializing libiomp5md.dll, but found libiomp5md.dll already initialized
#
# The documented workaround is to allow the duplicate. It has to be set before the first import
# rather than before the first use, which is why it sits above the imports instead of in the
# module that needs it.
_os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

import asyncio
import base64
import contextlib
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .agents.store import NADIA_AGENT, NADIA_TENANT, AgentStore, seed
from .calls.admission import Admission, visitor_id
from .calls.agenda import Agenda, Panel, Phase
from .calls.avatar import build_avatar
from .calls.avatar import describe as describe_avatar
from .calls.intake import IntakeError, parse_intake
from .calls.lipsync import LipSync, portrait_file
from .calls.pipeline import (
    TURN_BUDGET_MS,
    CallPipeline,
    Disclosure,
    Finished,
    Heard,
    LanguageModel,
    Spoke,
    TextToSpeech,
    Thought,
)
from .calls.providers import (
    ClientSpeechToText,
    build_language_model,
    build_voice,
    engines,
)
from .calls.session import CallSession, Prospect
from .mcp.client import ToolBroker
from .research import ResearchAgent, ResearchConfig, ResearchRequest, build_fetcher
from .sync.compaction import compact_workspace
from .sync.hub import SyncHub
from .sync.membership import AuthError, Members, authorise_ops
from .sync.oplog import OpLog
from .sync.replicas import ReplicaRegistry

log = logging.getLogger("rainmaker.api")

DATA_DIR = Path(os.environ.get("RAINMAKER_DATA", "data"))
DEFAULT_WORKSPACE = "demo"


class AppState:
    oplog: OpLog
    hub: SyncHub
    members: Members
    replicas: ReplicaRegistry
    agent: ResearchAgent
    llm: LanguageModel
    tts: TextToSpeech
    tools: ToolBroker
    avatar: Any
    lipsync: LipSync
    agents: AgentStore
    admission: Admission


state = AppState()


async def _warm_engines() -> None:
    """Load the model and the voice in the background, once, at startup.

    NOT ON THE FIRST TURN. Loading Qwen takes about four seconds and Kokoro about one and a
    half, and a lazily-loaded engine spends both of them on the first prospect of the day —
    the worst available place, and invisible in any average. Doing it here instead means the
    server answers `/api/calls/health` immediately and reports `ready: false` until the weights
    are in, which is what the console's engine badge shows.

    Off the event loop, because both loads are blocking and the console must be able to
    connect while they run.
    """
    for engine in (state.llm, state.tts, state.lipsync):
        load = getattr(engine, "load", None)
        if load is None or not getattr(engine, "available", False):
            continue
        try:
            await asyncio.to_thread(load)
        except Exception:  # noqa: BLE001 — a missing engine degrades, it does not crash
            log.exception("failed to load %s; falling back", getattr(engine, "name", engine))


#: How often the log is pruned. Hourly rather than on every append: compaction is a full scan of
#: a workspace, and doing it on the write path would put a table scan inside the latency of
#: dragging a card.
SWEEP_SECONDS = int(os.environ.get("RAINMAKER_COMPACT_EVERY", "3600"))


async def _sweep_log() -> None:
    """Prune ops every replica has already acknowledged.

    IN A THREAD, ALWAYS. `compact` is a blocking SQLite scan and delete; awaiting it on the event
    loop would stall every live socket in the process for the duration, which on a busy workspace
    is exactly the moment it must not.

    A failure here is not a failure of the product. The log growing is a cost, not a corruption,
    so this logs and goes back to sleep rather than taking the process down with it.
    """
    while True:
        await asyncio.sleep(SWEEP_SECONDS)
        try:
            report = await asyncio.to_thread(
                compact_workspace, state.oplog, state.replicas, DEFAULT_WORKSPACE
            )
            if report.dropped:
                log.info("compacted %s: %s", DEFAULT_WORKSPACE, report.to_dict())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a log that stays long is not an outage
            log.warning("compaction sweep failed: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    state.oplog = OpLog(DATA_DIR / "oplog.sqlite3")
    state.oplog.ensure_workspace(DEFAULT_WORKSPACE, "Demo workspace")
    state.hub = SyncHub(state.oplog)
    state.members = Members(DATA_DIR / "members.sqlite3")
    state.replicas = ReplicaRegistry(DATA_DIR / "replicas.sqlite3")

    state.llm = build_language_model(os.environ.get("RAINMAKER_BRAIN", "auto"))
    state.tts = build_voice(os.environ.get("RAINMAKER_VOICE", "auto"))
    state.lipsync = LipSync()
    state.agents = AgentStore()
    state.admission = Admission()
    # Tenant zero. Our own agent is a row loaded through the path a customer's agent takes, so
    # the demo cannot drift away from the product: if configuration breaks, our front page does.
    seed(state.agents)
    state.avatar = build_avatar(os.environ.get("RAINMAKER_AVATAR", "auto"))
    # The face reports whether it is lip-syncing, so it needs to know what is generating.
    if hasattr(state.avatar, "lipsync"):
        state.avatar.lipsync = state.lipsync
    warming = asyncio.create_task(_warm_engines())
    sweeping = asyncio.create_task(_sweep_log())

    # The tool servers are subprocesses and must be started and stopped from the SAME task —
    # `stdio_client` opens anyio cancel scopes that are task-bound. The lifespan is that task.
    state.tools = ToolBroker()
    await state.tools.start()

    key = os.environ.get("FIRECRAWL_API_KEY", "").strip()
    backend = os.environ.get("RESEARCH_BACKEND", "auto")
    state.agent = ResearchAgent(
        build_fetcher(key or None, prefer=backend),
        ResearchConfig(
            cache_dir=DATA_DIR / "research-cache",
            our_category=tuple(
                s.strip()
                for s in os.environ.get("OUR_CATEGORY", "").split(",")
                if s.strip()
            ),
        ),
    )
    log.info(
        "rainmaker api up | research=%s brain=%s voice=%s",
        state.agent.pool.fetcher.name,
        getattr(state.llm, "name", "?"),
        getattr(state.tts, "name", "?"),
    )
    try:
        yield
    finally:
        warming.cancel()
        sweeping.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await warming
        with contextlib.suppress(asyncio.CancelledError):
            await sweeping
        await state.tools.close()
        state.agents.close()
        await state.agent.close()
        state.oplog.close()
        state.replicas.close()
        state.members.close()


class TokenRequest(BaseModel):
    """Ask for a token binding this actor to this workspace."""

    workspace: str = DEFAULT_WORKSPACE
    actor: str = ""


class AppendRequest(BaseModel):
    """The body of a reconnect flush.

    AT MODULE SCOPE, AND IT HAS TO BE. This was declared inside `create_app`, and with
    `from __future__ import annotations` at the top of this file every annotation is a string that
    FastAPI resolves against the MODULE's globals. A class defined in a function body is not in
    them, so the lookup failed, `req` fell back to being treated as a query parameter, and every
    POST to /api/sync/append answered:

        422  {"detail":[{"type":"missing","loc":["query","req"],"msg":"Field required"}]}

    The endpoint could not accept a body at all.

    WHY NOBODY NOTICED. The console reaches for this path only when the WebSocket is unavailable
    -- see apps/console/src/lib/store.ts, "it works in situations the WebSocket does not (some
    corporate proxies)" -- and its failure handling is deliberately silent: ops stay in the outbox
    and retry, because from the user's point of view the write already succeeded locally. So on
    exactly the networks this fallback exists to serve, nothing ever synced and no one was told.

    `/api/research` was never affected: `ResearchRequest` is imported at module scope, so its
    annotation always resolved.
    """

    workspace: str = DEFAULT_WORKSPACE
    ops: list[dict[str, Any]] = Field(default_factory=list)
    client_id: str | None = None
    #: The same signed token the socket presents. Carried in the body rather than a header
    #: because this path exists for proxies that mangle WebSockets, and those are the proxies
    #: most likely to strip an Authorization header too.
    token: str = ""


def create_app() -> FastAPI:
    app = FastAPI(
        title="Rainmaker",
        version="0.1.0",
        description="AI sales agent platform with a conflict-free rep console.",
        lifespan=lifespan,
    )
    # The console is served from a different origin in development (Vite on :5173).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=os.environ.get("CORS_ORIGINS", "http://localhost:5173").split(","),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ─────────────────────────────────────────────────────────── health
    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "research_backend": state.agent.pool.fetcher.name,
            "workspace": DEFAULT_WORKSPACE,
            **state.oplog.stats(DEFAULT_WORKSPACE),
            "live_clients": state.hub.live_count(DEFAULT_WORKSPACE),
            # WHICH REPLICA IS HOLDING THE WATERMARK, BY NAME. "The log is not shrinking" is
            # otherwise a mystery with a dozen causes; it is nearly always one replica that has
            # not been seen, and this says which.
            "compaction": state.replicas.describe(DEFAULT_WORKSPACE),
        }

    # ─────────────────────────────────────────────────────────── research
    @app.post("/api/research")
    async def research(req: ResearchRequest) -> JSONResponse:
        enrichment = await state.agent.research(req)
        payload = enrichment.model_dump(mode="json")
        # `score` and `field_count` are computed properties; the console needs both and
        # recomputing them client-side would let the two drift.
        payload["score"] = enrichment.score
        payload["field_count"] = enrichment.field_count()
        return JSONResponse(payload)

    # ─────────────────────────────────────────────────────────── sync
    @app.post("/api/sync/token")
    async def sync_token(req: TokenRequest) -> dict[str, Any]:
        """Enrol an actor in a workspace and return a signed token for the pair.

        OPEN ENROLMENT IS A POLICY, AND IT IS THIS DEPLOYMENT'S. There is no sign-in screen, so
        anyone who asks is enrolled — and that is the ONE thing to change for a real
        deployment: put an identity provider in front of this handler and let it decide who
        gets a grant. Everything downstream of the token is already the real enforcement, and
        it is the half that is expensive to retrofit.

        What this is NOT is "no auth". A token names one actor in one workspace, is signed, is
        checked against the membership table on every use, and is revocable — see
        `sync/membership.py`.
        """
        actor = req.actor.strip()
        if not actor:
            raise HTTPException(422, "actor is required")
        try:
            token = state.members.issue(req.workspace, actor)
        except AuthError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"token": token, "workspace": req.workspace, "actor": actor}

    @app.get("/api/sync/members")
    async def sync_members(workspace: str = DEFAULT_WORKSPACE) -> dict[str, Any]:
        """Who may write here. Read-only, and it lists actors rather than people."""
        return {"workspace": workspace, "members": state.members.members(workspace)}

    @app.delete("/api/sync/members/{actor}")
    async def revoke_member(actor: str, workspace: str = DEFAULT_WORKSPACE) -> dict[str, Any]:
        """Revoke a membership.

        TAKES EFFECT ON THE NEXT USE, NOT ON TOKEN EXPIRY, because `verify` reads the table
        every time rather than trusting the signature alone. An open socket keeps streaming
        until it reconnects — closing live sockets on revoke is the obvious next step and is
        not wired up.
        """
        # AND STOP HOLDING EVERYONE ELSE'S LOG. A departed replica that stays in the registry
        # pins the watermark at whatever it last acknowledged, for the whole eviction horizon —
        # so revoking a member would quietly stop compaction for a fortnight.
        state.replicas.forget(workspace, actor)
        return {"revoked": state.members.remove(workspace, actor)}

    @app.post("/api/sync/append")
    async def append(req: AppendRequest) -> dict[str, Any]:
        """The reconnect flush path.

        A console that lost the server posts its whole queue here on reconnect. Deduplication is
        the op log's job, so a client that is unsure whether an earlier flush landed should
        simply send it again -- which is exactly what it does.
        """
        # CHECKED BEFORE THE EMPTY-BODY SHORTCUT. An unauthenticated caller posting no ops
        # used to learn the workspace's head, which is a small leak and an entirely free one
        # to close.
        try:
            grant = state.members.verify(req.token, req.workspace)
            authorise_ops(req.ops, grant)
        except AuthError as exc:
            raise HTTPException(401, str(exc)) from exc

        # Posting proves this replica still exists, which is what keeps its unseen ops out of
        # the compactor. It proves nothing about what it has applied.
        state.replicas.touch(req.workspace, grant.actor)

        if not req.ops:
            return {"stored": 0, "head": state.oplog.head(req.workspace)}
        try:
            stored = await state.hub.publish(req.workspace, req.ops, origin=req.client_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {
            "stored": len(stored),
            "head": state.oplog.head(req.workspace),
            "duplicates": len(req.ops) - len(stored),
        }

    @app.get("/api/sync/since")
    async def since(workspace: str = DEFAULT_WORKSPACE, seq: int = 0) -> dict[str, Any]:
        return await state.hub.catch_up(workspace, seq)

    @app.websocket("/api/sync/ws")
    async def sync_ws(ws: WebSocket) -> None:
        await ws.accept()
        workspace = ws.query_params.get("workspace", DEFAULT_WORKSPACE)
        since_seq = int(ws.query_params.get("since", "0") or 0)
        sub_id = uuid.uuid4().hex

        # THE ACTOR COMES FROM THE TOKEN, NOT FROM THE QUERY STRING. It used to come from
        # `?actor=`, which is not a claim about who you are — it is a claim you would like to
        # make. Everything downstream attributes ops to this value, and the CRDT breaks
        # concurrent-edit ties with it.
        try:
            grant = state.members.verify(ws.query_params.get("token", ""), workspace)
        except AuthError as exc:
            # 1008 is "policy violation", which is the WebSocket close code that means this.
            # Sent rather than simply dropping, so the console can tell "you are not allowed"
            # apart from "the network went away" and stop retrying a socket that will never
            # open.
            await ws.send_json({"type": "unauthorised", "detail": str(exc)})
            await ws.close(code=1008)
            return
        actor = grant.actor

        # THIS IS THE ACKNOWLEDGEMENT COMPACTION RUNS ON. `since` is what this replica has
        # already applied, it arrives on every connect, and it is now attributable to a verified
        # actor — which is what makes it safe to prune anything below it. An unauthenticated
        # `since` would let anyone advance the watermark past ops a real replica had not seen,
        # and pruning those is the one way this system can lose data.
        state.replicas.ack(workspace, actor, since_seq)

        sub = await state.hub.subscribe(workspace, actor, sub_id)
        try:
            # Catch up BEFORE streaming live. Ordering matters: a live op that arrives during
            # catch-up is queued behind it, so the client sees a single ordered stream and
            # never has to reconcile "did I already get this".
            await ws.send_json(await state.hub.catch_up(workspace, since_seq))

            async def pump() -> None:
                while True:
                    await ws.send_json(await sub.queue.get())

            pump_task = asyncio.create_task(pump())
            try:
                while True:
                    message = await ws.receive_json()
                    if message.get("type") == "ops":
                        ops = message.get("ops") or []
                        try:
                            authorise_ops(ops, grant)
                        except AuthError as exc:
                            # The socket is authenticated; this message is not entitled. Reject
                            # the message and keep the connection, because the honest cause is a
                            # client bug rather than an attack, and dropping the socket would
                            # lose every op still queued behind it.
                            await ws.send_json({"type": "rejected", "detail": str(exc)})
                            continue
                        await state.hub.publish(workspace, ops, origin=sub_id)
                    elif message.get("type") == "ping":
                        # PRESENT, BUT NOT NECESSARILY CAUGHT UP. A heartbeat keeps a quiet
                        # console from being evicted and its unseen ops pruned; it says nothing
                        # about what that console has applied, so it must not move the ack.
                        state.replicas.touch(workspace, actor)
                        await ws.send_json({"type": "pong"})
                    elif message.get("type") == "ack":
                        # An explicit, finer-grained acknowledgement for clients that apply ops
                        # long after they connect. Optional: `since` already covers reconnects.
                        with contextlib.suppress(TypeError, ValueError):
                            state.replicas.ack(workspace, actor, int(message.get("seq", 0)))
            finally:
                pump_task.cancel()
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001
            log.warning("sync ws error for %s: %s", actor, exc)
        finally:
            await state.hub.unsubscribe(workspace, sub_id)

    @app.get("/api/sync/presence")
    async def presence(workspace: str = DEFAULT_WORKSPACE) -> dict[str, Any]:
        return {
            "workspace": workspace,
            "actors": state.hub.presence(workspace),
            "live": state.hub.live_count(workspace),
        }

    # ─────────────────────────────────────────────────────────── the live call
    @app.get("/api/calls/health")
    async def call_health() -> dict[str, Any]:
        """Which engines are actually loaded.

        The console shows this rather than assuming. A demo that has quietly fallen back to the
        browser voice and says nothing about it is how a reviewer concludes the product sounds
        like that.
        """
        return {
            "engines": engines(state.llm, state.tts),
            "avatar": {**describe_avatar(state.avatar), "lipsync": state.lipsync.describe()},
            "tools": state.tools.describe(),
            "agents": state.agents.list_agents(),
            "admission": state.admission.describe(),
            "disclosure": Disclosure().spoken,
            "budget_ms": TURN_BUDGET_MS,
        }

    @app.websocket("/api/calls/ws")
    async def call_ws(ws: WebSocket) -> None:
        """One call per socket.

        THE CALL STARTS WITH A FORM, not a button. `intake` is the first message and carries a
        name, a work address and a company: the domain is everything Nadia knows before she
        speaks, so research runs on it and the prospect watches that happen. `start` remains for
        a call with no intake — the plain conversation, no research, no agenda.

        In:  `intake`, `start`, `say`, `pick_slot`, `stop`, `ping`.
        Out: `disclosure`, `heard`, `token`, `clip`, `done`, `panel`, `phase`, `interrupted`.

        Audio goes out as base64 WAV inside the JSON rather than on a binary frame — a second
        channel would need its own ordering and its own reconnect logic to keep clips in
        sequence with the captions they belong to, and this stream is a few hundred kilobytes a
        minute. The browser frames on `panel` are JPEG for the same reason.

        `say` carries the prospect's words whether they typed them or spoke them, plus the two
        latency stages only the client can measure. See `LatencyBudget.adopt`.
        """
        await ws.accept()

        # WHICH AGENT IS ANSWERING. The key is the one in the script tag on a customer's own
        # website: public by construction, and it authorises nothing — it names an agent, it
        # does not grant the ability to edit one. An unknown key, an unpublished agent and a
        # typo all resolve to nothing on purpose, because telling a stranger which of the three
        # it was is an enumeration oracle.
        spec = state.agents.resolve(ws.query_params.get("key", "")) or state.agents.live(
            NADIA_TENANT, NADIA_AGENT
        )
        if spec is None:
            await ws.send_json({"type": "no_agent", "detail": "no published agent for that key"})
            await ws.close()
            return

        # WHO IS CALLING, AND MAY THEY. On an embed this socket is open to the public web, so
        # the decision happens before anything expensive: no model, no synthesiser, no browser.
        # A refusal is a sentence, because the person reading it is standing on a customer's
        # website rather than looking at a status code.
        agent_key = spec.public_key or f"{spec.tenant}/{spec.agent_id}"
        caller = visitor_id(
            ws.client.host if ws.client else None, ws.headers.get("x-forwarded-for")
        )
        verdict = state.admission.may_start(agent_key, caller)
        if not verdict.allowed:
            log.info("refused a call to %s: %s", agent_key, verdict.reason)
            await ws.send_json(
                {"type": "refused", "reason": verdict.reason, "spoken": verdict.spoken}
            )
            await ws.close()
            return
        state.admission.started(agent_key, caller)
        started_at = time.monotonic()
        turns_taken = 0

        # The tenant's chosen voice, on the shared synthesiser. Set per call rather than per
        # process: two tenants' agents must be able to sound different on the same box.
        if hasattr(state.tts, "voice"):
            state.tts.voice = spec.voice

        stt = ClientSpeechToText()
        pipeline = CallPipeline(
            stt=stt,
            llm=state.llm,
            tts=state.tts,
            avatar=state.avatar,
            disclosure=Disclosure(spoken=spec.guardrails.disclosure),
            max_sentences=spec.guardrails.max_sentences,
            may_speak_prices=spec.guardrails.speak_prices,
        )
        session = CallSession(
            pipeline,
            stt,
            spec=spec,
            prospect=Prospect(
                company=ws.query_params.get("company", ""),
                domain=ws.query_params.get("domain", ""),
            ),
        )
        agenda: Agenda | None = None
        mouths: set[asyncio.Task] = set()

        async def send_mouth(index: int, wav: bytes) -> None:
            """Generate and send the mouth frames for a clip that has already been sent.

            AUDIO IS NEVER DELAYED BY VIDEO. Generation is fast — seventeen times realtime once
            warm — but it is not free, and putting it on the path between synthesis and the
            socket would add its cost to the one number this project is about. So the clip goes
            out the instant the audio exists, this runs after it, and the console syncs the
            frames to the audio's already-scheduled start time, dropping any that are late.
            Worst case the mouth joins a beat into the first clause; the voice never waits.
            """
            try:
                # THE AGENT'S OWN FACE. A dental practice's receptionist must not be lip-synced
                # onto our account executive's portrait, which is what happened the first time
                # this ran on a second tenant: Alex answered in Nadia's face.
                frames = await asyncio.to_thread(
                    state.lipsync.frames_for_wav, wav, portrait_file(spec.portrait)
                )
                if not frames:
                    return
                encoded = await asyncio.to_thread(LipSync.encode, frames)
                await ws.send_json(
                    {"type": "mouth", "index": index, "fps": 25, "frames": encoded}
                )
            except Exception as exc:  # noqa: BLE001 — a missing mouth is not a broken call
                log.debug("no mouth frames for clip %d: %s", index, exc)

        async def send_events(events: AsyncIterator[Any]) -> None:
            async for event in events:
                if isinstance(event, Heard):
                    await ws.send_json(
                        {"type": "heard", "text": event.text, "final": event.final}
                    )
                elif isinstance(event, Thought):
                    await ws.send_json({"type": "token", "text": event.token})
                elif isinstance(event, Spoke):
                    await ws.send_json(
                        {
                            "type": "clip",
                            "index": event.clip.index,
                            "text": event.clip.text,
                            # What to READ ALOUD, when that differs from what to show. Only the
                            # browser-voice path uses it; with a local voice the audio is
                            # already synthesised from it.
                            "spoken": event.clip.to_say,
                            "duration_ms": round(event.clip.duration_ms, 1),
                            "generate_ms": round(event.clip.generate_ms, 1),
                            # Empty when there is no local voice; the client then speaks the
                            # text itself rather than playing silence.
                            "wav": base64.b64encode(event.clip.wav).decode()
                            if event.clip.wav
                            else "",
                            "browser_voice": event.clip.browser_voice,
                        }
                    )
                    if state.lipsync.ready and event.clip.wav:
                        task = asyncio.create_task(
                            send_mouth(event.clip.index, event.clip.wav)
                        )
                        mouths.add(task)
                        task.add_done_callback(mouths.discard)
                elif isinstance(event, Panel):
                    await ws.send_json({"type": "panel", "panel": event.kind, **event.data})
                elif isinstance(event, Phase):
                    await ws.send_json(
                        {"type": "phase", "step": str(event.step), "detail": event.detail}
                    )
                elif isinstance(event, Finished):
                    await ws.send_json(
                        {
                            "type": "done",
                            "response": event.result.response.strip(),
                            "budget": event.result.budget.report(),
                            "handoff": event.result.handoff_requested,
                        }
                    )

        turn: asyncio.Task | None = None
        try:
            while True:
                message = await ws.receive_json()
                kind = message.get("type")

                if kind in ("intake", "email"):
                    # THE FRONT DOOR. Everything Nadia knows before she speaks comes from these
                    # three fields, so a bad one is answered with a sentence and the field it
                    # belongs to rather than a validation code — someone is watching a form
                    # with her face next to it.
                    try:
                        contact = parse_intake(
                            message.get("name", ""),
                            message.get("email", ""),
                            message.get("company", ""),
                            ask_company=spec.intake.ask_company if spec else True,
                            require_work_email=(
                                spec.intake.require_work_email if spec else True
                            ),
                        )
                    except IntakeError as exc:
                        await ws.send_json(
                            {"type": "intake_error", "spoken": exc.spoken, "field": exc.field}
                        )
                        continue

                    agenda = Agenda(session, state.tools, contact)
                    await ws.send_json(
                        {
                            "type": "disclosure",
                            "text": session.pipeline.disclosure.spoken,
                            "engines": engines(state.llm, state.tts),
                            "avatar": describe_avatar(state.avatar),
                            "agent": {
                                "name": spec.name,
                                "company": spec.company,
                                "portrait": spec.portrait,
                                "version": spec.version,
                            },
                            "contact": {
                                "email": contact.email,
                                "domain": contact.domain,
                                "first_name": contact.first_name,
                                "company": contact.company_guess,
                                "researchable": contact.researchable,
                            },
                        }
                    )
                    turn = asyncio.create_task(send_events(agenda.begin()))

                elif kind == "start":
                    # The plain conversation: no intake, no research, no agenda. Kept because
                    # it is the smallest thing that exercises the whole voice path, which is
                    # what the tests and the screenshot script use.
                    await ws.send_json(
                        {
                            "type": "disclosure",
                            "text": session.pipeline.disclosure.spoken,
                            "engines": engines(state.llm, state.tts),
                            "avatar": describe_avatar(state.avatar),
                            "agent": {
                                "name": spec.name,
                                "company": spec.company,
                                "portrait": spec.portrait,
                                "version": spec.version,
                            },
                        }
                    )
                    turn = asyncio.create_task(send_events(session.open()))

                elif kind == "pick_slot":
                    if agenda is None:
                        continue
                    if turn and not turn.done():
                        turn.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await turn
                    turn = asyncio.create_task(
                        send_events(agenda.confirm_slot(int(message.get("index", 0))))
                    )

                elif kind == "say":
                    # A call that has gone eighty turns or twenty minutes is a loop or a tab
                    # somebody walked away from. It is ended with a sentence, not dropped.
                    turns_taken += 1
                    ongoing = state.admission.check_ongoing(started_at, turns_taken)
                    if not ongoing.allowed:
                        await ws.send_json(
                            {"type": "refused", "reason": ongoing.reason,
                             "spoken": ongoing.spoken}
                        )
                        break

                    # THE DISCLOSURE IS NOT INTERRUPTIBLE. Cancelling the opening turn leaves it
                    # undelivered, and the pipeline then refuses every turn for the rest of the
                    # call — see `Agenda.defer`. Held and replayed instead.
                    if agenda is not None and agenda.defer(message.get("text", "")):
                        await ws.send_json({"type": "deferred"})
                        continue

                    # BARGE-IN. A prospect who starts talking over the agent has stopped
                    # listening, and an agent that finishes its sentence anyway is the single
                    # most robotic thing it can do. Cancelling here also frees the GPU for the
                    # turn they actually want answered.
                    if turn and not turn.done():
                        turn.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await turn
                        await ws.send_json({"type": "interrupted"})
                    # THE CLIENT OWNS THESE TWO NUMBERS. `stt_ms` is the browser recogniser's
                    # own cost — endpointing and transcription together, which it does not
                    # separate — and it is absent when the prospect typed, because then nothing
                    # was transcribed. `avatar_ms` is audio-in-hand to first frame drawn.
                    hints = {
                        stage: float(message[key])
                        for stage, key in (("stt", "stt_ms"), ("avatar", "avatar_ms"))
                        if isinstance(message.get(key), int | float)
                    }
                    said = message.get("text", "")
                    # With an agenda the turn is a step in a plan; without one it is a reply.
                    # Both end up in `session.respond` — the agenda decides what happens around
                    # it, not instead of it.
                    turn = asyncio.create_task(
                        send_events(
                            agenda.respond(said, budget_hints=hints)
                            if agenda is not None
                            else session.respond(said, budget_hints=hints)
                        )
                    )

                elif kind == "stop":
                    if turn and not turn.done():
                        turn.cancel()
                    await ws.send_json({"type": "stopped"})

                elif kind == "ping":
                    await ws.send_json({"type": "pong"})
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001
            log.warning("call ws error: %s", exc)
        finally:
            if turn and not turn.done():
                turn.cancel()
            # The socket is going; frames for it are worth nothing.
            for task in list(mouths):
                task.cancel()

            # THE CALL STILL HAPPENED. Most calls end because somebody closed the tab, not
            # because the agent reached the last step of the plan, and a pipeline that only
            # records the tidy ones is a pipeline nobody trusts. Nothing is sent — the socket
            # is gone — but the CRM write and the follow-up draft still run.
            if agenda is not None:
                try:
                    async for _ in agenda.end():
                        pass
                except Exception as exc:  # noqa: BLE001
                    log.warning("could not close out the call: %s", exc)
            # ALWAYS, including when the socket died badly. A leaked live-count is worse than
            # any limit it enforces: the agent stops answering and nothing says why.
            state.admission.finished(agent_key)

    # ─────────────────────────────────────────────────────────── the front door
    @app.get("/api/agents")
    async def agents() -> dict[str, Any]:
        """Every published agent, for the console's picker.

        The console belongs to the platform, so it can see its tenants. This is not the endpoint
        a stranger on a customer's website reaches — that one is `front-door`, takes a key, and
        never lists anything.
        """
        # A CUSTOMER'S AGENT FIRST, OURS LAST. The console opens on whichever comes back first,
        # and Rainmaker's own agent is the weakest demo of the set: it sells the thing you are
        # already looking at, and its tour opens this console inside this console. A customer
        # selling GPU-hours has a rate card, a competitor and a site to walk you round.
        rows = sorted(
            state.agents.list_agents(),
            key=lambda row: (row["tenant"] == NADIA_TENANT, row["tenant"]),
        )
        listed = []
        for row in rows:
            spec = state.agents.live(row["tenant"], row["agent_id"])
            if spec is None:
                continue
            listed.append(
                {
                    "key": row["public_key"],
                    "name": spec.name,
                    "company": spec.company,
                    "portrait": spec.portrait,
                    "sells": next(
                        (t.unit_name for t in spec.pricing if t.unit_amount), ""
                    ),
                    # WHICH AGENT MAKES THE BEST DEMO, decided by what is configured rather than
                    # by which row came back first. One with a tour, a comparison and a price
                    # can run all ten steps; one without will fall through to the model at three
                    # of them, which is the version somebody clicks first and judges by.
                    "complete": bool(
                        spec.tour
                        and spec.competitors
                        and any(tier.unit_amount for tier in spec.pricing)
                    ),
                    "version": spec.version,
                }
            )
        return {"agents": listed}

    @app.get("/api/agents/front-door")
    async def front_door(key: str = "") -> dict[str, Any]:
        """Who is answering, and what their form asks for — before the socket is opened.

        THE WIDGET CANNOT DRAW A FORM IT HAS NOT BEEN TOLD ABOUT. Everything else about an
        agent arrives on the socket after the call starts, which is fine for a disclosure and
        useless for the fields somebody has to fill in to start one.

        PUBLIC, AND CAREFUL ABOUT IT. It returns what the page would show anyway — a name, a
        face, the disclosure — and never knowledge, pricing or tools. An unknown key and an
        unpublished agent get the same answer as tenant zero rather than a 404, because
        distinguishing them for a stranger is an enumeration oracle.
        """
        spec = state.agents.resolve(key) if key else state.agents.live(NADIA_TENANT, NADIA_AGENT)
        if spec is None:
            spec = state.agents.live(NADIA_TENANT, NADIA_AGENT)
        if spec is None:  # pragma: no cover - the seed runs at startup
            return {"name": "", "company": "", "fields": ["name", "email"]}
        return {
            "name": spec.name,
            "company": spec.company,
            "portrait": spec.portrait,
            "disclosure": spec.guardrails.disclosure,
            **spec.intake.as_dict(),
        }

    # ─────────────────────────────────────────────────────────── the diary
    @app.get("/api/calendar")
    async def calendar(include_cancelled: bool = False) -> dict[str, Any]:
        """What the agent has actually booked.

        THE MEETINGS EXISTED AND NOTHING SHOWED THEM. `list_bookings` has been on the calendar
        server since it was written, the agent has been filling it in, and the only way to read
        it back was to open a SQLite file — so the honest reaction to booking a meeting was
        "where is the calendar?". A tool nobody can see the output of is indistinguishable from
        a tool that did nothing.

        Through the tool, like the checkout lookup, so swapping the local calendar for a
        customer's Google Calendar changes nothing above this line.
        """
        try:
            found = await state.tools.call(
                "calendar.list_bookings", {"include_cancelled": include_cancelled}
            )
        except Exception as exc:  # noqa: BLE001
            log.info("could not read the calendar: %s", exc)
            return {"bookings": [], "count": 0, "unavailable": True}
        return found

    @app.post("/api/calendar/{booking_id}/cancel")
    async def cancel_booking(booking_id: str) -> dict[str, Any]:
        """Cancel a meeting the agent booked. The slot goes back on offer."""
        try:
            return await state.tools.call("calendar.cancel_meeting", {"booking_id": booking_id})
        except Exception as exc:  # noqa: BLE001
            return {"cancelled": False, "reason": str(exc)}

    # ─────────────────────────────────────────────────────────── checkouts
    @app.get("/api/checkouts/{checkout_id}")
    async def checkout(checkout_id: str) -> dict[str, Any]:
        """What the mock checkout page renders.

        THROUGH THE TOOL, NOT AROUND IT. The payments server owns that database, and a second
        reader with its own SQL is a second thing to keep correct. It is also the only way this
        stays honest when the server is swapped for a hosted one over stdio.
        """
        try:
            return await state.tools.call("payments.checkout_status", {"checkout_id": checkout_id})
        except Exception as exc:  # noqa: BLE001
            log.info("checkout lookup failed: %s", exc)
            return {"found": False, "checkout_id": checkout_id}

    @app.post("/api/checkouts/{checkout_id}/pay")
    async def pay_checkout(checkout_id: str) -> dict[str, Any]:
        """Complete a MOCK checkout.

        The tool refuses outright when a real provider is configured — there, the processor's
        webhook is the only thing allowed to say a checkout was paid, and an endpoint that can
        say it instead is an endpoint that grants subscriptions nobody paid for.
        """
        try:
            return await state.tools.call("payments.mark_paid", {"checkout_id": checkout_id})
        except Exception as exc:  # noqa: BLE001
            return {"paid": False, "reason": str(exc)}

    # ─────────────────────────────────────────────────────────── deals (read-only)
    @app.get("/api/deals")
    async def deals(workspace: str = DEFAULT_WORKSPACE) -> dict[str, Any]:
        """Server-side materialisation of the pipeline.

        READ ONLY, on purpose. The console renders from its own local replica; this exists for
        integrations, reporting, and server-side jobs that have no CRDT. Two materialisers is
        a real duplication risk, so this one replays the same op semantics and is covered by
        a test asserting it agrees with the TypeScript replica on the same log.
        """
        from .crm.materialise import materialise

        ops = [o.payload for o in state.oplog.since(workspace, 0, limit=100_000)]
        return {"workspace": workspace, "deals": materialise(ops)}

    return app


app = create_app()
