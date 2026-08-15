"""Opt-in integration test: exercises the REAL OS credential store.

Not part of the main smoke suite (tests/test_smoke.py), which must stay
CI-portable and runnable in any execution context. This one depends on a real
keyring backend being reachable, which is not guaranteed everywhere — a
service account or a non-interactive Windows session can fail with
WinError 1312 ("A specified logon session does not exist") the moment it
touches Credential Manager. When that happens here, the run reports SKIPPED
rather than failing, since that reflects the environment, not the code.

Run directly:

    python tests/test_integration_keyring.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PASS, FAIL = 0, 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {detail}")


def main() -> int:
    import keyring

    from mailcheck import outlook_auth as oa
    from mailcheck.secrets import SERVICE

    print("mail-check keyring integration test (real OS credential store)")

    label = "mailcheck-selftest-delete-me"
    try:
        keyring.get_password(SERVICE, label)  # cheap probe before committing to the suite
    except Exception as exc:  # noqa: BLE001 - any backend failure means "skip", not "fail"
        print(f"\nSKIPPED — no usable OS keyring in this session: {exc}")
        return 0

    print("\noutlook token cache storage")
    oa.delete_cache(label)
    try:
        blob = json.dumps({
            "AccessToken": {"k": {"secret": "A" * 1800, "target": "Mail.Read"}},
            "RefreshToken": {"k": {"secret": "R" * 1200}},
            "IdToken": {"k": {"secret": "I" * 900}},
            "Account": {"k": {"username": "me@outlook.com"}},
        })
        try:
            keyring.set_password(SERVICE, "mailcheck-selftest-single", blob)
            keyring.delete_password(SERVICE, "mailcheck-selftest-single")
            check("a real cache is too big for one entry", False, "unexpectedly fit")
        except Exception:  # noqa: BLE001 - the platform limit is the assertion
            check(f"a real cache ({len(blob)} chars) is too big for one entry", True)

        oa._write_blob(label, blob)
        check("chunked write succeeds", True)
        check("  round-trips byte-identical", oa._read_blob(label) == blob)
        header = keyring.get_password(SERVICE, oa._cache_key(label))
        count = int(header.split(":")[1])
        check("  header records the chunk count", header.startswith("chunks:"))
        check("  every chunk is under the platform limit",
              all(len(keyring.get_password(SERVICE, oa._chunk_key(label, i))) <= 1280
                  for i in range(count)))

        small = json.dumps({"AccessToken": {"k": {"secret": "S" * 200}}})
        oa._write_blob(label, small)
        check("a shorter cache overwrites cleanly", oa._read_blob(label) == small)
        check("  stale chunks are removed",
              keyring.get_password(SERVICE, oa._chunk_key(label, count - 1)) is None)

        oa.delete_cache(label)
        keyring.set_password(SERVICE, oa._cache_key(label), '{"legacy":true}')
        check("pre-chunking caches stay readable", oa._read_blob(label) == '{"legacy":true}')

        oa.delete_cache(label)
        keyring.set_password(SERVICE, oa._cache_key(label), "chunks:3")
        keyring.set_password(SERVICE, oa._chunk_key(label, 0), "abc")
        check("a torn write reads as empty, not garbage", oa._read_blob(label) == "")

        oa.delete_cache(label)
        import msal

        cache = msal.SerializableTokenCache()
        cache.deserialize(blob)
        cache.has_state_changed = True   # deserialize() clears it; a real grant sets it
        oa.save_cache(label, cache)
        check("MSAL cache survives save -> load",
              json.loads(oa.load_cache(label).serialize()) == json.loads(blob))

        keyring.set_password(SERVICE, oa._cache_key(label), "not json at all")
        check("a corrupt cache degrades to empty rather than raising",
              oa.load_cache(label).serialize() in ("{}", ""))
    finally:
        oa.delete_cache(label)
    check("delete clears header and chunks",
          keyring.get_password(SERVICE, oa._cache_key(label)) is None
          and keyring.get_password(SERVICE, oa._chunk_key(label, 0)) is None)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
