"""并票（吞并式）契约测试。

口径：并票后现行票籍只认父票。
- 子票关账为「已并」退出在途水位；
- 衣物件整批迁父，子票不留件；
- 父票取最慢态；待取页/取衣/水位/总览只认父票；
- 写路径同事务；并发并票不丢件。
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
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

    # 清掉种子票，避免计数断言被样例数据污染。
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


def test_merge_moves_items_closes_children_and_uses_slowest_status(client):
    make_ticket(client, "父-1", status_path=["水洗"], code="甲")
    make_ticket(client, "子-1", status_path=["水洗", "烘干", "熨烫"], code="乙", qty=2)
    make_ticket(client, "子-2", code="丙", garment="大衣", qty=3)

    before = client.get("/api/总览").json()["在途水位"]
    r = client.post(
        "/api/并票",
        json={
            "parent_ticket_no": "父-1",
            "child_ticket_nos": ["子-1", "子-2"],
            "operator": "店长",
        },
    )
    assert r.status_code == 200, r.text
    receipt = r.json()

    # 回执
    assert receipt["child_count"] == 2
    assert receipt["added_qty"] == 5
    assert receipt["item_qty"] == 6
    assert receipt["watermark_before"] - receipt["watermark_after"] == 2
    assert receipt["parent_status"] == "已收"  # 子-2 最慢为「已收」

    # 父票：件全在，态为最慢，可继续推进
    parent = detail(client, "父-1")
    assert parent["status"] == "已收"
    assert parent["item_qty"] == 6
    assert {i["garment"] for i in parent["items"]} == {"衬衣", "大衣"}
    assert {i["origin_ticket_no"] for i in parent["items"]} == {"父-1", "子-1", "子-2"}
    assert len(parent["children"]) == 2

    # 子票：关账、无件、退出水位、写路径不可再动
    for child in ("子-1", "子-2"):
        d = detail(client, child)
        assert d["status"] == "已并"
        assert d["merged_into"] == "父-1"
        assert d["items"] == []
        assert d["closed_at"]
        assert client.post(
            f"/api/挂牌/{child}/推进", json={"to_status": "待取"}
        ).status_code == 409
        assert client.post(
            f"/api/挂牌/{child}/取衣", json={"pickup_code": "乙"}
        ).status_code == 409

    # 水位与总览：每吞一张子票减一，且与在途票数自洽
    dash = client.get("/api/总览").json()
    assert dash["在途水位"] == before - 2
    assert dash["在途票"] == dash["在途水位"]
    assert dash["已并"] == 2
    assert dash["并票件数"] == 5

    # 守恒：全部件数不因并票增减
    all_items = 0
    for no in ("父-1", "子-1", "子-2"):
        all_items += detail(client, no)["item_qty"]
    assert all_items == 6


def test_merged_parent_pickup_closes_group_and_child_codes_still_open(client):
    make_ticket(client, "父-2", code="父码", qty=1)
    make_ticket(client, "子-3", code="子码", qty=2)
    client.post(
        "/api/并票",
        json={"parent_ticket_no": "父-2", "child_ticket_nos": ["子-3"]},
    )
    client.post("/api/挂牌/父-2/推进", json={"to_status": "待取"})

    # 父票待取，被吞子票已不在待取页
    ready = client.get("/api/挂牌", params={"status": "待取"}).json()
    assert [r["ticket_no"] for r in ready] == ["父-2"]

    # 子票暗码仍可开这一组（同顾客同组）
    r = client.post("/api/挂牌/父-2/取衣", json={"pickup_code": "子码"})
    assert r.status_code == 200, r.text
    assert detail(client, "父-2")["status"] == "已取"

    # 取衣后水位清零，子票仍为已并
    assert client.get("/api/总览").json()["在途水位"] == 0
    assert detail(client, "子-3")["status"] == "已并"


def test_merge_rejects_mixed_customers_and_bad_participants(client):
    make_ticket(client, "甲-1", cust="周小姐")
    make_ticket(client, "乙-1", cust="陈先生")
    make_ticket(client, "已取-1", cust="周小姐", status_path=["待取"])
    client.post("/api/挂牌/已取-1/取衣", json={"pickup_code": "码"})

    bad = client.post(
        "/api/并票", json={"parent_ticket_no": "甲-1", "child_ticket_nos": ["乙-1"]}
    )
    assert bad.status_code == 409
    assert "同一顾客" in bad.json()["detail"]

    closed = client.post(
        "/api/并票", json={"parent_ticket_no": "甲-1", "child_ticket_nos": ["已取-1"]}
    )
    assert closed.status_code == 409

    self_merge = client.post(
        "/api/并票", json={"parent_ticket_no": "甲-1", "child_ticket_nos": ["甲-1"]}
    )
    assert self_merge.status_code == 400

    assert client.post(
        "/api/并票", json={"parent_ticket_no": "甲-1", "child_ticket_nos": []}
    ).status_code == 400


def test_merge_is_atomic_on_failure(client):
    make_ticket(client, "父-3", qty=1)
    make_ticket(client, "子-4", qty=2)
    before_parent = detail(client, "父-3")
    before_child = detail(client, "子-4")
    before_wm = client.get("/api/总览").json()["在途水位"]

    # 件已迁、子票已关账之后让水位/回执写入失败 —— 半截并票必须整体回滚，
    # 不得留下「关联已建、衣物件未迁」或「子票已关、件未进父票」。
    import app.services.merge as merge_mod

    orig = merge_mod.refresh_in_flight
    calls = {"n": 0}

    def boom(conn):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("模拟半截并票")
        return orig(conn)

    merge_mod.refresh_in_flight = boom
    try:
        with pytest.raises(RuntimeError):
            client.post(
                "/api/并票",
                json={"parent_ticket_no": "父-3", "child_ticket_nos": ["子-4"]},
            )
    finally:
        merge_mod.refresh_in_flight = orig

    assert calls["n"] == 2
    after_parent = detail(client, "父-3")
    after_child = detail(client, "子-4")
    assert after_parent["status"] == before_parent["status"]
    assert after_parent["item_qty"] == before_parent["item_qty"] == 1
    assert after_child["status"] == before_child["status"]
    assert after_child["item_qty"] == before_child["item_qty"] == 2
    assert after_child["merged_into"] is None
    assert client.get("/api/总览").json()["在途水位"] == before_wm
    # 无孤立关联、无半截回执
    assert client.get("/api/并票").json() == []


def test_concurrent_merge_of_same_child_loses_no_items(client):
    make_ticket(client, "父-4", qty=1)
    make_ticket(client, "父-5", qty=1)
    make_ticket(client, "子-5", qty=4)

    results = []

    def run(parent):
        results.append(
            client.post(
                "/api/并票",
                json={"parent_ticket_no": parent, "child_ticket_nos": ["子-5"]},
            ).status_code
        )

    threads = [threading.Thread(target=run, args=(p,)) for p in ("父-4", "父-5")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 恰好一个成功，一个冲突；件只进一个父票。
    assert sorted(results) == [200, 409]
    totals = detail(client, "父-4")["item_qty"] + detail(client, "父-5")["item_qty"]
    assert totals == 1 + 1 + 4  # 无丢件
    assert detail(client, "子-5")["status"] == "已并"
    assert client.get("/api/总览").json()["在途票"] == 2
