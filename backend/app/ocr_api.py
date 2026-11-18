"""OCR 작업 시작·상태 조회·진행 이벤트 스트리밍 API."""

import asyncio
import json
import logging
import sqlite3
from time import monotonic
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from app.ocr_jobs import OCRJobManager
from app.ocr.selection import InvalidPhotoRegion

logger = logging.getLogger(__name__)
MAX_EVENT_ID = 2**63 - 1
SSE_POLL_SECONDS = 0.25
SSE_HEARTBEAT_SECONDS = 10.0


def _encode_event(event) -> str:
    event_type = event["event_type"]
    if "\r" in event_type or "\n" in event_type:
        raise ValueError("이벤트 이름에 줄바꿈을 사용할 수 없습니다.")
    payload = json.dumps(
        event["payload"], ensure_ascii=False, allow_nan=False
    )
    return f"id: {event['id']}\nevent: {event_type}\ndata: {payload}\n\n"


async def _stream_events(request, manager, job_id, owner_id, after_id):
    # 연결이 끊겨도 OCR 작업은 계속된다. 재연결 시 저장된 이벤트를 재생한다.
    cursor = after_id
    last_sent = monotonic()
    yield "retry: 1000\n\n"

    while not await request.is_disconnected():
        try:
            job, events = await run_in_threadpool(
                manager.read_events, job_id, owner_id, cursor
            )
            for event in events:
                yield _encode_event(event)
                cursor = event["id"]
                last_sent = monotonic()
        except (sqlite3.Error, LookupError, ValueError):
            logger.exception("OCR 스트리밍 조회 실패: %s", job_id)
            payload = json.dumps(
                {
                    "job_id": job_id,
                    "message": "이벤트를 읽지 못했습니다. 마지막 이벤트 번호로 다시 연결해 주세요.",
                },
                ensure_ascii=False,
            )
            # 재연결 커서가 이동하지 않도록 이 알림에는 id를 붙이지 않는다.
            yield f"event: stream_error\ndata: {payload}\n\n"
            return

        # 한 번에 최대 200개를 읽으므로, 완료 상태여도 남은 기록을 먼저 보낸다.
        if events:
            continue
        if job["status"] in {"completed", "failed"}:
            return

        if monotonic() - last_sent >= SSE_HEARTBEAT_SECONDS:
            yield ": keep-alive\n\n"
            last_sent = monotonic()

        # DB 연결은 read_events 안에서 이미 닫혔으며, 대기 중에는 잡고 있지 않다.
        await asyncio.sleep(SSE_POLL_SECONDS)


PositivePixel = Annotated[int, Field(strict=True, gt=0)]
Pixel = Annotated[int, Field(strict=True, ge=0)]


class PhotoRegionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    coordinate_space: Literal["exif_transposed"]
    image_size: tuple[PositivePixel, PositivePixel]
    bbox: tuple[Pixel, Pixel, Pixel, Pixel]


class OCRJobBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    region: PhotoRegionInput | None = None


class OCRJobResponse(BaseModel):
    id: str
    submission_id: int
    status: Literal["queued", "running", "completed", "failed"]
    ocr_result_id: int | None
    error_message: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    selected_region: PhotoRegionInput | None = None


def create_ocr_router(manager: OCRJobManager, current_owner) -> APIRouter:
    router = APIRouter(tags=["OCR 작업"])

    @router.post(
        "/submissions/{submission_id}/ocr",
        status_code=202,
        response_model=OCRJobResponse,
        summary="사진의 OCR 작업 시작",
    )
    def start_ocr(
        submission_id: Annotated[int, Path(ge=1)],
        student_id: Annotated[str, Depends(current_owner)],
        body: OCRJobBody | None = None,
    ):
        try:
            return manager.submit(
                submission_id=submission_id,
                owner_id=student_id,
                selected_region=body.region.model_dump(mode="json") if body and body.region else None,
            )
        except InvalidPhotoRegion as error:
            raise HTTPException(422, str(error)) from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        except RuntimeError as error:
            raise HTTPException(
                503, "OCR 작업을 시작할 수 없습니다. 잠시 후 다시 시도해 주세요."
            ) from error

    @router.get(
        "/ocr-jobs/{job_id}",
        response_model=OCRJobResponse,
        summary="OCR 작업 상태 조회",
    )
    def read_ocr_job(
        job_id: str,
        student_id: Annotated[str, Depends(current_owner)],
    ):
        return manager.get_job(job_id, student_id)

    @router.get(
        "/ocr-jobs/{job_id}/events",
        response_class=StreamingResponse,
        responses={
            200: {
                "description": "저장된 진행 이벤트를 순서대로 전송하고 새 이벤트를 기다립니다.",
                "content": {"text/event-stream": {"schema": {"type": "string"}}},
            },
            400: {"description": "Last-Event-ID가 올바르지 않습니다."},
            401: {"description": "인증이 필요합니다."},
            404: {"description": "작업이 없거나 접근 권한이 없습니다."},
        },
        summary="OCR 진행 이벤트 스트리밍",
    )
    async def stream_ocr_events(
        job_id: str,
        request: Request,
        student_id: Annotated[str, Depends(current_owner)],
        after_id: Annotated[int, Query(ge=0, le=MAX_EVENT_ID)] = 0,
        last_event_id: Annotated[
            str | None, Header(alias="Last-Event-ID")
        ] = None,
    ):
        cursor = after_id
        if last_event_id not in (None, ""):
            try:
                cursor = int(last_event_id)
            except ValueError:
                raise HTTPException(400, "Last-Event-ID는 이벤트 번호여야 합니다.") from None
            if not 0 <= cursor <= MAX_EVENT_ID:
                raise HTTPException(400, "Last-Event-ID의 범위가 올바르지 않습니다.")

        # 스트리밍 응답 헤더를 보내기 전에 작업 존재 여부와 소유자를 확인한다.
        await run_in_threadpool(manager.get_job, job_id, student_id)
        return StreamingResponse(
            _stream_events(request, manager, job_id, student_id, cursor),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return router
