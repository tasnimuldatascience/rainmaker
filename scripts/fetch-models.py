#!/usr/bin/env python3
"""Download the local voice.

WHY A SCRIPT AND NOT A PIP EXTRA. Kokoro's weights are 330MB of ONNX plus a 27MB voice pack.
Putting them in git would make every clone pay for them forever; putting them in a package
would make `pip install` do a silent third-of-a-gigabyte download. So they are fetched
deliberately, once, by someone who has decided they want the real voice.

WITHOUT THIS, THE AGENT STILL TALKS — the browser's own `speechSynthesis` speaks instead, and
`/api/calls/health` reports which one is running. It sounds markedly worse, which is the reason
this script exists rather than the reason it is optional.

    python scripts/fetch-models.py            # fetch what is missing
    python scripts/fetch-models.py --force    # fetch again over what is there

Both files come from the kokoro-onnx release page. Apache-2.0, no account, no key.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import urllib.request
from pathlib import Path

RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
MODELS = Path(__file__).resolve().parents[1] / "services" / "api" / "models"

FILES = [
    ("kokoro-v1.0.onnx", f"{RELEASE}/kokoro-v1.0.onnx", 310_000_000),
    ("voices-v1.0.bin", f"{RELEASE}/voices-v1.0.bin", 25_000_000),
]


def fetch(name: str, url: str, expect_at_least: int, *, force: bool) -> bool:
    target = MODELS / name
    if target.exists() and not force:
        print(f"  {name:24} already here ({target.stat().st_size / 1e6:.0f}MB)")
        return True

    # Downloaded to a temporary name and moved into place only on success. A half-written ONNX
    # file at the right path is worse than no file: `available` would report true and the load
    # would fail at the first call of a live demo.
    partial = target.with_suffix(target.suffix + ".partial")
    print(f"  {name:24} downloading…", end="", flush=True)
    try:
        with urllib.request.urlopen(url) as response, partial.open("wb") as out:
            shutil.copyfileobj(response, out)
    except Exception as exc:  # noqa: BLE001 — the message matters more than the traceback
        partial.unlink(missing_ok=True)
        print(f"\r  {name:24} FAILED: {exc}")
        return False

    size = partial.stat().st_size
    if size < expect_at_least:
        partial.unlink(missing_ok=True)
        print(f"\r  {name:24} FAILED: got {size / 1e6:.0f}MB, expected far more")
        return False

    partial.replace(target)
    print(f"\r  {name:24} done ({size / 1e6:.0f}MB)      ")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="re-download files already present")
    args = parser.parse_args()

    MODELS.mkdir(parents=True, exist_ok=True)
    print(f"Kokoro-82M → {MODELS}")
    ok = all(fetch(name, url, size, force=args.force) for name, url, size in FILES)

    if ok:
        print("\nThe agent now speaks with Kokoro. Restart the API to pick it up.")
        return 0
    print("\nSomething did not download. The agent will use the browser voice until it does.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
