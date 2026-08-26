"""What the payment server is and is not allowed to do.

THE INVARIANTS HERE ARE THE ONES WORTH BREAKING A BUILD OVER. An agent that invents a meeting
wastes a slot; an agent that invents an amount takes somebody's money. So: the amount is a
number the caller computed, nothing above the autonomous ceiling goes through without a person,
a checkout is always attached to a buyer, and nothing in this repository ever touches a card.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def payments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The server module, pointed at a database of its own.

    Reimported per test because the provider is chosen at import time from the environment,
    which is the same way it will behave in a deployment: a key appears, the process restarts,
    the provider changes.
    """
    monkeypatch.setenv("RAINMAKER_PAYMENTS_DB", str(tmp_path / "payments.sqlite3"))
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    module = importlib.import_module("rainmaker.mcp.servers.payments")
    return importlib.reload(module)


class TestTheAmountIsNeverTheModels:
    def test_a_checkout_carries_the_amount_it_was_given(self, payments: Any):
        made = payments.create_checkout(
            amount=255_000, currency="usd", description="Business, 40 seats",
            email="dana@corvusdata.io", period="month",
        )
        assert made["created"]
        assert made["amount"] == 255_000
        assert made["amount_display"] == "$2,550"

    @pytest.mark.parametrize("amount", [0, -1, 0.5, "2550"])
    def test_anything_that_is_not_a_positive_whole_number_of_minor_units_is_refused(
        self, payments: Any, amount: Any
    ):
        """Minor units, always. A float here is somebody who meant dollars, and charging
        $2,550 as if it were 2,550 cents is the cheap version of the mistake."""
        with pytest.raises(ValueError):
            payments.create_checkout(amount=amount, email="dana@corvusdata.io")

    def test_an_unsupported_currency_is_refused(self, payments: Any):
        with pytest.raises(ValueError):
            payments.create_checkout(amount=1000, currency="xyz", email="dana@corvusdata.io")

    def test_a_checkout_with_nobody_attached_is_refused(self, payments: Any):
        """It cannot be reconciled, refunded or chased, and it is almost always a bug upstream
        rather than a buyer who declined to say who they are."""
        with pytest.raises(ValueError):
            payments.create_checkout(amount=1000, email="")


class TestTheCeilingOnWhatAnAgentMaySell:
    def test_a_large_amount_is_reported_rather_than_charged(self, payments: Any):
        """Every real payment system has a figure above which a person signs. Reported, not
        raised: the agent has to say something true and keep going, and what it should say is
        that somebody will set it up with them."""
        answer = payments.create_checkout(
            amount=payments.MAX_AUTONOMOUS_AMOUNT + 1, email="dana@corvusdata.io"
        )
        assert answer["created"] is False
        assert answer["reason"] == "above_autonomous_limit"
        assert "someone" in answer["spoken"]

    def test_the_ceiling_itself_still_goes_through(self, payments: Any):
        answer = payments.create_checkout(
            amount=payments.MAX_AUTONOMOUS_AMOUNT, email="dana@corvusdata.io"
        )
        assert answer["created"]


class TestTheMockIsExercisableEndToEnd:
    """A payment step nobody can click through is a payment step nobody has debugged."""

    def test_a_checkout_can_be_looked_up_and_paid(self, payments: Any):
        made = payments.create_checkout(
            amount=4_000, email="dana@corvusdata.io", description="Team, 1 seat"
        )
        checkout_id = made["checkout_id"]

        assert payments.checkout_status(checkout_id)["status"] == "open"
        assert payments.mark_paid(checkout_id)["paid"]
        assert payments.checkout_status(checkout_id)["status"] == "paid"

    def test_paying_twice_does_not_take_twice(self, payments: Any):
        made = payments.create_checkout(amount=4_000, email="dana@corvusdata.io")
        payments.mark_paid(made["checkout_id"])
        assert payments.mark_paid(made["checkout_id"]) == {
            "paid": False, "reason": "not_open", "checkout_id": made["checkout_id"],
        }

    def test_an_unknown_checkout_is_not_found_rather_than_an_error(self, payments: Any):
        assert payments.checkout_status("co_nope") == {"found": False, "checkout_id": "co_nope"}

    def test_checkouts_can_be_listed_for_one_buyer(self, payments: Any):
        payments.create_checkout(amount=1_000, email="dana@corvusdata.io")
        payments.create_checkout(amount=2_000, email="someone@else.io")
        listed = payments.list_checkouts(email="dana@corvusdata.io")
        assert listed["count"] == 1
        assert listed["checkouts"][0]["email"] == "dana@corvusdata.io"


class TestWhatTheRealProviderChanges:
    def test_only_the_processor_may_say_a_real_checkout_was_paid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A tool that can declare a real checkout paid grants subscriptions nobody paid for,
        and it is reachable by anything holding the tool server."""
        monkeypatch.setenv("RAINMAKER_PAYMENTS_DB", str(tmp_path / "p.sqlite3"))
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_notreal")
        payments = importlib.reload(importlib.import_module("rainmaker.mcp.servers.payments"))
        try:
            with pytest.raises(ValueError, match="webhook"):
                payments.mark_paid("co_anything")
        finally:
            monkeypatch.delenv("STRIPE_SECRET_KEY")
            importlib.reload(payments)


class TestNothingHereTouchesACard:
    def test_the_server_exposes_no_way_to_send_card_details(self, payments: Any):
        """Card data never reaching this product is what keeps it out of PCI scope, and the
        cheapest way to guarantee that is to have no parameter that could carry it."""
        import inspect

        forbidden = ("card", "cvc", "cvv", "expiry", "exp_month", "card_number")
        for name in ("create_checkout", "checkout_status", "mark_paid", "list_checkouts"):
            parameters = inspect.signature(getattr(payments, name)).parameters
            assert not [p for p in parameters if any(word in p.lower() for word in forbidden)]
