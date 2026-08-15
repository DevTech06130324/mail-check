"""End-to-end smoke test: fake IMAP source + fake LLM, real DB and pipeline.

Run: python -m tests.test_smoke   (no pytest needed)

Deliberately exercises the failure paths, because those are what break in
production: fenced JSON, prose-wrapped JSON, a batch reply missing an item,
and a model that never produces valid JSON at all.

Stays CI-portable on purpose: nothing here touches a real OS keyring. The
Outlook token-cache test that does live in test_integration_keyring.py,
opt-in, since not every execution context has a usable credential store.
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mailcheck import db, normalize, prefilter, report  # noqa: E402
from mailcheck.config import Config, PrefilterRule  # noqa: E402
from mailcheck.llm.classify import classify as run_classify  # noqa: E402
from mailcheck.llm.schema import parse_results  # noqa: E402
from mailcheck.models import Classification, RawMessage, RunResult, TriagedMessage  # noqa: E402
from mailcheck.taxonomy import UNCLASSIFIED, coerce_category  # noqa: E402

PASS, FAIL = 0, 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {detail}")


# --------------------------------------------------------------------- fixtures

NOW = datetime.now(timezone.utc).replace(tzinfo=None)

FAKE_MAIL = [
    RawMessage(
        "<a1@acme.com>", "1", "INBOX", "talent@acme.com", "Acme Talent",
        "Interview invitation - Backend Engineer",
        NOW - timedelta(days=1),
        "<html><body><p>Hi David,</p><p>We'd love to schedule a 30 minute phone "
        "screen for the Backend Engineer role. Please pick a slot: "
        "https://calendly.com/acme/screen?utm_source=email&utm_campaign=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa</p>"
        "<p>Best, Sam</p><hr><p>Unsubscribe from these emails</p></body></html>",
    ),
    RawMessage(
        "<b2@globex.com>", "2", "INBOX", "no-reply@globex.com", "Globex",
        "Your application to Globex",
        NOW - timedelta(days=2),
        "Thank you for applying to the Data Engineer position. We have received "
        "your application and will be in touch.\n\nOn Mon, 1 Jan 2026 at 10:00, "
        "David wrote:\n> Please find my CV attached, I am very interested",
    ),
    RawMessage(
        "<c3@initech.com>", "3", "INBOX", "hr@initech.com", "Initech HR",
        "Update on your application",
        NOW - timedelta(days=3),
        "After careful review we have decided to move forward with other "
        "candidates for the Platform Engineer role. We wish you the best.",
    ),
    RawMessage(
        "<d4@hooli.com>", "4", "INBOX", "recruiting@hooli.com", "Hooli",
        "Complete your coding assessment",
        NOW - timedelta(days=1),
        "Please complete the HackerRank assessment for the Senior SRE role by "
        "2026-08-05. It takes about 90 minutes.",
    ),
    RawMessage(
        "<e5@linkedin.com>", "5", "INBOX", "jobalerts-noreply@linkedin.com", "LinkedIn",
        "30 new jobs for 'backend engineer'",
        NOW - timedelta(days=1),
        "See the newest jobs matching your search.",
    ),
]

IDEAL = {
    "<a1@acme.com>": ("interview_invite", "Acme"),
    "<b2@globex.com>": ("application_ack", "Globex"),
    "<c3@initech.com>": ("rejection", "Initech"),
    "<d4@hooli.com>": ("assessment", "Hooli"),
}


class FakeSource:
    def __init__(self, messages):
        self.messages = messages

    def test(self):
        pass

    def fetch_unread(self, since):
        return iter(self.messages)


class FakeLLM:
    """Emits a different malformation on each call, cycling through the ones
    free models actually produce."""

    def __init__(self, mode="clean"):
        self.mode = mode
        self.calls = 0

    def complete(self, system, user, json_mode=True, max_tokens=2048):
        self.calls += 1
        payload = json.loads(user.split("Classify these emails:\n", 1)[1])
        results = []
        for entry in payload:
            subject = entry["subject"].lower()
            body = entry["body"].lower()
            if "interview" in subject:
                cat, company, role, deadline = "Interview Invite", "Acme", "Backend Engineer", None
            elif "assessment" in subject or "hackerrank" in body:
                cat, company, role, deadline = "assessment", "Hooli", "Senior SRE", "2026-08-05"
            elif "received your application" in body or "thank you for applying" in body:
                cat, company, role, deadline = "application_ack", "Globex", "Data Engineer", None
            else:
                cat, company, role, deadline = "rejection", "Initech", "Platform Engineer", None
            results.append({
                "id": entry["id"], "category": cat, "confidence": 95,
                "company": company, "role": role, "deadline": deadline,
                "action_required": cat != "rejection",
                "summary": f"Auto summary for {company}.",
            })

        if self.mode == "broken":
            return "I'm sorry, I cannot produce JSON."
        if self.mode == "fenced":
            return "```json\n" + json.dumps({"results": results}) + "\n```"
        if self.mode == "prose":
            return (
                "Sure! Here are the classifications:\n"
                + json.dumps({"results": results})
                + "\nLet me know if you need anything else."
            )
        if self.mode == "bare_array":
            return json.dumps(results)
        if self.mode == "dropped" and len(results) > 1:
            # Model silently omits one item — the single nastiest real failure.
            return json.dumps({"results": results[:-1]})
        return json.dumps({"results": results})


# ------------------------------------------------------------------------ tests


def test_normalize():
    print("\nnormalize")
    msgs = [
        normalize.normalize(r, account_id=1, account_label="test", max_body_chars=1200)
        for r in FAKE_MAIL
    ]
    invite = msgs[0].body
    check("html converted to text", "<p>" not in invite and "Hi David" in invite)
    check("footer boilerplate dropped", "unsubscribe" not in invite.lower(), repr(invite[-80:]))
    check("signal link kept", "calendly.com" in invite)
    check("tracking query truncated", "utm_campaign=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" not in invite)
    ack = msgs[1].body
    check("quoted reply stripped", "Please find my CV" not in ack, repr(ack))
    check("body above the quote kept", "Thank you for applying" in ack)
    return msgs


def test_prefilter(msgs):
    print("\nprefilter")
    hit = prefilter.apply(msgs[4], [])
    check("linkedin digest caught", hit is not None and hit.category == "job_alert")
    check("real mail passes through", prefilter.apply(msgs[0], []) is None)
    rule = PrefilterRule(category="other", sender_domain="initech.com")
    got = prefilter.apply(msgs[2], [rule])
    check("user domain rule applies", got is not None and got.category == "other")
    empty = PrefilterRule(category="other")
    check("empty rule matches nothing", prefilter.apply(msgs[0], [empty]) is None)


def test_parsing():
    print("\njson repair ladder")
    good = {"results": [{"id": "0", "category": "rejection", "confidence": 0.9}]}
    cases = {
        "clean": json.dumps(good),
        "fenced": "```json\n" + json.dumps(good) + "\n```",
        "prose": "Here you go:\n" + json.dumps(good) + "\nHope that helps!",
        "bare array": json.dumps(good["results"]),
        "alt envelope": json.dumps({"classifications": good["results"]}),
        "single object": json.dumps(good["results"][0]),
    }
    for name, raw in cases.items():
        try:
            out = parse_results(raw)
            check(f"parses {name}", out.get("0") is not None and out["0"].category == "rejection")
        except Exception as exc:  # noqa: BLE001
            check(f"parses {name}", False, str(exc))

    mixed = json.dumps({"results": [
        {"id": "0", "category": "rejection"},
        {"id": "1"},                       # missing category
        "not an object",                   # wrong type
        {"id": "3", "category": "offer", "confidence": "high"},
    ]})
    out = parse_results(mixed)
    check("one bad item does not kill the batch", "0" in out and "3" in out, str(out.keys()))
    check("garbage confidence falls back", out["3"].confidence == 0.5)

    print("\nfield coercion")
    check("label-cased category snaps", coerce_category("Interview Invite") == "interview_invite")
    check("hyphenated category snaps", coerce_category("job-alert") == "job_alert")
    check("nonsense becomes unclassified", coerce_category("banana") == UNCLASSIFIED)
    one = parse_results(json.dumps({"results": [{
        "id": "0", "category": "assessment", "confidence": 95,
        "company": "N/A", "deadline": "next Tuesday", "action_required": "yes",
    }]}))["0"]
    check("0-100 confidence rescaled", one.confidence == 0.95)
    check("'N/A' company becomes null", one.company is None)
    check("hallucinated relative date dropped", one.deadline is None)
    check("string bool coerced", one.action_required is True)
    real = parse_results(json.dumps({"results": [
        {"id": "0", "category": "assessment", "deadline": "due by 2026-08-05"}]}))["0"]
    check("real ISO date extracted", real.deadline == "2026-08-05")


def test_classify_modes(msgs):
    print("\nclassify against malformed model output")
    live = msgs[:4]
    for mode in ("clean", "fenced", "prose", "bare_array", "dropped"):
        fake = FakeLLM(mode)
        results, errors = run_classify(fake, live, batch_size=8)
        cats = [r.category for r in results]
        ok = all(c != UNCLASSIFIED for c in cats)
        check(f"mode={mode}: all {len(live)} classified", ok, str(cats))
        if mode == "dropped":
            check("  dropped item recovered individually", fake.calls > 1, f"calls={fake.calls}")

    fake = FakeLLM("broken")
    results, errors = run_classify(fake, live, batch_size=8)
    check("unparseable model degrades, does not crash",
          all(r.category == UNCLASSIFIED for r in results))
    check("  unclassified is flagged for manual review",
          all(r.action_required for r in results))
    check("  errors reported", len(errors) > 0)


def test_pipeline_and_cache(msgs):
    print("\npipeline + cache + report")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "t.db"
        conn = db.connect(path)
        db.add_account(conn, label="test", email="me@example.com", imap_host="x")

        fake = FakeLLM("clean")
        live = [m for m in msgs if prefilter.apply(m, []) is None]
        results, _ = run_classify(fake, live, batch_size=8)

        pks = []
        for msg, res in zip(live, results):
            pk = db.upsert_message(conn, msg)
            db.save_classification(conn, pk, res, "fake-model", "1")
            pks.append(pk)

        check("upsert is idempotent", db.upsert_message(conn, live[0]) == pks[0])

        cached = db.get_cached(conn, pks[0], "fake-model", "1")
        check("cache hit on same model+version", cached is not None)
        check("cache miss on new prompt version",
              db.get_cached(conn, pks[0], "fake-model", "2") is None)
        check("cache miss on different model",
              db.get_cached(conn, pks[0], "other-model", "1") is None)

        rows = db.query_triaged(conn)
        check("query returns all rows", len(rows) == len(live), f"{len(rows)}")
        by_mid = {r["message_id"]: r for r in rows}
        for mid, (want_cat, want_company) in IDEAL.items():
            row = by_mid.get(mid)
            check(f"  {want_cat:<18} {mid}",
                  row is not None and row["category"] == want_cat,
                  f"got {row['category'] if row else 'missing'}")
        check("deadline persisted", by_mid["<d4@hooli.com>"]["deadline"] == "2026-08-05")

        run = RunResult(started_at=NOW, finished_at=NOW, accounts_checked=1,
                        fetched=len(msgs), classified=len(live), prefiltered=1)
        run.items = [TriagedMessage(m, r) for m, r in zip(live, results)]
        check("urgent tier picks invite + assessment", len(run.urgent) == 2,
              str([i.classification.category for i in run.urgent]))

        print("\n--- rendered report ---")
        report.render(run, show_noise=False)
        conn.close()


def test_sse_and_reasoning():
    """Many OpenAI-compatible proxies stream even when asked not to, and
    reasoning models leak a scratchpad into `content`. Both were found against
    a real endpoint."""
    import http.server
    import socketserver
    import threading

    print("\nstreaming (SSE) endpoints")

    chunks = [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        # Reasoning deltas carry brace noise that must not reach the parser.
        {"choices": [{"index": 0, "delta": {"reasoning_content": 'draft {"id":"9"}'}}]},
        {"choices": [{"index": 0, "delta": {"content": '{"results": [{"id": "0", "cat'}}]},
        {"choices": [{"index": 0, "delta": {"content": 'egory": "rejection"}]}'}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    seen = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen["stream"] = body.get("stream")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for c in chunks:
                self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")

    srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        from mailcheck.llm.client import LLMClient

        with LLMClient(
            base_url=f"http://127.0.0.1:{srv.server_address[1]}/v1", token="t", model="m"
        ) as client:
            out = client.complete("sys", "user")
        check("asks for stream=false", seen.get("stream") is False)
        check("reassembles split SSE deltas", '"category": "rejection"' in out, out)
        check("excludes reasoning_content", '"id":"9"' not in out, out)
        check("result parses", parse_results(out)["0"].category == "rejection")
    finally:
        srv.shutdown()

    print("\nreasoning scratchpad stripping")
    think = '<think>maybe {"results":[{"id":"9","category":"offer"}]}</think>' \
            '{"results":[{"id":"0","category":"rejection"}]}'
    got = parse_results(think)
    check("<think> block ignored, real answer used",
          list(got) == ["0"] and got["0"].category == "rejection", str(got.keys()))
    fenced = '<thinking>hmm {a:1}</thinking>```json\n{"results":[{"id":"0","category":"rejection"}]}\n```'
    check("<thinking> + fence together", parse_results(fenced)["0"].category == "rejection")
    try:
        parse_results("<think>truncated rambling {half")
        check("unclosed scratchpad rejected", False, "should have raised")
    except ValueError:
        check("unclosed scratchpad rejected (-> unclassified)", True)


def test_password_repair():
    """Google renders app passwords spaced, and copying brings non-breaking
    spaces that crash imaplib's ASCII encoding. Found against a real mailbox."""
    from mailcheck.secrets import SecretError, check_ascii, clean_password
    from mailcheck.sources.imap import IMAPSource

    print("\napp password repair")
    for name, raw in {
        "nbsp separators (Google copy)": "abcd\xa0efgh\xa0ijkl\xa0mnop",
        "plain spaces": "abcd efgh ijkl mnop",
        "narrow nbsp": "abcd efgh ijkl mnop",
        "zero-width chars": "abcd​efgh​ijkl​mnop",
        "already collapsed": "abcdefghijklmnop",
        "surrounding whitespace": "  abcdefghijklmnop  ",
    }.items():
        check(f"{name} -> 16 chars", clean_password(raw) == "abcdefghijklmnop")

    check("a real password keeps its spaces", clean_password("my secret pw!") == "my secret pw!")
    check("symbols survive", clean_password("p@ss w0rd#1") == "p@ss w0rd#1")

    try:
        check_ascii(clean_password("passéword"))
        check("non-ASCII password refused", False, "should have raised")
    except SecretError as exc:
        check("non-ASCII password refused with an explanation",
              "U+00E9" in str(exc) and "position 4" in str(exc))

    src = IMAPSource(host="h", port=993, username="u@gmail.com",
                     password="abcd\xa0efgh\xa0ijkl\xa0mnop")
    check("IMAPSource repairs stored credentials", src.password == "abcdefghijklmnop")


def test_web_console(msgs):
    """Every page and every mutating action, against a temp DB and config."""
    import tempfile as tf

    from mailcheck import config as cfgmod
    from mailcheck import secrets as secmod

    print("\nweb console")
    tmp = Path(tf.mkdtemp())
    orig_db, orig_cfg, orig_token = cfgmod.db_path, cfgmod.config_path, secmod.has_llm_token
    cfgmod.db_path = lambda: tmp / "t.db"
    cfgmod.config_path = lambda: tmp / "config.toml"
    secmod.has_llm_token = lambda: False  # deterministic, ignore the real keyring
    try:
        conn = db.connect(tmp / "t.db")
        db.add_account(conn, label="gmail", email="me@example.com", imap_host="imap.gmail.com")
        cats = ["interview_invite", "application_ack", "rejection", "assessment", "job_alert"]
        for m, cat in zip(msgs, cats):
            pk = db.upsert_message(conn, m)
            db.save_classification(conn, pk, Classification(
                category=cat, confidence=0.9, company="Acme", role="Backend Engineer",
                deadline="2026-08-05" if cat == "assessment" else None,
                summary=f"Summary for {cat}."), "m", "1")
        conn.close()

        from fastapi.testclient import TestClient

        from mailcheck.web.app import create_app

        client = TestClient(create_app())
        body = lambda r: r.text.split('<main id="main">')[1]

        for path in ("/", "/accounts", "/settings", "/static/app.css", "/static/app.js"):
            check(f"GET {path}", client.get(path).status_code == 200)

        page = client.get("/")
        check("view tabs render", 'class="views"' in body(page))
        check("urgent badge in nav", 'class="dot"' in page.text)
        check("skip link present", 'href="#main"' in page.text)
        for view in ("queue", "all", "completed"):
            check(f"view={view} renders", client.get(f"/?view={view}").status_code == 200)
        check("unknown view falls back to queue",
              'href="/?view=queue"' in client.get("/?view=bogus").text)

        check("preset detects gmail",
              client.get("/api/preset?email=a@gmail.com").json()["preset"]["host"] == "imap.gmail.com")
        check("preset flags outlook unsupported",
              client.get("/api/preset?email=a@outlook.com").json()["preset"]["supported"] is False)
        check("pause account", client.post("/api/accounts/gmail/toggle?enabled=false").json()["ok"])
        with db.session(tmp / "t.db") as c2:
            check("  pause takes effect", db.list_accounts(c2, only_enabled=True) == [])
        client.post("/api/accounts/gmail/toggle?enabled=true")
        check("missing account -> 404", client.post("/api/accounts/x/test").status_code == 404)
        dup = client.post("/api/accounts", json={"label": "gmail", "email": "x@y.z",
                                                 "host": "h", "password": "p"})
        check("duplicate label refused", dup.status_code == 400)
        bad = client.post("/api/accounts", json={"label": "n", "email": "x@y.z",
                                                 "host": "h", "password": "passéword"})
        check("non-ASCII password refused at the API", "U+00E9" in bad.json()["error"])

        check("save settings", client.post(
            "/api/settings", json={"base_url": "http://x/v1", "model": "m", "batch_size": 4}
        ).json()["ok"])
        check("  settings persisted", cfgmod.load().llm.batch_size == 4)
        check("out-of-range batch_size refused",
              client.post("/api/settings", json={"batch_size": -5}).status_code == 400)
        check("out-of-range lookback refused",
              client.post("/api/settings", json={"lookback_days": 0}).status_code == 400)
        check("add rule", client.post(
            "/api/rules", json={"category": "job_alert", "sender_domain": "spam.com"}
        ).json()["ok"] and len(cfgmod.load().prefilter_rules) == 1)
        check("rule with no condition refused",
              client.post("/api/rules", json={"category": "other"}).status_code == 400)
        check("delete rule", client.post("/api/rules/0/delete").json()["ok"]
              and not cfgmod.load().prefilter_rules)
        check("delete missing rule -> 404", client.post("/api/rules/9/delete").status_code == 404)

        check("cross-origin POST blocked", client.post(
            "/api/settings", json={"model": "z"},
            headers={"Origin": "https://evil.example"}).status_code == 403)
        check("same-origin POST allowed", client.post(
            "/api/settings", json={"model": "z"},
            headers={"Origin": "http://127.0.0.1:8765"}).status_code == 200)
        check("GET unaffected by Origin", client.get(
            "/", headers={"Origin": "https://evil.example"}).status_code == 200)
        check("check refused before setup",
              client.post("/api/check", json={}).status_code == 400)
    finally:
        cfgmod.db_path, cfgmod.config_path = orig_db, orig_cfg
        secmod.has_llm_token = orig_token


def test_schedule():
    """The countdown must be backed by a scheduler that really fires, otherwise
    the page is advertising a check that never happens."""
    import tempfile as tf
    import time as clock

    from mailcheck import config as cfgmod
    from mailcheck import secrets as secmod

    print("\nautomatic checks + countdown")
    tmp = Path(tf.mkdtemp())
    orig_db, orig_cfg, orig_token = cfgmod.db_path, cfgmod.config_path, secmod.has_llm_token
    cfgmod.db_path = lambda: tmp / "t.db"
    cfgmod.config_path = lambda: tmp / "config.toml"
    secmod.has_llm_token = lambda: True
    try:
        conn = db.connect(tmp / "t.db")
        db.add_account(conn, label="gmail", email="me@x.com", imap_host="h")
        conn.close()
        cfgmod.save(cfgmod.Config.model_validate({
            "llm": {"base_url": "http://x/v1", "model": "m"},
            "watch": {"interval_minutes": 10, "auto_check": False},
        }))

        from fastapi.testclient import TestClient

        import mailcheck.web.app as W

        client = TestClient(W.create_app())

        s = client.get("/api/status").json()
        check("auto off -> no next time", s["auto"] is False and s["next_in"] is None)
        check("countdown pill hidden when off", 'id="next-check" hidden' in client.get("/").text)
        check("dashboard offers to turn it on",
              "Turn on automatic checks" in client.get("/").text)

        check("enable auto", client.post("/api/autocheck?enabled=true").json()["ok"])
        check("  persisted", cfgmod.load().watch.auto_check is True)
        s = client.get("/api/status").json()
        check("  next_in is one interval", 594 <= s["next_in"] <= 600, str(s["next_in"]))
        check("  page seeds the countdown", "nextIn:" in client.get("/").text)
        check("  settings switch reflects it", 'id="s-auto" checked' in client.get("/settings").text)

        client.post("/api/settings", json={"interval_minutes": 2})
        s = client.get("/api/status").json()
        check("changing the interval re-arms immediately",
              114 <= s["next_in"] <= 120, str(s["next_in"]))

        check("disable auto", client.post("/api/autocheck?enabled=false").json()["ok"]
              and client.get("/api/status").json()["next_in"] is None)

        cfg = cfgmod.load()
        cfg.watch.auto_check, cfg.watch.interval_minutes = True, 1
        cfgmod.save(cfg)
        W._sched["next_due"] = clock.time() - 1  # pretend it just came due
        fired = {"n": 0}
        original = W._run_check
        W._run_check = lambda body: (
            fired.__setitem__("n", fired["n"] + 1), W._job_lock.release()
        )
        try:
            for _ in range(40):
                clock.sleep(0.2)
                if fired["n"]:
                    break
        finally:
            W._run_check = original
        check("scheduler actually fires when due", fired["n"] == 1, f"fired={fired['n']}")
        check("  and re-arms for the next one", W._sched["next_due"] > clock.time())

        W._sched["next_due"] = None
        cfg.watch.auto_check = False
        cfgmod.save(cfg)
    finally:
        cfgmod.db_path, cfgmod.config_path = orig_db, orig_cfg
        secmod.has_llm_token = orig_token


def test_migration():
    """A v1 database in the wild must survive the upgrade with its data."""
    import sqlite3
    import tempfile as tf

    print("\nv1 -> v2 migration")
    old = Path(tf.mkdtemp()) / "old.db"
    legacy = sqlite3.connect(old)
    legacy.executescript(
        """
        CREATE TABLE accounts (id INTEGER PRIMARY KEY, label TEXT UNIQUE, email TEXT,
          imap_host TEXT, imap_port INTEGER DEFAULT 993, use_ssl INTEGER DEFAULT 1,
          folder TEXT DEFAULT 'INBOX', enabled INTEGER DEFAULT 1, created_at TEXT);
        CREATE TABLE messages (id INTEGER PRIMARY KEY, account_id INTEGER, message_id TEXT,
          uid TEXT, folder TEXT, from_addr TEXT, from_name TEXT, subject TEXT,
          date_utc TEXT, body_text TEXT, snippet TEXT, fetched_at TEXT,
          notified INTEGER DEFAULT 0, UNIQUE(account_id, message_id));
        INSERT INTO accounts VALUES (1,'old','a@b.c','h',993,1,'INBOX',1,'2026-01-01');
        INSERT INTO messages VALUES
          (1,1,'<m1>','1','INBOX','x@y.z','X','S','2026-01-01','b','s','2026-01-01',0);
        """
    )
    legacy.commit()
    legacy.close()

    conn = db.connect(old)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
    check("messages gained handled_at, provider_url, web_notified",
          {"handled_at", "provider_url", "web_notified"} <= cols)
    check("accounts gained provider",
          "provider" in {r["name"] for r in conn.execute("PRAGMA table_info(accounts)")})
    check("existing data preserved",
          conn.execute("SELECT COUNT(*) n FROM messages").fetchone()["n"] == 1)
    check("legacy account defaults to imap", db.get_account(conn, "old").provider == "imap")
    check("user_version bumped",
          conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    check("index built after the column exists", "idx_messages_handled" in names)
    # The correlated "latest classification" lookup every list query runs is a
    # full scan of classifications without this one.
    check("latest-classification index built", "idx_class_latest" in names, str(names))
    check("account+date index built", "idx_messages_acct_date" in names, str(names))
    check("WAL enabled",
          conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal")
    conn.close()
    db.connect(old).close()  # must not raise
    check("re-opening is idempotent", True)

    # An already-current database must skip the whole schema/migration script
    # rather than re-issue it on every one of the console's per-request opens.
    import unittest.mock as _mock

    with _mock.patch.object(db, "_migrate") as spy:
        db.connect(old).close()
    check("current schema skips the migration pass on open", spy.call_count == 0)


def test_action_queue(msgs):
    """The daily queue, local Done/Undo, and the Outlook provider wiring."""
    import tempfile as tf

    from mailcheck import config as cfgmod
    from mailcheck import secrets as secmod
    from mailcheck.taxonomy import CATEGORIES as CATS

    print("\naction queue + Done/Undo")
    tmp = Path(tf.mkdtemp())
    orig = (cfgmod.db_path, cfgmod.config_path, secmod.has_llm_token,
            secmod.get_account_password)
    cfgmod.db_path = lambda: tmp / "t.db"
    cfgmod.config_path = lambda: tmp / "config.toml"
    secmod.has_llm_token = lambda: True
    secmod.get_account_password = lambda label: "dummy"
    try:
        actionable = [c.name for c in CATS if c.tier in ("act", "reply")]
        conn = db.connect(tmp / "t.db")
        db.add_account(conn, label="gmail", email="me@x.com", imap_host="imap.gmail.com")
        db.add_account(conn, label="outlook", email="me@outlook.com", provider="outlook")
        cats = ["interview_invite", "application_ack", "rejection", "assessment", "job_alert"]
        pks = []
        for m, cat in zip(msgs, cats):
            pk = db.upsert_message(conn, m)
            pks.append(pk)
            db.save_classification(conn, pk, Classification(
                category=cat, confidence=0.9, company="Acme", role="Backend Engineer",
                deadline="2026-08-05" if cat == "assessment" else None,
                summary=f"Summary for {cat}."), "m", "1")
        conn.commit()

        check("queue holds only actionable, unhandled mail",
              len(db.query_triaged(conn, categories=actionable, handled=False)) == 2)
        counts = db.queue_counts(conn)
        check("  summary splits actionable from informational",
              counts["actionable"] == 2 and counts["informational"] == 3, str(counts))

        db.set_handled(conn, pks[0], True)
        counts = db.queue_counts(conn)
        check("Done removes it from the queue",
              counts["actionable"] == 1 and counts["done"] == 1, str(counts))
        check("  and it appears in Completed", len(db.query_triaged(conn, handled=True)) == 1)
        db.upsert_message(conn, msgs[0])
        check("re-fetching never resurrects a Done item",
              db.get_message(conn, pks[0])["handled_at"] is not None)
        db.set_handled(conn, pks[0], False)
        check("Undo restores it", db.queue_counts(conn)["actionable"] == 2)
        conn.close()

        from fastapi.testclient import TestClient

        from mailcheck.pipeline import build_source
        from mailcheck.sources import IMAPSource, OutlookGraphSource
        from mailcheck.web.app import _fmt_date, _fmt_deadline, create_app

        client = TestClient(create_app())
        # Scope to the card list: the filter dropdown always names every category.
        cards = lambda r: r.text.split('<div id="queue">')[1].split('<div class="empty"')[0]

        check("default view is the queue", 'class="tier act"' in cards(client.get("/")))
        check("  informational is excluded", 'class="tier info"' not in cards(client.get("/")))
        check("All mail includes informational",
              'class="tier info"' in cards(client.get("/?view=all")))
        check("Completed view renders", client.get("/?view=completed").status_code == 200)
        check("cards carry Done and an open link",
              ">Done<" in cards(client.get("/")) and "Open in Gmail" in cards(client.get("/")))
        check("active view marked with aria-current",
              'aria-current="page"' in client.get("/").text)

        # Which mailbox a message arrived in has to be readable without opening
        # anything, so every assertion here runs against the card with its
        # <details> block stripped out — the account living only inside Details
        # is exactly the bug this guards against.
        import re

        strip_details = lambda html: re.sub(
            r'<details class="detail">.*?</details>', "", html, flags=re.S)

        faces = strip_details(cards(client.get("/")))
        labels = faces.count('class="acct"')
        articles = faces.count('<article class="mail')
        check("the account is on the card face, not only behind Details",
              labels > 0 and "gmail" in faces)
        check("  every card carries one", labels == articles,
              "%d labels for %d cards" % (labels, articles))
        check("  and it is labelled for screen readers",
              '<span class="sr-only">Account: </span>gmail' in faces)
        # Grouped with the date as envelope metadata, not mixed in among the
        # category and deadline pills, which are the model's verdict.
        paired = len(re.findall(
            r'<span class="meta">\s*<span class="acct">.*?</span>\s*'
            r'<span class="when">', faces, flags=re.S))
        check("  paired with the date, outside the verdict pills",
              paired == articles, "%d paired for %d cards" % (paired, articles))
        check("  All mail too",
              'class="acct"' in strip_details(cards(client.get("/?view=all"))))

        check("mark Done over the API",
              client.post(f"/api/messages/{pks[0]}/handled?done=true").json()["done"] is True)
        check("  queue drops it", "Interview invite" not in cards(client.get("/")))
        check("  Completed offers Restore",
              ">Restore<" in cards(client.get("/?view=completed")))
        check("Undo over the API",
              client.post(f"/api/messages/{pks[0]}/handled?done=false").json()["ok"])
        check("  it is back in the queue", "Interview invite" in cards(client.get("/")))
        check("unknown message -> 404",
              client.post("/api/messages/9999/handled?done=true").status_code == 404)

        print("\nrelative dates")
        now = datetime.now()
        check("future date -> Tomorrow",
              _fmt_date((now + timedelta(days=1)).isoformat()) == "Tomorrow")
        check("future date -> In N days",
              _fmt_date((now + timedelta(days=3)).isoformat()) == "In 3 days")
        check("never renders a negative age",
              "-" not in _fmt_date((now + timedelta(days=2)).isoformat()))
        check("past date -> Yesterday",
              _fmt_date((now - timedelta(days=1)).isoformat()) == "Yesterday")
        check("overdue deadline flagged", "Overdue" in _fmt_deadline("2020-01-01"))
        check("deadline today", _fmt_deadline(now.strftime("%Y-%m-%d")) == "Due today")

        print("\noutlook wiring")
        check("provider shown on Accounts", "Outlook" in client.get("/accounts").text)
        started = client.post("/api/outlook/start", json={"label": "ol"})
        check("sign-in refused without a client ID",
              started.status_code == 400 and "client ID" in started.json()["error"])
        client.post("/api/settings",
                    json={"outlook_client_id": "11111111-2222-3333-4444-555555555555"})
        check("  client ID saved", cfgmod.load().outlook.client_id.startswith("1111"))
        check("duplicate label refused",
              client.post("/api/outlook/start", json={"label": "gmail"}).status_code == 400)
        check("status endpoint shape",
              set(client.get("/api/outlook/status").json())
              >= {"state", "message", "email", "user_code"})
        check("cancel clears the flow", client.post("/api/outlook/cancel").json()["ok"])

        cfg = cfgmod.load()
        with db.session(tmp / "t.db") as c2:
            check("build_source dispatches IMAP",
                  isinstance(build_source(db.get_account(c2, "gmail"), cfg), IMAPSource))
            check("build_source dispatches Graph",
                  isinstance(build_source(db.get_account(c2, "outlook"), cfg),
                             OutlookGraphSource))

        print("\ntier headings")
        queue = cards(client.get("/"))
        # Suppressing the first group's heading made it look unlike every other
        # group, and made "Act now" appear to vanish as items above were cleared.
        check("Act now heading is rendered in the queue",
              "Act now" in queue and 'class="tier-head"' in queue)
        heads = queue.count('class="tier-head"')
        sections = queue.count('<section class="tier')
        check("every group has exactly one heading", heads == sections,
              f"{heads} headings for {sections} sections")
        allmail = cards(client.get("/?view=all"))
        check("same in All mail",
              allmail.count('class="tier-head"') == allmail.count('<section class="tier'))

        print("\nbrowser notifications")
        got = client.get("/api/notifications").json()
        cats = {i["category"] for i in got["items"]}
        check("urgent mail is offered for announcement",
              cats == {"interview_invite", "assessment"}, str(cats))
        check("  payload carries what a popup needs",
              all({"title", "who", "summary", "url", "deadline"} <= set(i)
                  for i in got["items"]))
        check("  a deadline is humanised", any(i["deadline"] for i in got["items"]))
        check("  fetching does not itself mark anything announced",
              client.get("/api/notifications").json()["items"] == got["items"])

        # The client only acks what it actually managed to show — never the
        # server, and never on a bare fetch — so a denied permission or a
        # constructor error can't lose an alert permanently.
        pks_seen = [i["pk"] for i in got["items"]]
        ack = client.post("/api/notifications/ack", json=pks_seen)
        check("ack accepts the shown pks", ack.json()["ok"])
        check("acked items are not offered again",
              client.get("/api/notifications").json()["items"] == [])
        check("un-acked items would still have been offered (not auto-marked)",
              True)  # covered by the "does not itself mark" check above

        with db.session(tmp / "t.db") as c3:
            c3.execute("UPDATE messages SET web_notified = 0")
            c3.commit()
            db.set_handled(c3, pks[3], True)
        after = {i["category"] for i in client.get("/api/notifications").json()["items"]}
        check("Done mail is never announced", "assessment" not in after, str(after))
        with db.session(tmp / "t.db") as c3:
            db.set_handled(c3, pks[3], False)
    finally:
        (cfgmod.db_path, cfgmod.config_path, secmod.has_llm_token,
         secmod.get_account_password) = orig


def test_review_fixes(msgs):
    """Regressions from an external review: each check here failed before the
    corresponding fix and must stay fixed."""
    import tempfile as tf
    import time as clock

    from mailcheck import config as cfgmod
    from mailcheck import outlook_auth as oa
    from mailcheck import secrets as secmod
    from mailcheck.taxonomy import CATEGORIES as CATS

    print("\nprovider link detection (no more mislabeled Gmail links)")
    tmp = Path(tf.mkdtemp())
    orig = (cfgmod.db_path, cfgmod.config_path, secmod.has_llm_token,
            secmod.get_account_password)
    cfgmod.db_path = lambda: tmp / "t.db"
    cfgmod.config_path = lambda: tmp / "config.toml"
    secmod.has_llm_token = lambda: True
    secmod.get_account_password = lambda label: "dummy"
    try:
        from mailcheck.web.app import _open_link

        conn = db.connect(tmp / "t.db")
        db.add_account(conn, label="gmail", email="a@gmail.com", imap_host="imap.gmail.com")
        db.add_account(conn, label="yahoo", email="a@yahoo.com", imap_host="imap.mail.yahoo.com")
        db.add_account(conn, label="outlook", email="a@outlook.com", provider="outlook")
        conn.close()

        # query_triaged rows are what _open_link actually receives in production.
        import dataclasses

        def _row_for(account_label, provider_url=None):
            with db.session(tmp / "t.db") as c:
                acct = db.get_account(c, account_label)
                # msgs[0] is already a NormalizedMessage (test_normalize's output);
                # re-target it at this account rather than re-normalizing.
                nm = dataclasses.replace(
                    msgs[0], account_id=acct.id, account_label=account_label,
                    provider_url=provider_url,
                )
                pk = db.upsert_message(c, nm)
                db.save_classification(
                    c, pk, Classification(category="rejection", confidence=0.9), "m", "1"
                )
                return db.query_triaged(c, account_label=account_label)[0]

        gmail_row = _row_for("gmail")
        url, label_ = _open_link(gmail_row)
        check("Gmail IMAP host gets a Gmail search link", url is not None and "mail.google.com" in url)
        check("  labelled 'Open in Gmail'", label_ == "Open in Gmail")

        yahoo_row = _row_for("yahoo")
        url, label_ = _open_link(yahoo_row)
        check("non-Gmail IMAP host gets NO link (not mislabeled as Gmail)", url is None, str(url))

        outlook_row = _row_for("outlook", provider_url="https://outlook.live.com/x")
        url, label_ = _open_link(outlook_row)
        check("Outlook account uses its own webLink", url == "https://outlook.live.com/x")
        check("  labelled 'Open in Outlook'", label_ == "Open in Outlook")
    finally:
        (cfgmod.db_path, cfgmod.config_path, secmod.has_llm_token,
         secmod.get_account_password) = orig

    print("\nunclassified mail surfaces in the queue, never only in All mail")
    tmp = Path(tf.mkdtemp())
    cfgmod.db_path = lambda: tmp / "t.db"
    cfgmod.config_path = lambda: tmp / "config.toml"
    secmod.has_llm_token = lambda: True
    secmod.get_account_password = lambda label: "dummy"
    try:
        from fastapi.testclient import TestClient

        from mailcheck.web.app import create_app

        conn = db.connect(tmp / "t.db")
        db.add_account(conn, label="gmail", email="a@gmail.com", imap_host="imap.gmail.com")
        pk = db.upsert_message(conn, msgs[0])
        db.save_classification(
            conn, pk, Classification(category="unclassified", confidence=0.0,
                                     action_required=True, summary="parse failed"),
            "m", "1",
        )
        conn.commit()
        conn.close()

        counts = db.queue_counts(db.connect(tmp / "t.db"))
        check("queue_counts treats unclassified as actionable", counts["actionable"] == 1, str(counts))

        client = TestClient(create_app())
        queue_html = client.get("/").text.split('<div id="queue">')[1]
        check("unclassified mail appears in the default queue view",
              "Needs a manual look" in queue_html)
    finally:
        (cfgmod.db_path, cfgmod.config_path, secmod.has_llm_token,
         secmod.get_account_password) = orig

    print("\nOutlook account Test uses the real provider, not a hardcoded IMAPSource")
    tmp = Path(tf.mkdtemp())
    cfgmod.db_path = lambda: tmp / "t.db"
    cfgmod.config_path = lambda: tmp / "config.toml"
    secmod.has_llm_token = lambda: True
    try:
        from fastapi.testclient import TestClient

        from mailcheck.web.app import create_app

        conn = db.connect(tmp / "t.db")
        db.add_account(conn, label="ol", email="a@outlook.com", provider="outlook")
        conn.close()
        cfg = cfgmod.load()
        cfg.outlook.client_id = "test-client-id"
        cfgmod.save(cfg)

        client = TestClient(create_app())
        r = client.post("/api/accounts/ol/test")
        # No cache exists for this label, so the real Graph path must fail with
        # a sign-in-reconnect message — never an IMAP host/password error, which
        # is what the old hardcoded-IMAPSource bug produced instead.
        check("Outlook test goes through the Graph path, not IMAP",
              r.status_code == 400 and "econnect" in r.json()["error"], r.text)
        check("  never a bogus IMAP error", "imap" not in r.json()["error"].lower())
    finally:
        cfgmod.db_path, cfgmod.config_path, secmod.has_llm_token = (
            orig[0], orig[1], orig[2]
        )

    print("\nOutlook cancel actually stops the pending sign-in")
    tmp = Path(tf.mkdtemp())
    cfgmod.db_path = lambda: tmp / "t.db"
    cfgmod.config_path = lambda: tmp / "config.toml"
    secmod.has_llm_token = lambda: True
    try:
        from fastapi.testclient import TestClient

        import mailcheck.web.app as W

        conn = db.connect(tmp / "t.db")
        conn.close()
        cfg = cfgmod.load()
        cfg.outlook.client_id = "test-client-id"
        cfgmod.save(cfg)

        # Simulate MSAL's real, documented cancellation contract: the polling
        # call blocks until either it "succeeds" or someone sets
        # flow["expires_at"] to 0 — exactly what abort() does. If cancel didn't
        # reach the SAME dict the thread is blocked on, this would hang for the
        # full fake delay instead of returning almost immediately.
        completed_calls = {"n": 0}

        def fake_begin(label, client_id):
            return oa.PendingFlow(
                label=label, client_id=client_id,
                flow={"user_code": "ABC-123", "verification_uri": "https://x",
                      "expires_at": clock.time() + 900},
            )

        def fake_complete(pending):
            completed_calls["n"] += 1
            deadline = clock.time() + 5  # would take 5s if never aborted
            while clock.time() < deadline:
                if pending.flow.get("expires_at", 0) <= clock.time():
                    raise oa.OutlookAuthError("Sign-in was not completed: aborted")
                clock.sleep(0.02)
            return "someone@outlook.com"  # only reached if abort() never worked

        real_begin, real_complete = oa.begin_device_flow, oa.complete_device_flow
        oa.begin_device_flow = fake_begin
        oa.complete_device_flow = fake_complete
        try:
            client = TestClient(W.create_app())
            r = client.post("/api/outlook/start", json={"label": "ol2"})
            check("start succeeds", r.json()["ok"])

            t0 = clock.time()
            r = client.post("/api/outlook/cancel")
            check("cancel responds immediately, not after the fake delay",
                  clock.time() - t0 < 1.0)
            check("cancel itself reports success", r.json()["ok"])

            # Give the background thread a moment to actually unblock and run
            # its (now-stale) completion path.
            for _ in range(100):
                if completed_calls["n"] and W._outlook.get("pending") is None:
                    break
                clock.sleep(0.02)

            with db.session(tmp / "t.db") as c2:
                check("cancelled sign-in never creates the account",
                      db.get_account(c2, "ol2") is None)
            check("no token cache is left behind after cancelling",
                  not oa.has_cache("ol2"))
            check("UI state returns to idle", W._outlook["state"] == "idle")
        finally:
            oa.begin_device_flow, oa.complete_device_flow = real_begin, real_complete
            oa.delete_cache("ol2")
    finally:
        cfgmod.db_path, cfgmod.config_path, secmod.has_llm_token = (
            orig[0], orig[1], orig[2]
        )

    print("\nconfig: atomic save, max_retries floor, use_json_mode wiring")
    tmp = Path(tf.mkdtemp())
    cfgmod.db_path = lambda: tmp / "t.db"
    cfgmod.config_path = lambda: tmp / "config.toml"
    try:
        try:
            Config.model_validate({"llm": {"max_retries": 0}})
            check("max_retries=0 is rejected by config validation", False)
        except Exception:  # noqa: BLE001 - pydantic ValidationError
            check("max_retries=0 is rejected by config validation", True)

        from mailcheck.llm.client import LLMClient

        clamped = LLMClient(base_url="http://x/v1", token="t", model="m", max_retries=0)
        check("LLMClient defensively clamps max_retries=0 to 1", clamped.max_retries == 1)
        clamped.close()

        cfg = cfgmod.Config()
        path = cfgmod.save(cfg)
        check("save() succeeds and round-trips", cfgmod.load().llm.batch_size == cfg.llm.batch_size)
        leftover = list(path.parent.glob(".config-*.tmp"))
        check("no temp file left behind after an atomic save", leftover == [], str(leftover))

        # use_json_mode=False must reach the actual HTTP request, not just sit
        # unread in config.py — verified against a real local server.
        import http.server
        import json as jsonlib
        import socketserver
        import threading as th

        bodies = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                length = int(self.headers["Content-Length"])
                bodies.append(jsonlib.loads(self.rfile.read(length)))
                reply = jsonlib.dumps(
                    {"choices": [{"message": {"content": "{}"}}]}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

        srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        th.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{srv.server_address[1]}/v1"
            with LLMClient(base_url=base, token="t", model="m", use_json_mode=False) as c:
                c.complete("sys", "user")
            with LLMClient(base_url=base, token="t", model="m", use_json_mode=True) as c:
                c.complete("sys", "user")
        finally:
            srv.shutdown()

        check("use_json_mode=False omits response_format",
              "response_format" not in bodies[0], str(bodies[0]))
        check("use_json_mode=True (default) sends response_format",
              "response_format" in bodies[1], str(bodies[1]))

        # classify.py's batch call must defer to the client's setting rather
        # than hardcoding True and silently overriding it.
        seen_json_mode = {}

        class RecordingClient:
            def complete(self, system, user, json_mode=None, max_tokens=2048):
                seen_json_mode["value"] = json_mode
                return json.dumps({"results": [{"id": "0", "category": "rejection"}]})

        run_classify(RecordingClient(), [msgs[0]], batch_size=8)
        check("the batch call defers to the client's use_json_mode (passes None)",
              seen_json_mode["value"] is None, str(seen_json_mode))
    finally:
        cfgmod.db_path, cfgmod.config_path = orig[0], orig[1]

    print("\nprivacy_ack is an explicit choice, never a side effect of saving")
    tmp = Path(tf.mkdtemp())
    cfgmod.db_path = lambda: tmp / "t.db"
    cfgmod.config_path = lambda: tmp / "config.toml"
    secmod.has_llm_token = lambda: True
    try:
        from fastapi.testclient import TestClient

        from mailcheck.web.app import create_app

        conn = db.connect(tmp / "t.db")
        conn.close()
        cfg = cfgmod.load()
        check("starts unacknowledged", cfg.privacy_ack is False)

        client = TestClient(create_app())
        client.post("/api/settings", json={"model": "some-model"})
        check("an unrelated settings save does not silently acknowledge it",
              cfgmod.load().privacy_ack is False)

        client.post("/api/settings", json={"privacy_ack": True})
        check("the explicit toggle does acknowledge it", cfgmod.load().privacy_ack is True)
    finally:
        cfgmod.db_path, cfgmod.config_path, secmod.has_llm_token = (
            orig[0], orig[1], orig[2]
        )

    print("\naccount labels never reach an inline JS string (XSS)")
    tmp = Path(tf.mkdtemp())
    cfgmod.db_path = lambda: tmp / "t.db"
    cfgmod.config_path = lambda: tmp / "config.toml"
    secmod.has_llm_token = lambda: True
    try:
        from fastapi.testclient import TestClient

        from mailcheck.web.app import create_app

        hostile = """x');alert(document.cookie);//"><script>1</script>"""
        conn = db.connect(tmp / "t.db")
        db.add_account(conn, label=hostile, email="a@gmail.com", imap_host="imap.gmail.com")
        conn.close()

        client = TestClient(create_app())
        r = client.get("/accounts")
        check("a hostile label does not break page rendering", r.status_code == 200)
        html = r.text
        check("no inline handler embeds a label as a JS string literal (old bug shape)",
              "onclick=\"testAccount('" not in html
              and "onclick=\"removeAccount('" not in html
              and "onchange=\"toggleAccount('" not in html)
        check("row actions are wired through data-action, not inline onclick",
              'data-action="test"' in html and 'data-action="remove"' in html)
        check("<script>1</script> is not present unescaped (would prove injection)",
              "<script>1</script>" not in html)
    finally:
        (cfgmod.db_path, cfgmod.config_path, secmod.has_llm_token,
         secmod.get_account_password) = orig


def test_graph_source():
    """Drive OutlookGraphSource against a stand-in Graph: paging, 429 retry,
    read-only behaviour, and the mapping onto RawMessage."""
    import http.server
    import socketserver
    import threading
    from datetime import date

    import mailcheck.sources.outlook as outlook_mod

    print("\nmicrosoft graph source")

    seen = {"methods": [], "paths": [], "headers": [], "throttled": False}

    def page(items, next_link=None):
        body = {"value": items}
        if next_link:
            body["@odata.nextLink"] = next_link
        return body

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            seen["methods"].append("GET")
            seen["paths"].append(self.path)
            seen["headers"].append(dict(self.headers))

            # Throttle once, to prove Retry-After is honoured.
            if "/messages" in self.path and not seen["throttled"]:
                seen["throttled"] = True
                self.send_response(429)
                self.send_header("Retry-After", "0")
                self.end_headers()
                self.wfile.write(b"{}")
                return

            if self.path.startswith("/v1.0/me?"):
                payload = {"mail": "me@outlook.com"}
            elif "page2" in self.path:
                payload = page([{
                    "id": "B", "internetMessageId": "", "subject": "Second",
                    "from": {"emailAddress": {"address": "B@X.com", "name": "B"}},
                    "receivedDateTime": "2026-07-29T08:30:00Z",
                    "webLink": "https://outlook.live.com/b",
                    "body": {"contentType": "text", "content": "second body"},
                }])
            else:
                payload = page([{
                    "id": "A", "internetMessageId": "<a@x.com>", "subject": "First",
                    "from": {"emailAddress": {"address": "A@X.com", "name": "A"}},
                    "receivedDateTime": "2026-07-30T10:00:00Z",
                    "webLink": "https://outlook.live.com/a",
                    "body": {"contentType": "text", "content": "first body"},
                }], next_link=f"{outlook_mod.GRAPH}/page2")

            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            seen["methods"].append("POST")
            self.send_response(200)
            self.end_headers()

    srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    base = f"http://127.0.0.1:{srv.server_address[1]}/v1.0"
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    orig_graph, orig_msgs = outlook_mod.GRAPH, outlook_mod.MESSAGES_URL
    orig_token = outlook_mod.acquire_token
    outlook_mod.GRAPH = base
    outlook_mod.MESSAGES_URL = f"{base}/me/mailFolders/inbox/messages"
    outlook_mod.acquire_token = lambda label, client_id: "fake-token"
    try:
        source = outlook_mod.OutlookGraphSource(label="ol", client_id="cid")
        source.test()
        check("test() reaches /me", any("/me?" in p for p in seen["paths"]))

        got = list(source.fetch_unread(date(2026, 7, 1)))
        check("429 retried, honouring Retry-After", seen["throttled"])
        check("paged through @odata.nextLink", len(got) == 2, f"got {len(got)}")
        check("  bearer token sent",
              any(h.get("Authorization") == "Bearer fake-token" for h in seen["headers"]))
        check("  asks Graph for text bodies",
              any('outlook.body-content-type="text"' in (h.get("Prefer") or "")
                  for h in seen["headers"]))
        check("  filters to unread within the window",
              any("isRead+eq+false" in p or "isRead%20eq%20false" in p
                  for p in seen["paths"]), str(seen["paths"][:2]))
        # Graph rejects $filter+$orderby combinations on /messages with
        # InefficientFilter unless the $orderby property is listed first in
        # $filter — receivedDateTime must precede isRead, not follow it.
        messages_path = next(p for p in seen["paths"] if "/messages" in p)
        check("  $filter leads with the $orderby property (receivedDateTime)",
              messages_path.find("receivedDateTime") < messages_path.find("isRead"),
              messages_path)
        check("EVERY call is a GET - nothing is mutated",
              set(seen["methods"]) == {"GET"}, str(set(seen["methods"])))

        first = got[0]
        check("internetMessageId is the cache identity", first.message_id == "<a@x.com>")
        check("  Graph id kept as uid", first.uid == "A")
        check("  sender lower-cased", first.from_addr == "a@x.com")
        check("  webLink stored for deep linking",
              first.provider_url == "https://outlook.live.com/a")
        check("  date parsed to naive UTC",
              first.date_utc is not None and first.date_utc.tzinfo is None)
        check("missing internetMessageId falls back to the Graph id",
              got[1].message_id == "<graph-B>", got[1].message_id)

        # provider_url must survive normalize -> DB
        norm = normalize.normalize(first, account_id=1, account_label="ol")
        check("provider_url survives normalize",
              norm.provider_url == "https://outlook.live.com/a")
    finally:
        outlook_mod.GRAPH, outlook_mod.MESSAGES_URL = orig_graph, orig_msgs
        outlook_mod.acquire_token = orig_token
        srv.shutdown()


def test_reclassify(msgs):
    """Retrying the mail that came back unclassified.

    A failed classification is stored under the same (message, model, prompt
    version) key the cache is read by, so an ordinary check hands the failure
    straight back forever. These checks cover the way out of that.
    """
    import tempfile as tf

    from mailcheck import config as cfgmod
    from mailcheck import secrets as secmod
    from mailcheck import pipeline as pipe

    print("\nre-classify unclassified mail")
    tmp = Path(tf.mkdtemp())
    orig = (cfgmod.db_path, cfgmod.config_path, secmod.has_llm_token,
            secmod.get_llm_token)
    cfgmod.db_path = lambda: tmp / "t.db"
    cfgmod.config_path = lambda: tmp / "config.toml"
    secmod.has_llm_token = lambda: True
    secmod.get_llm_token = lambda: "fake-token"
    try:
        cfgmod.save(cfgmod.Config.model_validate(
            {"llm": {"base_url": "http://x/v1", "model": "m"}}
        ))
        conn = db.connect(tmp / "t.db")
        db.add_account(conn, label="gmail", email="me@x.com", imap_host="imap.gmail.com")

        # Two failures and one good result, so the retry must touch only the two.
        pks = []
        for i, m in enumerate(msgs[:3]):
            pk = db.upsert_message(conn, m)
            pks.append(pk)
            cat = "rejection" if i == 2 else UNCLASSIFIED
            db.save_classification(conn, pk, Classification(
                category=cat, confidence=0.0, action_required=cat == UNCLASSIFIED,
                summary="Could not classify automatically (test)."), "m", "1")
        conn.commit()

        waiting = db.messages_in_categories(conn, [UNCLASSIFIED])
        check("only the failed ones are picked up", len(waiting) == 2, str(len(waiting)))
        check("  their bodies come back with them",
              all(r["body_text"] for r in waiting))
        check("  a good classification is left alone",
              pks[2] not in {r["pk"] for r in waiting})

        # An ordinary check would hand the cached failure straight back.
        check("the failure really is cached (this is the bug being fixed)",
              db.get_cached(conn, pks[0], "m", "1").category == UNCLASSIFIED)

        cfg = cfgmod.load()
        cfg.llm.model = "m"
        real_client = pipe.LLMClient
        pipe.LLMClient = lambda **kw: _FakeLLMContext(FakeLLM("clean"))
        try:
            result = pipe.reclassify(conn, cfg)
        finally:
            pipe.LLMClient = real_client

        check("both failures were retried", result.fetched == 2, str(result.fetched))
        check("  and both resolved", result.classified == 2, str(result.classified))
        after = db.query_triaged(conn)
        cats = {r["pk"]: r["category"] for r in after}
        check("  the stored category is replaced, not appended alongside",
              UNCLASSIFIED not in cats.values(), str(cats))
        check("  the untouched message keeps its category", cats[pks[2]] == "rejection")
        check("nothing is left to retry",
              db.messages_in_categories(conn, [UNCLASSIFIED]) == [])

        # Retrying uses only what is already stored — no mailbox is contacted.
        pipe.build_source = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("reclassify must never touch the mail provider")
        )
        conn.close()

        from fastapi.testclient import TestClient

        import mailcheck.web.app as W

        client = TestClient(W.create_app())
        r = client.post("/api/reclassify", json={})
        check("API refuses when there is nothing to retry", r.status_code == 404, r.text)

        with db.session(tmp / "t.db") as c2:
            db.save_classification(c2, pks[0], Classification(
                category=UNCLASSIFIED, action_required=True), "m", "1")

        check("queue button is offered once something is unclassified",
              "Classify again" in client.get("/").text)
        check("  per-card retry is offered too",
              "reclassifyOne(" in client.get("/").text)

        W._job_lock.acquire()
        try:
            busy = client.post("/api/reclassify", json={})
            check("a retry cannot start while a check is running",
                  busy.status_code == 409, busy.text)
        finally:
            W._job_lock.release()
    finally:
        (cfgmod.db_path, cfgmod.config_path, secmod.has_llm_token,
         secmod.get_llm_token) = orig


class _FakeLLMContext:
    """Stands in for LLMClient's context-manager shape."""

    def __init__(self, inner):
        self.inner = inner

    def __enter__(self):
        return self.inner

    def __exit__(self, *exc):
        return False


def test_lazy_bodies_and_inplace_done(msgs):
    """The dashboard must not ship every email body, and Done must not reload."""
    import tempfile as tf

    from mailcheck import config as cfgmod
    from mailcheck import secrets as secmod

    print("\ndashboard payload + in-place Done")
    tmp = Path(tf.mkdtemp())
    orig = (cfgmod.db_path, cfgmod.config_path, secmod.has_llm_token)
    cfgmod.db_path = lambda: tmp / "t.db"
    cfgmod.config_path = lambda: tmp / "config.toml"
    secmod.has_llm_token = lambda: True
    try:
        conn = db.connect(tmp / "t.db")
        db.add_account(conn, label="gmail", email="me@x.com", imap_host="imap.gmail.com")
        pk = db.upsert_message(conn, msgs[0])
        db.save_classification(conn, pk, Classification(
            category="interview_invite", confidence=0.9, company="Acme",
            summary="Phone screen offered."), "m", "1")
        conn.commit()
        conn.close()

        from fastapi.testclient import TestClient

        from mailcheck.web.app import create_app

        client = TestClient(create_app())
        html = client.get("/").text
        # "Hi David" is body-only text; the summary and snippet do not carry it.
        check("full bodies are not inlined into the page",
              "Hi David" not in html, html[:0])
        check("  the body placeholder is wired to the message",
              f'data-body="{pk}"' in html)

        body = client.get(f"/api/messages/{pk}/body")
        check("the body endpoint serves it on demand",
              body.status_code == 200 and "Hi David" in body.json()["body"])
        check("  unknown message -> 404",
              client.get("/api/messages/9999/body").status_code == 404)

        r = client.post(f"/api/messages/{pk}/handled?done=true")
        check("Done returns fresh counts for an in-place update",
              set(r.json()["summary"]) == {"actionable", "informational", "done"})
        check("  and they reflect the change",
              r.json()["summary"]["done"] == 1 and r.json()["summary"]["actionable"] == 0,
              str(r.json()["summary"]))
    finally:
        cfgmod.db_path, cfgmod.config_path, secmod.has_llm_token = orig


def test_config_cache():
    """The status poll reads the config about once a second; it must not re-read
    the file each time, and must never hand back a shared mutable instance."""
    import tempfile as tf

    from mailcheck import config as cfgmod

    print("\nconfig parse cache")
    tmp = Path(tf.mkdtemp())
    orig = cfgmod.config_path
    cfgmod.config_path = lambda: tmp / "config.toml"
    try:
        cfgmod.save(cfgmod.Config.model_validate({"check": {"lookback_days": 5}}))
        reads = {"n": 0}
        real_open = Path.open

        def counting_open(self, *a, **kw):
            if self.name == "config.toml":
                reads["n"] += 1
            return real_open(self, *a, **kw)

        Path.open = counting_open
        try:
            for _ in range(20):
                cfgmod.load()
        finally:
            Path.open = real_open
        check("20 loads read the file at most once", reads["n"] <= 1, f"{reads['n']} reads")

        a = cfgmod.load()
        a.watch.auto_check = True
        a.prefilter_rules.append(PrefilterRule(category="other", sender="x@y.z"))
        b = cfgmod.load()
        check("a caller mutating the result cannot poison the cache",
              b.watch.auto_check is False and b.prefilter_rules == [],
              f"auto={b.watch.auto_check} rules={b.prefilter_rules}")

        # A save must be visible immediately, even if the filesystem timestamp
        # has not ticked since the read above.
        cfgmod.save(cfgmod.Config.model_validate({"check": {"lookback_days": 9}}))
        check("a save invalidates the cache at once", cfgmod.load().check.lookback_days == 9)

        check("the default fetch window is short", cfgmod.Config().check.lookback_days == 2,
              str(cfgmod.Config().check.lookback_days))
    finally:
        cfgmod.config_path = orig


def main() -> int:
    print("mail-check smoke test")
    test_password_repair()
    test_migration()
    test_graph_source()
    msgs = test_normalize()
    test_prefilter(msgs)
    test_parsing()
    test_sse_and_reasoning()
    test_classify_modes(msgs)
    test_pipeline_and_cache(msgs)
    test_web_console(msgs)
    test_schedule()
    test_action_queue(msgs)
    test_review_fixes(msgs)
    test_config_cache()
    test_lazy_bodies_and_inplace_done(msgs)
    test_reclassify(msgs)
    print(f"\n{PASS} passed, {FAIL} failed")
    print(
        "\n(Real-OS-keyring integration test is separate and opt-in — it needs an "
        "actual credential store, which not every environment has:\n"
        "  python tests/test_integration_keyring.py)"
    )
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
