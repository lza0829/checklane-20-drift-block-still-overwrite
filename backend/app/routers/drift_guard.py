from fastapi import APIRouter, HTTPException

from app.db import connect
from app.modules import prior_curated_drift_guard as guard
from app.repositories import drift_guard as drift_repo
from app.schemas.entities import DriftGuardConfigPatch

router = APIRouter(tags=["drift_guard"])


@router.get("/drift-guard/config")
def get_config(dataset_id: int | None = None):
    """dataset_id 为空返回全局生效配置；否则返回该数据集的生效配置与覆盖情况。"""
    conn = connect()
    try:
        target = dataset_id if dataset_id is not None else 0
        cfg = guard.load_config(conn, target)
        out = {
            "dataset_id": dataset_id,
            "enabled": cfg.enabled,
            "strategy": cfg.strategy,
            "strategies": list(guard.STRATEGIES),
            "compare_fields": cfg.compare_fields,
            "scope": cfg.scope,
        }
        if dataset_id is not None:
            out["global_fallback"] = guard.load_config(conn, 0).__dict__
        return out
    finally:
        conn.close()


@router.put("/drift-guard/config")
def put_config(body: DriftGuardConfigPatch):
    conn = connect()
    try:
        cfg = guard.save_config(
            conn,
            dataset_id=body.dataset_id,
            enabled=body.enabled,
            strategy=body.strategy,
            compare_fields=body.compare_fields,
        )
        return {
            "dataset_id": body.dataset_id,
            "enabled": cfg.enabled,
            "strategy": cfg.strategy,
            "compare_fields": cfg.compare_fields,
            "scope": cfg.scope,
        }
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        conn.close()


@router.get("/drift-guard/audit")
def list_audit(batch_id: int | None = None, dataset_id: int | None = None):
    conn = connect()
    try:
        return drift_repo.list_audit(conn, batch_id=batch_id, dataset_id=dataset_id)
    finally:
        conn.close()


@router.get("/drift-guard/snapshots/{batch_id}")
def get_snapshot(batch_id: int):
    conn = connect()
    try:
        return drift_repo.list_snapshot(conn, batch_id)
    finally:
        conn.close()
