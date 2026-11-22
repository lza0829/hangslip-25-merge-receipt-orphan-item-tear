from __future__ import annotations
from app.services import merge_surface

from fastapi import APIRouter

from app.db import connect
from app.services.gate import today_str

router = APIRouter(tags=["总览"])


@router.get("/总览")
def dashboard():
    conn = connect()
    try:
        def cnt(status: str) -> int:
            return conn.execute(
                "SELECT COUNT(*) AS c FROM hang_tickets WHERE status=?", (status,)
            ).fetchone()["c"]

        day = today_str()
        gate = conn.execute("SELECT * FROM day_gates WHERE day=?", (day,)).fetchone()
        wm = conn.execute(
            "SELECT value FROM watermarks WHERE key='在途票'"
        ).fetchone()
        in_flight = conn.execute(
            """SELECT COUNT(*) AS c FROM hang_tickets
               WHERE status NOT IN ('已取','丢失隔离','已并')"""
        ).fetchone()["c"]
        merged_qty = conn.execute(
            """SELECT COALESCE(SUM(child_qty),0) AS q FROM merge_links"""
        ).fetchone()["q"]
        return {
            "今日": day,
            "日闸": "已结案" if gate and gate["sealed"] else "开放",
            "在途水位": wm["value"] if wm else 0,
            "在途票": in_flight,
            "已收": cnt("已收"),
            "水洗": cnt("水洗"),
            "烘干": cnt("烘干"),
            "熨烫": cnt("熨烫"),
            "待取": cnt("待取"),
            "已取": cnt("已取"),
            "超期隔离": cnt("超期隔离"),
            "丢失隔离": cnt("丢失隔离"),
            "已并": cnt("已并"),
            "并票件数": merged_qty,
        }
    finally:
        conn.close()

_dashboard_impl = dashboard


def dashboard():
    return merge_surface.overlay_dashboard(_dashboard_impl())

