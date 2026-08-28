"""A real MCP server for booking meetings, backed by SQLite.

RUN IT LIKE ANY MCP SERVER:

    python -m rainmaker.mcp.servers.calendar

It speaks JSON-RPC over stdio and works in Claude Desktop, in `mcp dev`, or in anything else
that speaks the protocol. That is the point of building it this way rather than as a function
the call loop imports: **the tools are the integration surface**. A customer who already runs
Google Calendar or Cal.com swaps the server entry in `mcp.toml` and Nadia books into their
calendar with no change to Rainmaker — see `rainmaker/mcp/client.py`.

WHY A LOCAL SERVER IS THE DEFAULT. Every hosted calendar needs an OAuth app and an account, and
a repository a reviewer cannot run is a repository they do not evaluate. This one needs nothing,
works with the network off like the rest of the console, and demonstrates the protocol honestly
rather than demonstrating a Google login.

THE INVARIANT THAT MATTERS is that two prospects cannot be sold the same slot. Availability and
booking are separate calls with a gap between them — the agent lists slots, talks for ninety
seconds, then books — so the check has to happen inside the write, under a constraint the
database enforces, rather than in the code that read the availability.

A TAKEN SLOT IS AN ANSWER, NOT AN EXCEPTION, and `book_meeting` returns `confirmed: false` with
a sayable reason rather than raising. Two reasons. Protocol: the MCP SDK wraps a raised
exception as "Error executing tool book_meeting" and the original message does not survive, so
the caller cannot tell "someone else took it" from "the database is gone" — and those need
different sentences on a live call. Design: losing a race is a normal outcome of a booking
system, and modelling normal outcomes as exceptions pushes control flow into except blocks.

Genuinely malformed input still raises. An address with no `@` in it is a caller bug, and the
caller should hear about it as one.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

DATA_DIR = Path(os.environ.get("RAINMAKER_DATA", "data"))
DB_PATH = Path(os.environ.get("RAINMAKER_CALENDAR_DB", DATA_DIR / "calendar.sqlite3"))

#: The working day the agent may offer, in UTC hours. A demo booked at three in the morning is
#: a demo nobody attends, and "any time is available" is the tell that a calendar is fake.
DAY_START_HOUR = 9
DAY_END_HOUR = 17

#: Meetings start on the hour or the half hour. Offering 14:07 is technically more available and
#: reads as a machine talking.
SLOT_MINUTES = 30

#: How far ahead the agent will look. Beyond a fortnight a prospect stops treating it as a
#: commitment.
MAX_HORIZON_DAYS = 21

server = MCPServer(
    "rainmaker-calendar",
    instructions=(
        "Availability and booking for sales meetings. Call `list_availability` before "
        "`book_meeting`: a slot that was free a minute ago may not be now."
    ),
)


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bookings (
            id             TEXT PRIMARY KEY,
            starts_at      TEXT NOT NULL,
            ends_at        TEXT NOT NULL,
            attendee_email TEXT NOT NULL,
            attendee_name  TEXT NOT NULL DEFAULT '',
            company        TEXT NOT NULL DEFAULT '',
            subject        TEXT NOT NULL DEFAULT '',
            notes          TEXT NOT NULL DEFAULT '',
            created_at     TEXT NOT NULL,
            cancelled_at   TEXT
        )
        """
    )
    # THE INVARIANT, ENFORCED BY THE DATABASE. A `SELECT` then `INSERT` in application code has
    # a window between them, and the agent's window is the length of a sentence. A partial
    # unique index means the second booking of the same slot fails on the write, whoever wrote
    # the code that raced.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS one_meeting_per_slot "
        "ON bookings (starts_at) WHERE cancelled_at IS NULL"
    )
    return conn


def _parse(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO 8601 timestamp, got {value!r}") from exc
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _slots_on(day: datetime, duration: int) -> list[datetime]:
    """Every start time that fits a meeting of `duration` inside the working day."""
    out: list[datetime] = []
    cursor = day.replace(hour=DAY_START_HOUR, minute=0, second=0, microsecond=0)
    end_of_day = day.replace(hour=DAY_END_HOUR, minute=0, second=0, microsecond=0)
    while cursor + timedelta(minutes=duration) <= end_of_day:
        out.append(cursor)
        cursor += timedelta(minutes=SLOT_MINUTES)
    return out


@server.tool(
    title="List available meeting slots",
    description=(
        "Open slots for a meeting, in UTC. Skips weekends, anything already booked, and "
        "anything in the past. Call this before booking."
    ),
)
def list_availability(
    duration_minutes: int = 30,
    days_ahead: int = 5,
    after: str | None = None,
    limit: int = 6,
) -> dict[str, Any]:
    """Args:
    duration_minutes: How long the meeting runs.
    days_ahead: How many days from now to search.
    after: Earliest acceptable start, ISO 8601. Defaults to now.
    limit: Most slots to return. A prospect offered twenty options picks none.
    """
    if duration_minutes <= 0:
        raise ValueError("duration_minutes must be positive")
    days_ahead = max(1, min(days_ahead, MAX_HORIZON_DAYS))
    # Never offer the next four minutes. A slot the prospect cannot physically reach reads as
    # availability and lands as a no-show.
    earliest = _parse(after, "after") if after else datetime.now(UTC) + timedelta(minutes=15)

    with _connect() as conn:
        taken = {
            row["starts_at"]
            for row in conn.execute(
                "SELECT starts_at FROM bookings WHERE cancelled_at IS NULL"
            )
        }

    found: list[dict[str, str]] = []
    for offset in range(days_ahead + 1):
        day = (datetime.now(UTC) + timedelta(days=offset)).replace(microsecond=0)
        if day.weekday() >= 5:  # Saturday, Sunday
            continue
        for slot in _slots_on(day, duration_minutes):
            if slot < earliest or slot.isoformat() in taken:
                continue
            found.append(
                {
                    "starts_at": slot.isoformat(),
                    "ends_at": (slot + timedelta(minutes=duration_minutes)).isoformat(),
                    # Pre-formatted for speech: the agent must say "Tuesday at half past two",
                    # not read an ISO timestamp down the phone.
                    "spoken": _spoken(slot),
                }
            )
            if len(found) >= limit:
                return {"slots": found, "duration_minutes": duration_minutes}
    return {"slots": found, "duration_minutes": duration_minutes}


@server.tool(
    title="Book a meeting",
    description=(
        "Book one of the slots returned by list_availability. Fails if the slot was taken in "
        "the meantime — offer the prospect another rather than retrying."
    ),
)
def book_meeting(
    starts_at: str,
    attendee_email: str,
    attendee_name: str = "",
    company: str = "",
    duration_minutes: int = 30,
    subject: str = "Rainmaker demo",
    notes: str = "",
) -> dict[str, Any]:
    """Args:
    starts_at: Slot start, ISO 8601, from list_availability.
    attendee_email: Who the invitation is for.
    attendee_name: Their name, if known.
    company: Their company, if known.
    duration_minutes: How long the meeting runs.
    subject: Meeting title.
    notes: What was discussed, carried into the invitation.
    """
    if "@" not in attendee_email:
        raise ValueError(f"attendee_email does not look like an address: {attendee_email!r}")
    start = _parse(starts_at, "starts_at")

    if start < datetime.now(UTC):
        return {
            "confirmed": False,
            "reason": "in_the_past",
            "spoken": "That time has already gone by — let me offer you another.",
            "starts_at": start.isoformat(),
        }

    booking_id = f"mtg_{uuid.uuid4().hex[:12]}"
    end = start + timedelta(minutes=duration_minutes)
    with _connect() as conn:
        try:
            conn.execute(
                "INSERT INTO bookings (id, starts_at, ends_at, attendee_email, attendee_name,"
                " company, subject, notes, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    booking_id,
                    start.isoformat(),
                    end.isoformat(),
                    attendee_email,
                    attendee_name,
                    company,
                    subject,
                    notes,
                    datetime.now(UTC).isoformat(),
                ),
            )
        except sqlite3.IntegrityError:
            # The unique index did its job. Reported as an outcome the agent can act on rather
            # than raised, so she offers another slot instead of apologising for a system error.
            return {
                "confirmed": False,
                "reason": "slot_taken",
                "spoken": (
                    f"Someone just took {_spoken(start)} — I have other times if you'd like."
                ),
                "starts_at": start.isoformat(),
            }

    return {
        "booking_id": booking_id,
        "starts_at": start.isoformat(),
        "ends_at": end.isoformat(),
        "spoken": _spoken(start),
        "attendee_email": attendee_email,
        "confirmed": True,
    }


@server.tool(title="Cancel a meeting", description="Cancel a booking by its id.")
def cancel_meeting(booking_id: str) -> dict[str, Any]:
    """Args:
    booking_id: The id returned by book_meeting.
    """
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE bookings SET cancelled_at = ? WHERE id = ? AND cancelled_at IS NULL",
            (datetime.now(UTC).isoformat(), booking_id),
        )
        if cursor.rowcount == 0:
            return {
                "cancelled": False,
                "reason": "not_found",
                "spoken": "I could not find that booking — it may already have been cancelled.",
                "booking_id": booking_id,
            }
    return {"booking_id": booking_id, "cancelled": True}


@server.tool(
    title="List booked meetings",
    description="Meetings on the calendar, optionally filtered to one attendee.",
)
def list_bookings(attendee_email: str | None = None, include_cancelled: bool = False) -> dict[str, Any]:
    """Args:
    attendee_email: Only this person's meetings.
    include_cancelled: Include meetings that were cancelled.
    """
    sql = "SELECT * FROM bookings WHERE 1=1"
    params: list[Any] = []
    if attendee_email:
        sql += " AND attendee_email = ?"
        params.append(attendee_email)
    if not include_cancelled:
        sql += " AND cancelled_at IS NULL"
    sql += " ORDER BY starts_at"

    with _connect() as conn:
        rows = [dict(row) for row in conn.execute(sql, params)]
    for row in rows:
        row["spoken"] = _spoken(_parse(row["starts_at"], "starts_at"))
    return {"bookings": rows, "count": len(rows)}


#: Words, not digits. The call rules tell the agent to say numbers the way people say them, and
#: the calendar is the one place a number becomes a commitment — so it arrives already spoken
#: rather than trusting a 1.5B model to convert "09:30" without dropping the thirty.
_ONES = (
    "twelve", "one", "two", "three", "four", "five", "six",
    "seven", "eight", "nine", "ten", "eleven",
)
_TEENS_AND_TENS = {
    20: "twenty", 30: "thirty", 40: "forty", 50: "fifty",
    10: "ten", 11: "eleven", 12: "twelve", 13: "thirteen", 14: "fourteen",
    15: "fifteen", 16: "sixteen", 17: "seventeen", 18: "eighteen", 19: "nineteen",
}
_ORDINALS = {
    1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth", 6: "sixth", 7: "seventh",
    8: "eighth", 9: "ninth", 10: "tenth", 11: "eleventh", 12: "twelfth", 13: "thirteenth",
    14: "fourteenth", 15: "fifteenth", 16: "sixteenth", 17: "seventeenth", 18: "eighteenth",
    19: "nineteenth", 20: "twentieth", 30: "thirtieth",
}


def _ordinal_words(day: int) -> str:
    if day in _ORDINALS:
        return _ORDINALS[day]
    tens, ones = divmod(day, 10)
    return f"{_TEENS_AND_TENS[tens * 10]}-{_ORDINALS[ones]}"


def _minutes_words(minute: int) -> str:
    if minute == 0:
        return ""
    if minute == 30:
        return " thirty"
    if minute == 15:
        return " fifteen"
    if minute in _TEENS_AND_TENS:
        return f" {_TEENS_AND_TENS[minute]}"
    tens, ones = divmod(minute, 10)
    # "oh five" rather than "five": a bare number after the hour is heard as the hour repeated.
    if tens == 0:
        return f" oh {_ONES[ones]}"
    return f" {_TEENS_AND_TENS[tens * 10]} {_ONES[ones]}"


def _spoken(when: datetime, *, now: datetime | None = None) -> str:
    """A time as a person offering it would actually say it.

    Formatted here rather than in the agent because the model is the least reliable place to put
    a fact. Asked to read "2026-08-27T14:30:00+00:00" aloud, a small model will cheerfully say
    the wrong day, and a wrong time in a confirmed booking is a missed meeting. Handing it a
    finished phrase removes the opportunity.

    IT USED TO SAY "FRIDAY THE TWENTY-EIGHTH AT TEN IN THE MORNING, U T C", which is correct,
    unambiguous, and not a sentence anybody has ever spoken to another person. Three things were
    wrong with it and all three are about being heard rather than being right:

      the ordinal   Nobody offering a slot four days out says the date. They say the day. The
                    date belongs on the confirmation, where it is read rather than heard.
      the timezone  "U T C" spelled into the middle of an offer is the clearest possible sign
                    that a machine composed the sentence. The exact timestamp is on the screen
                    and in the confirmation; the offer is a conversation.
      the distance  A slot tomorrow is "tomorrow". Saying "Friday" for tomorrow makes the
                    listener do arithmetic to check you.
    """
    now = now or datetime.now(UTC)
    days = (when.date() - now.date()).days
    if days == 0:
        day = "today"
    elif days == 1:
        day = "tomorrow"
    elif 2 <= days <= 6:
        day = f"{when:%A}"
    else:
        # Far enough out that the weekday alone is ambiguous, so the date comes back.
        day = f"{when:%A} the {_ordinal_words(when.day)}"

    hour_words = _ONES[when.hour % 12]
    part = "in the morning" if when.hour < 12 else "in the afternoon"
    return f"{day} at {hour_words}{_minutes_words(when.minute)} {part}"


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
