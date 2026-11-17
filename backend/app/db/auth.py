from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, MetaData, String, Table, Text

metadata = MetaData()

users = Table(
    "users",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("name", Text, nullable=False),
    Column("email", String(254), nullable=False, unique=True),
    Column("password_hash", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

auth_sessions = Table(
    "auth_sessions",
    metadata,
    Column("token_hash", String(64), primary_key=True),
    Column("user_id", ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("expires_at", BigInteger, nullable=False, index=True),
)

login_attempts = Table(
    "login_attempts",
    metadata,
    Column("client", String(64), primary_key=True),
    Column("expires_at", BigInteger, primary_key=True, index=True),
    Column("count", BigInteger, nullable=False),
)
