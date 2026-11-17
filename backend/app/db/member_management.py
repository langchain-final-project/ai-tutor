from sqlalchemy import Column, DateTime, ForeignKey, Index, String, Table, Text

from app.db.auth import metadata, users

PENDING = "pending"
ANSWERED = "answered"

inquiries = Table(
    "inquiries",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("user_id", ForeignKey(users.c.id, ondelete="CASCADE"), nullable=False, index=True),
    Column("category", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("body", Text, nullable=False),
    Column("status", String(16), nullable=False, server_default=PENDING),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Index("ix_inquiries_status_created_id", "status", "created_at", "id"),
)

inquiry_replies = Table(
    "inquiry_replies",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("inquiry_id", ForeignKey(inquiries.c.id, ondelete="CASCADE"), nullable=False, unique=True),
    Column("request_id", Text, nullable=False),
    Column("body", Text, nullable=False),
)
