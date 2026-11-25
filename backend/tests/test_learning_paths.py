"""실제 쿠키·임시 DB·모델 대역으로 학습 순환과 독립 수행의 저장 경계를 확인한다."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from app.db.auth import metadata
from app.db.connection import build_engine
from app.db import learning_paths
from app.student_api import create_student_app
from app.tutor import learning_catalog
from app.tutor.session_service import run_turn


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/setup_solution_flow.py'
spec = importlib.util.spec_from_file_location('learning_path_setup', SCRIPT)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)
PASSWORD = 'learning-path-test-password'


class Model:
    def generate(self, problem, context, action):
        return {'message': '양변에 같은 계산을 하는지 확인하고 한 단계씩 적어 보세요.', 'model': 'test', 'usage': {}}


def no_pipeline():
    raise AssertionError('학습 경로 검사에서 실제 OCR 모델을 실행하면 안 됩니다.')


def runner(problem, state, events, request):
    return run_turn(problem, state, events, request, model_client=Model())


def login(client, email='learning-first@example.com', register=True):
    if register:
        assert client.post('/auth/signup', json={'name': '학습 검사', 'email': email,
            'password': PASSWORD, 'password_confirmation': PASSWORD}).status_code == 201
    assert client.post('/auth/login', json={'email': email, 'password': PASSWORD}).status_code == 200


def new_tutor(client, text):
    submitted = client.post('/submissions/text', json={'content_text': text}).json()
    assert client.post(f"/submissions/{submitted['id']}/confirm", json={
        'content_text': text, 'expected_version_number': 1}).status_code == 200
    started = client.post(f"/submissions/{submitted['id']}/tutor-sessions", json={
        'expected_input_version_number': 1, 'target_text': text})
    assert started.status_code == 201, started.text
    return started.json()


@pytest.fixture
def learner(tmp_path, monkeypatch):
    database = tmp_path / 'learning.sqlite3'
    setup.prepare(database, create_empty=True)
    engine = build_engine(f'sqlite:///{database}')
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()
    clock = {'now': datetime(2026, 11, 23, tzinfo=timezone.utc)}
    monkeypatch.setattr(learning_paths, '_now', lambda now=None: now or clock['now'])
    app = create_student_app(database, pipeline_factory=no_pipeline, tutor_runner=runner)
    with TestClient(app) as client:
        login(client)
        yield database, app, client, new_tutor(client, '2x+3=17'), clock


def start(client, source):
    response = client.post(f"/tutor-sessions/{source['id']}/learning-path", json={})
    assert response.status_code == 200, response.text
    return response.json()


def next_activity(client, path):
    response = client.post(f"/learning-paths/{path['id']}/next", json={'expected_revision': path['revision']})
    assert response.status_code == 200, response.text
    return response.json()


def turn(client, session_id, text='', intent='submit'):
    session = client.get(f'/tutor-sessions/{session_id}').json()
    payload = {'request_id': f"learning-turn-{session['revision']:04}", 'expected_revision': session['revision'],
               'intent': intent, 'student_text': text, 'steps': []}
    response = client.post(f'/tutor-sessions/{session_id}/turns', json=payload)
    assert response.status_code == 200, response.text
    return response.json(), payload


def read(client, path):
    response = client.get(f"/learning-paths/{path['id']}")
    assert response.status_code == 200, response.text
    return response.json()


def independent(client, source):
    path = next_activity(client, start(client, source))
    assert path['phase'] == 'practice'
    turn(client, path['active_session_id'], 'x=-1')
    return next_activity(client, read(client, path))


def test_cycle_uses_new_attempts_waits_for_review_and_restores(learner):
    database, _, client, source, clock = learner
    assert client.get(f"/tutor-sessions/{source['id']}/learning-path").status_code == 404
    first = start(client, source)
    assert start(client, source)['id'] == first['id']
    assert first['remediation'] and first['mastery_claim'] is False
    path = independent(client, source)
    independent_id = path['active_session_id']
    assert path['phase'] == 'independent'
    incorrect, _ = turn(client, independent_id, 'x=0')
    assert incorrect['state']['assisted'] is False
    completed, replay = turn(client, independent_id, 'x=2')
    assert completed['state']['independent_verified'] is True
    path = read(client, path)
    assert path['phase'] == 'review_due' and path['status'] == 'review_scheduled'
    assert path['active_session_id'] is None and path['mastery_claim'] is False
    assert len({item['session_id'] for item in path['history']}) == 2
    assert path['history'][-1]['outcome']['code'] == 'independent_pass'
    assert client.post(f"/learning-paths/{path['id']}/next", json={'expected_revision': path['revision']}).status_code == 409
    # 완료 요청 재전송은 같은 결과지만 종료된 독립 시도에 새 힌트를 추가할 수 없다.
    assert client.post(f'/tutor-sessions/{independent_id}/turns', json=replay).status_code == 200
    assert client.post(f'/tutor-sessions/{independent_id}/turns', json={**replay,
        'request_id': 'another-help-001', 'expected_revision': completed['revision'], 'intent': 'hint'}).status_code == 409
    clock['now'] += timedelta(days=1)
    path = next_activity(client, read(client, path))
    assert path['phase'] == 'review' and path['history'][-1]['is_new_question'] is True
    turn(client, path['active_session_id'], 'x=4')
    path = read(client, path)
    assert path['phase'] == 'review_due' and path['review_count'] == 1
    assert path['history'][-1]['outcome']['code'] == 'review_pass'
    assert path['review_due_at'].startswith('2026-11-27')
    # 서버를 다시 만들어도 같은 계정의 순환과 복습 예약을 복원한다.
    with TestClient(create_student_app(database, pipeline_factory=no_pipeline, tutor_runner=runner)) as restored:
        login(restored, register=False)
        assert read(restored, path) == path
        assert restored.get(f'/tutor-sessions/{independent_id}/learning-path').json()['id'] == path['id']


def test_independent_help_is_not_a_pass_and_delayed_review_can_reuse(learner):
    _, _, client, source, clock = learner
    path = independent(client, source)
    hinted, _ = turn(client, path['active_session_id'], intent='hint')
    assert hinted['state']['assisted'] is True
    turn(client, path['active_session_id'], intent='answer')
    assert read(client, path)['phase'] == 'independent'
    turn(client, path['active_session_id'], 'x=0')
    still_active = read(client, path)
    assert still_active['phase'] == 'independent' and still_active['history'][-1]['outcome']['code'] == 'incorrect'
    turn(client, path['active_session_id'], 'x=2')
    path = read(client, path)
    assert path['phase'] == 'remediation' and path['review_due_at'] is None
    assert path['history'][-1]['outcome']['code'] == 'assisted_complete'
    path = next_activity(client, path)
    turn(client, path['active_session_id'], 'x=4')
    path = next_activity(client, read(client, path))
    assert path['phase'] == 'independent' and path['history'][-1]['is_new_question'] is True
    turn(client, path['active_session_id'], 'x=2')
    path = read(client, path)
    clock['now'] += timedelta(days=1)
    path = next_activity(client, read(client, path))
    assert path['phase'] == 'review' and path['history'][-1]['is_new_question'] is False
    assert '지연 복습' in path['history'][-1]['selection_reason']
    assert path['history'][-1]['session_id'] != path['history'][0]['session_id']


def test_fraction_uses_bank_questions_and_concept_cards(learner):
    _, _, client, _, clock = learner
    source = new_tutor(client, r'\frac{x+3}{2}=x-1')
    assert source['target_text'] == '(x+3)/2=x-1'
    availability = f"/tutor-sessions/{source['id']}/learning-availability"
    assert client.get(availability).json()['available'] is True
    assert client.get(f"/tutor-sessions/{source['id']}/learning-path").status_code == 404
    path = start(client, source)
    assert path['type']['code'] == '7103050'
    assert any(card['concept_id'] == 'm1.s1.u03.c021' for card in path['remediation']['concept_cards'])
    path = next_activity(client, path)
    assert path['history'][-1]['target_text'] == '(1/2)*x-2/3=-7/6'
    turn(client, path['active_session_id'], 'x=-1')
    path = next_activity(client, read(client, path))
    assert path['phase'] == 'independent' and path['history'][-1]['is_new_question'] is True
    assert path['history'][-1]['target_text'] == '(x+1)/2-(4x+1)/3=5/4'
    correct, _ = turn(client, path['active_session_id'], 'x=-13/10')
    assert correct['state']['independent_verified'] is True
    path = read(client, path)
    assert path['status'] == 'review_scheduled'
    clock['now'] += timedelta(days=1)
    assert next_activity(client, read(client, path))['phase'] == 'review'

    unsupported = new_tutor(client, '0.5x=4')
    assert client.get(f"/tutor-sessions/{unsupported['id']}/learning-availability").json() == {
        'available': False, 'reason': '이 풀이 유형의 추가 학습 문항은 아직 준비되지 않았어요.',
    }


def test_cookie_ownership_and_duplicate_next_are_enforced(learner):
    _, app, client, source, _ = learner
    path = start(client, source)
    body = {'expected_revision': path['revision']}
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: client.post(f"/learning-paths/{path['id']}/next", json=body).status_code, range(2)))
    assert sorted(results) == [200, 409]
    assert len(read(client, path)['history']) == 1
    with TestClient(app) as second:
        assert second.get('/learning-paths').status_code == 401
        login(second, 'learning-second@example.com')
        assert second.get('/learning-paths').json() == []
        assert second.get(f"/learning-paths/{path['id']}").status_code == 404
        assert second.post(f"/learning-paths/{path['id']}/next", json=body).status_code == 404
        assert second.get(f"/tutor-sessions/{source['id']}/learning-path").status_code == 404
        assert second.get(f"/tutor-sessions/{source['id']}/learning-availability").status_code == 404
        assert second.post(f"/tutor-sessions/{source['id']}/learning-path", json={}).status_code == 404


def test_new_questions_exclude_the_students_other_paths_and_answer_history(learner):
    _, app, client, source, _ = learner
    viewed = new_tutor(client, '2*y-3=-5')
    answered, _ = turn(client, viewed['id'], intent='answer')
    assert answered['state']['answer_revealed'] is True
    with TestClient(app) as other:
        login(other, 'learning-other-history@example.com')
        someone_elses = new_tutor(other, '-3x+4=-2')
        turn(other, someone_elses['id'], intent='answer')
    path = next_activity(client, start(client, source))
    # 다른 학생에게 공개한 문항은 이 학생의 새 문항 후보에서 제외하지 않는다.
    assert path['history'][-1]['target_text'] == '-3x+4=-2'
    turn(client, path['active_session_id'], 'x=2')
    path = next_activity(client, read(client, path))
    assert path['history'][-1]['target_text'] == 'x=3x-8'
    assert path['phase'] == 'independent'
    another = next_activity(client, start(client, new_tutor(client, '4x+5=33')))
    assert another['history'][-1]['target_text'] == '-3x=2x-10'
    turn(client, another['active_session_id'], 'x=2')
    exhausted = read(client, another)
    assert exhausted['status'] == 'unavailable'
    assert '새 문항이 부족' in exhausted['next_action']['reason']
    assert client.post(f"/learning-paths/{another['id']}/next", json={'expected_revision': exhausted['revision']}).status_code == 409


@pytest.mark.parametrize('edit_original', [True, False])
def test_changed_source_or_activity_blocks_next_and_tutor_turn(learner, edit_original):
    _, _, client, source, _ = learner
    path = next_activity(client, start(client, source))
    active = client.get(f"/tutor-sessions/{path['active_session_id']}").json()
    target = source if edit_original else active
    response = client.post(f"/submissions/{target['submission_id']}/confirm", json={
        'content_text': '2x+3=19', 'expected_version_number': 1})
    assert response.status_code == 200, response.text
    stale = read(client, path)
    assert stale['status'] == ('source_changed' if edit_original else 'activity_changed')
    assert not stale['next_action']['available']
    assert client.post(f"/learning-paths/{path['id']}/next", json={'expected_revision': stale['revision']}).status_code == 409
    assert client.post(f"/tutor-sessions/{active['id']}/turns", json={
        'request_id': 'stale-study-001', 'expected_revision': 0,
        'intent': 'submit', 'student_text': 'x=-1', 'steps': []}).status_code == 409


def test_catalog_is_bounded_and_missing_bank_is_unavailable(learner, tmp_path, monkeypatch):
    _, _, client, source, _ = learner
    items, error = learning_catalog.load_catalog()
    assert error is None and len(items) >= 14
    assert {item['type_code'] for item in items} == {'7103047', '7103048', '7103050'}
    assert all('answer' not in item for item in items)
    # 곱셈 기호·변수 표기만 바꾼 원문을 새 독립 문항으로 내지 않는다.
    selected, fresh, _ = learning_catalog.choose(items, type_code='7103047',
        source_text='2*y-3=-5', source_target='2*y-3=-5', used_ids=[], mode='independent')
    assert fresh and selected['target_text'] != '2x-3=-5'
    missing = tmp_path / 'missing-bank.sqlite3'
    available, reason = learning_catalog.load_catalog(missing)
    assert available == [] and reason and not missing.exists()
    monkeypatch.setattr(learning_catalog, 'load_catalog', lambda _bank: (available, reason))
    assert client.get(f"/tutor-sessions/{source['id']}/learning-availability").json() == {
        'available': False, 'reason': reason,
    }
    path = start(client, source)
    assert path['status'] == 'unavailable' and path['next_action']['available'] is False
    assert '문제은행' in path['next_action']['reason']
    assert client.post(f"/learning-paths/{path['id']}/next", json={'expected_revision': 0}).status_code == 409
