"""Create a new administrator in the configured database."""

import argparse
import os
from datetime import UTC, datetime
from getpass import getpass
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from app.auth import AuthSettings, SignupRequest, hash_password, validate_new_password
from app.db.auth import ADMIN_ROLE, users
from app.db.connection import build_engine


def create_admin(engine: Engine, account: SignupRequest) -> None:
    validate_new_password(account.password, account.password_confirmation, AuthSettings.from_env())
    try:
        with engine.begin() as connection:
            connection.execute(insert(users).values(
                id=str(uuid4()), name=account.name, email=account.email,
                password_hash=hash_password(account.password), created_at=datetime.now(UTC),
                role=ADMIN_ROLE,
            ))
    except IntegrityError:
        with engine.connect() as connection:
            if connection.scalar(select(users.c.id).where(users.c.email == account.email)):
                raise SystemExit("That email is already registered.") from None
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    try:
        account = SignupRequest(
            email=args.email, name=args.name,
            password=getpass("Password: "), password_confirmation=getpass("Confirm password: "),
        )
    except ValidationError as error:
        fields = ", ".join(str(item["loc"][0]) for item in error.errors())
        parser.error(f"Invalid account fields: {fields}")

    engine = build_engine(os.environ["DATABASE_URL"])
    try:
        create_admin(engine, account)
    except HTTPException as error:
        parser.error(error.detail)
    finally:
        engine.dispose()
    print(f"Administrator created: {account.email}")


if __name__ == "__main__":
    main()
