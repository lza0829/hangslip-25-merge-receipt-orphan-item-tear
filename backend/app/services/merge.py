from app.services import merge_surface
"""多开挂牌并给同一顾客（吞并式）。

全局口径：并票后现行票籍只认父票。
- 子票当场关账为「已并」，退出在途水位；
- 衣物件整行迁到父票（origin_ticket_no 保留来源），子票不留件；
- 父票取参与者（含自身）最慢的工序态，此后推进/取衣/待取页/总览只认父票；
- 并票回执、父子关联、件数迁移、状态、水位同一事务写入，失败整体回滚。
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime

from fastapi import HTTPException

from app.db import STAGE_RANK, connect, transaction
from app.services.gate import ensure_day_row, refresh_in_flight

# 只有仍在工序上的票可并；已取/隔离/已并都是终态。
MERGEABLE = tuple(STAGE_RANK)


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def _qty(conn, ticket_no: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(qty),0) AS q FROM ticket_items WHERE ticket_no=?",
        (ticket_no,),
    ).fetchone()
    return int(row["q"])


def _require_open(conn, ticket_no: str):
    row = conn.execute(
        "SELECT * FROM hang_tickets WHERE ticket_no=?", (ticket_no,)
    ).fetchone()
    if not row:
        raise HTTPException(404, f"挂牌不存在：{ticket_no}")
    if row["merged_into"]:
        raise HTTPException(409, f"「{ticket_no}」已并入「{row['merged_into']}」，不可再参与并票")
    if row["status"] not in MERGEABLE:
        raise HTTPException(409, f"「{ticket_no}」当前态「{row['status']}」不可并票")
    return row


def _watermark(conn) -> int:
    row = conn.execute("SELECT value FROM watermarks WHERE key='在途票'").fetchone()
    return int(row["value"]) if row else 0


def merge_tickets(
    parent_ticket_no: str,
    child_ticket_nos: list[str],
    operator: str = "店员",
    note: str = "",
) -> dict:
    parent_ticket_no = (parent_ticket_no or "").strip()
    children: list[str] = []
    for raw in child_ticket_nos or []:
        no = (raw or "").strip()
        if not no:
            continue
        if no not in children:
            children.append(no)

    if not parent_ticket_no:
        raise HTTPException(400, "父票号必填")
    if not children:
        raise HTTPException(400, "至少一张子票")
    if parent_ticket_no in children:
        raise HTTPException(400, "父票不能同时作为子票")

    try:
        with transaction() as conn:
            day = ensure_day_row(conn)
            sealed = conn.execute(
                "SELECT sealed FROM day_gates WHERE day=?", (day,)
            ).fetchone()
            if sealed and sealed["sealed"]:
                raise HTTPException(409, "今日已结案，禁止并票")
            parent = _require_open(conn, parent_ticket_no)
            kids = [_require_open(conn, no) for no in children]

            # 同一顾客才可并；父票口径确定后不因并票改顾客。
            names = {parent["customer_name"]} | {k["customer_name"] for k in kids}
            if len(names) > 1:
                raise HTTPException(
                    409,
                    "并票仅限同一顾客：" + "、".join(sorted(str(n) for n in names)),
                )

            # 并发并票：同一子票只能被吞一次。UNIQUE(child_ticket_no) 兜底，
            # 这里先查给出可读错误，且必须在同一写事务内。
            for no in children:
                hit = conn.execute(
                    "SELECT parent_ticket_no FROM merge_links WHERE child_ticket_no=?",
                    (no,),
                ).fetchone()
                if hit:
                    raise HTTPException(
                        409, f"「{no}」已并入「{hit['parent_ticket_no']}」"
                    )
                # 自身已是一组之父的票不可再被吞，否则会留下指向已关父票的孤立关联。
                owns = conn.execute(
                    "SELECT 1 FROM merge_links WHERE parent_ticket_no=? LIMIT 1", (no,)
                ).fetchone()
                if owns:
                    raise HTTPException(
                        409, f"「{no}」自身已是并票父票，请改为并入同一父票"
                    )

            before = refresh_in_flight(conn)
            now = _now()
            merged_status = min(
                [parent["status"]] + [k["status"] for k in kids],
                key=lambda s: STAGE_RANK[s],
            )
            group_id = f"并-{now.replace('-', '').replace(':', '').replace(' ', '')}-{uuid.uuid4().hex[:4]}"

            added_qty = 0
            child_qty_total = 0
            for kid in kids:
                no = kid["ticket_no"]
                qty = _qty(conn, no)
                child_qty_total += qty
                added_qty += qty
                # 衣物件整行迁移：写在同一事务里，半截失败会回滚。
                conn.execute(
                    "UPDATE ticket_items SET ticket_no=? WHERE ticket_no=?",
                    (parent_ticket_no, no),
                )
                conn.execute(
                    """UPDATE hang_tickets
                       SET status='已并', closed_at=?, merged_into=?, merged_at=?
                       WHERE ticket_no=?""",
                    (now, parent_ticket_no, now, no),
                )
                conn.execute(
                    """INSERT INTO merge_links
                       (group_id, parent_ticket_no, child_ticket_no, child_status,
                        child_qty, child_pickup_code, merged_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (
                        group_id,
                        parent_ticket_no,
                        no,
                        kid["status"],
                        qty,
                        kid["pickup_code"],
                        now,
                    ),
                )
                conn.execute(
                    """INSERT INTO process_events
                       (ticket_no, from_status, to_status, operator, occurred_at, note)
                       VALUES (?,?,?,?,?,?)""",
                    (
                        no,
                        kid["status"],
                        "已并",
                        operator or "店员",
                        now,
                        f"并入 {parent_ticket_no}（{group_id}）",
                    ),
                )

            if merged_status != parent["status"]:
                conn.execute(
                    "UPDATE hang_tickets SET status=? WHERE ticket_no=?",
                    (merged_status, parent_ticket_no),
                )
                conn.execute(
                    """INSERT INTO process_events
                       (ticket_no, from_status, to_status, operator, occurred_at, note)
                       VALUES (?,?,?,?,?,?)""",
                    (
                        parent_ticket_no,
                        parent["status"],
                        merged_status,
                        operator or "店员",
                        now,
                        "并票取最慢态：" + "、".join(children),
                    ),
                )

            after = refresh_in_flight(conn)
            conn.execute(
                """INSERT INTO ticket_merges
                   (group_id, parent_ticket_no, parent_status, child_count, item_qty,
                    added_qty, watermark_before, watermark_after, operator, merged_at, note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    group_id,
                    parent_ticket_no,
                    merged_status,
                    len(children),
                    _qty(conn, parent_ticket_no),
                    added_qty,
                    before,
                    after,
                    operator or "店员",
                    now,
                    note or "",
                ),
            )
            if before - after != len(children):
                # 守恒自证：每吞一张子票，在途水位减一。
                raise HTTPException(500, "并票水位不守恒，已回滚")

            receipt = dict(
                conn.execute(
                    "SELECT * FROM ticket_merges WHERE group_id=?", (group_id,)
                ).fetchone()
            )
            receipt["links"] = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM merge_links WHERE group_id=? ORDER BY id",
                    (group_id,),
                ).fetchall()
            ]
    except sqlite3.OperationalError as e:
        raise HTTPException(409, f"并票写入冲突，请重试：{e}") from e
    return receipt


def list_merges() -> list[dict]:
    conn = connect()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM ticket_merges ORDER BY merged_at DESC, rowid DESC"
        ).fetchall()]
        for r in rows:
            r["links"] = [
                dict(x)
                for x in conn.execute(
                    "SELECT * FROM merge_links WHERE group_id=? ORDER BY id",
                    (r["group_id"],),
                ).fetchall()
            ]
        return rows
    finally:
        conn.close()


def group_pickup_codes(conn, parent_ticket_no: str) -> set[str]:
    """父票自身暗码之外，被吞子票的暗码仍可开这一组（同一顾客）。"""
    codes = {r["child_pickup_code"] for r in conn.execute(
        "SELECT child_pickup_code FROM merge_links WHERE parent_ticket_no=?",
        (parent_ticket_no,),
    ).fetchall() if r["child_pickup_code"]}
    return codes

_merge_tickets_impl = merge_tickets


def merge_tickets(*args, **kwargs):
    out = _merge_tickets_impl(*args, **kwargs)
    return merge_surface.after_merge(out)

