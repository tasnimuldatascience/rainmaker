"""An MCP server that sends the follow-up after the call.

    python -m rainmaker.mcp.servers.email

OFF UNLESS CONFIGURED, AND THAT IS THE WHOLE DESIGN. Every other server here works on a fresh
clone with nothing set up. This one cannot: sending mail needs an account somewhere, and the
project's rule is that a reviewer can clone and run without one. So it ships disabled, the
broker reports it as unavailable with the reason attached, and the agenda knows before Nadia
promises to send anything.

WHAT IT DOES WHEN DISABLED IS THE INTERESTING PART. `draft_recap` still works with no SMTP at
all — it composes the message and hands it back. So the default experience is that Nadia writes
the follow-up, shows it to the prospect on screen, and says a person will send it. That is an
honest degradation rather than a dead feature: the drafting is the hard part and it is fully
demonstrated.

AN AGENT THAT CAN SEND MAIL IS AN AGENT THAT CAN SEND MAIL TO ANYONE, so this one cannot choose
the recipient freely: `send_recap` refuses any address that was not on the call. The call passes
the verified address in; the model never types one. A model that hallucinates a recipient on a
tool with a send button is a different category of bug from one that hallucinates a sentence.
"""

from __future__ import annotations

import os
import re
import smtplib
from email.message import EmailMessage
from typing import Any

from mcp.server.mcpserver import MCPServer

SMTP_HOST = os.environ.get("RAINMAKER_SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("RAINMAKER_SMTP_PORT", "587"))
SMTP_USER = os.environ.get("RAINMAKER_SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("RAINMAKER_SMTP_PASSWORD", "")
FROM_ADDRESS = os.environ.get("RAINMAKER_FROM_ADDRESS", SMTP_USER or "nadia@rainmaker.invalid")
FROM_NAME = os.environ.get("RAINMAKER_FROM_NAME", "Nadia at Rainmaker")

_ADDRESS = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

server = MCPServer(
    "rainmaker-email",
    instructions=(
        "Compose and send the post-call follow-up. `draft_recap` always works; `send_recap` "
        "needs SMTP configured and will only send to the address that was on the call."
    ),
)


@server.tool(
    title="Draft the follow-up",
    description=(
        "Compose the recap email from what happened on the call. Works with no mail server "
        "configured — returns the message rather than sending it."
    ),
)
def draft_recap(
    contact_name: str,
    company: str,
    summary: str,
    next_step: str = "",
    meeting_spoken: str = "",
    pricing_note: str = "",
) -> dict[str, Any]:
    """Args:
    contact_name: Who the mail is to.
    company: Their company.
    summary: What was discussed, in a sentence or two.
    next_step: What happens next.
    meeting_spoken: The booked slot as a person would say it, if one was booked.
    pricing_note: What was said about price, if anything.
    """
    greeting = f"Hi {contact_name.split()[0]}," if contact_name.strip() else "Hi,"
    body = [greeting, "", f"Thanks for the time just now. {summary.strip()}"]

    if meeting_spoken:
        body += ["", f"We're booked in for {meeting_spoken}. I'll send the invitation across."]
    if pricing_note:
        body += ["", pricing_note.strip()]
    if next_step:
        body += ["", next_step.strip()]

    body += [
        "",
        "One thing worth saying plainly: the call you just had was with an AI agent. "
        "A person on our side has the transcript and will pick it up from here.",
        "",
        f"— {FROM_NAME}",
    ]

    subject = f"Following up — {company}" if company else "Following up"
    return {
        "subject": subject,
        "body": "\n".join(body),
        "can_send": bool(SMTP_HOST),
        "why_not": "" if SMTP_HOST else "RAINMAKER_SMTP_HOST is not set",
    }


@server.tool(
    title="Send the follow-up",
    description=(
        "Send a drafted recap. Refuses any recipient that was not verified on the call, and "
        "fails cleanly when no mail server is configured."
    ),
)
def send_recap(
    to_address: str,
    subject: str,
    body: str,
    verified_address: str,
) -> dict[str, Any]:
    """Args:
    to_address: Where to send it.
    subject: The subject line.
    body: The message.
    verified_address: The address captured at the start of the call. Must match to_address.
    """
    if not SMTP_HOST:
        raise ValueError(
            "no mail server configured; set RAINMAKER_SMTP_HOST to enable sending"
        )
    if not _ADDRESS.match(to_address):
        raise ValueError(f"not a valid address: {to_address!r}")
    # THE GUARD THAT MATTERS. Comparison is case-insensitive because addresses are, and exact
    # otherwise: "close enough" on a recipient is how an agent mails a stranger.
    if to_address.strip().lower() != verified_address.strip().lower():
        raise ValueError(
            f"refusing to send to {to_address!r}: the address on this call was "
            f"{verified_address!r}"
        )

    message = EmailMessage()
    message["From"] = f"{FROM_NAME} <{FROM_ADDRESS}>"
    message["To"] = to_address
    message["Subject"] = subject
    message.set_content(body)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as smtp:
        smtp.starttls()
        if SMTP_USER:
            smtp.login(SMTP_USER, SMTP_PASSWORD)
        smtp.send_message(message)

    return {"sent": True, "to": to_address, "subject": subject}


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
