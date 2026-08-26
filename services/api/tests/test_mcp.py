"""The tool layer: the calendar's invariants, and the protocol path that reaches them.

TWO LEVELS, DELIBERATELY. Most of this file calls the server functions directly, because what is
worth testing about a calendar is that it will not sell the same slot twice — not that JSON-RPC
works. One test spawns the real server as a subprocess and talks to it over stdio, because
everything else here would still pass if the protocol wiring were broken, and "we speak MCP" is
a claim the README makes.

THE BUG THIS FILE EXISTS BECAUSE OF: `_payload_of` read `result.structuredContent`, which is the
WIRE alias. The Python model exposes `structured_content`, so the attribute was always None,
every tool call silently returned the human-readable text rendering instead of data, and the
first caller indexed a string and got `TypeError: string indices must be integers`. A test that
only checked "the call succeeded" would have passed.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


@pytest.fixture
def calendar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The calendar server module, pointed at a throwaway database.

    Reloaded per test because the module resolves its database path at import time — which is
    the right shape for a server process that is started once, and needs handling here.
    """
    import importlib

    monkeypatch.setenv("RAINMAKER_CALENDAR_DB", str(tmp_path / "calendar.sqlite3"))
    from rainmaker.mcp.servers import calendar as module

    return importlib.reload(module)


def _first_slot(calendar, **kwargs) -> str:
    slots = calendar.list_availability(limit=3, **kwargs)["slots"]
    assert slots, "the calendar offered nothing at all"
    return slots[0]["starts_at"]


class TestWhatTheCalendarWillOffer:
    def test_it_offers_slots_inside_working_hours(self, calendar):
        for slot in calendar.list_availability(limit=20)["slots"]:
            hour = datetime.fromisoformat(slot["starts_at"]).hour
            assert calendar.DAY_START_HOUR <= hour < calendar.DAY_END_HOUR

    def test_it_never_offers_a_weekend(self, calendar):
        """A demo booked for Saturday is a demo nobody attends."""
        for slot in calendar.list_availability(limit=40, days_ahead=10)["slots"]:
            assert datetime.fromisoformat(slot["starts_at"]).weekday() < 5

    def test_it_never_offers_the_past(self, calendar):
        for slot in calendar.list_availability(limit=10)["slots"]:
            assert datetime.fromisoformat(slot["starts_at"]) > datetime.now(UTC)

    def test_it_does_not_offer_the_next_thirty_seconds(self, calendar):
        """A slot the prospect cannot physically reach reads as availability and lands as a
        no-show."""
        soonest = datetime.fromisoformat(_first_slot(calendar))
        assert soonest > datetime.now(UTC) + timedelta(minutes=10)

    def test_every_slot_arrives_ready_to_say_out_loud(self, calendar):
        """Formatted by the server, not the model. Asked to read an ISO timestamp aloud, a small
        model will cheerfully say the wrong day, and a wrong time in a confirmed booking is a
        missed meeting."""
        slot = calendar.list_availability(limit=1)["slots"][0]
        spoken = slot["spoken"]
        assert slot["starts_at"] not in spoken, "the raw timestamp leaked into the spoken form"
        assert not any(char.isdigit() for char in spoken), f"digits survived: {spoken!r}"
        assert any(day in spoken for day in
                   ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday"))

    def test_a_limit_is_respected_because_choice_paralyses(self, calendar):
        assert len(calendar.list_availability(limit=2)["slots"]) == 2

    def test_a_nonsense_duration_is_a_caller_bug_and_raises(self, calendar):
        with pytest.raises(ValueError, match="positive"):
            calendar.list_availability(duration_minutes=0)


class TestTheOneThingThatMustNotHappen:
    def test_a_slot_can_be_booked(self, calendar):
        result = calendar.book_meeting(
            starts_at=_first_slot(calendar), attendee_email="dana@corvus.example"
        )
        assert result["confirmed"] is True
        assert result["booking_id"].startswith("mtg_")

    def test_the_same_slot_cannot_be_booked_twice(self, calendar):
        """THE INVARIANT. Availability and booking are separate calls with a conversation in
        between, so the check has to live in the write."""
        slot = _first_slot(calendar)
        assert calendar.book_meeting(starts_at=slot, attendee_email="first@x.example")["confirmed"]

        second = calendar.book_meeting(starts_at=slot, attendee_email="second@x.example")
        assert second["confirmed"] is False
        assert second["reason"] == "slot_taken"

    def test_losing_the_race_is_an_answer_rather_than_an_exception(self, calendar):
        """The MCP SDK rewrites a raised exception as "Error executing tool book_meeting", so a
        raise would leave the agent unable to tell "someone took it" from "the database is
        gone" — and those need different sentences on a live call."""
        slot = _first_slot(calendar)
        calendar.book_meeting(starts_at=slot, attendee_email="first@x.example")
        second = calendar.book_meeting(starts_at=slot, attendee_email="second@x.example")
        assert "took" in second["spoken"].lower()
        assert "error" not in second["spoken"].lower()

    def test_the_invariant_is_the_databases_job_not_the_codes(self, calendar):
        """Enforced by a partial unique index, so it holds against a writer that never went
        through `book_meeting` at all."""
        slot = _first_slot(calendar)
        calendar.book_meeting(starts_at=slot, attendee_email="first@x.example")
        with sqlite3.connect(calendar.DB_PATH) as conn, pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO bookings (id, starts_at, ends_at, attendee_email, created_at)"
                " VALUES (?,?,?,?,?)",
                ("mtg_sneaky", slot, slot, "sneak@x.example", datetime.now(UTC).isoformat()),
            )

    def test_a_cancelled_slot_becomes_available_again(self, calendar):
        slot = _first_slot(calendar)
        booking = calendar.book_meeting(starts_at=slot, attendee_email="first@x.example")
        assert calendar.cancel_meeting(booking["booking_id"])["cancelled"] is True

        again = calendar.book_meeting(starts_at=slot, attendee_email="second@x.example")
        assert again["confirmed"] is True

    def test_a_booked_slot_disappears_from_availability(self, calendar):
        slot = _first_slot(calendar)
        calendar.book_meeting(starts_at=slot, attendee_email="first@x.example")
        remaining = [s["starts_at"] for s in calendar.list_availability(limit=10)["slots"]]
        assert slot not in remaining

    def test_booking_the_past_is_refused_with_something_sayable(self, calendar):
        yesterday = (datetime.now(UTC) - timedelta(days=1)).replace(microsecond=0)
        result = calendar.book_meeting(
            starts_at=yesterday.isoformat(), attendee_email="dana@corvus.example"
        )
        assert result["confirmed"] is False
        assert result["reason"] == "in_the_past"

    def test_a_malformed_address_is_a_caller_bug_and_raises(self, calendar):
        """Not an outcome to narrate — the caller passed rubbish and should hear about it."""
        with pytest.raises(ValueError, match="address"):
            calendar.book_meeting(starts_at=_first_slot(calendar), attendee_email="not-an-email")

    def test_cancelling_something_that_is_not_there_is_not_an_error(self, calendar):
        result = calendar.cancel_meeting("mtg_never_existed")
        assert result["cancelled"] is False
        assert result["reason"] == "not_found"


class TestTheEmailServerIsSafeByDefault:
    def test_a_recap_can_be_drafted_with_no_mail_server(self):
        """The drafting is the hard part and works everywhere. Sending needs an account, which
        a fresh clone does not have."""
        from rainmaker.mcp.servers import email

        draft = email.draft_recap(
            contact_name="Dana Whitfield",
            company="Corvus Data",
            summary="We talked about the overlap with your Postgres setup.",
            meeting_spoken="Wednesday the twenty-sixth at nine in the morning",
        )
        assert "Dana" in draft["body"]
        assert "Wednesday" in draft["body"]
        assert draft["subject"].endswith("Corvus Data")

    def test_the_draft_says_it_was_an_ai(self):
        """The disclosure is enforced on the call; a follow-up that quietly implies a human wrote
        it undoes that an hour later."""
        from rainmaker.mcp.servers import email

        body = email.draft_recap(contact_name="Dana", company="Corvus", summary="x")["body"]
        assert "AI" in body

    def test_sending_is_refused_when_nothing_is_configured(self):
        from rainmaker.mcp.servers import email

        with pytest.raises(ValueError, match="no mail server"):
            email.send_recap("a@b.com", "s", "b", verified_address="a@b.com")

    def test_it_will_not_mail_someone_who_was_not_on_the_call(self, monkeypatch):
        """THE GUARD THAT MATTERS. An agent that can send mail can send mail to anyone, and a
        model that invents a recipient on a tool with a send button is a different category of
        bug from one that invents a sentence."""
        from rainmaker.mcp.servers import email

        monkeypatch.setattr(email, "SMTP_HOST", "localhost")
        with pytest.raises(ValueError, match="refusing to send"):
            email.send_recap(
                to_address="someone.else@elsewhere.example",
                subject="s",
                body="b",
                verified_address="dana@corvus.example",
            )

    def test_the_check_is_case_insensitive_because_addresses_are(self, monkeypatch):
        """Refusing `Dana@Corvus.example` when the call captured `dana@corvus.example` would be
        a guard that fires on the honest path."""
        from rainmaker.mcp.servers import email

        monkeypatch.setattr(email, "SMTP_HOST", "localhost")
        monkeypatch.setattr(email.smtplib, "SMTP", _ExplodingSMTP)
        with pytest.raises(_ReachedSend):
            email.send_recap(
                to_address="Dana@Corvus.example",
                subject="s",
                body="b",
                verified_address="dana@corvus.example",
            )


class _ReachedSend(RuntimeError):
    """Raised by the stub to prove the guard let the call through."""


class _ExplodingSMTP:
    def __init__(self, *args, **kwargs):
        raise _ReachedSend


class TestTheProtocolPathItself:
    """Everything above would pass with the transport completely broken."""

    async def test_the_broker_reaches_a_real_server_over_stdio(self, tmp_path: Path):
        """Spawns `python -m rainmaker.mcp.servers.calendar` as a subprocess and books through
        it. Slow — about a second — and the only test here that proves the claim on the tin."""
        import sys

        from rainmaker.mcp.client import ServerSpec, ToolBroker

        broker = ToolBroker(
            [
                ServerSpec(
                    "calendar",
                    sys.executable,
                    ["-m", "rainmaker.mcp.servers.calendar"],
                    env={"RAINMAKER_CALENDAR_DB": str(tmp_path / "cal.sqlite3")},
                )
            ]
        )
        try:
            await broker.start()
            assert "calendar.book_meeting" in broker.tools, broker.failures

            slots = await broker.call("calendar.list_availability", {"limit": 2})
            # THE REGRESSION. A string here means `structured_content` was read by its wire
            # alias again and every caller is about to index a string.
            assert isinstance(slots, dict), f"expected structured output, got {type(slots)}"
            assert slots["slots"], "no availability came back through the protocol"

            booked = await broker.call(
                "calendar.book_meeting",
                {
                    "starts_at": slots["slots"][0]["starts_at"],
                    "attendee_email": "dana@corvus.example",
                },
            )
            assert booked["confirmed"] is True
        finally:
            await broker.close()

    async def test_a_server_that_cannot_start_degrades_rather_than_raising(self):
        """A missing tool costs a capability. It must not cost the call."""
        from rainmaker.mcp.client import ServerSpec, ToolBroker

        broker = ToolBroker([ServerSpec("ghost", "definitely-not-a-real-command-xyz")])
        try:
            await broker.start()
            assert broker.tools == {}
            assert "ghost" in broker.failures
        finally:
            await broker.close()

    async def test_calling_a_tool_that_is_not_there_says_something_sayable(self):
        from rainmaker.mcp.client import ToolBroker, ToolError

        broker = ToolBroker([])
        await broker.start()
        try:
            with pytest.raises(ToolError) as caught:
                await broker.call("calendar.book_meeting", {})
            assert caught.value.spoken
            assert "Traceback" not in caught.value.spoken
        finally:
            await broker.close()

    def test_the_shipped_defaults_need_no_account(self):
        """The rule the whole server set is built around: a clone gets working tools.

        Email included. The whole email server used to be disabled without SMTP, which
        contradicted its own design — `draft_recap` needs no mail server and `send_recap`
        refuses without one — and meant that composing the follow-up, the interesting half, had
        never once run. Only sending is gated now, inside the tool that sends.
        """
        from rainmaker.mcp.client import default_servers

        for spec in default_servers():
            assert spec.enabled, f"{spec.name} is off by default; a clone would not have it"

    def test_composing_a_follow_up_needs_no_account_but_sending_does(self):
        from rainmaker.mcp.servers import email

        assert email.draft_recap(contact_name="Dana", company="Corvus", summary="x")["body"]
        if not os.environ.get("RAINMAKER_SMTP_HOST"):
            with pytest.raises(ValueError, match="no mail server"):
                email.send_recap("a@b.com", "s", "b", verified_address="a@b.com")
