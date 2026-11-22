"""并票（吞并式）——合并事务世代为唯一真相。

全局口径只此一套，任何入口都听它：
- 合并码、衣物件键迁移、旧票退账（子票关「已并」退出在途）、事件、水位、
  待取墙由同一个写事务一次落账；中途失败整体回滚，不存在「码已发、件未迁」。
- 合并码不是独立凭证，而是该世代（group_id）的取衣入口；能否使用必须由
  世代重放核对（audit）裁决，不能凭一条「成功」标记。
- 重放核对：件键全部在父票名下且来源和等于子票退账件数、子票关账、水位
  增量等于子票张数、无散挂/双挂。差异未清的世代一律作废合并码，且总览
  不得计合并成功。
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime

from fastapi import HTTPException

from app.db import STAGE_RANK, connect, transaction
from app.services.gate import (
    count_in_flight,
    ensure_day_row,
    refresh_in_flight,
)

# 只有仍在工序上的票可并；已取/隔离/已并都是终态。
MERGEABLE = tuple(STAGE_RANK)

# 终止态世代不可再当取衣凭证。
VOID = "void"
COMMITTED = "committed"


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def _qty(conn, ticket_no: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(qty),0) AS q FROM ticket_items WHERE ticket_no=?",
        (ticket_no,),
    ).fetchone()
    return int(row["q"])


def _origin_qty(conn, ticket_no: str, origin: str) -> int:
    row = conn.execute(
        """SELECT COALESCE(SUM(qty),0) AS q FROM ticket_items
           WHERE ticket_no=? AND COALESCE(origin_ticket_no, ticket_no)=?""",
        (ticket_no, origin),
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


def _receipt_row(conn, group_id: str):
    return conn.execute(
        "SELECT * FROM ticket_merges WHERE group_id=?", (group_id,)
    ).fetchone()


def _links(conn, group_id: str) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM merge_links WHERE group_id=? ORDER BY id",
            (group_id,),
        ).fetchall()
    ]


def audit_group(conn, receipt) -> dict:
    """按世代重放核对一张并票回执；不写库，只给差异清单。

    这是「假合并收口」的唯一裁决口：只要重放不闭合，合并码即不可用、
    总览不计成功。差异不会被读取动作悄悄抹平，必须显式签账作废。
    """
    r = dict(receipt)
    gid = r["group_id"]
    parent = r["parent_ticket_no"]
    issues: list[str] = []

    if r.get("state") != COMMITTED:
        issues.append(f"并票世代已{r.get('state') or '作废'}，合并码不可用")

    row = conn.execute(
        "SELECT * FROM hang_tickets WHERE ticket_no=?", (parent,)
    ).fetchone()
    if not row:
        issues.append(f"父票不存在：{parent}")
        links = _links(conn, gid)
        return _audit_payload(r, issues, links, 0, "")

    if row["merged_into"]:
        issues.append(f"父票已并入「{row['merged_into']}」，不可作合并码")

    if not (r.get("merge_code") or "").strip():
        issues.append("世代缺合并码")

    links = _links(conn, gid)
    if not links:
        issues.append("世代缺子票关联")
    if r.get("child_count") != len(links):
        issues.append(
            f"回执子票张数 {r.get('child_count')} 与关联 {len(links)} 不符"
        )

    child_ids = [l["child_ticket_no"] for l in links]
    expected_added = 0
    for l in links:
        child = l["child_ticket_no"]
        cri = conn.execute(
            "SELECT * FROM hang_tickets WHERE ticket_no=?", (child,)
        ).fetchone()
        if not cri:
            issues.append(f"子票不存在：{child}")
            continue
        if cri["status"] != "已并":
            issues.append(f"子票「{child}」未退在途：当前态「{cri['status']}」")
        if cri["merged_into"] != parent:
            issues.append(
                f"子票「{child}」票籍未指向父票（现指向「{cri['merged_into'] or '无'}」）"
            )
        if not cri["closed_at"]:
            issues.append(f"子票「{child}」未关账")
        if _qty(conn, child) != 0:
            issues.append(f"子票「{child}」仍挂着衣物件：{_qty(conn, child)} 件")

        migrated = _origin_qty(conn, parent, child)
        declared = int(l["child_qty"])
        if int(l.get("migrated_qty") or 0) != declared:
            issues.append(
                f"子票「{child}」件键迁移记录 {l.get('migrated_qty')} 与退账 {declared} 不符"
            )
        if migrated != declared:
            issues.append(
                f"子票「{child}」件数未迁：父票名下仅 {migrated} 件，退账 {declared} 件"
            )
        expected_added += declared

        ev = conn.execute(
            """SELECT 1 FROM process_events
               WHERE ticket_no=? AND to_status='已并' LIMIT 1""",
            (child,),
        ).fetchone()
        if not ev:
            issues.append(f"子票「{child}」缺退账事件")

    # 散挂/双挂：来自本组任一张票的件必须且只能挂在父票名下。
    if child_ids:
        marks = ",".join("?" for _ in child_ids)
        stray = conn.execute(
            f"""SELECT ticket_no, COUNT(*) AS c FROM ticket_items
                WHERE COALESCE(origin_ticket_no, ticket_no) IN ({marks})
                  AND ticket_no<>?
                GROUP BY ticket_no""",
            (*child_ids, parent),
        ).fetchall()
        for s in stray:
            issues.append(
                f"衣物件散挂在「{s['ticket_no']}」：{s['c']} 行未归父票「{parent}」"
            )

    parent_own = _origin_qty(conn, parent, parent)
    parent_total = _qty(conn, parent)

    # 同一父票可能先后并入多批子票，因此按世代签发顺序逐条累加重放：
    # 本代「并入前件数」= 父票自留 + 此前各代迁入之和。该式完全由持久化
    # 数据算出，不依赖当前父票总数，任一代漏迁都会被单独指认。
    gens = conn.execute(
        "SELECT * FROM ticket_merges WHERE parent_ticket_no=? ORDER BY rowid",
        (parent,),
    ).fetchall()
    prior_added = 0
    all_added = 0
    seen_self = False
    for g in gens:
        if g["group_id"] == gid:
            seen_self = True
        elif not seen_self:
            prior_added += int(g["added_qty"] or 0)
        all_added += int(g["added_qty"] or 0)
    if not seen_self:
        all_added += int(r.get("added_qty") or 0)
    expected_before = parent_own + prior_added
    receipt_before = int(r.get("parent_qty_before") or 0)
    if receipt_before != expected_before:
        issues.append(
            f"回执并入前件数 {receipt_before} 与重放 {expected_before} 不符"
        )
    if int(r.get("item_qty") or 0) != receipt_before + int(r.get("added_qty") or 0):
        issues.append(
            f"回执件数 {r.get('item_qty')} ≠ 并入前 {receipt_before} + 迁入 {r.get('added_qty')}"
        )
    if r.get("added_qty") != expected_added:
        issues.append(f"回执迁入件数 {r.get('added_qty')} 与退账合计 {expected_added} 不符")
    # 父票总账：自留 + 各代迁入，必须等于现挂件数。
    if parent_total != parent_own + all_added:
        issues.append(
            f"父票件数账实不符：现有 {parent_total} 件，重放应为 "
            f"自留 {parent_own} + 各代迁入 {all_added}"
        )
    if r.get("watermark_before", 0) - r.get("watermark_after", 0) != len(links):
        issues.append(
            f"水位增量 {r.get('watermark_before')}→{r.get('watermark_after')} "
            f"与子票 {len(links)} 张不符"
        )
    return _audit_payload(r, issues, links, parent_total, row["status"])


def _audit_payload(receipt: dict, issues: list[str], links: list[dict], parent_qty: int,
                   parent_status: str = "") -> dict:
    out = {
        "group_id": receipt.get("group_id"),
        "merge_code": receipt.get("merge_code") or "",
        "parent_ticket_no": receipt.get("parent_ticket_no"),
        "parent_status": parent_status,
        "state": receipt.get("state") or COMMITTED,
        "ok": not issues and receipt.get("state", COMMITTED) == COMMITTED,
        "issues": issues,
        "parent_item_qty": parent_qty,
        "links": links,
    }
    return out


def audit_all(conn, only_group: str | None = None) -> list[dict]:
    sql = "SELECT * FROM ticket_merges"
    args: list = []
    if only_group:
        sql += " WHERE group_id=?"
        args.append(only_group)
    sql += " ORDER BY merged_at DESC, rowid DESC"
    return [audit_group(conn, r) for r in conn.execute(sql, args).fetchall()]


def receipts(conn) -> list[dict]:
    """回执列表：每条都带世代重放结论，前端不再信一条裸「成功」标记。"""
    out = []
    for r in conn.execute(
        "SELECT * FROM ticket_merges ORDER BY merged_at DESC, rowid DESC"
    ).fetchall():
        item = dict(r)
        audit = audit_group(conn, r)
        item["links"] = audit["links"]
        item["audit"] = audit
        item["merge_ok"] = audit["ok"]
        item["merge_issues"] = audit["issues"]
        out.append(item)
    return out


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

            # 并发并票：同一子票只能被吞一次，且子票自身不能已是另一组之父。
            # BEGIN IMMEDIATE 已把写串行化，这里的检查与写入同事务。
            for no in children:
                hit = conn.execute(
                    "SELECT parent_ticket_no FROM merge_links WHERE child_ticket_no=?",
                    (no,),
                ).fetchone()
                if hit:
                    raise HTTPException(
                        409, f"「{no}」已并入「{hit['parent_ticket_no']}」"
                    )
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
            stamp = now.replace("-", "").replace(":", "").replace(" ", "")
            group_id = f"并-{stamp}-{uuid.uuid4().hex[:4]}"
            merge_code = group_id  # 合并取衣码即世代号，不另设可漂移的凭证

            own_before = _qty(conn, parent_ticket_no)
            child_qty_total = 0
            for kid in kids:
                no = kid["ticket_no"]
                qty = _qty(conn, no)
                child_qty_total += qty
                # 件键整行迁移（origin 保留来源）。与退账、关联、事件同事务。
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
                        child_qty, child_pickup_code, merged_at, migrated_qty)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        group_id,
                        parent_ticket_no,
                        no,
                        kid["status"],
                        qty,
                        kid["pickup_code"],
                        now,
                        qty,
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
                        f"并入选票 {parent_ticket_no}（{group_id}），件键迁出 {qty} 件",
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
                    f"并票世代 {group_id}：并入 "
                    + "、".join(children)
                    + f"，件键迁入 {child_qty_total} 件，合并码 {merge_code}",
                ),
            )

            after = refresh_in_flight(conn)
            parent_total = _qty(conn, parent_ticket_no)
            conn.execute(
                """INSERT INTO ticket_merges
                   (group_id, parent_ticket_no, parent_status, child_count, item_qty,
                    added_qty, watermark_before, watermark_after, parent_qty_before,
                    operator, merged_at, note, merge_code, state)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    group_id,
                    parent_ticket_no,
                    merged_status,
                    len(children),
                    parent_total,
                    child_qty_total,
                    before,
                    after,
                    own_before,
                    operator or "店员",
                    now,
                    note or "",
                    merge_code,
                    COMMITTED,
                ),
            )

            # 世代自证：任何一条不闭合都整体回滚，绝不留下半截账。
            if before - after != len(children):
                raise HTTPException(500, "并票水位不守恒，已回滚")
            if parent_total != own_before + child_qty_total:
                raise HTTPException(500, "并票件数不守恒，已回滚")
            for kid in kids:
                if _qty(conn, kid["ticket_no"]) != 0:
                    raise HTTPException(500, "并票子票仍挂件，已回滚")

            receipt = dict(_receipt_row(conn, group_id))
            audit = audit_group(conn, receipt)
            if not audit["ok"]:
                raise HTTPException(500, "并票世代重放不闭合，已回滚")
            receipt["links"] = audit["links"]
            receipt["audit"] = audit
            receipt["merge_ok"] = True
            receipt["merge_issues"] = []
    except sqlite3.OperationalError as e:
        raise HTTPException(409, f"并票写入冲突，请重试：{e}") from e
    return receipt


def list_merges() -> list[dict]:
    conn = connect()
    try:
        return receipts(conn)
    finally:
        conn.close()


def get_merge(group_id: str) -> dict:
    conn = connect()
    try:
        row = _receipt_row(conn, group_id)
        if not row:
            raise HTTPException(404, "并票世代不存在")
        out = dict(row)
        audit = audit_group(conn, row)
        out["links"] = audit["links"]
        out["audit"] = audit
        out["merge_ok"] = audit["ok"]
        out["merge_issues"] = audit["issues"]
        out["parent_item_qty"] = audit["parent_item_qty"]
        out["parent"] = dict(
            conn.execute(
                "SELECT * FROM hang_tickets WHERE ticket_no=?",
                (out["parent_ticket_no"],),
            ).fetchone()
            or {}
        )
        out["items"] = [
            dict(i)
            for i in conn.execute(
                "SELECT * FROM ticket_items WHERE ticket_no=? ORDER BY id",
                (out["parent_ticket_no"],),
            ).fetchall()
        ]
        out["events"] = [
            dict(e)
            for e in conn.execute(
                """SELECT * FROM process_events
                   WHERE ticket_no=? OR note LIKE ?
                   ORDER BY id""",
                (out["parent_ticket_no"], f"%{group_id}%"),
            ).fetchall()
        ]
        return out
    finally:
        conn.close()


def resolve_merge_code(conn, code: str) -> dict:
    """合并码 → 世代。未签发、已作废、重放不闭合都拒绝，绝不「码绿账假」。"""
    code = (code or "").strip()
    if not code:
        raise HTTPException(400, "合并码必填")
    row = conn.execute(
        "SELECT * FROM ticket_merges WHERE merge_code=? OR group_id=? ORDER BY merged_at DESC LIMIT 1",
        (code, code),
    ).fetchone()
    if not row:
        raise HTTPException(404, "合并码不存在")
    if row["state"] != COMMITTED:
        raise HTTPException(409, f"合并码已作废（{row['state']}），不可取衣")
    audit = audit_group(conn, row)
    if not audit["ok"]:
        raise HTTPException(
            409,
            "合并码差异未清，不可取衣：" + "；".join(audit["issues"]),
        )
    parent = conn.execute(
        "SELECT * FROM hang_tickets WHERE ticket_no=?", (row["parent_ticket_no"],)
    ).fetchone()
    if not parent:
        raise HTTPException(409, "合并码所属父票不存在")
    # 扫码/取衣入口都先能解析出世代；能否核销由取衣服务再按「待取」把关，
    # 这里不把「不在待取」当成码作废，否则会把合法码误伤成假合并。
    return audit


def group_pickup_codes(conn, parent_ticket_no: str) -> set[str]:
    """父票自身暗码之外，被吞子票的暗码仍可开这一组（同一顾客）。

    但只认世代重放闭合的组：差异未清的组，旧票暗码不能当合并成功的凭证，
    否则等于绕过合并码的账实核对把件放走。
    """
    ok_groups = {
        r["group_id"]
        for r in conn.execute(
            "SELECT * FROM ticket_merges WHERE parent_ticket_no=? AND state=?",
            (parent_ticket_no, COMMITTED),
        ).fetchall()
        if audit_group(conn, r)["ok"]
    }
    if not ok_groups:
        return set()
    marks = ",".join("?" for _ in ok_groups)
    rows = conn.execute(
        f"""SELECT child_pickup_code FROM merge_links
            WHERE parent_ticket_no=? AND group_id IN ({marks})""",
        (parent_ticket_no, *ok_groups),
    ).fetchall()
    return {r["child_pickup_code"] for r in rows if r["child_pickup_code"]}


def group_merge_codes(conn, parent_ticket_no: str) -> set[str]:
    """该父票名下仍可用的合并取衣码（只认重放闭合的世代）。"""
    codes: set[str] = set()
    for r in conn.execute(
        "SELECT * FROM ticket_merges WHERE parent_ticket_no=? AND state=?",
        (parent_ticket_no, COMMITTED),
    ).fetchall():
        if r["merge_code"] and audit_group(conn, r)["ok"]:
            codes.add(r["merge_code"])
    return codes


def item_integrity(conn) -> dict:
    """全局件键体检：散挂在已并子票、无主孤儿、来源双挂的行数。"""
    stray = conn.execute(
        """SELECT COALESCE(SUM(qty),0) AS q, COUNT(*) AS c FROM ticket_items
           WHERE ticket_no IN (
             SELECT ticket_no FROM hang_tickets
             WHERE status='已并' OR merged_into IS NOT NULL
           )"""
    ).fetchone()
    orphan = conn.execute(
        """SELECT COALESCE(SUM(qty),0) AS q, COUNT(*) AS c FROM ticket_items
           WHERE ticket_no NOT IN (SELECT ticket_no FROM hang_tickets)"""
    ).fetchone()
    double = conn.execute(
        """SELECT COALESCE(SUM(qty),0) AS q, COUNT(*) AS c FROM ticket_items i
           WHERE COALESCE(origin_ticket_no, ticket_no) <> ticket_no
             AND EXISTS (
               SELECT 1 FROM ticket_items j
               WHERE COALESCE(j.origin_ticket_no, j.ticket_no)
                     = COALESCE(i.origin_ticket_no, i.ticket_no)
                 AND j.ticket_no <> i.ticket_no
             )"""
    ).fetchone()
    total = conn.execute(
        "SELECT COALESCE(SUM(qty),0) AS q FROM ticket_items"
    ).fetchone()["q"]
    return {
        "件数合计": int(total),
        "散挂件": int(stray["q"]),
        "散挂行": int(stray["c"]),
        "孤儿件": int(orphan["q"]),
        "双挂件": int(double["q"]),
        "件键闭合": int(stray["q"]) == 0 and int(orphan["q"]) == 0 and int(double["q"]) == 0,
    }


def item_ledger(conn) -> dict:
    """衣物件键台账：每行件挂在哪个现行票、来源哪张旧票、是否已收口。

    与合并码/水位/待取墙同源读库，供前端「衣物件列表」逐行核对。
    """
    rows = []
    for r in conn.execute(
        """SELECT i.id, i.ticket_no, COALESCE(i.origin_ticket_no, i.ticket_no)
                  AS origin_ticket_no, i.garment, i.qty, i.remark,
                  t.status AS ticket_status, t.merged_into
           FROM ticket_items i
           LEFT JOIN hang_tickets t ON t.ticket_no = i.ticket_no
           ORDER BY i.ticket_no, i.id"""
    ).fetchall():
        d = dict(r)
        issues = []
        if d["ticket_status"] is None:
            issues.append("孤儿件：票籍不存在")
        elif d["ticket_status"] == "已并" or d["merged_into"]:
            issues.append("散挂：件仍挂在已退账的旧票")
        dup = conn.execute(
            """SELECT COUNT(*) AS c FROM ticket_items j
               WHERE COALESCE(j.origin_ticket_no, j.ticket_no)=?
                 AND j.ticket_no<>?""",
            (d["origin_ticket_no"], d["ticket_no"]),
        ).fetchone()["c"]
        if dup:
            issues.append(f"双挂：同来源件另挂在 {dup} 张票")
        d["issues"] = issues
        d["ok"] = not issues
        rows.append(d)
    return {
        "rows": rows,
        "件数合计": sum(int(r["qty"]) for r in rows),
        "未收口行": sum(1 for r in rows if r["issues"]),
        "件键闭合": all(r["ok"] for r in rows),
    }


def summary(conn) -> dict:
    """总览口径：差异未清的世代不计成功，也不计入并票件数。

    「合并差异」只数未收口的世代；签账作废后转入「合并作废」，
    两者都不计成功，但差异清单不再挂着已处理的世代。
    """
    audits = audit_all(conn)
    ok = [a for a in audits if a["ok"]]
    voided = [a for a in audits if not a["ok"] and a["state"] == VOID]
    bad = [a for a in audits if not a["ok"] and a["state"] != VOID]
    return {
        "合并码数": len(audits),
        "合并成功": len(ok),
        "合并差异": len(bad),
        "合并作废": len(voided),
        # 并票件数沿用「迁入件数」口径，只计重放闭合的世代。
        "并票件数": sum(int(l["child_qty"]) for a in ok for l in a["links"]),
        "并票父票件数": sum(int(a["parent_item_qty"]) for a in ok),
        "差异明细": [
            {
                "group_id": a["group_id"],
                "merge_code": a["merge_code"],
                "parent_ticket_no": a["parent_ticket_no"],
                "state": a["state"],
                "issues": a["issues"],
            }
            for a in bad[:20]
        ],
        "作废明细": [
            {
                "group_id": a["group_id"],
                "merge_code": a["merge_code"],
                "parent_ticket_no": a["parent_ticket_no"],
                "issues": a["issues"],
            }
            for a in voided[:20]
        ],
    }


def reconcile_merges() -> dict:
    """显式签账：逐世代重放，不闭合的作废合并码；水位按票籍重算。

    这是假合并的收口动作：作废后合并码页/扫码/取衣一律拒绝，总览不计成功。
    """
    voided: list[dict] = []
    with transaction() as conn:
        for r in conn.execute("SELECT * FROM ticket_merges").fetchall():
            audit = audit_group(conn, r)
            if audit["ok"] or r["state"] == VOID:
                continue
            reason = "；".join(audit["issues"])
            note = (r["note"] or "").strip()
            tail = f"签账作废：{reason}"
            new_note = f"{note}；{tail}" if note else tail
            conn.execute(
                "UPDATE ticket_merges SET state=?, note=? WHERE group_id=?",
                (VOID, new_note, r["group_id"]),
            )
            voided.append(
                {
                    "group_id": r["group_id"],
                    "merge_code": r["merge_code"],
                    "parent_ticket_no": r["parent_ticket_no"],
                    "issues": audit["issues"],
                }
            )
        stored = _watermark(conn)
        live = count_in_flight(conn)
        watermark_fixed = stored != live
        if watermark_fixed:
            refresh_in_flight(conn)
        integrity = item_integrity(conn)
        return {
            "作废": voided,
            "作废数": len(voided),
            "水位修正": watermark_fixed,
            "水位前": stored,
            "水位后": live,
            "件键": integrity,
            "件键闭合": integrity["件键闭合"],
            "合并成功": len(
                [a for a in audit_all(conn) if a["ok"]]
            ),
        }
