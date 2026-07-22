"""Assert-based tests for prune decision logic and web UI pure helpers.

Runnable with plain `python tests/test_prune.py` (no framework/fixtures/sockets),
in an environment where the app's deps are installed (e.g. inside the image).
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from traefiktounifi import webui  # noqa: E402
from traefiktounifi.app import (  # noqa: E402
    REASON_NOT_OURS,
    REASON_OPTED_OUT,
    REASON_VALUE_DRIFT,
    compute_prune_actions,
)

IP = "10.0.32.4"
NOW = "2026-07-21T00:00:00+00:00"


def entry(key, value=IP, rtype="A", _id=None):
    return {"key": key, "value": value, "record_type": rtype, "_id": _id or f"id-{key}"}


def managed_ledger(host, missing=0, eligible=True):
    return {
        "managed": {
            host: {
                "value": IP,
                "record_type": "A",
                "prune_eligible": eligible,
                "missing_count": missing,
            }
        },
        "unmanaged": {},
    }


def test_created_now_enters_managed():
    to_del, released, nxt = compute_prune_actions(
        {"managed": {}, "unmanaged": {}},
        desired_hosts={"a.example"},
        prune_prefs={"a.example": True},
        unifi_entries=[entry("a.example")],
        created_now={"a.example"},
        traefik_ip=IP,
        record_type="A",
        grace=3,
        now_iso=NOW,
    )
    assert to_del == [] and released == []
    assert nxt["managed"]["a.example"]["missing_count"] == 0
    assert nxt["managed"]["a.example"]["prune_eligible"] is True


def test_present_host_resets_missing_count():
    _, _, nxt = compute_prune_actions(
        managed_ledger("a.example", missing=2),
        {"a.example"},
        {"a.example": True},
        [entry("a.example")],
        set(),
        IP,
        "A",
        3,
        NOW,
    )
    assert nxt["managed"]["a.example"]["missing_count"] == 0


def test_grace_increments_then_deletes():
    # missing_count 0 -> gone: increments to 1, no delete (grace 3).
    to_del, _, nxt = compute_prune_actions(
        managed_ledger("a.example", missing=0),
        set(),
        {},
        [entry("a.example")],
        set(),
        IP,
        "A",
        3,
        NOW,
    )
    assert to_del == []
    assert nxt["managed"]["a.example"]["missing_count"] == 1

    # missing_count 2 -> gone: increments to 3 == grace -> delete.
    to_del, _, nxt = compute_prune_actions(
        managed_ledger("a.example", missing=2),
        set(),
        {},
        [entry("a.example", _id="ID9")],
        set(),
        IP,
        "A",
        3,
        NOW,
    )
    assert to_del == [("a.example", "ID9")]
    # stays in managed (retriable) until caller confirms deletion
    assert "a.example" in nxt["managed"]


def test_value_drift_releases_not_deletes():
    to_del, released, nxt = compute_prune_actions(
        managed_ledger("a.example", missing=2),
        set(),
        {},
        [entry("a.example", value="9.9.9.9")],  # manually changed
        set(),
        IP,
        "A",
        3,
        NOW,
    )
    assert to_del == []
    assert released == [("a.example", REASON_VALUE_DRIFT)]
    assert nxt["unmanaged"]["a.example"]["reason"] == REASON_VALUE_DRIFT
    assert "a.example" not in nxt["managed"]


def test_opted_out_orphan_releases_not_deletes():
    to_del, released, nxt = compute_prune_actions(
        managed_ledger("a.example", missing=2, eligible=False),
        set(),
        {},
        [entry("a.example")],
        set(),
        IP,
        "A",
        3,
        NOW,
    )
    assert to_del == []
    assert released == [("a.example", REASON_OPTED_OUT)]
    assert nxt["unmanaged"]["a.example"]["reason"] == REASON_OPTED_OUT


def test_already_gone_from_unifi_dropped_silently():
    to_del, released, nxt = compute_prune_actions(
        managed_ledger("a.example", missing=2),
        set(),
        {},
        [],  # not present in UniFi anymore
        set(),
        IP,
        "A",
        3,
        NOW,
    )
    assert to_del == [] and released == []
    assert "a.example" not in nxt["managed"]
    assert "a.example" not in nxt["unmanaged"]


def test_manual_entry_classified_unmanaged_never_deleted():
    to_del, _, nxt = compute_prune_actions(
        {"managed": {}, "unmanaged": {}},
        {"a.example"},
        {"a.example": True},
        [entry("a.example"), entry("manual.example")],
        {"a.example"},  # we created a.example this cycle; manual is pre-existing
        IP,
        "A",
        3,
        NOW,
    )
    assert to_del == []
    assert "manual.example" in nxt["unmanaged"]
    assert nxt["unmanaged"]["manual.example"]["reason"] == REASON_NOT_OURS
    assert "manual.example" not in nxt["managed"]


def test_shared_host_opt_out_wins():
    # Present + eligible False -> stays managed but ineligible; when gone later
    # it would be released, never deleted.
    _, _, nxt = compute_prune_actions(
        {"managed": {}, "unmanaged": {}},
        {"shared.example"},
        {"shared.example": False},
        [entry("shared.example")],
        {"shared.example"},
        IP,
        "A",
        3,
        NOW,
    )
    assert nxt["managed"]["shared.example"]["prune_eligible"] is False


def test_first_seen_preserved_for_existing_unmanaged():
    ledger = {
        "managed": {},
        "unmanaged": {
            "manual.example": {
                "value": IP,
                "record_type": "A",
                "reason": REASON_NOT_OURS,
                "first_seen": "2020-01-01T00:00:00+00:00",
            }
        },
    }
    _, _, nxt = compute_prune_actions(
        ledger, set(), {}, [entry("manual.example")], set(), IP, "A", 3, NOW
    )
    assert (
        nxt["unmanaged"]["manual.example"]["first_seen"] == "2020-01-01T00:00:00+00:00"
    )


# --- Web UI pure helpers ---


def test_tail_missing_and_last_n():
    assert webui.tail("/nonexistent/path", 5) == []
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
        f.write("\n".join(f"line{i}" for i in range(10)))
        path = f.name
    try:
        assert webui.tail(path, 3) == ["line7", "line8", "line9"]
    finally:
        os.unlink(path)


def test_load_state_and_render_page():
    state = {
        "managed": {
            "a.example": {
                "value": IP,
                "record_type": "A",
                "prune_eligible": True,
                "missing_count": 0,
            }
        },
        "unmanaged": {
            "m.example": {
                "value": IP,
                "record_type": "A",
                "reason": REASON_NOT_OURS,
                "first_seen": NOW,
            }
        },
    }
    history = [
        {
            "ts": NOW,
            "status": "ok",
            "added": 1,
            "updated": 0,
            "deleted": 0,
            "released": 0,
            "errors": 0,
            "dry_run": False,
        }
    ]
    html_out = webui.render_page(state, history, ["log line 1", "log line 2"])
    assert "a.example" in html_out
    assert "m.example" in html_out
    assert REASON_NOT_OURS in html_out
    assert "log line 1" in html_out
    # empty state renders without error
    empty = webui.render_page({"managed": {}, "unmanaged": {}}, [], [])
    assert "none" in empty


def test_load_state_corrupt_file_empty():
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        f.write("{ not json")
        path = f.name
    try:
        assert webui.load_state(path) == {"managed": {}, "unmanaged": {}}
    finally:
        os.unlink(path)


def test_render_escapes_html():
    state = {
        "managed": {
            "<script>x</script>": {
                "value": IP,
                "record_type": "A",
                "prune_eligible": True,
                "missing_count": 0,
            }
        },
        "unmanaged": {},
    }
    out = webui.render_page(state, [], [])
    assert "<script>x</script>" not in out
    assert "&lt;script&gt;" in out


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} tests passed")


if __name__ == "__main__":
    main()
