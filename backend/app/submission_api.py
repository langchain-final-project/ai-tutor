# 텍스트·사진 질문 접수와 확인 API

from contextlib import asynccontextmanager
from io import BytesIO
import os
from pathlib import Path
import secrets
import sqlite3
from typing import Annotated
from urllib.parse import quote
import warnings

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from app.db.submissions import (
    create_photo_submission,
    create_text_submission,
    get_input_version_history,
    get_ocr_history,
    get_submission,
    get_uploaded_photo,
    save_and_confirm_input,
    save_student_input_version,
    submission_connection,
)
from app.ocr_api import create_ocr_router
from app.ocr_jobs import OCRJobManager, build_qwen_pipeline
from app.solution_api import create_solution_router

ROOT = Path(__file__).resolve().parents[2]
MAX_IMAGE_BYTES = 30 * 1024 * 1024
bearer = HTTPBearer(auto_error=False)


class InputBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_text: str = Field(
        min_length=1, max_length=24000, strict=True
    )
    expected_version_number: int = Field(ge=1, strict=True)


class TextSubmissionBody(BaseModel):
    """현재 텍스트 검증 흐름에는 실수 범위의 등식 한 개를 입력한다."""

    model_config = ConfigDict(extra="forbid")
    content_text: str = Field(min_length=1, max_length=10000, strict=True)


def validate_photo(content: bytes) -> str:
    # 실제 이미지 내용을 검사하고 MIME 형식을 반환
    if not content:
        raise HTTPException(400, "빈 사진 파일입니다.")

    if len(content) > MAX_IMAGE_BYTES:
        raise HTTPException(413, "사진은 30MB 이하여야 합니다.")

    formats = {
        "JPEG": "image/jpeg",
        "PNG": "image/png",
        "WEBP": "image/webp",
    }

    try:
        with warnings.catch_warnings():
            warnings.simplefilter(
                "error", Image.DecompressionBombWarning
            )

            with Image.open(BytesIO(content)) as image:
                mime_type = formats.get(image.format)

                if mime_type is None:
                    raise HTTPException(
                        415, "JPEG·PNG·WebP 사진을 사용해 주세요."
                    )

                if image.width * image.height > 20_000_000:
                    raise HTTPException(
                        413, "사진은 2천만 픽셀 이하여야 합니다."
                    )

                if getattr(image, "n_frames", 1) != 1:
                    raise HTTPException(
                        415, "움직이는 이미지 대신 사진을 사용해 주세요."
                    )

                image.verify()

            with Image.open(BytesIO(content)) as image:
                image.load()

    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        raise HTTPException(
            413, "이미지 크기가 너무 큽니다."
        ) from None

    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        SyntaxError,
    ):
        raise HTTPException(
            400, "이미지를 정상적으로 읽을 수 없습니다."
        ) from None

    return mime_type


def create_submission_app(
    database: str | Path | None = None,
    *,
    dev_token: str | None = None,
    owner_id: str | None = None,
    pipeline_factory=build_qwen_pipeline,
) -> FastAPI:
    database = Path(database) if database is not None else Path(
        os.getenv("DIDGO_DATABASE", str(ROOT / "data/problem_bank/didgo.sqlite3"))
    )

    token = (
        dev_token
        if dev_token is not None
        else os.getenv("DIDGO_API_TOKEN")
    )
    owner = (
        owner_id
        if owner_id is not None
        else os.getenv("DIDGO_DEV_USER_ID", "local-student")
    )

    manager = OCRJobManager(database, pipeline_factory=pipeline_factory)

    @asynccontextmanager
    async def lifespan(_app):
        # DB 복구와 모델 작업의 종료 대기는 별도 스레드에서 처리 진행
        await run_in_threadpool(manager.start)
        try:
            yield
        finally:
            await run_in_threadpool(manager.shutdown)

    app = FastAPI(title="딛고 질문·풀이 검증 API", lifespan=lifespan)
    app.state.ocr_jobs = manager

    def current_owner(
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Depends(bearer),
        ],
    ) -> str:
        if not token or not owner.strip():
            raise HTTPException(
                503, "서버의 개발용 계정 설정이 필요합니다."
            )

        if credentials is None or not secrets.compare_digest(
            credentials.credentials.encode("utf-8"),
            token.encode("utf-8"),
        ):
            raise HTTPException(
                401,
                "인증이 필요합니다.",
                headers={"WWW-Authenticate": "Bearer"},
            )

        return owner

    @app.exception_handler(LookupError)
    async def missing_submission(_request, _error):
        return JSONResponse(
            status_code=404,
            content={
                "detail": "문제가 없거나 접근 권한이 없습니다."
            },
        )

    @app.exception_handler(ValueError)
    async def invalid_input(_request, error):
        return JSONResponse(
            status_code=422,
            content={"detail": str(error)},
        )

    @app.exception_handler(sqlite3.IntegrityError)
    async def conflicting_data(_request, _error):
        return JSONResponse(
            status_code=409,
            content={
                "detail": "저장 상태가 충돌했습니다. 다시 조회해 주세요."
            },
        )

    @app.exception_handler(sqlite3.OperationalError)
    async def unavailable_database(_request, _error):
        return JSONResponse(
            status_code=503,
            content={
                "detail": "DB 연결·마이그레이션 또는 사용 상태를 확인해 주세요."
            },
        )

    def check_editable(
        connection, submission_id, student_id, expected
    ):
        current = get_submission(
            connection,
            submission_id=submission_id,
            owner_id=student_id,
        )
        latest = current["latest_input"]

        if latest is None:
            raise HTTPException(
                409, "OCR 처리가 완료된 뒤 내용을 수정해 주세요."
            )

        if latest["version_number"] != expected:
            raise HTTPException(
                409, "내용이 변경되었습니다. 다시 조회해 주세요."
            )

    @app.get("/health")
    def health():
        with submission_connection(database) as connection:
            connection.execute("SELECT 1")

        return {"status": "ok", "database": "sqlite"}

    @app.post("/submissions/text", status_code=201)
    def submit_text(
        body: TextSubmissionBody,
        student_id: Annotated[str, Depends(current_owner)],
    ):
        """질문과 확인 대기 중인 첫 입력 버전을 함께 저장한다."""
        with submission_connection(database) as connection:
            ids = create_text_submission(
                connection, owner_id=student_id, content_text=body.content_text,
            )
            return get_submission(
                connection, submission_id=ids["submission_id"], owner_id=student_id,
            )

    @app.post("/submissions/photo", status_code=201)
    def upload_photo(
        files: Annotated[
            list[UploadFile],
            File(min_length=1, max_length=1),
        ],
        student_id: Annotated[str, Depends(current_owner)],
    ):
        upload = files[0]

        try:
            content = upload.file.read(MAX_IMAGE_BYTES + 1)
            mime_type = validate_photo(content)

            filename = Path(
                (upload.filename or "photo").replace("\\", "/")
            ).name

            if not filename.strip() or len(filename) > 255:
                raise HTTPException(422, "파일명을 확인해 주세요.")

            with submission_connection(database) as connection:
                ids = create_photo_submission(
                    connection,
                    owner_id=student_id,
                    original_filename=filename,
                    mime_type=mime_type,
                    image_content=content,
                )

                return get_submission(
                    connection,
                    submission_id=ids["submission_id"],
                    owner_id=student_id,
                )

        finally:
            upload.file.close()

    @app.get("/submissions/{submission_id}")
    def read_submission(
        submission_id: int,
        student_id: Annotated[str, Depends(current_owner)],
    ):
        with submission_connection(database) as connection:
            return get_submission(
                connection,
                submission_id=submission_id,
                owner_id=student_id,
            )

    @app.get("/submissions/{submission_id}/photo")
    def read_photo(
        submission_id: int,
        student_id: Annotated[str, Depends(current_owner)],
    ):
        with submission_connection(database) as connection:
            photo = get_uploaded_photo(
                connection,
                submission_id=submission_id,
                owner_id=student_id,
            )

        return Response(
            content=photo["image_content"],
            media_type=photo["mime_type"],
            headers={
                "Content-Disposition":
                    "inline; filename*=UTF-8''"
                    + quote(photo["original_filename"])
            },
        )

    @app.get("/submissions/{submission_id}/input/history")
    def read_input_history(
        submission_id: int,
        student_id: Annotated[str, Depends(current_owner)],
    ):
        with submission_connection(database) as connection:
            return get_input_version_history(
                connection,
                submission_id=submission_id,
                owner_id=student_id,
            )

    @app.get("/submissions/{submission_id}/ocr/history")
    def read_ocr_history(
        submission_id: int,
        student_id: Annotated[str, Depends(current_owner)],
    ):
        with submission_connection(database) as connection:
            return get_ocr_history(
                connection,
                submission_id=submission_id,
                owner_id=student_id,
            )

    @app.patch("/submissions/{submission_id}/input")
    def edit_input(
        submission_id: int,
        body: InputBody,
        student_id: Annotated[str, Depends(current_owner)],
    ):
        with submission_connection(database) as connection:
            check_editable(
                connection,
                submission_id,
                student_id,
                body.expected_version_number,
            )

            save_student_input_version(
                connection,
                submission_id=submission_id,
                owner_id=student_id,
                content_text=body.content_text,
                expected_version_number=body.expected_version_number,
            )

            return get_submission(
                connection,
                submission_id=submission_id,
                owner_id=student_id,
            )

    @app.post("/submissions/{submission_id}/confirm")
    def confirm_input(
        submission_id: int,
        body: InputBody,
        student_id: Annotated[str, Depends(current_owner)],
    ):
        with submission_connection(database) as connection:
            check_editable(
                connection,
                submission_id,
                student_id,
                body.expected_version_number,
            )

            save_and_confirm_input(
                connection,
                submission_id=submission_id,
                owner_id=student_id,
                content_text=body.content_text,
                expected_version_number=body.expected_version_number,
            )

            return get_submission(
                connection,
                submission_id=submission_id,
                owner_id=student_id,
            )

    app.include_router(create_ocr_router(manager, current_owner))
    app.include_router(create_solution_router(database, current_owner))

    return app


app = create_submission_app()
