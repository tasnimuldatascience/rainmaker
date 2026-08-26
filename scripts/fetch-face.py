#!/usr/bin/env python3
"""Fetch a synthetic portrait for an agent, and let you pick which one.

    python scripts/fetch-face.py                      # a contact sheet to choose from
    python scripts/fetch-face.py --save mara --pick 3 # save candidate 3 as agent/mara.jpg

WHY A GENERATED FACE. Pointing an AI salesperson at a real person's likeness is a problem
whatever the licence says: most stock licences prohibit exactly this — a model's image used so
that it implies they endorse a product, or used in synthetic media — and a portfolio repository
is a bad place to be relying on nobody reading the terms. A StyleGAN face has no such person to
wrong, and it is the honest match for what the product is: an agent that says it is an AI,
wearing a face that was never anybody's.

THIS SCRIPT EXISTED IN THE ATTRIBUTION FILE BEFORE IT EXISTED HERE. `agent/ATTRIBUTION.md` has
cited it as the way the face was chosen since the first portrait landed, and it was never
written — so the provenance trail ended at a filename nobody could re-run. Writing it was also
the only way to give a second tenant a face of their own rather than borrowing tenant zero's,
which is the difference between demonstrating multi-tenancy and describing it.

WHAT IT DOWNLOADS. One 25MB shard of SFHQ-Tiny-512 from Hugging Face, Apache-2.0, no account
and no token. Nothing is written into the repository unless you pass `--save`.
"""

from __future__ import annotations

import argparse
import io
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FACES = ROOT / "apps" / "console" / "public" / "agent"
CACHE = ROOT / "data" / "faces"

DATASET = "canva999888/SFHQ-Tiny-512-Part1"
SHARD = "validation/validation-000000.tar"
URL = f"https://huggingface.co/datasets/{DATASET}/resolve/main/{SHARD}"

#: How many faces a sheet shows. Enough to choose from, few enough to look at.
CANDIDATES = 24


def shard() -> Path:
    """The dataset shard, downloaded once and kept out of git."""
    CACHE.mkdir(parents=True, exist_ok=True)
    local = CACHE / "sfhq-validation-000000.tar"
    if local.exists():
        return local

    print(f"downloading {SHARD} (~25MB, Apache-2.0, no account needed)…")
    with urllib.request.urlopen(URL, timeout=120) as response:
        local.write_bytes(response.read())
    return local


def candidates(offset: int) -> list[str]:
    with tarfile.open(shard()) as tar:
        names = sorted(n for n in tar.getnames() if n.endswith((".jpg", ".jpeg", ".png")))
    if not names:
        raise SystemExit("the shard contained no images — the dataset layout may have changed")
    return names[offset : offset + CANDIDATES]


def sheet(offset: int, out: Path) -> list[str]:
    """A contact sheet of the candidates, so a person picks rather than a hash does."""
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - the dev extra carries Pillow
        raise SystemExit("Pillow is needed to build the contact sheet: pip install pillow") from None

    picks = candidates(offset)
    cols, size = 8, 180
    rows = (len(picks) + cols - 1) // cols
    page = Image.new("RGB", (cols * size, rows * size), (12, 14, 20))
    with tarfile.open(shard()) as tar:
        for i, name in enumerate(picks):
            member = tar.extractfile(name)
            if member is None:
                continue
            face = Image.open(io.BytesIO(member.read())).convert("RGB").resize((size, size))
            page.paste(face, ((i % cols) * size, (i // cols) * size))
    page.save(out)
    return picks


def save(name: str, index: int, offset: int) -> Path:
    picks = candidates(offset)
    if not 0 <= index < len(picks):
        raise SystemExit(f"--pick must be between 0 and {len(picks) - 1}")

    try:
        from PIL import Image
    except ImportError:  # pragma: no cover
        raise SystemExit("Pillow is needed to save a face: pip install pillow") from None

    with tarfile.open(shard()) as tar:
        member = tar.extractfile(picks[index])
        if member is None:
            raise SystemExit("could not read that image out of the shard")
        face = Image.open(io.BytesIO(member.read())).convert("RGB")

    FACES.mkdir(parents=True, exist_ok=True)
    out = FACES / f"{name}.jpg"
    face.save(out, quality=88, optimize=True)
    print(f"wrote {out.relative_to(ROOT)} from {picks[index]}")
    print("\nAdd it to agent/ATTRIBUTION.md — the provenance of a face is not optional.")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offset", type=int, default=100, help="where in the shard to look")
    parser.add_argument("--save", help="agent name; writes agent/<name>.jpg")
    parser.add_argument("--pick", type=int, help="which candidate, by its index on the sheet")
    args = parser.parse_args()

    if args.save and args.pick is not None:
        save(args.save, args.pick, args.offset)
        return 0

    out = CACHE / "sheet.png"
    picks = sheet(args.offset, out)
    print(f"\nwrote {out} — open it and pick one\n")
    for i, name in enumerate(picks):
        print(f"  {i:>2}  {name}")
    print(f"\nthen: python scripts/fetch-face.py --offset {args.offset} --pick N --save <name>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
