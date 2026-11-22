"""假合并收口契约：合并事务世代是唯一真相。

口径（架构二）：
- 合并码、件键迁移、旧票退账、事件、水位、待取墙同一写事务；
- 合并码是否可用，由世代重放（audit）裁决，不信一条裸「成功」标记；
- 差异未清前总览不得计合并成功；签账显式作废合并码；
- 件数与在途守恒，并发合并不丢件、不双挂。
"""

from __future__ import annotations

import importlib
import sys
import threading
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HANGSLIP_DB", str(tmp_path / "test.db"))
    for mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
        sys.modules.pop(mod, None)
    from fastapi.testclient import TestClient

    import app.main as main_mod

    importlib.reload(main_mod)

    from app.db import connect

    conn = connect()
    try:
        for table in (
            "hang_tickets",
            "ticket_items",
            "process_events",
            "pickups",
            "quarantines",
            "ticket_merges",
            "merge_links",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("UPDATE watermarks SET value=0 WHERE key='在途票'")
        conn.commit()
    finally:
        conn.close()

    with TestClient(main_mod.app) as c:
        yield c


def make_ticket(c, no, cust="周小姐", status_path=(), code="码", garment="衬衣", qty=1):
    r = c.post(
        "/api/挂牌",
        json={
            "ticket_no": no,
            "store_code": "店-河西",
            "pickup_code": code,
            "customer_name": cust,
            "items": [{"garment": garment, "qty": qty}],
        },
    )
    assert r.status_code == 200, r.text
    for st in status_path:
        rr = c.post(f"/api/挂牌/{no}/推进", json={"to_status": st, "operator": "工序员"})
        assert rr.status_code == 200, rr.text
    return r.json()


def detail(c, no):
    return c.get(f"/api/挂牌/{no}").json()


def db():
    from app.db import connect

    return connect()


def merge(c, parent, children):
    return c.post(
        "/api/并票",
        json={"parent_ticket_no": parent, "child_ticket_nos": children},
    )


def test_happy_merge_emits_usable_code_and_replays_clean(client):
    make_ticket(client, "父-绿", qty=2)
    make_ticket(client, "子-绿", code="子码", qty=3)
    r = merge(client, "父-绿", ["子-绿"])
    assert r.status_code == 200, r.text
    receipt = r.json()
    assert receipt["merge_ok"] is True
    assert receipt["audit"]["ok"] is True
    assert receipt["merge_code"]

    # 合并码可解析，件数与旧票退账闭合
    scan = client.post("/api/并票/扫码", json={"merge_code": receipt["merge_code"]})
    assert scan.status_code == 200, scan.text
    assert scan.json()["parent_item_qty"] == 5

    parent = detail(client, "父-绿")
    assert parent["item_qty"] == 5
    assert parent["merge_ok"] is True
    assert parent["merge_code"] == receipt["merge_code"]

    child = detail(client, "子-绿")
    assert child["status"] == "已并"
    assert child["items"] == []
    assert child["merge_origin"]["migrated_qty"] == 3
    assert child["merge_origin"]["merge_ok"] is True

    dash = client.get("/api/总览").json()
    assert dash["合并成功"] == 1
    assert dash["合并差异"] == 0
    assert dash["并票件数"] == 3
    assert dash["件键闭合"] is True
    assert dash["水位闭合"] is True


def test_fixture_code_green_but_items_still_stray(client):
    """夹具一：合并码绿、但件键仍散挂在旧票。"""
    make_ticket(client, "父-散", qty=2)
    make_ticket(client, "子-散", qty=3)
    receipt = merge(client, "父-散", ["子-散"]).json()
    code = receipt["merge_code"]

    # 伪造撕裂：把子票的件从父票挪回子票，制造「码已发、键散挂」。
    conn = db()
    try:
        conn.execute(
            "UPDATE ticket_items SET ticket_no='子-散' WHERE ticket_no='父-散' AND origin_ticket_no='子-散'"
        )
        conn.commit()
    finally:
        conn.close()

    # 合并码立即不可用：重放不闭合，不凭历史成功标记放行。
    scan = client.post("/api/并票/扫码", json={"merge_code": code})
    assert scan.status_code == 409
    assert "件数未迁" in scan.json()["detail"] or "散挂" in scan.json()["detail"]

    dash = client.get("/api/总览").json()
    assert dash["合并成功"] == 0
    assert dash["合并差异"] == 1
    assert dash["件键闭合"] is False
    assert dash["散挂件"] == 3
    # 待取墙/详情同口径：父票不再宣称合并成功
    assert detail(client, "父-散")["merge_ok"] is False

    # 签账作废后码被拒，差异转入作废、不计成功
    rec = client.post("/api/并票/核对").json()
    assert rec["作废数"] == 1
    assert rec["件键闭合"] is False  # 作废只收口码，不掩盖物理散挂
    scan2 = client.post("/api/并票/扫码", json={"merge_code": code})
    assert scan2.status_code == 409
    assert "作废" in scan2.json()["detail"]
    dash2 = client.get("/api/总览").json()
    assert dash2["合并成功"] == 0
    assert dash2["合并差异"] == 0
    assert dash2["合并作废"] == 1


def test_fixture_child_refund_qty_not_migrated(client):
    """夹具二：旧票退件数与实际迁入件数不符。"""
    make_ticket(client, "父-差", qty=1)
    make_ticket(client, "子-差", qty=4)
    receipt = merge(client, "父-差", ["子-差"]).json()

    conn = db()
    try:
        # 退账写着 4 件，实际只有 3 件迁入（模拟少迁一件）。
        conn.execute("DELETE FROM ticket_items WHERE ticket_no='父-差' AND origin_ticket_no='子-差' AND id=(SELECT MIN(id) FROM ticket_items WHERE ticket_no='父-差' AND origin_ticket_no='子-差')")
        conn.execute(
            "UPDATE ticket_items SET qty=qty-1 WHERE ticket_no='父-差' AND origin_ticket_no='子-差'"
        )
        conn.commit()
    finally:
        conn.close()

    scan = client.post("/api/并票/扫码", json={"merge_code": receipt["merge_code"]})
    assert scan.status_code == 409
    assert "件数未迁" in scan.json()["detail"]

    audit = client.get(f"/api/并票/{receipt['group_id']}/核对").json()
    assert audit["ok"] is False
    assert any("件数未迁" in i for i in audit["issues"])
    assert client.get("/api/总览").json()["合并成功"] == 0


def test_fixture_half_merge_code_sent_items_not_migrated(client):
    """夹具三：半截合并——码已发、件键未迁、旧票未退在途。"""
    make_ticket(client, "父-半", qty=1)
    make_ticket(client, "子-半", qty=2)
    receipt = merge(client, "父-半", ["子-半"]).json()

    # 模拟旧实现：回执/码保留，件搬回、子票恢复在途，但水位仍停在合并后值
    # （即「旧票在途未退」，水位与票籍各认各的）。
    conn = db()
    try:
        conn.execute(
            "UPDATE ticket_items SET ticket_no='子-半' WHERE ticket_no='父-半' AND origin_ticket_no='子-半'"
        )
        conn.execute(
            "UPDATE hang_tickets SET status='已收', closed_at=NULL, merged_into=NULL WHERE ticket_no='子-半'"
        )
        conn.commit()
    finally:
        conn.close()

    dash = client.get("/api/总览").json()
    assert dash["合并成功"] == 0
    assert dash["合并差异"] == 1
    assert dash["水位闭合"] is False  # 水位与票籍各认各的

    scan = client.post("/api/并票/扫码", json={"merge_code": receipt["merge_code"]})
    assert scan.status_code == 409
    assert "未退在途" in scan.json()["detail"] or "未关账" in scan.json()["detail"]

    # 签账：作废码，水位按票籍重算回闭
    rec = client.post("/api/并票/核对").json()
    assert rec["作废数"] == 1
    assert rec["水位修正"] is True
    dash2 = client.get("/api/总览").json()
    assert dash2["水位闭合"] is True
    assert dash2["合并成功"] == 0


def test_concurrent_merge_no_lost_or_double_items(client):
    """并发两人合同一批旧票：恰好一个成功，件不丢不双挂。"""
    make_ticket(client, "父-A", qty=1)
    make_ticket(client, "父-B", qty=1)
    make_ticket(client, "子-C", qty=4)

    results = []

    def run(parent):
        results.append(merge(client, parent, ["子-C"]).status_code)

    threads = [threading.Thread(target=run, args=(p,)) for p in ("父-A", "父-B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [200, 409]
    dash = client.get("/api/总览").json()
    assert dash["件数合计"] == 6  # 1+1+4 一件不丢
    assert dash["双挂件"] == 0
    assert dash["散挂件"] == 0
    assert dash["合并成功"] == 1
    assert dash["合并差异"] == 0
    assert detail(client, "子-C")["status"] == "已并"


def test_torn_merge_blocks_child_code_pickup_too(client):
    """差异未清时，旧票暗码也不能替合并码放行（否则等于绕过账实核对）。"""
    make_ticket(client, "父-暗", code="父码", qty=1)
    make_ticket(client, "子-暗", code="子码", qty=2)
    merge(client, "父-暗", ["子-暗"])
    client.post("/api/挂牌/父-暗/推进", json={"to_status": "待取"})

    conn = db()
    try:
        conn.execute(
            "UPDATE ticket_items SET ticket_no='子-暗' WHERE ticket_no='父-暗' AND origin_ticket_no='子-暗'"
        )
        conn.commit()
    finally:
        conn.close()

    r = client.post("/api/挂牌/父-暗/取衣", json={"pickup_code": "子码"})
    assert r.status_code == 403
    assert detail(client, "父-暗")["status"] == "待取"


def test_second_merge_into_same_parent_still_replays_both(client):
    """同一父票先后并入两批子票：两条回执都必须长期可重放，不得互相污染。"""
    make_ticket(client, "父-多", qty=1)
    make_ticket(client, "子-多1", qty=2)
    make_ticket(client, "子-多2", qty=3)
    r1 = merge(client, "父-多", ["子-多1"]).json()
    r2 = merge(client, "父-多", ["子-多2"]).json()
    assert r1["merge_ok"] and r2["merge_ok"]

    # 两条回执各自的「并入前件数」不同，且都能独立闭合。
    assert r1["parent_qty_before"] == 1
    assert r2["parent_qty_before"] == 3
    for gid in (r1["group_id"], r2["group_id"]):
        a = client.get(f"/api/并票/{gid}/核对").json()
        assert a["ok"] is True, a["issues"]
    for code in (r1["merge_code"], r2["merge_code"]):
        assert (
            client.post("/api/并票/扫码", json={"merge_code": code}).status_code
            == 200
        )
    assert detail(client, "父-多")["item_qty"] == 6
    dash = client.get("/api/总览").json()
    assert dash["合并成功"] == 2
    assert dash["合并差异"] == 0
    assert dash["件键闭合"] is True


def test_torn_second_generation_is_pinned_but_blocks_whole_parent(client):
    """后一代撕裂时，差异必须精确指认到该代；但父票是单一取衣单元，
    件数短缺时整组合并码都不得放行（不能部分交付）。"""
    make_ticket(client, "父-序", qty=1)
    make_ticket(client, "子-序1", qty=2)
    make_ticket(client, "子-序2", qty=3)
    r1 = merge(client, "父-序", ["子-序1"]).json()
    r2 = merge(client, "父-序", ["子-序2"]).json()

    conn = db()
    try:
        conn.execute(
            "UPDATE ticket_items SET ticket_no='子-序2' WHERE ticket_no='父-序' AND origin_ticket_no='子-序2'"
        )
        conn.commit()
    finally:
        conn.close()

    a1 = client.get(f"/api/并票/{r1['group_id']}/核对").json()
    a2 = client.get(f"/api/并票/{r2['group_id']}/核对").json()
    # 精确指认：只有撕裂的那一代带「件数未迁」。
    assert a1["ok"] is False and a2["ok"] is False
    assert any("件数未迁" in i for i in a2["issues"])
    assert not any("件数未迁" in i for i in a1["issues"])
    # 父票账短，整组码都停用，不能只交付没出问题的那一代。
    for code in (r1["merge_code"], r2["merge_code"]):
        assert (
            client.post("/api/并票/扫码", json={"merge_code": code}).status_code
            == 409
        )
    dash = client.get("/api/总览").json()
    assert dash["合并成功"] == 0
    assert dash["件键闭合"] is False


def test_item_ledger_flags_stray_rows(client):
    """件键台账与合并码同源：散挂/双挂行必须被逐行指认。"""
    make_ticket(client, "父-账", qty=1)
    make_ticket(client, "子-账", qty=2)
    merge(client, "父-账", ["子-账"])

    ok = client.get("/api/并票/件键").json()
    assert ok["件键闭合"] is True
    assert ok["件数合计"] == 3
    assert ok["未收口行"] == 0

    conn = db()
    try:
        conn.execute(
            "UPDATE ticket_items SET ticket_no='子-账' WHERE ticket_no='父-账' AND origin_ticket_no='子-账'"
        )
        conn.commit()
    finally:
        conn.close()

    torn = client.get("/api/并票/件键").json()
    assert torn["件键闭合"] is False
    assert torn["未收口行"] == 1
    row = [r for r in torn["rows"] if r["ticket_no"] == "子-账"][0]
    assert any("散挂" in i for i in row["issues"])


def test_merge_code_can_open_merged_ticket(client):
    """合并码就是取衣入口，可在待取墙用合并码核销。"""
    make_ticket(client, "父-取", code="父码", qty=1)
    make_ticket(client, "子-取", code="子码", qty=2)
    receipt = merge(client, "父-取", ["子-取"]).json()
    client.post("/api/挂牌/父-取/推进", json={"to_status": "待取"})

    r = client.post(
        "/api/挂牌/父-取/取衣", json={"pickup_code": receipt["merge_code"]}
    )
    assert r.status_code == 200, r.text
    assert detail(client, "父-取")["status"] == "已取"
    dash = client.get("/api/总览").json()
    assert dash["在途水位"] == 0
    assert dash["水位闭合"] is True
