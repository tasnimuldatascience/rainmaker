#!/usr/bin/env python3
"""Publish the second tenant and wire their key into the demo website.

    python scripts/demo-embed.py

WHAT THIS DEMONSTRATES, and it is the thing the rest of the repository is in service of:
Rainmaker sells the agent to other businesses. Running this publishes a GPU cloud's agent — their
name, their voice, their pitch, their prices, their disclosure wording, their competitors, their
tour — then pastes its public key into `demo/tessera.html`, a page that shares no stylesheet,
font or colour with our console.

Open that page and the agent in the corner is theirs. It quotes two dollars forty an hour because
their price list says so, not because ours does.

WHY A GPU CLOUD AND NOT THE DENTIST THIS REPLACED. The dental practice proved a tenant could
switch things OFF — no research, calendar only — and proved nothing about the product, because a
patient with toothache has no company to research, no seats to quote and no card to enter. It
exercised one step of ten. Tessera runs all ten, and it runs them on a business that is nothing
like ours: hours instead of seats, engineers instead of sales teams, a commitment discount
instead of an annual one.

IT ALSO MAKES THE RESEARCH STEP MEAN SOMETHING. Their buyer is identifiable from a careers page —
a company hiring ML engineers and mentioning training runs is a company that needs GPUs next
quarter — so the agent's opening line is a real buying signal rather than a party trick.

The key is minted by the store rather than chosen, which is why the HTML ships with a placeholder
and this script fills it in. It is public either way — it sits in the page source of a customer's
marketing site, it selects a published agent, and it authorises nothing.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api" / "src"))

PAGE = ROOT / "apps" / "console" / "public" / "demo" / "tessera.html"

from rainmaker.agents.spec import (  # noqa: E402
    AgentSpec,
    Competitor,
    Fact,
    Guardrails,
    Need,
    Tier,
    TourStop,
)
from rainmaker.agents.store import AgentStore  # noqa: E402

TENANT, AGENT = "tessera", "alex"

#: Where their own tour opens. Their site, not ours — the whole point of the guide step.
# Same override as `agents.store.CONSOLE`, for the same reason: vite takes the next free
# port when 5173 is busy, and a tour pinned to a port that moved drives to a dead page.
CONSOLE = os.environ.get("RAINMAKER_CONSOLE", "http://localhost:5173").rstrip("/")
SITE = f"{CONSOLE}/demo/tessera.html"


def tessera() -> AgentSpec:
    """A GPU cloud's agent, configured exactly the way a customer would configure one."""
    return AgentSpec(
        tenant=TENANT,
        agent_id=AGENT,
        name="Mara",
        company="Tessera Compute",
        persona="a straight-talking solutions engineer who would rather be exact than keen",
        objective=(
            "Work out what they are training and how much of it, show them the capacity and "
            "what it costs, and either get them started on a card or get them a time with an "
            "engineer."
        ),
        # A DIFFERENT VOICE FROM OURS, DELIBERATELY. Two tenants that sound the same are two
        # tenants a listener cannot tell apart, and the voice is the first thing a buyer on a
        # call notices. Ours is US English; hers is British.
        #
        # Her own face, not a borrowed one. Two agents wearing one photograph is a demo of a
        # template rather than of multi-tenancy, and the face is the first thing a buyer
        # notices. `scripts/fetch-face.py` picked it out of the same Apache-2.0 set of
        # synthetic portraits Nadia came from.
        voice="female-warm",
        portrait="/agent/mara.jpg",
        knowledge=(
            Fact(
                "Tessera rents H100 and A100 GPUs by the hour, in clusters of up to 64, with "
                "no minimum term.",
                source="positioning",
            ),
            Fact(
                "Capacity is live on the pricing page: what is free right now, in which region, "
                "and what it costs. Nothing is quoted that is not available.",
                source="positioning",
            ),
            Fact(
                "Nodes come up in about ninety seconds with CUDA, PyTorch and NCCL already "
                "configured, so a training run starts the same afternoon it is paid for.",
                source="product",
                topic="setup",
            ),
            Fact(
                "Storage is a shared NVMe volume mounted on every node in a cluster. Ingress "
                "and egress are not charged for.",
                source="product",
                topic="storage",
            ),
            Fact(
                "Teams committing to a month or more get the reserved rate, which is about a "
                "third below on-demand.",
                source="price list",
                topic="pricing",
            ),
            Fact(
                "Interconnect is 3.2 terabits per second InfiniBand within a cluster, which is "
                "what makes multi-node training worth doing at all.",
                source="product",
                topic="performance",
            ),
            Fact(
                "Most teams arrive because their cloud quota request has been pending for weeks "
                "and a model is not training in the meantime.",
                source="the problem",
                topic="why",
            ),
            Fact(
                "Billing is per second of node time, stops when the node does, and never has a "
                "control-plane fee on top.",
                source="price list",
                topic="pricing",
            ),
        ),
        # WHY ANYBODY BUYS GPU-HOURS, AND HOW TO SPOT EACH REASON IN WHAT RESEARCH FOUND.
        #
        # Without these the agent can read a website and do nothing with it — which is exactly
        # what happened on a real call: it opened by reading four job titles off a careers page
        # back to the person who wrote them, on a call about renting GPUs. A seller does not
        # recite a finding, they say what it suggests and check.
        #
        # Ordered strongest first: the more specific the signal, the more it is worth opening on.
        needs=(
            Need(
                signals=(
                    "machine learning", "deep learning", "ml engineer", "training", "train",
                    "model", "llm", "pytorch", "tensorflow", "inference", "fine-tun",
                    "data scien", "research engineer", "computer vision", "nlp",
                ),
                means=(
                    "they are training or serving models already, and the thing that limits "
                    "them is how much GPU they can get hold of"
                ),
                opener=(
                    "it looks like you are training or serving models already, so the thing "
                    "in your way is how much GPU you can actually get hold of"
                ),
                ask="what are you training at the moment, and what are you training it on?",
            ),
            Need(
                signals=("aws", "gcp", "azure", "cloud", "kubernetes", "terraform", "devops"),
                means=(
                    "they already run in the cloud, so they will know exactly what a GPU quota "
                    "queue feels like"
                ),
                opener=(
                    "it looks like you already run in the cloud, so you will know exactly what "
                    "waiting on a GPU quota feels like"
                ),
                ask="are you getting the GPU quota you ask for, or waiting on it?",
            ),
            Need(
                signals=(
                    "research and development", "r&d", "research engineer", "hiring",
                    "engineer", "product",
                ),
                means=(
                    "they are building something new, and new work runs into compute long "
                    "before anybody budgets for it"
                ),
                opener=(
                    "it looks like you are building something new, and new work tends to run "
                    "into compute long before anybody has budgeted for it"
                ),
                ask="is any of that work model training, or is it mostly product engineering?",
            ),
        ),
        tour=(
            TourStop(
                url=f"{SITE}#capacity",
                label="what is free right now",
                shows=(
                    "live capacity by region and card, with the hourly rate next to each one, "
                    "so the number being discussed is one they can see"
                ),
                scroll_to="Available now",
                answers=("capacity", "available", "region", "h100", "a100", "what do you have"),
            ),
            TourStop(
                url=f"{SITE}#pricing",
                label="the rate card",
                shows=(
                    "on-demand against reserved, per GPU-hour, with what the commitment "
                    "actually buys"
                ),
                scroll_to="Per GPU-hour",
                answers=("price", "pricing", "cost", "rate", "how much", "reserved"),
            ),
        ),
        competitors=(
            Competitor(
                name="a hyperscaler",
                positioning=(
                    "everything in one account, and the compliance paperwork already done"
                ),
                against=(
                    ("waiting", "give you nodes today rather than a quota request in a queue"),
                    ("price", "cost about half per GPU-hour at the reserved rate"),
                    ("egress", "not charge you to get your own weights back out"),
                ),
            ),
            Competitor(
                name="buying your own boxes",
                positioning=(
                    "the cheapest possible hour, if you can keep them busy and someone racks "
                    "them"
                ),
                against=(
                    ("time to start", "be training this afternoon rather than next quarter"),
                    ("bursts", "let you take 64 nodes for a week and give them back"),
                    ("who fixes it", "replace a failed card without it being your problem"),
                ),
            ),
        ),
        pricing=(
            Tier(
                "On-demand",
                "$3.60 / GPU-hour",
                "H100, no commitment, per-second billing",
                unit_amount=360,
                min_seats=1,
                unit_name="GPU-hour",
            ),
            Tier(
                "Reserved",
                "$2.40 / GPU-hour",
                "H100, one month or more, same hardware",
                unit_amount=240,
                min_seats=500,
                unit_name="GPU-hour",
            ),
            Tier("Cluster", "quoted", "32 nodes and up, dedicated interconnect"),
        ),
        pricing_note="Per GPU-hour, billed per second. Reserved needs a month's commitment.",
        pricing_period="month",
        currency="usd",
        # WHAT A BUYER CALLS IT, which is never "GPU-hour". They say "32 H100s", "a couple of
        # nodes", "sixteen cards". Without these the quantity detector heard nothing in "what
        # does it cost for 32 H100s for a month" and the quote fell back to a guessed size band
        # — thirty six dollars a month, said out loud, for eighty-four thousand of compute.
        unit_nouns=(
            "GPU", "H100", "A100", "card", "node", "accelerator", "chip", "device",
        ),
        # NOT AN ANNUAL DISCOUNT. Their commitment discount is already the difference between
        # two tiers, and stacking a second one on top would quote a number their price list
        # does not contain.
        annual_discount_pct=0,
        # NO EMAIL SERVER. Their follow-up goes through their own system, so the tool is not
        # granted and the agent cannot reach for it — which is the point of an allow-list.
        tools=("calendar", "crm", "research", "payments"),
        step_objectives=(
            (
                "discovery",
                "Find out what they are training, on what, and what is blocking them today.",
            ),
            (
                "guide",
                "Show live capacity and say what it would run. Never promise a card that is "
                "not on the page.",
            ),
        ),
        guardrails=Guardrails(
            disclosure=(
                "Quick thing first — I'm an AI, not a person. I can size a cluster, quote it and "
                "get you started, and I'll bring in an engineer whenever you want one."
            ),
            handoff_line=(
                "Sure — let me get one of our engineers on this with you. I'll pass along "
                "everything we've covered."
            ),
        ),
    )


def main() -> int:
    store = AgentStore()
    saved = store.save(tessera())
    live = store.publish(TENANT, AGENT, saved.version)
    store.close()

    print(f"published {live.tenant}/{live.agent_id} v{live.version}")
    print(f"  voice     {live.voice}")
    print(f"  tools     {', '.join(live.tools)}")
    print(f"  key       {live.public_key}")

    if not PAGE.exists():
        print(f"\n{PAGE} is missing; nothing to wire the key into.")
        return 1

    html = PAGE.read_text(encoding="utf-8")
    wired = re.sub(r'data-key="[^"]*"', f'data-key="{live.public_key}"', html, count=1)
    PAGE.write_text(wired, encoding="utf-8")

    print(f"\nwired into {PAGE.relative_to(ROOT)}")
    print(f"open {SITE} — the agent in the corner is theirs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
