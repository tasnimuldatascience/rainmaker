#!/usr/bin/env python3
"""Download the lip-sync model, so Nadia's mouth moves.

    python scripts/fetch-lipsync.py

WITHOUT THIS she is a photoreal still that brightens with her voice, and the console says so
rather than pretending. With it, her mouth is generated from the audio she is actually saying,
on your GPU, at about seventeen times realtime once warm.

READ THE LICENCE BEFORE YOU SHIP THIS. Everything else in this repository is Apache-2.0, MIT or
public domain, and this is not: **Wav2Lip's weights are released for academic and personal use
only, not for commercial use.** That is why it is a separate opt-in step rather than something a
clone downloads, and why the console reports which face is running. If you are building on this
commercially, the same interface takes a hosted provider — see `calls/avatar.py`.

    Wav2Lip  Prajwal K R et al., "A Lip Sync Expert Is All You Need for Speech to Lip
             Generation In The Wild", ACM Multimedia 2020.
             https://github.com/Rudrabha/Wav2Lip
"""

from __future__ import annotations

import argparse
import shutil
import sys
import urllib.request
from pathlib import Path

MODELS = Path(__file__).resolve().parents[1] / "services" / "api" / "models"

#: A mirror rather than the original Google Drive link, which needs a browser and a confirmation
#: token and breaks in scripts. The file is the same checkpoint.
CHECKPOINT_URL = "https://huggingface.co/EraSpire/wav2lip/resolve/main/wav2lip_gan.pth"
CHECKPOINT_NAME = "wav2lip_gan.pth"
EXPECT_AT_LEAST = 400_000_000

CONSENT = """
Wav2Lip's weights are licensed for academic and personal use ONLY, not commercial use.
Everything else in this repository is permissively licensed; this is the exception.
""".strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="re-download if already present")
    parser.add_argument(
        "--yes", action="store_true", help="skip the licence prompt (for CI and scripts)"
    )
    args = parser.parse_args()

    target = MODELS / CHECKPOINT_NAME
    if target.exists() and not args.force:
        print(f"{CHECKPOINT_NAME} is already here ({target.stat().st_size / 1e6:.0f}MB)")
        return 0

    print(CONSENT)
    if not args.yes:
        # Deliberately a prompt. A licence restriction that scrolls past in a build log is a
        # licence restriction nobody read.
        answer = input("\nDownload it anyway? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Left alone. She will hold still and the console will say why.")
            return 1

    MODELS.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".partial")
    print(f"\n  {CHECKPOINT_NAME} downloading…", end="", flush=True)
    try:
        request = urllib.request.Request(CHECKPOINT_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=120) as response, partial.open("wb") as out:
            shutil.copyfileobj(response, out, length=1 << 20)
    except Exception as exc:  # noqa: BLE001 — the message matters more than the traceback
        partial.unlink(missing_ok=True)
        print(f"\r  {CHECKPOINT_NAME} FAILED: {exc}")
        return 1

    size = partial.stat().st_size
    if size < EXPECT_AT_LEAST:
        # A truncated checkpoint at the right path is worse than none: `available` would report
        # true and the load would fail in the middle of a live call.
        partial.unlink(missing_ok=True)
        print(f"\r  {CHECKPOINT_NAME} FAILED: got {size / 1e6:.0f}MB, expected far more")
        return 1

    partial.replace(target)
    print(f"\r  {CHECKPOINT_NAME} done ({size / 1e6:.0f}MB)      ")
    print("\nRestart the API. /api/calls/health will report the face as lip-synced.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
