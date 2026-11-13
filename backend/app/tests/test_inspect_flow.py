def test_orders_inspect_quarantine_and_release(env):
    detail = env.run_inspect(1)
    assert detail["scanned_count"] == 6
    # ORD-3 missing sku, ORD-4 bad price → quarantine; ORD-5 warn-only still pass
    assert detail["quarantined_count"] == 2
    assert detail["passed_count"] == 4
    assert detail["released_count"] == 4
    assert all(c["ok"] for c in detail["report"]["checks"] if c["name"] == "scan_breakdown")
    assert any(c["name"] == "release_matches_drift_breakdown" and c["ok"] for c in detail["report"]["checks"])
