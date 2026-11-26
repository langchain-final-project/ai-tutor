# 텍스트·사진 질문 접수와 확인 API

from contextlib import asynccontextmanager
from io import BytesIO
import logging
import os
from pathlib import Path
import secrets
import sqlite3
from time import monotonic
from typing import Annotated, Callable
from urllib.parse import quote
import warnings

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from app.db.submissions import (
    create_equation_submission,
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
from app.db import tutor_sessions
from app.ocr_api import create_ocr_router
from app.ocr_jobs import OCRJobManager, build_qwen_backend, build_qwen_pipeline
from app.solution_api import create_solution_router
from app.tutor_api import create_tutor_router
from app.learning_api import create_learning_router
from app.ocr.images import open_image
from app.ocr.llama import OCRError
from app.ocr.selection import crop_photo_region
from app.ocr.quality import check_photo_quality, requires_photo_retake
from app.db.type_matches import ensure_type_match_schema, load_type_match, save_type_match
from app.tutor.unit_type_matcher import (
    AUTOMATIC_POLICY_VERSION,
    DEFAULT_BANK,
    SCOPE,
    VERSION as TYPE_MATCH_VERSION,
    classify_for_tutoring,
    get_unit_matcher,
)
from app.tutor.work_photo import read_student_work

ROOT = Path(__file__).resolve().parents[2]
MAX_IMAGE_BYTES = 30 * 1024 * 1024
bearer = HTTPBearer(auto_error=False)
logger = logging.getLogger(__name__)


class InputBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_text: str = Field(
        min_length=1, max_length=24000, strict=True
    )
    expected_version_number: int = Field(ge=1, strict=True)


class TextSubmissionBody(BaseModel):
    """튜터 질문은 본문도 받으며 수학 자동 검증은 별도의 지원 범위를 따른다."""

    model_config = ConfigDict(extra="forbid")
    content_text: str = Field(min_length=1, max_length=10000, strict=True)


class EquationSubmissionBody(InputBody):
    """확인한 사진에서 학생이 직접 옮긴 평문 등식 한 개를 별도로 저장한다."""

    content_text: str = Field(min_length=1, max_length=512, strict=True)


class TypeMatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version_number: int = Field(ge=1, strict=True)


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
    owner_dependency: Callable[..., str] | None = None,
    pipeline_factory=build_qwen_pipeline,
    type_bank_database: str | Path | None = None,
    tutor_runner=None,
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

    if owner_dependency is not None:
        current_owner = owner_dependency

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

    @app.post("/tutor-sessions/{session_id}/work-photo")
    def recognize_work_photo(
        session_id: int,
        file: Annotated[UploadFile, File()],
        student_id: Annotated[str, Depends(current_owner)],
    ):
        try:
            with submission_connection(database) as connection:
                session = tutor_sessions.snapshot(connection, session_id=session_id, owner_id=student_id)
        except LookupError as error:
            raise HTTPException(404, "튜터 기록을 찾을 수 없어요.") from error
        if not session["can_submit"] or (session["pending_request"] or {}).get("status") == "running":
            raise HTTPException(409, "현재 풀이를 확인한 뒤 사진을 올려 주세요.")

        try:
            content = file.file.read(MAX_IMAGE_BYTES + 1)
        finally:
            file.file.close()
        validate_photo(content)
        started = monotonic()
        try:
            lines = read_student_work(build_qwen_backend(), content)
        except (OCRError, ValueError) as error:
            logger.warning("풀이 사진 인식 실패: session=%s, reason=%s", session_id, error)
            raise HTTPException(503, "풀이 사진을 인식하지 못했어요. 다시 시도하거나 직접 입력해 주세요.") from error
        if not lines:
            raise HTTPException(422, "읽을 풀이를 찾지 못했어요. 풀이가 잘 보이는 사진으로 다시 올려 주세요.")

        # 사진은 저장하지 않고, 학생이 확인한 줄을 기존 튜터 제출에서 저장한다.
        return {"lines": lines, "recognition_seconds": round(monotonic() - started, 3)}

    @app.post("/submissions/{submission_id}/equation", status_code=201)
    def submit_photo_equation(
        submission_id: int,
        body: EquationSubmissionBody,
        student_id: Annotated[str, Depends(current_owner)],
    ):
        # 원문 확인·버전 검사와 파생 문제 생성을 같은 쓰기 잠금 안에서 처리한다.
        with submission_connection(database, write=True) as connection:
            source = get_submission(
                connection, submission_id=submission_id, owner_id=student_id,
            )
            latest = source["latest_input"]
            if source["input_kind"] != "photo" or latest is None or latest["confirmed_at"] is None:
                raise HTTPException(409, "사진의 인식 내용을 먼저 확인해 주세요.")
            if latest["version_number"] != body.expected_version_number:
                raise HTTPException(409, "사진의 인식 내용이 변경되었습니다. 다시 조회해 주세요.")
            ids = create_equation_submission(
                connection, source_submission_id=submission_id, owner_id=student_id,
                content_text=body.content_text, expected_version_number=body.expected_version_number,
            )
            return get_submission(
                connection, submission_id=ids["submission_id"], owner_id=student_id,
            )

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
            saved = get_submission(
                connection,
                submission_id=submission_id,
                owner_id=student_id,
            )

        version = saved["latest_input"]["version_number"]
        classify_confirmed_input(submission_id, student_id, version)

        with submission_connection(database) as connection:
            current, _ = confirmed_type_input(
                connection, submission_id, student_id, version,
            )
            return current

    def confirmed_type_input(connection, submission_id, student_id, expected):
        current = get_submission(
            connection, submission_id=submission_id, owner_id=student_id,
        )
        latest = current["latest_input"]
        if not latest or latest["version_number"] != expected:
            raise HTTPException(409, "문제 내용이 변경되었습니다. 최신 내용을 불러와 주세요.")
        if not latest["confirmed_at"]:
            raise HTTPException(409, "문제 내용 확인 완료가 필요합니다.")
        return current, latest

    def classify_confirmed_input(submission_id, student_id, expected):
        with submission_connection(database) as connection:
            current, latest = confirmed_type_input(
                connection, submission_id, student_id, expected,
            )
            cached = load_type_match(connection, latest["id"])

        try:
            matcher = get_unit_matcher(type_bank)

            def reusable(value):
                return (
                    value
                    and value.get("data_version") == matcher.data_version
                    and value.get("classifier_version") == TYPE_MATCH_VERSION
                    and value.get("selection_policy_version") == AUTOMATIC_POLICY_VERSION
                    and value.get("method") != "manual"
                )

            if reusable(cached):
                result = cached
            else:
                layout = (current.get("layout_detection") or {}).get("layout") or {}
                has_visuals = any(
                    region["kind"] in {"figure", "graph", "table"}
                    for region in layout.get("regions", [])
                )
                result = classify_for_tutoring(
                    matcher, latest["content_text"], has_visuals=has_visuals,
                )
        except Exception:
            logger.exception("내부 유형 분류 실패: submission_id=%s", submission_id)
            result = {
                "classifier_version": TYPE_MATCH_VERSION,
                "data_version": None,
                "selection_policy_version": AUTOMATIC_POLICY_VERSION,
                "purpose": "tutoring_reference",
                "status": "needs_review",
                "method": "retrieval",
                "scope": dict(SCOPE),
                "bank_question_count": 0,
                "bank_type_count": 0,
                "selected_type": None,
                "matched_question_id": None,
                "is_provisional": False,
                "candidates": [],
                "reasons": ["내부 유형 분류를 완료하지 못했습니다."],
                "error_code": "internal_type_classification_failed",
            }

        with submission_connection(database, immediate=True) as connection:
            _, latest = confirmed_type_input(
                connection, submission_id, student_id, expected,
            )
            existing = load_type_match(connection, latest["id"])
            if (
                existing
                and existing.get("selection_policy_version") == AUTOMATIC_POLICY_VERSION
                and existing.get("classifier_version") == TYPE_MATCH_VERSION
                and existing.get("data_version") is not None
                and existing.get("method") != "manual"
                and (
                    result["data_version"] is None
                    or existing["data_version"] == result["data_version"]
                )
            ):
                return existing
            return save_type_match(connection, latest, result)

    @app.post("/submissions/{submission_id}/type-match")
    def match_type(
        submission_id: int,
        body: TypeMatchBody,
        student_id: Annotated[str, Depends(current_owner)],
    ):
        return classify_confirmed_input(
            submission_id, student_id, body.expected_version_number,
        )

    app.include_router(create_ocr_router(manager, current_owner))
    app.include_router(create_solution_router(database, current_owner))
    app.include_router(create_tutor_router(database, current_owner, runner=tutor_runner))
    app.include_router(create_learning_router(database, current_owner, bank_database=type_bank))

    return app


app = create_submission_app()
