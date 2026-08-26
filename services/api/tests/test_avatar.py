"""Which face is on screen, and whether the system tells the truth about it."""

from __future__ import annotations

import pytest

from rainmaker.calls.avatar import HostedAvatar, PortraitAvatar, build_avatar, describe
from rainmaker.calls.pipeline import PlaceholderAvatar


class TestWhichFaceRuns:
    def test_the_photoreal_still_is_the_default(self, monkeypatch: pytest.MonkeyPatch):
        """A clone with no key and no weights still gets a real-looking face."""
        for var in ("RAINMAKER_AVATAR_KEY", "SIMLI_API_KEY", "TAVUS_API_KEY", "HEYGEN_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        assert isinstance(build_avatar(), PortraitAvatar)

    def test_a_configured_provider_wins(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("SIMLI_API_KEY", "sk-not-a-real-key")
        avatar = build_avatar()
        assert isinstance(avatar, HostedAvatar)
        assert avatar.api_key == "sk-not-a-real-key"

    def test_an_empty_key_is_not_a_key(self, monkeypatch: pytest.MonkeyPatch):
        """An unset variable exported as "" is the normal state of a half-filled .env, and
        treating it as configured means a call that fails instead of a call with a local face."""
        monkeypatch.setenv("SIMLI_API_KEY", "   ")
        assert isinstance(build_avatar(), PortraitAvatar)

    def test_the_rig_can_be_forced(self):
        assert isinstance(build_avatar("placeholder"), PlaceholderAvatar)


class TestWhatItAdmitsTo:
    def test_the_local_face_does_not_claim_lip_sync(self):
        """It brightens and drifts with the real output level and never moves its mouth. The
        console badge says so, because a still face that claimed to lip-sync would read as
        broken rather than as deliberate."""
        described = describe(PortraitAvatar())
        assert described["lip_synced"] is False
        assert "does not pretend" in described["note"]

    def test_every_face_admits_to_being_synthetic(self):
        """The product enforces telling people it is an AI. A face that was never anyone's is
        the same claim, and it ends up in the alt text and the tooltip."""
        for avatar in (PortraitAvatar(), HostedAvatar("Simli", "k"), PlaceholderAvatar()):
            assert describe(avatar)["synthetic"] is True

    def test_the_hosted_provider_is_marked_as_not_run_here(self):
        assert "not exercised" in describe(HostedAvatar("Simli", "k"))["note"].lower()

    async def test_the_hosted_renderer_refuses_rather_than_pretending(self):
        """A plausible-looking implementation that has never had a response come back is worse
        than none: it looks tested."""

        async def audio():
            yield b"\x00"

        with pytest.raises(NotImplementedError, match="not implemented"):
            [chunk async for chunk in HostedAvatar("Simli", "k").render(audio())]

    def test_an_unknown_avatar_still_describes_itself(self):
        assert describe(PlaceholderAvatar())["kind"] == "placeholder"


class TestTheInterfaceHolds:
    async def test_the_portrait_still_produces_frames_for_a_server_side_consumer(self):
        """The browser does the drawing, but the interface promises frames — a recorder or a
        headless test consumes them."""

        async def audio():
            yield b"\x40\x20"
            yield b"\xff\x10"

        frames = [f async for f in PortraitAvatar().render(audio())]
        assert len(frames) == 2
        assert frames[1] > frames[0], "the frame should track the audio it was made from"
