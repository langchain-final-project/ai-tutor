# 풀이 버전·확인·검증·안내 기록을 연결하는 SQLite 개발 API

import sqlite3
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.db import solution_records as records

# 풀이 단계, 버전, 작업, 확인, 빈 본문 모델 정의
class StepBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=255, strict=True)
    text: str = Field(max_length=10000, strict=True)
    state: Literal["active", "deleted", "uncertain"]

    @field_validator("id") # ID가 공백이 아닌지 검증
    @classmethod
    def nonblank_id(cls, value):
        if not value.strip():
            raise ValueError("풀이 줄 ID가 필요합니다.")
        return value


class VersionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_problem_version_number: int = Field(ge=1, strict=True)
    expected_work_version_number: int = Field(ge=0, strict=True)

# 버전 모델 끝
class WorkBody(VersionBody):
    steps: list[StepBody] = Field(max_length=100)


class ConfirmWorkBody(VersionBody):
    expected_work_version_number: int = Field(ge=1, strict=True)


class EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


# 실제 API 라우터 생성 함수
def create_solution_router(database, current_owner) -> APIRouter:
    """소유자와 현재 버전을 검사하며 실제 모델은 호출하지 않음"""
    router = APIRouter(tags=["풀이 검증"])

    def call(function, owner_id, submission_id=None, *, write=False, **values):
        try:
            with records.solution_connection(database, write=write) as connection:
                if submission_id is not None:
                    values["submission_id"] = submission_id
                return function(
                    connection,
                    owner_id=owner_id,
                    **values,
                )
        except records.ConflictError as error:
            raise HTTPException(409, str(error)) from None
        except LookupError:
            raise HTTPException(404, "문제나 결과가 없거나 접근 권한이 없습니다.") from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        except sqlite3.Error:
            raise HTTPException(503, "DB 연결·마이그레이션 또는 사용 상태를 확인해 주세요.") from None

    @router.get("/submissions") # 소유자가 가진 제출 목록 조회
    def list_submissions(
        owner_id: Annotated[str, Depends(current_owner)],
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ):
        return call(records.list_owned_submissions, owner_id, limit=limit)

    @router.get("/submissions/{submission_id}/work") # 특정 제출의 작업 조회
    def read_work(
        submission_id: Annotated[int, Path(ge=1)],
        owner_id: Annotated[str, Depends(current_owner)],
    ):
        return call(records.get_work, owner_id, submission_id)

    @router.put("/submissions/{submission_id}/work", status_code=201) # 특정 제출의 작업 저장
    def save_work(
        submission_id: Annotated[int, Path(ge=1)],
        body: WorkBody,
        owner_id: Annotated[str, Depends(current_owner)],
    ):
        values = body.model_dump()
        return call(records.save_work, owner_id, submission_id, write=True, **values)

    @router.post("/submissions/{submission_id}/work/confirm") # 특정 제출의 작업 확인
    def confirm_work(
        submission_id: Annotated[int, Path(ge=1)],
        body: ConfirmWorkBody,
        owner_id: Annotated[str, Depends(current_owner)],
    ):
        return call(records.confirm_work, owner_id, submission_id, write=True, **body.model_dump())

    @router.post("/submissions/{submission_id}/verify", status_code=201) # 특정 제출의 검증 수행
    def verify(
        submission_id: Annotated[int, Path(ge=1)],
        body: VersionBody,
        owner_id: Annotated[str, Depends(current_owner)],
    ):
        return call(records.evaluate_solution, owner_id, submission_id, write=True, **body.model_dump())

    @router.get("/submissions/{submission_id}/verification") # 특정 제출의 현재 검증 상태 조회
    def current_verification(
        submission_id: Annotated[int, Path(ge=1)],
        owner_id: Annotated[str, Depends(current_owner)],
    ):
        return call(records.get_current_verification, owner_id, submission_id)

    @router.get("/submissions/{submission_id}/verification/history") # 특정 제출의 검증 이력 조회
    def verification_history(
        submission_id: Annotated[int, Path(ge=1)],
        owner_id: Annotated[str, Depends(current_owner)],
    ):
        return call(records.list_verifications, owner_id, submission_id)

    @router.get("/submissions/{submission_id}/verification/{result_id}") # 특정 검증 결과 조회
    def read_verification(
        submission_id: Annotated[int, Path(ge=1)],
        result_id: Annotated[int, Path(ge=1)],
        owner_id: Annotated[str, Depends(current_owner)],
    ):
        return call(records.get_verification, owner_id, submission_id, result_id=result_id)

    @router.post("/submissions/{submission_id}/verification/{result_id}/feedback-viewed") # 특정 검증 결과에 대한 피드백 확인 기록
    def feedback_viewed(
        submission_id: Annotated[int, Path(ge=1)],
        result_id: Annotated[int, Path(ge=1)],
        owner_id: Annotated[str, Depends(current_owner)],
        body: EmptyBody | None = None,
    ):
        return call(records.record_feedback_view, owner_id, submission_id, write=True, result_id=result_id)

    @router.get("/submissions/{submission_id}/feedback/history") # 특정 제출의 피드백 확인 이력 조회
    def feedback_history(
        submission_id: Annotated[int, Path(ge=1)],
        owner_id: Annotated[str, Depends(current_owner)],
    ):
        return call(records.list_feedback_views, owner_id, submission_id)

    return router
