"""Single source of truth for categories and urgency tiers.

The prompt, the report, and the web UI all read from here so they can never
drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Category:
    name: str
    tier: str
    label: str
    definition: str


TIER_ACT = "act"
TIER_REPLY = "reply"
TIER_INFO = "info"
TIER_NOISE = "noise"
TIER_UNKNOWN = "unknown"

TIER_ORDER = [TIER_ACT, TIER_REPLY, TIER_UNKNOWN, TIER_INFO, TIER_NOISE]

TIER_META = {
    TIER_ACT: ("Act now", "bold red"),
    TIER_REPLY: ("Needs a reply", "bold yellow"),
    TIER_UNKNOWN: ("Needs a manual look", "bold magenta"),
    TIER_INFO: ("For information", "cyan"),
    TIER_NOISE: ("Noise", "dim"),
}

CATEGORIES: list[Category] = [
    Category(
        "interview_invite",
        TIER_ACT,
        "Interview invite",
        "An invitation to interview, a phone/video screen, or a request to pick "
        "interview times. Includes scheduling links and calendar invitations for "
        "an interview.",
    ),
    Category(
        "assessment",
        TIER_ACT,
        "Assessment",
        "A coding test, online assessment, take-home task, or skills test the "
        "candidate must complete. These usually carry a deadline — always try to "
        "extract it.",
    ),
    Category(
        "offer",
        TIER_ACT,
        "Offer",
        "A job offer is extended, or offer terms, compensation, or a contract are "
        "being discussed.",
    ),
    Category(
        "info_request",
        TIER_REPLY,
        "Info request",
        "The sender asks the candidate for something before proceeding: "
        "references, salary expectations, notice period, availability, right to "
        "work, a CV, or other documents.",
    ),
    Category(
        "recruiter_outreach",
        TIER_REPLY,
        "Recruiter outreach",
        "An unsolicited approach from a recruiter or hiring manager about a role "
        "the candidate did NOT apply to. If it clearly responds to an application "
        "the candidate sent, it is not this category.",
    ),
    Category(
        "rejection",
        TIER_INFO,
        "Rejection",
        "The application was declined, the role was filled or closed, or the "
        "company is proceeding with other candidates.",
    ),
    Category(
        "application_ack",
        TIER_INFO,
        "Application received",
        "An automated confirmation that an application was received. No action is "
        "required and no decision has been made yet.",
    ),
    Category(
        "job_alert",
        TIER_NOISE,
        "Job alert",
        "A job board digest, saved-search alert, or marketing newsletter listing "
        "multiple vacancies. Not about any specific application.",
    ),
    Category(
        "other",
        TIER_NOISE,
        "Other",
        "Anything not related to the candidate's job search.",
    ),
    Category(
        "unclassified",
        TIER_UNKNOWN,
        "Unclassified",
        "Reserved for internal use when classification fails. The model must "
        "never return this.",
    ),
]

BY_NAME: dict[str, Category] = {c.name: c for c in CATEGORIES}

#: Categories the model is allowed to return.
MODEL_CATEGORIES = [c.name for c in CATEGORIES if c.name != "unclassified"]

UNCLASSIFIED = "unclassified"


def tier_of(category: str) -> str:
    cat = BY_NAME.get(category)
    return cat.tier if cat else TIER_UNKNOWN


def label_of(category: str) -> str:
    cat = BY_NAME.get(category)
    return cat.label if cat else category


def is_urgent(category: str) -> bool:
    return tier_of(category) == TIER_ACT


def coerce_category(raw: str | None) -> str:
    """Map whatever the model said onto a known category.

    Free models drift — they return ``Interview Invite``, ``interview-invite``,
    or a short phrase. Rather than discard an otherwise-good result we snap it
    to the nearest label, falling back to ``unclassified`` so it surfaces for a
    manual look instead of being silently mislabelled.
    """
    if not raw:
        return UNCLASSIFIED
    key = raw.strip().lower().replace(" ", "_").replace("-", "_")
    if key in BY_NAME and key != UNCLASSIFIED:
        return key
    # Substring match, longest name first so `interview_invite` wins over `other`.
    for name in sorted(MODEL_CATEGORIES, key=len, reverse=True):
        if name in key or key in name:
            return name
    return UNCLASSIFIED
