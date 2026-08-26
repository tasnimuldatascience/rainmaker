# The agents' faces

Every portrait here is a **synthetic image** from the SFHQ (Synthetic Faces High Quality)
dataset. Each depicts **no real person**.

| File | Agent | Original | Licence |
|---|---|---|---|
| `liv.jpg` | Liv, at Rainmaker | `SFHQ_pt1_00000185` | Apache-2.0 |
| `mara.jpg` | Mara, at Tessera Compute | `SFHQ_pt1_00002743` | Apache-2.0 |
| `alex.jpg` | unused — kept as a second option | SFHQ | Apache-2.0 |

Source: [`canva999888/SFHQ-Tiny-512-Part1`](https://huggingface.co/datasets/canva999888/SFHQ-Tiny-512-Part1)
on Hugging Face.

Selected by `scripts/fetch-face.py`, which downloads the same shard, prints a contact sheet, and
writes whichever one you pick. No account and no token.

```bash
python scripts/fetch-face.py                        # a sheet of candidates
python scripts/fetch-face.py --pick 3 --save mara   # save one as agent/mara.jpg
```

## Why a generated face rather than a stock photo

Pointing an AI salesperson at a real person's likeness is a problem regardless of what the
licence says. Most stock licences prohibit exactly this use — using a model's image in a way
that implies they endorse a product, or in synthetic media — and a portfolio repository is a
bad place to be relying on nobody reading the terms.

A generated face has no such person to wrong. It is also the honest match for what the product
is: an AI agent that says it is an AI, wearing a face that was never anybody's.

## Why each tenant has their own

Two agents wearing one photograph is a demo of a template, not of multi-tenancy. A tenant
configures their name, their voice, their prices and their pitch; the face is the first of those
a buyer notices, and it was the one thing they had to share until this script existed.
