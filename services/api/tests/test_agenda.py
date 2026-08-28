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

from rainmaker.agents.spec import Fact
from rainmaker.agents.store import nadia_spec
from rainmaker.calls.agenda import Agenda, Panel, Phase, Step, detect_intent, read_marker
from rainmaker.calls.intake import FREE_PROVIDERS, IntakeError, parse_contact, parse_intake
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
        with pytest.raises(IntakeError) as caught:
            parse_contact("dana at corvus")
        assert "email address" in caught.value.spoken.lower()

    def test_the_domain_is_normalised(self):
        assert parse_contact("a@WWW.Corvus.IO").domain == "corvus.io"


class TestTheThreeFieldFrontDoor:
    """NAME, WORK EMAIL, COMPANY. One field was a nicer form and a worse call: the name is how
    she greets you, the company is what she talks about, and the address is what she reads
    before she says a word.
    """

    def test_the_form_produces_a_contact_she_can_open_with(self):
        contact = parse_intake("Dana Whitfield", "dana@corvusdata.io", "Corvus Data")
        assert contact.first_name == "Dana"
        assert contact.company == "Corvus Data"
        assert contact.domain == "corvusdata.io"
        assert contact.researchable

    def test_what_they_typed_beats_what_the_local_part_suggests(self):
        """`dwhitfield@` guesses nothing and `sales@` guesses wrong. A typed name is not a
        guess at all."""
        assert parse_intake("Dana", "dwhitfield@corvusdata.io", "Corvus").first_name == "Dana"
        assert parse_intake("Dana", "sales@corvusdata.io", "Corvus").first_name == "Dana"

    def test_a_personal_address_is_turned_away_with_the_reason(self):
        """The domain is the entire cold start and it is also the qualifying question. Somebody
        who will not give a business address is not a buyer worth a call, and a rep would have
        reached the same conclusion a minute later."""
        with pytest.raises(IntakeError) as caught:
            parse_intake("Sam Reed", "sam@gmail.com", "Corvus Data")
        assert caught.value.field == "email"
        assert "work email" in caught.value.spoken.lower()

    @pytest.mark.parametrize(
        "name,email,company,field",
        [
            ("", "dana@corvusdata.io", "Corvus", "name"),
            ("Dana", "not an address", "Corvus", "email"),
            ("Dana", "dana@corvusdata.io", "", "company"),
            ("Dana", "dana@mailinator.com", "Corvus", "email"),
        ],
    )
    def test_every_refusal_names_its_field_and_says_why(
        self, name: str, email: str, company: str, field: str
    ):
        with pytest.raises(IntakeError) as caught:
            parse_intake(name, email, company)
        assert caught.value.field == field
        assert caught.value.spoken.endswith(("?", ".")), caught.value.spoken

    @pytest.mark.parametrize(
        "typed,company",
        [
            ("Corvus Data", "Corvus Data"),
            ("corvusdata.io", "Corvusdata"),
            ("Corvus Data (corvusdata.io)", "Corvus Data"),
            ("https://www.corvusdata.io/about", "Corvusdata"),
        ],
    )
    def test_a_company_field_is_read_as_a_name_however_it_was_typed(
        self, typed: str, company: str
    ):
        """People answer "company" with a name, a website, or both. An agent addressing a
        company called "About" is the version where the URL was stripped carelessly."""
        assert parse_intake("Dana", "dana@corvusdata.io", typed).company == company

    def test_a_website_they_typed_wins_over_their_mail_domain(self):
        """Holding companies, agencies, and anyone whose mail domain is not their marketing
        domain. They know which site describes their business; the mail server does not."""
        contact = parse_intake("Dana", "dana@holdco.com", "Corvus Data (corvusdata.io)")
        assert contact.domain == "corvusdata.io"
        assert contact.domain_from_company


class TestTheNameSheSaysOutLoud:
    @pytest.mark.parametrize(
        "title,expected",
        [
            ("Home \\ Anthropic", "Anthropic"),
            ("Plans & Pricing | Claude by Anthropic", "Claude by Anthropic"),
            ("Stripe | Financial Infrastructure", "Stripe"),
            ("About us | Corvus Data", "Corvus Data"),
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
            ("so how much is it", Step.QUOTE),
            ("what does it cost for a team our size", Step.QUOTE),
            ("can we book something next week", Step.BOOKING),
            ("let's schedule a call", Step.BOOKING),
            ("show me what it looks like", Step.GUIDE),
            ("how do you compare to a chat widget", Step.COMPARE),
            ("alright, sign me up", Step.PAY),
        ],
    )
    def test_an_explicit_ask_outranks_the_plan(self, said: str, step: Step):
        """Someone who asks the price during discovery is telling you what the call is about
        now. Matched before the model is consulted, because guessing here is expensive."""
        assert detect_intent(said) is step

    async def test_a_tenants_own_vocabulary_moves_the_call(self):
        """FOUND BY DRIVING A REAL CALL. "What have you got available right now" is the one
        question a GPU cloud exists to answer, and the guide step never fired on it: the intent
        patterns know how buyers ask in general, and cannot know that "available" is a word
        about capacity. The tenant had already written it down in the tour stop's `answers`.
        """
        from dataclasses import replace

        from rainmaker.agents.spec import TourStop

        spec = replace(
            nadia_spec(),
            tour=(
                TourStop(
                    url="https://demo.example/#capacity",
                    label="what is free right now",
                    shows="live capacity",
                    answers=("available", "capacity", "region"),
                ),
            ),
        )
        agenda, tools = build(spec=spec)
        await collect(agenda.begin())
        await collect(agenda.respond("what have you got available right now?"))

        assert agenda.step is Step.GUIDE
        assert any("demo.example" in call["url"] for call in tools.named("research.browse"))

    async def test_naming_a_competitor_opens_the_comparison(self):
        agenda, _ = build()
        await collect(agenda.begin())
        events = await collect(agenda.respond("we already use a chat widget for this"))
        assert agenda.step is Step.COMPARE
        assert panels(events, "comparison")

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
        "spoken": "Tuesday at nine in the morning",
    },
    {
        "starts_at": "2099-01-05T09:30:00+00:00",
        "ends_at": "2099-01-05T10:00:00+00:00",
        "spoken": "Tuesday at nine thirty in the morning",
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
    "payments.create_checkout": {
        "created": True,
        "checkout_id": "co_test",
        "url": "http://localhost:5173/checkout.html?id=co_test",
        "provider": "mock",
        "amount": 255_000,
        "currency": "usd",
        "amount_display": "$2,550",
        "period": "month",
        "description": "Business, 40 seats",
        "test_mode": True,
    },
}


def build(email: str = "dana.whitfield@corvus.example", spec: Any = None, **overrides: Any):
    """A call, carrying an agent spec the way a real one does.

    Defaults to tenant zero. A session without a spec is a legitimate state — the plain `start`
    call uses one — but it is not what a configured agent looks like, and the tests about what
    she may say and charge need the configured shape.
    """
    spec = nadia_spec() if spec is None else spec
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

    async def test_a_contact_with_nothing_to_research_says_so(self):
        """The front door now insists on a work address, but the guard stays: the plain `start`
        call has no form in front of it, and researching gmail.com opens Google's marketing
        site and describes it back to them."""
        agenda, tools = build("someone@gmail.com")
        events = await collect(agenda.begin())
        assert not tools.named("research.research_company")
        assert panels(events, "note")

    async def test_a_site_that_will_not_load_does_not_end_the_call(self):
        agenda, _ = build(**{"research.research_company": RuntimeError("dns")})
        events = await collect(agenda.begin())
        # Research failed, she still opened, and the call is listening rather than stalled.
        assert agenda.step is Step.DISCOVERY
        assert panels(events, "note")
        assert agenda.opened


class TestShowingThemTheProduct:
    """THE TOUR DRIVES THE SELLER'S PRODUCT, NOT THE BUYER'S SITE, and the two are opposite
    directions through the same browser tool. Research opens the buyer's website to work out who
    they are. The guide opens ours to show them what they would be buying. For a while this code
    did the first and called it the second, so the demo consisted of narrating the prospect's own
    homepage back at them.
    """

    @staticmethod
    def tour_pages(tools: Any) -> list[str]:
        """Browse calls that are part of the demo, not part of the research."""
        return [
            call["url"]
            for call in tools.named("research.browse")
            if "corvus.example" not in call["url"]
        ]

    async def test_a_page_is_opened_and_put_on_screen(self):
        agenda, tools = build()
        await collect(agenda.begin())
        events = await collect(agenda.respond("show me what it looks like"))

        assert self.tour_pages(tools), "she talked about a page without opening it"
        shots = panels(events, "browser")
        assert [p.data["state"] for p in shots] == ["opening", "open"]
        assert shots[1].data["frame"], "no picture reached the stage"

    async def test_the_page_she_opens_is_ours_not_theirs(self):
        agenda, tools = build()
        await collect(agenda.begin())
        await collect(agenda.respond("show me what it looks like"))

        stops = {stop.url for stop in nadia_spec().tour}
        assert set(self.tour_pages(tools)) <= stops

    async def test_what_is_on_the_screen_is_stated_rather_than_inferred(self):
        """THE MODEL IS NO LONGER SHOWN THE PAGE, and that is the point. Given the page text it
        described it and got it wrong every time the wording changed — at 700 characters it
        narrated a demo customer's product as ours; cut to 240 it still read numbers off it and
        invented the rest ("392 GPUs for free, with a reservation period of a week", from a page
        that says 392 are available and nothing about a week).

        It cannot misread a page it has not been given, and it does not need one: what is on
        screen is said in the tenant's own words in the sentence immediately before."""
        agenda, _ = build()
        await collect(agenda.begin())
        llm = agenda.session.pipeline.llm
        said = spoken(await collect(agenda.respond("show me what it looks like")))

        stop = nadia_spec().tour[0]
        assert f"What you're looking at is {stop.shows}." in said, said

        narration = [p for p in llm.calls if "screen-sharing" in p][0]
        assert "49 dollars" not in narration, narration
        assert "the page reads" not in narration, narration

    async def test_asking_for_more_moves_the_tour_on(self):
        """A step that only runs on arrival answers "show me another" by talking about the page
        already on the screen."""
        agenda, tools = build()
        await collect(agenda.begin())
        await collect(agenda.respond("show me what it looks like"))
        await collect(agenda.respond("show me another"))

        seen = self.tour_pages(tools)
        assert len(seen) == 2, "the tour did not move on"
        assert len(set(seen)) == 2, "the same page twice is a screen share nobody follows"


class TestTheSentencesTheModelIsNotTrustedWith:
    async def test_the_times_offered_come_from_the_calendar_verbatim(self):
        """A model that invents "how about Thursday?" has promised a slot nobody holds."""
        agenda, _ = build()
        await collect(agenda.begin())
        events = await collect(agenda.respond("can we book something"))

        said = spoken(events)
        assert "Tuesday at nine in the morning" in said
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
        assert "Tuesday at nine in the morning" in spoken(events)
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


class TestTheNumberIsComputedNotGenerated:
    """A CALL THAT CANNOT SAY A PRICE CANNOT CLOSE, which is why this changed. The old rule was
    that no figure was ever spoken; a demo that ends in "someone will send you a quote" is a
    lead-generation agent, and this one is supposed to be able to finish. The rule that replaced
    it is narrower and stronger: the MODEL never produces a figure. The platform computes the
    quote and speaks its own sentence, the way the calendar reads out its own times.
    """

    async def test_the_quote_goes_on_screen_with_its_arithmetic(self):
        agenda, _ = build()
        await collect(agenda.begin())
        events = await collect(agenda.respond("how much does it cost for 40 people?"))

        quoted = panels(events, "quote")
        assert quoted, "she talked about price without putting the number on screen"
        data = quoted[0].data
        assert data["seats"] == 40
        assert data["seats_from"] == "said"
        # Every line a buyer would check before agreeing to it.
        assert data["unit_display"] and data["subtotal_display"] and data["total_display"]

    async def test_the_spoken_figure_is_the_platforms_sentence_verbatim(self):
        """The one place a number becomes a commitment is not where you want a 1.5B model
        paraphrasing."""
        agenda, _ = build()
        await collect(agenda.begin())
        events = await collect(agenda.respond("how much does it cost for 40 people?"))

        quote = panels(events, "quote")[0].data
        assert quote["spoken"] in spoken(events)

    async def test_the_model_is_not_asked_to_follow_a_quote_at_all(self):
        """IT ANSWERED AS THE BUYER. Told "ask in one sentence whether that works for them", the
        model produced "That works perfectly, thank you." — accepting its own quote on the
        seller's behalf, out loud, one sentence after the number.

        There is exactly one sentence that belongs after a price and it never varies, so the
        model is no longer consulted about it."""
        agenda, _ = build()
        await collect(agenda.begin())
        llm = agenda.session.pipeline.llm
        before = len(llm.calls)
        said = spoken(await collect(agenda.respond("how much does it cost?")))

        assert "How does that sit against what you had in mind?" in said, said
        assert not llm.calls[before:], llm.calls[before:]

    async def test_pricing_with_no_amounts_shows_the_tiers_and_quotes_nothing(self):
        """"Enterprise, quoted" is a legitimate answer. There is nothing to compute, so nothing
        goes on the screen with a number on it."""
        from dataclasses import replace

        from rainmaker.agents.spec import Tier

        unpriced = replace(nadia_spec(), pricing=(Tier("Enterprise", "quoted", "talk to us"),))
        agenda, _ = build(spec=unpriced)
        await collect(agenda.begin())
        events = await collect(agenda.respond("how much does it cost?"))

        assert not panels(events, "quote")
        priced = panels(events, "pricing")
        assert priced and priced[0].data["tiers"]
        assert "$" not in spoken(events)


class TestClosingTheDeal:
    """A BUYER READY AT ELEVEN AT NIGHT SHOULD BE ABLE TO FINISH. That is the whole reason the
    funnel goes past "book a meeting": the agent exists so nobody waits for a rep, and a call
    that can only end in a calendar invite has handed the buyer back to the queue it replaced.
    """

    async def test_the_checkout_is_built_from_the_quote_not_the_conversation(self):
        """An agent that invents a booking wastes a slot. An agent that invents an amount takes
        somebody's money."""
        agenda, tools = build()
        await collect(agenda.begin())
        quoted = await collect(agenda.respond("how much for 40 people?"))
        await collect(agenda.respond("great, sign me up"))

        asked = tools.named("payments.create_checkout")
        assert asked, "she agreed a sale and never opened a checkout"
        assert asked[0]["amount"] == panels(quoted, "quote")[0].data["total"]
        assert asked[0]["email"] == agenda.contact.email

    async def test_a_checkout_goes_on_screen_as_a_link(self):
        agenda, _ = build()
        await collect(agenda.begin())
        await collect(agenda.respond("how much for 40 people?"))
        events = await collect(agenda.respond("great, sign me up"))

        shown = panels(events, "checkout")
        assert shown and shown[0].data["url"].startswith("http")
        assert "card" in spoken(events).lower(), "she did not say she never asks for a card"

    async def test_nobody_is_asked_to_pay_for_a_number_they_have_not_seen(self):
        """A checkout for an amount nobody has agreed is how a sale becomes a chargeback."""
        agenda, tools = build()
        await collect(agenda.begin())
        await collect(agenda.respond("I'll take it"))

        assert agenda.step is Step.QUOTE
        assert not tools.named("payments.create_checkout")

    async def test_a_payment_server_that_is_down_offers_a_person_instead(self):
        agenda, _ = build(**{"payments.create_checkout": RuntimeError("no server")})
        await collect(agenda.begin())
        await collect(agenda.respond("how much for 40 people?"))
        events = await collect(agenda.respond("sign me up"))

        assert agenda.step is Step.BOOKING
        assert panels(events, "slots")

    async def test_a_paid_checkout_is_the_outcome_that_is_written_down(self):
        agenda, tools = build()
        await collect(agenda.begin())
        await collect(agenda.respond("how much for 40 people?"))
        await collect(agenda.respond("sign me up"))
        await collect(agenda.end())

        outcomes = tools.named("crm.record_call_outcome")
        assert outcomes and outcomes[0]["outcome"] == "checkout_sent"

    async def test_the_comparison_on_screen_is_the_tenants_words(self):
        """A comparison table a language model wrote about a named competitor is a defamation
        risk with a grid layout."""
        agenda, _ = build()
        await collect(agenda.begin())
        events = await collect(agenda.respond("how do you compare to a chat widget?"))

        shown = panels(events, "comparison")
        assert shown
        rivals = {r["name"]: r for r in shown[0].data["rivals"]}
        assert "a chat widget" in rivals
        configured = {c.name: c for c in nadia_spec().competitors}["a chat widget"]
        assert rivals["a chat widget"]["positioning"] == configured.positioning


class TestHowACallEnds:
    async def test_asking_for_a_person_stops_the_sell_and_offers_the_diary(self):
        """NOBODY IS IN THE ROOM TO HAND TO — that is the product. Asking for a person cannot
        mean a transfer, so it means a time with one: the fixed line is still said first, and
        what follows it is a diary rather than a promise."""
        agenda, _ = build()
        await collect(agenda.begin())
        events = await collect(agenda.respond("can I just talk to a real person"))

        assert agenda.step is Step.BOOKING
        assert agenda.wants_human
        said = spoken(events).lower()
        assert "bring someone in" in said
        assert "tuesday at nine in the morning" in said
        assert panels(events, "slots")

    async def test_a_call_that_wanted_a_person_is_written_down_as_one(self):
        agenda, tools = build()
        await collect(agenda.begin())
        await collect(agenda.respond("can I just talk to a real person"))
        await collect(agenda.end())

        outcomes = tools.named("crm.record_call_outcome")
        assert outcomes and outcomes[0]["outcome"] == "handed_off"

    async def test_the_call_is_written_to_the_pipeline_after_the_talking(self):
        """Mid-call it would put a database round trip inside the latency budget for no benefit
        — nobody reads the pipeline while the call is still happening."""
        agenda, tools = build()
        await collect(agenda.begin())
        await collect(agenda.respond("can I just talk to a real person"))
        assert not tools.named("crm.log_call"), "the database round trip landed inside the call"

        await collect(agenda.end())
        logged = tools.named("crm.log_call")
        assert logged and logged[0]["transcript"]
        assert "Prospect: can I just talk to a real person" in logged[0]["transcript"]

    async def test_a_follow_up_is_drafted_even_though_sending_is_off(self):
        agenda, tools = build()
        await collect(agenda.begin())
        await collect(agenda.respond("can I just talk to a real person"))
        events = await collect(agenda.end())
        assert tools.named("email.draft_recap")
        assert panels(events, "draft")

    async def test_a_crm_that_is_down_does_not_break_a_call_that_already_happened(self):
        agenda, _ = build(**{"crm.record_call_outcome": RuntimeError("gone")})
        await collect(agenda.begin())
        await collect(agenda.respond("can I just talk to a real person"))
        events = await collect(agenda.end())
        assert any(isinstance(e, Phase) for e in events)


class TestTheCallCannotStall:
    async def test_discovery_runs_out_of_budget_and_moves_on(self):
        """Small models are happy to discover forever, and a demo that never reaches the price
        is a demo that failed."""
        agenda, _ = build()
        await collect(agenda.begin())
        # The opening ends with the tenant's discovery question, so the call is already
        # listening for the answer by the time she stops talking. See `_hand_over`.
        assert agenda.step is Step.DISCOVERY

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

    async def test_the_greeting_is_handed_a_diagnosis_rather_than_a_fact(self):
        """RECITING RESEARCH IS NOT SELLING. Handed "mention one specific thing you found", a
        1.5B model reads the fact back — label, list and all. Observed on a real call: "I
        noticed you're currently hiring four positions: Production Associates, Product Manager,
        Research and Development Engineer, and Sales and Marketing Specialist", from an agent
        that rents GPUs.

        The prompt now carries what the finding MEANS for what this agent sells, and forbids
        reading it back."""
        from dataclasses import replace

        from rainmaker.agents.spec import Need

        spec = replace(
            nadia_spec(),
            needs=(
                Need(
                    signals=("ClickHouse", "analytics", "logistics"),
                    means="they run their own analytics stack and answer inbound themselves",
                    opener="it looks like you answer inbound yourselves",
                    ask="who picks up an inbound demo request today?",
                ),
            ),
        )
        agenda, _ = build(spec=spec)
        before = len(agenda.session.pipeline.llm.calls)
        said = spoken(await collect(agenda.begin()))

        # THE OPENING IS THE TENANT'S SENTENCE, not the model's. Every earlier version of this
        # test asserted on a prompt; there is no prompt now, which is the point.
        assert "it looks like you answer inbound yourselves" in said.lower(), said
        assert "who picks up an inbound demo request today?" in said.lower(), said
        assert not [p for p in agenda.session.pipeline.llm.calls[before:] if "Greet" in p]

    async def test_research_that_says_nothing_about_the_product_is_not_read_out(self):
        """An opening that recites irrelevant research is worse than one admitting it has none:
        it tells the buyer the reading was mechanical. Four job titles from a careers page, on a
        call about GPUs, is exactly that."""
        from dataclasses import replace

        from rainmaker.agents.spec import Need

        spec = replace(
            nadia_spec(),
            needs=(
                Need(signals=("kubernetes",), means="they run their own clusters",
                     opener="it looks like you run your own clusters",
                     ask="how much of that is yours to keep running?"),
            ),
        )
        agenda, _ = build(spec=spec)
        await collect(agenda.begin())

        opening = [p for p in agenda.session.pipeline.llm.calls if "Greet" in p]
        assert opening
        assert "do not mention what you read" in opening[0]
        assert "ClickHouse" not in opening[0]


class TestWhoseWebsiteItIs:
    """BOTH DIRECTIONS ARE A REAL MISTAKE, and the same tool makes both. Told "you are showing
    them their own pricing page", the model said "you might have stumbled upon OUR pricing page"
    — claiming Stripe's page as Rainmaker's, to someone who works at Stripe. Now that the tour
    drives our product instead, the inverse is available: narrating our own demo as though it
    were theirs. So the prompt names both companies and says which one owns the page.
    """

    async def test_what_is_on_the_screen_is_said_in_the_tenants_own_words(self):
        """THE ONE NARRATION JOB THE MODEL DOES NOT GET. Handed the page text it described the
        page and got it wrong — "a compute capacity service called Tessera, offered by
        Rainmaker", when Tessera is the example customer and not the product. Told not to
        describe the page it described the PROSPECT instead, from the research dossier, while
        our own product sat on screen behind it.

        `TourStop.shows` is right by construction, so it is spoken rather than paraphrased."""
        agenda, _ = build()
        await collect(agenda.begin())
        said = spoken(await collect(agenda.respond("show me what it looks like")))

        stop = nadia_spec().tour[0]
        assert f"What you're looking at is {stop.shows}." in said, said

    async def test_the_model_is_told_not_to_describe_the_screen_or_the_prospect(self):
        agenda, _ = build()
        await collect(agenda.begin())
        await collect(agenda.respond("show me what it looks like"))

        prompt = [p for p in agenda.session.pipeline.llm.calls if "screen-sharing" in p][0]
        assert "Rainmaker's own product with Corvus Data" in prompt, prompt
        assert "do NOT describe it again" in prompt, prompt
        assert "do NOT describe Corvus Data" in prompt, prompt

    async def test_no_page_text_reaches_the_narration_prompt_at_all(self):
        """Trimming the excerpt was tried twice and failed twice. The only size that cannot be
        misread is none."""
        agenda, _ = build()
        await collect(agenda.begin())
        await collect(agenda.respond("show me what it looks like"))

        prompt = [p for p in agenda.session.pipeline.llm.calls if "screen-sharing" in p][0]
        assert "the page reads" not in prompt, prompt
        assert "reference only" not in prompt, prompt
        assert "do NOT quote any number" in prompt, prompt


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
        assert not agenda.wants_human


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

    THE PHRASE IS THE TOUR'S, NOT A GUESS. A stop declares what it is there to show, so the
    scroll target is configuration the tenant wrote rather than a word the agent hoped would be
    on the page.
    """

    @staticmethod
    def tour_calls(tools: Any) -> list[dict]:
        return [c for c in tools.named("research.browse") if "corvus.example" not in c["url"]]

    async def test_a_stop_opens_at_the_thing_it_exists_to_show(self):
        agenda, tools = build()
        await collect(agenda.begin())
        await collect(agenda.respond("show me what it looks like"))

        opened = self.tour_calls(tools)
        wanted = {stop.url: stop.scroll_to for stop in nadia_spec().tour}
        assert opened and opened[0]["scroll_to"] == wanted[opened[0]["url"]]

    async def test_nothing_to_aim_at_is_not_an_error(self):
        """A miss costs nothing: `browse` treats an absent phrase as no scroll, and a stop is
        allowed to have nothing in particular to point at."""
        from dataclasses import replace

        from rainmaker.agents.spec import TourStop

        spec = replace(
            nadia_spec(),
            tour=(TourStop(url="https://demo.example/", label="the product", shows="the app"),),
        )
        agenda, tools = build(spec=spec)
        await collect(agenda.begin())
        await collect(agenda.respond("show me what it looks like"))
        assert self.tour_calls(tools)[0]["scroll_to"] == ""


class TestItSellsOnNeedRatherThanRecitingResearch:
    """FROM A REAL CALL, VERBATIM. The agent rents GPUs; the prospect runs a manufacturing
    company:

        agent     Hello Tasnimul! I noticed you're currently hiring four positions: Production
                  Associates, Product Manager, Research & Development Engineer, and Sales &
                  Marketing Specialist.
        prospect  so what
        agent     That looks interesting. Could you tell me why you think these roles are
                  important?
        prospect  Sure! ...
        agent     These roles seem like key areas where innovation happens at your company.

    Four job titles read back to the person who wrote them, then the buyer asked to justify
    their own recruitment, then filler. Nothing in there is about GPUs, and the agent never
    once said what it sells.

    Three separate causes, all ours: the greeting recited a finding instead of interpreting it,
    nothing connected a finding to a reason to buy, and a challenge fell through to an ordinary
    turn where a small model mirrors.
    """

    @staticmethod
    def gpu_spec():
        from dataclasses import replace

        from rainmaker.agents.spec import Need

        return replace(
            nadia_spec(),
            company="Tessera Compute",
            knowledge=(
                Fact("Tessera rents H100 GPUs by the hour, with no minimum term.",
                     source="positioning"),
                Fact("Most teams arrive because their cloud quota has been pending for weeks.",
                     source="the problem", topic="why"),
            ),
            needs=(
                Need(
                    signals=("research and development", "engineer", "hiring"),
                    means="they are building something new that will run into compute",
                    opener="it looks like you are building something new",
                    ask="is any of that work model training?",
                ),
            ),
        )

    @staticmethod
    def hiring_research() -> dict[str, Any]:
        """The careers page that produced the worst opening this agent ever gave.

        Four job titles, read back verbatim to the person who wrote them, on a call about
        renting GPUs. Kept as a fixture because every test in this class is about what should
        happen INSTEAD.
        """
        return {
            "research.research_company": {
                "name": {"value": "Halden Industries"},
                "hiring": [
                    {"title": "Production Associate"}, {"title": "Product Manager"},
                    {"title": "Research and Development Engineer"},
                    {"title": "Sales and Marketing Specialist"},
                ],
                "pages_fetched": [],
            }
        }

    async def test_the_greeting_says_what_the_finding_means_and_never_lists_it(self):
        agenda, _ = build(spec=self.gpu_spec(), **self.hiring_research())
        said = spoken(await collect(agenda.begin()))

        # THE LIST IS NEVER SPOKEN. Only the role that carried the signal is — the other three
        # job titles are the ones the agent read out loud, verbatim, on the real call.
        assert "Research and Development Engineer" in said, said
        for unrelated in ("Production Associate", "Product Manager", "Sales and Marketing"):
            assert unrelated not in said, f"{unrelated!r} was read out anyway"

        assert "it looks like you are building something new" in said.lower(), said
        assert "is any of that work model training?" in said.lower(), said

    async def test_a_field_with_a_value_is_not_quoted_as_a_noun_phrase(self):
        """"How they charge: sales assisted" is a field, not an enumeration, and reading it into
        the quote slot produced "sales assisted stood out" — grammatical, and not English
        anybody speaks. The need still selects; it just does not get quoted."""
        from dataclasses import replace

        from rainmaker.agents.spec import Need

        spec = replace(
            self.gpu_spec(),
            needs=(
                Need(
                    signals=("sales assisted",),
                    means="they sell through people",
                    opener="it looks like you sell through a team",
                    ask="how big is that team today?",
                ),
            ),
        )
        agenda, _ = build(
            spec=spec,
            **{"research.research_company": {"pricing_model": {"value": "sales assisted"},
                                             "pages_fetched": []}},
        )
        said = spoken(await collect(agenda.begin()))
        assert "stood out" not in said, said
        assert "it looks like you sell through a team" in said.lower(), said
        assert "how big is that team today?" in said.lower(), said

    async def test_the_tenants_question_is_spoken_word_for_word(self):
        """A wrong question is worse than no question, which is why a tenant writes it. Routed
        through a 1.5B model it came back paraphrased on a good turn and missing on a bad one."""
        agenda, _ = build(spec=self.gpu_spec(), **self.hiring_research())
        said = spoken(await collect(agenda.begin()))
        assert "is any of that work model training?" in said.lower(), said

    async def test_every_sentence_in_the_opening_starts_with_a_capital(self):
        """`opener` and `ask` are written lower case because a tenant writes them as fragments.
        Each one starts a sentence when it is spoken, and a full stop followed by "how long does
        a new rep" is something a reader sees before they have finished the line."""
        agenda, _ = build(spec=self.gpu_spec(), **self.hiring_research())
        said = spoken(await collect(agenda.begin()))
        opening = said[said.index("Hi Dana"):]
        for sentence in (s.strip() for s in opening.split(". ") if s.strip()):
            assert sentence[0].isupper(), f"{sentence!r} in {opening!r}"

    async def test_an_acronym_in_the_evidence_is_not_flattened(self):
        """`str.capitalize` lower-cases everything after the first letter, which turned
        "Research and Development Engineer" into "Research and development engineer"."""
        agenda, _ = build(spec=self.gpu_spec(), **self.hiring_research())
        said = spoken(await collect(agenda.begin()))
        assert "Research and Development Engineer" in said, said

    async def test_a_challenge_is_answered_with_a_reason_not_a_question(self):
        """"So what" is the commonest first objection. It carries no intent, so it used to fall
        through to a discovery turn — and the model asked the buyer to justify their own job
        adverts."""
        agenda, _ = build(spec=self.gpu_spec())
        await collect(agenda.begin())
        before = len(agenda.session.pipeline.llm.calls)

        await collect(agenda.respond("so what"))
        asked = " ".join(agenda.session.pipeline.llm.calls[before:])

        assert "pushed back" in asked
        assert "Do NOT ask them a question about their own business" in asked
        assert "rents H100 GPUs" in asked, "it was not told what it sells"
        # The tenant's own "why they move" line is handed over as the reason.
        assert "quota has been pending" in asked

    @pytest.mark.parametrize(
        "said", ["so what", "ok", "why should I care", "not interested", "who cares"]
    )
    async def test_the_shapes_a_pushback_takes(self, said: str):
        agenda, _ = build(spec=self.gpu_spec())
        await collect(agenda.begin())
        before = len(agenda.session.pipeline.llm.calls)
        await collect(agenda.respond(said))
        assert "pushed back" in " ".join(agenda.session.pipeline.llm.calls[before:]), said

    async def test_a_real_question_is_not_treated_as_a_pushback(self):
        agenda, _ = build(spec=self.gpu_spec())
        await collect(agenda.begin())
        before = len(agenda.session.pipeline.llm.calls)
        await collect(agenda.respond("how much does it cost for 40 people?"))
        assert "pushed back" not in " ".join(agenda.session.pipeline.llm.calls[before:])

    async def test_the_need_is_carried_into_every_later_step(self):
        """Working out what they need in the opening and then never mentioning it again is how
        a call drifts back into small talk by the second question."""
        agenda, _ = build(spec=self.gpu_spec(), **self.hiring_research())
        await collect(agenda.begin())
        assert agenda.need is not None
        assert "will run into compute" in agenda.session.profile.objective


class TestSheKnowsHowToStop:
    """`Step.WRAP`, `_wrap` and `end` were all written and nothing routed to them.

    "thanks, that's all for now" matched no intent, fell through to an ordinary discovery turn,
    and got back "Great! Let me know if you need anything else." The call never closed, never
    wrote itself down, and sat on whatever step it had reached — so a buyer who had just said
    they were done was left with an open call and nothing confirmed.
    """

    @pytest.mark.parametrize(
        "said",
        [
            "thanks, that's all for now",
            "thanks for your time",
            "we're done",
            "that's everything",
            "goodbye",
            "I have to go",
            "let's wrap it there",
            "nothing else",
        ],
    )
    def test_a_goodbye_is_heard_as_one(self, said: str):
        assert detect_intent(said) is Step.WRAP, said

    @pytest.mark.parametrize(
        ("said", "step"),
        [
            ("thanks, can you book something?", Step.BOOKING),
            ("thanks! how much does it cost?", Step.QUOTE),
            ("that looks great, show me more", Step.GUIDE),
        ],
    )
    def test_thanks_in_the_middle_of_a_call_is_not_a_goodbye(self, said: str, step: Step):
        """"thanks, can you book something?" is the middle of a call, not the end of one."""
        assert detect_intent(said) is step, said

    async def test_the_close_recites_what_was_agreed_and_ends_the_call(self):
        agenda, _ = build()
        await collect(agenda.begin())
        await collect(agenda.respond("how much is it for 40 seats?"))
        said = spoken(await collect(agenda.respond("thanks, that's all for now")))

        assert "Thanks for your time" in said, said
        assert "on screen" in said, said
        assert agenda.closed, "the call did not write itself down"

    async def test_the_close_never_speaks_a_currency_symbol(self):
        """`Quote.money` is the screen form and starts with a "$", which a synthesiser reads as
        the word "dollar" placed in front of the digits."""
        agenda, _ = build()
        await collect(agenda.begin())
        await collect(agenda.respond("how much is it for 40 seats?"))
        said = spoken(await collect(agenda.respond("that's all, thanks")))
        assert not set(said) & set("$£€"), said

    async def test_a_call_with_nothing_agreed_says_so_rather_than_implying_otherwise(self):
        agenda, _ = build()
        await collect(agenda.begin())
        said = spoken(await collect(agenda.respond("goodbye")))
        assert "Thanks for your time" in said, said
        assert "short note" in said, said
        assert agenda.closed


class TestAnsweringIsNotAsking:
    """A tour stop's trigger words are nouns from the tenant's own product, and a buyer says
    those nouns for two different reasons. Tessera's capacity stop answers to "h100" — so
    "we're training a 70B model, about 32 H100s for a month", which is the ANSWER to the
    discovery question, drove the call straight to a web page instead of being heard.
    """

    @pytest.mark.parametrize(
        "said",
        [
            "we're training a 70B model, about 32 H100s for a month",
            "we need about 32 H100s",
            "our budget is around 80k",
        ],
    )
    def test_a_statement_about_their_own_needs_is_not_a_request(self, said: str):
        from rainmaker.calls.agenda import _is_asking

        assert not _is_asking(said), said

    @pytest.mark.parametrize(
        "said",
        [
            "do you have h100s",
            "what have you got available right now",
            "h100s?",
            "can i see the pricing",
            "show me the capacity page",
            "any A100s in eu-west?",
        ],
    )
    def test_a_request_still_reads_as_one(self, said: str):
        from rainmaker.calls.agenda import _is_asking

        assert _is_asking(said), said

    async def test_the_quantity_is_still_read_off_an_answer_that_does_not_move_the_call(self):
        """The answer must not be lost just because it does not change the step."""
        agenda, _ = build()
        await collect(agenda.begin())
        await collect(agenda.respond("we've got about 40 reps"))
        assert agenda.said_seats == 40


class TestAcceptingATimeOutLoud:
    """`confirm_slot` was reachable only by CLICKING a slot in the console.

    On a voice call the agent offered two real times, the buyer said "the first one works for
    me", and nothing happened — the model improvised "our team will confirm the booking for
    you", which is a promise nobody wrote down and no diary holds. The entire escalation path
    ended in a sentence.
    """

    @staticmethod
    def two_slots() -> list[dict[str, str]]:
        from datetime import UTC, datetime, timedelta

        base = datetime(2026, 8, 27, 14, 0, tzinfo=UTC)
        return [
            {"starts_at": base.isoformat()},
            {"starts_at": (base + timedelta(minutes=30)).isoformat()},
        ]

    @pytest.mark.parametrize(
        ("said", "index"),
        [
            ("the first one", 0),
            ("first one works", 0),
            ("the earlier one", 0),
            ("the second one please", 1),
            ("the later one", 1),
            ("Thursday at two works for me", 0),
            ("can we do two thirty", 1),
            ("two thirty is better", 1),
        ],
    )
    def test_a_time_they_named_is_the_time_that_gets_booked(self, said: str, index: int):
        from rainmaker.calls.agenda import pick_slot

        assert pick_slot(said, self.two_slots()) == index, said

    @pytest.mark.parametrize(
        "said",
        ["actually how much is it again?", "what about the interconnect?", "yes"],
    )
    def test_anything_that_is_not_an_acceptance_books_nothing(self, said: str):
        from rainmaker.calls.agenda import pick_slot

        assert pick_slot(said, self.two_slots()) is None, said

    def test_a_bare_yes_books_the_only_time_on_the_table(self):
        from rainmaker.calls.agenda import pick_slot

        assert pick_slot("yes, that works", self.two_slots()[:1]) == 0

    def test_naming_only_the_day_asks_which_rather_than_guessing(self):
        """Two slots half an hour apart are both "Thursday". Booking the wrong half of somebody's
        afternoon is the one failure a diary cannot quietly absorb."""
        from rainmaker.calls.agenda import AMBIGUOUS, pick_slot

        assert pick_slot("Thursday works for me", self.two_slots()) == AMBIGUOUS

    async def test_saying_yes_to_a_time_actually_books_it(self):
        agenda, _ = build()
        await collect(agenda.begin())
        await collect(agenda.respond("can I talk to someone?"))
        assert agenda.offered, "no times were offered"

        said = spoken(await collect(agenda.respond("the first one works for me")))
        assert agenda.booking is not None, "nothing was booked"
        assert agenda.booking["spoken"] in said, said

    async def test_an_ambiguous_time_produces_a_question_not_a_booking(self):
        agenda, _ = build()
        await collect(agenda.begin())
        await collect(agenda.respond("can I talk to someone?"))
        day = __import__("datetime").datetime.fromisoformat(
            agenda.offered[0]["starts_at"]
        ).strftime("%A")

        said = spoken(await collect(agenda.respond(f"{day} works for me")))
        assert agenda.booking is None, "it guessed"
        assert said.rstrip().endswith("?"), said


class TestTheComparisonIsTheTenantsWords:
    """A comparison table a language model wrote about a named competitor is a defamation risk
    with a grid layout — and one that will not actually compare loses the deal on the one
    question where the buyer has said out loud what they need convincing of.

    Handed the table and told to be fair, the model answered "how does this compare to AWS?"
    with "it seems you're already well equipped for what you need": it compared nothing, conceded
    the deal, and said something that is not in the table anywhere.
    """

    async def test_the_rival_they_named_is_the_one_answered(self):
        """Substring matching needed the tenant's exact phrase, so "why not just buy our own
        boxes?" missed "buying your own boxes" on a single letter."""
        from dataclasses import replace

        from rainmaker.agents.spec import Competitor

        spec = replace(
            nadia_spec(),
            competitors=(
                Competitor(name="a hyperscaler", positioning="one account",
                           against=(("waiting", "give you nodes today"),)),
                Competitor(name="buying your own boxes", positioning="the cheapest hour",
                           against=(("time to start", "have you training this afternoon"),)),
            ),
        )
        agenda, _ = build(spec=spec)
        await collect(agenda.begin())
        said = spoken(await collect(agenda.respond("why not just buy our own boxes?")))
        assert "buying your own boxes" in said, said
        assert "a hyperscaler" not in said, said

    async def test_it_says_what_the_rival_is_good_at_before_the_difference(self):
        agenda, _ = build()
        await collect(agenda.begin())
        said = spoken(await collect(agenda.respond("how do you compare to a chat widget?")))
        assert "The honest case for" in said, said
        assert "Where we differ:" in said, said
        assert said.index("The honest case for") < said.index("Where we differ:")

    async def test_the_model_is_never_asked_to_write_a_comparison(self):
        agenda, _ = build()
        await collect(agenda.begin())
        llm = agenda.session.pipeline.llm
        before = len(llm.calls)
        await collect(agenda.respond("how do you compare to a chat widget?"))
        assert not llm.calls[before:], llm.calls[before:]


class TestTheCheckoutSaysWhatIsBeingBought:
    """The last screen before somebody enters a card is the one where a wrong word costs the
    sale. It was hardcoded to "seats" — so a GPU buyer's checkout read "On-demand, 23360 seats",
    on a product with no seats in it, from a platform whose whole multi-tenancy claim is that it
    stopped calling everything a seat."""

    async def test_the_line_item_uses_the_tenants_own_unit(self):
        import importlib.util
        from pathlib import Path

        spec_file = Path(__file__).resolve().parents[3] / "scripts" / "demo-embed.py"
        module_spec = importlib.util.spec_from_file_location("demo_embed", spec_file)
        demo_embed = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(demo_embed)

        agenda, tools = build(spec=demo_embed.tessera())
        await collect(agenda.begin())
        await collect(agenda.respond("we need about 32 H100s for a month"))
        await collect(agenda.respond("what does that cost?"))
        await collect(agenda.respond("great, how do I get started?"))

        sent = [c for c in tools.calls if c[0] == "payments.create_checkout"]
        assert sent, "no checkout was created"
        description = sent[-1][1]["description"]
        assert "GPU-hour" in description, description
        assert "seats" not in description, description


class TestTheTourExplainsItselfWhenTheTenantSaidHow:
    """The sentence after "what you're looking at is…" was the model's, and it got it wrong in
    four different shapes across four recorded calls: it narrated the page badly, then narrated
    the PROSPECT instead, then invented "392 GPUs for free", then slipped into the buyer's
    pronouns ("this would give US live capacity, compared to OUR current setup").

    `TourStop.because` lets the tenant answer it once. When they have, the model is not asked.
    """

    @staticmethod
    def spec_with_reason():
        from dataclasses import replace


        base = nadia_spec()
        return replace(
            base,
            tour=(
                replace(
                    base.tour[0],
                    because="you stop losing the buyers who arrive at eleven at night",
                ),
            ),
        )

    async def test_the_reason_is_spoken_with_what_is_on_screen(self):
        agenda, _ = build(spec=self.spec_with_reason())
        await collect(agenda.begin())
        said = spoken(await collect(agenda.respond("show me what it looks like")))
        assert "And that means you stop losing the buyers" in said, said

    async def test_the_model_is_not_asked_for_a_second_opinion_on_it(self):
        agenda, _ = build(spec=self.spec_with_reason())
        await collect(agenda.begin())
        llm = agenda.session.pipeline.llm
        before = len(llm.calls)
        await collect(agenda.respond("show me what it looks like"))
        assert not [p for p in llm.calls[before:] if "screen-sharing" in p]

    async def test_a_tenant_who_wrote_no_reason_still_gets_one(self):
        """The field is optional. Without it the model does the connecting, which is what it is
        for — it is only the factual half that was taken away."""
        agenda, _ = build()
        await collect(agenda.begin())
        llm = agenda.session.pipeline.llm
        before = len(llm.calls)
        await collect(agenda.respond("show me what it looks like"))
        assert [p for p in llm.calls[before:] if "screen-sharing" in p]

    def test_the_reason_survives_a_round_trip(self):
        from rainmaker.agents.spec import AgentSpec

        spec = self.spec_with_reason()
        back = AgentSpec.from_dict(spec.as_dict())
        assert back.tour[0].because == spec.tour[0].because
