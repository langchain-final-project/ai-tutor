"""사용자 확인·세션 버전·중복 요청을 지키며 실제 튜터 도움을 연결한다."""

from typing import Annotated, Literal
import logging
import sqlite3
from time import monotonic

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.db import tutor_sessions as records
from app.db.solution_records import ConflictError, solution_connection


logger = logging.getLogger(__name__)


class StartTutorBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_input_version_number: int = Field(ge=1, strict=True)
    target_text: str = Field(min_length=1, max_length=24000, strict=True)
    subject: str = Field(default='mathematics', min_length=1, max_length=80, strict=True)

    @field_validator('target_text', 'subject')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('공백만 있는 값은 사용할 수 없습니다.')
        return value.strip()


class TutorStepBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1, max_length=100, strict=True)
    text: str = Field(max_length=10000, strict=True)
    state: Literal['active', 'deleted', 'uncertain'] = 'active'


class TutorTurnBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str = Field(min_length=8, max_length=100, pattern=r'^[A-Za-z0-9_-]+$', strict=True)
    expected_revision: int = Field(ge=0, strict=True)
    intent: Literal['hint', 'submit', 'more_help', 'answer']
    student_text: str = Field(default='', max_length=10000, strict=True)
    steps: list[TutorStepBody] = Field(default_factory=list, max_length=100)

    @model_validator(mode='after')
    def valid_work(self):
        if len({step.id for step in self.steps}) != len(self.steps) or any(not step.id.strip() for step in self.steps):
            raise ValueError('풀이 줄에는 서로 다른 ID가 필요합니다.')
        if self.intent == 'submit' and not self.student_text.strip() and not self.steps:
            raise ValueError('풀이 또는 답변을 입력해 주세요.')
        if sum(len(step.text) for step in self.steps) + len(self.student_text) > 24000:
            raise ValueError('한 번의 풀이·답변은 24000자 이내로 입력해 주세요.')
        return self


def create_tutor_router(database, current_owner, *, runner=None):
    router = APIRouter(tags=['공통 튜터'])

    def call(function, owner_id, *, write=False, **values):
        try:
            with solution_connection(database, write=write) as connection:
                return function(connection, owner_id=owner_id, **values)
        except ConflictError as error:
            raise HTTPException(409, str(error)) from None
        except LookupError:
            raise HTTPException(404, '튜터 또는 문제가 없거나 접근 권한이 없습니다.') from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        except sqlite3.Error:
            raise HTTPException(503, '튜터 저장 구조를 준비한 뒤 다시 시도해 주세요.') from None

    @router.post('/submissions/{submission_id}/tutor-sessions', status_code=201)
    def start(submission_id: Annotated[int, Path(ge=1)], body: StartTutorBody,
              owner_id: Annotated[str, Depends(current_owner)]):
        return call(records.create, owner_id, write=True, submission_id=submission_id, **body.model_dump())

    @router.get('/tutor-sessions')
    def sessions(owner_id: Annotated[str, Depends(current_owner)],
                 limit: Annotated[int, Query(ge=1, le=100)] = 50):
        return call(records.list_sessions, owner_id, limit=limit)

    @router.get('/tutor-sessions/{session_id}')
    def read(session_id: Annotated[int, Path(ge=1)], owner_id: Annotated[str, Depends(current_owner)]):
        return call(records.snapshot, owner_id, session_id=session_id)

    @router.post('/tutor-sessions/{session_id}/turns')
    def turn(session_id: Annotated[int, Path(ge=1)], body: TutorTurnBody, response: Response,
             owner_id: Annotated[str, Depends(current_owner)]):
        payload = body.model_dump()
        reservation, cached = call(records.reserve, owner_id, write=True, session_id=session_id, request=payload)
        if cached is not None:
            if (cached.get('pending_request') or {}).get('status') == 'running':
                response.status_code = 202
            return cached
        from app.tutor.session_model import TutorModelError
        from app.tutor.session_service import run_turn

        started = monotonic()
        try:
            # 외부 모델 응답을 기다리는 동안 DB 연결·쓰기 잠금을 보유하지 않는다.
            result = (runner or run_turn)(reservation['problem'],
                                          reservation['state'], reservation['events'], payload)
            result['response'].setdefault('usage', {})['request_seconds'] = round(monotonic() - started, 4)
        except Exception as error:
            with solution_connection(database, write=True) as connection:
                records.fail(connection, session_id=session_id, reservation=reservation)
            if isinstance(error, (TutorModelError, TimeoutError)):
                logger.warning('튜터 모델 요청 실패: session=%s, reason=%s', session_id, error)
                raise HTTPException(503, '튜터 모델에 연결하지 못했어요. 저장한 입력으로 다시 시도해 주세요.') from None
            logger.exception('튜터 요청 처리 실패: session=%s', session_id)
            raise HTTPException(503, '도움 생성에 실패했어요. 입력을 보존했으니 다시 시도해 주세요.') from None
        saved = call(records.finish, owner_id, write=True, session_id=session_id,
                     reservation=reservation, result=result)
        if not saved:
            raise HTTPException(409, '문제나 요청 상태가 바뀌어 이전 응답을 공개하지 않았어요. 최신 기록을 확인해 주세요.')
        return call(records.snapshot, owner_id, session_id=session_id)

    return router
