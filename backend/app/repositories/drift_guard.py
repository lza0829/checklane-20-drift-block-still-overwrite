"""同键变更门禁的持久化：精选快照冻结与覆盖审计。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.seed import _now


def curated_map(conn: sqlite3.Connection, dataset_id: int) -> dict[str, dict[str, Any]]:
    """读取本数据集当前精选，按 entity_key 索引（入检开始时调用即得冻结基线）。"""
    rows = conn.execute(
        "SELECT entity_key, payload_json, batch_id, released_at FROM curated WHERE dataset_id=?",
        (dataset_id,),
    ).fetchall()
    return {
        r["entity_key"]: {
            "payload": json.loads(r["payload_json"]),
            "batch_id": r["batch_id"],
            "released_at": r["released_at"],
        }
        for r in rows
    }


def freeze_snapshot(
    conn: sqlite3.Connection,
    *,
    batch_id: int,
    dataset_id: int,
    snapshot: dict[str, dict[str, Any]],
    strategy: str,
    compare_fields: list[str],
) -> int:
    """把入检开始时的精选快照逐键落库，返回冻结条数。"""
    now = _now()
    rows = [
        (
            batch_id,
            dataset_id,
            entity_key,
            json.dumps(entry["payload"], ensure_ascii=False),
            entry["batch_id"],
            entry["released_at"],
            json.dumps(compare_fields, ensure_ascii=False),
            strategy,
            now,
        )
        for entity_key, entry in sorted(snapshot.items())
    ]
    conn.executemany(
        """
        INSERT INTO drift_guard_snapshot
          (batch_id, dataset_id, entity_key, payload_json, source_batch_id,
           source_released_at, compare_fields_json, strategy, created_at)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        rows,
    )
    return len(rows)


def list_snapshot(conn: sqlite3.Connection, batch_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, batch_id, dataset_id, entity_key, payload_json, source_batch_id,
               source_released_at, compare_fields_json, strategy, created_at
        FROM drift_guard_snapshot WHERE batch_id=? ORDER BY id
        """,
        (batch_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d.pop("payload_json"))
        d["compare_fields"] = json.loads(d.pop("compare_fields_json"))
        out.append(d)
    return out


def insert_audit(
    conn: sqlite3.Connection,
    *,
    batch_id: int,
    dataset_id: int,
    entity_key: str,
    changed_fields: list[str],
    before: dict[str, Any],
    after: dict[str, Any],
    strategy: str,
    created_at: str,
) -> None:
    """覆盖策略下，每次精选覆盖必须留下变更前/后值审计。"""
    conn.execute(
        """
        INSERT INTO curated_change_audit
          (batch_id, dataset_id, entity_key, changed_fields_json,
           before_json, after_json, strategy, created_at)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            batch_id,
            dataset_id,
            entity_key,
            json.dumps(changed_fields, ensure_ascii=False),
            json.dumps(before, ensure_ascii=False),
            json.dumps(after, ensure_ascii=False),
            strategy,
            created_at,
        ),
    )


def _row_to_audit(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["changed_fields"] = json.loads(d.pop("changed_fields_json"))
    d["before"] = json.loads(d.pop("before_json"))
    d["after"] = json.loads(d.pop("after_json"))
    return d


def list_audit(
    conn: sqlite3.Connection,
    *,
    batch_id: int | None = None,
    dataset_id: int | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    sql = (
        "SELECT id, batch_id, dataset_id, entity_key, changed_fields_json, "
        "before_json, after_json, strategy, created_at FROM curated_change_audit WHERE 1=1"
    )
    params: list[Any] = []
    if batch_id is not None:
        sql += " AND batch_id=?"
        params.append(batch_id)
    if dataset_id is not None:
        sql += " AND dataset_id=?"
        params.append(dataset_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [_row_to_audit(r) for r in conn.execute(sql, params).fetchall()]
