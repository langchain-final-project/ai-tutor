"""모델 호출 전후의 짧은 트랜잭션으로 튜터 세션과 공개 이력을 저장한다."""

import hashlib
import json
import os
import time
import uuid

from app.db.solution_records import ConflictError, solution_connection
from app.db.submissions import get_submission
from app.tutor.learning_catalog import EXPRESSION_TYPES, TYPES
from app.tutor.problem_target import analyze_target, expression_type


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':'))


def _row(connection, sql, values=()):
    cursor = connection.execute(sql, values)
    try:
        row = cursor.fetchone()
        return dict(zip([column[0] for column in cursor.description], row)) if row is not None else None
    finally:
        cursor.close()


def _rows(connection, sql, values=()):
    cursor = connection.execute(sql, values)
    try:
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]
    finally:
        cursor.close()


def _type_reference(value):
    if not isinstance(value, dict):
        return None
    # 참고 문항 본문·정답 대신 유형과 그 출처만 세션에 전달한다.
    output = {key: value[key] for key in (
        'status', 'scope', 'selected_type', 'is_provisional', 'reasons',
        'classifier_version', 'data_version', 'selection_policy_version',
    ) if key in value}
    output['candidates'] = [{key: candidate[key] for key in (
        'source_topic_code', 'source_topic_name', 'reasons',
    ) if key in candidate} for candidate in value.get('candidates', [])[:3]]
    return output


def _session(connection, session_id, owner_id):
    row = _row(connection, '''SELECT t.* FROM tutor_sessions t
        JOIN problem_submissions s ON s.id=t.submission_id WHERE t.id=? AND s.owner_id=?''',
        (session_id, owner_id))
    if row is None:
        raise LookupError('튜터 세션이 없거나 접근 권한이 없습니다.')
    return row


def _current(connection, row, owner_id):
    source = get_submission(connection, submission_id=row['submission_id'], owner_id=owner_id)
    latest = source['latest_input']
    return bool(latest and latest['id'] == row['input_version_id'] and latest['confirmed_at'])


def _target_info(text, subject='mathematics'):
    if subject == 'mathematics':
        return analyze_target(text)
    return {'status': 'open_problem', 'normalized_text': None, 'choices': [], 'message': ''}


def target_options(connection, *, owner_id, submission_id):
    source = get_submission(connection, submission_id=submission_id, owner_id=owner_id)
    latest = source['latest_input']
    if not latest:
        raise ConflictError('문제 내용을 먼저 입력하거나 인식을 완료해 주세요.')
    saved_sessions = []
    if latest['confirmed_at'] and source['input_kind'] == 'photo':
        # 확인한 같은 사진 입력 버전에서 시작한 풀이만 이어갈 수 있게 표시한다.
        saved_sessions = _rows(connection, '''SELECT id,target_text FROM tutor_sessions
            WHERE submission_id=? AND input_version_id=? AND subject='mathematics'
            ORDER BY updated_at DESC,id DESC''', (submission_id, latest['id']))
    return {'submission_id': submission_id, 'input_version_number': latest['version_number'],
            'input_confirmed': bool(latest['confirmed_at']),
            'saved_sessions': saved_sessions,
            **analyze_target(latest['content_text'])}


def snapshot(connection, *, session_id, owner_id):
    row = _session(connection, session_id, owner_id)
    problem = json.loads(row['problem_json'])
    turns = _rows(connection, 'SELECT * FROM tutor_turns WHERE session_id=? ORDER BY id', (session_id,))
    events, pending = [], None
    running, failed = [], []
    for turn in turns:
        request = json.loads(turn['request_json'])
        if turn['status'] == 'completed':
            events.append({
                'id': turn['id'], 'request_id': turn['request_id'], 'intent': request['intent'],
                'student_text': request['student_text'], 'steps': request['steps'],
                'response': json.loads(turn['response_json']),
                'state_after': json.loads(turn['state_after_json']), 'created_at': turn['completed_at'],
            })
        else:
            item = {'request_id': turn['request_id'], 'status': turn['status'],
                    'error': turn['error'], 'expected_revision': turn['expected_revision'],
                    'intent': request['intent'], 'student_text': request['student_text'],
                    'steps': request['steps'], 'lease_expires_at': turn['lease_expires_at']}
            if turn['status'] == 'running':
                running.append(item)
            elif turn['expected_revision'] == row['revision']:
                failed.append(item)
    # 오래된 요청의 재시도는 ID 순서와 다를 수 있으므로 실행 중 요청을 우선한다.
    pending = running[-1] if running else failed[-1] if failed else None
    is_current = _current(connection, row, owner_id)
    target_selection = _target_info(row['target_text'], row['subject'])
    can_submit = is_current
    if problem.get('learning_context'):
        activity = _row(connection, '''SELECT a.id,a.status,a.mode,p.current_activity_id,p.phase,
            original.submission_id,original.input_version_id
            FROM learning_activities a JOIN learning_paths p ON p.id=a.path_id
            JOIN tutor_sessions original ON original.id=p.source_session_id
            WHERE a.session_id=?''', (session_id,))
        # 문제 버전의 현재 여부와 학습 시도의 종료 여부를 화면에서도 구분한다.
        can_submit = bool(is_current and activity and activity['status'] == 'active'
                          and activity['current_activity_id'] == activity['id']
                          and activity['phase'] == activity['mode']
                          and _current(connection, activity, owner_id))
    # 예전에 여러 소문항으로 시작한 세션도 기록은 남기고 대상 선택부터 다시 연결한다.
    can_submit = can_submit and target_selection['status'] != 'selection_required'
    return {
        'id': row['id'], 'submission_id': row['submission_id'],
        'input_version_number': problem['input_version_number'],
        'subject': row['subject'], 'target_text': row['target_text'],
        'source_text': problem['source_text'], 'input_kind': problem['input_kind'],
        'target_selection': target_selection,
        'target_input_text': problem.get('target_input_text', row['target_text']),
        'attempt_mode': problem.get('attempt_mode', 'practice'),
        'learning_context': problem.get('learning_context'),
        'type_match': problem.get('type_match'), 'condition_extraction': problem.get('condition_extraction'),
        'revision': row['revision'], 'state': json.loads(row['state_json']),
        'created_at': row['created_at'], 'updated_at': row['updated_at'],
        'is_current': is_current, 'can_submit': can_submit, 'events': events, 'pending_request': pending,
    }


def create(connection, *, owner_id, submission_id, expected_input_version_number, target_text,
           subject='mathematics', learning_context=None):
    from app.tutor.session_service import initial_state

    source = get_submission(connection, submission_id=submission_id, owner_id=owner_id)
    latest = source['latest_input']
    if not latest or latest['confirmed_at'] is None:
        raise ConflictError('문제 내용을 먼저 확인해 주세요.')
    if latest['version_number'] != expected_input_version_number:
        raise ConflictError('문제 내용이 변경되었습니다. 최신 문제를 확인해 주세요.')
    target_text = target_text.strip()
    if not target_text:
        raise ValueError('이번에 풀 문제 또는 소문항을 확인해 주세요.')
    target_input_text = target_text
    target_selection = _target_info(target_text, subject)
    if target_selection['status'] == 'selection_required':
        raise ValueError(target_selection['message'])
    target_text = target_selection['normalized_text'] or target_text
    digest = hashlib.sha256(target_text.encode()).hexdigest()
    previous = _row(connection, '''SELECT id FROM tutor_sessions
        WHERE input_version_id=? AND subject=? AND target_sha256=?''', (latest['id'], subject, digest))
    if previous:
        return snapshot(connection, session_id=previous['id'], owner_id=owner_id)
    problem = {
        'submission_id': submission_id, 'input_version_number': latest['version_number'],
        'source_text': latest['content_text'], 'target_text': target_text,
        'target_input_text': target_input_text,
        'subject': subject, 'input_kind': source['input_kind'],
        'target_confirmed': True, 'type_match': _type_reference(source.get('type_match')),
        'condition_extraction': source.get('condition_extraction'),
    }
    if target_selection['status'] == 'linear_expression':
        reference = source.get('type_match') or {}
        current_type = (reference.get('selected_type') or {}).get('source_topic_code')
        if current_type not in EXPRESSION_TYPES or not reference.get('matched_question_id'):
            code = expression_type(target_text)
            problem['type_match'] = {
                'status': 'classified', 'is_provisional': True,
                'selected_type': {'source_topic_code': code, 'source_topic_name': TYPES[code][0]},
                'reasons': ['확인한 일차식의 구조를 문제은행 유형에 연결했어요.'],
            }
    if learning_context is not None:
        # 학습 시도 종류는 학습 경로가 서버에서 정한다. 학생 요청으로 독립 평가를 위조하지 않는다.
        if learning_context.get('mode') not in {'practice', 'independent', 'review'}:
            raise ValueError('지원하지 않는 학습 시도입니다.')
        problem['attempt_mode'] = learning_context['mode']
        problem['learning_context'] = dict(learning_context)
        if learning_context.get('type_code') and learning_context.get('type_name'):
            problem['type_match'] = {
                'status': 'curated_reference', 'is_provisional': True,
                'selected_type': {'source_topic_code': learning_context['type_code'],
                                  'source_topic_name': learning_context['type_name']},
                'reasons': ['기존 문제은행 유형에 연결한 검수 문항입니다. 개념 숙달 판정은 아닙니다.'],
            }
    state = initial_state()
    selected_type = (problem['type_match'] or {}).get('selected_type') or {}
    if selected_type.get('source_topic_name'):
        state.update(goal_concept=selected_type['source_topic_name'], goal_concept_source='type_reference',
                     goal_concept_is_provisional=True)
    cursor = connection.execute('''INSERT INTO tutor_sessions
        (submission_id,input_version_id,subject,target_text,target_sha256,problem_json,state_json)
        VALUES (?,?,?,?,?,?,?)''', (submission_id, latest['id'], subject, target_text, digest,
                                   _json(problem), _json(state)))
    session_id = cursor.lastrowid
    cursor.close()
    return snapshot(connection, session_id=session_id, owner_id=owner_id)


def list_sessions(connection, *, owner_id, limit=50):
    rows = _rows(connection, '''SELECT t.id FROM tutor_sessions t JOIN problem_submissions s
        ON s.id=t.submission_id WHERE s.owner_id=? ORDER BY t.updated_at DESC,t.id DESC LIMIT ?''',
        (owner_id, limit))
    return [snapshot(connection, session_id=row['id'], owner_id=owner_id) for row in rows]


def reserve(connection, *, owner_id, session_id, request):
    row = _session(connection, session_id, owner_id)
    if not _current(connection, row, owner_id):
        raise ConflictError('원본 문제가 바뀌었습니다. 확인한 최신 문제에서 새 튜터를 시작해 주세요.')
    payload = _json(request)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    prior = _row(connection, 'SELECT * FROM tutor_turns WHERE session_id=? AND request_id=?',
                 (session_id, request['request_id']))
    if prior and prior['request_sha256'] != digest:
        raise ConflictError('같은 요청 번호로 다른 내용을 전송할 수 없습니다.')
    if prior and prior['status'] == 'completed':
        return None, snapshot(connection, session_id=session_id, owner_id=owner_id)
    if _target_info(row['target_text'], row['subject'])['status'] == 'selection_required':
        raise ConflictError('여러 소문항이 함께 있어요. 이번에 풀 소문항을 선택한 뒤 새 튜터로 이어가 주세요.')
    if json.loads(row['problem_json']).get('learning_context'):
        from app.db.learning_paths import require_active_attempt
        require_active_attempt(connection, owner_id=owner_id, session_id=session_id)
    if row['revision'] != request['expected_revision']:
        raise ConflictError('다른 요청으로 학습 기록이 바뀌었습니다. 최신 기록을 확인해 주세요.')
    now = time.time()
    # 프로세스 종료로 남은 요청만 유예 시간 뒤 재시도할 수 있다.
    connection.execute('''UPDATE tutor_turns SET status='failed',error=?
        WHERE session_id=? AND status='running' AND lease_expires_at < ?''',
        ('이전 응답이 완료되지 않았습니다. 같은 요청으로 다시 시도할 수 있어요.', session_id, now))
    running = _row(connection, "SELECT id FROM tutor_turns WHERE session_id=? AND status='running'", (session_id,))
    if running:
        if prior and prior['id'] == running['id']:
            return None, snapshot(connection, session_id=session_id, owner_id=owner_id)
        raise ConflictError('진행 중인 도움 요청이 있습니다. 완료 후 다시 요청해 주세요.')
    lease = uuid.uuid4().hex
    timeout = min(300, max(10, float(os.getenv('DIDGO_TUTOR_TIMEOUT', '90'))))
    if prior:
        if prior['status'] == 'stale':
            raise ConflictError('이 요청은 이전 문제 상태에 속합니다. 최신 기록을 확인해 주세요.')
        connection.execute('''UPDATE tutor_turns SET status='running',lease_token=?,lease_expires_at=?,
            error=NULL WHERE id=?''', (lease, now + timeout + 30, prior['id']))
        turn_id = prior['id']
    else:
        cursor = connection.execute('''INSERT INTO tutor_turns
            (session_id,request_id,request_sha256,expected_revision,request_json,status,lease_token,lease_expires_at)
            VALUES (?,?,?,?,?,'running',?,?)''',
            (session_id, request['request_id'], digest, request['expected_revision'], payload, lease, now + timeout + 30))
        turn_id = cursor.lastrowid
        cursor.close()
    context = snapshot(connection, session_id=session_id, owner_id=owner_id)
    return {'turn_id': turn_id, 'lease': lease, 'revision': row['revision'],
            'problem': json.loads(row['problem_json']), 'state': context['state'], 'events': context['events']}, None


def finish(connection, *, owner_id, session_id, reservation, result):
    row = _session(connection, session_id, owner_id)
    turn = _row(connection, 'SELECT * FROM tutor_turns WHERE id=? AND session_id=?',
                (reservation['turn_id'], session_id))
    if not turn or turn['lease_token'] != reservation['lease'] or turn['status'] != 'running':
        return False
    if row['revision'] != reservation['revision'] or not _current(connection, row, owner_id):
        connection.execute("UPDATE tutor_turns SET status='stale',error=? WHERE id=?",
                           ('응답을 기다리는 동안 문제가 바뀌어 이 도움은 공개하지 않았어요.', turn['id']))
        return False
    linked = json.loads(row['problem_json']).get('learning_context')
    if linked:
        from app.db.learning_paths import require_active_attempt
        try:
            require_active_attempt(connection, owner_id=owner_id, session_id=session_id)
        except ConflictError:
            # 원본 경로가 바뀐 동안 도착한 응답도 완료 처리하거나 다시 공개하지 않는다.
            connection.execute("UPDATE tutor_turns SET status='stale',error=? WHERE id=?",
                               ('학습 경로가 바뀌어 이전 응답을 공개하지 않았어요.', turn['id']))
            return False
    connection.execute('''UPDATE tutor_turns SET status='completed',response_json=?,state_after_json=?,
        completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?''',
        (_json(result['response']), _json(result['state']), turn['id']))
    connection.execute('''UPDATE tutor_sessions SET state_json=?,revision=revision+1,
        updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?''', (_json(result['state']), session_id))
    if linked:
        # 도움 공개와 다음 학습 상태는 같은 트랜잭션에서 함께 저장한다.
        from app.db.learning_paths import record_tutor_result
        record_tutor_result(connection, owner_id=owner_id, session_id=session_id, result=result)
    return True


def fail(connection, *, session_id, reservation):
    connection.execute('''UPDATE tutor_turns SET status='failed',error=?
        WHERE id=? AND session_id=? AND lease_token=? AND status='running' ''',
        ('도움을 생성하지 못했어요. 입력을 보존했으니 같은 요청으로 다시 시도해 주세요.',
         reservation['turn_id'], session_id, reservation['lease']))
