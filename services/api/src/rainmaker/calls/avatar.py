"""Who the prospect is looking at, and how honest the system is about it.

THREE FACES, ONE INTERFACE. `pipeline.Avatar` takes audio and returns frames; everything below
implements it, and which one is running is reported by `/api/calls/health` rather than assumed.

    PortraitAvatar   the default. A photoreal synthetic portrait, lit and moved by the real
                     output loudness. No GPU, no key, no weights. Does not fake a mouth.
    HostedAvatar     a streaming avatar service. Real lip-sync on a real-looking face. Needs
                     an account and a key, so it is off unless one is configured.
    PlaceholderAvatar the vector rig in `pipeline.py`, kept as the last fallback.

WHY THE DEFAULT DOES NOT MOVE HER LIPS, since that is the obvious thing to build and the reason
not to is worth writing down. Warping the mouth of a 512-pixel still is guesswork: the image
contains no information about teeth, tongue or the inside of the mouth, so any opening has to be
invented. The uncanny valley is steepest exactly there — a face that is nearly right and moves
slightly wrong reads as a corpse, while the SAME face holding still reads as a video call with a
frozen frame, which people see every day and think nothing of. So the local face moves only in
ways that are true: it brightens and blooms with the actual RMS of the audio playing at that
instant, and drifts on a slow camera path. Nothing claims to be lip movement, so nothing is
wrong.

WHAT WAS TRIED AND REJECTED: MuseTalk, which is the right answer and does not run here. It pins
`torch 2.0.1+cu118`, whose newest supported architecture is `sm_90`; this machine's GPU is
`sm_120`, so that build has no GPU at all — not a slower GPU, none. It also needs mmcv, mmdet
and mmpose compiled from source, which on Windows means a full MSVC and CUDA toolchain. And its
own README's laptop datapoint is five minutes of compute for eight seconds of video, on a card
that is not simultaneously hosting a language model. It is documented here rather than left as
an adapter nobody can run.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from .pipeline import Avatar, PlaceholderAvatar

log = logging.getLogger("rainmaker.calls.avatar")

#: The portrait the console renders. Served as a static asset rather than streamed, because it
#: is one 21KB image that never changes during a call — pushing it down the socket every turn
#: would be a megabyte an hour to say the same thing.
PORTRAIT_PATH = os.environ.get("RAINMAKER_PORTRAIT", "/agent/liv.jpg")

#: Set to turn on a hosted talking head. Named per provider so a deployment can hold several.
HOSTED_KEY_VARS = ("RAINMAKER_AVATAR_KEY", "SIMLI_API_KEY", "TAVUS_API_KEY", "HEYGEN_API_KEY")


@dataclass(slots=True)
class FaceDescription:
    """What the console needs to render this avatar, and what to say about it.

    `synthetic` is not a detail. The face is a StyleGAN portrait of nobody, and a product that
    enforces telling people it is an AI should be equally willing to say the face was never a
    person's. It ends up in the console's tooltip and in the alt text.
    """

    kind: str
    label: str
    portrait: str = ""
    synthetic: bool = True
    lip_synced: bool = False
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "portrait": self.portrait,
            "synthetic": self.synthetic,
            "lip_synced": self.lip_synced,
            "note": self.note,
        }


class PortraitAvatar(Avatar):
    """A photoreal still, animated by the audio that is actually playing.

    The rendering happens in the browser — see `apps/console/src/components/Portrait.tsx` — and
    this class exists to declare that fact to the rest of the system rather than to push pixels.
    `render` still honours the interface: it yields one amplitude byte per audio chunk, which is
    what a server-side consumer of frames (a recorder, a headless test) would need.
    """

    name = "portrait"
    realtime = True

    def __init__(self, portrait: str = PORTRAIT_PATH):
        self.portrait = portrait

    def describe(self) -> FaceDescription:
        return FaceDescription(
            kind="portrait",
            label="Photoreal still, audio-lit",
            portrait=self.portrait,
            synthetic=True,
            lip_synced=False,
            note=(
                "A generated face of no real person. It brightens and moves with the real "
                "output level; it does not pretend to lip-sync."
            ),
        )

    async def render(self, audio: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        async for chunk in audio:
            yield bytes([min(255, max(chunk[:64], default=0) if chunk else 0)])

    async def idle(self) -> AsyncIterator[bytes]:
        import asyncio

        while True:
            yield b"\x00"
            await asyncio.sleep(1 / 30)


class HostedAvatar(Avatar):
    """A streaming avatar service: genuinely photoreal, genuinely lip-synced, genuinely not free.

    OFF UNLESS A KEY IS SET, which is the same shape as `FIRECRAWL_API_KEY` in the research
    layer: the repository runs completely without it, and the paid path is an upgrade rather
    than a requirement. A reviewer who clones this gets a working call with the local face.

    NOT EXERCISED IN THIS REPOSITORY, and said plainly rather than discovered. There is no key
    here, so the request shape below is written from each provider's published API and has never
    had a response come back. The interface boundary is the tested part: `CallPipeline` cannot
    tell which avatar it holds, so swapping this in changes the renderer and nothing else.
    """

    name = "hosted"
    realtime = True
    #: Whether the transport below actually exists. False until someone writes it, and
    #: `build_avatar` refuses to select a provider that cannot render — see the note there.
    implemented = False

    def __init__(self, provider: str, api_key: str):
        self.provider = provider
        self.api_key = api_key

    def describe(self) -> FaceDescription:
        return FaceDescription(
            kind="hosted",
            label=f"{self.provider} streaming avatar",
            synthetic=True,
            lip_synced=True,
            note="Photoreal lip-sync from a hosted provider. Not exercised in this repository.",
        )

    async def render(self, audio: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        """Stream audio up, stream video frames back.

        Deliberately a thin shell. Every provider differs in transport — WebRTC for Simli and
        Tavus, a websocket for HeyGen — and writing a plausible-looking implementation for all
        three, none of which has ever run, would be worse than writing none: it would look
        tested. What is real is that it is reachable through the same interface.
        """
        raise NotImplementedError(
            f"{self.provider} streaming is not implemented in this repository. The provider "
            "interface is the contract; wire the transport here and CallPipeline needs no change."
        )
        # Unreachable, and load-bearing: without a `yield` this is a coroutine rather than an
        # async generator, so `async for` over it fails with a type error somewhere else instead
        # of the message above. The interface promises an async iterator; this keeps it one.
        yield b""  # pragma: no cover


def build_avatar(prefer: str = "auto") -> Avatar:
    """The best face this deployment can actually show.

    Order is honesty-first: a hosted provider only when someone has configured one, the
    photoreal still otherwise, and the vector rig only if the portrait has been removed.
    """
    if prefer == "placeholder":
        return PlaceholderAvatar()
    if prefer == "portrait":
        return PortraitAvatar()

    for variable in HOSTED_KEY_VARS:
        key = os.environ.get(variable, "").strip()
        if not key:
            continue
        provider = (
            os.environ.get("RAINMAKER_AVATAR_PROVIDER", "hosted")
            if variable == "RAINMAKER_AVATAR_KEY"
            else variable.split("_")[0].title()
        )
        hosted = HostedAvatar(provider, key)
        if hosted.implemented:
            log.info("avatar: using hosted provider %s", provider)
            return hosted

        # A KEY MUST NOT BUY A CLAIM NOBODY HONOURS. Selecting the hosted provider here made
        # /api/calls/health report `lip_synced: true` while the console went on drawing the same
        # still portrait, because nothing calls `render` yet. The face would not have changed and
        # the badge would have lied about it — which is worse than the missing feature, and worse
        # than the honest fallback it replaced.
        log.warning(
            "avatar: %s is configured but its transport is not implemented in this "
            "repository; using the local portrait and saying so",
            provider,
        )
        return PortraitAvatar()

    return PortraitAvatar()


def describe(avatar: Avatar) -> dict[str, Any]:
    """What is on screen, for `/api/calls/health` and the console."""
    description = getattr(avatar, "describe", None)
    if callable(description):
        return description().as_dict()
    return FaceDescription(
        kind=getattr(avatar, "name", "unknown"),
        label="Vector rig",
        synthetic=True,
        lip_synced=False,
        note="Drawn, not photographic. The fallback when no portrait is available.",
    ).as_dict()
