"""Agents as data: what a tenant may configure, what they may not, and how a version goes live.

WHAT THIS FILE IS REALLY ABOUT. Rainmaker sells this agent to other businesses, who point it at
their own buyers. So an agent is a row someone edits, and the interesting tests are not "can you
store a name" — they are the ones about the line between what a customer controls and what the
platform does. A tenant who can switch off the AI disclosure, or grant their agent a tool nobody
gave them, or put a price on screen that was never entered, is a liability the vendor inherits.

The other half is versioning. Publishing is a pointer move onto an immutable row, which is what
makes rollback a single call and what stops a publish changing an agent underneath somebody who
is mid-call with it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rainmaker.agents.spec import (
    MAX_KNOWLEDGE_CHARS,
    AgentSpec,
    Fact,
    Guardrails,
    SpecError,
    Tier,
)
from rainmaker.agents.store import LIV_AGENT, LIV_TENANT, AgentStore, liv_spec, seed


@pytest.fixture
def store(tmp_path: Path) -> AgentStore:
    return AgentStore(path=tmp_path / "agents.sqlite3")


def spec(**changes) -> AgentSpec:
    base = {"tenant": "acme", "agent_id": "sam", "name": "Sam", "company": "Acme"}
    return AgentSpec(**{**base, **changes})


# ───────────────────────────────────────────────────────────── the line
class TestWhatATenantMayNotDo:
    def test_the_disclosure_cannot_be_emptied(self):
        """Its wording is theirs — brand, jurisdiction, their lawyers. Its existence is not."""
        with pytest.raises(SpecError, match="cannot be empty"):
            Guardrails(disclosure="   ").validate()

    def test_a_disclosure_that_does_not_disclose_is_refused(self):
        """THE SUBTLE VERSION OF THE SAME ATTACK. "Hi, I'm Sam from Acme, how can I help?" is a
        non-empty disclosure that discloses nothing, and it would pass a check for emptiness
        while looking like compliance."""
        with pytest.raises(SpecError, match="must actually say"):
            Guardrails(disclosure="Hi, I'm Sam from Acme. How can I help today?").validate()

    @pytest.mark.parametrize(
        "wording",
        [
            "Heads up: you're speaking with an AI.",
            "I should say up front that I am not a human.",
            "This is an automated assistant — a bot, not a person.",
            "Quick note: I'm an artificial assistant.",
        ],
    )
    def test_real_rewordings_are_allowed(self, wording: str):
        """The check has to be narrow. A guard that rejects honest rewordings is a guard that
        gets worked around."""
        Guardrails(disclosure=wording).validate()

    def test_the_handoff_line_cannot_be_emptied(self):
        with pytest.raises(SpecError, match="handoff"):
            Guardrails(handoff_line="").validate()

    def test_prices_cannot_be_switched_to_spoken(self):
        """On screen a number is a reference. The same number said aloud on a sales call is a
        quote, and it would be a 1.5B model reading it."""
        with pytest.raises(SpecError, match="shown, never spoken"):
            Guardrails(speak_prices=True).validate()

    def test_a_turn_cannot_be_made_into_an_essay(self):
        with pytest.raises(SpecError, match="not an essay"):
            Guardrails(max_sentences=9).validate()

    def test_tools_are_granted_by_server_not_by_tool(self):
        """Naming individual tools means an agent silently loses a capability the day the server
        adds one."""
        with pytest.raises(SpecError, match="grant a server"):
            spec(tools=("calendar.book_meeting",)).validate()


class TestWhatATenantMustGetRight:
    def test_an_unknown_voice_is_caught_at_publish(self):
        """Not discovered when the agent opens its mouth in front of a customer."""
        with pytest.raises(SpecError, match="unknown voice"):
            spec(voice="morgan-freeman").validate()

    def test_an_agent_needs_a_name_and_a_company(self):
        with pytest.raises(SpecError, match="needs a name"):
            spec(name="  ").validate()
        with pytest.raises(SpecError, match="needs a company"):
            spec(company="").validate()

    def test_a_fact_longer_than_a_paragraph_is_refused(self):
        """Sixty of these share a prompt with the call rules and the prospect's research."""
        with pytest.raises(SpecError, match="characters"):
            spec(knowledge=(Fact(text="x" * (MAX_KNOWLEDGE_CHARS + 1)),)).validate()

    def test_an_empty_fact_is_refused(self):
        with pytest.raises(SpecError, match="not a fact"):
            spec(knowledge=(Fact(text="   "),)).validate()

    def test_identifiers_must_be_slugs(self):
        with pytest.raises(SpecError, match="tenant must be a slug"):
            spec(tenant="Acme Corp!").validate()


# ───────────────────────────────────────────────────────────── the knowledge
class TestWhatTheAgentMaySay:
    def test_knowledge_reaches_the_prompt_grouped_by_topic(self):
        """A model handed a flat list of sixty sentences answers from the first three."""
        text = spec(
            knowledge=(
                Fact("We are SOC 2 Type II.", topic="security"),
                Fact("Forty dollars a seat.", topic="pricing"),
                Fact("We integrate with Salesforce.", topic="integrations"),
            )
        ).knowledge_text()
        assert "SECURITY" in text and "PRICING" in text
        assert text.index("INTEGRATIONS") < text.index("PRICING") < text.index("SECURITY")

    def test_a_source_travels_with_its_claim(self):
        """A disputed sentence should be traceable to whoever wrote it, not argued about."""
        text = spec(knowledge=(Fact("Free for 30 days.", source="pricing page"),)).knowledge_text()
        assert "[pricing page]" in text

    def test_an_agent_with_no_knowledge_renders_nothing_rather_than_a_heading(self):
        """An empty heading invites the model to fill it in."""
        assert spec().knowledge_text() == ""


class TestPerStepWording:
    def test_a_tenant_can_reword_a_step(self):
        """What she says at each step differs between selling payments and selling dentistry.
        The SHAPE of the call does not, which is why the steps stay in code."""
        configured = spec(step_objectives=(("discovery", "Find out which clinic they run."),))
        assert configured.objective_for("discovery", "default") == "Find out which clinic they run."

    def test_an_unset_step_falls_back(self):
        assert spec().objective_for("discovery", "the default") == "the default"

    def test_an_empty_override_falls_back_rather_than_emptying_the_objective(self):
        configured = spec(step_objectives=(("discovery", "   "),))
        assert configured.objective_for("discovery", "the default") == "the default"


class TestTheToolGrant:
    def test_a_granted_server_is_reachable(self):
        assert spec(tools=("calendar",)).may_use("calendar.book_meeting")

    def test_an_ungranted_server_is_not(self):
        """The failure worth designing against is an agent reaching a tool nobody gave it."""
        assert not spec(tools=("calendar",)).may_use("crm.record_call_outcome")

    def test_an_agent_with_no_grants_can_reach_nothing(self):
        assert not spec(tools=()).may_use("calendar.book_meeting")


# ───────────────────────────────────────────────────────────── versions
class TestPublishing:
    def test_a_draft_does_not_answer_calls(self, store: AgentStore):
        store.save(spec())
        assert store.live("acme", "sam") is None

    def test_publishing_makes_a_version_live(self, store: AgentStore):
        saved = store.save(spec())
        store.publish("acme", "sam", saved.version)

        live = store.live("acme", "sam")
        assert live is not None and live.published and live.version == saved.version

    def test_versions_are_immutable_so_a_publish_cannot_change_history(self, store: AgentStore):
        first = store.save(spec(knowledge=(Fact("Forty dollars a seat."),)))
        store.publish("acme", "sam", first.version)
        store.save(first.bump(knowledge=(Fact("Fifty dollars a seat."),)))

        # v1 still says forty, whatever v2 says.
        assert "Forty" in store.version("acme", "sam", 1).knowledge_text()
        assert "Fifty" in store.version("acme", "sam", 2).knowledge_text()

    def test_publishing_v2_then_rolling_back_is_one_call(self, store: AgentStore):
        """A customer whose agent starts saying something wrong at four in the afternoon needs
        the previous version back in seconds, not a support ticket."""
        first = store.save(spec(knowledge=(Fact("Forty dollars a seat."),)))
        store.publish("acme", "sam", 1)
        store.save(first.bump(knowledge=(Fact("Free forever, actually."),)))
        store.publish("acme", "sam", 2)
        assert "Free forever" in store.live("acme", "sam").knowledge_text()

        store.publish("acme", "sam", 1)
        assert "Forty" in store.live("acme", "sam").knowledge_text()

    def test_a_bumped_version_is_a_draft_again(self, store: AgentStore):
        """Editing a live agent must not be the same action as deploying the edit."""
        first = store.save(spec())
        store.publish("acme", "sam", 1)
        assert first.bump(name="Samantha").published is False

    def test_publishing_something_that_does_not_exist_says_so(self, store: AgentStore):
        store.save(spec())
        with pytest.raises(SpecError, match="no version 7"):
            store.publish("acme", "sam", 7)

    def test_a_bad_spec_never_reaches_the_database(self, store: AgentStore):
        """Validation at write, so a misconfigured agent fails in front of the person who broke
        it rather than halfway through their customer's call."""
        with pytest.raises(SpecError):
            store.save(spec(voice="nonexistent"))
        assert store.versions("acme", "sam") == []


class TestTheEmbedKey:
    def test_a_key_is_minted_once_and_survives_new_versions(self, store: AgentStore):
        """It is pasted into a customer's website. Rotating it on every edit would break their
        page the first time they changed a sentence."""
        first = store.save(spec())
        second = store.save(first.bump(name="Samantha"))
        assert first.public_key and first.public_key == second.public_key

    def test_the_key_resolves_to_the_published_agent(self, store: AgentStore):
        saved = store.save(spec())
        store.publish("acme", "sam", saved.version)

        resolved = store.resolve(saved.public_key)
        assert resolved is not None and resolved.agent_id == "sam"

    def test_an_unpublished_agent_resolves_to_nothing(self, store: AgentStore):
        saved = store.save(spec())
        assert store.resolve(saved.public_key) is None

    def test_an_unknown_key_and_an_unpublished_one_answer_identically(self, store: AgentStore):
        """Telling a stranger which of the two it was is an enumeration oracle."""
        saved = store.save(spec())
        assert store.resolve(saved.public_key) == store.resolve("rk_not_a_real_key") is None

    def test_two_agents_never_share_a_key(self, store: AgentStore):
        one = store.save(spec(agent_id="sam"))
        two = store.save(spec(agent_id="alex", name="Alex"))
        assert one.public_key != two.public_key


class TestTenantIsolation:
    def test_two_tenants_can_hold_the_same_agent_id(self, store: AgentStore):
        store.save(spec(tenant="acme", agent_id="sam", company="Acme"))
        store.save(spec(tenant="corvus", agent_id="sam", company="Corvus"))
        store.publish("acme", "sam", 1)
        store.publish("corvus", "sam", 1)

        assert store.live("acme", "sam").company == "Acme"
        assert store.live("corvus", "sam").company == "Corvus"

    def test_listing_can_be_scoped_to_one_tenant(self, store: AgentStore):
        store.save(spec(tenant="acme"))
        store.save(spec(tenant="corvus"))
        assert len(store.list_agents("acme")) == 1
        assert len(store.list_agents()) == 2


# ───────────────────────────────────────────────────────────── tenant zero
class TestLivIsJustTheFirstRow:
    def test_our_own_agent_is_a_valid_configuration(self):
        """THE POINT OF THE WHOLE MODULE. If Liv were a special case she would drift, and we
        would be demonstrating something we do not sell. She goes through the same validation a
        customer's agent does."""
        liv_spec().validate()

    def test_seeding_publishes_her(self, store: AgentStore):
        seeded = seed(store)
        assert seeded.published
        assert store.live(LIV_TENANT, LIV_AGENT) is not None

    def test_seeding_twice_does_not_make_a_second_version(self, store: AgentStore):
        seed(store)
        seed(store)
        assert store.versions(LIV_TENANT, LIV_AGENT) == [1]

    def test_seeding_after_the_code_changed_publishes_the_change(self, store: AgentStore):
        """IDEMPOTENT IS NOT INERT. Liv is defined in code, so a new tour stop or a priced tier
        is a code change — and a seed that returned early whenever anything was live served
        whatever had been seeded first. The tour was empty and the comparison step fell through
        to the model, silently, on a database nobody thought to look at."""
        from dataclasses import replace

        from rainmaker.agents.store import liv_spec

        stale = store.save(replace(liv_spec(), tour=(), competitors=()))
        store.publish(stale.tenant, stale.agent_id, stale.version)

        seeded = seed(store)
        assert seeded.tour and seeded.competitors
        assert store.versions(LIV_TENANT, LIV_AGENT) == [1, 2]
        assert store.live(LIV_TENANT, LIV_AGENT).version == 2

    def test_a_key_survives_the_agent_being_upgraded(self, store: AgentStore):
        """The key is in a script tag on somebody's website. Reissuing it on every deploy would
        take every embedded agent offline."""
        from dataclasses import replace

        from rainmaker.agents.store import liv_spec

        stale = store.save(replace(liv_spec(), tour=()))
        store.publish(stale.tenant, stale.agent_id, stale.version)
        before = store.live(LIV_TENANT, LIV_AGENT).public_key
        assert seed(store).public_key == before

    def test_she_knows_about_the_things_the_readme_claims(self):
        """WHAT SHE SELLS IS WHAT THE PRODUCT DOES, not how it is built. An earlier version led
        with "the console is offline-first", which is a true sentence about a CRDT and not a
        reason anybody buys anything."""
        text = liv_spec().knowledge_text().lower()
        for claim in ("work email", "mcp", "ai before anything else", "at any hour"):
            assert claim in text, f"Liv cannot talk about {claim!r}"

    def test_she_does_not_pitch_the_architecture(self):
        text = liv_spec().knowledge_text().lower()
        assert "offline-first" not in text
        assert "crdt" not in text

    def test_she_can_quote_and_take_payment(self):
        """The funnel closes: a buyer ready at eleven at night should be able to finish."""
        spec = liv_spec()
        assert any(tier.unit_amount > 0 for tier in spec.pricing), "nothing is quotable"
        assert "payments" in spec.tools
        assert spec.tour and spec.competitors

    def test_a_quote_speaks_the_tenants_unit_not_ours(self):
        """The word "seat" was hard-coded into the sentence the agent reads out, which is how a
        platform finds out it was shaped around its first customer. A GPU cloud sells hours."""
        from rainmaker.agents.quoting import build_quote

        spec = AgentSpec(
            tenant="t", agent_id="a", currency="usd", pricing_period="month",
            pricing=(
                Tier("Reserved", "$2.40 / GPU-hour", "committed",
                     unit_amount=240, min_seats=100, unit_name="GPU-hour"),
            ),
        )
        spoken = build_quote(spec, said_seats=2_000).spoken()
        assert "2,000 GPU-hours" in spoken
        assert "per GPU-hour per month" in spoken
        assert "seat" not in spoken

    def test_the_seat_detector_listens_for_the_tenants_word(self):
        """"We need about two thousand GPU hours" states the quantity. A detector that only
        knows about headcount falls back to a guess on a sentence that told it the answer."""
        from rainmaker.agents.quoting import seats_from_conversation, unit_words

        spec = AgentSpec(
            tenant="t", agent_id="a",
            pricing=(Tier("Reserved", "$2.40", "", unit_amount=240, unit_name="GPU-hour"),),
        )
        words = unit_words(spec)
        assert seats_from_conversation("we need about 2,000 GPU hours a month", words) == 2_000
        assert seats_from_conversation("we closed forty deals last quarter", words) is None

    def test_the_second_tenant_is_a_different_business_not_a_reskin(self):
        """WHAT THE SECOND ROW IS FOR. A tenant that differs only in name and colour proves
        nothing; this one sells hours instead of seats, to engineers instead of sales teams,
        with its own tour and its own competitors — and is published through the same store."""
        import importlib.util

        root = Path(__file__).resolve().parents[3]
        spec_file = importlib.util.spec_from_file_location(
            "demo_embed", root / "scripts" / "demo-embed.py"
        )
        module = importlib.util.module_from_spec(spec_file)
        spec_file.loader.exec_module(module)

        theirs, ours = module.tessera(), liv_spec()
        theirs.validate()

        assert theirs.tenant != ours.tenant
        assert {t.unit_name for t in theirs.pricing if t.unit_amount} == {"GPU-hour"}
        assert theirs.tour and theirs.competitors
        # Their allow-list is theirs: no email server, so the agent cannot reach for one.
        assert "email" not in theirs.tools
        assert theirs.guardrails.disclosure != ours.guardrails.disclosure

    def test_her_prices_are_configured_rather_than_compiled(self):
        """They were a module-level constant in `agenda.py`, which meant a second tenant's
        pricing was a code change."""
        assert [t.name for t in liv_spec().pricing] == ["Team", "Business", "Enterprise"]

    def test_she_is_granted_the_tools_her_call_actually_uses(self):
        granted = liv_spec().tools
        for server in ("calendar", "crm", "research", "email", "payments"):
            assert server in granted


class TestRoundTripping:
    def test_a_spec_survives_being_stored_and_read_back(self, store: AgentStore):
        original = liv_spec()
        store.save(original)
        store.publish(LIV_TENANT, LIV_AGENT, 1)
        back = store.live(LIV_TENANT, LIV_AGENT)

        assert back.name == original.name
        assert back.knowledge_text() == original.knowledge_text()
        assert [t.price for t in back.pricing] == [t.price for t in original.pricing]
        assert back.guardrails.disclosure == original.guardrails.disclosure
        assert back.tools == original.tools

    def test_step_overrides_survive_the_round_trip(self, store: AgentStore):
        store.save(spec(step_objectives=(("discovery", "Ask which clinic they run."),)))
        store.publish("acme", "sam", 1)
        assert store.live("acme", "sam").objective_for("discovery", "x").startswith("Ask which")

    def test_a_tier_with_no_detail_round_trips(self, store: AgentStore):
        store.save(spec(pricing=(Tier("Solo", "$9"),)))
        store.publish("acme", "sam", 1)
        assert store.live("acme", "sam").pricing[0].detail == ""


class TestTheDisclosureGuardIsWideEnoughToBeHonest:
    """It started at five phrases and refused a dental practice's perfectly good disclosure —
    "I'm an automated assistant, not a person" — because it avoided the word "AI". A guard that
    rejects honest wording is a guard people route around, and the way they route around this
    one is by writing something that passes without disclosing.
    """

    @pytest.mark.parametrize(
        "wording",
        [
            "Hello — before we go on, I should say I'm an automated assistant, not a person.",
            "Quick note: you're speaking to a machine, not a member of staff.",
            "I'm a computer, not a human being — happy to help either way.",
            "Heads up, I'm a virtual assistant. Not a real person.",
            "This call is handled by a robot.",
            "I'm an AI.",
        ],
    )
    def test_honest_wordings_pass(self, wording: str):
        Guardrails(disclosure=wording).validate()

    @pytest.mark.parametrize(
        "wording",
        [
            "Hi, I'm Sam from Acme. How can I help today?",
            "I'm your assistant here at Acme.",
            "Welcome to Acme, you're through to the sales team.",
        ],
    )
    def test_wordings_that_disclose_nothing_are_still_refused(self, wording: str):
        """"Assistant" is about a ROLE, not about being a machine — a human can be one."""
        with pytest.raises(SpecError, match="must actually say"):
            Guardrails(disclosure=wording).validate()
