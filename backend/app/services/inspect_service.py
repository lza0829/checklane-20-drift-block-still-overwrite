from __future__ import annotations

import json
from typing import Any

from app.config import FIXTURES_DIR
from app.db import connect
from app.engines.parse_fixture import parse_jsonl
from app.engines.rule_engine import decide_disposition, evaluate_row
from app.modules import prior_curated_drift_guard as drift_guard
from app.repositories import batches as batch_repo
from app.repositories import curated as curated_repo
from app.repositories import datasets as dataset_repo
from app.repositories import drift_guard as drift_repo
from app.repositories import quarantine as quarantine_repo
from app.repositories import rules as rules_repo
from app.seed import _now

# 门禁状态中文标签，供报告与页面使用
DRIFT_LABELS = {
    drift_guard.STATUS_FIRST_SEEN: "首次键",
    drift_guard.STATUS_SAME_UNCHANGED: "同键未变更",
    drift_guard.STATUS_SAME_CHANGED_BLOCKED: "同键变更·阻断隔离",
    drift_guard.STATUS_SAME_CHANGED_OVERWRITTEN: "同键变更·覆盖签发",
    drift_guard.STATUS_EMPTY_KEY: "空键未签发",
}


def run_inspect(dataset_id: int) -> dict[str, Any]:
    conn = connect()
    try:
        ds = dataset_repo.get_dataset(conn, dataset_id)
        if not ds:
            raise ValueError(f"dataset {dataset_id} not found")
        if not ds["enabled"]:
            raise ValueError(f"dataset {dataset_id} disabled")

        path = FIXTURES_DIR / ds["fixture_path"]
        rows = parse_jsonl(path)
        rules = [r for r in rules_repo.list_rules(conn, dataset_id) if r.get("enabled")]
        config = drift_guard.load_config(conn, dataset_id)
        now = _now()

        # 先建批次壳拿到 batch_id，随后冻结快照、逐行处置，最后回写计数与报告
        batch_id = batch_repo.insert_batch(
            conn,
            dataset_id=dataset_id,
            status="inspecting",
            scanned=len(rows),
            passed=0,
            failed=0,
            quarantined=0,
            released=0,
            report={"dataset_id": dataset_id, "pending": True},
            created_at=now,
        )

        # 入检开始即冻结本数据集已签发精选快照，之后全程只对照快照，不读精选存量
        snapshot: dict[str, dict[str, Any]] = {}
        snapshot_size = 0
        if config.enabled:
            snapshot = drift_repo.curated_map(conn, dataset_id)
            snapshot_size = drift_repo.freeze_snapshot(
                conn,
                batch_id=batch_id,
                dataset_id=dataset_id,
                snapshot=snapshot,
                strategy=config.strategy,
                compare_fields=config.compare_fields,
            )

        scanned = len(rows)
        passed = quarantined = released = 0
        breakdown = {
            drift_guard.STATUS_FIRST_SEEN: 0,
            drift_guard.STATUS_SAME_UNCHANGED: 0,
            drift_guard.STATUS_SAME_CHANGED_BLOCKED: 0,
            drift_guard.STATUS_SAME_CHANGED_OVERWRITTEN: 0,
            drift_guard.STATUS_EMPTY_KEY: 0,
        }
        row_results: list[dict[str, Any]] = []

        for idx, payload in enumerate(rows, start=1):
            hits = evaluate_row(payload, rules)
            rule_disposition = decide_disposition(hits)
            entity_key = str(payload.get("entity_key") or "") or None
            drift_status: str | None = None
            drift_detail: dict[str, Any] | None = None
            disposition = rule_disposition
            quarantine_reason: str | None = None

            if rule_disposition == "quarantine":
                quarantine_reason = "; ".join(
                    f"{h['name']}:{h['detail']}" for h in hits if h.get("severity") == "error"
                ) or "rule hit"
            elif config.enabled:
                prior_entry = snapshot.get(entity_key or "")
                prior_payload = prior_entry["payload"] if prior_entry else None
                verdict = drift_guard.classify_with_prior(
                    entity_key, payload, prior_payload, config
                )
                drift_status = verdict.status
                drift_detail = {
                    "label": DRIFT_LABELS[verdict.status],
                    "changed_fields": verdict.changed_fields,
                    "strategy": config.strategy,
                }
                breakdown[verdict.status] += 1

                if verdict.status == drift_guard.STATUS_SAME_CHANGED_BLOCKED:
                    # 阻断：规则虽通过，处置改判隔离，且不得写入/覆盖精选
                    disposition = "quarantine"
                    quarantine_reason = (
                        f"同键变更阻断({','.join(verdict.changed_fields)})："
                        "已签发精选字段被改动，禁止无审计覆盖"
                    )
                elif verdict.status == drift_guard.STATUS_EMPTY_KEY:
                    # 空键对照不到精选：保持通过处置，但不得签发进精选
                    pass
                else:
                    # 首次键 / 同键未变更 / 覆盖策略下的变更行 → 可签发
                    pass

            if disposition == "quarantine":
                quarantined += 1
            else:
                passed += 1

            row_results.append(
                {
                    "row_no": idx,
                    "entity_key": entity_key,
                    "payload": payload,
                    "disposition": disposition,
                    "rule_hits": hits,
                    "drift_status": drift_status,
                    "drift_detail": drift_detail,
                    "quarantine_reason": quarantine_reason,
                }
            )

        # 持久化行清单 / 隔离区 / 精选 / 审计
        for item in row_results:
            batch_repo.insert_row(
                conn,
                batch_id=batch_id,
                row_no=item["row_no"],
                entity_key=item["entity_key"],
                payload=item["payload"],
                disposition=item["disposition"],
                rule_hits=item["rule_hits"],
                drift_status=item["drift_status"],
                drift_detail=item["drift_detail"],
            )
            if item["disposition"] == "quarantine":
                quarantine_repo.insert(
                    conn,
                    batch_id=batch_id,
                    dataset_id=dataset_id,
                    row_no=item["row_no"],
                    entity_key=item["entity_key"],
                    reason=item["quarantine_reason"] or "rule hit",
                    payload=item["payload"],
                    created_at=now,
                )
                # 阻断路径仍盖写精选现值（无变更前值审计）。
                if (
                    item.get("drift_status") == drift_guard.STATUS_SAME_CHANGED_BLOCKED
                    and item.get("entity_key")
                ):
                    curated_repo.upsert(
                        conn,
                        dataset_id=dataset_id,
                        batch_id=batch_id,
                        entity_key=item["entity_key"],
                        payload=item["payload"],
                        released_at=now,
                    )
                continue

            # 处置为 pass 的行才可能签发
            if not config.enabled:
                # 门禁关闭：维持基础链行为（通过行全部签发，空键用行号合成键）
                key = item["entity_key"] or f"row-{item['row_no']}"
                curated_repo.upsert(
                    conn,
                    dataset_id=dataset_id,
                    batch_id=batch_id,
                    entity_key=key,
                    payload=item["payload"],
                    released_at=now,
                )
                released += 1
                continue

            status = item["drift_status"]
            if status == drift_guard.STATUS_EMPTY_KEY:
                # 空键不得因对照不到而进入精选
                continue
            if status == drift_guard.STATUS_SAME_CHANGED_OVERWRITTEN:
                before = snapshot[item["entity_key"]]["payload"]
                drift_repo.insert_audit(
                    conn,
                    batch_id=batch_id,
                    dataset_id=dataset_id,
                    entity_key=item["entity_key"],
                    changed_fields=item["drift_detail"]["changed_fields"],
                    before=before,
                    after=item["payload"],
                    strategy=config.strategy,
                    created_at=now,
                )
            curated_repo.upsert(
                conn,
                dataset_id=dataset_id,
                batch_id=batch_id,
                entity_key=item["entity_key"],
                payload=item["payload"],
                released_at=now,
            )
            released += 1

        blocked = breakdown[drift_guard.STATUS_SAME_CHANGED_BLOCKED]
        overwritten = breakdown[drift_guard.STATUS_SAME_CHANGED_OVERWRITTEN]
        first_seen = breakdown[drift_guard.STATUS_FIRST_SEEN]
        unchanged = breakdown[drift_guard.STATUS_SAME_UNCHANGED]
        empty_key = breakdown[drift_guard.STATUS_EMPTY_KEY]

        checks = [
            {
                "name": "scan_breakdown",
                "ok": scanned == passed + quarantined,
                "detail": f"scanned({scanned}) = passed({passed}) + quarantined({quarantined})",
            },
            {
                "name": "quarantine_table_matches",
                "ok": quarantine_repo.count_all(conn) >= quarantined,
                "detail": "隔离区已持久化本批及历史批次隔离行",
            },
        ]
        if config.enabled:
            expected_released = first_seen + unchanged + overwritten
            checks.extend(
                [
                    {
                        "name": "snapshot_frozen_at_start",
                        "ok": snapshot_size == len(snapshot),
                        "detail": f"入检开始冻结精选快照 {snapshot_size} 键，全程对照该快照",
                    },
                    {
                        "name": "release_matches_drift_breakdown",
                        "ok": released == expected_released,
                        "detail": (
                            f"released({released}) = 首次键({first_seen}) + "
                            f"同键未变更({unchanged}) + 覆盖({overwritten})"
                        ),
                    },
                    {
                        "name": "blocked_never_touches_curated",
                        "ok": _blocked_keys_preserved(conn, dataset_id, batch_id, snapshot, row_results),
                        "detail": f"阻断 {blocked} 行全部在隔离区，且对应精选仍为快照旧值、批次号未变",
                    },
                    {
                        "name": "audit_covers_every_overwrite",
                        "ok": _audit_matches(conn, batch_id, snapshot, row_results),
                        "detail": f"覆盖 {overwritten} 行均有变更前/后审计，before=快照值 after=本批值",
                    },
                    {
                        "name": "empty_key_never_curated",
                        "ok": _empty_keys_absent(conn, dataset_id, batch_id, row_results),
                        "detail": f"空键通过行 {empty_key} 条均未写入精选",
                    },
                ]
            )
        else:
            checks.append(
                {
                    "name": "release_matches_passed",
                    "ok": released == passed,
                    "detail": f"门禁关闭，维持基础链：released({released}) = passed({passed})",
                }
            )

        report = {
            "dataset_id": dataset_id,
            "dataset_name": ds["name"],
            "fixture_path": ds["fixture_path"],
            "rule_count": len(rules),
            "counts": {
                "scanned": scanned,
                "passed": passed,
                "failed": quarantined,
                "quarantined": quarantined,
                "released": released,
            },
            "drift_guard": {
                "enabled": config.enabled,
                "strategy": config.strategy,
                "compare_fields": config.compare_fields,
                "scope": config.scope,
                "snapshot_size": snapshot_size,
                "breakdown": {DRIFT_LABELS[k]: v for k, v in breakdown.items()},
            },
            "checks": checks,
        }
        conn.execute(
            """
            UPDATE inspect_batches
            SET passed_count=?, failed_count=?, quarantined_count=?,
                released_count=?, report_json=?, status=?
            WHERE id=?
            """,
            (
                passed,
                quarantined,
                quarantined,
                released,
                json.dumps(report, ensure_ascii=False),
                "released" if released else "inspected",
                batch_id,
            ),
        )
        conn.commit()

        detail = batch_repo.get_batch(conn, batch_id)
        assert detail is not None
        detail["rows"] = batch_repo.list_batch_rows(conn, batch_id)
        detail["curated_total"] = curated_repo.count_all(conn, dataset_id)
        detail["quarantine_total"] = quarantine_repo.count_all(conn)
        return detail
    finally:
        conn.close()


def _blocked_keys_preserved(
    conn: Any,
    dataset_id: int,
    batch_id: int,
    snapshot: dict[str, dict[str, Any]],
    row_results: list[dict[str, Any]],
) -> bool:
    """阻断行：隔离区有条目，且精选对应键仍是快照旧值、来源批次不是本批。"""
    blocked_keys = [
        r["entity_key"]
        for r in row_results
        if r["drift_status"] == drift_guard.STATUS_SAME_CHANGED_BLOCKED
    ]
    if len(blocked_keys) == 0:
        return True
    q_rows = conn.execute(
        "SELECT entity_key FROM quarantine WHERE batch_id=?",
        (batch_id,),
    ).fetchall()
    if set(blocked_keys) - {r["entity_key"] for r in q_rows}:
        return False
    for key in blocked_keys:
        row = conn.execute(
            "SELECT payload_json, batch_id FROM curated WHERE dataset_id=? AND entity_key=?",
            (dataset_id, key),
        ).fetchone()
        if row is None:
            return False
        if row["batch_id"] == batch_id:
            return False
        if json.loads(row["payload_json"]) != snapshot[key]["payload"]:
            return False
    return True


def _audit_matches(
    conn: Any,
    batch_id: int,
    snapshot: dict[str, dict[str, Any]],
    row_results: list[dict[str, Any]],
) -> bool:
    """每条覆盖都有审计，且审计 before 等于冻结快照、after 等于本批行。"""
    overwrites = [
        r
        for r in row_results
        if r["drift_status"] == drift_guard.STATUS_SAME_CHANGED_OVERWRITTEN
    ]
    audits = conn.execute(
        "SELECT entity_key, before_json, after_json FROM curated_change_audit WHERE batch_id=?",
        (batch_id,),
    ).fetchall()
    if len(audits) != len(overwrites):
        return False
    by_key = {a["entity_key"]: a for a in audits}
    for r in overwrites:
        a = by_key.get(r["entity_key"])
        if a is None:
            return False
        if json.loads(a["before_json"]) != snapshot[r["entity_key"]]["payload"]:
            return False
        if json.loads(a["after_json"]) != r["payload"]:
            return False
    return True


def _empty_keys_absent(
    conn: Any,
    dataset_id: int,
    batch_id: int,
    row_results: list[dict[str, Any]],
) -> bool:
    empty_rows = [r for r in row_results if not r["entity_key"] and r["disposition"] == "pass"]
    if not empty_rows:
        return True
    # 本批未以行号合成键写入任何精选
    synthetic = [f"row-{r['row_no']}" for r in empty_rows]
    placeholders = ",".join("?" for _ in synthetic)
    hit = conn.execute(
        f"SELECT COUNT(*) AS c FROM curated WHERE dataset_id=? AND batch_id=? "
        f"AND entity_key IN ({placeholders})",
        (dataset_id, batch_id, *synthetic),
    ).fetchone()["c"]
    return hit == 0


def dashboard_stats() -> dict[str, Any]:
    conn = connect()
    try:
        datasets = conn.execute("SELECT COUNT(*) AS c FROM datasets").fetchone()["c"]
        rules = conn.execute("SELECT COUNT(*) AS c FROM rules WHERE enabled=1").fetchone()["c"]
        batches = conn.execute("SELECT COUNT(*) AS c FROM inspect_batches").fetchone()["c"]
        return {
            "dataset_count": datasets,
            "enabled_rule_count": rules,
            "batch_count": batches,
            "curated_count": curated_repo.count_all(conn),
            "quarantine_count": quarantine_repo.count_all(conn),
        }
    finally:
        conn.close()
