"""IMAP connection presets, so `account add` is a two-field wizard for most users."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Preset:
    key: str
    name: str
    host: str
    port: int = 993
    use_ssl: bool = True
    note: str = ""
    supported: bool = True


PRESETS: list[Preset] = [
    Preset(
        "gmail",
        "Gmail / Google Workspace",
        "imap.gmail.com",
        note="Requires an App Password: turn on 2-Step Verification, then "
        "myaccount.google.com/apppasswords. Your normal password will not work.",
    ),
    Preset(
        "yahoo",
        "Yahoo Mail",
        "imap.mail.yahoo.com",
        note="Requires an App Password from Yahoo Account Security.",
    ),
    Preset(
        "icloud",
        "iCloud Mail",
        "imap.mail.me.com",
        note="Requires an app-specific password from appleid.apple.com.",
    ),
    Preset(
        "fastmail",
        "Fastmail",
        "imap.fastmail.com",
        note="Create an app password under Settings > Privacy & Security.",
    ),
    Preset(
        "outlook",
        "Outlook.com / Microsoft 365",
        "outlook.office365.com",
        note="NOT SUPPORTED YET. Microsoft disabled basic-auth IMAP, so app "
        "passwords do not work. This account needs OAuth2 via Microsoft Graph.",
        supported=False,
    ),
    Preset("custom", "Other (enter IMAP host manually)", "", port=993),
]

BY_KEY = {p.key: p for p in PRESETS}


def guess_from_email(email: str) -> Preset | None:
    domain = email.split("@")[-1].lower() if "@" in email else ""
    mapping = {
        "gmail.com": "gmail",
        "googlemail.com": "gmail",
        "yahoo.com": "yahoo",
        "yahoo.co.uk": "yahoo",
        "icloud.com": "icloud",
        "me.com": "icloud",
        "fastmail.com": "fastmail",
        "outlook.com": "outlook",
        "hotmail.com": "outlook",
        "live.com": "outlook",
    }
    key = mapping.get(domain)
    return BY_KEY.get(key) if key else None
