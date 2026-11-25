"""검수한 문제은행 등식과 팀 개념 카드를 학생 학습에 연결한다."""

from contextlib import closing
from fractions import Fraction
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

from scripts.lookup_problem_concepts import DEFAULT_CONCEPTS, load_index

from .guided_feedback import _sides
from .problem_target import analyze_target
from .step_verifier import _equation
from .unit_type_matcher import DEFAULT_BANK, canonical


POLICY_VERSION = 'didgo-learning-cycle-v2'
TYPES = {
    '7103047': ('간단한 일차방정식의 풀이', '양변에 같은 계산을 해도 해가 유지되는지 확인해 보세요. 문자가 있는 항과 상수항을 정리한 다음 원래 등식에 대입해 확인해요.'),
    '7103048': ('괄호가 있는 일차방정식의 풀이', '괄호 앞의 수를 괄호 안의 각 항에 곱해 보세요. 부호를 확인하고 양변에 같은 계산을 하며 문자를 혼자 남겨요.'),
    '7103050': ('계수에 분수가 있는 일차방정식의 풀이', '양변에 분모의 최소공배수를 곱해 분수를 없앤 뒤 식을 정리해 보세요.'),
}
UNIT = (1, 1, '03')
CURATED_DATA = Path(__file__).with_name('learning_curated.json')


def _item(row, equation):
    return {
        'id': row['id'] + ':equation', 'source_question_id': row['id'],
        'source_id': row['source_id'], 'question_filename': row['question_filename'],
        'source_question_text': row['question_text'], 'type_code': row['source_topic_code'],
        'type_name': row['source_topic_name'], 'target_text': equation,
        'question_text': '다음 일차방정식을 풀어보세요.\n' + equation,
    }


def _verified_equation(row):
    equation = analyze_target(row['equation_latex'])['normalized_text']
    answer = re.fullmatch(r'\s*(?:([A-Za-z])\s*=\s*)?([+-]?\d+(?:/\d+|\.\d+)?)\s*', row['answer'] or '')
    if not equation or answer is None:
        return None
    variable = re.search(r'[A-Za-z]', equation).group()
    if answer[1] is not None and answer[1] != variable:
        return None
    try:
        return equation if _equation(equation, variable)['root'] == Fraction(answer[2]) else None
    except (ValueError, ZeroDivisionError):
        return None


def load_catalog(bank_database=DEFAULT_BANK):
    """기존 검수 목록을 보존하고 식과 답이 일치하는 검수 문항을 이어 붙인다."""
    items = []
    try:
        curated = json.loads(CURATED_DATA.read_text(encoding='utf-8'))
        with closing(sqlite3.connect(Path(bank_database).resolve().as_uri() + '?mode=ro', uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            scope = '''SELECT q.* FROM questions q
                WHERE (q.grade,q.semester,q.unit_code)=(?,?,?)
                AND NOT EXISTS (SELECT 1 FROM benchmark_items b WHERE b.question_id=q.id)
                AND coalesce(trim(q.figure_text),'')='' '''
            for entry in curated:
                row = connection.execute(scope + 'AND q.id=?', (*UNIT, entry['question_id'])).fetchone()
                equation = entry['target_text']
                if (row is None or row['source_topic_code'] != entry['type_code']
                        or hashlib.sha256(canonical(row['question_text']).encode()).hexdigest() != entry['source_text_sha256']
                        or canonical(equation) not in canonical(row['question_text'])
                        or analyze_target(equation)['normalized_text'] is None):
                    continue
                items.append(_item(row, equation))
            used_ids = {item['source_question_id'] for item in items}
            signatures = {_equation_key(item['target_text']) for item in items}
            placeholders = ','.join('?' for _ in TYPES)
            rows = connection.execute(scope + f'''AND q.source_topic_code IN ({placeholders})
                AND q.review_status='verified' AND q.question_type='단답형'
                AND q.subquestion_number IS NULL ORDER BY q.id''', (*UNIT, *TYPES))
            for row in rows:
                if row['id'] in used_ids:
                    continue
                equation = _verified_equation(row)
                if equation is not None:
                    signature = _equation_key(equation)
                    if signature is not None and signature not in signatures:
                        items.append(_item(row, equation))
                        signatures.add(signature)
    except (sqlite3.Error, OSError, KeyError, ValueError):
        return [], '문제은행을 읽지 못했어요. 학습 기록은 보존했으니 문제은행 설정을 확인한 뒤 다시 시도해 주세요.'
    return items, None


@lru_cache(maxsize=4)
def _concept_index(root):
    return load_index(root)


def concept_cards(type_code):
    root = Path(os.getenv('DIDGO_CONCEPT_BANK_DIR', str(DEFAULT_CONCEPTS))).resolve()
    try:
        _, by_id, mapping, _ = _concept_index(root)
        match = mapping.get((*UNIT, type_code), {})
        cards = [by_id[ident][1] for ident in match.get('concept_ids', [])]
    except (OSError, KeyError, TypeError, ValueError):
        return []
    # 연습에 앞서 필요한 정의와 규칙부터 보여준다. 문제은행의 답과 해설은 전달하지 않는다.
    return [{key: card[key] for key in ('concept_id', 'title', 'definition', 'rules') if key in card}
            for card in cards]


def _equation_key(text):
    letters = set(re.findall(r'[A-Za-z]', text))
    try:
        return _sides(text, next(iter(letters))) if len(letters) == 1 else None
    except ValueError:
        return None


def choose(items, *, type_code, source_text, source_target, used_ids, mode, seen_questions=()):
    """계정 전체에서 새 문항을 고르며 복습만 지연 후 기존 문항을 허용한다."""
    original, equation = canonical(source_text), canonical(source_target)
    source_key = _equation_key(source_target)
    source_families = {item['source_id'] for item in items
                       if canonical(item['source_question_text']) == original
                       or canonical(item['target_text']) == equation
                       or (source_key is not None and _equation_key(item['target_text']) == source_key)}
    # 문항 ID가 없던 일반 튜터에서도 본문·확인한 식으로 이미 본 문항을 찾는다.
    used_ids = list(used_ids)
    for seen in seen_questions:
        seen_key = _equation_key(seen['target_text'])
        seen_source = canonical(seen['source_text'])
        used_ids.extend(item['id'] for item in items
                        if canonical(item['source_question_text']) == seen_source
                        or (seen_key is not None and _equation_key(item['target_text']) == seen_key))
    candidates = [item for item in items if item['type_code'] == type_code
                  and canonical(item['target_text']) != equation
                  and canonical(item['source_question_text']) != original
                  and item['source_id'] not in source_families]
    fresh = [item for item in candidates if item['id'] not in used_ids]
    if fresh:
        return fresh[0], True, '확인한 원문과 이 계정의 이전 튜터·학습 기록에서 사용하지 않은 같은 유형의 문항이에요.'
    if mode == 'review' and candidates:
        last_used = {ident: index for index, ident in enumerate(used_ids)}
        item = min(candidates, key=lambda candidate: last_used.get(candidate['id'], -1))
        return item, False, '새 문항 후보가 없어 이전 문항을 지연 복습해요. 새로운 문항의 독립 수행 평가와 구분해 기록해요.'
    return None, False, '확인한 원문과 이전 시도를 제외하면 검수된 새 문항이 부족해요. 기록은 보존하고 문항 추가 검수 전까지 다음 학습을 보류해요.'
