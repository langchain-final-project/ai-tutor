"""로그인 사용자의 최소 학습 경로 생성·재개·다음 시도를 연결한다."""

from typing import Annotated
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from app.db import learning_paths as records
from app.db.solution_records import ConflictError, solution_connection
from app.tutor.unit_type_matcher import DEFAULT_BANK


class StartLearningBody(BaseModel):
    model_config = ConfigDict(extra='forbid')


class NextLearningBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=0, strict=True)


def create_learning_router(database, current_owner, *, bank_database=DEFAULT_BANK):
    router = APIRouter(tags=['학습 경로'])

    def call(function, owner_id, *, write=False, **values):
        try:
            with solution_connection(database, write=write) as connection:
                return function(connection, owner_id=owner_id, bank_database=bank_database, **values)
        except ConflictError as error:
            raise HTTPException(409, str(error)) from None
        except LookupError:
            raise HTTPException(404, '학습 경로 또는 문제가 없거나 접근 권한이 없습니다.') from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        except sqlite3.Error:
            raise HTTPException(503, '학습 저장 구조를 준비한 뒤 다시 시도해 주세요.') from None

    @router.get('/tutor-sessions/{session_id}/learning-path')
    def for_session(session_id: Annotated[int, Path(ge=1)], owner_id: Annotated[str, Depends(current_owner)]):
        return call(records.get_for_session, owner_id, session_id=session_id)

    @router.post('/tutor-sessions/{session_id}/learning-path')
    def start(session_id: Annotated[int, Path(ge=1)], body: StartLearningBody,
              owner_id: Annotated[str, Depends(current_owner)]):
        return call(records.create, owner_id, write=True, source_session_id=session_id)

    @router.get('/learning-paths')
    def listing(owner_id: Annotated[str, Depends(current_owner)], limit: Annotated[int, Query(ge=1, le=100)] = 50):
        return call(records.list_paths, owner_id, limit=limit)

    @router.get('/learning-paths/{path_id}')
    def read(path_id: Annotated[int, Path(ge=1)], owner_id: Annotated[str, Depends(current_owner)]):
        return call(records.snapshot, owner_id, path_id=path_id)

    @router.post('/learning-paths/{path_id}/next')
    def next_activity(path_id: Annotated[int, Path(ge=1)], body: NextLearningBody,
                      owner_id: Annotated[str, Depends(current_owner)]):
        return call(records.advance, owner_id, write=True, path_id=path_id, **body.model_dump())

    return router
