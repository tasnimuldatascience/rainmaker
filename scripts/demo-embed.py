#!/usr/bin/env python3
"""Publish the second tenant and wire their key into the demo website.

    python scripts/demo-embed.py

WHAT THIS DEMONSTRATES, and it is the thing the rest of the repository is in service of:
Rainmaker sells the agent to other businesses. Running this publishes a dental practice's agent
— their name, their voice, their prices, their disclosure wording, and only the calendar tool
granted — then pastes its public key into `demo/northgate.html`, a page that shares no
stylesheet, font or colour with our console.

Open that page and the agent in the corner is theirs. It quotes forty-five pounds because their
price list says so, not because ours does.

The key is minted by the store rather than chosen, which is why the HTML ships with a
placeholder and this script fills it in. It is public either way — it sits in the page source of
a customer's marketing site, it selects a published agent, and it authorises nothing.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api" / "src"))

PAGE = ROOT / "apps" / "console" / "public" / "demo" / "northgate.html"

from rainmaker.agents.spec import AgentSpec, Fact, Guardrails, Tier  # noqa: E402
from rainmaker.agents.store import AgentStore  # noqa: E402

TENANT, AGENT = "northgate", "alex"


def northgate() -> AgentSpec:
    """A dental practice's agent, configured exactly the way a customer would configure one."""
    return AgentSpec(
        tenant=TENANT,
        agent_id=AGENT,
        name="Alex",
        company="Northgate Dental",
        persona="a calm, unhurried dental receptionist who never rushes anybody",
        objective="Find out what is bothering them and get them booked in.",
        voice="male-warm",
        portrait="/agent/alex.jpg",
        knowledge=(
            Fact(
                "A routine check-up is forty-five pounds, including examination and polish.",
                source="price list",
                topic="pricing",
            ),
            Fact(
                "A hygienist appointment is sixty pounds for thirty minutes.",
                source="price list",
                topic="pricing",
            ),
            Fact(
                "A white filling is between one hundred and twenty and one hundred and eighty "
                "pounds, quoted after an examination.",
                source="price list",
                topic="pricing",
            ),
            Fact(
                "We are open Monday to Friday, eight thirty until six. Closed at weekends.",
                source="opening hours",
            ),
            Fact(
                "For an emergency we keep same-day slots. Out of hours, call one one one.",
                source="emergency policy",
            ),
            Fact(
                "We take both NHS and private patients, and have been on Northgate for "
                "thirty-one years.",
                source="about page",
            ),
        ),
        pricing=(
            Tier("Check-up", "£45", "examination and polish"),
            Tier("Hygienist", "£60", "30 minutes"),
            Tier("White filling", "£120–£180", "quoted after examination"),
        ),
        pricing_note="From our published price list. A dentist confirms after examining you.",
        # ONLY THE CALENDAR. A dental practice has no use for a research agent that reads a
        # prospect's website, and an agent that can reach a tool nobody needs is a tool nobody
        # is watching.
        tools=("calendar",),
        step_objectives=(
            ("discovery", "Find out what is bothering them, and for how long."),
            ("proposing", "Suggest the appointment that fits what they described. One sentence."),
        ),
        guardrails=Guardrails(
            disclosure=(
                "Hello — before we go on, I should say I'm an automated assistant, not a person. "
                "I can book you in, and I'll put you through to reception whenever you like."
            ),
            handoff_line="Of course, I'll put you through to reception now.",
        ),
    )


def main() -> int:
    store = AgentStore()
    saved = store.save(northgate())
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
    print("open http://localhost:5173/demo/northgate.html — the agent in the corner is theirs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
