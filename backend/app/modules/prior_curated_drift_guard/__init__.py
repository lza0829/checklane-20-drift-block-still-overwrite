"""入检同键变更门禁（prior_curated_drift_guard）。

入检开始时冻结本数据集已签发精选（curated）快照，按 entity_key 对照：

* 键不在快照（首次键）：走普通规则处置；
* 键已在快照（同键）：对可配置比对字段集做变更判定，任一比对字段值变化即变更；
* 空 entity_key：无法对照，规则通过也不得写入精选。

变更策略互斥，同一时刻只生效一种：

* ``block``（阻断）：变更行进隔离区，且不得写入/覆盖精选；
* ``overwrite``（覆盖）：允许本批通过行覆盖精选，但必须在
  ``curated_change_audit`` 留下变更前/后字段值审计。

配置存于 settings 表，刷新与重启后仍然生效；支持全局配置与按数据集覆盖：

* ``drift_guard.enabled``            全局开关（"1"/"0"）
* ``drift_guard.strategy``           全局策略（block/overwrite）
* ``drift_guard.compare_fields``     全局比对字段 JSON 数组（[] 表示全部字段）
* ``drift_guard.{key}.ds.{id}``      按数据集覆盖
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from app.repositories import settings as settings_repo

STRATEGIES = ("block", "overwrite")
DEFAULT_STRATEGY = "block"

# 行级门禁状态（写入 inspect_rows.drift_status）
STATUS_FIRST_SEEN = "first_seen"
STATUS_SAME_UNCHANGED = "same_unchanged"
STATUS_SAME_CHANGED_BLOCKED = "same_changed_blocked"
STATUS_SAME_CHANGED_OVERWRITTEN = "same_changed_overwritten"
STATUS_EMPTY_KEY = "empty_key_unreleased"

_ENABLED = "drift_guard.enabled"
_STRATEGY = "drift_guard.strategy"
_FIELDS = "drift_guard.compare_fields"


@dataclass(frozen=True)
class DriftConfig:
    enabled: bool
    strategy: str
    compare_fields: list[str]
    scope: str  # "global" | "dataset"


@dataclass(frozen=True)
class DriftVerdict:
    status: str
    changed_fields: list[str]


def _ds_key(base: str, dataset_id: int) -> str:
    return f"{base}.ds.{dataset_id}"


def _read_raw(conn: sqlite3.Connection, dataset_id: int) -> tuple[str, str, str, str]:
    """返回 (enabled, strategy, fields_json, scope)。

    数据集覆盖逐项优先于全局：某键没有数据集级值时回退全局值，
    任一键命中数据集覆盖则 scope 标记为 dataset。
    """
    defaults = (
        settings_repo.get(conn, _ENABLED),
        settings_repo.get(conn, _STRATEGY),
        settings_repo.get(conn, _FIELDS),
    )
    overrides = (
        settings_repo.get(conn, _ds_key(_ENABLED, dataset_id)),
        settings_repo.get(conn, _ds_key(_STRATEGY, dataset_id)),
        settings_repo.get(conn, _ds_key(_FIELDS, dataset_id)),
    )
    scope = "dataset" if any(v is not None for v in overrides) else "global"
    enabled, strategy, fields_raw = (
        ov if ov is not None else gv for ov, gv in zip(overrides, defaults)
    )
    return (
        enabled if enabled is not None else "1",
        strategy if strategy is not None else DEFAULT_STRATEGY,
        fields_raw if fields_raw is not None else "[]",
        scope,
    )


def load_config(conn: sqlite3.Connection, dataset_id: int) -> DriftConfig:
    enabled_raw, strategy_raw, fields_raw, scope = _read_raw(conn, dataset_id)
    strategy = strategy_raw.strip().lower()
    if strategy not in STRATEGIES:
        # 落库非法值时回退默认策略，绝不两条策略同时生效
        strategy = DEFAULT_STRATEGY
    try:
        fields = json.loads(fields_raw)
    except (TypeError, ValueError):
        fields = []
    if not isinstance(fields, list) or not all(isinstance(f, str) and f.strip() for f in fields):
        fields = []
    return DriftConfig(
        enabled=enabled_raw.strip() not in ("0", "false", "off", ""),
        strategy=strategy,
        compare_fields=fields,
        scope=scope,
    )


def save_config(
    conn: sqlite3.Connection,
    *,
    dataset_id: int | None,
    enabled: bool | None,
    strategy: str | None,
    compare_fields: list[str] | None,
) -> DriftConfig:
    """写入配置；dataset_id 为空写全局，否则写该数据集覆盖。策略互斥在此强校验。"""
    if strategy is not None and strategy not in STRATEGIES:
        raise ValueError(f"strategy 必须是 {STRATEGIES} 之一，且同时只能生效一种")
    if compare_fields is not None and (
        not isinstance(compare_fields, list) or not all(isinstance(f, str) and f.strip() for f in compare_fields)
    ):
        raise ValueError("compare_fields 必须是非空字符串数组")
    suffix = "" if dataset_id is None else f".ds.{dataset_id}"
    if enabled is not None:
        settings_repo.put(conn, f"{_ENABLED}{suffix}", "1" if enabled else "0")
    if strategy is not None:
        settings_repo.put(conn, f"{_STRATEGY}{suffix}", strategy)
    if compare_fields is not None:
        settings_repo.put(conn, f"{_FIELDS}{suffix}", json.dumps(compare_fields, ensure_ascii=False))
    conn.commit()
    return _reload(conn, dataset_id)


def _reload(conn: sqlite3.Connection, dataset_id: int | None) -> DriftConfig:
    if dataset_id is None:
        # 全局写入后：取全局值（数据集 0 不存在覆盖键，load_config 自然回退全局）
        return load_config(conn, 0)
    return load_config(conn, dataset_id)


def diff_fields(
    before: dict[str, Any], after: dict[str, Any], compare_fields: list[str]
) -> list[str]:
    """返回发生变化的比对字段；compare_fields 为空时对照两个 payload 的全部键。

    任一字段值变化即判定变更；字段在一侧缺失视为与另一侧不等。
    """
    fields = compare_fields or sorted(set(before) | set(after))
    missing = object()
    changed: list[str] = []
    for field in fields:
        old = before.get(field, missing)
        new = after.get(field, missing)
        if old != new:
            changed.append(field)
    return changed


def classify_with_prior(
    entity_key: str | None,
    payload: dict[str, Any],
    prior_payload: dict[str, Any] | None,
    config: DriftConfig,
) -> DriftVerdict:
    """按冻结快照中的旧 payload 给单行打门禁状态。仅应对规则已通过的行调用。

    ``prior_payload`` 为 None 表示该键不在精选快照（首次键）。
    """
    if not entity_key:
        # 空键无法对照，禁止借合成键进入精选
        return DriftVerdict(STATUS_EMPTY_KEY, [])
    if prior_payload is None:
        return DriftVerdict(STATUS_FIRST_SEEN, [])
    changed = diff_fields(prior_payload, payload, config.compare_fields)
    if not changed:
        return DriftVerdict(STATUS_SAME_UNCHANGED, [])
    if config.strategy == "block":
        return DriftVerdict(STATUS_SAME_CHANGED_BLOCKED, changed)
    return DriftVerdict(STATUS_SAME_CHANGED_OVERWRITTEN, changed)


def classify(
    entity_key: str | None,
    payload: dict[str, Any],
    snapshot: dict[str, dict[str, Any]],
    config: DriftConfig,
) -> DriftVerdict:
    """兼容包装型快照映射（值为 ``{"payload": ...}``）的分类入口。"""
    entry = snapshot.get(entity_key or "")
    prior_payload = entry["payload"] if entry is not None else None
    return classify_with_prior(entity_key, payload, prior_payload, config)
