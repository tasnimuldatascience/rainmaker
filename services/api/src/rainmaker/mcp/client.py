"""The MCP client: everything Nadia can reach for, and the rules about how she reaches.

WHY MCP RATHER THAN FUNCTION CALLS. Booking a meeting could be a method on the call session. It
is a protocol instead because **the tools are where this product meets a customer's systems**,
and a customer runs their own calendar and their own CRM. With MCP, swapping the local calendar
for theirs is a line in `mcp.toml`; without it, every integration is a Rainmaker release. The
servers in `servers/` are the defaults, not the design.

THE SERVERS ARE SEPARATE PROCESSES, over stdio, and that is a reliability decision rather than
protocol tourism. A third-party MCP server that hangs, leaks or segfaults must not take a live
call down with it. Out of process means a timeout is enforceable and a crash is recoverable —
`ToolBroker.call` gives every call a deadline and reports the failure to the agenda, which says
something true to the prospect instead of going silent mid-sentence.

THE MODEL DOES NOT CHOOSE THE TOOLS. This is the part most likely to be built the other way, so
it is worth stating plainly: Qwen2.5-1.5B is not reliable at deciding when to call a calendar,
and a booking is a commitment to a person. The agenda in `calls/agenda.py` decides which tool
fires at which point in the conversation; the model is given the RESULT and writes the sentence.
A model that misreads a slot produces an awkward sentence. A model that invents a booking
produces a meeting nobody attends.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("rainmaker.mcp")

#: How long a tool call may take before the agenda is told it failed.
#:
#: PER SERVER, BECAUSE ONE NUMBER IS WRONG FOR BOTH KINDS OF TOOL. A calendar lookup is a SQLite
#: read and should never take a second; reading a prospect's website is half a dozen real page
#: loads over someone else's network. A flat twelve seconds killed the research call at 11.5s
#: — the tool had done its job and the client hung up on it, and the console showed "could not
#: read anthropic.com" for a site that had been read perfectly well.
#:
#: The long deadlines are only tolerable because the agenda says something before it waits: see
#: `Agenda._research`. A tool that takes twenty seconds in silence is a broken call whatever the
#: timeout says.
DEFAULT_TIMEOUT_SECONDS = 12.0
TIMEOUT_SECONDS: dict[str, float] = {
    "research": 60.0,   # real page loads on someone else's site
    "email": 25.0,      # an SMTP handshake to a server that may be far away
}

#: How long a server gets to start and answer `initialize`. A server that cannot start in this
#: long is not going to be useful mid-call.
STARTUP_TIMEOUT_SECONDS = 20.0

CONFIG_PATH = Path(os.environ.get("RAINMAKER_MCP_CONFIG", "mcp.toml"))


@dataclass(slots=True)
class ServerSpec:
    """One MCP server this deployment can reach."""

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    #: Shown in the console when the server is off, so "no email went out" has a reason
    #: attached rather than looking like a bug.
    disabled_reason: str = ""


@dataclass(slots=True)
class ToolSpec:
    """One tool, namespaced by the server that offers it."""

    server: str
    name: str
    description: str
    input_schema: dict[str, Any]

    @property
    def qualified(self) -> str:
        return f"{self.server}.{self.name}"


class ToolError(RuntimeError):
    """A tool call that failed. Carries a sentence the agent can actually say."""

    def __init__(self, tool: str, detail: str, *, spoken: str | None = None):
        super().__init__(f"{tool}: {detail}")
        self.tool = tool
        self.detail = detail
        #: What Nadia should say about it. A stack trace is not a sentence.
        self.spoken = spoken or "I could not get that done just now — let me have someone follow up."


def default_servers() -> list[ServerSpec]:
    """The servers that ship with the repository.

    All local, all free, none requiring an account, so a fresh clone has a working calendar and
    a working CRM. `mcp.toml` overrides this entirely when present.
    """
    python = sys.executable
    return [
        ServerSpec("calendar", python, ["-m", "rainmaker.mcp.servers.calendar"]),
        ServerSpec("crm", python, ["-m", "rainmaker.mcp.servers.crm"]),
        ServerSpec("research", python, ["-m", "rainmaker.mcp.servers.research"]),
        # ALWAYS ON, WHICH IT WAS NOT. The whole server used to be disabled unless SMTP was
        # configured, which contradicted its own design: `draft_recap` needs no mail server and
        # `send_recap` refuses without one. The effect was that composing the follow-up — the
        # part that is actually interesting, and the part a demo can show — had never once run.
        # Only sending needs an account, and only sending is gated.
        ServerSpec("email", python, ["-m", "rainmaker.mcp.servers.email"]),
        # Mock by default, real behind STRIPE_SECRET_KEY. Shipped on rather than gated for the
        # same reason as email: the interesting half - building a checkout from a computed quote
        # - needs no account, and only moving actual money does.
        ServerSpec("payments", python, ["-m", "rainmaker.mcp.servers.payments"]),
    ]


def load_servers(path: Path = CONFIG_PATH) -> list[ServerSpec]:
    """Read `mcp.toml` if it exists, otherwise ship the defaults.

    The format is the one every other MCP host uses, so a server someone already runs in Claude
    Desktop can be pasted in unchanged:

        [servers.calendar]
        command = "npx"
        args = ["-y", "@some/google-calendar-mcp"]
        env = { GOOGLE_CREDENTIALS = "..." }
    """
    if not path.exists():
        return default_servers()

    import tomllib

    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    specs: list[ServerSpec] = []
    for name, entry in (raw.get("servers") or {}).items():
        command = entry.get("command")
        if not command:
            log.warning("mcp.toml: server %r has no command; skipping", name)
            continue
        specs.append(
            ServerSpec(
                name=name,
                command=command,
                args=list(entry.get("args") or []),
                env={str(k): str(v) for k, v in (entry.get("env") or {}).items()},
                enabled=bool(entry.get("enabled", True)),
                disabled_reason=str(entry.get("disabled_reason", "")),
            )
        )
    return specs or default_servers()


class ToolBroker:
    """Connects to every configured server and routes calls to the right one.

    One instance per process, started at boot. Servers are long-lived: spawning a subprocess per
    tool call would put a Python interpreter's startup time inside a conversation, and the whole
    project is an argument about what belongs inside a conversation.
    """

    def __init__(self, specs: list[ServerSpec] | None = None):
        self.specs = specs if specs is not None else load_servers()
        self.sessions: dict[str, Any] = {}
        self.tools: dict[str, ToolSpec] = {}
        #: Why each unavailable server is unavailable. Surfaced in /api/calls/health.
        self.failures: dict[str, str] = {}
        self._stack: AsyncExitStack | None = None
        self._lock = asyncio.Lock()

    # ── lifecycle ───────────────────────────────────────────────────────
    async def start(self) -> None:
        """Spawn and initialise every enabled server. Never raises.

        A missing tool degrades the call; it does not prevent one. If the calendar is down Nadia
        can still research, demo and hand off — she just cannot book, and the agenda knows that
        before she promises anything.
        """
        if self._stack is not None:
            return
        self._stack = AsyncExitStack()

        for spec in self.specs:
            if not spec.enabled:
                self.failures[spec.name] = spec.disabled_reason or "disabled in config"
                continue
            if not _runnable(spec.command):
                self.failures[spec.name] = f"command not found: {spec.command}"
                log.warning("mcp: %s -> %s", spec.name, self.failures[spec.name])
                continue
            try:
                # `asyncio.timeout`, NOT `asyncio.wait_for`. `wait_for` runs the coroutine in a
                # NEW TASK, so the anyio cancel scopes that `stdio_client` opens are entered in
                # that task and later exited in this one, and anyio refuses:
                #     RuntimeError: Attempted to exit cancel scope in a different task
                # `asyncio.timeout` applies to the current task and keeps the affinity intact.
                async with asyncio.timeout(STARTUP_TIMEOUT_SECONDS):
                    await self._connect(spec)
            except TimeoutError:
                self.failures[spec.name] = f"did not start within {STARTUP_TIMEOUT_SECONDS:.0f}s"
                log.warning("mcp: %s timed out starting", spec.name)
            except Exception as exc:  # noqa: BLE001 — one bad server must not stop the rest
                self.failures[spec.name] = str(exc)[:200]
                log.warning("mcp: %s failed to start: %s", spec.name, exc)

        log.info(
            "mcp ready | %d tools from %s%s",
            len(self.tools),
            ", ".join(sorted(self.sessions)) or "nothing",
            f" | unavailable: {', '.join(sorted(self.failures))}" if self.failures else "",
        )

    async def _connect(self, spec: ServerSpec) -> None:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        assert self._stack is not None
        env = {**os.environ, **spec.env}
        params = StdioServerParameters(command=spec.command, args=spec.args, env=env)

        read, write = await self._stack.enter_async_context(stdio_client(params))
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()

        listed = await session.list_tools()
        self.sessions[spec.name] = session
        for tool in listed.tools:
            spec_ = ToolSpec(
                server=spec.name,
                name=tool.name,
                description=tool.description or "",
                input_schema=getattr(tool, "input_schema", None) or {},
            )
            self.tools[spec_.qualified] = spec_

    async def close(self) -> None:
        if self._stack is None:
            return
        stack, self._stack = self._stack, None
        try:
            await stack.aclose()
        except Exception:  # noqa: BLE001 — shutdown noise from a dead child is not interesting
            log.debug("mcp: error while closing servers", exc_info=True)
        self.sessions.clear()
        self.tools.clear()

    # ── calling ─────────────────────────────────────────────────────────
    def has(self, qualified: str) -> bool:
        return qualified in self.tools

    def timeout_for(self, server: str) -> float:
        return TIMEOUT_SECONDS.get(server, DEFAULT_TIMEOUT_SECONDS)

    async def call(
        self,
        qualified: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        """Call `server.tool` and return its structured result.

        Raises `ToolError` for everything — a missing server, a timeout, a tool that raised —
        because the caller is a conversation and every one of those has the same consequence:
        Nadia has to say something true and keep going.
        """
        spec = self.tools.get(qualified)
        if spec is None:
            raise ToolError(qualified, "no such tool", spoken=self._spoken_for_missing(qualified))
        session = self.sessions.get(spec.server)
        if session is None:
            raise ToolError(qualified, f"{spec.server} is not connected")

        deadline = timeout if timeout is not None else self.timeout_for(spec.server)
        try:
            # Serialised per broker. The servers are single SQLite writers and a live call makes
            # one tool call at a time anyway; contention here would be a bug elsewhere.
            async with self._lock, asyncio.timeout(deadline):
                result = await session.call_tool(spec.name, arguments or {})
        except TimeoutError as exc:
            raise ToolError(
                qualified,
                f"no answer within {deadline:.0f}s",
                spoken="That is taking longer than it should — let me have a colleague confirm it.",
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise ToolError(qualified, str(exc)[:300]) from exc

        if getattr(result, "is_error", False):
            detail = _text_of(result)
            raise ToolError(qualified, detail, spoken=_spoken_from_detail(detail))
        return _payload_of(result)

    def _spoken_for_missing(self, qualified: str) -> str:
        server = qualified.split(".", 1)[0]
        if server in self.failures:
            return "I can't reach my calendar right now — I'll have someone send you times."
        return "I don't have a way to do that on this call, but I can have someone follow up."

    def describe(self) -> dict[str, Any]:
        """What is connected, for /api/calls/health and the console."""
        return {
            "connected": sorted(self.sessions),
            "tools": sorted(self.tools),
            "unavailable": self.failures,
        }


def _runnable(command: str) -> bool:
    return Path(command).exists() or shutil.which(command) is not None


def _text_of(result: Any) -> str:
    parts = [getattr(block, "text", "") for block in getattr(result, "content", []) or []]
    return " ".join(part for part in parts if part).strip() or "the tool reported an error"


def _payload_of(result: Any) -> Any:
    """Prefer the structured result; fall back to the text blocks.

    MCP servers may return both. `structuredContent` is what a program should read — parsing the
    human-readable rendering back into data is how a booking id becomes a substring bug.
    """
    # `structured_content` in Python; `structuredContent` is only the wire alias. Reading the
    # wire name off the model returns None forever, so every caller silently gets the
    # human-readable rendering and indexes into a string — which is exactly how this was found.
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    return _text_of(result)


def _spoken_from_detail(detail: str) -> str:
    """Turn a tool's error into something sayable.

    The calendar's own messages are already written to be said out loud ("that slot was booked
    by someone else"), so they are passed through. Anything else gets a generic line, because
    reading an exception to a prospect is worse than admitting the system had a problem.
    """
    lowered = detail.lower()
    if "booked by someone else" in lowered or "in the past" in lowered:
        return detail
    return "That did not go through — let me get a colleague to sort it out."
