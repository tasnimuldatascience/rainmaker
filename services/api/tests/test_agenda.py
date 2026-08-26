"""The call's plot: the front door, the steps, and which sentences the model is not trusted with.

WHAT IS ACTUALLY AT STAKE HERE. The agenda decides when a tool fires and which words are fixed,
and the split is the design: a model that phrases a question awkwardly costs a moment, a model
that decides on its own to confirm a meeting books nothing and promises everything. So the tests
that matter are the ones asserting the model was NOT consulted — for the times offered, for the
booking confirmation, for the handoff.

The tool layer here is a fake. `test_mcp.py` proves the protocol reaches real servers; repeating
that per agenda test would put four subprocess spawns behind every assertion about a state
machine.
"""

from __future__ import annotations

from typing import Any

import pytest

from rainmaker.calls.agenda import Agenda, Panel, Phase, Step, detect_intent, read_marker
from rainmaker.calls.intake import FREE_PROVIDERS, InvalidEmail, parse_contact
from rainmaker.calls.naming import clean_company_name
from rainmaker.calls.pipeline import CallPipeline, Spoke
from rainmaker.calls.providers import ClientSpeechToText, ScriptedLanguageModel, SilentTextToSpeech
from rainmaker.calls.session import CallSession, facts_from_enrichment


# ───────────────────────────────────────────────────────────── the front door
class TestTheEmailIsTheColdStart:
    def test_a_work_address_yields_a_domain_to_research(self):
        contact = parse_contact("dana.whitfield@corvusdata.io")
        assert contact.domain == "corvusdata.io"
        assert contact.researchable

    def test_the_first_name_is_guessed_only_when_it_looks_like_one(self):
        assert parse_contact("dana.whitfield@acme.dev").first_name == "Dana"
        # "Hi, Info" is a worse opening than asking.
        assert parse_contact("info@acme.dev").first_name == ""
        assert parse_contact("dw@acme.dev").first_name == ""

    def test_a_personal_address_is_accepted_but_not_researched(self):
        """A demo that refuses gmail turns away half the people who try it — including every
        founder using a personal address. Researching it would have her open Google's marketing
        site and describe it back to them."""
        contact = parse_contact("someone@gmail.com")
        assert contact.personal
        assert not contact.researchable
        assert contact.company_guess == ""

    @pytest.mark.parametrize("provider", sorted(FREE_PROVIDERS)[:6])
    def test_the_common_free_providers_are_known(self, provider: str):
        assert not parse_contact(f"a.person@{provider}").researchable

    def test_a_throwaway_address_is_not_researched_either(self):
        assert not parse_contact("test@mailinator.com").researchable

    def test_plus_tags_and_long_tlds_are_not_rejected(self):
        """A stricter pattern refuses real addresses, and the cost of being wrong here is
        turning a prospect away at the front door."""
        assert parse_contact("dana+demo@corvus.engineering").domain == "corvus.engineering"

    def test_nonsense_is_refused_with_a_sentence(self):
        """Someone is watching a form. The failure has to be sayable, not a validation code."""
        with pytest.raises(InvalidEmail) as caught:
            parse_contact("dana at corvus")
        assert "email address" in caught.value.spoken.lower()

    def test_the_domain_is_normalised(self):
        assert parse_contact("a@WWW.Corvus.IO").domain == "corvus.io"


class TestTheNameSheSaysOutLoud:
    @pytest.mark.parametrize(
        "title,expected",
        [
            ("Home \\ Anthropic", "Anthropic"),
            ("Plans & Pricing | Claude by Anthropic", "Claude by Anthropic"),
            ("Stripe | Financial Infrastructure", "Stripe"),
            ("About us | Northgate Dental", "Northgate Dental"),
            ("Acme Inc", "Acme Inc"),
        ],
    )
    def test_a_title_becomes_a_name(self, title: str, expected: str):
        assert clean_company_name(title) == expected

    def test_a_tagline_does_not_win(self):
        """Preferring the LONGEST segment seems right and turns this into a tagline. Names are
        short; taglines are long."""
        assert clean_company_name("Corvus Data — Analytics for logistics") == "Corvus Data"

    def test_nothing_usable_falls_back_rather_than_saying_something_absurd(self):
        assert clean_company_name("Welcome", fallback="Corvusdata") == "Corvusdata"
        assert clean_company_name("", fallback="Corvusdata") == "Corvusdata"


# ───────────────────────────────────────────────────────────── moving the call along
class TestWhatMovesTheCall:
    @pytest.mark.parametrize(
        "said,step",
        [
            ("so how much is it", Step.PRICING),
            ("what does it cost for a team our size", Step.PRICING),
            ("can we book something next week", Step.BOOKING),
            ("let's schedule a call", Step.BOOKING),
            ("show me what it looks like", Step.SHOWING),
        ],
    )
    def test_an_explicit_ask_outranks_the_plan(self, said: str, step: Step):
        """Someone who asks the price during discovery is telling you what the call is about
        now. Matched before the model is consulted, because guessing here is expensive."""
        assert detect_intent(said) is step

    def test_ordinary_conversation_does_not_jump_the_call(self):
        assert detect_intent("we have about forty inbound calls a week") is None

    def test_a_marker_never_reaches_the_synthesiser(self):
        """The bug this prevents only exists once there is audio: an agent saying "double
        bracket booking double bracket" out loud."""
        spoken, step = read_marker("That makes sense. [[booking]]")
        assert spoken == "That makes sense."
        assert step is Step.BOOKING

    def test_an_invented_marker_is_ignored_rather_than_obeyed(self):
        spoken, step = read_marker("Sure. [[teleport]]")
        assert step is None
        assert "[[" not in spoken


# ───────────────────────────────────────────────────────────── the agenda itself
class FakeTools:
    """Records what was called, and answers with the shape the real servers answer with."""

    def __init__(self, **overrides: Any):
        self.calls: list[tuple[str, dict]] = []
        self.overrides = overrides

    def has(self, qualified: str) -> bool:
        return True

    async def call(self, qualified: str, arguments: dict | None = None, **_: Any) -> Any:
        self.calls.append((qualified, arguments or {}))
        if qualified in self.overrides:
            answer = self.overrides[qualified]
            if isinstance(answer, Exception):
                raise answer
            return answer
        return _DEFAULTS.get(qualified, {})

    def named(self, qualified: str) -> list[dict]:
        return [args for name, args in self.calls if name == qualified]


_SLOTS = [
    {
        "starts_at": "2099-01-05T09:00:00+00:00",
        "ends_at": "2099-01-05T09:30:00+00:00",
        "spoken": "Tuesday the fifth at nine in the morning, U T C",
    },
    {
        "starts_at": "2099-01-05T09:30:00+00:00",
        "ends_at": "2099-01-05T10:00:00+00:00",
        "spoken": "Tuesday the fifth at nine thirty in the morning, U T C",
    },
]

_DEFAULTS: dict[str, Any] = {
    "research.research_company": {
        "name": {"value": "Corvus Data", "provenance": "observed"},
        "description": {"value": "Analytics for logistics fleets", "provenance": "observed"},
        "tech": [{"name": "ClickHouse"}],
        "size": {"value": "mid", "provenance": "derived"},
        "pages_fetched": ["https://corvus.example/"],
        "score": 0.6,
    },
    "research.pages_worth_showing": {
        "pages": [
            {"label": "pricing", "url": "https://corvus.example/pricing", "title": "Pricing"},
        ]
    },
    "research.browse": {
        "url": "https://corvus.example/pricing",
        "title": "Pricing — Corvus Data",
        "text": "Starter 49 dollars. Growth 199 dollars. Talk to sales for enterprise.",
        "frame_jpeg_base64": "ZmFrZQ==",
    },
    "calendar.list_availability": {"slots": _SLOTS, "duration_minutes": 30},
    "calendar.book_meeting": {
        "booking_id": "mtg_test",
        "confirmed": True,
        "spoken": _SLOTS[0]["spoken"],
        "starts_at": _SLOTS[0]["starts_at"],
    },
    "crm.record_call_outcome": {"ops_written": 4},
    "crm.log_call": {"call_id": "call_test", "ops_written": 5},
    "email.draft_recap": {"subject": "Following up", "body": "Hi Dana,", "can_send": False},
}


def build(email: str = "dana.whitfield@corvus.example", spec: Any = None, **overrides: Any):
    """A call, carrying an agent spec the way a real one does.

    Defaults to tenant zero. A session without a spec is a legitimate state — the plain `start`
    call uses one — but it is not what a configured agent looks like, and the tests about what
    she may say and charge need the configured shape.
    """
    from rainmaker.agents.store import liv_spec

    spec = liv_spec() if spec is None else spec
    stt = ClientSpeechToText()
    session = CallSession(
        CallPipeline(stt=stt, llm=ScriptedLanguageModel(ms_per_word=0), tts=SilentTextToSpeech()),
        stt,
        spec=spec,
    )
    tools = FakeTools(**overrides)
    return Agenda(session, tools, parse_contact(email)), tools


async def collect(stream) -> list[Any]:
    return [event async for event in stream]


def spoken(events: list[Any]) -> str:
    return " ".join(e.clip.text for e in events if isinstance(e, Spoke))


def panels(events: list[Any], kind: str) -> list[Panel]:
    return [e for e in events if isinstance(e, Panel) and e.kind == kind]


class TestOpeningTheCall:
    async def test_she_researches_before_she_greets(self):
        """The ordering IS the product: a rep who has read your site before calling is the thing
        being sold, so the demo does it in front of you rather than claiming it afterwards."""
        agenda, tools = build()
        await collect(agenda.begin())
        assert tools.named("research.research_company"), "she greeted without reading anything"

    async def test_she_says_something_before_a_ten_second_silence(self):
        """Reading a real site is six page loads. Ten seconds of silence after someone types
        their email is the point at which they close the tab."""
        agenda, _ = build()
        events = await collect(agenda.begin())
        assert "moment" in spoken(events).lower()

    async def test_the_disclosure_still_comes_first(self):
        agenda, _ = build()
        assert "not a human" in spoken(await collect(agenda.begin())).lower()

    async def test_what_research_found_goes_on_screen(self):
        agenda, _ = build()
        found = panels(await collect(agenda.begin()), "facts")
        assert found and found[0].data["company"] == "Corvus Data"
        assert any("ClickHouse" in fact for fact in found[0].data["facts"])

    async def test_a_personal_address_skips_research_and_says_so(self):
        agenda, tools = build("someone@gmail.com")
        events = await collect(agenda.begin())
        assert not tools.named("research.research_company")
        assert panels(events, "note")

    async def test_a_site_that_will_not_load_does_not_end_the_call(self):
        agenda, _ = build(**{"research.research_company": RuntimeError("dns")})
        events = await collect(agenda.begin())
        assert agenda.step is Step.OPENING
        assert panels(events, "note")


class TestShowingThemTheirOwnSite:
    async def test_a_page_is_opened_and_put_on_screen(self):
        agenda, tools = build()
        await collect(agenda.begin())
        events = await collect(agenda.respond("show me what it looks like"))

        assert tools.named("research.browse"), "she talked about a page without opening it"
        shots = panels(events, "browser")
        assert [p.data["state"] for p in shots] == ["opening", "open"]
        assert shots[1].data["frame"], "no picture reached the stage"

    async def test_the_model_is_told_what_is_on_the_screen(self):
        """Given only a URL it would describe a page it has not seen, and the narration would
        drift from the picture the prospect is looking at."""
        agenda, _ = build()
        await collect(agenda.begin())
        llm = agenda.session.pipeline.llm
        await collect(agenda.respond("show me what it looks like"))
        assert any("49 dollars" in prompt for prompt in llm.calls)

    async def test_the_same_page_is_not_shown_twice(self):
        """A slideshow of six tabs is a screen share nobody follows."""
        agenda, tools = build()
        await collect(agenda.begin())
        await collect(agenda.respond("show me what it looks like"))
        await collect(agenda.respond("show me another"))
        assert len(tools.named("research.browse")) == 1


class TestTheSentencesTheModelIsNotTrustedWith:
    async def test_the_times_offered_come_from_the_calendar_verbatim(self):
        """A model that invents "how about Thursday?" has promised a slot nobody holds."""
        agenda, _ = build()
        await collect(agenda.begin())
        events = await collect(agenda.respond("can we book something"))

        said = spoken(events)
        assert "Tuesday the fifth at nine in the morning" in said
        assert panels(events, "slots")

    async def test_the_offer_never_reaches_the_model(self):
        agenda, _ = build()
        await collect(agenda.begin())
        before = len(agenda.session.pipeline.llm.calls)
        await collect(agenda.respond("can we book something"))
        assert len(agenda.session.pipeline.llm.calls) == before

    async def test_confirming_a_slot_books_it_and_reads_back_the_tools_words(self):
        agenda, tools = build()
        await collect(agenda.begin())
        await collect(agenda.respond("can we book something"))
        events = await collect(agenda.confirm_slot(0))

        booked = tools.named("calendar.book_meeting")
        assert booked and booked[0]["attendee_email"] == "dana.whitfield@corvus.example"
        assert "Tuesday the fifth at nine in the morning" in spoken(events)
        assert panels(events, "booking")

    async def test_a_slot_taken_in_the_meantime_is_relayed_in_the_tools_own_words(self):
        """The tool knows whether the slot was taken or the time had passed, and those are
        different apologies."""
        agenda, _ = build(
            **{
                "calendar.book_meeting": {
                    "confirmed": False,
                    "reason": "slot_taken",
                    "spoken": "Someone just took that one — I have other times.",
                }
            }
        )
        await collect(agenda.begin())
        await collect(agenda.respond("can we book something"))
        events = await collect(agenda.confirm_slot(0))
        assert "Someone just took that one" in spoken(events)
        assert agenda.booking is None

    async def test_a_dead_calendar_does_not_promise_anything(self):
        agenda, _ = build(**{"calendar.list_availability": RuntimeError("no server")})
        await collect(agenda.begin())
        events = await collect(agenda.respond("can we book something"))
        said = spoken(events).lower()
        assert "send you times" in said
        assert agenda.offered == []

    async def test_picking_a_slot_that_was_never_offered_does_nothing(self):
        agenda, tools = build()
        await collect(agenda.begin())
        await collect(agenda.confirm_slot(7))
        assert not tools.named("calendar.book_meeting")


class TestPriceIsShownAndNotSpoken:
    async def test_the_figures_go_on_screen(self):
        agenda, _ = build()
        await collect(agenda.begin())
        events = await collect(agenda.respond("how much does it cost?"))
        priced = panels(events, "pricing")
        assert priced and priced[0].data["tiers"]

    async def test_no_figure_is_ever_said_out_loud(self):
        """On screen a number is a reference. Spoken on a sales call it is a quote, and this one
        would be a 1.5B model's guess."""
        agenda, _ = build()
        await collect(agenda.begin())
        said = spoken(await collect(agenda.respond("how much does it cost?")))
        assert "$" not in said
        assert "40" not in said and "75" not in said


class TestHowACallEnds:
    async def test_asking_for_a_person_ends_the_sell_and_writes_it_down(self):
        agenda, tools = build()
        await collect(agenda.begin())
        events = await collect(agenda.respond("can I just talk to a real person"))

        assert agenda.step is Step.HANDOFF
        assert "bring someone in" in spoken(events).lower()
        outcomes = tools.named("crm.record_call_outcome")
        assert outcomes and outcomes[0]["outcome"] == "handed_off"

    async def test_the_call_is_written_to_the_pipeline_after_the_talking(self):
        """Mid-call it would put a database round trip inside the latency budget for no benefit
        — nobody reads the pipeline while the call is still happening."""
        agenda, tools = build()
        await collect(agenda.begin())
        await collect(agenda.respond("can I just talk to a real person"))
        logged = tools.named("crm.log_call")
        assert logged and logged[0]["transcript"]
        assert "Prospect: can I just talk to a real person" in logged[0]["transcript"]

    async def test_a_follow_up_is_drafted_even_though_sending_is_off(self):
        agenda, tools = build()
        await collect(agenda.begin())
        events = await collect(agenda.respond("can I just talk to a real person"))
        assert tools.named("email.draft_recap")
        assert panels(events, "draft")

    async def test_a_crm_that_is_down_does_not_break_a_call_that_already_happened(self):
        agenda, _ = build(**{"crm.record_call_outcome": RuntimeError("gone")})
        await collect(agenda.begin())
        events = await collect(agenda.respond("can I just talk to a real person"))
        assert any(isinstance(e, Phase) for e in events)


class TestTheCallCannotStall:
    async def test_discovery_runs_out_of_budget_and_moves_on(self):
        """Small models are happy to discover forever, and a demo that never reaches the price
        is a demo that failed."""
        agenda, _ = build()
        await collect(agenda.begin())
        assert agenda.step is Step.OPENING

        for _ in range(6):
            await collect(agenda.respond("we handle it by hand at the moment"))
        assert agenda.step is not Step.DISCOVERY


class TestTheOpeningCannotBeInterrupted:
    """Barge-in is right for a sentence about pricing and fatal for the disclosure.

    Cancelling `session.open()` half way leaves the disclosure undelivered, and `CallPipeline`
    then refuses every subsequent turn for the rest of the call — `DisclosureError: turn()
    before open()`. The enforcement was right; the caller was wrong.
    """

    async def test_words_typed_over_the_introduction_are_held(self):
        agenda, _ = build()
        assert agenda.defer("show me what it looks like") is True
        assert not agenda.ready

    async def test_they_are_answered_once_the_disclosure_has_landed(self):
        agenda, tools = build()
        agenda.defer("show me what it looks like")
        events = await collect(agenda.begin())

        assert agenda.opened
        # Held, then replayed: the page they asked for actually gets opened.
        assert tools.named("research.browse"), "the deferred message was dropped"
        assert panels(events, "browser")

    async def test_after_the_opening_barge_in_is_allowed_again(self):
        agenda, _ = build()
        await collect(agenda.begin())
        assert agenda.ready
        assert agenda.defer("interrupt me") is False

    async def test_the_research_is_protected_too_not_only_the_disclosure(self):
        """The window that the first fix left open: the disclosure lands at two seconds and the
        research runs until fifteen. Gating on the disclosure alone let a keen prospect cancel
        the research, and the model then invented a page it had never opened."""
        agenda, _ = build()
        agenda.opened = True          # as it is a moment after the disclosure is spoken
        assert agenda.defer("show me") is True, "research was left interruptible"


class TestSheSaysWhatSheIsBeforeAnythingElse:
    async def test_the_disclosure_is_the_very_first_thing_spoken(self):
        """It used to come after the holding line, so her opening words were "give me a moment,
        I'm having a look at your site" — said by something that had not yet mentioned it was a
        machine. "Before anything else" includes being helpful."""
        agenda, _ = build()
        said = spoken(await collect(agenda.begin())).lower()
        # Asserted on ORDER, not on the first clip: the disclosure is cut into chunks for
        # synthesis latency, so the first clip is "Hi," and proves nothing.
        assert "not a human" in said
        assert said.index("not a human") < said.index("moment"), said[:160]

    async def test_it_is_said_once(self):
        agenda, _ = build()
        said = spoken(await collect(agenda.begin()))
        assert said.lower().count("not a human") == 1

    async def test_the_greeting_is_handed_a_specific_fact_to_use(self):
        """Given nine facts a small model greets with "Hello! Nice to meet you." — generically,
        wasting the one moment where knowing something specific is worth anything."""
        agenda, _ = build()
        await collect(agenda.begin())
        assert any("ClickHouse" in prompt for prompt in agenda.session.pipeline.llm.calls)


class TestWhoseWebsiteItIs:
    async def test_the_prompt_names_the_company_whose_page_is_on_screen(self):
        """Given "you are showing them their own pricing page", the model said "you might have
        stumbled upon OUR pricing page" — claiming Stripe's page as Rainmaker's, to someone who
        works at Stripe."""
        agenda, _ = build()
        await collect(agenda.begin())
        await collect(agenda.respond("show me what it looks like"))

        narration = [p for p in agenda.session.pipeline.llm.calls if "screen-sharing" in p]
        assert narration, "the narration prompt did not survive"
        assert "Corvus Data" in narration[0]
        assert "not to Rainmaker" in narration[0]


class TestTheAudienceNeverHearsTheStageDirections:
    """The agenda steers the model with instructions — "(You are screen-sharing their pricing
    page, never call it 'our' page...)". Those went through the same path as a prospect's words,
    so they were emitted as `Heard`, rendered as the caption, and written into the transcript as
    something the prospect had said. The instructions to the actor were read out to the audience.
    """

    async def test_a_steering_prompt_is_not_reported_as_something_they_said(self):
        from rainmaker.calls.pipeline import Heard

        agenda, _ = build()
        events = await collect(agenda.begin())
        heard = [e.text for e in events if isinstance(e, Heard)]
        assert not any(text.startswith("(") for text in heard), heard

    async def test_a_steering_prompt_stays_out_of_the_transcript(self):
        agenda, _ = build()
        await collect(agenda.begin())
        said = [line for line in agenda.session.transcript]
        assert not any(
            turn["who"] == "prospect" and turn["text"].startswith("(") for turn in said
        ), said

    async def test_the_prospects_own_words_are_still_reported(self):
        from rainmaker.calls.pipeline import Heard

        agenda, _ = build()
        await collect(agenda.begin())
        events = await collect(agenda.respond("we get forty inbound calls a week"))
        heard = [e.text for e in events if isinstance(e, Heard)]
        assert "we get forty inbound calls a week" in heard

    async def test_a_stage_direction_mentioning_a_person_does_not_hand_over(self):
        """`_wants_human` runs on what the prospect says. Running it on a direction that
        contains "the person you are speaking to" ends the call mid-demo."""
        agenda, _ = build()
        await collect(agenda.begin())
        await collect(agenda.respond("show me what it looks like"))
        assert agenda.step is not Step.HANDOFF


class TestFactsSheIsNotAllowedToRepeat:
    """The research agent scrapes; scraping produces rubbish alongside facts. A wrong fact said
    confidently to the person who works there is the most expensive kind of wrong here.
    """

    def test_a_price_of_one_cent_is_dropped_not_hedged(self):
        """Observed against stripe.com: `published_price_usd = 0.01`, which is the cents half of
        "2.9% + $0.30". Reading that back to someone at Stripe is worse than knowing nothing."""
        facts = facts_from_enrichment({"published_price_usd": {"value": 0.01}})
        assert facts == []

    def test_a_plausible_price_survives(self):
        facts = facts_from_enrichment({"published_price_usd": {"value": 49.0}})
        assert facts and "49" in facts[0]

    def test_an_absurd_price_is_dropped(self):
        assert facts_from_enrichment({"published_price_usd": {"value": 9_999_999}}) == []

    def test_a_paragraph_is_not_an_open_role(self):
        """Observed: "AI is replatforming the global economy, Products and pricing Pricing Atlas
        Authorizatio" arrived as a job title from a careers page's link text."""
        facts = facts_from_enrichment(
            {
                "hiring": [
                    {"title": "AI is replatforming the global economy, Products and pricing"},
                    {"title": "Data Engineer"},
                ]
            }
        )
        joined = " ".join(facts)
        assert "Data Engineer" in joined
        assert "replatforming" not in joined
        assert "1 open roles" in joined, "the count must reflect what survived, not what was scraped"

    def test_a_real_title_with_a_slash_is_kept(self):
        """Filtering has to be narrow. "Engineer / Platform" is a real posting."""
        facts = facts_from_enrichment({"hiring": [{"title": "Senior Engineer / Platform"}]})
        assert "Senior Engineer / Platform" in " ".join(facts)


class TestSheScrollsToThePoint:
    """`browse` has taken a `scroll_to` since it was written and nothing passed one, so every
    page opened at the top and she narrated a viewport the prospect had to scroll past to check.
    """

    async def test_a_pricing_page_is_scrolled_to_the_pricing(self):
        agenda, tools = build()
        await collect(agenda.begin())
        await collect(agenda.respond("show me what it looks like"))

        browsed = tools.named("research.browse")
        assert browsed and browsed[0]["scroll_to"] == "pricing"

    async def test_the_phrase_comes_from_what_research_actually_found(self):
        """Guessing a phrase gets a page that does not contain it. These come from the facts, so
        the page demonstrably has them."""
        agenda, tools = build(
            **{
                "research.pages_worth_showing": {
                    "pages": [{"label": "careers", "url": "https://corvus.example/careers"}]
                },
                "research.research_company": {
                    "name": {"value": "Corvus Data"},
                    "hiring": [{"title": "Data Engineer"}, {"title": "Platform Engineer"}],
                    "pages_fetched": [],
                },
            }
        )
        await collect(agenda.begin())
        await collect(agenda.respond("show me what it looks like"))

        assert tools.named("research.browse")[0]["scroll_to"] == "Data Engineer"

    async def test_nothing_to_aim_at_is_not_an_error(self):
        """A miss costs nothing: `browse` treats an absent phrase as no scroll."""
        agenda, tools = build(
            **{
                "research.pages_worth_showing": {
                    "pages": [{"label": "about", "url": "https://corvus.example/about"}]
                },
                "research.research_company": {"name": {"value": "Corvus"}, "pages_fetched": []},
            }
        )
        await collect(agenda.begin())
        await collect(agenda.respond("show me what it looks like"))
        assert tools.named("research.browse")[0]["scroll_to"] == ""
