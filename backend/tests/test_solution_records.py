#임시 SQLite에서 문제·풀이 버전과 검증/안내 기록의 연결을 검사

from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from app.db.submissions import create_text_submission, create_submission, confirm_input_version, save_student_input_version
from app.db import solution_records as records

SCHEMAS = Path(__file__).resolve().parents[1] / 'migrations'


@pytest.fixture
def database(tmp_path):
    database = tmp_path / 'solutions.sqlite3'
    with closing(sqlite3.connect(database)) as c:
        for name in ['001_problem_bank', '002_problem_classification', '004_problem_submissions',
                     '005_problem_ocr_jobs', '006_solution_verification']:
            c.executescript((SCHEMAS / (name + '.sqlite.sql')).read_text())
    return database


def question(c, text='2*(x+3)=10', owner='student-A', confirmed=True):
    ident = create_text_submission(c, owner_id=owner, content_text=text)['submission_id']
    if confirmed:
        confirm_input_version(c, submission_id=ident, owner_id=owner, expected_version_number=1)
    return ident


def steps(*lines, states=None):
    return [{'id': f's{i}', 'text': line, 'state': states[i - 1] if states else 'active'}
            for i, line in enumerate(lines, 1)]


def save(c, ident, lines, problem=1, previous=0):
    return records.save_work(c, submission_id=ident, owner_id='student-A',
        expected_problem_version_number=problem, expected_work_version_number=previous, steps=lines)


def confirm(c, ident, problem=1, work=1):
    return records.confirm_work(c, submission_id=ident, owner_id='student-A',
        expected_problem_version_number=problem, expected_work_version_number=work)


def evaluate(c, ident, problem=1, work=1):
    return records.evaluate_solution(c, submission_id=ident, owner_id='student-A',
        expected_problem_version_number=problem, expected_work_version_number=work)


def test_correct_result_and_view_restore_after_reopen(database):
    with records.solution_connection(database, write=True) as c:
        ident = question(c)
        save(c, ident, steps('x+3=5', 'x=2'))
        confirm(c, ident)
        result = evaluate(c, ident)
        assert result['is_current'] is True
        assert result['evaluation']['verification']['judgement'] == 'correct'
        assert result['evaluation']['model_route']['model_required'] is False
        assert records.list_feedback_views(c, submission_id=ident, owner_id='student-A') == []
        first = records.record_feedback_view(c, submission_id=ident, owner_id='student-A', result_id=result['id'])
        second = records.record_feedback_view(c, submission_id=ident, owner_id='student-A', result_id=result['id'])
        assert first == second
    with records.solution_connection(database) as c:
        restored = records.get_current_verification(c, submission_id=ident, owner_id='student-A')
        assert restored == result
        views = records.list_feedback_views(c, submission_id=ident, owner_id='student-A')
        assert views == [first]
        assert views[0]['feedback'] == result['evaluation']['student_feedback']


def test_middle_error_remains_after_right_final_answer(database):
    with records.solution_connection(database, write=True) as c:
        ident = question(c)
        save(c, ident, steps('2*x+3=10', 'x=2'))
        confirm(c, ident)
        value = evaluate(c, ident)['evaluation']
        assert value['verification']['judgement'] == 'incorrect'
        assert value['verification']['completion'] == 'complete'
        assert value['verification']['first_error_step_id'] == 's1'
        assert value['error_evidence']['target_side'] == 'lhs'


def test_uncertain_prefix_and_confirmed_deleted_line(database):
    with records.solution_connection(database, write=True) as c:
        ident = question(c)
        save(c, ident, steps('?', 'x=3', states=['uncertain', 'active']))
        confirm(c, ident)
        result = evaluate(c, ident)['evaluation']['verification']
        assert result['first_error_step_id'] is None
        assert result['first_observed_error_step_id'] == 's2'
        assert result['action'] == 'ask_confirmation'
        save(c, ident, steps('x=3', 'x=2', states=['deleted', 'active']), previous=1)
        draft = evaluate(c, ident, work=2)
        assert draft['evaluation']['verification']['action'] == 'ask_confirmation'
        confirm(c, ident, work=2)
        result = evaluate(c, ident, work=2)
        assert result['evaluation']['verification']['judgement'] == 'correct'
        assert not draft['input_fingerprint'] == result['input_fingerprint']


def test_confirmation_changes_current_fingerprint(database):
    with records.solution_connection(database, write=True) as c:
        ident = question(c, confirmed=False)
        first = evaluate(c, ident, work=0)
        assert first['evaluation']['verification']['action'] == 'ask_confirmation'
        confirm_input_version(c, submission_id=ident, owner_id='student-A', expected_version_number=1)
        assert records.get_current_verification(c, submission_id=ident, owner_id='student-A') is None
        save(c, ident, steps('x=2'))
        draft = evaluate(c, ident)
        assert draft['evaluation']['verification']['action'] == 'ask_confirmation'
        confirm(c, ident)
        old = records.get_verification(c, submission_id=ident, owner_id='student-A', result_id=draft['id'])
        assert old['is_current'] is False
        assert old['evaluation']['verification']['action'] == 'ask_confirmation'
        with pytest.raises(records.ConflictError):
            records.record_feedback_view(c, submission_id=ident, owner_id='student-A', result_id=draft['id'])


def test_problem_revision_resets_current_work_and_retains_history(database):
    with records.solution_connection(database, write=True) as c:
        ident = question(c)
        save(c, ident, steps('x=2'))
        confirm(c, ident)
        result = evaluate(c, ident)
        records.record_feedback_view(c, submission_id=ident, owner_id='student-A', result_id=result['id'])
        save_student_input_version(c, submission_id=ident, owner_id='student-A', content_text='x=3', expected_version_number=1)
        assert records.get_work(c, submission_id=ident, owner_id='student-A') is None
        assert records.get_current_verification(c, submission_id=ident, owner_id='student-A') is None
        assert records.get_verification(c, submission_id=ident, owner_id='student-A', result_id=result['id'])['is_current'] is False
        assert len(records.list_feedback_views(c, submission_id=ident, owner_id='student-A')) == 1
        with pytest.raises(records.ConflictError):
            evaluate(c, ident)
        created = save(c, ident, steps('x=3'), problem=2)
        assert created['version_number'] == 1


def test_no_work_first_hint_and_unsupported_math(database):
    with records.solution_connection(database, write=True) as c:
        ident = question(c)
        result = evaluate(c, ident, work=0)['evaluation']
        assert result['verification']['completion'] == 'no_work'
        assert result['model_route']['reason'] == 'first_hint_requested'
        for equation in ['x*x=4', 'x=x', '問題を解け', 'x+y=4']:
            ident = question(c, equation)
            result = evaluate(c, ident, work=0)['evaluation']
            assert result['verification']['action'] == 'unsupported'
            assert result['verification']['judgement'] == 'undetermined'


def test_variable_is_derived_from_stored_equation(database):
    with records.solution_connection(database, write=True) as c:
        ident = question(c, '2*y=8')
        save(c, ident, steps('y=4'))
        confirm(c, ident)
        assert evaluate(c, ident)['evaluation']['verification']['judgement'] == 'correct'


def test_owner_filter_applies_to_all_reads_and_writes(database):
    with records.solution_connection(database, write=True) as c:
        ident = question(c)
        result = evaluate(c, ident, work=0)
        for callback in [
            lambda: records.get_work(c, submission_id=ident, owner_id='student-B'),
            lambda: records.get_current_verification(c, submission_id=ident, owner_id='student-B'),
            lambda: records.get_verification(c, submission_id=ident, owner_id='student-B', result_id=result['id']),
            lambda: records.list_verifications(c, submission_id=ident, owner_id='student-B'),
            lambda: records.list_feedback_views(c, submission_id=ident, owner_id='student-B'),
            lambda: records.record_feedback_view(c, submission_id=ident, owner_id='student-B', result_id=result['id']),
            lambda: records.save_work(c, submission_id=ident, owner_id='student-B', expected_problem_version_number=1, expected_work_version_number=0, steps=[]),
            lambda: records.confirm_work(c, submission_id=ident, owner_id='student-B', expected_problem_version_number=1, expected_work_version_number=0),
            lambda: records.evaluate_solution(c, submission_id=ident, owner_id='student-B', expected_problem_version_number=1, expected_work_version_number=0),
        ]:
            with pytest.raises(LookupError):
                callback()
        assert records.list_owned_submissions(c, owner_id='student-B') == []
        assert records.list_owned_submissions(c, owner_id='student-A')[0]['id'] == ident


def test_old_version_writes_fail_without_new_records(database):
    with records.solution_connection(database, write=True) as c:
        ident = question(c)
        save(c, ident, steps('x=2'))
        with pytest.raises(records.ConflictError):
            save(c, ident, steps('x=3'))
        with pytest.raises(records.ConflictError):
            evaluate(c, ident, work=0)
        assert c.execute('SELECT count(*) FROM student_work_versions').fetchone()[0] == 1
        assert c.execute('SELECT count(*) FROM solution_verifications').fetchone()[0] == 0


@pytest.mark.parametrize('bad', [
    [{'id': 's1', 'text': 'x=2', 'state': 'active', 'confirmed': True}],
    [{'id': 's1', 'text': 'x=2', 'state': 'active'}, {'id': 's1', 'text': 'x=2', 'state': 'active'}],
    [{'id': '', 'text': 'x=2', 'state': 'active'}],
    [{'id': 's1', 'text': 'x=2', 'state': 'deleted?'}],
    [{'id': 's1', 'text': 2, 'state': 'active'}],
    [{'id': 's1', 'text': 'x'*10001, 'state': 'active'}],
    [{'id': str(i), 'text': 'x=2', 'state': 'active'} for i in range(101)],
])
def test_bad_work_contract_is_not_saved(database, bad):
    with records.solution_connection(database, write=True) as c:
        ident = question(c)
        with pytest.raises(ValueError):
            save(c, ident, bad)
        assert records.get_work(c, submission_id=ident, owner_id='student-A') is None


def test_database_constraints_and_transaction_rollback(database):
    with records.solution_connection(database, write=True) as c:
        ident = question(c)
        other = question(c)
        work = save(c, ident, steps('x=2'))
        with pytest.raises(sqlite3.IntegrityError):
            c.execute('UPDATE student_work_versions SET steps_json = ? WHERE id = ?', ('[]', work['id']))
        with pytest.raises(sqlite3.IntegrityError):
            c.execute('INSERT INTO student_work_versions (submission_id,problem_input_version_id,version_number,steps_json) VALUES (?,?,1,?)', (other, work['problem_input_version_id'], '[]'))
    with pytest.raises(RuntimeError):
        with records.solution_connection(database, write=True) as c:
            question(c)
            raise RuntimeError('rollback')
    with records.solution_connection(database) as c:
        assert c.execute('SELECT count(*) FROM problem_submissions').fetchone()[0] == 2


def test_history_survives_current_input_outside_verifier_contract(database):
    with records.solution_connection(database, write=True) as c:
        ident = question(c)
        result = evaluate(c, ident, work=0)
        save_student_input_version(c, submission_id=ident, owner_id='student-A',
                                   content_text='x' * 10001, expected_version_number=1)
        old = records.get_verification(c, submission_id=ident, owner_id='student-A', result_id=result['id'])
        assert old['evaluation'] == result['evaluation']
        assert old['is_current'] is False
        assert records.get_current_verification(c, submission_id=ident, owner_id='student-A') is None
        assert len(records.list_verifications(c, submission_id=ident, owner_id='student-A')) == 1
        with pytest.raises(ValueError):
            evaluate(c, ident, problem=2, work=0)
