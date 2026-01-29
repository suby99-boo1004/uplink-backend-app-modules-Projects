from __future__ import annotations

import datetime as dt
from typing import Optional, List, Literal

from pydantic import BaseModel, Field, field_validator


ProjectStatus = Literal["PLANNING", "IN_PROGRESS", "ON_HOLD", "DONE", "CLOSED"]


class ProjectListItem(BaseModel):
    id: int
    name: str
    status: ProjectStatus
    department_id: Optional[int] = None
    client_id: int
    client_name: str
    business_type_id: Optional[int] = None
    business_type_name: Optional[str] = None
    start_date: Optional[dt.date] = None
    due_date: Optional[dt.date] = None
    end_date: Optional[dt.date] = None
    has_unread_update: bool = False
    updated_at: dt.datetime


class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1)
    client_id: Optional[int] = None
    client_name: Optional[str] = None  # 수기 발주처 지원
    department_id: Optional[int] = None
    business_type_id: Optional[int] = None
    pm_user_id: Optional[int] = None
    status: ProjectStatus = "PLANNING"
    start_date: Optional[dt.date] = None
    due_date: Optional[dt.date] = None
    end_date: Optional[dt.date] = None
    contract_amount: Optional[float] = None
    memo: Optional[str] = None

class ProjectUpdatePatch(BaseModel):
    name: Optional[str] = None
    client_id: Optional[int] = None
    department_id: Optional[int] = None
    business_type_id: Optional[int] = None
    pm_user_id: Optional[int] = None
    status: Optional[ProjectStatus] = None
    start_date: Optional[dt.date] = None
    due_date: Optional[dt.date] = None
    end_date: Optional[dt.date] = None
    contract_amount: Optional[float] = None
    memo: Optional[str] = None


class ProjectDetail(BaseModel):
    id: int
    name: str
    status: ProjectStatus
    department_id: Optional[int] = None
    department_name: Optional[str] = None
    client_id: int
    client_name: str
    business_type_id: Optional[int] = None
    business_type_name: Optional[str] = None
    pm_user_id: Optional[int] = None
    pm_user_name: Optional[str] = None
    start_date: Optional[dt.date] = None
    due_date: Optional[dt.date] = None
    end_date: Optional[dt.date] = None
    contract_amount: Optional[float] = None
    memo: Optional[str] = None
    has_unread_update: bool = False
    admin_last_seen_at: Optional[dt.datetime] = None
    admin_last_seen_by: Optional[int] = None
    created_at: dt.datetime
    updated_at: dt.datetime


class ProjectUpdateItem(BaseModel):
    id: int
    content: str
    department_id: Optional[int] = None
    department_name: Optional[str] = None
    created_by: int
    created_by_name: str
    created_at: dt.datetime


class ProjectUpdateCreate(BaseModel):
    content: str = Field(..., min_length=1)


class AdminAckResponse(BaseModel):
    ok: bool = True
    has_unread_update: bool
    admin_last_seen_at: dt.datetime


class EvaluationItem(BaseModel):
    user_id: int
    score: float = Field(..., ge=0, le=10)
    comment: Optional[str] = None


class EvaluationSaveRequest(BaseModel):
    items: List[EvaluationItem] = Field(default_factory=list)

    @field_validator("items")
    @classmethod
    def _validate_sum_10(cls, v: List[EvaluationItem]):
        total = 0.0
        for it in v:
            total += float(it.score)
        # 소수 첫째자리 허용 → 10.0 ± 0.05
        if abs(total - 10.0) > 0.05:
            raise ValueError(f"완료평가 합계는 10점이어야 합니다. (현재: {total})")
        return v


class BusinessTypeItem(BaseModel):
    id: int
    name: str
    sort_order: int = 0
    is_active: bool = True
    memo: Optional[str] = None


class BusinessTypeCreate(BaseModel):
    name: str = Field(..., min_length=1)
    sort_order: int = 0
    is_active: bool = True
    memo: Optional[str] = None


class ClientItem(BaseModel):
    id: int
    name: str
    type: str
    contact_name: Optional[str] = None
    contact_phone: Optional[str] = None
    contact_email: Optional[str] = None
    memo: Optional[str] = None


class ClientCreate(BaseModel):
    name: str = Field(..., min_length=1)
    type: str = "client"
    contact_name: Optional[str] = None
    contact_phone: Optional[str] = None
    contact_email: Optional[str] = None
    memo: Optional[str] = None


class DepartmentItem(BaseModel):
    id: int
    name: str
    code: Optional[str] = None
    sort_order: int = 0
    in_progress_count: int = 0
