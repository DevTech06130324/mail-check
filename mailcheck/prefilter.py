"""Rule-based labelling before the LLM, for traffic that is unambiguous.

Deliberately conservative: a false negative costs a few tokens, a false
positive hides an interview invite. Rules fire only on exact sender or domain
matches — never on heuristics like a List-Unsubscribe header, because ATS
platforms set that too.
"""

from __future__ import annotations

from .config import PrefilterRule
from .models import Classification, NormalizedMessage

#: Senders that only ever emit job-board digests and saved-search alerts.
DEFAULT_ALERT_SENDERS = {
    "jobalerts-noreply@linkedin.com",
    "jobs-listings@linkedin.com",
    "jobs-noreply@linkedin.com",
    "alert@indeed.com",
    "alerts@indeed.com",
    "noreply@indeed.com",
    "invitetoapply@indeed.com",
    "no-reply@ziprecruiter.com",
    "jobs@ziprecruiter.com",
    "alerts@glassdoor.com",
    "noreply@glassdoor.com",
    "jobalerts@monster.com",
    "alerts@dice.com",
    "notifications@talent.com",
    "jobs@weworkremotely.com",
}

DEFAULT_ALERT_DOMAINS = {
    "jobalerts.linkedin.com",
    "e.linkedin.com",
}


def _match(rule: PrefilterRule, msg: NormalizedMessage) -> bool:
    if rule.sender and msg.from_addr.lower() != rule.sender.lower():
        return False
    if rule.sender_domain:
        domain = msg.from_addr.rsplit("@", 1)[-1].lower()
        if domain != rule.sender_domain.lower():
            return False
    if rule.subject_contains and rule.subject_contains.lower() not in msg.subject.lower():
        return False
    # A rule with no conditions must never match everything.
    return any([rule.sender, rule.sender_domain, rule.subject_contains])


def apply(
    msg: NormalizedMessage, rules: list[PrefilterRule] | None = None
) -> Classification | None:
    """Return a Classification to skip the LLM, or None to let it through."""
    for rule in rules or []:
        if _match(rule, msg):
            return Classification(
                category=rule.category,
                confidence=1.0,
                summary="Matched a local prefilter rule.",
                source="prefilter",
            )

    addr = msg.from_addr.lower()
    domain = addr.rsplit("@", 1)[-1] if "@" in addr else ""
    if addr in DEFAULT_ALERT_SENDERS or domain in DEFAULT_ALERT_DOMAINS:
        return Classification(
            category="job_alert",
            confidence=1.0,
            summary="Job board digest (matched a known alert sender).",
            source="prefilter",
        )
    return None
