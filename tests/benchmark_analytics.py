"""Reproducible 10,000-email, 90-day dashboard API benchmark (temporary data)."""

from __future__ import annotations

import json
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from mailcheck import config, db
from mailcheck.taxonomy import CATEGORIES
from mailcheck.web.app import create_app


def seed(path: Path, count: int = 10_000, *, end: datetime = datetime(2026, 3, 31, 23, 30)) -> None:
    conn = db.connect(path)
    account = db.add_account(conn, label="Synthetic", email="synthetic@example.test")
    messages, classifications = [], []
    for index in range(count):
        received = (end - timedelta(days=index % 90, minutes=index % 60)).isoformat()
        category = CATEGORIES[index % len(CATEGORIES)]
        pk = index + 1
        done = index % 4 == 0
        messages.append((pk, account, f"synthetic-{index}", str(index), "INBOX",
                         "recruiter@example.test", "Example recruiter", f"Application update {index}",
                         received, "Synthetic email body. No real mailbox data.", "Synthetic email",
                         received if done else None, end.isoformat()))
        classifications.append((pk, category.name, 0.95, "Example Company", "Engineer",
                                int(category.tier in ("act", "reply", "unknown")),
                                f"Synthetic {category.label} result", "synthetic-model", "v1",
                                int(category.name == "unclassified" and index % 3 == 0), end.isoformat()))
    conn.executemany(
        "INSERT INTO messages(id,account_id,message_id,uid,folder,from_addr,from_name,subject,"
        "date_utc,body_text,snippet,handled_at,fetched_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", messages)
    conn.executemany(
        "INSERT INTO classifications(message_pk,category,confidence,company,role,action_required,"
        "summary,model,prompt_version,retryable,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", classifications)
    conn.execute("INSERT INTO runs(started_at,finished_at,accounts_checked,fetched) VALUES(?,?,1,?)",
                 ((end - timedelta(days=89)).isoformat(), end.isoformat(), count))
    conn.commit()
    conn.close()


def main():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "mail.db"
        seed(path)
        with patch.object(config, "db_path", return_value=path), \
             patch.object(config, "config_path", return_value=Path(tmp) / "config.toml"), \
             TestClient(create_app()) as client:
            times = []
            for _ in range(5):
                started = time.perf_counter()
                response = client.get("/api/dashboard?start=2026-01-01&end=2026-03-31&tz=America/Chicago")
                times.append(time.perf_counter() - started)
                assert response.status_code == 200, response.text
                assert response.json()["summary"]["total"] == 10_000, response.json()["summary"]
                assert len(response.json()["messages"]["items"]) == 50
            print(json.dumps({"messages": 10_000, "days": 90,
                              "api_seconds": [round(value, 4) for value in times],
                              "maximum_seconds": round(max(times), 4)}))
            assert max(times) < 1, times


if __name__ == "__main__":
    main()
