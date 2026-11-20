# 회원 세션과 질문·풀이·OCR API를 같은 SQLite DB에 연결

from contextlib import asynccontextmanager
import os
from pathlib import Path

from fastapi import FastAPI, Request
from sqlalchemy.engine import URL

from app.auth import create_auth_router, require_session_profile
from app.db.connection import build_engine
from app.ocr_jobs import build_qwen_pipeline
from app.submission_api import create_submission_app


ROOT = Path(__file__).resolve().parents[2]


def create_student_app(
    database: str | Path | None = None,
    *,
    pipeline_factory=build_qwen_pipeline,
    tutor_runner=None,
) -> FastAPI:
    """학생 API는 세션 쿠키만 사용하며 개발용 Bearer 토큰으로 우회하지 않는다."""
    database = Path(database if database is not None else os.getenv(
        "DIDGO_DATABASE", str(ROOT / "data/local/solution-flow.sqlite3")
    )).resolve()
    engine = build_engine(URL.create("sqlite", database=str(database)))

    def current_owner(request: Request) -> str:
        return require_session_profile(engine, request).user_id

    try:
        app = create_submission_app(
            database, owner_dependency=current_owner, pipeline_factory=pipeline_factory,
            tutor_runner=tutor_runner,
        )
        app.include_router(create_auth_router(engine))
    except BaseException:
        engine.dispose()
        raise

    submission_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(current_app):
        try:
            async with submission_lifespan(current_app):
                yield
        finally:
            engine.dispose()

    app.router.lifespan_context = lifespan
    app.title = "딛고 학생 API"
    app.state.auth_engine = engine
    return app


app = create_student_app()
