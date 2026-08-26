"""The Rainmaker API.

Four surfaces, one process:

    /api/research     prospect enrichment (the browser agent)
    /api/sync         the CRDT relay: POST to append, WS to stream
    /api/deals        materialised pipeline views, derived from the op log
    /api/calls        the live call: WS for the conversation, GET for which engines are loaded

The console never talks to /api/deals to WRITE. Every mutation goes through the op log so the
offline path and the online path are the same path — a write that only works when connected is
a write the local-first design would have to special-case, and special cases are where
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
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .calls.agenda import Agenda, Panel, Phase
from .calls.avatar import build_avatar
from .calls.avatar import describe as describe_avatar
from .calls.intake import InvalidEmail, parse_contact
from .calls.lipsync import LipSync
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
from .calls.session import CallSession, Prospect, facts_from_enrichment
from .mcp.client import ToolBroker
from .research import ResearchAgent, ResearchConfig, ResearchRequest, build_fetcher
from .sync.hub import SyncHub
from .sync.oplog import OpLog

log = logging.getLogger("rainmaker.api")

DATA_DIR = Path(os.environ.get("RAINMAKER_DATA", "data"))
DEFAULT_WORKSPACE = "demo"


class AppState:
    oplog: OpLog
    hub: SyncHub
    agent: ResearchAgent
    llm: LanguageModel
    tts: TextToSpeech
    tools: ToolBroker
    avatar: Any
    lipsync: LipSync


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    state.oplog = OpLog(DATA_DIR / "oplog.sqlite3")
    state.oplog.ensure_workspace(DEFAULT_WORKSPACE, "Demo workspace")
    state.hub = SyncHub(state.oplog)

    state.llm = build_language_model(os.environ.get("RAINMAKER_BRAIN", "auto"))
    state.tts = build_voice(os.environ.get("RAINMAKER_VOICE", "auto"))
    state.lipsync = LipSync()
    state.avatar = build_avatar(os.environ.get("RAINMAKER_AVATAR", "auto"))
    # The face reports whether it is lip-syncing, so it needs to know what is generating.
    if hasattr(state.avatar, "lipsync"):
        state.avatar.lipsync = state.lipsync
    warming = asyncio.create_task(_warm_engines())

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
        with contextlib.suppress(asyncio.CancelledError):
            await warming
        await state.tools.close()
        await state.agent.close()
        state.oplog.close()


class AppendRequest(BaseModel):
    """The body of an offline flush.

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


def create_app() -> FastAPI:
    app = FastAPI(
        title="Rainmaker",
        version="0.1.0",
        description="AI sales agent platform with an offline-first rep console.",
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
    @app.post("/api/sync/append")
    async def append(req: AppendRequest) -> dict[str, Any]:
        """The offline flush path.

        A console that was offline posts its whole queue here on reconnect. Deduplication is
        the op log's job, so a client that is unsure whether an earlier flush landed should
        simply send it again -- which is exactly what it does.
        """
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
        actor = ws.query_params.get("actor", "anonymous")
        since_seq = int(ws.query_params.get("since", "0") or 0)
        sub_id = uuid.uuid4().hex

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
                        await state.hub.publish(
                            workspace, message.get("ops") or [], origin=sub_id
                        )
                    elif message.get("type") == "ping":
                        await ws.send_json({"type": "pong"})
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
            "disclosure": Disclosure().spoken,
            "budget_ms": TURN_BUDGET_MS,
        }

    @app.websocket("/api/calls/ws")
    async def call_ws(ws: WebSocket) -> None:
        """One call per socket.

        THE CALL STARTS WITH AN EMAIL ADDRESS, not a button. `email` is the first message: the
        domain in it is everything Liv knows before she speaks, so research runs on it and the
        prospect watches that happen. `start` remains for a call with no intake — the plain
        conversation, no research, no agenda.

        In:  `email`, `start`, `say`, `pick_slot`, `stop`, `ping`.
        Out: `disclosure`, `heard`, `token`, `clip`, `done`, `panel`, `phase`, `interrupted`.

        Audio goes out as base64 WAV inside the JSON rather than on a binary frame — a second
        channel would need its own ordering and its own reconnect logic to keep clips in
        sequence with the captions they belong to, and this stream is a few hundred kilobytes a
        minute. The browser frames on `panel` are JPEG for the same reason.

        `say` carries the prospect's words whether they typed them or spoke them, plus the two
        latency stages only the client can measure. See `LatencyBudget.adopt`.
        """
        await ws.accept()

        stt = ClientSpeechToText()
        pipeline = CallPipeline(stt=stt, llm=state.llm, tts=state.tts, avatar=state.avatar)
        session = CallSession(
            pipeline,
            stt,
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
                frames = await asyncio.to_thread(state.lipsync.frames_for_wav, wav)
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

                if kind == "brief":
                    # The console already has the research result on screen, so it sends that
                    # rather than making the agent re-fetch a site it just read.
                    #
                    # THE CLIENT THEREFORE DECIDES WHAT THE AGENT MAY CLAIM, which is acceptable
                    # here — one console, one operator — and would not be in a deployment where
                    # the prospect can reach the socket. There the brief is looked up server-side
                    # from the deal, and this message does not exist.
                    enrichment = message.get("enrichment") or {}
                    session.prospect = Prospect(
                        company=message.get("company") or session.prospect.company,
                        domain=message.get("domain") or session.prospect.domain,
                        facts=facts_from_enrichment(enrichment) if enrichment else [],
                    )
                    await ws.send_json(
                        {"type": "briefed", "facts": len(session.prospect.facts)}
                    )

                elif kind == "email":
                    # THE FRONT DOOR. Everything Liv knows before she speaks comes from the
                    # domain in this address, so a bad one is answered with a sentence rather
                    # than a validation code — someone is watching a form.
                    try:
                        contact = parse_contact(message.get("email", ""))
                    except InvalidEmail as exc:
                        await ws.send_json({"type": "intake_error", "spoken": exc.spoken})
                        continue

                    agenda = Agenda(session, state.tools, contact)
                    await ws.send_json(
                        {
                            "type": "disclosure",
                            "text": session.pipeline.disclosure.spoken,
                            "engines": engines(state.llm, state.tts),
                            "avatar": describe_avatar(state.avatar),
                            "contact": {
                                "email": contact.email,
                                "domain": contact.domain,
                                "first_name": contact.first_name,
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
