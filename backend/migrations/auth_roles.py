"""Add account roles to existing authentication tables."""

import os

from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from app.db.auth import STUDENT_ROLE, USER_ROLE_CHECK
from app.db.connection import build_engine


def migrate(engine: Engine) -> None:
    with engine.begin() as connection:
        if any(column["name"] == "role" for column in inspect(connection).get_columns("users")):
            return
        connection.exec_driver_sql(
            f"ALTER TABLE users ADD COLUMN role VARCHAR(16) NOT NULL DEFAULT '{STUDENT_ROLE}' "
            f"CONSTRAINT ck_users_role CHECK ({USER_ROLE_CHECK})"
        )


if __name__ == "__main__":
    engine = build_engine(os.environ["DATABASE_URL"])
    try:
        migrate(engine)
    finally:
        engine.dispose()
