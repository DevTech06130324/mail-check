"""Opt-in live Ollama acceptance check using only synthetic emails.

Run: python tests/test_integration_ollama.py --base-url http://192.168.2.230:11440
It performs inference, but does not change config, mailboxes, or the database.
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone

import httpx

from mailcheck.config import LLMConfig
from mailcheck.llm import LLMClient, classify
from mailcheck.models import NormalizedMessage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", default="",
                        help="Pin a model; by default the one Ollama has loaded is used.")
    args = parser.parse_args()
    cfg = LLMConfig(base_url=args.base_url)
    fixtures = [
        ("Interview invitation", "We would like to interview you for the Backend Engineer role at Acme. Please reply with your availability.", "interview_invite", True),
        ("Application decision", "Thank you for applying for Backend Engineer at Acme. We have decided not to move forward with your application. No reply is needed.", "rejection", False),
        ("Application received", "We have received your application for Backend Engineer at Acme. Our team will review it. No action is required.", "application_ack", False),
        ("Your daily job digest", "Today's matching openings: Backend Engineer at Acme, Data Analyst at Example Corp, and Designer at Sample Ltd. This is an automated job alert digest.", "job_alert", False),
    ]
    messages = [NormalizedMessage(account_id=0, account_label="synthetic", message_id=f"synthetic-{i}",
        uid=str(i), folder="INBOX", from_addr="recruiting@example.test", from_name="Example",
        subject=subject, date_utc=datetime.now(timezone.utc), body=body)
        for i, (subject, body, _, _) in enumerate(fixtures)]
    with LLMClient.from_config(cfg) as client:
        client.model = args.model
        model = client.resolve_model()
        with httpx.Client(trust_env=False, timeout=10) as http:
            state = http.get(f"{cfg.base_url}/api/ps")
            state.raise_for_status()
            loaded = any(m.get("name") == model for m in state.json().get("models", []))
        print(json.dumps({"model": model, "loaded": loaded}), flush=True)
        for label in ("first_batch_already_loaded" if loaded else "cold_batch", "warm_batch"):
            start = time.perf_counter()
            results, errors = classify(client, messages, batch_size=4, concurrency=1)
            record = {"run": label, "seconds": round(time.perf_counter()-start, 2),
                "categories": [r.category for r in results],
                "action_required": [r.action_required for r in results], "errors": errors}
            print(json.dumps(record), flush=True)
            assert len(results) == len(fixtures), record
            assert not errors, record
            for result, (_, _, category, action) in zip(results, fixtures):
                assert result.category == category and result.action_required == action, record
        start = time.perf_counter()
        client.ping()
        print(json.dumps({"health": "ok", "seconds": round(time.perf_counter()-start, 2)}), flush=True)


if __name__ == "__main__":
    main()
