import hashlib
import os
import re
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.errors import HeaderParseError
from email.headerregistry import Address
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, insert, literal, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.db.auth import auth_sessions, login_attempts, users

SESSION_COOKIE = "didgo_session"
DOMAIN_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def positive_setting(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 1:
        raise ValueError(f"{name} must be positive")
    return value


@dataclass(frozen=True)
class AuthSettings:
    password_min_length: int
    session_seconds: int
    remembered_session_seconds: int
    login_window_seconds: int
    login_attempt_limit: int

    @classmethod
    def from_env(cls) -> "AuthSettings":
        return cls(
            password_min_length=positive_setting("AUTH_PASSWORD_MIN_LENGTH", 8),
            session_seconds=positive_setting("AUTH_SESSION_SECONDS", 12 * 60 * 60),
            remembered_session_seconds=positive_setting(
                "AUTH_REMEMBERED_SESSION_SECONDS", 14 * 24 * 60 * 60
            ),
            login_window_seconds=positive_setting("AUTH_LOGIN_WINDOW_SECONDS", 60),
            login_attempt_limit=positive_setting("AUTH_LOGIN_ATTEMPT_LIMIT", 20),
        )


class Credentials(BaseModel):
    email: str
    password: str = Field(min_length=1, max_length=1024, repr=False)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        email = value.strip().lower()
        try:
            address = Address(addr_spec=email)
            domain = address.domain.encode("idna").decode("ascii")
        except (ValueError, HeaderParseError) as error:
            raise ValueError("Invalid email address") from error
        labels = domain.split(".")
        if (
            not address.username
            or len(address.username.encode()) > 64
            or len(email.encode()) > 254
            or len(labels) < 2
            or not all(DOMAIN_LABEL.fullmatch(label) for label in labels)
        ):
            raise ValueError("Invalid email address")
        return f"{address.addr_spec.rsplit('@', 1)[0]}@{domain}"


class SignupRequest(Credentials):
    name: str
    password_confirmation: str = Field(max_length=1024, repr=False)

    @field_validator("name")
    @classmethod
    def clean_name(cls, name: str) -> str:
        name = name.strip()
        if not name or "\0" in name:
            raise ValueError("Name is required")
        return name


class LoginRequest(Credentials):
    remember_me: bool = False


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024, repr=False)
    new_password: str = Field(min_length=1, max_length=1024, repr=False)
    password_confirmation: str = Field(max_length=1024, repr=False)


class WithdrawalRequest(BaseModel):
    password: str = Field(min_length=1, max_length=1024, repr=False)


class UserID(BaseModel):
    user_id: str


class Profile(UserID):
    name: str
    email: str


class AuthRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                response = await handler(request)
            except RequestValidationError as error:
                # Validation errors can include the raw password in the input field.
                response = JSONResponse(
                    status_code=422,
                    content={"detail": [
                        {key: item[key] for key in ("loc", "msg", "type")}
                        for item in error.errors()
                    ]},
                )
            except SQLAlchemyError:
                response = JSONResponse(status_code=503, content={"detail": "Database unavailable"})
            response.headers["Cache-Control"] = "no-store"
            return response

        return handle


def password_digest(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2**15, r=8, p=3, maxmem=64 * 1024**2)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    return f"scrypt${salt.hex()}${password_digest(password, salt).hex()}"


def password_matches(password: str, password_hash: str) -> bool:
    _, salt, expected = password_hash.split("$")
    return secrets.compare_digest(password_digest(password, bytes.fromhex(salt)), bytes.fromhex(expected))


def validate_new_password(password: str, confirmation: str, settings: AuthSettings) -> None:
    if password != confirmation:
        raise HTTPException(422, "Passwords do not match")
    if len(password) < settings.password_min_length:
        raise HTTPException(422, f"Password must be at least {settings.password_min_length} characters")


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def active_session(token_hash: str):
    return select(auth_sessions.c.user_id).where(
        auth_sessions.c.token_hash == token_hash,
        auth_sessions.c.expires_at > int(time.time()),
    )


def clear_session_cookie(request: Request) -> Response:
    response = Response(status_code=204)
    response.delete_cookie(
        SESSION_COOKIE, secure=request.url.scheme == "https", httponly=True, samesite="lax",
    )
    return response


def consume_login_attempt(engine: Engine, client: str, now: int, settings: AuthSettings) -> None:
    window = settings.login_window_seconds
    expires_at = (now // window + 1) * window
    dialect_insert = sqlite_insert if engine.dialect.name == "sqlite" else postgres_insert
    statement = dialect_insert(login_attempts).values(client=client, expires_at=expires_at, count=1)
    statement = statement.on_conflict_do_update(
        index_elements=[login_attempts.c.client, login_attempts.c.expires_at],
        set_={"count": login_attempts.c.count + 1},
        where=login_attempts.c.count < settings.login_attempt_limit,
    ).returning(login_attempts.c.count)
    with engine.begin() as connection:
        connection.execute(delete(login_attempts).where(login_attempts.c.expires_at <= now))
        allowed = connection.execute(statement).scalar_one_or_none()
    if allowed is None:
        raise HTTPException(429, "Too many login attempts", headers={"Retry-After": str(expires_at - now)})


def create_auth_router(engine: Engine) -> APIRouter:
    router = APIRouter(route_class=AuthRoute, tags=["auth"])
    settings = AuthSettings.from_env()
    dummy_salt = secrets.token_bytes(16)

    @router.post("/auth/signup", status_code=201, response_model=UserID)
    def signup(payload: SignupRequest) -> UserID:
        validate_new_password(payload.password, payload.password_confirmation, settings)
        user_id = str(uuid4())
        password_hash = hash_password(payload.password)
        try:
            with engine.begin() as connection:
                connection.execute(insert(users).values(
                    id=user_id, name=payload.name, email=payload.email,
                    password_hash=password_hash, created_at=datetime.now(UTC),
                ))
        except IntegrityError as error:
            with engine.connect() as connection:
                duplicate = connection.scalar(select(users.c.id).where(users.c.email == payload.email))
            if duplicate is not None:
                raise HTTPException(409, "Email already registered") from error
            raise
        return UserID(user_id=user_id)

    @router.post("/auth/login", response_model=UserID)
    def login(payload: LoginRequest, request: Request, response: Response) -> UserID:
        now = int(time.time())
        consume_login_attempt(engine, request.client.host if request.client else "", now, settings)
        with engine.connect() as connection:
            user = connection.execute(
                select(users.c.id, users.c.password_hash).where(users.c.email == payload.email)
            ).one_or_none()

        if user is None:
            password_digest(payload.password, dummy_salt)
            raise HTTPException(401, "Invalid email or password")
        if not password_matches(payload.password, user.password_hash):
            raise HTTPException(401, "Invalid email or password")

        duration = settings.remembered_session_seconds if payload.remember_me else settings.session_seconds
        token = secrets.token_urlsafe(32)
        with engine.begin() as connection:
            # Lock the matching account while issuing the session on PostgreSQL.
            # On SQLite the INSERT serializes this check with account changes.
            source = select(literal(token_digest(token)), users.c.id, literal(now + duration)).where(
                users.c.id == user.id, users.c.password_hash == user.password_hash,
            ).with_for_update(of=users)
            issued = connection.scalar(
                insert(auth_sessions).from_select(["token_hash", "user_id", "expires_at"], source)
                .returning(auth_sessions.c.user_id)
            )
            if issued is None:
                raise HTTPException(401, "Invalid email or password")
            connection.execute(delete(auth_sessions).where(auth_sessions.c.expires_at <= now))
        response.set_cookie(
            SESSION_COOKIE, token, max_age=duration if payload.remember_me else None,
            secure=request.url.scheme == "https", httponly=True, samesite="lax",
        )
        return UserID(user_id=user.id)

    @router.post("/auth/logout", status_code=204, response_class=Response)
    def logout(request: Request) -> Response:
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            with engine.begin() as connection:
                connection.execute(delete(auth_sessions).where(auth_sessions.c.token_hash == token_digest(token)))

        return clear_session_cookie(request)

    @router.get("/me", response_model=Profile)
    def me(request: Request) -> Profile:
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            with engine.connect() as connection:
                user = connection.execute(
                    select(users.c.id, users.c.name, users.c.email)
                    .select_from(auth_sessions.join(users))
                    .where(auth_sessions.c.token_hash == token_digest(token),
                           auth_sessions.c.expires_at > int(time.time()))
                ).one_or_none()
            if user is not None:
                return Profile(user_id=user.id, name=user.name, email=user.email)
        raise HTTPException(401, "Authentication required")

    def confirm_account_password(request: Request, password: str) -> tuple[str, str, str]:
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            raise HTTPException(401, "Authentication required")

        token_hash = token_digest(token)
        with engine.connect() as connection:
            user = connection.execute(select(users.c.id, users.c.password_hash).where(
                users.c.id == active_session(token_hash).scalar_subquery(),
            )).one_or_none()
        if user is None:
            raise HTTPException(401, "Authentication required")

        consume_login_attempt(engine, f"account:{user.id}", int(time.time()), settings)
        if not password_matches(password, user.password_hash):
            raise HTTPException(403, "Incorrect current password")
        return user.id, user.password_hash, token_hash

    @router.patch("/me/password", status_code=204, response_class=Response)
    def change_password(payload: PasswordChangeRequest, request: Request) -> Response:
        validate_new_password(payload.new_password, payload.password_confirmation, settings)
        user_id, previous_hash, token_hash = confirm_account_password(request, payload.current_password)
        new_hash = hash_password(payload.new_password)
        with engine.begin() as connection:
            changed = connection.scalar(update(users).where(
                users.c.id == user_id,
                users.c.password_hash == previous_hash,
                users.c.id == active_session(token_hash).scalar_subquery(),
            ).values(password_hash=new_hash).returning(users.c.id))
            if changed is None:
                raise HTTPException(401, "Authentication required")
            connection.execute(delete(auth_sessions).where(auth_sessions.c.user_id == user_id))
        return clear_session_cookie(request)

    @router.post("/me/withdrawal", status_code=204, response_class=Response)
    def withdraw(payload: WithdrawalRequest, request: Request) -> Response:
        user_id, password_hash, token_hash = confirm_account_password(request, payload.password)
        try:
            with engine.begin() as connection:
                removed = connection.scalar(delete(users).where(
                    users.c.id == user_id,
                    users.c.password_hash == password_hash,
                    users.c.id == active_session(token_hash).scalar_subquery(),
                ).returning(users.c.id))
                if removed is None:
                    raise HTTPException(401, "Authentication required")
        except IntegrityError as error:
            raise HTTPException(409, "Unable to withdraw account") from error
        return clear_session_cookie(request)

    return router
