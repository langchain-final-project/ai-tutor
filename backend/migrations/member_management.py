"""Add member edit versions and inquiry tables."""

import os

from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from app.db.connection import build_engine
from app.db.member_management import inquiries, inquiry_replies


def migrate(engine: Engine) -> None:
    with engine.begin() as connection:
        if not any(column["name"] == "version" for column in inspect(connection).get_columns("users")):
            connection.exec_driver_sql("ALTER TABLE users ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
        inquiries.create(connection, checkfirst=True)
        inquiry_replies.create(connection, checkfirst=True)


if __name__ == "__main__":
    engine = build_engine(os.environ["DATABASE_URL"])
    try:
        migrate(engine)
    finally:
        engine.dispose()
