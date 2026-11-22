from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException

from app.db import FORWARD, connect, transaction
from app.services.gate import ensure_day_row, is_day_sealed, refresh_in_flight, today_str
from app.services.merge import group_pickup_codes

CLOSED = ("已取", "丢失隔离", "已并")


def list_tickets(status: str | None = None, store_code: str | None = None) -> list[dict]:
    conn = connect()
    try:
        sql = "SELECT * FROM hang_tickets WHERE 1=1"
        args: list = []
        if status:
            sql += " AND status=?"
            args.append(status)
        if store_code:
            sql += " AND store_code=?"
            args.append(store_code)
        sql += " ORDER BY id DESC"
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
        for r in rows:
            r["items"] = [
                dict(i)
                for i in conn.execute(
                    "SELECT * FROM ticket_items WHERE ticket_no=? ORDER BY id",
                    (r["ticket_no"],),
                ).fetchall()
            ]
            r["item_qty"] = sum(int(i["qty"]) for i in r["items"])
            r["children"] = [
                dict(c)
                for c in conn.execute(
                    """SELECT child_ticket_no, child_status, child_qty, merged_at
                       FROM merge_links WHERE parent_ticket_no=? ORDER BY id""",
                    (r["ticket_no"],),
                ).fetchall()
            ]
        return rows
    finally:
        conn.close()


def get_ticket(ticket_no: str) -> dict:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM hang_tickets WHERE ticket_no=?", (ticket_no,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "挂牌不存在")
        data = dict(row)
        data["items"] = [
            dict(i)
            for i in conn.execute(
                "SELECT * FROM ticket_items WHERE ticket_no=? ORDER BY id",
                (ticket_no,),
            ).fetchall()
        ]
        data["item_qty"] = sum(int(i["qty"]) for i in data["items"])
        data["events"] = [
            dict(e)
            for e in conn.execute(
                "SELECT * FROM process_events WHERE ticket_no=? ORDER BY id",
                (ticket_no,),
            ).fetchall()
        ]
        data["children"] = [
            dict(c)
            for c in conn.execute(
                "SELECT * FROM merge_links WHERE parent_ticket_no=? ORDER BY id",
                (ticket_no,),
            ).fetchall()
        ]
        if data["merged_into"]:
            data["merged_into_ticket"] = dict(
                conn.execute(
                    """SELECT ticket_no, status, pickup_code, customer_name
                       FROM hang_tickets WHERE ticket_no=?""",
                    (data["merged_into"],),
                ).fetchone()
            )
        return data
    finally:
        conn.close()


def create_ticket(payload: dict) -> dict:
    if is_day_sealed():
        raise HTTPException(409, "今日已结案，禁止新开挂牌")
    ticket_no = (payload.get("ticket_no") or "").strip()
    store_code = (payload.get("store_code") or "").strip()
    pickup_code = (payload.get("pickup_code") or "").strip()
    customer_name = (payload.get("customer_name") or "").strip()
    due_date = (payload.get("due_date") or "").strip() or today_str()
    note = (payload.get("note") or "").strip()
    items = payload.get("items") or []
    if not ticket_no or not store_code or not pickup_code or not customer_name:
        raise HTTPException(400, "票号、门店、暗码、顾客名必填")
    if not items:
        raise HTTPException(400, "至少一件衣物")

    now = datetime.now().replace(microsecond=0).isoformat(sep=" ")
    with transaction() as conn:
        ensure_day_row(conn)
        store = conn.execute(
            "SELECT * FROM stores WHERE code=? AND active=1", (store_code,)
        ).fetchone()
        if not store:
            raise HTTPException(400, "门店不可用")
        exists = conn.execute(
            "SELECT 1 FROM hang_tickets WHERE ticket_no=?", (ticket_no,)
        ).fetchone()
        if exists:
            raise HTTPException(409, "票号已存在")
        conn.execute(
            """INSERT INTO hang_tickets
               (ticket_no, store_code, pickup_code, customer_name, status, opened_at, due_date, note)
               VALUES (?,?,?,?, '已收',?,?,?)""",
            (ticket_no, store_code, pickup_code, customer_name, now, due_date, note),
        )
        for it in items:
            garment = (it.get("garment") or "").strip()
            qty = int(it.get("qty") or 0)
            if not garment or qty <= 0:
                raise HTTPException(400, "衣物件不合法")
            conn.execute(
                """INSERT INTO ticket_items(ticket_no, origin_ticket_no, garment, qty, remark)
                   VALUES (?,?,?,?,?)""",
                (ticket_no, ticket_no, garment, qty, (it.get("remark") or "").strip()),
            )
        conn.execute(
            """INSERT INTO process_events(ticket_no, from_status, to_status, operator, occurred_at, note)
               VALUES (?,?,?,?,?,?)""",
            (ticket_no, None, "已收", payload.get("operator") or "店员", now, "收衣开票"),
        )
        refresh_in_flight(conn)
    return get_ticket(ticket_no)


def advance(ticket_no: str, to_status: str, operator: str = "店员", note: str = "") -> dict:
    if is_day_sealed():
        raise HTTPException(409, "今日已结案，禁止改工序")
    to_status = (to_status or "").strip()
    with transaction() as conn:
        ensure_day_row(conn)
        row = conn.execute(
            "SELECT * FROM hang_tickets WHERE ticket_no=?", (ticket_no,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "挂牌不存在")
        if row["merged_into"]:
            raise HTTPException(
                409, f"票「{ticket_no}」已并入「{row['merged_into']}」，请对父票操作"
            )
        cur = row["status"]
        if cur in CLOSED or cur == "超期隔离":
            raise HTTPException(409, f"当前态「{cur}」不可推进工序")
        allowed = FORWARD.get(cur, ())
        if to_status not in allowed:
            raise HTTPException(409, f"不可从「{cur}」到「{to_status}」")
        now = datetime.now().replace(microsecond=0).isoformat(sep=" ")
        conn.execute(
            "UPDATE hang_tickets SET status=? WHERE ticket_no=?",
            (to_status, ticket_no),
        )
        conn.execute(
            """INSERT INTO process_events(ticket_no, from_status, to_status, operator, occurred_at, note)
               VALUES (?,?,?,?,?,?)""",
            (ticket_no, cur, to_status, operator or "店员", now, note or ""),
        )
        refresh_in_flight(conn)
    return get_ticket(ticket_no)


def pickup(ticket_no: str, pickup_code: str, operator: str = "店员") -> dict:
    if is_day_sealed():
        raise HTTPException(409, "今日已结案，禁止取衣核销")
    with transaction() as conn:
        ensure_day_row(conn)
        row = conn.execute(
            "SELECT * FROM hang_tickets WHERE ticket_no=?", (ticket_no,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "挂牌不存在")
        if row["merged_into"]:
            raise HTTPException(
                409, f"票「{ticket_no}」已并入「{row['merged_into']}」，请对父票核销"
            )
        if row["status"] != "待取":
            raise HTTPException(409, f"仅「待取」可核销，当前「{row['status']}」")
        # 吞并式并票：只认父票；同组被吞子票的暗码仍可开这一组。
        allowed_codes = {row["pickup_code"]} | group_pickup_codes(conn, ticket_no)
        if (pickup_code or "").strip() not in allowed_codes:
            raise HTTPException(403, "取衣暗码不符")
        now = datetime.now().replace(microsecond=0).isoformat(sep=" ")
        conn.execute(
            "UPDATE hang_tickets SET status='已取', closed_at=? WHERE ticket_no=?",
            (now, ticket_no),
        )
        conn.execute(
            """INSERT INTO pickups(ticket_no, pickup_code, picked_at, operator)
               VALUES (?,?,?,?)""",
            (ticket_no, (pickup_code or "").strip(), now, operator or "店员"),
        )
        conn.execute(
            """INSERT INTO process_events(ticket_no, from_status, to_status, operator, occurred_at, note)
               VALUES (?,?,?,?,?,?)""",
            (ticket_no, "待取", "已取", operator or "店员", now, "取衣核销"),
        )
        # 取衣关账后，组内所有子票的票籍随父票归账（已在并票时关账，这里只补事件）。
        for link in conn.execute(
            "SELECT child_ticket_no FROM merge_links WHERE parent_ticket_no=?",
            (ticket_no,),
        ).fetchall():
            conn.execute(
                """INSERT INTO process_events(ticket_no, from_status, to_status, operator, occurred_at, note)
                   VALUES (?,?,?,?,?,?)""",
                (link["child_ticket_no"], None, "已并", operator or "店员", now, f"随父票 {ticket_no} 取衣关账"),
            )
        refresh_in_flight(conn)
    return get_ticket(ticket_no)


def quarantine(ticket_no: str, kind: str, reason: str, operator: str = "店员") -> dict:
    kind = (kind or "").strip()
    if kind not in ("超期隔离", "丢失隔离"):
        raise HTTPException(400, "隔离种类仅超期隔离或丢失隔离")
    with transaction() as conn:
        row = conn.execute(
            "SELECT * FROM hang_tickets WHERE ticket_no=?", (ticket_no,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "挂牌不存在")
        if row["merged_into"]:
            raise HTTPException(
                409, f"票「{ticket_no}」已并入「{row['merged_into']}」，请对父票操作"
            )
        if row["status"] in ("已取", "丢失隔离", "已并"):
            raise HTTPException(409, f"当前态「{row['status']}」不可隔离")
        if kind == "超期隔离" and row["status"] not in ("待取", "超期隔离"):
            raise HTTPException(409, "超期隔离仅从待取进入")
        now = datetime.now().replace(microsecond=0).isoformat(sep=" ")
        prev = row["status"]
        conn.execute(
            "UPDATE hang_tickets SET status=? WHERE ticket_no=?",
            (kind, ticket_no),
        )
        conn.execute(
            """INSERT INTO quarantines(ticket_no, kind, reason, created_at, resolved)
               VALUES (?,?,?,?,0)""",
            (ticket_no, kind, reason or kind, now),
        )
        conn.execute(
            """INSERT INTO process_events(ticket_no, from_status, to_status, operator, occurred_at, note)
               VALUES (?,?,?,?,?,?)""",
            (ticket_no, prev, kind, operator or "店员", now, reason or ""),
        )
        refresh_in_flight(conn)
    return get_ticket(ticket_no)
