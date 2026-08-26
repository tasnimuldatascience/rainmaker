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

    def test_a_key_does_not_buy_a_claim_nobody_honours(self, monkeypatch: pytest.MonkeyPatch):
        """Setting a key used to select `HostedAvatar`, so `/api/calls/health` reported
        `lip_synced: true` while the console drew the same still portrait — nothing calls
        `render` yet. The face would not have changed and the badge would have lied about it,
        which is worse than the missing feature."""
        monkeypatch.setenv("SIMLI_API_KEY", "sk-not-a-real-key")
        avatar = build_avatar()

        assert isinstance(avatar, PortraitAvatar)
        assert describe(avatar)["lip_synced"] is False

    def test_a_provider_is_selected_once_its_transport_exists(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The selection logic itself is right; it is gated on the transport being real. This
        is the test that starts passing the day someone implements `render`."""
        monkeypatch.setenv("SIMLI_API_KEY", "sk-not-a-real-key")
        monkeypatch.setattr(HostedAvatar, "implemented", True)
        avatar = build_avatar()

        assert isinstance(avatar, HostedAvatar)
        assert avatar.api_key == "sk-not-a-real-key"
        assert describe(avatar)["lip_synced"] is True

    def test_an_empty_key_is_not_a_key(self, monkeypatch: pytest.MonkeyPatch):
        """An unset variable exported as "" is the normal state of a half-filled .env, and
        treating it as configured means a call that fails instead of a call with a local face."""
        monkeypatch.setenv("SIMLI_API_KEY", "   ")
        assert isinstance(build_avatar(), PortraitAvatar)

    def test_the_rig_can_be_forced(self):
        assert isinstance(build_avatar("placeholder"), PlaceholderAvatar)


class TestWhatItAdmitsTo:
    def test_a_still_face_says_it_is_still(self):
        """With no checkpoint she does not move, and the console badge says so — a still face
        claiming to lip-sync reads as broken rather than as deliberate."""
        described = describe(PortraitAvatar())
        assert described["lip_synced"] is False
        assert "fetch-lipsync" in described["note"]

    def test_a_face_that_is_lip_syncing_says_that_instead(self):
        """The badge tracks what is loaded rather than a constant. A reader checks it against
        the screen, so it has to be true in both directions."""

        class Loaded:
            ready = True

        described = describe(PortraitAvatar(lipsync=Loaded()))
        assert described["lip_synced"] is True
        assert "Wav2Lip" in described["label"]

    def test_an_installed_but_unloaded_generator_is_not_yet_lip_sync(self):
        """It takes six seconds to load. Claiming lip-sync during that window puts a badge on
        screen that the face is not honouring yet."""

        class Loading:
            ready = False

        assert describe(PortraitAvatar(lipsync=Loading()))["lip_synced"] is False

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
