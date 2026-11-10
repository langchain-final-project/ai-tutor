# Compose에서 전달한 DATABASE_URL로 DB 엔진을 구성

import sqlite3

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url

# 데이터베이스 엔진을 구성하는 함수
# database_url: 데이터베이스 연결 URL
# 반환값: 구성된 SQLAlchemy 엔진
def build_engine(database_url: str) -> Engine:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        return create_engine(url, connect_args={"connect_timeout": 3})

    engine = create_engine(url, connect_args={"autocommit": False})


    # SQLite 연결 시 외래키 검사를 활성화하는 이벤트 리스너
    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection: sqlite3.Connection, _record: object) -> None:
        # SQLite는 연결마다 외래키 검사를 활성화해야 합니다.
        # PRAGMA는 트랜잭션 밖에서 적용하고 이후 원래 모드로 되돌립니다.
        previous = connection.autocommit
        connection.autocommit = True
        try:
            cursor = connection.execute("PRAGMA foreign_keys=ON")
            cursor.close()
        finally:
            connection.autocommit = previous

    return engine
