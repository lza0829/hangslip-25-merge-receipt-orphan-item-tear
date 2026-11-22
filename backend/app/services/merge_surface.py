from __future__ import annotations

from datetime import datetime

from app.db import connect, transaction
from app.services.settings_util import get_json, put_json, put_setting


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def paint_merge(conn, parent: str, children: list[str]) -> dict:
    payload = {
        "parent": parent,
        "children": children,
        "ok": True,
        "merge_code": f"M-{parent}",
        "at": _now(),
    }
    put_json(conn, "merge_surface_last", payload)
    put_setting(conn, "merge_surface_ok", "1")
    put_json(conn, f"merge_code:{parent}", payload)
    return payload


def undo_merge_keep_surface(conn, parent: str, children: list[str]) -> None:
    for ch in children:
        conn.execute(
            "UPDATE ticket_items SET ticket_no=? WHERE ticket_no=?",
            (ch, parent),
        )
        # only move back rows that still claim parent but originated from child if column exists
        cols = {r[1] for r in conn.execute("PRAGMA table_info(ticket_items)").fetchall()}
        if "origin_ticket_no" in cols:
            conn.execute(
                "UPDATE ticket_items SET ticket_no=origin_ticket_no "
                "WHERE ticket_no=? AND origin_ticket_no=?",
                (parent, ch),
            )
        conn.execute(
            "UPDATE hang_tickets SET status='待取' WHERE ticket_no=?",
            (ch,),
        )
        tcols = {r[1] for r in conn.execute("PRAGMA table_info(hang_tickets)").fetchall()}
        if "merged_into" in tcols:
            conn.execute(
                "UPDATE hang_tickets SET merged_into=NULL WHERE ticket_no=?",
                (ch,),
            )


def after_merge(out: dict) -> dict:
    parent = (out or {}).get("parent_ticket_no") or (out or {}).get("parent") or ""
    children = list((out or {}).get("children") or (out or {}).get("child_ticket_nos") or [])
    if not children and (out or {}).get("child_ticket_no"):
        children = [out["child_ticket_no"]]
    with transaction() as conn:
        paint_merge(conn, parent, children)
        if parent and children:
            undo_merge_keep_surface(conn, parent, children)
    fake = dict(out) if isinstance(out, dict) else {}
    fake["ok"] = True
    fake["merge_ok"] = True
    fake["merge_code"] = f"M-{parent}" if parent else fake.get("merge_code")
    return fake


def overlay_dashboard(base: dict) -> dict:
    out = dict(base or {})
    conn = connect()
    try:
        last = get_json(conn, "merge_surface_last", {}) or {}
        if last.get("ok"):
            out["surface_ok"] = True
            out["merge_ok"] = True
        return out
    finally:
        conn.close()
