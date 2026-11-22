from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.db import connect
from app.services import merge as merge_svc
from app.services import tickets as svc

router = APIRouter(tags=["挂牌"])


class ItemIn(BaseModel):
    garment: str
    qty: int = Field(ge=1)
    remark: str = ""


class TicketIn(BaseModel):
    ticket_no: str
    store_code: str
    pickup_code: str
    customer_name: str
    due_date: str = ""
    note: str = ""
    operator: str = "店员"
    items: list[ItemIn]


class AdvanceIn(BaseModel):
    to_status: str
    operator: str = "店员"
    note: str = ""


class PickupIn(BaseModel):
    pickup_code: str
    operator: str = "店员"


class QuarantineIn(BaseModel):
    kind: str
    reason: str = ""
    operator: str = "店员"


class MergeIn(BaseModel):
    parent_ticket_no: str
    child_ticket_nos: list[str]
    operator: str = "店员"
    note: str = ""


class MergeCodeIn(BaseModel):
    merge_code: str
    operator: str = "店员"


@router.get("/并票")
def list_merges():
    return merge_svc.list_merges()


@router.post("/并票")
def merge(body: MergeIn):
    return merge_svc.merge_tickets(
        body.parent_ticket_no, body.child_ticket_nos, body.operator, body.note
    )


@router.post("/并票/核对")
def reconcile_merges():
    """签账：逐世代重放，不闭合的作废合并码；水位按票籍重算。"""
    return merge_svc.reconcile_merges()


@router.post("/并票/扫码")
def scan_merge_code(body: MergeCodeIn):
    """取衣扫码入口：合并码只在世代重放闭合时可解析。"""
    conn = connect()
    try:
        return merge_svc.resolve_merge_code(conn, body.merge_code)
    finally:
        conn.close()


@router.post("/并票/扫码/取衣")
def scan_pickup(body: MergeCodeIn):
    """取衣扫码入口：凭合并码直接核销，先解析世代、再走同一取衣事务。"""
    conn = connect()
    try:
        audit = merge_svc.resolve_merge_code(conn, body.merge_code)
    finally:
        conn.close()
    return svc.pickup(
        audit["parent_ticket_no"], body.merge_code, body.operator
    )


@router.get("/并票/件键")
def item_ledger():
    """衣物件键列表：逐行核对件挂在哪张票、来源哪张旧票、是否收口。"""
    conn = connect()
    try:
        return merge_svc.item_ledger(conn)
    finally:
        conn.close()


@router.get("/并票/{group_id}")
def get_merge(group_id: str):
    return merge_svc.get_merge(group_id)


@router.get("/并票/{group_id}/核对")
def audit_merge(group_id: str):
    conn = connect()
    try:
        rows = merge_svc.audit_all(conn, only_group=group_id)
        if not rows:
            raise HTTPException(404, "并票世代不存在")
        return rows[0]
    finally:
        conn.close()


@router.get("/挂牌")
def list_tickets(
    status: str | None = Query(None),
    store_code: str | None = Query(None),
):
    return svc.list_tickets(status=status, store_code=store_code)


@router.get("/挂牌/{ticket_no}")
def get_ticket(ticket_no: str):
    return svc.get_ticket(ticket_no)


@router.post("/挂牌")
def create_ticket(body: TicketIn):
    return svc.create_ticket(body.model_dump())


@router.post("/挂牌/{ticket_no}/推进")
def advance(ticket_no: str, body: AdvanceIn):
    return svc.advance(ticket_no, body.to_status, body.operator, body.note)


@router.post("/挂牌/{ticket_no}/取衣")
def pickup(ticket_no: str, body: PickupIn):
    return svc.pickup(ticket_no, body.pickup_code, body.operator)


@router.post("/挂牌/{ticket_no}/隔离")
def quarantine(ticket_no: str, body: QuarantineIn):
    return svc.quarantine(ticket_no, body.kind, body.reason, body.operator)
