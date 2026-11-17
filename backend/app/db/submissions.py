# 문제 제출과 학생 입력 버전의 저장·조회를 담당
import sqlite3
import hashlib
import json
from contextlib import closing, contextmanager
from pathlib import Path

def create_submission(
    connection: sqlite3.Connection,
    *,
    owner_id: str,
    input_kind: str,
    uploaded_problem_id: int | None = None,
) -> int:
    """문제 제출을 저장하고 생성된 제출 ID를 반환한다."""
    if not owner_id.strip():
        raise ValueError("학생 ID가 필요합니다.")

    if input_kind not in {"photo", "text"}:
        raise ValueError("입력 방식은 photo 또는 text여야 합니다.")

    if input_kind == "photo" and uploaded_problem_id is None:
        raise ValueError("사진 제출에는 사진 ID가 필요합니다.")

    if input_kind == "text" and uploaded_problem_id is not None:
        raise ValueError("텍스트 제출에는 사진 ID를 연결할 수 없습니다.")

    cursor = connection.execute(
        """
        INSERT INTO problem_submissions (
            owner_id, input_kind, uploaded_problem_id
        )
        VALUES (?, ?, ?)
        """,
        (owner_id, input_kind, uploaded_problem_id),
    )

    submission_id = cursor.lastrowid
    cursor.close()

    if submission_id is None:
        raise RuntimeError("생성된 문제 제출 ID를 가져오지 못했습니다.")

    return submission_id

def save_uploaded_problem(
    connection: sqlite3.Connection,
    *,
    original_filename: str,
    mime_type: str,
    image_content: bytes,
) -> int:
    """사진 원본을 저장하고 생성된 사진 ID를 반환한다."""
    if not original_filename.strip():
        raise ValueError("원본 파일명이 필요합니다.")

    allowed_types = {
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/heic",
        "image/heif",
    }

    if mime_type not in allowed_types:
        raise ValueError("지원하지 않는 이미지 MIME 형식입니다.")

    if not isinstance(image_content, bytes):
        raise ValueError("이미지 내용은 bytes여야 합니다.")

    if not image_content or len(image_content) > 30 * 1024 * 1024:
        raise ValueError("이미지는 비어 있지 않은 30MB 이하 파일이어야 합니다.")

    image_sha256 = hashlib.sha256(image_content).hexdigest()

    cursor = connection.execute(
        """
        INSERT INTO uploaded_problems (
            original_filename, mime_type, image_sha256, image_content
        )
        VALUES (?, ?, ?, ?)
        """,
        (original_filename, mime_type, image_sha256, image_content),
    )

    uploaded_problem_id = cursor.lastrowid
    cursor.close()

    if uploaded_problem_id is None:
        raise RuntimeError("생성된 사진 ID를 가져오지 못했습니다.")

    return uploaded_problem_id

def create_photo_submission(connection: sqlite3.Connection, *, owner_id: str, original_filename: str, mime_type: str, image_content: bytes,) -> dict[str, int]:
    # 사진 원본과 문제 제출 기록을 하나의 작업으로 저장한다.
    """
    → OCR 실행
    → record_ocr_attempt()로 실행 결과 기록
    → 내부적으로 재시도·최종 결과 선택
    → create_initial_input_version()
    → 학생이 수정·확인
    → save_and_confirm_input()
    → get_submission()으로 다시 조회
    """

    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise ValueError("DB 연결의 외래키 검사를 먼저 활성화해 주세요.")

    connection.execute("SAVEPOINT photo_submission")

    try:
        uploaded_problem_id = save_uploaded_problem(
            connection,
            original_filename=original_filename,
            mime_type=mime_type,
            image_content=image_content,
        )

        submission_id = create_submission(
            connection,
            owner_id=owner_id,
            input_kind="photo",
            uploaded_problem_id=uploaded_problem_id,
        )

        connection.execute("RELEASE SAVEPOINT photo_submission")

    except BaseException:
        connection.execute("ROLLBACK TO SAVEPOINT photo_submission")
        connection.execute("RELEASE SAVEPOINT photo_submission")
        raise

    return {
        "submission_id": submission_id,
        "uploaded_problem_id": uploaded_problem_id,
    }

def save_ocr_result(
    connection: sqlite3.Connection,
    *,
    uploaded_problem_id: int,
    model_name: str,
    pipeline_version: str,
    result: dict,
    model_version: str | None = None,
) -> int:
    """OCR 파이프라인의 실행 결과를 저장하고 OCR 결과 ID를 반환한다."""
    if not model_name.strip() or not pipeline_version.strip():
        raise ValueError("모델 이름과 파이프라인 버전이 필요합니다.")

    pipeline_status = result.get("status")

    if pipeline_status not in {"needs_review", "incomplete", "failed"}:
        raise ValueError("지원하지 않는 OCR 결과 상태입니다.")

    ocr_text = result.get("text", "")

    if not isinstance(ocr_text, str):
        raise ValueError("OCR 본문은 문자열이어야 합니다.")

    if pipeline_status == "needs_review":
        if not ocr_text.strip():
            raise ValueError("인식 완료 결과에는 OCR 본문이 필요합니다.")

        status = "completed"
        error_message = None

    else:
        status = "failed"
        error_message = (
            "일부 영역의 OCR 처리가 완료되지 않았습니다."
            if pipeline_status == "incomplete"
            else "OCR 인식에 실패했습니다."
        )

    raw_response_json = json.dumps(
        result,
        ensure_ascii=False,
        allow_nan=False,
    )

    cursor = connection.execute(
        """
        INSERT INTO problem_ocr_results (
            uploaded_problem_id, model_name, model_version,
            pipeline_version, status, ocr_text,
            raw_response_json, error_message
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            uploaded_problem_id, model_name, model_version,
            pipeline_version, status, ocr_text,
            raw_response_json, error_message,
        ),
    )

    ocr_result_id = cursor.lastrowid
    cursor.close()

    if ocr_result_id is None:
        raise RuntimeError("생성된 OCR 결과 ID를 가져오지 못했습니다.")

    return ocr_result_id

def create_initial_input_version(connection: sqlite3.Connection, *, submission_id: int, owner_id: str, ocr_result_id: int,) -> int:
    # 최종 선택된 OCR 원문을 확인 대기 중인 입력 버전 1로 저장한다.

    if not owner_id.strip():
        raise ValueError("학생 ID가 필요합니다.")

    cursor = connection.execute(
        """
        INSERT INTO problem_input_versions (
            submission_id, version_number, source_ocr_result_id,
            origin, content_text
        )
        SELECT
            s.id, 1, o.id, 'ocr', o.ocr_text
        FROM problem_submissions s
        JOIN problem_ocr_results o
            ON o.uploaded_problem_id = s.uploaded_problem_id
        WHERE s.id = ?
          AND s.owner_id = ?
          AND o.id = ?
          AND o.status = 'completed'
          AND length(trim(o.ocr_text)) > 0
          AND NOT EXISTS (
              SELECT 1
              FROM problem_input_versions v
              WHERE v.submission_id = s.id
          )
        """,
        (submission_id, owner_id, ocr_result_id),
    )

    inserted_count = cursor.rowcount
    input_version_id = cursor.lastrowid
    cursor.close()

    if inserted_count != 1:
        raise ValueError(
            "문제 소유자·OCR 결과·기존 입력 버전을 확인해 주세요."
        )

    if input_version_id is None:
        raise RuntimeError("생성된 입력 버전 ID를 가져오지 못했습니다.")

    return input_version_id

@contextmanager
def submission_connection(database: str | Path | None = None):
    """개인 DB를 열고, 성공 시 확정하고 오류 시 되돌린 뒤 닫는다."""
    path = (
        Path(database)
        if database is not None
        else Path(__file__).resolve().parents[3]
        / "data/problem_bank/didgo.sqlite3"
    )
    uri = path.resolve().as_uri() + "?mode=rw"

    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with connection:
            connection.execute("BEGIN")
            yield connection


@contextmanager
def _submission_write(connection):
    """이 작업 범위만 함께 저장하거나 되돌린다."""
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise ValueError("외래키 검사가 활성화된 연결이 필요합니다.")

    connection.execute("SAVEPOINT submission_write")

    try:
        yield
    except BaseException:
        connection.execute("ROLLBACK TO SAVEPOINT submission_write")
        connection.execute("RELEASE SAVEPOINT submission_write")
        raise
    else:
        connection.execute("RELEASE SAVEPOINT submission_write")


def _fetch_one(connection, sql, parameters):
    with closing(connection.execute(sql, parameters)) as cursor:
        row = cursor.fetchone()

        if row is None:
            return None

        names = [column[0] for column in cursor.description]
        return dict(zip(names, row))


def _fetch_all(connection, sql, parameters):
    with closing(connection.execute(sql, parameters)) as cursor:
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]


def _inserted_id(cursor):
    inserted_count = cursor.rowcount
    result_id = cursor.lastrowid
    cursor.close()

    if inserted_count != 1 or result_id is None:
        raise RuntimeError("저장된 행의 ID를 가져오지 못했습니다.")

    return result_id


def _require_submission(connection, submission_id, owner_id):
    if not owner_id.strip():
        raise ValueError("학생 ID가 필요합니다.")

    row = _fetch_one(
        connection,
        """
        SELECT s.*, p.original_filename, p.mime_type
        FROM problem_submissions s
        LEFT JOIN uploaded_problems p ON p.id = s.uploaded_problem_id
        WHERE s.id = ? AND s.owner_id = ?
        """,
        (submission_id, owner_id),
    )

    if row is None:
        raise LookupError("문제가 없거나 접근 권한이 없습니다.")

    return row


def _latest_input(connection, submission_id, owner_id):
    return _fetch_one(
        connection,
        """
        SELECT v.*
        FROM problem_input_versions v
        JOIN problem_submissions s ON s.id = v.submission_id
        WHERE s.id = ? AND s.owner_id = ?
        ORDER BY v.version_number DESC
        LIMIT 1
        """,
        (submission_id, owner_id),
    )


def get_submission(
    connection,
    *,
    submission_id: int,
    owner_id: str,
) -> dict:
    """제출 정보와 최신 입력 내용을 조회한다."""
    result = _require_submission(connection, submission_id, owner_id)
    latest = _latest_input(connection, submission_id, owner_id)

    result["latest_input"] = latest
    result["review_status"] = (
        "awaiting_input" if latest is None
        else "confirmed" if latest["confirmed_at"] is not None
        else "needs_review"
    )

    # 학생 수정본도 최초 OCR 결과를 참조하므로 방향 정보가 함께 복원된다.
    result["image_rotation_ccw"] = 0
    result["image_orientation"] = None
    result["selected_region"] = None
    result["layout_detection"] = None
    if latest is not None and latest["source_ocr_result_id"] is not None:
        ocr = _fetch_one(
            connection,
            "SELECT raw_response_json FROM problem_ocr_results WHERE id = ?",
            (latest["source_ocr_result_id"],),
        )
        if ocr and ocr["raw_response_json"]:
            raw = json.loads(ocr["raw_response_json"])
            rotation = raw.get("rotation_ccw", 0)
            if type(rotation) is int and rotation in {0, 90, 180, 270}:
                result["image_rotation_ccw"] = rotation
            if isinstance(raw.get("orientation"), dict):
                result["image_orientation"] = raw["orientation"]
            if isinstance(raw.get("selected_region"), dict):
                result["selected_region"] = raw["selected_region"]
            if isinstance(raw.get("layout_detection"), dict):
                result["layout_detection"] = raw["layout_detection"]

    return result


def get_latest_input_version(
    connection,
    *,
    submission_id: int,
    owner_id: str,
) -> dict:
    _require_submission(connection, submission_id, owner_id)
    latest = _latest_input(connection, submission_id, owner_id)

    if latest is None:
        raise ValueError("아직 입력 내용이 준비되지 않았습니다.")

    return latest


def get_input_version_history(
    connection,
    *,
    submission_id: int,
    owner_id: str,
) -> list[dict]:
    """OCR 원문과 학생 수정본을 버전 순서대로 조회한다."""
    _require_submission(connection, submission_id, owner_id)

    return _fetch_all(
        connection,
        """
        SELECT v.*
        FROM problem_input_versions v
        JOIN problem_submissions s ON s.id = v.submission_id
        WHERE s.id = ? AND s.owner_id = ?
        ORDER BY v.version_number
        """,
        (submission_id, owner_id),
    )


def get_uploaded_photo(
    connection,
    *,
    submission_id: int,
    owner_id: str,
) -> dict:
    """학생이 제출한 원본 사진 바이트와 파일 정보를 조회한다."""
    _require_submission(connection, submission_id, owner_id)

    result = _fetch_one(
        connection,
        """
        SELECT p.id, p.original_filename, p.mime_type,
               p.image_sha256, p.image_content
        FROM uploaded_problems p
        JOIN problem_submissions s ON s.uploaded_problem_id = p.id
        WHERE s.id = ? AND s.owner_id = ?
        """,
        (submission_id, owner_id),
    )

    if result is None:
        raise ValueError("이 문제에는 사진이 없습니다.")

    return result


def get_ocr_history(
    connection,
    *,
    submission_id: int,
    owner_id: str,
) -> list[dict]:
    """성공·실패를 포함한 OCR 실행 이력을 조회한다."""
    _require_submission(connection, submission_id, owner_id)

    rows = _fetch_all(
        connection,
        """
        SELECT o.*
        FROM problem_ocr_results o
        JOIN problem_submissions s
            ON s.uploaded_problem_id = o.uploaded_problem_id
        WHERE s.id = ? AND s.owner_id = ?
        ORDER BY o.id
        """,
        (submission_id, owner_id),
    )

    for row in rows:
        raw = row.pop("raw_response_json")
        row["raw_response"] = json.loads(raw) if raw is not None else None

    return rows


def record_ocr_attempt(
    connection,
    *,
    submission_id: int,
    owner_id: str,
    model_name: str,
    pipeline_version: str,
    result: dict,
    model_version: str | None = None,
) -> int:
    """학생 수정 단계로 넘기기 전에 OCR 실행 결과를 저장한다."""
    with _submission_write(connection):
        submission = _require_submission(
            connection, submission_id, owner_id
        )

        if submission["input_kind"] != "photo":
            raise ValueError("사진 문제에만 OCR을 연결할 수 있습니다.")

        if _latest_input(connection, submission_id, owner_id) is not None:
            raise ValueError(
                "학생 수정 단계가 시작되어 OCR 처리가 종료되었습니다."
            )

        return save_ocr_result(
            connection,
            uploaded_problem_id=submission["uploaded_problem_id"],
            model_name=model_name,
            model_version=model_version,
            pipeline_version=pipeline_version,
            result=result,
        )


def _check_expected_version(latest, expected_version_number):
    if (
        type(expected_version_number) is not int
        or expected_version_number < 1
    ):
        raise ValueError("기준 버전은 1 이상의 정수여야 합니다.")

    if latest["version_number"] != expected_version_number:
        raise ValueError(
            "내용이 변경되었습니다. 최신 내용을 다시 조회해 주세요."
        )


def save_student_input_version(
    connection,
    *,
    submission_id: int,
    owner_id: str,
    content_text: str,
    expected_version_number: int,
) -> int:
    """현재 버전을 기준으로 학생 수정본을 새 버전으로 저장한다."""
    if not isinstance(content_text, str) or not content_text.strip():
        raise ValueError("문제 내용을 입력해 주세요.")

    with _submission_write(connection):
        latest = get_latest_input_version(
            connection,
            submission_id=submission_id,
            owner_id=owner_id,
        )

        _check_expected_version(latest, expected_version_number)

        if content_text == latest["content_text"]:
            return latest["id"]

        cursor = connection.execute(
            """
            INSERT INTO problem_input_versions (
                submission_id, version_number, source_ocr_result_id,
                origin, content_text
            )
            SELECT
                v.submission_id, v.version_number + 1,
                v.source_ocr_result_id, 'student', ?
            FROM problem_input_versions v
            JOIN problem_submissions s ON s.id = v.submission_id
            WHERE s.id = ? AND s.owner_id = ?
              AND v.version_number = ?
              AND NOT EXISTS (
                  SELECT 1 FROM problem_input_versions newer
                  WHERE newer.submission_id = v.submission_id
                    AND newer.version_number > v.version_number
              )
            """,
            (
                content_text,
                submission_id,
                owner_id,
                expected_version_number,
            ),
        )

        return _inserted_id(cursor)


def confirm_input_version(
    connection,
    *,
    submission_id: int,
    owner_id: str,
    expected_version_number: int,
) -> dict:
    """학생이 보고 있는 최신 버전을 확인 완료한다."""
    with _submission_write(connection):
        latest = get_latest_input_version(
            connection,
            submission_id=submission_id,
            owner_id=owner_id,
        )

        _check_expected_version(latest, expected_version_number)

        if latest["confirmed_at"] is not None:
            return latest

        cursor = connection.execute(
            """
            UPDATE problem_input_versions
            SET confirmed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = ? AND confirmed_at IS NULL
              AND EXISTS (
                  SELECT 1 FROM problem_submissions s
                  WHERE s.id = problem_input_versions.submission_id
                    AND s.owner_id = ?
              )
              AND NOT EXISTS (
                  SELECT 1 FROM problem_input_versions newer
                  WHERE newer.submission_id =
                        problem_input_versions.submission_id
                    AND newer.version_number >
                        problem_input_versions.version_number
              )
            """,
            (latest["id"], owner_id),
        )

        updated_count = cursor.rowcount
        cursor.close()

        if updated_count != 1:
            raise ValueError("최신 버전을 다시 확인해 주세요.")

        return get_latest_input_version(
            connection,
            submission_id=submission_id,
            owner_id=owner_id,
        )


def save_and_confirm_input(
    connection,
    *,
    submission_id: int,
    owner_id: str,
    content_text: str,
    expected_version_number: int,
) -> dict:
    """수정 내용 저장과 확인 완료를 하나의 작업으로 처리한다."""
    with _submission_write(connection):
        save_student_input_version(
            connection,
            submission_id=submission_id,
            owner_id=owner_id,
            content_text=content_text,
            expected_version_number=expected_version_number,
        )

        latest = get_latest_input_version(
            connection,
            submission_id=submission_id,
            owner_id=owner_id,
        )

        return confirm_input_version(
            connection,
            submission_id=submission_id,
            owner_id=owner_id,
            expected_version_number=latest["version_number"],
        )


def create_text_submission(
    connection,
    *,
    owner_id: str,
    content_text: str,
) -> dict[str, int]:
    """직접 입력한 문제와 확인 대기 중인 첫 버전을 저장한다."""
    if not isinstance(content_text, str) or not content_text.strip():
        raise ValueError("문제 내용을 입력해 주세요.")

    with _submission_write(connection):
        submission_id = create_submission(
            connection,
            owner_id=owner_id,
            input_kind="text",
        )

        cursor = connection.execute(
            """
            INSERT INTO problem_input_versions (
                submission_id, version_number, origin, content_text
            )
            VALUES (?, 1, 'student', ?)
            """,
            (submission_id, content_text),
        )

        return {
            "submission_id": submission_id,
            "input_version_id": _inserted_id(cursor),
        }
