"""An MCP server that takes the money, without the agent ever seeing a card.

    python -m rainmaker.mcp.servers.payments

THE AGENT NEVER TOUCHES CARD DATA, AND THAT IS THE WHOLE ARCHITECTURE. It asks for a checkout
built from a quote and gets back a URL. The buyer enters their card on the processor's page, in
their own browser, on the processor's domain. Nothing here, nothing in the model's context, and
nothing in a transcript ever contains a card number — which keeps this product out of PCI scope
entirely rather than putting it in and managing it.

THE AMOUNT COMES FROM THE CALLER, WHICH COMES FROM `Quote`, WHICH IS ARITHMETIC. Not from the
conversation and not from the model. An agent that invents a meeting wastes a slot; an agent
that invents an amount takes somebody's money, and there is no version of that which is
recoverable by apologising.

TWO PROVIDERS, SAME SHAPE:

    mock    the default. Records the intent and returns a local checkout page, so a clone can
            run the whole call end to end -- quote, checkout, paid -- with no account anywhere.
            It never moves money and says so on the page.
    stripe  behind STRIPE_SECRET_KEY. Real Checkout Sessions. Selected only when a key exists,
            the same shape as FIRECRAWL_API_KEY in the research layer.

WHY THE MOCK IS NOT A STUB. A payment step nobody can exercise is a payment step nobody has
debugged. The mock persists intents to SQLite, enforces the same invariants as the real one, and
is what the tests run against.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

# Numbers as words for anything spoken aloud. Shared with the quote rather than reimplemented:
# two number-spellers drift, and the symptom is a price that is right on screen and wrong in the
# ear on one path only.
from ...agents.quoting import money_words as _spoken_money

DATA_DIR = Path(os.environ.get("RAINMAKER_DATA", "data"))
DB_PATH = Path(os.environ.get("RAINMAKER_PAYMENTS_DB", DATA_DIR / "payments.sqlite3"))

STRIPE_KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()

#: Where the mock's checkout page lives. A real page the buyer can open, so the flow is
#: demonstrable rather than described.
CHECKOUT_BASE = os.environ.get("RAINMAKER_CHECKOUT_BASE", "http://localhost:5173/checkout.html")

#: Nothing above this may be charged without a person. Not a technical limit: an agent that can
#: raise an unbounded charge is a headline, and every real payment system has a ceiling above
#: which a human signs.
#:
#: THE DEFAULT HAS TO CLEAR THE PRODUCT'S OWN LARGEST HONEST QUOTE, or the first thing anybody
#: sees is the agent refusing its own pricing page. £25,000 a month is well above a self-serve
#: subscription and well below the amount where a mistake is unrecoverable; a tenant selling
#: something bigger raises it deliberately, which is the point of it being a setting.
MAX_AUTONOMOUS_AMOUNT = int(os.environ.get("RAINMAKER_MAX_CHARGE", 25_000_00))

SUPPORTED_CURRENCIES = ("usd", "gbp", "eur")

server = MCPServer(
    "rainmaker-payments",
    instructions=(
        "Create a hosted checkout from an agreed quote. The agent never handles card details; "
        "it shows the link and the buyer pays on the processor's page."
    ),
)

_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS checkouts (
            id          TEXT PRIMARY KEY,
            provider    TEXT NOT NULL,
            amount      INTEGER NOT NULL,
            currency    TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            email       TEXT NOT NULL DEFAULT '',
            company     TEXT NOT NULL DEFAULT '',
            period      TEXT NOT NULL DEFAULT '',
            url         TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'open',
            created_at  TEXT NOT NULL,
            paid_at     TEXT
        )
        """
    )
    return conn


def _money(amount: int, currency: str) -> str:
    symbol = {"usd": "$", "gbp": "£", "eur": "€"}.get(currency.lower(), "")
    whole, minor = divmod(amount, 100)
    body = f"{whole:,}" if minor == 0 else f"{whole:,}.{minor:02d}"
    return f"{symbol}{body}" if symbol else f"{body} {currency.upper()}"


@server.tool(
    title="Create a checkout",
    description=(
        "A hosted checkout for an agreed amount. Returns a URL to show the buyer. The amount "
        "must come from a computed quote, never from the conversation."
    ),
)
def create_checkout(
    amount: int,
    currency: str = "usd",
    description: str = "",
    email: str = "",
    company: str = "",
    period: str = "",
) -> dict[str, Any]:
    """Args:
    amount: Minor units - cents, pence. From the quote, never from the model.
    currency: Three-letter code.
    description: What is being bought, for the buyer's benefit on the checkout page.
    email: The address captured at the start of the call.
    company: Their company, for the receipt.
    period: "month" or "year" for a subscription; empty for one-off.
    """
    if not isinstance(amount, int) or amount <= 0:
        raise ValueError(f"amount must be a positive number of minor units, got {amount!r}")
    if currency.lower() not in SUPPORTED_CURRENCIES:
        raise ValueError(f"unsupported currency {currency!r}")
    if "@" not in email:
        # A checkout with no buyer attached cannot be reconciled, refunded or chased.
        raise ValueError("a checkout needs the email captured at the start of the call")
    if amount > MAX_AUTONOMOUS_AMOUNT:
        # Reported rather than raised: the agent has to say something true and keep going, and
        # what it should say is that a person will handle it.
        return {
            "created": False,
            "reason": "above_autonomous_limit",
            "spoken": (
                "That's above what I can take on a call myself - let me get someone to set it "
                "up with you properly."
            ),
            "limit": MAX_AUTONOMOUS_AMOUNT,
        }

    checkout_id = f"co_{secrets.token_urlsafe(12)}"
    provider = "stripe" if STRIPE_KEY else "mock"
    url = (
        _stripe_checkout(checkout_id, amount, currency, description, email, period)
        if STRIPE_KEY
        else f"{CHECKOUT_BASE}?id={checkout_id}"
    )

    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO checkouts (id, provider, amount, currency, description, email,"
            " company, period, url, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,'open',?)",
            (
                checkout_id, provider, amount, currency.lower(), description, email,
                company, period, url, datetime.now(UTC).isoformat(),
            ),
        )

    return {
        "created": True,
        "checkout_id": checkout_id,
        "url": url,
        "provider": provider,
        "amount": amount,
        "currency": currency.lower(),
        "amount_display": _money(amount, currency),
        "period": period,
        "description": description,
        # Said by the platform, not composed by the model, because it names a sum — and said
        # in WORDS, because a synthesiser handed "$4,800" phonemises the symbol first and reads
        # "dollar four thousand eight hundred". See `agents.quoting.money_words`.
        "spoken": (
            f"That's {_spoken_money(amount, currency)}"
            + (f" a {period}" if period else "")
            + " - the checkout is on your screen."
        ),
        "test_mode": provider == "mock",
    }


@server.tool(
    title="Check a checkout",
    description="Whether a checkout has been paid. Safe to call repeatedly.",
)
def checkout_status(checkout_id: str) -> dict[str, Any]:
    """Args:
    checkout_id: The id returned by create_checkout.
    """
    with _lock, _connect() as conn:
        row = conn.execute("SELECT * FROM checkouts WHERE id = ?", (checkout_id,)).fetchone()
    if row is None:
        return {"found": False, "checkout_id": checkout_id}
    record = dict(row)
    record["found"] = True
    record["amount_display"] = _money(record["amount"], record["currency"])
    return record


@server.tool(
    title="Mark a checkout paid",
    description=(
        "Records payment for a mock checkout. Refuses when a real provider is configured, "
        "because there the processor's webhook is the only thing that may say this."
    ),
)
def mark_paid(checkout_id: str) -> dict[str, Any]:
    """Args:
    checkout_id: The id returned by create_checkout.
    """
    if STRIPE_KEY:
        # THE ONLY AUTHORITY ON PAYMENT IS THE PROCESSOR. A tool that can declare a real
        # checkout paid is a tool that can grant a subscription nobody paid for, and it would be
        # reachable by anything holding the tool server.
        raise ValueError(
            "a real provider is configured; payment is confirmed by the processor's webhook, "
            "not by this tool"
        )

    with _lock, _connect() as conn:
        cursor = conn.execute(
            "UPDATE checkouts SET status = 'paid', paid_at = ? WHERE id = ? AND status = 'open'",
            (datetime.now(UTC).isoformat(), checkout_id),
        )
        if cursor.rowcount == 0:
            return {"paid": False, "reason": "not_open", "checkout_id": checkout_id}
    return {"paid": True, "checkout_id": checkout_id}


@server.tool(title="List checkouts", description="Checkouts created, most recent first.")
def list_checkouts(email: str = "", limit: int = 20) -> dict[str, Any]:
    """Args:
    email: Only this buyer's checkouts.
    limit: How many to return.
    """
    sql = "SELECT * FROM checkouts"
    params: list[Any] = []
    if email:
        sql += " WHERE email = ?"
        params.append(email)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(max(1, min(limit, 200)))

    with _lock, _connect() as conn:
        rows = [dict(row) for row in conn.execute(sql, params)]
    for row in rows:
        row["amount_display"] = _money(row["amount"], row["currency"])
    return {"checkouts": rows, "count": len(rows), "provider": "stripe" if STRIPE_KEY else "mock"}


def _stripe_checkout(
    checkout_id: str, amount: int, currency: str, description: str, email: str, period: str
) -> str:
    """A real Stripe Checkout Session.

    NOT EXERCISED IN THIS REPOSITORY - there is no key here, so this request shape comes from
    Stripe's published API and has never had a response come back. Said plainly rather than
    discovered. The interface boundary is the tested part: the agent asks for a checkout and
    gets a URL, and which provider produced it changes nothing above this line.
    """
    import json
    import urllib.parse
    import urllib.request

    fields = {
        "mode": "subscription" if period else "payment",
        "success_url": f"{CHECKOUT_BASE}?id={checkout_id}&paid=1",
        "cancel_url": f"{CHECKOUT_BASE}?id={checkout_id}",
        "customer_email": email,
        "client_reference_id": checkout_id,
        "line_items[0][quantity]": "1",
        "line_items[0][price_data][currency]": currency.lower(),
        "line_items[0][price_data][unit_amount]": str(amount),
        "line_items[0][price_data][product_data][name]": description or "Subscription",
    }
    if period:
        fields["line_items[0][price_data][recurring][interval]"] = period

    request = urllib.request.Request(
        "https://api.stripe.com/v1/checkout/sessions",
        data=urllib.parse.urlencode(fields).encode(),
        headers={"Authorization": f"Bearer {STRIPE_KEY}"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)["url"]


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
