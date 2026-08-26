"""Who may start a call once the agent is on a stranger's website.

WHY THIS EXISTS AT ALL. While the agent lived in our console the only person who could open a
call was whoever had the console. An embed puts a socket on a public marketing page, and every
call costs a slice of a GPU — a model generating, a synthesiser running, a lip-sync pass per
clause. A script opening five hundred sockets is not a hypothetical.

The tests worth writing are not "does the counter increment". They are: does a refusal say
something a person could read, does a limit release when a call ends badly, and does the
per-visitor key resist being chosen by the visitor.
"""

from __future__ import annotations

import time

import pytest

from rainmaker.calls.admission import Admission, Limits, visitor_id


@pytest.fixture
def gate() -> Admission:
    return Admission(limits=Limits(concurrent=2, per_visitor_hourly=3, per_agent_hourly=5))


class TestProtectingTheHardware:
    def test_calls_are_allowed_up_to_the_concurrency_limit(self, gate: Admission):
        for i in range(2):
            assert gate.may_start("agent", f"visitor-{i}").allowed
            gate.started("agent", f"visitor-{i}")
        assert not gate.may_start("agent", "visitor-3").allowed

    def test_a_finished_call_frees_a_slot(self, gate: Admission):
        gate.started("agent", "a")
        gate.started("agent", "b")
        assert not gate.may_start("agent", "c").allowed

        gate.finished("agent")
        assert gate.may_start("agent", "c").allowed

    def test_finishing_more_than_started_does_not_go_negative(self, gate: Admission):
        """A socket can die in ways that call the cleanup twice. A negative live count would let
        the limit be exceeded forever afterwards."""
        gate.started("agent", "a")
        gate.finished("agent")
        gate.finished("agent")
        gate.finished("agent")
        assert gate.live_calls("agent") == 0

    def test_two_agents_do_not_share_a_concurrency_budget(self, gate: Admission):
        gate.started("one", "a")
        gate.started("one", "b")
        assert not gate.may_start("one", "c").allowed
        assert gate.may_start("two", "c").allowed


class TestProtectingAgainstALoop:
    def test_one_visitor_is_capped_per_hour(self, gate: Admission):
        for _ in range(3):
            assert gate.may_start("agent", "same-person").allowed
            gate.started("agent", "same-person")
            gate.finished("agent")
        assert not gate.may_start("agent", "same-person").allowed

    def test_a_different_visitor_is_unaffected(self, gate: Admission):
        for _ in range(3):
            gate.started("agent", "loop")
            gate.finished("agent")
        assert not gate.may_start("agent", "loop").allowed
        assert gate.may_start("agent", "someone-else").allowed

    def test_the_agent_wide_cap_catches_a_distributed_loop(self, gate: Admission):
        """Five hundred sockets from five hundred addresses defeats a per-visitor limit. This is
        the number a subscription tier actually sells."""
        for i in range(5):
            gate.started("agent", f"visitor-{i}")
            gate.finished("agent")
        assert not gate.may_start("agent", "visitor-brand-new").allowed

    def test_old_starts_fall_out_of_the_window(self, gate: Admission):
        """Trimmed on read rather than by a timer: a sweep is a background task that can stop
        running without anyone noticing until the memory graph does."""
        gate.started("agent", "person")
        gate.finished("agent")
        # Reach in and age the entry rather than sleeping an hour.
        gate._visitor_starts["person"][0] = time.monotonic() - 3601
        assert gate._recent(gate._visitor_starts, "person", time.monotonic()) == 0


class TestEndingACallThatWillNotEnd:
    def test_a_call_within_its_budget_continues(self, gate: Admission):
        assert gate.check_ongoing(time.monotonic(), turns=3).allowed

    def test_a_call_that_has_gone_eighty_turns_is_a_loop(self, gate: Admission):
        verdict = gate.check_ongoing(time.monotonic(), turns=gate.limits.max_turns)
        assert not verdict.allowed
        assert verdict.reason == "turn_limit"

    def test_a_tab_somebody_walked_away_from_is_closed(self, gate: Admission):
        started = time.monotonic() - gate.limits.max_call_seconds - 1
        verdict = gate.check_ongoing(started, turns=2)
        assert not verdict.allowed
        assert verdict.reason == "time_limit"


class TestEveryRefusalIsSayable:
    def test_a_refusal_carries_a_sentence(self, gate: Admission):
        """The person reading it is standing on a customer's website. "429" on a dental
        practice's homepage is worse than the call it prevented."""
        gate.started("agent", "a")
        gate.started("agent", "b")
        verdict = gate.may_start("agent", "c")

        assert verdict.spoken
        assert verdict.spoken[0].isupper() and verdict.spoken.rstrip().endswith((".", "?"))
        assert verdict.reason not in verdict.spoken

    @pytest.mark.parametrize("turns,seconds", [(999, 0), (0, 10_000)])
    def test_an_ended_call_is_ended_with_a_sentence_too(self, turns: int, seconds: int):
        gate = Admission()
        verdict = gate.check_ongoing(time.monotonic() - seconds, turns=turns)
        assert not verdict.allowed and verdict.spoken

    def test_the_tenants_ceiling_does_not_explain_their_billing_to_a_stranger(
        self, gate: Admission
    ):
        for i in range(5):
            gate.started("agent", f"v{i}")
            gate.finished("agent")
        spoken = gate.may_start("agent", "new").spoken.lower()
        for leak in ("limit", "quota", "plan", "billing", "tier"):
            assert leak not in spoken


class TestWhoTheVisitorIs:
    def test_the_socket_address_is_used_by_default(self):
        assert visitor_id("203.0.113.9", None) == "203.0.113.9"

    def test_only_the_first_forwarded_hop_is_trusted(self):
        """Everything after the first hop is whatever the client felt like sending, and a
        spoofable key turns the per-visitor limit into decoration."""
        assert visitor_id("10.0.0.1", "203.0.113.9, 10.0.0.5, 10.0.0.9") == "203.0.113.9"

    def test_a_missing_address_is_still_a_key(self):
        """Everyone unidentifiable shares one bucket, which is stricter than letting them all
        through."""
        assert visitor_id(None, None) == "unknown"

    def test_an_empty_forwarded_header_falls_back(self):
        assert visitor_id("10.0.0.1", "") == "10.0.0.1"
