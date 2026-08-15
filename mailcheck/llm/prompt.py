"""The classification prompt. Bump PROMPT_VERSION to invalidate the cache."""

from __future__ import annotations

import json

from ..models import NormalizedMessage
from ..taxonomy import CATEGORIES, MODEL_CATEGORIES

PROMPT_VERSION = "1"


def _category_block() -> str:
    lines = []
    for cat in CATEGORIES:
        if cat.name not in MODEL_CATEGORIES:
            continue
        lines.append(f"- {cat.name}: {cat.definition}")
    return "\n".join(lines)


SYSTEM = f"""You triage a job-seeker's inbox. For each email you are given, assign \
exactly one category and extract structured details.

CATEGORIES (use the exact name, lowercase):
{_category_block()}

RULES
1. Choose the single best category. Never invent a category name.
2. If an email both invites an interview and asks for documents, prefer \
interview_invite. If it asks the candidate to complete a test, prefer assessment.
3. A rejection is still a rejection even if it is warm or encourages future \
applications.
4. Automated "we have received your application" mail is application_ack, NOT a \
rejection and NOT an interview_invite.
5. Digests listing many jobs are job_alert, even when they come from a company the \
candidate applied to.
6. company: the hiring company, not the ATS vendor (not Greenhouse, Lever, Workday, \
Ashby, SmartRecruiters). Use null if unclear.
7. role: the specific job title. Use null if not stated.
8. deadline: an ISO date (YYYY-MM-DD) ONLY if the email states a date or deadline the \
candidate must meet. Never guess or compute a relative date. Use null otherwise.
9. action_required: true only if the candidate must reply or do something.
10. summary: one sentence, max 20 words, stating what the sender wants. No preamble.
11. confidence: 0.0-1.0, your own certainty in the category.

OUTPUT
Return ONLY a JSON object of this exact shape, with one entry per input email, \
preserving the given ids:

{{"results": [{{"id": "1", "category": "rejection", "confidence": 0.95, \
"company": "Acme", "role": "Backend Engineer", "deadline": null, \
"action_required": false, "summary": "Application declined after review."}}]}}

No markdown, no code fences, no commentary."""


STRICT_SUFFIX = """

REMINDER: your previous reply could not be parsed. Output raw JSON only. \
Start your reply with { and end it with }. No code fences, no explanation."""


def build_user_message(messages: list[tuple[str, NormalizedMessage]]) -> str:
    """``messages`` is a list of (id, message) pairs; ids are echoed back."""
    payload = [
        {
            "id": mid,
            "from": f"{msg.from_name} <{msg.from_addr}>".strip(),
            "subject": msg.subject,
            "date": msg.date_utc.strftime("%Y-%m-%d") if msg.date_utc else None,
            "body": msg.body,
        }
        for mid, msg in messages
    ]
    return "Classify these emails:\n" + json.dumps(payload, ensure_ascii=False, indent=None)
