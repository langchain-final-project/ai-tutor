"""Create the initial authentication tables using DATABASE_URL."""

import os

from app.db.auth import metadata
from app.db.connection import build_engine


def main() -> None:
    engine = build_engine(os.environ["DATABASE_URL"])
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
