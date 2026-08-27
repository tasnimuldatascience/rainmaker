"""What the voice is given, as opposed to what the screen is shown.

Every case here is a string a synthesiser reads wrong. The system prompt asks the model not to
produce most of them and the model mostly complies — these tests are about the rest of the time,
because a listener who hears "asterisk asterisk" concludes the voice is broken rather than that
the model slipped.
"""

from __future__ import annotations

import pytest

from rainmaker.calls.speech import say


class TestMarkdownIsNeverHeard:
    @pytest.mark.parametrize(
        ("written", "spoken"),
        [
            ("**Reserved** is cheaper", "Reserved is cheaper"),
            ("*Reserved* is cheaper", "Reserved is cheaper"),
            ("***Reserved*** is cheaper", "Reserved is cheaper"),
            ("__Reserved__ is cheaper", "Reserved is cheaper"),
            ("_Reserved_ is cheaper", "Reserved is cheaper"),
            ("~~Reserved~~ is cheaper", "Reserved is cheaper"),
            ("`Reserved` is cheaper", "Reserved is cheaper"),
            ("## Pricing", "Pricing"),
            ("> Reserved is cheaper", "Reserved is cheaper"),
        ],
    )
    def test_the_markers_go_and_the_words_stay(self, written: str, spoken: str):
        assert say(written) == spoken

    def test_a_bullet_list_becomes_a_sentence_rather_than_a_shopping_list(self):
        assert say("- On-demand\n- Reserved\n- Cluster") == "On-demand Reserved Cluster"

    def test_a_numbered_item_keeps_its_number(self):
        """"1. Reserved" is a thing somebody says out loud. "hash hash Pricing" is not."""
        assert say("1. Reserved\n2. Cluster") == "1. Reserved 2. Cluster"

    def test_a_link_is_read_as_its_words_not_its_address(self):
        assert say("see [the rate card](https://acme.dev/pricing)") == "see the rate card"

    def test_an_underscore_inside_a_word_is_not_emphasis(self):
        assert say("the on_demand tier") == "the on_demand tier"

    def test_multiplication_is_not_emphasis(self):
        assert say("2 * 3 nodes") == "2 * 3 nodes"


class TestAddressesAreSaidTheWayPeopleSayThem:
    def test_a_url_loses_its_scheme_and_its_path(self):
        assert say("visit https://stripe.com/pricing now") == "visit stripe dot com now"

    def test_a_bare_domain_is_still_a_domain(self):
        assert say("I looked at stripe.com") == "I looked at stripe dot com"

    def test_an_email_is_read_rather_than_spelled(self):
        assert say("email dana@stripe.com") == "email dana at stripe dot com"

    def test_a_dotted_name_in_an_address_survives(self):
        assert say("dana.whitfield@stripe.com") == "dana dot whitfield at stripe dot com"

    def test_a_version_number_is_not_an_address(self):
        """THE RULE THAT ATE ITS OWN INPUT. Anchored on "a word with dots in it", this turned
        `Qwen2.5-1.5B-Instruct` into "Qwen2 dot 5-1 dot 5B" and `e.g` into "e dot g"."""
        assert say("Qwen2.5-1.5B-Instruct") == "Qwen2.5-1.5B-Instruct"
        assert say("version 1.5 of the A100") == "version 1.5 of the A100"

    def test_an_abbreviation_is_not_an_address(self):
        assert say("e.g. Reserved") == "for example, Reserved"


class TestTheRulesDoNotUndoEachOther:
    def test_an_expansion_is_not_swallowed_by_the_contraction_pass(self):
        """"i.e." expanded to "that is," and the next rule contracted that to "that's,". Each
        rule was right on its own; in sequence they produced something nobody wrote."""
        assert say("i.e. cheaper") == "in other words, cheaper"

    def test_a_link_target_is_not_eaten_before_the_link_resolves(self):
        """The URL rule runs after the link rule for this reason: the other way round it
        consumes the target and leaves the brackets standing around nothing."""
        assert say("[pricing](https://acme.dev/x)") == "pricing"


class TestThingsASynthesiserSaysWrong:
    @pytest.mark.parametrize(
        ("written", "spoken"),
        [
            ("e.g. Reserved", "for example, Reserved"),
            ("i.e. Reserved", "in other words, Reserved"),
            ("H100 vs. A100", "H100 versus A100"),
            ("approx. forty", "roughly forty"),
            ("nodes, GPUs, etc.", "nodes, GPUs, and so on"),
            ("24/7 support", "twenty-four seven support"),
            ("sales & marketing", "sales and marketing"),
            ("99.9% uptime", "99.9 percent uptime"),
            ("a 3x saving", "a 3 times saving"),
            ("H100 -> A100", "H100 to A100"),
        ],
    )
    def test_it_is_expanded(self, written: str, spoken: str):
        assert say(written) == spoken

    def test_a_thousands_separator_is_one_number_not_two(self):
        """"4,800" cut at the comma is read as "four" pause "eight hundred"."""
        assert say("4,800 dollars") == "4800 dollars"
        assert say("1,234,567 requests") == "1234567 requests"

    def test_a_comma_between_words_is_left_alone(self):
        assert say("forty seats, and no minimum") == "forty seats, and no minimum"

    def test_an_emoji_is_not_read_out(self):
        assert say("Done \U0001f389 and dusted") == "Done and dusted"


class TestSpokenRegister:
    @pytest.mark.parametrize(
        ("written", "spoken"),
        [
            ("I am not going to pretend", "I'm not going to pretend"),
            ("it is cheaper", "it's cheaper"),
            ("we cannot do that", "we can't do that"),
            ("you will see", "you'll see"),
            ("that is right", "that's right"),
        ],
    )
    def test_written_english_becomes_spoken_english(self, written: str, spoken: str):
        assert say(written) == spoken

    def test_capitalisation_survives_the_contraction(self):
        """"Do not" at the start of a sentence must not come back as "don't"."""
        assert say("Do not worry.") == "Don't worry."
        assert say("It is fine.") == "It's fine."


class TestPauses:
    def test_an_ellipsis_becomes_a_pause_it_can_perform(self):
        assert say("well... maybe") == "well, maybe"

    def test_piled_up_punctuation_is_reduced_to_one_mark(self):
        assert say("really?!") == "really?"
        assert say("no!!!") == "no!"

    def test_an_em_dash_gets_room_to_be_a_beat(self):
        assert say("Quick thing first—I'm an AI") == "Quick thing first — I'm an AI"


class TestItIsSafeToRunOnAnything:
    def test_plain_text_is_returned_unchanged(self):
        plain = "Reserved works out at two dollars forty per GPU-hour."
        assert say(plain) == plain

    @pytest.mark.parametrize("empty", ["", "   ", "\n\n"])
    def test_nothing_in_nothing_out(self, empty: str):
        assert say(empty) == ""

    @pytest.mark.parametrize(
        "text",
        [
            "**Reserved** is $4,800 — see [the card](https://acme.dev/p) e.g. now",
            "- 24/7 & 99.9%\n- dana@stripe.com",
            "I am not sure... it cannot be right!!",
        ],
    )
    def test_it_is_idempotent(self, text: str):
        """Clauses are re-normalised on paths that overlap; a second pass must be a no-op."""
        once = say(text)
        assert say(once) == once


class TestTheWrittenFormSurvivesForTheScreen:
    def test_a_clip_carries_both_and_prefers_the_spoken_one_for_a_voice(self):
        from rainmaker.calls.pipeline import Clip

        clip = Clip(text="visit stripe.com", spoken="visit stripe dot com", wav=b"")
        assert clip.text == "visit stripe.com", "the caption must keep the written form"
        assert clip.to_say == "visit stripe dot com"

    def test_a_clip_with_no_spoken_form_falls_back_to_its_text(self):
        from rainmaker.calls.pipeline import Clip

        assert Clip(text="forty seats", wav=b"").to_say == "forty seats"
