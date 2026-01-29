from __future__ import annotations

import datetime as dt
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.deps import get_db, get_current_user
from app.models.user import User
from app.models.project import ProjectBusinessType
from app.models.client import Client
from app.modules.projects.schema import BusinessTypeItem, BusinessTypeCreate, ClientItem, ClientCreate


router = APIRouter(prefix="/projects", tags=["Admin-Projects"])


ROLE_ADMIN_ID = 6


def _require_admin(user: User) -> None:
    rid = getattr(user, "role_id", None)
    try:
        rid_i = int(rid) if rid is not None else None
    except Exception:
        rid_i = None
    if rid_i != ROLE_ADMIN_ID:
        raise HTTPException(status_code=403, detail=f"관리자 권한이 필요합니다. (role_id={rid_i})")


# -----------------------------------------------------------------------------
# 사업종류
# -----------------------------------------------------------------------------
@router.get("/business-types", response_model=List[BusinessTypeItem])
def admin_list_business_types(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    rows = (
        db.query(ProjectBusinessType)
        .filter(ProjectBusinessType.deleted_at.is_(None))
        .order_by(ProjectBusinessType.sort_order.asc(), ProjectBusinessType.id.asc())
        .all()
    )
    return [
        BusinessTypeItem(
            id=int(r.id),
            name=r.name,
            sort_order=int(r.sort_order or 0),
            is_active=bool(r.is_active),
            memo=r.memo,
        )
        for r in rows
    ]


@router.post("/business-types", response_model=BusinessTypeItem)
def admin_create_business_type(
    payload: BusinessTypeCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    now = dt.datetime.utcnow()
    row = ProjectBusinessType(
        name=payload.name.strip(),
        sort_order=payload.sort_order,
        is_active=payload.is_active,
        memo=payload.memo,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return BusinessTypeItem(
        id=int(row.id),
        name=row.name,
        sort_order=int(row.sort_order or 0),
        is_active=bool(row.is_active),
        memo=row.memo,
    )


@router.patch("/business-types/{bt_id}", response_model=BusinessTypeItem)
def admin_patch_business_type(
    bt_id: int,
    payload: BusinessTypeCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    row = db.query(ProjectBusinessType).filter(ProjectBusinessType.id == bt_id, ProjectBusinessType.deleted_at.is_(None)).first()
    if not row:
        raise HTTPException(status_code=404, detail="사업종류를 찾을 수 없습니다.")
    row.name = payload.name.strip()
    row.sort_order = payload.sort_order
    row.is_active = payload.is_active
    row.memo = payload.memo
    row.updated_at = dt.datetime.utcnow()
    db.commit()
    return BusinessTypeItem(
        id=int(row.id),
        name=row.name,
        sort_order=int(row.sort_order or 0),
        is_active=bool(row.is_active),
        memo=row.memo,
    )


@router.delete("/business-types/{bt_id}")
def admin_delete_business_type(
    bt_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    row = db.query(ProjectBusinessType).filter(ProjectBusinessType.id == bt_id, ProjectBusinessType.deleted_at.is_(None)).first()
    if not row:
        raise HTTPException(status_code=404, detail="사업종류를 찾을 수 없습니다.")
    row.deleted_at = dt.datetime.utcnow()
    row.updated_at = dt.datetime.utcnow()
    db.commit()
    return {"ok": True}


# -----------------------------------------------------------------------------
# 발주처(Clients)
# -----------------------------------------------------------------------------
@router.get("/clients", response_model=List[ClientItem])
def admin_list_clients(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    rows = (
        db.query(Client)
        .filter(Client.deleted_at.is_(None))
        .order_by(Client.name.asc(), Client.id.asc())
        .all()
    )
    return [
        ClientItem(
            id=int(r.id),
            name=r.name,
            type=r.type,
            contact_name=r.contact_name,
            contact_phone=r.contact_phone,
            contact_email=r.contact_email,
            memo=r.memo,
        )
        for r in rows
    ]


@router.post("/clients", response_model=ClientItem)
def admin_create_client(
    payload: ClientCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    now = dt.datetime.utcnow()
    row = Client(
        name=payload.name.strip(),
        type=payload.type,
        contact_name=payload.contact_name,
        contact_phone=payload.contact_phone,
        contact_email=payload.contact_email,
        memo=payload.memo,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return ClientItem(
        id=int(row.id),
        name=row.name,
        type=row.type,
        contact_name=row.contact_name,
        contact_phone=row.contact_phone,
        contact_email=row.contact_email,
        memo=row.memo,
    )


@router.patch("/clients/{client_id}", response_model=ClientItem)
def admin_patch_client(
    client_id: int,
    payload: ClientCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    row = db.query(Client).filter(Client.id == client_id, Client.deleted_at.is_(None)).first()
    if not row:
        raise HTTPException(status_code=404, detail="발주처를 찾을 수 없습니다.")
    row.name = payload.name.strip()
    row.type = payload.type
    row.contact_name = payload.contact_name
    row.contact_phone = payload.contact_phone
    row.contact_email = payload.contact_email
    row.memo = payload.memo
    row.updated_at = dt.datetime.utcnow()
    db.commit()
    return ClientItem(
        id=int(row.id),
        name=row.name,
        type=row.type,
        contact_name=row.contact_name,
        contact_phone=row.contact_phone,
        contact_email=row.contact_email,
        memo=row.memo,
    )


@router.delete("/clients/{client_id}")
def admin_delete_client(
    client_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    row = db.query(Client).filter(Client.id == client_id, Client.deleted_at.is_(None)).first()
    if not row:
        raise HTTPException(status_code=404, detail="발주처를 찾을 수 없습니다.")
    row.deleted_at = dt.datetime.utcnow()
    row.updated_at = dt.datetime.utcnow()
    db.commit()
    return {"ok": True}
