"""원본 튜터와 새 문항의 연습·독립 풀이·지연 복습 시도를 따로 보관한다."""

from datetime import datetime, timedelta, timezone
import json
import re

from app.db import tutor_sessions
from app.db.solution_records import ConflictError
from app.db.submissions import create_text_submission, confirm_input_version
from app.tutor import learning_catalog as catalog
from app.tutor.step_verifier import _equation
from app.tutor.unit_type_matcher import DEFAULT_BANK, equation_focus


def _now(now=None):
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _stamp(value):
    return value.isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def _time(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


def _row(connection, sql, parameters=()):
    cursor = connection.execute(sql, parameters)
    try:
        value = cursor.fetchone()
        return dict(zip([column[0] for column in cursor.description], value)) if value is not None else None
    finally:
        cursor.close()


def _owned(connection, path_id, owner_id):
    row = _row(connection, '''SELECT p.* FROM learning_paths p
        JOIN tutor_sessions t ON t.id=p.source_session_id
        JOIN problem_submissions s ON s.id=t.submission_id WHERE p.id=? AND s.owner_id=?''',
        (path_id, owner_id))
    if row is None:
        raise LookupError('학습 경로가 없거나 접근 권한이 없습니다.')
    return row


def _activities(connection, path_id):
    ids = connection.execute('SELECT id FROM learning_activities WHERE path_id=? ORDER BY id', (path_id,)).fetchall()
    return [_row(connection, 'SELECT * FROM learning_activities WHERE id=?', (row[0],)) for row in ids]


def _source(connection, path, owner_id):
    return tutor_sessions.snapshot(connection, session_id=path['source_session_id'], owner_id=owner_id)


def _next_mode(path, active):
    if path['phase'] == 'remediation':
        return 'practice'
    if path['phase'] == 'practice' and active and active['status'] == 'completed':
        return 'independent'
    if path['phase'] == 'review_due':
        return 'review'
    return None


def _candidate(connection, path, source, mode, bank_database, owner_id):
    items, error = catalog.load_catalog(bank_database)
    if error:
        return None, False, error
    used = connection.execute('''SELECT a.catalog_item_id FROM learning_activities a
        JOIN learning_paths p ON p.id=a.path_id JOIN tutor_sessions t ON t.id=p.source_session_id
        JOIN problem_submissions s ON s.id=t.submission_id WHERE s.owner_id=? ORDER BY a.id''', (owner_id,)).fetchall()
    previous = connection.execute('''SELECT t.target_text,t.problem_json FROM tutor_sessions t
        JOIN problem_submissions s ON s.id=t.submission_id WHERE s.owner_id=? ORDER BY t.id''', (owner_id,)).fetchall()
    return catalog.choose(items, type_code=path['type_code'], source_text=source['source_text'],
                          source_target=source['target_text'],
                          used_ids=[row[0] for row in used], mode=mode,
                          seen_questions=[{'target_text': row[0], 'source_text': json.loads(row[1]).get('source_text', '')}
                                          for row in previous])


def snapshot(connection, *, path_id, owner_id, bank_database=DEFAULT_BANK, now=None):
    path = _owned(connection, path_id, owner_id)
    source = _source(connection, path, owner_id)
    activities = _activities(connection, path_id)
    active = next((item for item in activities if item['id'] == path['current_activity_id']), None)
    mode = _next_mode(path, active)
    next_action = {'label': '현재 문제 계속 풀기', 'reason': '현재 문항의 풀이와 확인을 마친 뒤 다음 학습으로 이어가요.', 'available': False}
    status = 'in_progress'
    if not source['is_current']:
        status = 'source_changed'
        next_action['reason'] = '원본 문제가 수정되었어요. 최신 문제에서 새 튜터와 학습 경로를 시작해 주세요.'
    elif active and not tutor_sessions.snapshot(connection, session_id=active['session_id'], owner_id=owner_id)['is_current']:
        status = 'activity_changed'
        next_action['reason'] = '학습 문항이 수정되어 이전 시도의 결과를 이어 쓸 수 없어요. 원래 문제에서 새 학습을 확인해 주세요.'
    elif path['phase'] == 'review_due' and _now(now) < _time(path['review_due_at']):
        status = 'review_scheduled'
        next_action.update(label='복습 예약됨', reason='간격을 두고 도움 없이 다시 풀어볼 시간이에요. 예약 시각부터 복습을 시작할 수 있어요.',
                           available_at=path['review_due_at'])
    elif mode:
        item, _, reason = _candidate(connection, path, source, mode, bank_database, owner_id)
        labels = {'practice': '보충 확인 후 유사 문제 풀기', 'independent': '새 문제 도움 없이 풀기', 'review': '예약한 복습 시작'}
        next_action.update(label=labels[mode], reason=reason, available=item is not None)
        if item is None:
            status = 'unavailable'
    history = [{
        'activity_id': item['id'], 'session_id': item['session_id'], 'mode': item['mode'],
        'status': item['status'], 'question_text': item['question_text'], 'target_text': item['target_text'],
        'source_question_id': item['source_question_id'], 'catalog_item_id': item['catalog_item_id'],
        'is_new_question': bool(item['is_new_question']), 'selection_reason': item['selection_reason'],
        'outcome': json.loads(item['outcome_json']), 'created_at': item['created_at'], 'completed_at': item['completed_at'],
    } for item in activities]
    remediation = None
    if path['phase'] == 'remediation':
        remediation = {
            'title': path['type_name'] + ' 다시 보기',
            'text': catalog.TYPES[path['type_code']][1],
            'concept_cards': catalog.concept_cards(path['type_code']),
        }
    return {
        'id': path['id'], 'revision': path['revision'], 'source_session_id': path['source_session_id'],
        'type': {'code': path['type_code'], 'name': path['type_name']},
        'phase': path['phase'], 'status': status, 'next_action': next_action,
        'active_session_id': active['session_id'] if active and path['phase'] in {'practice', 'independent', 'review'} else None,
        'history': history, 'remediation': remediation,
        'review_due_at': path['review_due_at'], 'review_count': path['review_count'],
        'policy_version': path['policy_version'], 'mastery_claim': False,
        'created_at': path['created_at'], 'updated_at': path['updated_at'],
    }


def get_for_session(connection, *, session_id, owner_id, bank_database=DEFAULT_BANK, now=None):
    # 활동 세션에서 다시 열어도 같은 원본 학습 경로를 반환한다.
    row = _row(connection, '''SELECT p.id FROM learning_paths p
        JOIN tutor_sessions t ON t.id=p.source_session_id JOIN problem_submissions s ON s.id=t.submission_id
        WHERE s.owner_id=? AND (p.source_session_id=? OR EXISTS (
            SELECT 1 FROM learning_activities a WHERE a.path_id=p.id AND a.session_id=?))''',
        (owner_id, session_id, session_id))
    if row is None:
        raise LookupError('학습 경로가 없거나 접근 권한이 없습니다.')
    return snapshot(connection, path_id=row['id'], owner_id=owner_id, bank_database=bank_database, now=now)


def _new_path_type(source):
    if not source['is_current']:
        raise ConflictError('확인한 최신 문제에서 학습 경로를 시작해 주세요.')
    letters = set(re.findall(r'[A-Za-z]', source['target_text']))
    if source['subject'] != 'mathematics' or len(letters) != 1:
        raise ValueError('이번 학습 경로는 해가 하나인 일차방정식을 지원해요.')
    try:
        if _equation(source['target_text'], next(iter(letters)))['root'] is None:
            raise ValueError
    except ValueError:
        raise ValueError('검수한 유일해 일차방정식에서만 학습 경로를 시작할 수 있어요.') from None
    selected = (source.get('type_match') or {}).get('selected_type') or {}
    # 본문 전체의 잠정 참고 유형 대신 학생이 확인한 단일 등식을 기존 분류 규칙에 전달한다.
    # 원래 제출의 유형 저장값은 그대로 두고 이번 학습 목표만 정한다.
    code = equation_focus('방정식 ' + source['target_text'] + ' 풀어라')[0] or selected.get('source_topic_code')
    # 선택한 식은 이미 평문으로 확인됐다. (x+1)/2 같은 수 분모도 분수 계수 유형이다.
    if code in {'7103047', '7103048'} and '/' in source['target_text']:
        code = '7103050'
    if code not in catalog.TYPES:
        raise ValueError('이 풀이 유형의 추가 학습 문항은 아직 준비되지 않았어요.')
    return code


def availability(connection, *, session_id, owner_id, bank_database=DEFAULT_BANK):
    source = tutor_sessions.snapshot(connection, session_id=session_id, owner_id=owner_id)
    try:
        code = _new_path_type(source)
    except (ValueError, ConflictError) as error:
        return {'available': False, 'reason': str(error)}
    item, _, reason = _candidate(connection, {'type_code': code}, source, 'practice', bank_database, owner_id)
    return {'available': item is not None, 'reason': reason}


def create(connection, *, source_session_id, owner_id, bank_database=DEFAULT_BANK, now=None):
    source = tutor_sessions.snapshot(connection, session_id=source_session_id, owner_id=owner_id)
    try:
        return get_for_session(connection, session_id=source_session_id, owner_id=owner_id,
                               bank_database=bank_database, now=now)
    except LookupError:
        pass
    code = _new_path_type(source)
    cursor = connection.execute('''INSERT INTO learning_paths(source_session_id,type_code,type_name,policy_version)
        VALUES(?,?,?,?)''', (source_session_id, code, catalog.TYPES[code][0], catalog.POLICY_VERSION))
    return snapshot(connection, path_id=cursor.lastrowid, owner_id=owner_id, bank_database=bank_database, now=now)


def list_paths(connection, *, owner_id, bank_database=DEFAULT_BANK, limit=50, now=None):
    ids = connection.execute('''SELECT p.id FROM learning_paths p JOIN tutor_sessions t ON t.id=p.source_session_id
        JOIN problem_submissions s ON s.id=t.submission_id WHERE s.owner_id=?
        ORDER BY p.updated_at DESC,p.id DESC LIMIT ?''', (owner_id, limit)).fetchall()
    return [snapshot(connection, path_id=row[0], owner_id=owner_id, bank_database=bank_database, now=now) for row in ids]


def advance(connection, *, path_id, owner_id, expected_revision, bank_database=DEFAULT_BANK, now=None):
    path = _owned(connection, path_id, owner_id)
    if path['revision'] != expected_revision:
        raise ConflictError('학습 경로가 바뀌었어요. 최신 기록을 확인한 뒤 다시 진행해 주세요.')
    current = snapshot(connection, path_id=path_id, owner_id=owner_id, bank_database=bank_database, now=now)
    if not current['next_action']['available']:
        raise ConflictError(current['next_action']['reason'])
    activities = _activities(connection, path_id)
    active = next((item for item in activities if item['id'] == path['current_activity_id']), None)
    mode = _next_mode(path, active)
    item, fresh, reason = _candidate(connection, path, _source(connection, path, owner_id), mode, bank_database, owner_id)
    if item is None:
        raise ConflictError(reason)
    cursor = connection.execute('''INSERT INTO learning_activities
        (path_id,catalog_item_id,source_question_id,question_text,target_text,mode,is_new_question,selection_reason,outcome_json)
        VALUES(?,?,?,?,?,?,?,?,?)''', (path_id, item['id'], item['source_question_id'], item['question_text'],
        item['target_text'], mode, int(fresh), reason, _json({'code': 'pending'})))
    activity_id = cursor.lastrowid
    # 각 시도는 새 제출·세션으로 만들고 원본의 답 공개·힌트 이력을 복사하지 않는다.
    submitted = create_text_submission(connection, owner_id=owner_id, content_text=item['target_text'])
    confirm_input_version(connection, submission_id=submitted['submission_id'], owner_id=owner_id, expected_version_number=1)
    session = tutor_sessions.create(connection, owner_id=owner_id, submission_id=submitted['submission_id'],
        expected_input_version_number=1, target_text=item['target_text'], learning_context={
            'path_id': path_id, 'activity_id': activity_id, 'mode': mode, 'catalog_item_id': item['id'],
            'type_code': path['type_code'], 'type_name': path['type_name']})
    connection.execute('UPDATE learning_activities SET session_id=? WHERE id=?', (session['id'], activity_id))
    connection.execute('''UPDATE learning_paths SET phase=?,current_activity_id=?,revision=revision+1,
        updated_at=? WHERE id=?''', (mode, activity_id, _stamp(_now(now)), path_id))
    return snapshot(connection, path_id=path_id, owner_id=owner_id, bank_database=bank_database, now=now)


def require_active_attempt(connection, *, owner_id, session_id):
    activity = _row(connection, 'SELECT * FROM learning_activities WHERE session_id=?', (session_id,))
    if activity is None:
        return
    path = _owned(connection, activity['path_id'], owner_id)
    if (path['current_activity_id'] != activity['id'] or activity['status'] != 'active'
            or path['phase'] != activity['mode']):
        raise ConflictError('이미 종료되었거나 이전 단계의 학습 시도예요. 현재 학습 경로에서 이어가 주세요.')
    if not _source(connection, path, owner_id)['is_current']:
        raise ConflictError('원본 문제가 변경되어 이전 학습 시도를 계속할 수 없어요.')
    if not tutor_sessions.snapshot(connection, session_id=session_id, owner_id=owner_id)['is_current']:
        raise ConflictError('학습 문항이 변경되어 이전 시도를 계속할 수 없어요.')


def record_tutor_result(connection, *, owner_id, session_id, result, now=None):
    """튜터 응답 저장과 같은 트랜잭션에서 실제 검산·도움 이력으로만 전이한다."""
    activity = _row(connection, 'SELECT * FROM learning_activities WHERE session_id=?', (session_id,))
    if activity is None:
        return
    require_active_attempt(connection, owner_id=owner_id, session_id=session_id)
    path = _owned(connection, activity['path_id'], owner_id)
    state, response = result['state'], result['response']
    verification = response.get('verification') or {}
    complete = (state.get('solved') is True and response.get('tool_supported') is True
                and verification.get('judgement') == 'correct' and verification.get('completion') == 'complete')
    assisted = bool(state.get('assisted') or state.get('answer_revealed') or state.get('hint_count', 0))
    independent = complete and state.get('independent_verified') is True and not assisted
    code = 'incorrect' if verification.get('judgement') == 'incorrect' else 'undetermined' if verification.get('judgement') == 'undetermined' else 'in_progress'
    status, phase, due, reviews = 'active', path['phase'], path['review_due_at'], path['review_count']
    if complete:
        if activity['mode'] == 'practice':
            code, status = 'practice_complete', 'completed'
        elif independent:
            code, status, phase = ('review_pass' if activity['mode'] == 'review' else 'independent_pass'), 'completed', 'review_due'
            reviews += int(activity['mode'] == 'review')
            due = _stamp(_now(now) + timedelta(days=(1 if reviews == 0 else 3 if reviews == 1 else 7)))
        else:
            code, status, phase, due = 'assisted_complete', 'needs_support', 'remediation', None
    outcome = {'code': code, 'verified_complete': complete, 'assisted': assisted,
               'independent_verified': independent, 'mastery_claim': False}
    connection.execute('''UPDATE learning_activities SET status=?,outcome_json=?,completed_at=? WHERE id=?''',
        (status, _json(outcome), _stamp(_now(now)) if status != 'active' else None, activity['id']))
    connection.execute('''UPDATE learning_paths SET phase=?,review_due_at=?,review_count=?,revision=revision+1,
        updated_at=? WHERE id=?''', (phase, due, reviews, _stamp(_now(now)), path['id']))
