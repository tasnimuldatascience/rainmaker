/**
 * The diary: what the agent has actually booked.
 *
 * WHY THIS EXISTS. `list_bookings` has been on the calendar tool server since it was written,
 * the agent has been filling it in on every call that needed a person, and there was nowhere to
 * see the result — so the honest reaction to watching a booking confirm was "where is the
 * calendar?". A tool whose output nobody can see is indistinguishable from a tool that did
 * nothing, and the whole claim of this product is that its actions are real.
 *
 * IT READS THROUGH THE SERVER, NOT THE DATABASE. The API asks the same MCP tool the agent asks,
 * so a customer who swaps the local calendar for their own Google Calendar gets this view
 * populated by their own meetings without a line changing here.
 */

import { useCallback, useEffect, useState } from "react";

interface Booking {
  /** `list_bookings` returns `id`; `book_meeting` returns `booking_id`. Accept both rather
   *  than picking one and rendering a column of undefined keys. */
  id?: string;
  booking_id?: string;
  starts_at: string;
  ends_at?: string;
  spoken: string;
  attendee_email: string;
  attendee_name?: string;
  company?: string;
  notes?: string;
  cancelled_at?: string | null;
}

/** Grouped by day, because a diary is read by day and never as a flat list. */
function byDay(bookings: Booking[]): [string, Booking[]][] {
  const days = new Map<string, Booking[]>();
  for (const booking of bookings) {
    const key = new Date(booking.starts_at).toDateString();
    days.set(key, [...(days.get(key) ?? []), booking]);
  }
  return [...days.entries()];
}

const dayLabel = (key: string) => {
  const date = new Date(key);
  const today = new Date();
  const tomorrow = new Date(today.getTime() + 86_400_000);
  if (date.toDateString() === today.toDateString()) return "Today";
  if (date.toDateString() === tomorrow.toDateString()) return "Tomorrow";
  return date.toLocaleDateString(undefined, {
    weekday: "long",
    day: "numeric",
    month: "long",
  });
};

const clock = (iso: string) =>
  new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });

export function CalendarView() {
  const [bookings, setBookings] = useState<Booking[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const response = await fetch("/api/calendar");
      if (!response.ok) throw new Error(String(response.status));
      const body = await response.json();
      setBookings(body.bookings ?? []);
      setFailed(Boolean(body.unavailable));
    } catch {
      setBookings([]);
      setFailed(true);
    }
  }, []);

  useEffect(() => {
    void load();
    // A call happening in the other tab writes here. Polling rather than a socket: this is a
    // page somebody glances at, and a second live channel is not worth its reconnect logic.
    const timer = window.setInterval(() => void load(), 15_000);
    return () => window.clearInterval(timer);
  }, [load]);

  const cancel = async (id: string) => {
    setBusy(id);
    try {
      await fetch(`/api/calendar/${encodeURIComponent(id)}/cancel`, { method: "POST" });
      await load();
    } finally {
      setBusy(null);
    }
  };

  const upcoming = (bookings ?? []).filter((b) => !b.cancelled_at);
  const idOf = (booking: Booking) => booking.id ?? booking.booking_id ?? "";

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Calendar</h1>
          <p>
            Every meeting on this page was booked by the agent, out of the same calendar tool it
            offers times from. It will not sell the same slot twice — that is a unique index, not
            a promise.
          </p>
        </div>
        <button className="btn" onClick={() => void load()}>
          Refresh
        </button>
      </div>

      {bookings === null ? (
        <p className="tiny muted">Reading the calendar…</p>
      ) : failed ? (
        <div className="empty-card">
          <h3>The calendar server is not answering</h3>
          <p className="tiny muted">
            It is started by the API as a subprocess. If the API is running, check its log — a
            tool server that will not start degrades the call rather than ending it, so this is
            the only place it shows.
          </p>
        </div>
      ) : upcoming.length === 0 ? (
        <div className="empty-card">
          <h3>Nothing booked yet</h3>
          <p className="tiny muted">
            Start a call, ask for a person, and pick one of the times she offers. It lands here.
          </p>
        </div>
      ) : (
        <div className="diary">
          {byDay(upcoming).map(([day, entries]) => (
            <section key={day}>
              <h2 className="diary-day">{dayLabel(day)}</h2>
              {entries.map((booking) => (
                <article className="meeting" key={idOf(booking)}>
                  <div className="meeting-when">
                    <b>{clock(booking.starts_at)}</b>
                    {booking.ends_at && (
                      <span className="tiny muted">until {clock(booking.ends_at)}</span>
                    )}
                  </div>
                  <div className="meeting-who">
                    <b>{booking.attendee_name || booking.attendee_email}</b>
                    {booking.company && <span className="tag">{booking.company}</span>}
                    <p className="tiny muted">{booking.attendee_email}</p>
                    {booking.notes && <p className="meeting-notes">{booking.notes}</p>}
                  </div>
                  <button
                    className="btn"
                    disabled={busy === idOf(booking)}
                    onClick={() => void cancel(idOf(booking))}
                  >
                    {busy === idOf(booking) ? "Cancelling…" : "Cancel"}
                  </button>
                </article>
              ))}
            </section>
          ))}
        </div>
      )}
    </div>
  );
}
