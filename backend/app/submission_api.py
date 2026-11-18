"""사진 제출·조회·수정·확인을 위한 로컬 개발 API."""

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
from app.ocr.images import open_image
from app.ocr.selection import crop_photo_region
from app.ocr.quality import check_photo_quality, requires_photo_retake
from app.db.type_matches import ensure_type_match_schema, load_type_match, save_type_match
from app.tutor.unit_type_matcher import DEFAULT_BANK, VERSION as TYPE_MATCH_VERSION, get_unit_matcher

ROOT = Path(__file__).resolve().parents[2]
MAX_IMAGE_BYTES = 30 * 1024 * 1024
bearer = HTTPBearer(auto_error=False)


class InputBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_text: str = Field(
        min_length=1, max_length=24000, strict=True
    )
    expected_version_number: int = Field(ge=1, strict=True)


class TypeMatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version_number: int = Field(ge=1, strict=True)


class TypeChoiceBody(TypeMatchBody):
    source_topic_code: str = Field(pattern=r"^[0-9]{7}$", strict=True)


def validate_photo(content: bytes) -> str:
    """실제 이미지 내용을 검사하고 MIME 형식을 반환한다."""
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
    type_bank_database: str | Path | None = None,
) -> FastAPI:
    database = Path(database) if database is not None else (
        ROOT / "data/problem_bank/didgo.sqlite3"
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
    type_bank = Path(type_bank_database or os.getenv("DIDGO_TYPE_BANK_DATABASE", str(DEFAULT_BANK)))

    @asynccontextmanager
    async def lifespan(_app):
        with submission_connection(database) as connection:
            ensure_type_match_schema(connection)
        # DB 복구와 모델 작업의 종료 대기는 별도 스레드에서 처리한다.
        await run_in_threadpool(manager.start)
        try:
            yield
        finally:
            await run_in_threadpool(manager.shutdown)

    app = FastAPI(title="딛고 사진 제출 API", lifespan=lifespan)
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

    @app.post("/submissions/photo", status_code=201)
    def upload_photo(
        files: Annotated[
            list[UploadFile],
            File(min_length=1, max_length=1, json_schema_extra={"items": {
                "type":"string", "format":"binary",
            }}),
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

            photo_quality = check_photo_quality(content)
            photo_quality["retake_required"] = requires_photo_retake(photo_quality)

            # 재촬영 대상은 제출 ID를 발급하지 않아 OCR을 시작할 수 없다.
            if photo_quality["retake_required"]:
                raise HTTPException(422, detail={
                    "code": "photo_retake_required",
                    "message": "문제 전체가 선명하게 보이도록 사진을 다시 촬영해 주세요.",
                    "photo_quality": photo_quality,
                })

            with submission_connection(database) as connection:
                ids = create_photo_submission(
                    connection,
                    owner_id=student_id,
                    original_filename=filename,
                    mime_type=mime_type,
                    image_content=content,
                )

                submission = get_submission(
                    connection,
                    submission_id=ids["submission_id"],
                    owner_id=student_id,
                )

            submission["photo_quality"] = photo_quality
            return submission

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
        corrected: bool = False,
        selected: bool = False,
    ):
        with submission_connection(database) as connection:
            photo = get_uploaded_photo(
                connection,
                submission_id=submission_id,
                owner_id=student_id,
            )

            rotation = 0
            region = None
            if corrected or selected:
                current = get_submission(
                    connection, submission_id=submission_id, owner_id=student_id,
                )
                if corrected:
                    rotation = current["image_rotation_ccw"]
                if selected:
                    region = current["selected_region"]

        if corrected or selected:
            content = crop_photo_region(photo["image_content"], region) if region else photo["image_content"]
            with open_image(content) as image:
                with image.rotate(rotation, expand=True) as upright:
                    output = BytesIO()
                    upright.save(output, format="PNG")
            suffix = "-selected" if region else ""
            filename = Path(photo["original_filename"]).stem + suffix + "-upright.png"
            return Response(
                content=output.getvalue(),
                media_type="image/png",
                headers={"Content-Disposition": "inline; filename*=UTF-8''" + quote(filename)},
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

    def confirmed_type_input(connection, submission_id, student_id, expected):
        current = get_submission(connection, submission_id=submission_id, owner_id=student_id)
        latest = current["latest_input"]
        if not latest or latest["version_number"] != expected:
            raise HTTPException(409, "문제 내용이 변경되었습니다. 최신 내용을 불러와 주세요.")
        if not latest["confirmed_at"]:
            raise HTTPException(409, "본문과 수식을 확인하고 문제 내용 확인 완료를 먼저 눌러주세요.")
        return current, latest

    def type_matcher():
        try:
            return get_unit_matcher(type_bank)
        except (OSError, sqlite3.Error, ValueError) as error:
            raise HTTPException(503, "문제은행을 불러오지 못했습니다. 문자와 식 문항이 있는 DB를 확인해 주세요.") from error

    @app.post("/submissions/{submission_id}/type-match")
    def match_type(submission_id: int, body: TypeMatchBody, student_id: Annotated[str, Depends(current_owner)]):
        with submission_connection(database) as connection:
            current, latest = confirmed_type_input(connection, submission_id, student_id, body.expected_version_number)
            cached = load_type_match(connection, latest["id"])
        matcher = type_matcher()
        reusable = cached and cached.get("data_version") == matcher.data_version and cached.get("classifier_version") == TYPE_MATCH_VERSION
        if reusable:
            result = cached
        else:
            layout = (current.get("layout_detection") or {}).get("layout") or {}
            has_visuals = any(region["kind"] in {"figure", "graph", "table"} for region in layout.get("regions", []))
            result = matcher.predict(latest["content_text"], has_visuals=has_visuals)
        with submission_connection(database, immediate=True) as connection:
            _, latest = confirmed_type_input(connection, submission_id, student_id, body.expected_version_number)
            existing = load_type_match(connection, latest["id"])
            if existing and existing.get("data_version") == matcher.data_version and existing.get("classifier_version") == TYPE_MATCH_VERSION:
                return existing
            return save_type_match(connection, latest, result)

    @app.post("/submissions/{submission_id}/type-match/confirm")
    def choose_type(submission_id: int, body: TypeChoiceBody, student_id: Annotated[str, Depends(current_owner)]):
        with submission_connection(database, immediate=True) as connection:
            _, latest = confirmed_type_input(connection, submission_id, student_id, body.expected_version_number)
            result = load_type_match(connection, latest["id"])
            if not result or result["status"] not in {"classified", "candidates"}:
                raise HTTPException(409, "유형 후보를 먼저 찾아주세요.")
            candidate = next((item for item in result["candidates"] if item["source_topic_code"] == body.source_topic_code), None)
            if candidate is None:
                raise HTTPException(422, "현재 문제의 문자와 식 유형 후보 중에서 선택해 주세요.")
            result = {**result, "status": "classified", "method": "manual", "matched_question_id": None,
                      "selected_type": {key: candidate[key] for key in ("source_topic_code", "source_topic_name")},
                      "reasons": ["확인된 문제 내용의 유형 후보 중 사용자가 선택했습니다."]}
            return save_type_match(connection, latest, result)

    app.include_router(create_ocr_router(manager, current_owner))

    return app


app = create_submission_app()
