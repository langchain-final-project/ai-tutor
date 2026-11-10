# 딛고 FastAPI 진입점과 데이터베이스 상태 확인 API

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.db.connection import build_engine


# FastAPI 애플리케이션 생성 함수
# database_url: 데이터베이스 연결 URL
# 반환값: 구성된 FastAPI 애플리케이션
def create_app(database_url: str | None = None) -> FastAPI:
    # Compose 밖에서 직접 실행할 때는 현재 작업 폴더에 로컬 SQLite 파일을 사용합니다.
    engine = build_engine(database_url or os.getenv("DATABASE_URL", "sqlite:///./didgo.sqlite3"))

    # FastAPI 애플리케이션의 수명 주기 관리
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            engine.dispose()

    app = FastAPI(title="딛고 API", lifespan=lifespan)

    @app.get("/health")
    # 헬스 체크 엔드포인트
    # 반환값: 상태 정보와 사용 중인 데이터베이스 종류
    def health() -> dict[str, str]:
        # API 응답뿐 아니라 현재 환경에서 선택된 DB에 실제로 쿼리할 수 있는지 확인
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError as error:
            raise HTTPException(status_code=503, detail="Database unavailable") from error

        return {"status": "ok", "database": engine.dialect.name}

    return app


app = create_app()
