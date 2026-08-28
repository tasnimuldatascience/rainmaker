# The agent's face

The portrait here is a **synthetic image** from the SFHQ (Synthetic Faces High Quality) dataset.
It depicts **no real person**.

| File | Agent | Original | Licence |
|---|---|---|---|
| `nadia.jpg` | Nadia | `SFHQ_pt1_00000185` | Apache-2.0 |

Source: [`canva999888/SFHQ-Tiny-512-Part1`](https://huggingface.co/datasets/canva999888/SFHQ-Tiny-512-Part1)
on Hugging Face.

Selected by `scripts/fetch-face.py`, which downloads the same shard, prints a contact sheet, and
writes whichever one you pick.

```bash
python scripts/fetch-face.py                        # a sheet of candidates
python scripts/fetch-face.py --pick 3 --save nadia  # save one as agent/nadia.jpg
```

## Why a generated face rather than a stock photo

Pointing an AI salesperson at a real person's likeness is a problem regardless of what the
licence says. Most stock licences prohibit exactly this use — using a model's image in a way
that implies they endorse a product, or in synthetic media — and a portfolio repository is a
bad place to be relying on nobody reading the terms.

A generated face has no such person to wrong. It is also the honest match for what the product
is: an AI agent that says it is an AI, wearing a face that was never anybody's.

## One face, and why that is a casting decision

`portrait` is a field on the agent spec, the lip-sync engine caches a crop per face, and a test
asserts that a second tenant never answers in the first one's face — a bug that happened once and
is now guarded. So a tenant *can* have their own.

Both tenants here point at the same one, because the walkthrough does and the screenshots should
agree with it. What actually separates the two tenants is everything a buyer hears and is quoted:
the voice, the rate card, the unit they are billed in, the competitors, the tour and the
disclosure wording. Run `fetch-face.py` to mint another whenever a tenant wants one.
