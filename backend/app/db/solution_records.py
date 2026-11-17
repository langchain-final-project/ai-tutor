# 문제·풀이 버전에 검증 결과와 안내 표시 확인을 저장

# 현재 텍스트 내용은 실수 일차방정식이고, 문장에서 식을 자동 추출하지 않음
# confirmed는 DB 확인 시각에서 만들며, 모든 쓰기는 solution_connection(write=True) 안에서 버전 검사와 함께 처리 진행

from contextlib import closing, contextmanager
import json
from pathlib import Path
import re
import sqlite3

from app.db.submissions import get_submission
from app.tutor.solution_service import evaluate_reviewed_solution
from app.tutor.step_verifier import input_fingerprint


class ConflictError(ValueError):
     # 충돌 오류, 클라이언트가 최신 버전을 다시 조회해야 함
    pass


@contextmanager # DB 연결 컨텍스트 관리자, 쓰기 모드일 경우 즉시 트랜잭션 시작
def solution_connection(database, *, write=False):
    uri = Path(database).resolve().as_uri() + '?mode=rw'
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as connection:
        connection.execute('PRAGMA foreign_keys = ON')
        with connection:
            connection.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield connection


def _json(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(',', ':'))


def _one(connection, sql, values=()):
    with closing(connection.execute(sql, values)) as cursor:
        row = cursor.fetchone()
        return dict(zip([c[0] for c in cursor.description], row)) if row is not None else None


def _all(connection, sql, values=()):
    with closing(connection.execute(sql, values)) as cursor:
        names = [c[0] for c in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]


def _problem(connection, submission_id, owner_id):
    submission = get_submission(connection, submission_id=submission_id, owner_id=owner_id)
    if submission['input_kind'] != 'text':
        raise ConflictError('현재 풀이 검증은 텍스트로 입력한 등식만 지원합니다.')
    problem = submission['latest_input']
    if problem is None:
        raise ConflictError('문제 입력을 먼저 저장해 주세요.')
    return problem


def _work_row(connection, problem):
    return _one(connection, '''SELECT * FROM student_work_versions
        WHERE problem_input_version_id = ? ORDER BY version_number DESC LIMIT 1''', (problem['id'],))


def _versions(problem, work, expected_problem, expected_work):
    if type(expected_problem) is not int or expected_problem < 1:
        raise ValueError('문제 버전은 1 이상의 정수여야 합니다.')
    if type(expected_work) is not int or expected_work < 0:
        raise ValueError('풀이 버전은 0 이상의 정수여야 합니다.')
    if problem['version_number'] != expected_problem or (work['version_number'] if work else 0) != expected_work:
        raise ConflictError('문제 또는 풀이가 변경되었습니다. 최신 버전을 다시 조회해 주세요.')


def _validate_steps(steps):
    if type(steps) is not list or len(steps) > 100:
        raise ValueError('풀이 줄은 100개 이하의 배열이어야 합니다.')
    identifiers = set()
    for step in steps:
        if type(step) is not dict or set(step) != {'id', 'text', 'state'}:
            raise ValueError('풀이 줄에는 id, text, state만 사용합니다.')
        if type(step['id']) is not str or not step['id'].strip() or len(step['id']) > 255 or step['id'] in identifiers:
            raise ValueError('풀이 줄 ID는 비어 있지 않은 고유 문자열이어야 합니다.')
        identifiers.add(step['id'])
        if type(step['text']) is not str or len(step['text']) > 10000:
            raise ValueError('풀이 줄 내용은 10000자 이하 문자열이어야 합니다.')
        if type(step['state']) is not str or step['state'] not in ('active', 'deleted', 'uncertain'):
            raise ValueError('풀이 상태는 active/deleted/uncertain 중 하나여야 합니다.')


def _work_output(row):
    if row is None:
        return None
    output = dict(row)
    output['steps'] = json.loads(output.pop('steps_json'))
    return output


def save_work(connection, *, submission_id, owner_id, expected_problem_version_number,
              expected_work_version_number, steps):
    problem = _problem(connection, submission_id, owner_id)
    latest = _work_row(connection, problem)
    _versions(problem, latest, expected_problem_version_number, expected_work_version_number)
    _validate_steps(steps)
    cursor = connection.execute('''INSERT INTO student_work_versions
        (submission_id, problem_input_version_id, version_number, steps_json)
        VALUES (?, ?, ?, ?)''', (submission_id, problem['id'], expected_work_version_number + 1, _json(steps)))
    row_id = cursor.lastrowid
    cursor.close()
    return _work_output(_one(connection, 'SELECT * FROM student_work_versions WHERE id = ?', (row_id,)))


def confirm_work(connection, *, submission_id, owner_id, expected_problem_version_number,
                 expected_work_version_number):
    problem = _problem(connection, submission_id, owner_id)
    latest = _work_row(connection, problem)
    _versions(problem, latest, expected_problem_version_number, expected_work_version_number)
    if latest is None:
        raise ConflictError('확인할 학생 풀이가 없습니다.')
    if problem['confirmed_at'] is None:
        raise ConflictError('현재 문제 내용을 먼저 확인해 주세요.')
    if latest['confirmed_at'] is None:
        connection.execute("UPDATE student_work_versions SET confirmed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?", (latest['id'],))
    return _work_output(_work_row(connection, problem))


def get_work(connection, *, submission_id, owner_id):
    return _work_output(_work_row(connection, _problem(connection, submission_id, owner_id)))


def _inputs(problem, work, submission_id):
    letters = set(re.findall('[A-Za-z]', problem['content_text']))
    variable = next(iter(letters)) if len(letters) == 1 else 'x'
    source = {'schema_version': 'verification-problem-v1', 'id': str(submission_id),
              'revision': str(problem['version_number']), 'equation': problem['content_text'],
              'variable': variable, 'domain': 'real', 'profile': 'linear-equation-v1',
              'confirmed': problem['confirmed_at'] is not None}
    observed = {'schema_version': 'verification-work-v1', 'problem_id': source['id'],
                'problem_revision': source['revision'], 'steps': []}
    if work:
        observed['steps'] = [dict(step, confirmed=work['confirmed_at'] is not None and step['state'] != 'uncertain')
                             for step in json.loads(work['steps_json'])]
    return source, observed


def evaluate_solution(connection, *, submission_id, owner_id, expected_problem_version_number,
                      expected_work_version_number):
    problem = _problem(connection, submission_id, owner_id)
    work = _work_row(connection, problem)
    _versions(problem, work, expected_problem_version_number, expected_work_version_number)
    source, observed = _inputs(problem, work, submission_id)
    evaluation = evaluate_reviewed_solution(source, observed)
    cursor = connection.execute('''INSERT INTO solution_verifications
        (submission_id, problem_input_version_id, work_version_id, problem_version_number,
         work_version_number, input_fingerprint, verifier_version, problem_json, work_json, evaluation_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (submission_id, problem['id'], work['id'] if work else None, problem['version_number'],
         work['version_number'] if work else 0, evaluation['input_fingerprint'],
         evaluation['verification']['verifier_version'], _json(source), _json(observed), _json(evaluation)))
    result_id = cursor.lastrowid
    cursor.close()
    return get_verification(connection, submission_id=submission_id, owner_id=owner_id, result_id=result_id)


def _result_output(row, problem, work, submission_id):
    output = {key: row[key] for key in ('id', 'submission_id', 'problem_version_number',
              'work_version_number', 'input_fingerprint', 'created_at')}
    output['evaluation'] = json.loads(row['evaluation_json'])
    source, observed = _inputs(problem, work, submission_id)
    try:
        current_fingerprint = input_fingerprint(source, observed)
    except ValueError:
        # 현재 원문이 검증 계약 밖이어도 과거 결과는 그대로 조회
        current_fingerprint = None
    output['is_current'] = (row['problem_input_version_id'] == problem['id']
        and row['work_version_id'] == (work['id'] if work else None)
        and row['input_fingerprint'] == current_fingerprint)
    return output


def get_verification(connection, *, submission_id, owner_id, result_id):
    problem = _problem(connection, submission_id, owner_id)
    row = _one(connection, 'SELECT * FROM solution_verifications WHERE id = ? AND submission_id = ?', (result_id, submission_id))
    if row is None:
        raise LookupError('검증 결과가 없거나 접근 권한이 없습니다.')
    return _result_output(row, problem, _work_row(connection, problem), submission_id)


def list_verifications(connection, *, submission_id, owner_id):
    problem = _problem(connection, submission_id, owner_id)
    work = _work_row(connection, problem)
    rows = _all(connection, 'SELECT * FROM solution_verifications WHERE submission_id = ? ORDER BY id DESC LIMIT 100', (submission_id,))
    return [_result_output(row, problem, work, submission_id) for row in rows]


def get_current_verification(connection, *, submission_id, owner_id):
    return next((row for row in list_verifications(connection, submission_id=submission_id, owner_id=owner_id)
                 if row['is_current']), None)


def record_feedback_view(connection, *, submission_id, owner_id, result_id):
    result = get_verification(connection, submission_id=submission_id, owner_id=owner_id, result_id=result_id)
    if not result['is_current']:
        raise ConflictError('이전 입력의 안내입니다. 현재 입력을 다시 검증해 주세요.')
    connection.execute('''INSERT INTO feedback_view_events (verification_result_id, feedback_json)
        VALUES (?, ?) ON CONFLICT (verification_result_id) DO NOTHING''',
        (result_id, _json(result['evaluation']['student_feedback'])))
    row = _one(connection, 'SELECT * FROM feedback_view_events WHERE verification_result_id = ?', (result_id,))
    return _feedback_output(row)


def _feedback_output(row):
    return {'id': row['id'], 'verification_result_id': row['verification_result_id'],
            'feedback': json.loads(row['feedback_json']), 'viewed_at': row['viewed_at']}


def list_feedback_views(connection, *, submission_id, owner_id):
    _problem(connection, submission_id, owner_id)
    rows = _all(connection, '''SELECT e.* FROM feedback_view_events e
        JOIN solution_verifications v ON v.id = e.verification_result_id
        WHERE v.submission_id = ? ORDER BY e.id''', (submission_id,))
    return [_feedback_output(row) for row in rows]


def list_owned_submissions(connection, *, owner_id, limit=50):
    """재접속한 학생의 질문 목록을 최대 100개 조회한다."""
    if type(owner_id) is not str or not owner_id.strip():
        raise ValueError('학생 ID가 필요합니다.')
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('조회 개수는 1부터 100까지의 정수여야 합니다.')
    rows = _all(connection, 'SELECT id FROM problem_submissions WHERE owner_id = ? ORDER BY id DESC LIMIT ?', (owner_id, limit))
    return [get_submission(connection, submission_id=row['id'], owner_id=owner_id) for row in rows]
