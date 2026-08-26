"""Who is allowed to start a call, and when to stop one.

THE MOMENT THE AGENT IS EMBEDDABLE IT IS ABUSABLE. Until now the only person who could open a
call was whoever had the console in front of them. An embed puts a socket on a customer's public
marketing page, which means the caller is an anonymous stranger and every call costs a slice of
a GPU: a language model generating, a synthesiser running, a lip-sync pass per clause. A script
opening five hundred sockets is not a hypothetical, it is a Tuesday.

WHAT EACH LIMIT IS ACTUALLY FOR, because they are not interchangeable:

    concurrency   protects the hardware. There is one GPU; a dozen simultaneous calls do not
                  make a dozen people happy, they make a dozen people wait.
    per-visitor   protects against one bored person in a loop. Generous enough that a real
                  prospect who reloads twice never notices it.
    per-agent     protects a tenant from a bill, and Rainmaker from a tenant. It is the number
                  a plan actually sells.
    per-call      protects against the call that never ends — a wedged client, or someone who
                  walked away with the tab open.

REFUSALS ARE SPOKEN, NOT STATUS CODES. A visitor turned away sees a sentence on the customer's
website, so every decision carries one. "429" on a dental practice's homepage is worse than the
call it prevented.

IN MEMORY, AND THAT IS A LIMITATION RATHER THAN A DESIGN. One process, one counter set. Two
instances behind a load balancer would each enforce their own half of the limit, and the fix is
Redis rather than anything clever here. It is written down because the alternative is somebody
assuming it scales.
"""

from __future__ import annotations

import os
import time
from collections import deque
from dataclasses import dataclass, field


def _limit(name: str, fallback: int) -> int:
    """A limit, overridable by environment.

    THE NUMBERS BELOW ARE DEFAULTS, NOT PHYSICS, and they were unreachable until something
    legitimate needed to exceed them: regenerating the README's screenshots drives eight calls
    in a few minutes from one machine, and the seventh was refused by a cap meant for a stranger
    on a customer's marketing site. A rate limit that cannot be raised for an operator is a rate
    limit somebody edits the source to get past, which is worse than one with a knob on it.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return fallback
    try:
        value = int(raw)
    except ValueError:
        return fallback
    return value if value > 0 else fallback


#: How many calls one agent may have running at once. The ceiling is the GPU, not the plan:
#: Qwen holds ~3.1GB, Kokoro takes CPU, and lip-sync runs on the same card, so a laptop-class
#: box serves one or two conversations before everybody is waiting.
DEFAULT_CONCURRENT = _limit("RAINMAKER_MAX_CONCURRENT_CALLS", 2)

#: One visitor, per hour. A real prospect might reload, change their mind, come back after
#: lunch. Six is past all of that and well short of a loop.
DEFAULT_PER_VISITOR_HOURLY = _limit("RAINMAKER_CALLS_PER_VISITOR_HOUR", 6)

#: One agent, per hour, across every visitor. This is the number a subscription tier sells.
DEFAULT_PER_AGENT_HOURLY = _limit("RAINMAKER_CALLS_PER_AGENT_HOUR", 60)

#: A single call. Twenty minutes is longer than any real first call; the cap exists for the tab
#: somebody left open, not for the conversation somebody is having.
MAX_CALL_SECONDS = 20 * 60

#: Turns in one call. A conversation that has gone eighty turns is a loop, not a sale.
MAX_TURNS = 80


@dataclass(frozen=True, slots=True)
class Verdict:
    """Whether a call may start, and what to say if not."""

    allowed: bool
    reason: str = ""
    spoken: str = ""

    @classmethod
    def yes(cls) -> Verdict:
        return cls(allowed=True)

    @classmethod
    def no(cls, reason: str, spoken: str) -> Verdict:
        return cls(allowed=False, reason=reason, spoken=spoken)


@dataclass(slots=True)
class Limits:
    """What one agent is allowed. In a billed deployment this comes from the tenant's plan."""

    concurrent: int = DEFAULT_CONCURRENT
    per_visitor_hourly: int = DEFAULT_PER_VISITOR_HOURLY
    per_agent_hourly: int = DEFAULT_PER_AGENT_HOURLY
    max_call_seconds: int = MAX_CALL_SECONDS
    max_turns: int = MAX_TURNS


@dataclass(slots=True)
class Admission:
    """Counters for every agent this process is serving.

    Deliberately not a decorator or middleware. Admission is a decision with a sentence
    attached, made once at the top of a call, and burying it in a framework hook makes the
    sentence somebody else's problem.
    """

    limits: Limits = field(default_factory=Limits)
    _live: dict[str, int] = field(default_factory=dict)
    _visitor_starts: dict[str, deque[float]] = field(default_factory=dict)
    _agent_starts: dict[str, deque[float]] = field(default_factory=dict)

    # ── the decision ────────────────────────────────────────────────────
    def may_start(self, agent_key: str, visitor: str) -> Verdict:
        """Checked before the socket does anything expensive.

        Order matters: the cheapest and most common refusal first, so a script in a loop is
        turned away by a dictionary lookup rather than after a model has been consulted.
        """
        now = time.monotonic()

        if self._live.get(agent_key, 0) >= self.limits.concurrent:
            return Verdict.no(
                "at_capacity",
                "We're speaking with someone else at the moment — try again in a minute, or "
                "leave your details and a person will come back to you.",
            )

        if self._recent(self._visitor_starts, visitor, now) >= self.limits.per_visitor_hourly:
            return Verdict.no(
                "visitor_hourly",
                "We've spoken a few times just now. Give it an hour, or ask for a person and "
                "someone will pick it up.",
            )

        if self._recent(self._agent_starts, agent_key, now) >= self.limits.per_agent_hourly:
            # The tenant's ceiling, not the visitor's fault. The sentence says so without
            # explaining somebody else's billing to a stranger.
            return Verdict.no(
                "agent_hourly",
                "We're not able to take calls right now. Leave your details and someone will "
                "get back to you.",
            )

        return Verdict.yes()

    def started(self, agent_key: str, visitor: str) -> None:
        now = time.monotonic()
        self._live[agent_key] = self._live.get(agent_key, 0) + 1
        self._visitor_starts.setdefault(visitor, deque()).append(now)
        self._agent_starts.setdefault(agent_key, deque()).append(now)

    def finished(self, agent_key: str) -> None:
        """Always called, including when a socket dies badly.

        A leaked live-count is worse than any of the limits it enforces: the agent stops
        answering and nothing in the logs says why.
        """
        remaining = self._live.get(agent_key, 0) - 1
        if remaining > 0:
            self._live[agent_key] = remaining
        else:
            self._live.pop(agent_key, None)

    # ── during the call ─────────────────────────────────────────────────
    def check_ongoing(self, started_at: float, turns: int) -> Verdict:
        """Whether a call in progress should keep going."""
        if turns >= self.limits.max_turns:
            return Verdict.no(
                "turn_limit",
                "We've covered a lot — let me have a person pick this up with you.",
            )
        if time.monotonic() - started_at >= self.limits.max_call_seconds:
            return Verdict.no(
                "time_limit",
                "I'll let you go — someone will follow up so you don't have to repeat any of "
                "this.",
            )
        return Verdict.yes()

    # ── introspection ───────────────────────────────────────────────────
    def live_calls(self, agent_key: str | None = None) -> int:
        if agent_key is not None:
            return self._live.get(agent_key, 0)
        return sum(self._live.values())

    def describe(self) -> dict[str, object]:
        return {
            "live": dict(self._live),
            "limits": {
                "concurrent": self.limits.concurrent,
                "per_visitor_hourly": self.limits.per_visitor_hourly,
                "per_agent_hourly": self.limits.per_agent_hourly,
                "max_call_seconds": self.limits.max_call_seconds,
                "max_turns": self.limits.max_turns,
            },
            "note": "in-process counters; a second instance would enforce its own half",
        }

    # ── internals ───────────────────────────────────────────────────────
    def _recent(self, bucket: dict[str, deque[float]], key: str, now: float) -> int:
        """How many starts in the last hour, dropping older ones as it goes.

        Trimmed on read rather than on a timer: the entries that matter are the ones being
        asked about, and a sweep is a background task that can stop running without anyone
        noticing until the memory graph does.
        """
        window = bucket.get(key)
        if window is None:
            return 0
        cutoff = now - 3600
        while window and window[0] < cutoff:
            window.popleft()
        if not window:
            bucket.pop(key, None)
            return 0
        return len(window)


def visitor_id(client_host: str | None, forwarded_for: str | None) -> str:
    """Who this caller is, for rate-limiting purposes only.

    NOT AN IDENTITY AND NOT STORED. It is an address, used to count starts in an hour and
    forgotten an hour later. `X-Forwarded-For` is trusted only for its first hop, because
    everything after that is whatever the client felt like sending — and a spoofable key would
    turn the per-visitor limit into decoration.
    """
    if forwarded_for:
        first = forwarded_for.split(",")[0].strip()
        if first:
            return first
    return client_host or "unknown"
