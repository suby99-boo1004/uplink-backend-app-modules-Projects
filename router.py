from __future__ import annotations

import datetime as dt
import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from app.modules.estimates.service import delete_estimate_with_revisions
from sqlalchemy.orm import Session

from app.core.deps import get_db, get_current_user
from app.models.user import User

# =========================================================
# 프로젝트(부서별업무) Router (STABLE CLEAN)
# - Syntax 안정화 버전 (unmatched ')' 방지)
# - 정책/기능: 기존 유지 + 아래만 추가
#   1) 발주처 수기 입력(client_name) 지원 → clients 자동 생성/number 자동 증가
#   2) 부서 미설정 계정 예외: payload.department_id 허용(등록 막힘 방지)
#   3) 관리자 재무 저장: /{id}/admin-info (finance 테이블 없으면 400 안내)
#   4) 상세조회: finance JOIN 실패 시 fallback (CREATE TABLE 절대 안함)
# =========================================================

router = APIRouter(prefix="/api/projects", tags=["projects"])


# --- project status enum helper (robust across DB enum values) ---
def _get_project_status_enum_values(db):
    """Return available enum labels for Postgres enum type project_status."""
    try:
        rows = db.execute(text("""
            SELECT e.enumlabel
            FROM pg_type t
            JOIN pg_enum e ON t.oid = e.enumtypid
            WHERE t.typname = 'project_status'
            ORDER BY e.enumsortorder
        """)).fetchall()
        return [r[0] for r in rows] if rows else []
    except Exception:
        return []

def _pick_status(db, preferred: list[str], fallback: str | None = None) -> str:
    vals = _get_project_status_enum_values(db)
    if vals:
        for s in preferred:
            if s in vals:
                return s
        # if fallback is valid, use it
        if fallback and fallback in vals:
            return fallback
        # as a last resort, return the last enum value (often terminal)
        return vals[-1]
    # if enum lookup fails, just return first preferred or fallback
    return fallback or (preferred[0] if preferred else "IN_PROGRESS")
ROLE_ADMIN_ID = 6

def _get_cancel_reason(db: Session, project_id: int) -> Optional[str]:
    """취소 사유 반환.
    - projects.cancel_reason 컬럼이 있으면 그 값을 사용
    - 없으면 project_updates의 '[취소사유] ...' 최신 1건을 사용
    """
    # 1) projects.cancel_reason 컬럼이 있는 DB
    try:
        if _column_exists(db, "projects", "cancel_reason"):
            v = db.execute(text("SELECT cancel_reason FROM projects WHERE id = :id"), {"id": project_id}).scalar()
            if v:
                return str(v).strip() or None
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass

    # 2) fallback: project_updates
    try:
        v = db.execute(
            text(
                """
                SELECT content
                FROM project_updates
                WHERE project_id = :pid AND content ILIKE '[취소사유] %'
                ORDER BY id DESC
                LIMIT 1
                """
            ),
            {"pid": project_id},
        ).scalar()
        if not v:
            return None
        s = str(v).strip()
        if s.startswith("[취소사유] "):
            s = s.replace("[취소사유] ", "", 1).strip()
        return s or None
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return None

ROLE_OPERATOR_ID = 7


def _role_id(user: Any) -> Optional[int]:
    rid = getattr(user, "role_id", None)
    try:
        return int(rid) if rid is not None else None
    except Exception:
        return None



def _has_column(db: Session, table: str, column: str) -> bool:
    """PostgreSQL에서 특정 테이블에 컬럼이 존재하는지 확인"""
    try:
        q = text("""
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = :t AND column_name = :c
            LIMIT 1
        """)
        return db.execute(q, {"t": table, "c": column}).scalar() is not None
    except Exception:
        return False

def _is_admin_by_code(db: Session, user: User) -> bool:
    """관리자 판정(안전):
    - role_id == ROLE_ADMIN_ID(=6) 이면 관리자
    - roles.code 가 ADMIN 이면 관리자(대소문자 무시)
    """
    role_id = _role_id(user)
    if not role_id:
        return False
    try:
        if int(role_id) == int(ROLE_ADMIN_ID):
            return True
    except Exception:
        pass
    try:
        code = db.execute(text("SELECT code FROM roles WHERE id = :id"), {"id": role_id}).scalar()
        return str(code or "").strip().upper() == "ADMIN"
    except Exception:
        return False


def _get_participant_scores(db: Session, project_id: int, can_view_scores: bool, *, project_status: Optional[str] = None) -> List[ParticipantScoreOut]:
    """
    프로젝트 참여자 점수 조회.
    - 기준 테이블: project_evaluations (존재 시) → project_participants(존재 시)
    - 프로젝트가 완료 상태가 아니면(사업완료 전) 빈 리스트 반환하여 '유령 참여자수'를 방지
    """
    status_u = (project_status or "").strip().upper()
    completed = status_u in {"DONE", "COMPLETED", "COMPLETE", "FINISHED", "CLOSED"}
    if project_status is not None and not completed:
        return []

    rows = []
    # 1) project_evaluations 우선
    if _table_exists(db, "project_evaluations"):
        try:
            rows = db.execute(
                text(
                    """
                    SELECT
                      pe.user_id AS employee_id,
                      u.name AS employee_name,
                      pe.score AS score
                    FROM project_evaluations pe
                    LEFT JOIN users u ON u.id = pe.user_id
                    WHERE pe.project_id = :pid
                    ORDER BY pe.user_id ASC
                    """
                ),
                {"pid": project_id},
            ).mappings().all()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            rows = []

    # 2) 하위호환: project_participants
    if (not rows) and _table_exists(db, "project_participants"):
        try:
            rows = db.execute(
                text(
                    """
                    SELECT
                      pp.employee_id AS employee_id,
                      e.name AS employee_name,
                      pp.score AS score
                    FROM project_participants pp
                    LEFT JOIN employees e ON e.id = pp.employee_id
                    WHERE pp.project_id = :pid
                    ORDER BY pp.employee_id ASC
                    """
                ),
                {"pid": project_id},
            ).mappings().all()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            rows = []

    out: List[ParticipantScoreOut] = []
    for r in rows or []:
        out.append(
            ParticipantScoreOut(
                employee_id=int(r["employee_id"]),
                employee_name=r.get("employee_name"),
                score=(float(r["score"]) if (can_view_scores and r.get("score") is not None) else None),
            )
        )
    return out



def _require_login(user: Optional[User]) -> User:
    if not user:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    return user


def _require_admin(user: Optional[User]) -> User:
    user = _require_login(user)
    if getattr(user, "role_id", None) != ROLE_ADMIN_ID:
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다.")
    return user


def _normalize_year(year: Optional[int]) -> Optional[int]:
    if year is None:
        return None
    if year < 2000 or year > 2100:
        raise HTTPException(status_code=400, detail="year 값이 올바르지 않습니다.")
    return year


# ----------------------
# Schemas
# ----------------------

class DepartmentMeta(BaseModel):
    id: int
    code: Optional[str] = None
    name: str
    sort_order: int = 0
    in_progress_count: int = 0


class ClientMeta(BaseModel):
    id: int
    name: str
    number: int = 0


class BusinessTypeMeta(BaseModel):
    id: int
    name: str
    number: int = 0


class ClientUpsert(BaseModel):
    name: str = Field(..., min_length=1)
    number: int = 0
    sort_order: int = 0
    is_active: bool = True
    memo: Optional[str] = None


class BusinessTypeUpsert(BaseModel):
    name: str = Field(..., min_length=1)
    number: int = 0
    sort_order: int = 0
    is_active: bool = True
    memo: Optional[str] = None


class ClientPatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1)
    number: Optional[int] = None


class BusinessTypePatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1)
    number: Optional[int] = None


class ProjectListItem(BaseModel):
    id: int
    name: str
    department_id: Optional[int] = None
    department_name: Optional[str] = None
    client_id: Optional[int] = None
    client_name: Optional[str] = None
    status: str
    has_unread_update: bool = False
    start_date: Optional[dt.date] = None
    end_date: Optional[dt.date] = None
    due_date: Optional[dt.date] = None  # 프론트 호환(alias)
    created_at: Optional[dt.datetime] = None


class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1)
    department_id: Optional[int] = None
    client_id: Optional[int] = None
    client_name: Optional[str] = None  # ✅ 수기 발주처 입력
    business_type_id: Optional[int] = None
    status: Optional[str] = None  # enum 문자열 (예: PLANNING, IN_PROGRESS)
    start_date: Optional[dt.date] = None
    end_date: Optional[dt.date] = None
    memo: Optional[str] = None


class ProjectAdminInfo(BaseModel):
    # ✅ 관리자 프로젝트 정보(최종점수 산출에 필요한 값 포함)
    # 프론트(ProjectDetailPage)에서 admin-info 저장 시 보내는 필드들을 그대로 받는다.
    contract_amount: Optional[float] = None

    # 최종점수 산출 필드(있으면 projects에 저장)
    project_period_days: Optional[float] = None
    project_period: Optional[str] = None
    difficulty: Optional[float] = None
    progress_step: Optional[float] = None
    participant_count: Optional[float] = None
    profit_rate: Optional[float] = None  # 프론트는 점수(= profitRateScore)를 보냄

    # 비용/메모 (기존 + 확장)
    cost_material: Optional[float] = None
    cost_labor: Optional[float] = None
    cost_office: Optional[float] = None
    cost_progress: Optional[float] = None
    cost_other: Optional[float] = None
    sales_cost: Optional[float] = None

    cost_other_note: Optional[str] = None
    other_note: Optional[str] = None


# ----------------------
# Meta APIs
# ----------------------



class ProjectPatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1)
    client_id: Optional[int] = None
    client_name: Optional[str] = None  # 수기 발주처(이름) → clients upsert
    business_type_id: Optional[int] = None
    memo: Optional[str] = None
    created_by_id: Optional[int] = None  # 관리자만 변경 가능


def _can_edit_project(db: Session, project_id: int, user: User) -> bool:
    rid = _role_id(user)
    is_admin = _is_admin_by_code(db, user) or rid == ROLE_ADMIN_ID
    if is_admin:
        return True
    created_by = db.execute(text("SELECT created_by FROM projects WHERE id = :id"), {"id": project_id}).scalar()
    try:
        return int(created_by or 0) == int(user.id)
    except Exception:
        return False



@router.patch("/{project_id}", response_model=Dict[str, Any])
def update_project_info(
    project_id: int,
    payload: ProjectPatch,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    user = _require_login(current_user)

    exists = db.execute(text("SELECT 1 FROM projects WHERE id = :id"), {"id": project_id}).first()
    if not exists:
        raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다.")

    if not _can_edit_project(db, project_id, user):
        raise HTTPException(status_code=403, detail="수정 권한이 없습니다.")

    fields: List[str] = []
    params: Dict[str, Any] = {"id": project_id}

    if payload.name is not None:
        fields.append("name = :name")
        params["name"] = payload.name.strip()

    if payload.business_type_id is not None:
        fields.append("business_type_id = :business_type_id")
        params["business_type_id"] = payload.business_type_id

    if payload.memo is not None:
        fields.append("memo = :memo")
        params["memo"] = payload.memo

    # 발주처: client_id 우선, 없으면 client_name으로 upsert
    if payload.client_id is not None:
        fields.append("client_id = :client_id")
        params["client_id"] = payload.client_id
    elif payload.client_name is not None:
        cname = payload.client_name.strip()
        if cname:
            row_c = db.execute(text("SELECT id FROM clients WHERE name = :name"), {"name": cname}).first()
            if row_c:
                cid = row_c[0]
            else:
                next_number = db.execute(text("SELECT COALESCE(MAX(number), 0) + 1 FROM clients")).scalar() or 1
                row_new = db.execute(
                    text("INSERT INTO clients (name, number) VALUES (:name, :number) RETURNING id"),
                    {"name": cname, "number": int(next_number)},
                ).first()
                cid = row_new[0]
            fields.append("client_id = :client_id")
            params["client_id"] = int(cid)

    # 등록자 변경: 관리자만 허용
    if payload.created_by_id is not None:
        rid = _role_id(user)
        is_admin = _is_admin_by_code(db, user) or rid == ROLE_ADMIN_ID
        if not is_admin:
            raise HTTPException(status_code=403, detail="등록자 변경은 관리자만 가능합니다.")
        fields.append("created_by = :created_by")
        params["created_by"] = int(payload.created_by_id)

    if not fields:
        raise HTTPException(status_code=400, detail="변경할 값이 없습니다.")

    # updated_at 컬럼이 없을 수도 있으니, 안전하게 2단계 시도
    try:
        db.execute(
            text(f"UPDATE projects SET {', '.join(fields)}, updated_at = NOW() WHERE id = :id"),
            params,
        )
    except Exception:
        db.execute(
            text(f"UPDATE projects SET {', '.join(fields)} WHERE id = :id"),
            params,
        )

    db.commit()
    return {"ok": True}


@router.get("/meta/departments", response_model=List[DepartmentMeta])
def list_departments(
    year: Optional[int] = Query(None, description="연도(예: 2026)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> List[DepartmentMeta]:
    _require_login(current_user)
    y = _normalize_year(year)

    dept_rows = db.execute(
        text(
            """
            SELECT id, code, name, COALESCE(sort_order,0) AS sort_order
            FROM departments
            ORDER BY COALESCE(sort_order,0) ASC, id ASC
            """
        )
    ).mappings().all()

    where_year = ""
    params: Dict[str, Any] = {}
    if y is not None:
        where_year = "AND EXTRACT(YEAR FROM COALESCE(p.start_date, p.created_at)) = :y"
        params["y"] = y

    cnt_rows = db.execute(
        text(
            f"""
            SELECT p.department_id AS department_id, COUNT(*) AS cnt
            FROM projects p
            WHERE p.department_id IS NOT NULL
              AND p.status IN ('PLANNING','IN_PROGRESS','ON_HOLD')
              {where_year}
            GROUP BY p.department_id
            """
        ),
        params,
    ).mappings().all()

    cnt_map = {int(r["department_id"]): int(r["cnt"]) for r in cnt_rows}

    out: List[DepartmentMeta] = []
    for r in dept_rows:
        did = int(r["id"])
        out.append(
            DepartmentMeta(
                id=did,
                code=r.get("code"),
                name=r.get("name"),
                sort_order=int(r.get("sort_order") or 0),
                in_progress_count=cnt_map.get(did, 0),
            )
        )
    return out


@router.get("/meta/clients", response_model=List[ClientMeta])
def list_clients(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> List[ClientMeta]:
    _require_login(current_user)
    rows = db.execute(
        text(
            """
            SELECT id, name, COALESCE(number,0) AS number
            FROM clients
            ORDER BY COALESCE(number, 999999) ASC, name ASC, id ASC
            """
        )
    ).mappings().all()
    return [ClientMeta(id=int(r["id"]), name=r["name"], number=int(r.get("number") or 0)) for r in rows]


@router.get("/meta/business-types", response_model=List[BusinessTypeMeta])
def list_business_types(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> List[BusinessTypeMeta]:
    _require_login(current_user)
    rows = db.execute(
        text(
            """
            SELECT id, name, COALESCE(number,0) AS number
            FROM project_business_types
            WHERE deleted_at IS NULL AND COALESCE(is_active, true) = true
            ORDER BY COALESCE(number, 999999) ASC, COALESCE(sort_order,0) ASC, id ASC
            """
        )
    ).mappings().all()
    return [BusinessTypeMeta(id=int(r["id"]), name=r["name"], number=int(r.get("number") or 0)) for r in rows]


@router.post("/meta/clients", response_model=ClientMeta)
def create_client(
    payload: ClientUpsert,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ClientMeta:
    _require_admin(current_user)

    # ✅ 번호 자동 부여: 입력이 없으면 마지막 번호 + 1
    num = int(payload.number or 0)
    if num <= 0:
        num = int(db.execute(text("SELECT COALESCE(MAX(number), 0) + 1 FROM clients")).scalar() or 1)

    row = db.execute(
        text(
            """
            INSERT INTO clients(name, number)
            VALUES (:name, :number)
            RETURNING id, name, COALESCE(number,0) AS number
            """
        ),
        {"name": payload.name.strip(), "number": int(num),
                "created_by": current_user.id},
    ).mappings().first()
    db.commit()
    return ClientMeta(id=int(row["id"]), name=row["name"], number=int(row.get("number") or 0))


@router.delete("/meta/clients/{client_id}")
def delete_client(
    client_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    _require_admin(current_user)
    db.execute(text("DELETE FROM clients WHERE id = :id"), {"id": client_id})
    db.commit()
    return {"ok": True}


@router.patch("/meta/clients/{client_id}", response_model=ClientMeta)
def update_client(
    client_id: int,
    payload: ClientPatch,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ClientMeta:
    _require_admin(current_user)
    fields: List[str] = []
    params: Dict[str, Any] = {"id": client_id}
    if payload.name is not None:
        fields.append("name = :name")
        params["name"] = payload.name.strip()
    if payload.number is not None:
        fields.append("number = :number")
        params["number"] = int(payload.number)
    if not fields:
        raise HTTPException(status_code=400, detail="변경할 값이 없습니다.")
    row = db.execute(
        text(
            f"""
            UPDATE clients
            SET {', '.join(fields)}
            WHERE id = :id
            RETURNING id, name, COALESCE(number,0) AS number
            """
        ),
        params,
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="발주처를 찾을 수 없습니다.")
    db.commit()
    return ClientMeta(id=int(row["id"]), name=row["name"], number=int(row.get("number") or 0))


@router.post("/meta/business-types", response_model=BusinessTypeMeta)
def create_business_type(
    payload: BusinessTypeUpsert,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> BusinessTypeMeta:
    _require_admin(current_user)

    # ✅ 번호 자동 부여: 입력이 없으면 마지막 번호 + 1
    num = int(payload.number or 0)
    if num <= 0:
        num = int(db.execute(text("SELECT COALESCE(MAX(number), 0) + 1 FROM project_business_types")).scalar() or 1)

    row = db.execute(
        text(
            """
            INSERT INTO project_business_types(name, number, sort_order, is_active, memo)
            VALUES (:name, :number, :sort_order, :is_active, :memo)
            RETURNING id, name, COALESCE(number,0) AS number
            """
        ),
        {
            "name": payload.name.strip(),
            "number": int(num),
            "sort_order": int(payload.sort_order or 0),
            "is_active": bool(payload.is_active),
            "memo": payload.memo,
                    },
    ).mappings().first()
    db.commit()
    return BusinessTypeMeta(id=int(row["id"]), name=row["name"], number=int(row.get("number") or 0))


@router.delete("/meta/business-types/{bt_id}")
def delete_business_type(
    bt_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    _require_admin(current_user)
    db.execute(text("UPDATE project_business_types SET deleted_at = NOW() WHERE id = :id"), {"id": bt_id})
    db.commit()
    return {"ok": True}


@router.patch("/meta/business-types/{bt_id}", response_model=BusinessTypeMeta)
def update_business_type(
    bt_id: int,
    payload: BusinessTypePatch,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> BusinessTypeMeta:
    _require_admin(current_user)
    fields: List[str] = []
    params: Dict[str, Any] = {"id": bt_id}
    if payload.name is not None:
        fields.append("name = :name")
        params["name"] = payload.name.strip()
    if payload.number is not None:
        fields.append("number = :number")
        params["number"] = int(payload.number)
    if not fields:
        raise HTTPException(status_code=400, detail="변경할 값이 없습니다.")
    row = db.execute(
        text(
            f"""
            UPDATE project_business_types
            SET {', '.join(fields)}
            WHERE id = :id
            RETURNING id, name, COALESCE(number,0) AS number
            """
        ),
        params,
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="사업종류를 찾을 수 없습니다.")
    db.commit()
    return BusinessTypeMeta(id=int(row["id"]), name=row["name"], number=int(row.get("number") or 0))


# ----------------------
# Projects
# ----------------------

@router.get("", response_model=List[ProjectListItem])
def list_projects(
    year: Optional[int] = Query(None),
    department_id: Optional[int] = Query(None),
    client_id: Optional[int] = Query(None),
    name: Optional[str] = Query(None, description="명칭(사업명)"),
    status: Optional[str] = Query(None, description="상태(enum 문자열)"),
    q: Optional[str] = Query(None, description="추가 검색(메모 등)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> List[ProjectListItem]:
    _require_login(current_user)
    y = _normalize_year(year)

    where: List[str] = ["1=1"]
    params: Dict[str, Any] = {}

    if department_id is not None:
        where.append("p.department_id = :department_id")
        params["department_id"] = department_id
    if client_id is not None:
        where.append("p.client_id = :client_id")
        params["client_id"] = client_id
    if name:
        where.append("p.name ILIKE :name")
        params["name"] = f"%{name}%"
    if status:
        where.append("p.status = :status")
        params["status"] = status
    if q:
        where.append("(p.name ILIKE :q OR COALESCE(p.memo,'') ILIKE :q)")
        params["q"] = f"%{q}%"
    if y is not None:
        where.append("EXTRACT(YEAR FROM COALESCE(p.start_date, p.created_at)) = :y")
        params["y"] = y

    rows = db.execute(
        text(
            f"""
            SELECT
              p.id, p.name, p.department_id, d.name AS department_name,
              p.client_id, c.name AS client_name,
              p.status, COALESCE(p.has_unread_update,false) AS has_unread_update,
              p.start_date, p.end_date, p.end_date AS due_date, p.created_at
            FROM projects p
            LEFT JOIN departments d ON d.id = p.department_id
            LEFT JOIN clients c ON c.id = p.client_id
            WHERE {' AND '.join(where)}
            ORDER BY p.id DESC
            """
        ),
        params,
    ).mappings().all()

    out: List[ProjectListItem] = []
    for r in rows:
        out.append(
            ProjectListItem(
                id=int(r["id"]),
                name=r["name"],
                department_id=r.get("department_id"),
                department_name=r.get("department_name"),
                client_id=r.get("client_id"),
                client_name=r.get("client_name"),
                status=str(r.get("status")),
                has_unread_update=bool(r.get("has_unread_update")),
                start_date=r.get("start_date"),
                end_date=r.get("end_date"),
                due_date=r.get("due_date"),
                created_at=r.get("created_at"),
            )
        )
    return out


@router.post("", response_model=Dict[str, Any])
def create_project(
    payload: ProjectCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    user = _require_login(current_user)

    rid = _role_id(user)
    is_admin = _is_admin_by_code(db, user) or rid == ROLE_ADMIN_ID

    # ✅ 부서 결정
    dept_id = payload.department_id
    if not is_admin:
        user_dept_id = getattr(user, "department_id", None)
        if user_dept_id:
            dept_id = user_dept_id
        else:
            if not dept_id:
                raise HTTPException(status_code=400, detail="부서가 설정되어 있지 않습니다. (부서를 선택하세요)")

    # status 기본값
    status = payload.status or "PLANNING"

    # ✅ client_id 보정(수기 발주처 지원 + number 자동 증가)
    client_id = payload.client_id
    if not client_id:
        cname = (payload.client_name or "").strip()
        if not cname:
            raise HTTPException(status_code=422, detail="발주처를 선택하거나 수기로 입력하세요.")

        row_c = db.execute(text("SELECT id FROM clients WHERE name = :name"), {"name": cname}).first()
        if row_c:
            client_id = row_c[0]
        else:
            next_number = db.execute(text("SELECT COALESCE(MAX(number), 0) + 1 FROM clients")).scalar() or 1
            row_c = db.execute(
                text("INSERT INTO clients (name, number) VALUES (:name, :number) RETURNING id"),
                {"name": cname, "number": int(next_number)},
            ).first()
            client_id = row_c[0]

    row = db.execute(
        text(
            """
            INSERT INTO projects (name, department_id, client_id, business_type_id, status, start_date, end_date, memo, has_unread_update , created_by)
            VALUES (:name, :department_id, :client_id, :business_type_id, :status, :start_date, :end_date, :memo, false , :created_by)
            RETURNING id
            """
        ),
        {
            "name": payload.name,
            "department_id": dept_id,
            "client_id": client_id,
            "business_type_id": payload.business_type_id,
            "status": status,
            "start_date": payload.start_date,
            "end_date": payload.end_date,
            "memo": payload.memo,
                    },
    ).first()

    db.commit()
    return {"id": int(row[0]) if row else None}


@router.put("/{project_id}/admin-info", response_model=Dict[str, Any])
def update_project_admin_info(
    project_id: int,
    payload: ProjectAdminInfo,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    _require_admin(current_user)

    exists = db.execute(text("SELECT 1 FROM projects WHERE id = :id"), {"id": project_id}).first()
    if not exists:
        raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다.")

    # ✅ admin-info 저장: projects 테이블에 가능한 컬럼을 모두 저장(컬럼이 없으면 조용히 skip)
    fields: List[str] = []
    params: Dict[str, Any] = {"id": project_id}

    def _set_if_exists(col: str, key: str, val: Any):
        if val is None:
            return
        if not _column_exists(db, "projects", col):
            return
        fields.append(f"{col} = :{key}")
        params[key] = val

    _set_if_exists("contract_amount", "contract_amount", payload.contract_amount)
    _set_if_exists("project_period_days", "project_period_days", payload.project_period_days)
    _set_if_exists("project_period", "project_period", payload.project_period.strip() if isinstance(payload.project_period, str) else payload.project_period)
    _set_if_exists("difficulty", "difficulty", payload.difficulty)
    _set_if_exists("progress_step", "progress_step", payload.progress_step)
    _set_if_exists("participant_count", "participant_count", payload.participant_count)
    _set_if_exists("profit_rate", "profit_rate", payload.profit_rate)

    # 확장(있으면 저장)
    _set_if_exists("cost_progress", "cost_progress", payload.cost_progress)
    _set_if_exists("cost_other_note", "cost_other_note", payload.cost_other_note.strip() if isinstance(payload.cost_other_note, str) else payload.cost_other_note)
    _set_if_exists("other_note", "other_note", payload.other_note.strip() if isinstance(payload.other_note, str) else payload.other_note)

    if fields:
        db.execute(
            text(f"UPDATE projects SET {', '.join(fields)}, updated_at = NOW() WHERE id = :id"),
            params,
        )

    # 원가/영업비는 별도 테이블에 저장(없으면 400 안내)
    try:
        db.execute(
            text(
                """
                INSERT INTO project_admin_finance
                  (project_id, cost_material, cost_labor, cost_office, cost_other, sales_cost, updated_at)
                VALUES
                  (:id, :cost_material, :cost_labor, :cost_office, :cost_other, :sales_cost, NOW())
                ON CONFLICT (project_id) DO UPDATE SET
                  cost_material = EXCLUDED.cost_material,
                  cost_labor = EXCLUDED.cost_labor,
                  cost_office = EXCLUDED.cost_office,
                  cost_other = EXCLUDED.cost_other,
                  sales_cost = EXCLUDED.sales_cost,
                  updated_at = NOW()
                """
            ),
            {
                "id": project_id,
                "cost_material": payload.cost_material,
                "cost_labor": payload.cost_labor,
                "cost_office": payload.cost_office,
                "cost_other": payload.cost_other,
                "sales_cost": payload.sales_cost,
            },
        )
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=400,
            detail=(
                "관리자 재무 테이블(project_admin_finance)이 없거나 DB 권한이 부족합니다.\n"
                "DB에 아래 SQL을 1회 적용한 뒤 다시 저장하세요.\n\n"
                "CREATE TABLE project_admin_finance (\n"
                "  project_id INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,\n"
                "  cost_material DOUBLE PRECISION NULL,\n"
                "  cost_labor DOUBLE PRECISION NULL,\n"
                "  cost_office DOUBLE PRECISION NULL,\n"
                "  cost_other DOUBLE PRECISION NULL,\n"
                "  sales_cost DOUBLE PRECISION NULL,\n"
                "  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),\n"
                "  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()\n"
                ");\n"
                f"\n원인: {e}"
            ),
        ) from e

    db.commit()
    return {"ok": True}


# ----------------------
# Project Detail / Updates
# ----------------------

class ParticipantScoreOut(BaseModel):
    employee_id: int
    employee_name: Optional[str] = None
    score: Optional[float] = None


class ProjectDetailOut(BaseModel):
    id: int
    name: str
    client_name: Optional[str] = None
    client_id: Optional[int] = None
    department_name: Optional[str] = None
    business_type_name: Optional[str] = None
    business_type_id: Optional[int] = None
    created_by_id: Optional[int] = None
    created_by_name: Optional[str] = None
    cancel_reason: Optional[str] = None
    participant_scores: Optional[List[ParticipantScoreOut]] = None
    participant_count: Optional[int] = None

    memo: Optional[str] = None
    status: Optional[str] = None
    created_at: Optional[dt.datetime] = None
    has_unread_update: bool = False
    contract_amount: Optional[float] = None
    cost_material: Optional[float] = None
    cost_labor: Optional[float] = None
    cost_office: Optional[float] = None
    cost_other: Optional[float] = None
    sales_cost: Optional[float] = None


class ProjectUpdateCreate(BaseModel):
    content: str = Field(..., min_length=1)


class ProjectUpdateOut(BaseModel):
    id: int
    content: str
    created_at: dt.datetime
    created_by_name: Optional[str] = None
    department_name: Optional[str] = None


@router.get("/{project_id}", response_model=ProjectDetailOut)
def get_project_detail(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ProjectDetailOut:
    _require_login(current_user)

    # finance 테이블이 없거나 권한이 부족해도 상세조회는 살아야 함
    try:
        r = db.execute(
            text(
                """
                SELECT
                  p.id, p.name, p.memo, p.status, p.created_at,
                  COALESCE(p.has_unread_update,false) AS has_unread_update,
                  p.client_id AS client_id,
                  c.name AS client_name,
                  d.name AS department_name,
                  p.business_type_id AS business_type_id,
                  bt.name AS business_type_name,
                  p.created_by AS created_by_id,
                  u.name AS created_by_name,
                  p.contract_amount AS contract_amount,
                  f.cost_material AS cost_material,
                  f.cost_labor AS cost_labor,
                  f.cost_office AS cost_office,
                  f.cost_other AS cost_other,
                  f.sales_cost AS sales_cost
                FROM projects p
                LEFT JOIN clients c ON c.id = p.client_id
                LEFT JOIN departments d ON d.id = p.department_id
                LEFT JOIN project_business_types bt ON bt.id = p.business_type_id
                LEFT JOIN users u ON u.id = p.created_by
                LEFT JOIN project_admin_finance f ON f.project_id = p.id
                WHERE p.id = :id
                """
            ),
            {"id": project_id},
        ).mappings().first()
    except Exception:
        r = db.execute(
            text(
                """
                SELECT
                  p.id, p.name, p.memo, p.status, p.created_at,
                  COALESCE(p.has_unread_update,false) AS has_unread_update,
                  p.client_id AS client_id,
                  c.name AS client_name,
                  d.name AS department_name,
                  p.business_type_id AS business_type_id,
                  bt.name AS business_type_name,
                  p.created_by AS created_by_id,
                  u.name AS created_by_name,
                  p.contract_amount AS contract_amount
                FROM projects p
                LEFT JOIN clients c ON c.id = p.client_id
                LEFT JOIN departments d ON d.id = p.department_id
                LEFT JOIN project_business_types bt ON bt.id = p.business_type_id
                LEFT JOIN users u ON u.id = p.created_by
                WHERE p.id = :id
                """
            ),
            {"id": project_id},
        ).mappings().first()

    if not r:
        raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다.")

    # 점수 표시 권한: 관리자 또는 등록자(created_by)
    can_view_scores = _is_admin_by_code(db, current_user)
    try:
        if not can_view_scores:
            created_by_id = r.get("created_by_id")
            if created_by_id is not None and int(created_by_id) == int(current_user.id):
                can_view_scores = True
    except Exception:
        pass

    scores = _get_participant_scores(db, int(project_id), can_view_scores, project_status=r.get("status"))
    return ProjectDetailOut(
        id=int(r["id"]),
        name=r["name"],
        memo=r.get("memo"),
        status=r.get("status"),
        created_at=r.get("created_at"),
        has_unread_update=bool(r.get("has_unread_update")),
        client_name=r.get("client_name"),
        department_name=r.get("department_name"),
        business_type_name=r.get("business_type_name"),
        contract_amount=r.get("contract_amount"),
        cost_material=r.get("cost_material"),
        cost_labor=r.get("cost_labor"),
        cost_office=r.get("cost_office"),
        cost_other=r.get("cost_other"),
        sales_cost=r.get("sales_cost"),
        client_id=r.get("client_id"),
        business_type_id=r.get("business_type_id"),
        created_by_id=r.get("created_by_id"),
        created_by_name=r.get("created_by_name"),
        cancel_reason=_get_cancel_reason(db, int(project_id)),
        participant_scores=scores,
        participant_count=len(scores),
    )



@router.get("/{project_id}/updates", response_model=List[ProjectUpdateOut])
def list_project_updates(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> List[ProjectUpdateOut]:
    _require_login(current_user)
    rows = db.execute(
        text(
            """
            SELECT
              u.id,
              u.content,
              u.created_at,
              usr.name AS created_by_name,
              d.name AS department_name
            FROM project_updates u
            LEFT JOIN users usr ON usr.id = u.created_by
            LEFT JOIN departments d ON d.id = u.department_id
            WHERE u.project_id = :pid
            ORDER BY u.id DESC
            """
        ),
        {"pid": project_id},
    ).mappings().all()

    return [
        ProjectUpdateOut(
            id=int(r["id"]),
            content=r["content"],
            created_at=r["created_at"],
            created_by_name=r.get("created_by_name"),
            department_name=r.get("department_name"),
        )
        for r in rows
    ]


@router.post("/{project_id}/updates", response_model=Dict[str, Any])
def create_project_update(
    project_id: int,
    payload: ProjectUpdateCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    user = _require_login(current_user)

    exists = db.execute(text("SELECT 1 FROM projects WHERE id = :id"), {"id": project_id}).scalar()
    if not exists:
        raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다.")

    dept_id = getattr(user, "department_id", None)
    row = db.execute(
        text(
            """
            INSERT INTO project_updates (project_id, content, created_by, department_id, created_at)
            VALUES (:pid, :content, :uid, :dept_id, NOW())
            RETURNING id
            """
        ),
        {"pid": project_id, "content": payload.content.strip(), "uid": user.id, "dept_id": dept_id},
    ).first()

    db.execute(text("UPDATE projects SET has_unread_update = true WHERE id = :id"), {"id": project_id})
    db.commit()
    return {"id": int(row[0]) if row else None, "ok": True}



@router.put("/{project_id}/updates/{update_id}", response_model=Dict[str, Any])
def update_project_update(
    project_id: int,
    update_id: int,
    payload: ProjectUpdateCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """프로젝트 진행내용 수정.
    정책:
    - 관리자(ROLE_ADMIN_ID=6 또는 roles.code=ADMIN) 또는 프로젝트 등록자(created_by)만 수정 가능
    """
    user = _require_login(current_user)

    # 프로젝트 존재 확인
    exists = db.execute(text("SELECT 1 FROM projects WHERE id = :id"), {"id": project_id}).scalar()
    if not exists:
        raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다.")

    # 권한 확인
    if not _can_edit_project(db, project_id, user):
        raise HTTPException(status_code=403, detail="수정 권한이 없습니다.")

    # 업데이트 존재/소속 확인
    row = db.execute(
        text(
            """
            SELECT id
            FROM project_updates
            WHERE id = :uid AND project_id = :pid
            """
        ),
        {"uid": update_id, "pid": project_id},
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="진행내용을 찾을 수 없습니다.")

    # 내용 수정
    try:
        db.execute(
            text(
                """
                UPDATE project_updates
                SET content = :content, created_at = NOW()
                WHERE id = :uid AND project_id = :pid
                """
            ),
            {"content": payload.content.strip(), "uid": update_id, "pid": project_id},
        )
    except Exception:
        # updated_at 컬럼이 있는 경우를 대비 (있으면 자동 갱신)
        try:
            db.execute(
                text(
                    """
                    UPDATE project_updates
                    SET content = :content, updated_at = NOW(), created_at = NOW()
                    WHERE id = :uid AND project_id = :pid
                    """
                ),
                {"content": payload.content.strip(), "uid": update_id, "pid": project_id},
            )
        except Exception as e:
            db.rollback()
            raise HTTPException(status_code=500, detail=f"진행내용 수정 실패: {e}") from e

    # 수정도 '미확인'으로 처리(관리자 확인 프로세스 유지)
    try:
        db.execute(text("UPDATE projects SET has_unread_update = true WHERE id = :id"), {"id": project_id})
    except Exception:
        pass

    db.commit()
    return {"ok": True, "id": int(update_id)}


@router.delete("/{project_id}/updates/{update_id}", response_model=Dict[str, Any])
def delete_project_update(
    project_id: int,
    update_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """프로젝트 진행내용 삭제.
    - 관리자(ROLE_ADMIN_ID=6) 또는 프로젝트 등록자(created_by)만 삭제 가능
    """
    user = _require_login(current_user)

    # 프로젝트 존재 확인
    exists = db.execute(text("SELECT 1 FROM projects WHERE id = :id"), {"id": project_id}).scalar()
    if not exists:
        raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다.")

    # 권한 확인
    if not _can_edit_project(db, project_id, user):
        raise HTTPException(status_code=403, detail="삭제 권한이 없습니다.")

    # 업데이트 존재/소속 확인
    row = db.execute(
        text("SELECT id FROM project_updates WHERE id = :uid AND project_id = :pid"),
        {"uid": update_id, "pid": project_id},
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="진행내용을 찾을 수 없습니다.")

    try:
        db.execute(
            text("DELETE FROM project_updates WHERE id = :uid AND project_id = :pid"),
            {"uid": update_id, "pid": project_id},
        )
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"진행내용 삭제 실패: {e}") from e

    return {"ok": True, "id": int(update_id)}



@router.post("/{project_id}/admin-ack", response_model=Dict[str, Any])
def admin_ack_project(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """관리자 내용확인(ACK)
    - 관리자만 가능
    - projects.has_unread_update=false 로 초기화
    - '마지막 확인 시각'을 전 직원이 공유할 수 있도록 project_updates에 ACK 마커를 남김
      * project_updates 컬럼명은 기존 INSERT 로직과 동일하게 사용(created_by, department_id, created_at)
    """
    _require_admin(current_user)

    # 프로젝트 존재/부서 확인
    proj = db.execute(text("SELECT id, department_id FROM projects WHERE id = :id"), {"id": project_id}).first()
    if not proj:
        raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다.")

    dept_id = proj[1] if len(proj) > 1 else None
    uid = int(getattr(current_user, "id", 0) or 0)

    # 1) unread 해제
    db.execute(text("UPDATE projects SET has_unread_update = false WHERE id = :id"), {"id": project_id})

    # 2) ACK 마커 업데이트 추가
    try:
        db.execute(
            text(
                """
                INSERT INTO project_updates (project_id, content, created_by, department_id, created_at)
                VALUES (:pid, :content, :uid, :dept_id, NOW())
                """
            ),
            {"pid": project_id, "content": "[관리자확인]", "uid": uid, "dept_id": dept_id},
        )
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"관리자 확인 기록 실패: {e}") from e

    db.commit()
    return {"ok": True}



# ==============================================
# 상태 변경: 완료/취소/다시진행
# ==============================================

class ProjectCompleteParticipant(BaseModel):
    employee_id: int
    score: float = Field(..., ge=0)


class ProjectCompletePayload(BaseModel):
    participants: List[ProjectCompleteParticipant]


class ProjectCancelPayload(BaseModel):
    reason: str = Field(..., min_length=1)


def _table_exists(db: Session, table_name: str) -> bool:
    # Postgres 기준: to_regclass 사용
    try:
        return db.execute(text("SELECT to_regclass(:t) IS NOT NULL"), {"t": f"public.{table_name}"}).scalar() is True
    except Exception:
        return False


def _column_exists(db: Session, table_name: str, column_name: str) -> bool:
    try:
        q = text(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name=:t AND column_name=:c
            LIMIT 1
            """
        )
        return db.execute(q, {"t": table_name, "c": column_name}).first() is not None
    except Exception:
        return False


@router.post("/{project_id}/complete", response_model=Dict[str, Any])
def complete_project(
    project_id: int,
    payload: ProjectCompletePayload,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    user = _require_login(current_user)
    # 권한: ADMIN 또는 등록자(created_by)만
    is_admin = _is_admin_by_code(db, user)
    created_by = db.execute(text("SELECT created_by FROM projects WHERE id = :id"), {"id": project_id}).scalar()
    if not is_admin and int(created_by or 0) != int(user.id):
        raise HTTPException(status_code=403, detail="권한이 없습니다.")

    # DB enum(project_status)에 맞는 완료 상태 자동 선택
    new_status = _pick_status(db, ["COMPLETED", "DONE", "FINISHED", "COMPLETE", "CLOSED"], fallback="DONE")

    # 프로젝트 상태 업데이트
    db.execute(
        text("""
            UPDATE projects
            SET status = :new_status,
                end_date = COALESCE(end_date, CURRENT_DATE),
                updated_at = NOW()
            WHERE id = :id
        """),
        {"id": project_id, "new_status": new_status},
    )

    # 참여자 점수 저장
    if _table_exists(db, "project_evaluations"):
        # 정식 테이블(유저 기반) 저장: 기존 삭제 후 재삽입
        db.execute(text("DELETE FROM project_evaluations WHERE project_id = :id"), {"id": project_id})
        # created_by NOT NULL 대응
        has_created_by = db.execute(text("""
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = 'project_evaluations' AND column_name = 'created_by'
            LIMIT 1
        """)).first() is not None

        for p in payload.participants:
                        if has_created_by:
                            db.execute(
                                text(
                                    """
                                    INSERT INTO project_evaluations (project_id, user_id, score, created_by, created_at)
                                    VALUES (:pid, :uid, :score, :created_by, NOW())
                                    """
                                ),
                                {"pid": project_id, "uid": int(p.employee_id), "score": p.score, "created_by": current_user.id},
                            )
                        else:
                            db.execute(
                                text(
                                    """
                                    INSERT INTO project_evaluations (project_id, user_id, score, created_at)
                                    VALUES (:pid, :uid, :score, NOW())
                                    """
                                ),
                                {"pid": project_id, "uid": int(p.employee_id), "score": p.score},
                            )
        # 혼선 방지: 기존 [평가점수] 로그는 남기지 않음(있으면 제거)
        try:
            if _table_exists(db, "project_updates"):
                db.execute(
                    text("DELETE FROM project_updates WHERE project_id = :pid AND content LIKE '[평가점수] %'"),
                    {"pid": project_id},
                )
        except Exception:
            pass
    elif _table_exists(db, "project_participants"):
        # 하위 호환(직원 기반)
        db.execute(text("DELETE FROM project_participants WHERE project_id = :id"), {"id": project_id})
        for p in payload.participants:
            db.execute(
                text(
                    """
                    INSERT INTO project_participants (project_id, employee_id, score, created_at)
                    VALUES (:pid, :eid, :score, NOW())
                    """
                ),
                {"pid": project_id, "eid": p.employee_id, "score": p.score},
            )
    else:
        # legacy fallback: project_updates에 JSON으로 남김(표시/검증용) - 가능하면 사용하지 않음
        try:
            if _table_exists(db, "project_updates"):
                payload_json = json.dumps(
                    [{"employee_id": p.employee_id, "score": p.score} for p in payload.participants],
                    ensure_ascii=False,
                )
                db.execute(
                    text(
                        "INSERT INTO project_updates (project_id, content, created_at, created_by) VALUES (:pid, :content, NOW(), :uid)"
                    ),
                    {"pid": project_id, "content": f"[평가점수] {payload_json}", "uid": user.id},
                )
        except Exception:
            pass


    db.commit()
    return {"ok": True}


@router.post("/{project_id}/cancel", response_model=Dict[str, Any])
def cancel_project(
    project_id: int,
    payload: ProjectCancelPayload,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    user = _require_login(current_user)
    is_admin = _is_admin_by_code(db, user)
    created_by = db.execute(text("SELECT created_by FROM projects WHERE id = :id"), {"id": project_id}).scalar()
    if not is_admin and int(created_by or 0) != int(user.id):
        raise HTTPException(status_code=403, detail="권한이 없습니다.")

    # DB enum(project_status)에 맞는 취소 상태 자동 선택
    new_status = _pick_status(db, ["CANCELLED", "CANCELED", "CANCEL", "ABORTED", "STOPPED"], fallback="CANCELLED")

    # 취소 사유 컬럼이 있으면 저장, 없으면 상태만 변경
    if _column_exists(db, "projects", "cancel_reason"):
        db.execute(
            text("""
                UPDATE projects
                SET status = :new_status,
                    cancel_reason = :r,
                    updated_at = NOW()
                WHERE id = :id
            """),
            {"id": project_id, "r": payload.reason, "new_status": new_status},
        )
    else:
        db.execute(
            text("""
                UPDATE projects
                SET status = :new_status,
                    updated_at = NOW()
                WHERE id = :id
            """),
            {"id": project_id, "new_status": new_status},
        )
        # cancel_reason 컬럼이 없는 DB에서는 project_updates에 사유를 남긴다.
        if _table_exists(db, "project_updates"):
            try:
                dept_id = getattr(user, "department_id", None)
                db.execute(
                    text("""
                        INSERT INTO project_updates (project_id, content, created_by, department_id, created_at)
                        VALUES (:pid, :content, :uid, :dept_id, NOW())
                    """),
                    {"pid": project_id, "content": f"[취소사유] {payload.reason}", "uid": user.id, "dept_id": dept_id},
                )
            except Exception:
                pass
    db.commit()
    return {"ok": True}


@router.post("/{project_id}/reopen", response_model=Dict[str, Any])
def reopen_project(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    user = _require_login(current_user)
    is_admin = _is_admin_by_code(db, user)
    created_by = db.execute(text("SELECT created_by FROM projects WHERE id = :id"), {"id": project_id}).scalar()
    if not is_admin and int(created_by or 0) != int(user.id):
        raise HTTPException(status_code=403, detail="권한이 없습니다.")

    # DB enum(project_status)에 맞는 재개 상태 자동 선택
    new_status = _pick_status(db, ["IN_PROGRESS", "ONGOING", "ACTIVE", "RUNNING", "PROGRESS"], fallback="IN_PROGRESS")

    # 상태 되돌리기
    if _column_exists(db, "projects", "cancel_reason"):
        db.execute(
            text("""
                UPDATE projects
                SET status = :new_status,
                    cancel_reason = NULL,
                    updated_at = NOW()
                WHERE id = :id
            """),
            {"id": project_id, "new_status": new_status},
        )
    else:
        db.execute(
            text("""
                UPDATE projects
                SET status = :new_status,
                    updated_at = NOW()
                WHERE id = :id
            """),
            {"id": project_id, "new_status": new_status},
        )

    # 참여자/평가 초기화
    if _table_exists(db, "project_evaluations"):
        db.execute(text("DELETE FROM project_evaluations WHERE project_id = :id"), {"id": project_id})
        # created_by NOT NULL 대응
        has_created_by = db.execute(text("""
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = 'project_evaluations' AND column_name = 'created_by'
            LIMIT 1
        """)).first() is not None

    if _table_exists(db, "project_participants"):
        db.execute(text("DELETE FROM project_participants WHERE project_id = :id"), {"id": project_id})
    # legacy 평가 로그 제거(유령 참여자수 방지)
    try:
        if _table_exists(db, "project_updates"):
            db.execute(
                text("DELETE FROM project_updates WHERE project_id = :pid AND content LIKE '[평가점수] %'"),
                {"pid": project_id},
            )
    except Exception:
        pass

    db.commit()
    return {"ok": True}



@router.delete("/{project_id}")
def delete_project(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 관리자만 허용 (대표님 기준 role_id == 6)
    if current_user.role_id != 6:
        raise HTTPException(status_code=403, detail="관리자만 삭제할 수 있습니다.")

    # 1) 해당 프로젝트에 연결된 견적서 조회
    rows = db.execute(
        text("SELECT id FROM estimates WHERE project_id = :pid"),
        {"pid": project_id},
    ).fetchall()

    # 2) 견적서가 있으면 구견적 포함 전부 삭제
    for r in rows:
        estimate_id = int(r[0])
        delete_estimate_with_revisions(db, estimate_id)

    # 3) 프로젝트 삭제
    db.execute(
        text("DELETE FROM projects WHERE id = :pid"),
        {"pid": project_id},
    )

    db.commit()
    return {"result": "ok"}
