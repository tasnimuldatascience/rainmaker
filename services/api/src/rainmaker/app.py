"""The Rainmaker API.

Three surfaces, one process:

    /api/research     prospect enrichment (the browser agent)
    /api/sync         the CRDT relay: POST to append, WS to stream
    /api/deals        materialised pipeline views, derived from the op log
    /api/calls        call records, disclosure log, latency budgets

The console never talks to /api/deals to WRITE. Every mutation goes through the op log so the
offline path and the online path are the same path — a write that only works when connected is
a write the local-first design would have to special-case, and special cases are where
divergence lives.
"""

from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

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


state = AppState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    state.oplog = OpLog(DATA_DIR / "oplog.sqlite3")
    state.oplog.ensure_workspace(DEFAULT_WORKSPACE, "Demo workspace")
    state.hub = SyncHub(state.oplog)

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
    log.info("rainmaker api up | research backend=%s", state.agent.pool.fetcher.name)
    try:
        yield
    finally:
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

            import asyncio

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
