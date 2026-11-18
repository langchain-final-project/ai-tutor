import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from sqlalchemy import Table, insert, select, tuple_, update
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.sql import ColumnElement, Select

from app.db.auth import STUDENT_ROLE, users
from app.db.member_management import ANSWERED, PENDING, inquiries, inquiry_replies

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, pattern=r"^[^\x00]+$")]
InquiryStatus = Literal["pending", "answered"]


class InquiryRequest(BaseModel):
    category: NonBlank
    title: NonBlank
    body: NonBlank


class InquiryCreated(BaseModel):
    inquiry_id: str
    status: InquiryStatus


class ReplyRequest(BaseModel):
    request_id: NonBlank
    body: NonBlank


class ReplyCreated(BaseModel):
    reply_id: str
    status: Literal["answered"] = ANSWERED


class InquirySummary(InquiryCreated):
    title: str


class AdminInquirySummary(InquirySummary):
    category: str


class StudentInquirySummary(InquirySummary):
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def with_timezone(cls, value: datetime) -> datetime:
        # SQLite returns a naive timestamp for these dates, which are stored in UTC.
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class Reply(BaseModel):
    reply_id: str
    body: str


class InquiryDetail(InquirySummary):
    body: str
    replies: list[Reply]


class MemberSummary(BaseModel):
    member_id: str
    name: str
    email: str
    status: Literal["active"] = "active"


class MemberDetail(MemberSummary):
    version: int


class MemberEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: NonBlank
    version: int = Field(ge=1, le=2147483647)


class MemberUpdated(BaseModel):
    member_id: str
    name: str
    version: int


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None


def read_page(
    connection: Connection, table: Table, statement: Select, cursor: str | None, size: int,
    *, oldest_first: bool = False, cursor_scope: ColumnElement[bool] | None = None,
):
    columns = (table.c.created_at, table.c.id)
    if cursor is not None:
        cursor_query = select(*columns).where(table.c.id == cursor)
        if cursor_scope is not None:
            cursor_query = cursor_query.where(cursor_scope)
        start = connection.execute(cursor_query).one_or_none()
        if start is None:
            raise HTTPException(422, "Invalid cursor")
        position = tuple_(*columns)
        boundary = tuple_(*start)
        statement = statement.where(position > boundary if oldest_first else position < boundary)
    order = [column.asc() if oldest_first else column.desc() for column in columns]
    rows = connection.execute(statement.order_by(*order).limit(size + 1)).mappings().all()
    return rows[:size], rows[size - 1]["id"] if len(rows) > size else None


def register_member_routes(
    router: APIRouter,
    engine: Engine,
    admin_access: Callable[[Request], None],
    student_access: Callable[[Request], str],
    inquiry_author: Callable[[Request], str],
) -> None:
    page_size = int(os.getenv("ADMIN_PAGE_SIZE", "25"))
    if page_size < 1:
        raise ValueError("ADMIN_PAGE_SIZE must be positive")
    admin = Depends(admin_access)
    student = Depends(student_access)
    author = Depends(inquiry_author)
    member_fields = (users.c.id, users.c.name, users.c.email, users.c.version)

    @router.post("/me/inquiries", status_code=201)
    def create_inquiry(payload: InquiryRequest, user_id: str = author) -> InquiryCreated:
        inquiry_id = str(uuid4())
        with engine.begin() as connection:
            connection.execute(insert(inquiries).values(
                id=inquiry_id, user_id=user_id, **payload.model_dump(), created_at=datetime.now(UTC),
            ))
        return InquiryCreated(inquiry_id=inquiry_id, status=PENDING)

    @router.get("/me/inquiries")
    def list_my_inquiries(cursor: str | None = None, user_id: str = student) -> Page[StudentInquirySummary]:
        owner = inquiries.c.user_id == user_id
        statement = select(inquiries.c.id, inquiries.c.title, inquiries.c.status, inquiries.c.created_at).where(owner)
        with engine.connect() as connection:
            rows, next_cursor = read_page(connection, inquiries, statement, cursor, page_size, cursor_scope=owner)
        return Page[StudentInquirySummary](
            items=[
                StudentInquirySummary(
                    inquiry_id=row["id"], title=row["title"], status=row["status"],
                    created_at=row["created_at"],
                )
                for row in rows
            ],
            next_cursor=next_cursor,
        )

    @router.get("/admin/inquiries", dependencies=[admin])
    def list_inquiries(
        status: InquiryStatus | None = None, cursor: str | None = None,
        category: Literal["learning", "account", "other"] | None = None,
        sort: Literal["newest", "oldest"] = "newest",
    ) -> Page[AdminInquirySummary]:
        statement = select(inquiries.c.id, inquiries.c.title, inquiries.c.status, inquiries.c.category)
        if status is not None:
            statement = statement.where(inquiries.c.status == status)
        if category is not None:
            statement = statement.where(inquiries.c.category == category)
        with engine.connect() as connection:
            rows, next_cursor = read_page(
                connection, inquiries, statement, cursor, page_size, oldest_first=sort == "oldest",
            )
        return Page[AdminInquirySummary](
            items=[AdminInquirySummary(
                inquiry_id=row["id"], title=row["title"], status=row["status"], category=row["category"],
            ) for row in rows],
            next_cursor=next_cursor,
        )

    def read_inquiry(inquiry_id: str, user_id: str | None = None) -> InquiryDetail:
        statement = select(
            inquiries.c.title, inquiries.c.body, inquiries.c.status,
            inquiry_replies.c.id.label("reply_id"), inquiry_replies.c.body.label("reply_body"),
        ).select_from(inquiries.outerjoin(inquiry_replies)).where(inquiries.c.id == inquiry_id)
        if user_id is not None:
            statement = statement.where(inquiries.c.user_id == user_id)
        with engine.connect() as connection:
            detail = connection.execute(statement).one_or_none()
        if detail is None:
            raise HTTPException(404, "Inquiry not found")
        replies = [Reply(reply_id=detail.reply_id, body=detail.reply_body)] if detail.reply_id else []
        return InquiryDetail(
            inquiry_id=inquiry_id, title=detail.title, body=detail.body, status=detail.status, replies=replies,
        )

    @router.get("/me/inquiries/{inquiry_id}")
    def get_my_inquiry(inquiry_id: str, user_id: str = student) -> InquiryDetail:
        return read_inquiry(inquiry_id, user_id)

    @router.get("/admin/inquiries/{inquiry_id}", dependencies=[admin])
    def get_inquiry(inquiry_id: str) -> InquiryDetail:
        return read_inquiry(inquiry_id)

    @router.post("/admin/inquiries/{inquiry_id}/replies", status_code=201, dependencies=[admin])
    def reply_to_inquiry(inquiry_id: str, payload: ReplyRequest) -> ReplyCreated:
        with engine.begin() as connection:
            claimed = connection.scalar(update(inquiries).where(
                inquiries.c.id == inquiry_id, inquiries.c.status == PENDING,
            ).values(status=ANSWERED).returning(inquiries.c.id))
            if claimed is not None:
                reply_id = str(uuid4())
                connection.execute(insert(inquiry_replies).values(
                    id=reply_id, inquiry_id=inquiry_id, **payload.model_dump(),
                ))
                return ReplyCreated(reply_id=reply_id)
            reply = connection.execute(select(
                inquiry_replies.c.id, inquiry_replies.c.request_id, inquiry_replies.c.body,
            ).where(inquiry_replies.c.inquiry_id == inquiry_id)).one_or_none()
            if reply is not None and reply.request_id == payload.request_id and reply.body == payload.body:
                return ReplyCreated(reply_id=reply.id)
            if connection.scalar(select(inquiries.c.id).where(inquiries.c.id == inquiry_id)) is None:
                raise HTTPException(404, "Inquiry not found")
            raise HTTPException(409, "Inquiry already answered")

    @router.get("/admin/members", dependencies=[admin])
    def list_members(
        q: str = "", status: Literal["active"] | None = None, cursor: str | None = None,
        sort: Literal["newest", "oldest"] = "newest",
    ) -> Page[MemberSummary]:
        statement = select(users.c.id, users.c.name, users.c.email).where(users.c.role == STUDENT_ROLE)
        if q := q.strip():
            statement = statement.where(users.c.name.icontains(q, autoescape=True) | users.c.email.icontains(q, autoescape=True))
        with engine.connect() as connection:
            rows, next_cursor = read_page(connection, users, statement, cursor, page_size, oldest_first=sort == "oldest")
        return Page[MemberSummary](
            items=[MemberSummary(member_id=row["id"], name=row["name"], email=row["email"]) for row in rows],
            next_cursor=next_cursor,
        )

    @router.get("/admin/members/{member_id}", dependencies=[admin])
    def get_member(member_id: str) -> MemberDetail:
        with engine.connect() as connection:
            member = connection.execute(select(*member_fields).where(
                users.c.id == member_id, users.c.role == STUDENT_ROLE,
            )).one_or_none()
        if member is None:
            raise HTTPException(404, "Member not found")
        return MemberDetail(member_id=member.id, name=member.name, email=member.email, version=member.version)

    @router.patch("/admin/members/{member_id}", dependencies=[admin])
    def edit_member(member_id: str, payload: MemberEdit) -> MemberUpdated:
        member = (users.c.id == member_id, users.c.role == STUDENT_ROLE)
        with engine.begin() as connection:
            version = connection.scalar(update(users).where(*member, users.c.version == payload.version)
                .values(name=payload.name, version=users.c.version + 1).returning(users.c.version))
            if version is None:
                if connection.scalar(select(users.c.id).where(*member)) is None:
                    raise HTTPException(404, "Member not found")
                raise HTTPException(409, "Member changed since it was loaded")
        return MemberUpdated(member_id=member_id, name=payload.name, version=version)
