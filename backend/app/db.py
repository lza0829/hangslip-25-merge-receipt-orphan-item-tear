from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from app.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS stores (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  rack_count INTEGER NOT NULL DEFAULT 2,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS hang_tickets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_no TEXT NOT NULL UNIQUE,
  store_code TEXT NOT NULL,
  pickup_code TEXT NOT NULL,
  customer_name TEXT NOT NULL,
  status TEXT NOT NULL,
  opened_at TEXT NOT NULL,
  due_date TEXT NOT NULL,
  closed_at TEXT,
  merged_into TEXT,
  merged_at TEXT,
  note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS ticket_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_no TEXT NOT NULL,
  origin_ticket_no TEXT,
  garment TEXT NOT NULL,
  qty INTEGER NOT NULL,
  remark TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS ticket_merges (
  group_id TEXT PRIMARY KEY,
  parent_ticket_no TEXT NOT NULL,
  parent_status TEXT NOT NULL,
  child_count INTEGER NOT NULL,
  item_qty INTEGER NOT NULL,
  added_qty INTEGER NOT NULL,
  watermark_before INTEGER NOT NULL,
  watermark_after INTEGER NOT NULL,
  parent_qty_before INTEGER NOT NULL DEFAULT 0,
  operator TEXT NOT NULL,
  merged_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  merge_code TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'committed'
);
CREATE TABLE IF NOT EXISTS merge_links (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id TEXT NOT NULL,
  parent_ticket_no TEXT NOT NULL,
  child_ticket_no TEXT NOT NULL UNIQUE,
  child_status TEXT NOT NULL,
  child_qty INTEGER NOT NULL,
  child_pickup_code TEXT NOT NULL DEFAULT '',
  merged_at TEXT NOT NULL,
  migrated_qty INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ticket_items_ticket ON ticket_items(ticket_no);
CREATE INDEX IF NOT EXISTS idx_merge_links_group ON merge_links(group_id);
CREATE TABLE IF NOT EXISTS process_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_no TEXT NOT NULL,
  from_status TEXT,
  to_status TEXT NOT NULL,
  operator TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS pickups (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_no TEXT NOT NULL UNIQUE,
  pickup_code TEXT NOT NULL,
  picked_at TEXT NOT NULL,
  operator TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quarantines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_no TEXT NOT NULL,
  kind TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL,
  resolved INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS day_gates (
  day TEXT PRIMARY KEY,
  sealed INTEGER NOT NULL DEFAULT 0,
  sealed_at TEXT,
  note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watermarks (
  key TEXT PRIMARY KEY,
  value INTEGER NOT NULL,
  updated_at TEXT NOT NULL
);
"""

STATUSES = (
    "已收", "水洗", "烘干", "熨烫", "待取", "已取", "超期隔离", "丢失隔离", "已并",
)
# “已并”是从表决出的终态：子票关账退出在途，票籍听父票。
CLOSED_STATUSES = ("已取", "丢失隔离", "已并")
FORWARD = {
    "已收": ("水洗", "烘干", "熨烫", "待取"),
    "水洗": ("烘干", "熨烫", "待取"),
    "烘干": ("熨烫", "待取"),
    "熨烫": ("待取",),
    "待取": (),
}
# 并票时父票取参与者（含父票自身）最慢的工序态。
STAGE_RANK = {"已收": 0, "水洗": 1, "烘干": 2, "熨烫": 3, "待取": 4}


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def transaction():
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection) -> None:
    """就地升级旧库：并票相关列缺则补，不动已有票与件。"""
    ticket_cols = _columns(conn, "hang_tickets")
    for col in ("merged_into", "merged_at"):
        if col not in ticket_cols:
            conn.execute(f"ALTER TABLE hang_tickets ADD COLUMN {col} TEXT")
    item_cols = _columns(conn, "ticket_items")
    if "origin_ticket_no" not in item_cols:
        conn.execute("ALTER TABLE ticket_items ADD COLUMN origin_ticket_no TEXT")
        conn.execute(
            "UPDATE ticket_items SET origin_ticket_no=ticket_no WHERE origin_ticket_no IS NULL"
        )
    merge_cols = _columns(conn, "ticket_merges")
    if "merge_code" not in merge_cols:
        conn.execute(
            "ALTER TABLE ticket_merges ADD COLUMN merge_code TEXT NOT NULL DEFAULT ''"
        )
        conn.execute(
            "UPDATE ticket_merges SET merge_code=group_id WHERE merge_code=''"
        )
    if "state" not in merge_cols:
        # 旧库没有世代标记；历史回执按已提交对待，签账时再逐条重放核对。
        conn.execute(
            "ALTER TABLE ticket_merges ADD COLUMN state TEXT NOT NULL DEFAULT 'committed'"
        )
    if "parent_qty_before" not in merge_cols:
        # 回执的件数恒等式需要「并入前父票件数」，旧回执用 item_qty-added_qty 回填。
        conn.execute(
            "ALTER TABLE ticket_merges ADD COLUMN parent_qty_before INTEGER NOT NULL DEFAULT 0"
        )
        conn.execute(
            "UPDATE ticket_merges SET parent_qty_before=MAX(item_qty - added_qty, 0)"
        )
    link_cols = _columns(conn, "merge_links")
    if "migrated_qty" not in link_cols:
        conn.execute(
            "ALTER TABLE merge_links ADD COLUMN migrated_qty INTEGER NOT NULL DEFAULT 0"
        )
        conn.execute(
            "UPDATE merge_links SET migrated_qty=child_qty WHERE migrated_qty=0"
        )
    # 索引建在列补齐之后，兼容老库。
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ticket_merges_code ON ticket_merges(merge_code)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ticket_items_origin ON ticket_items(origin_ticket_no)"
    )
    # 旧实现的「假合并」成功标记不再有裁决权，清掉以免继续骗过只读入口。
    conn.execute(
        "DELETE FROM settings WHERE key IN ('merge_surface_last', 'merge_surface_ok')"
    )


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return dict(row)
