import json

from app.db import connect
from app.modules import prior_curated_drift_guard as guard
from app.repositories import curated as curated_repo
from app.repositories import drift_guard as drift_repo

DATASET_ID = 3  # seed.py 中的 Curated Drift Demo


def _set_fixture(svc, name: str) -> None:
    conn = connect()
    conn.execute("UPDATE datasets SET fixture_path=? WHERE id=?", (name, DATASET_ID))
    conn.commit()
    conn.close()


def _curated_map() -> dict:
    conn = connect()
    rows = curated_repo.list_all(conn, DATASET_ID, limit=500)
    conn.close()
    return {r["entity_key"]: r for r in rows}


def _set_strategy(svc, strategy: str) -> None:
    conn = connect()
    guard.save_config(
        conn, dataset_id=None, enabled=True, strategy=strategy, compare_fields=[]
    )
    conn.close()


def _breakdown(detail: dict) -> dict:
    return detail["report"]["drift_guard"]["breakdown"]


def test_block_strategy_quarantines_changes_and_preserves_curated(env):
    """先签发建精选，再同键改字段：阻断行进隔离，精选不被覆盖。"""
    # 批次 1：基线夹具，4 个首次键全部签发建精选
    base = env.run_inspect(DATASET_ID)
    assert base["quarantined_count"] == 0
    assert base["released_count"] == 4
    assert _breakdown(base)["首次键"] == 4
    curated_after_base = _curated_map()
    assert set(curated_after_base) == {"G-1", "G-2", "G-3", "G-4"}
    assert curated_after_base["G-2"]["payload"]["price"] == 20.0

    # 切换同键改字段夹具（G-2 改 price、G-3 改 sku、G-5 新键）
    _set_fixture(env, "curated_drift_snapshot.jsonl")
    detail = env.run_inspect(DATASET_ID)

    bd = _breakdown(detail)
    assert bd["首次键"] == 1          # G-5
    assert bd["同键未变更"] == 1       # G-1
    assert bd["同键变更·阻断隔离"] == 2  # G-2, G-3
    assert bd["同键变更·覆盖签发"] == 0

    # 阻断计入隔离，G-5 与 G-1 正常签发
    assert detail["quarantined_count"] == 2
    assert detail["passed_count"] == 2
    assert detail["released_count"] == 2

    # 行清单带门禁状态与变更字段
    rows = {r["entity_key"]: r for r in detail["rows"]}
    assert rows["G-2"]["disposition"] == "quarantine"
    assert rows["G-2"]["drift_status"] == guard.STATUS_SAME_CHANGED_BLOCKED
    assert rows["G-2"]["drift_detail"]["changed_fields"] == ["price"]
    assert rows["G-3"]["drift_detail"]["changed_fields"] == ["sku"]
    assert rows["G-1"]["drift_status"] == guard.STATUS_SAME_UNCHANGED
    assert rows["G-5"]["drift_status"] == guard.STATUS_FIRST_SEEN

    # 隔离区有阻断行且原因可追溯
    conn = connect()
    q = conn.execute(
        "SELECT entity_key, reason FROM quarantine WHERE batch_id=? ORDER BY entity_key",
        (detail["id"],),
    ).fetchall()
    conn.close()
    assert {r["entity_key"] for r in q} == {"G-2", "G-3"}
    assert all("同键变更阻断" in r["reason"] for r in q)

    # 精选仍是旧值，来源批次不是本批（未被无审计盖掉）
    curated = _curated_map()
    assert curated["G-2"]["payload"]["price"] == 20.0
    assert curated["G-2"]["batch_id"] != detail["id"]
    assert curated["G-3"]["payload"]["sku"] == "SKU-G3"
    assert curated["G-5"]["batch_id"] == detail["id"]  # 新键照常进入

    # 阻断策略下无审计、无覆盖
    conn = connect()
    assert drift_repo.list_audit(conn, batch_id=detail["id"]) == []
    snap = drift_repo.list_snapshot(conn, detail["id"])
    conn.close()
    assert {s["entity_key"] for s in snap} == {"G-1", "G-2", "G-3", "G-4"}
    assert all(s["strategy"] == "block" for s in snap)

    assert all(c["ok"] for c in detail["report"]["checks"])


def test_overwrite_strategy_releases_with_before_after_audit(env):
    """覆盖策略：变更行通过并覆盖精选，且留下变更前/后审计。"""
    env.run_inspect(DATASET_ID)  # 建精选
    _set_fixture(env, "curated_drift_snapshot.jsonl")
    _set_strategy(env, "overwrite")

    detail = env.run_inspect(DATASET_ID)
    bd = _breakdown(detail)
    assert bd["同键变更·覆盖签发"] == 2
    assert bd["同键变更·阻断隔离"] == 0
    assert detail["quarantined_count"] == 0
    assert detail["released_count"] == 4  # G-1 未变更 + G-5 首次 + G-2/G-3 覆盖

    rows = {r["entity_key"]: r for r in detail["rows"]}
    assert rows["G-2"]["disposition"] == "pass"
    assert rows["G-2"]["drift_status"] == guard.STATUS_SAME_CHANGED_OVERWRITTEN

    # 精选已被新值覆盖
    curated = _curated_map()
    assert curated["G-2"]["payload"]["price"] == 99.0
    assert curated["G-3"]["payload"]["sku"] == "SKU-G3-NEW"

    # 审计：before=快照旧值，after=本批新值，逐字段可查
    conn = connect()
    audits = drift_repo.list_audit(conn, batch_id=detail["id"])
    conn.close()
    assert len(audits) == 2
    by_key = {a["entity_key"]: a for a in audits}
    assert by_key["G-2"]["changed_fields"] == ["price"]
    assert by_key["G-2"]["before"]["price"] == 20.0
    assert by_key["G-2"]["after"]["price"] == 99.0
    assert by_key["G-3"]["before"]["sku"] == "SKU-G3"
    assert by_key["G-3"]["after"]["sku"] == "SKU-G3-NEW"
    assert all(a["strategy"] == "overwrite" for a in audits)

    assert all(c["ok"] for c in detail["report"]["checks"])


def test_compare_fields_only_guards_configured_fields(env):
    """比对字段集可配：不在集合内的字段变化不判定为变更。"""
    env.run_inspect(DATASET_ID)  # 建精选
    _set_fixture(env, "curated_drift_snapshot.jsonl")
    conn = connect()
    # 只盯 price：G-3 只改了 sku，应视为同键未变更
    guard.save_config(
        conn, dataset_id=None, enabled=True, strategy="block", compare_fields=["price"]
    )
    conn.close()

    detail = env.run_inspect(DATASET_ID)
    bd = _breakdown(detail)
    assert bd["同键变更·阻断隔离"] == 1  # 仅 G-2 改 price
    assert bd["同键未变更"] == 2         # G-1 原样 + G-3 的 sku 变化不在比对集
    assert all(c["ok"] for c in detail["report"]["checks"])


def test_empty_key_never_curated_even_when_passing(env):
    """空键规则通过也不得进入精选，且不影响同键变更门禁。"""
    env.run_inspect(DATASET_ID)  # 建精选 G-1..G-4
    _set_fixture(env, "curated_drift_empty_key.jsonl")

    detail = env.run_inspect(DATASET_ID)
    bd = _breakdown(detail)
    assert bd["空键未签发"] == 1
    assert bd["同键变更·阻断隔离"] == 1  # G-4 改 price
    assert detail["passed_count"] == 2
    assert detail["released_count"] == 1  # 仅 G-1 未变更重签；空键不签发

    curated = _curated_map()
    assert set(curated) == {"G-1", "G-2", "G-3", "G-4"}  # 无合成键混入
    empty_row = next(r for r in detail["rows"] if not r["entity_key"])
    assert empty_row["disposition"] == "pass"
    assert empty_row["drift_status"] == guard.STATUS_EMPTY_KEY
    assert all(c["ok"] for c in detail["report"]["checks"])


def test_config_persists_across_refresh(env):
    """策略与比对字段写入 settings，重新加载（模拟刷新/重启）后仍生效，且互斥。"""
    conn = connect()
    guard.save_config(
        conn, dataset_id=DATASET_ID, enabled=True, strategy="overwrite",
        compare_fields=["price", "sku"],
    )
    conn.close()

    conn = connect()
    cfg = guard.load_config(conn, DATASET_ID)
    conn.close()
    assert cfg.enabled is True
    assert cfg.strategy == "overwrite"
    assert cfg.compare_fields == ["price", "sku"]
    assert cfg.scope == "dataset"

    # 非法策略被拒绝；落库脏值回退默认 block，永远不会两种策略同时生效
    conn = connect()
    try:
        guard.save_config(conn, dataset_id=None, enabled=None, strategy="both", compare_fields=None)
        raised = False
    except ValueError:
        raised = True
    conn.close()
    assert raised

    conn = connect()
    from app.repositories import settings as settings_repo
    settings_repo.put(conn, "drift_guard.strategy", "bogus")
    conn.commit()
    fallback = guard.load_config(conn, 0)
    conn.close()
    assert fallback.strategy == guard.DEFAULT_STRATEGY


def test_diff_fields_semantics(env):
    assert guard.diff_fields({"a": 1}, {"a": 1, "b": 2}, ["a"]) == []
    assert guard.diff_fields({"a": 1}, {"a": 2}, ["a"]) == ["a"]
    # 一侧缺字段也算变化
    assert guard.diff_fields({"a": 1}, {}, ["a"]) == ["a"]
    # 空字段集 → 全键并集对照
    assert guard.diff_fields({"a": 1}, {"a": 1, "b": 2}, []) == ["b"]


def test_disabled_guard_keeps_base_chain(env):
    """门禁关闭：不冻结快照、不分类，通过行全部签发（含空键合成键），维持基础链。"""
    conn = connect()
    guard.save_config(conn, dataset_id=None, enabled=False, strategy=None, compare_fields=None)
    conn.close()

    env.run_inspect(DATASET_ID)  # 建精选
    _set_fixture(env, "curated_drift_empty_key.jsonl")
    detail = env.run_inspect(DATASET_ID)

    dg = detail["report"]["drift_guard"]
    assert dg["enabled"] is False
    assert dg["snapshot_size"] == 0
    assert all(v == 0 for v in dg["breakdown"].values())
    # 空键行按基础链以 row-N 合成键签发
    assert detail["released_count"] == detail["passed_count"] == 3
    conn = connect()
    keys = {
        r["entity_key"]
        for r in conn.execute(
            "SELECT entity_key FROM curated WHERE dataset_id=? AND batch_id=?",
            (DATASET_ID, detail["id"]),
        ).fetchall()
    }
    conn.close()
    assert "row-2" in keys


def test_dataset_override_falls_back_per_key(env):
    """数据集只覆盖策略时，开关与比对字段逐项回退全局值。"""
    conn = connect()
    guard.save_config(
        conn, dataset_id=None, enabled=True, strategy="block", compare_fields=["price"]
    )
    guard.save_config(
        conn, dataset_id=DATASET_ID, enabled=None, strategy="overwrite", compare_fields=None
    )
    cfg = guard.load_config(conn, DATASET_ID)
    conn.close()
    assert cfg.scope == "dataset"
    assert cfg.strategy == "overwrite"          # 数据集覆盖
    assert cfg.enabled is True                  # 回退全局
    assert cfg.compare_fields == ["price"]      # 回退全局
