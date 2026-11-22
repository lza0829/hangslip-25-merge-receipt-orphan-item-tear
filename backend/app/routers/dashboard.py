from __future__ import annotations

from fastapi import APIRouter

from app.db import connect
from app.services import merge as merge_svc
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
        merge = merge_svc.summary(conn)
        integrity = merge_svc.item_integrity(conn)
        return {
            "今日": day,
            "日闸": "已结案" if gate and gate["sealed"] else "开放",
            "在途水位": wm["value"] if wm else 0,
            "在途票": in_flight,
            "件数合计": integrity["件数合计"],
            "散挂件": integrity["散挂件"],
            "孤儿件": integrity["孤儿件"],
            "双挂件": integrity["双挂件"],
            "件键闭合": integrity["件键闭合"],
            "水位闭合": (wm["value"] if wm else 0) == in_flight,
            "已收": cnt("已收"),
            "水洗": cnt("水洗"),
            "烘干": cnt("烘干"),
            "熨烫": cnt("熨烫"),
            "待取": cnt("待取"),
            "已取": cnt("已取"),
            "超期隔离": cnt("超期隔离"),
            "丢失隔离": cnt("丢失隔离"),
            "已并": cnt("已并"),
            # 只计入世代重放闭合的并票；差异未清不计成功、不计件数。
            "并票件数": merge["并票件数"],
            "并票父票件数": merge["并票父票件数"],
            "合并码数": merge["合并码数"],
            "合并成功": merge["合并成功"],
            "合并差异": merge["合并差异"],
            "合并作废": merge["合并作废"],
            "合并差异明细": merge["差异明细"],
            "合并作废明细": merge["作废明细"],
            "合并口径": "合并事务世代",
        }
    finally:
        conn.close()
