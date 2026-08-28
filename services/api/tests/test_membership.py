"""Membership, enforced at the relay — the only place it can be.

WHAT THIS REPLACED. Actor identity was `?actor=dana` in a query string, which is not a claim
about who you are, it is a claim you would like to make. Anyone able to open a socket could open
it as anybody, into any workspace, and the op log attributed their writes accordingly.

The tests below are written against the four things that are actually enforced — identity,
membership, attribution, revocation — rather than against "auth", which is a word that hides
whether any of them are true.

ENROLMENT IS STILL OPEN, and that is deliberate: there is no sign-in screen, so `POST
/api/sync/token` gives a grant to whoever asks. That is the one line a real deployment changes.
`test_enrolment_is_open_and_that_is_the_policy_not_an_accident` pins it as a decision, so that
if somebody later puts an identity provider in front of it they find a failing test that says
what the old behaviour was, rather than a silent change of meaning.
"""

from __future__ import annotations

import time

import pytest

from rainmaker.sync.membership import AuthError, Members, authorise_ops


@pytest.fixture
def members() -> Members:
    return Members(":memory:", secret="test-secret-not-a-real-one")


class TestATokenIsAClaimAboutOneActorInOneWorkspace:
    def test_a_valid_token_names_its_actor(self, members: Members):
        token = members.issue("demo", "dana")
        grant = members.verify(token, "demo")
        assert grant.actor == "dana"
        assert grant.workspace == "demo"

    def test_a_token_for_one_workspace_is_not_a_token_for_another(self, members: Members):
        """The whole point of scoping. A grant in `demo` must not open `acme`."""
        token = members.issue("demo", "dana")
        members.add("acme", "someone-else")
        with pytest.raises(AuthError, match="another workspace"):
            members.verify(token, "acme")

    def test_editing_the_actor_breaks_the_signature_rather_than_changing_who_you_are(
        self, members: Members
    ):
        """THE ATTACK THE QUERY PARAMETER ALLOWED, tried against the replacement.

        Under the old scheme, changing `dana` to `sam` in the URL changed who the ops belonged
        to. Here the body is signed, so the same edit produces a token that does not verify.
        """
        import base64
        import json

        token = members.issue("demo", "dana")
        body_b64, mac_b64 = token.split(".", 1)
        body = json.loads(base64.urlsafe_b64decode(body_b64 + "=="))
        body["a"] = "sam"
        forged = base64.urlsafe_b64encode(
            json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
        ).decode().rstrip("=")

        with pytest.raises(AuthError, match="bad signature"):
            members.verify(f"{forged}.{mac_b64}", "demo")

    def test_a_token_signed_with_another_secret_is_refused(self, members: Members):
        """A second deployment's token is not this deployment's token."""
        elsewhere = Members(":memory:", secret="a-different-deployment")
        elsewhere.add("demo", "dana")
        stolen = elsewhere.issue("demo", "dana")
        members.add("demo", "dana")
        with pytest.raises(AuthError, match="bad signature"):
            members.verify(stolen, "demo")

    def test_garbage_is_refused_rather_than_crashing(self, members: Members):
        for junk in ("", "not-a-token", "a.b", "...", "%%%.%%%"):
            with pytest.raises(AuthError):
                members.verify(junk, "demo")

    def test_an_expired_token_stops_working(self, members: Members, monkeypatch):
        import rainmaker.sync.membership as mod

        token = members.issue("demo", "dana")
        assert members.verify(token, "demo").actor == "dana"
        # Moved rather than slept. The check is `exp < now`, so advancing `now` past the TTL
        # is the honest way to test it and costs nothing.
        #
        # THE REAL CLOCK IS CAPTURED FIRST. `mod.time` IS the `time` module, so patching
        # `mod.time.time` patches `time.time` — and a lambda that then called `time.time()`
        # would be calling itself. It recursed until the stack ran out, which reads as a
        # mysterious crash rather than as a test bug.
        expired_at = time.time() + mod.TOKEN_TTL_SECONDS + 60
        monkeypatch.setattr(mod.time, "time", lambda: expired_at)
        with pytest.raises(AuthError, match="expired"):
            members.verify(token, "demo")


class TestRevocationTakesEffectWithoutWaitingForExpiry:
    def test_a_removed_member_cannot_use_a_token_that_is_still_signed_and_unexpired(
        self, members: Members
    ):
        """THE REASON `verify` READS THE TABLE EVERY TIME.

        A token is a claim that a row existed. If verification trusted the signature alone, an
        ex-member would keep writing until the token expired — up to thirty days of an account
        that was revoked this morning.
        """
        token = members.issue("demo", "dana")
        assert members.remove("demo", "dana") is True
        with pytest.raises(AuthError, match="not a member"):
            members.verify(token, "demo")

    def test_removing_a_member_who_is_not_one_reports_that_honestly(self, members: Members):
        assert members.remove("demo", "nobody") is False


class TestAnOpMayNotWearSomebodyElsesActor:
    def test_ops_must_match_the_authenticated_actor(self, members: Members):
        """FORGING AN ACTOR IS FORGING A MERGE, not merely a byline.

        The CRDT breaks hybrid-logical-clock ties by actor id. An op wearing another actor does
        not only misattribute an edit — it decides which of two concurrent edits wins. That is
        why this check is part of correctness and not only of the audit trail.
        """
        grant = members.verify(members.issue("demo", "dana"), "demo")
        authorise_ops([{"actor": "dana", "op_type": "set"}], grant)
        with pytest.raises(AuthError, match="does not match"):
            authorise_ops([{"actor": "sam", "op_type": "set"}], grant)

    def test_one_bad_op_in_a_batch_rejects_the_batch(self, members: Members):
        """A flush is a batch. Storing the good half of it would leave the client believing
        the whole batch landed, because the outbox clears on success."""
        grant = members.verify(members.issue("demo", "dana"), "demo")
        with pytest.raises(AuthError):
            authorise_ops(
                [{"actor": "dana"}, {"actor": "dana"}, {"actor": "mallory"}], grant
            )

    def test_an_op_with_no_actor_is_allowed_through(self, members: Members):
        """The relay stamps attribution; an op that makes no claim is not making a false one."""
        grant = members.verify(members.issue("demo", "dana"), "demo")
        authorise_ops([{"op_type": "set"}], grant)


class TestEnrolmentIsAPolicyAndEnforcementIsNot:
    def test_enrolment_is_open_and_that_is_the_policy_not_an_accident(self, members: Members):
        """Anyone who asks is enrolled, because there is no sign-in screen to ask them.

        Pinned as a test so that putting an identity provider in front of `/api/sync/token`
        breaks something that says what the old behaviour was, rather than quietly changing
        what the system means.
        """
        assert members.role_of("demo", "newcomer") is None
        members.issue("demo", "newcomer")
        assert members.role_of("demo", "newcomer") == "member"

    def test_issuing_twice_does_not_duplicate_a_membership(self, members: Members):
        members.issue("demo", "dana")
        members.issue("demo", "dana")
        assert [m["actor"] for m in members.members("demo")] == ["dana"]

    def test_a_workspace_lists_only_its_own_members(self, members: Members):
        members.issue("demo", "dana")
        members.issue("acme", "sam")
        assert [m["actor"] for m in members.members("demo")] == ["dana"]
        assert [m["actor"] for m in members.members("acme")] == ["sam"]

    def test_an_actorless_token_is_refused(self, members: Members):
        with pytest.raises(AuthError):
            members.issue("demo", "")
