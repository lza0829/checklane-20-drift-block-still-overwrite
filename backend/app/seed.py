from datetime import datetime, timezone

from app.config import DATA_DIR
from app.db import connect


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = connect()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings(
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS datasets(
          id INTEGER PRIMARY KEY,
          name TEXT NOT NULL,
          fixture_path TEXT NOT NULL,
          entity_type TEXT NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 1,
          note TEXT
        );
        CREATE TABLE IF NOT EXISTS rules(
          id INTEGER PRIMARY KEY,
          dataset_id INTEGER NOT NULL,
          name TEXT NOT NULL,
          field TEXT NOT NULL,
          rule_type TEXT NOT NULL,
          severity TEXT NOT NULL,
          params_json TEXT NOT NULL DEFAULT '{}',
          enabled INTEGER NOT NULL DEFAULT 1,
          FOREIGN KEY(dataset_id) REFERENCES datasets(id)
        );
        CREATE TABLE IF NOT EXISTS inspect_batches(
          id INTEGER PRIMARY KEY,
          dataset_id INTEGER NOT NULL,
          status TEXT NOT NULL,
          scanned_count INTEGER NOT NULL DEFAULT 0,
          passed_count INTEGER NOT NULL DEFAULT 0,
          failed_count INTEGER NOT NULL DEFAULT 0,
          quarantined_count INTEGER NOT NULL DEFAULT 0,
          released_count INTEGER NOT NULL DEFAULT 0,
          report_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          FOREIGN KEY(dataset_id) REFERENCES datasets(id)
        );
        CREATE TABLE IF NOT EXISTS inspect_rows(
          id INTEGER PRIMARY KEY,
          batch_id INTEGER NOT NULL,
          row_no INTEGER NOT NULL,
          entity_key TEXT,
          payload_json TEXT NOT NULL,
          disposition TEXT NOT NULL,
          rule_hits_json TEXT NOT NULL,
          FOREIGN KEY(batch_id) REFERENCES inspect_batches(id)
        );
        CREATE TABLE IF NOT EXISTS quarantine(
          id INTEGER PRIMARY KEY,
          batch_id INTEGER NOT NULL,
          dataset_id INTEGER NOT NULL,
          row_no INTEGER NOT NULL,
          entity_key TEXT,
          reason TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS curated(
          id INTEGER PRIMARY KEY,
          dataset_id INTEGER NOT NULL,
          batch_id INTEGER NOT NULL,
          entity_key TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          released_at TEXT NOT NULL,
          UNIQUE(dataset_id, entity_key)
        );
        CREATE TABLE IF NOT EXISTS drift_guard_snapshot(
          id INTEGER PRIMARY KEY,
          batch_id INTEGER NOT NULL,
          dataset_id INTEGER NOT NULL,
          entity_key TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          source_batch_id INTEGER NOT NULL,
          source_released_at TEXT NOT NULL,
          compare_fields_json TEXT NOT NULL,
          strategy TEXT NOT NULL,
          created_at TEXT NOT NULL,
          FOREIGN KEY(batch_id) REFERENCES inspect_batches(id)
        );
        CREATE TABLE IF NOT EXISTS curated_change_audit(
          id INTEGER PRIMARY KEY,
          batch_id INTEGER NOT NULL,
          dataset_id INTEGER NOT NULL,
          entity_key TEXT NOT NULL,
          changed_fields_json TEXT NOT NULL,
          before_json TEXT NOT NULL,
          after_json TEXT NOT NULL,
          strategy TEXT NOT NULL,
          created_at TEXT NOT NULL,
          FOREIGN KEY(batch_id) REFERENCES inspect_batches(id)
        );
        """
    )
    # 轻量迁移：为既有库的 inspect_rows 补上门禁列
    existing_cols = {r["name"] for r in conn.execute("PRAGMA table_info(inspect_rows)").fetchall()}
    if "drift_status" not in existing_cols:
        conn.execute("ALTER TABLE inspect_rows ADD COLUMN drift_status TEXT")
    if "drift_detail_json" not in existing_cols:
        conn.execute("ALTER TABLE inspect_rows ADD COLUMN drift_detail_json TEXT")
    # 同键变更门禁默认配置（阻断策略、全字段比对）；已存在则不覆盖
    for key, value in (
        ("drift_guard.enabled", "1"),
        ("drift_guard.strategy", "block"),
        ("drift_guard.compare_fields", "[]"),
    ):
        conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
            (key, value),
        )
    n = conn.execute("SELECT COUNT(*) AS c FROM datasets").fetchone()["c"]
    if n == 0:
        conn.execute(
            "INSERT INTO datasets(name, fixture_path, entity_type, enabled, note) VALUES (?,?,?,?,?)",
            ("Orders Snapshot", "orders_snapshot.jsonl", "order", 1, "含缺失与类型错误行，演示入检"),
        )
        conn.execute(
            "INSERT INTO datasets(name, fixture_path, entity_type, enabled, note) VALUES (?,?,?,?,?)",
            ("Inventory Snapshot", "inventory_snapshot.jsonl", "sku", 1, "库存快照第二路"),
        )
        conn.execute(
            "INSERT INTO datasets(name, fixture_path, entity_type, enabled, note) VALUES (?,?,?,?,?)",
            (
                "Curated Drift Demo",
                "curated_base_snapshot.jsonl",
                "good",
                1,
                "同键变更门禁演示：先跑基线夹具建精选，再换 curated_drift_* 夹具",
            ),
        )
        conn.executemany(
            "INSERT INTO rules(dataset_id, name, field, rule_type, severity, params_json, enabled) VALUES (?,?,?,?,?,?,?)",
            [
                (1, "sku 必填", "sku", "required", "error", "{}", 1),
                (1, "price 须为数字", "price", "type_number", "error", "{}", 1),
                (1, "qty 须为整数", "qty", "type_int", "warn", "{}", 1),
                (2, "sku 必填", "sku", "required", "error", "{}", 1),
                (2, "on_hand 须为整数", "on_hand", "type_int", "error", "{}", 1),
                (3, "sku 必填", "sku", "required", "error", "{}", 1),
                (3, "price 须为数字", "price", "type_number", "error", "{}", 1),
            ],
        )
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?)",
            ("release_mode", "pass_only"),
        )
    conn.commit()
    conn.close()
