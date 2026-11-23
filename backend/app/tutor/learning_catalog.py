"""문제은행을 고치지 않고 두 기존 유형의 검수한 등식만 작은 학습 후보로 읽는다."""

from contextlib import closing
import hashlib
from pathlib import Path
import re
import sqlite3

from .guided_feedback import _sides
from .step_verifier import _equation
from .unit_type_matcher import DEFAULT_BANK, canonical


POLICY_VERSION = 'didgo-learning-cycle-v1'
TYPES = {
    '7103047': ('간단한 일차방정식의 풀이', '양변에 같은 계산을 해도 해가 유지되는지 확인해 보세요. 문자가 있는 항과 상수항을 정리한 다음 원래 등식에 대입해 확인해요.'),
    '7103048': ('괄호가 있는 일차방정식의 풀이', '괄호 앞의 수를 괄호 안의 각 항에 곱해 보세요. 부호를 확인하고 양변에 같은 계산을 하며 문자를 혼자 남겨요.'),
}
# 원문과 해설을 대조한 단일 등식만 고정한다. 은행 전체의 검수 완료를 뜻하지 않는다.
ITEMS = (
    ('45910', '7103047', '2x-3=-5', '5f5d95d4c16b59b6f904901624d05d2287564970e8cb27b68fc5718a23bbb09c'),
    ('45911', '7103047', '-3x+4=-2', '39b8f3401bb03d964c11a346e95acdc1befc2c98fff9d881dc68e2fb302672d9'),
    ('45912', '7103047', 'x=3x-8', 'e45910372ec71e52d13411cc2ecee520bf2405b564af60cbb8ff318d17a0643a'),
    ('45913', '7103047', '-3x=2x-10', '760e83e65ead650d8f683a2f00d1779f4fb9a83537e936ae62adf1127f0ec98a'),
    ('45918', '7103048', '2x-3=3(x+1)', '60899e84432bfe21164ab2c7ad4f87ba5aa32a439aa67d81ce1783eefa30e318'),
    ('45967', '7103048', '4x-2(x+2)=-2(x-2)', 'c1fb379e4dc869fb4d9962f8bfb62d79a8f34a9e33b6e30aa9e3e05b3809bef9'),
    ('45968', '7103048', '-3(x-1)+4=-2(x-3)', '4b1566d8c2ac49ab61905ab4ddd9b1c77148022e6d90f4f74ade82b8d6ddda4d'),
    ('45969', '7103048', '-3(2x-1)+2x=-x-6', '5df9787ac582a8fafaf63d6ec2623707c61aad02f745692f2c661ddcb7235f39'),
)


def load_catalog(bank_database=DEFAULT_BANK):
    """누락·변경된 은행은 추천을 보류하며 빈 DB나 새 문제를 만들지 않는다."""
    items = []
    try:
        with closing(sqlite3.connect(Path(bank_database).resolve().as_uri() + '?mode=ro', uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            for number, code, equation, digest in ITEMS:
                ident = f'M1_1_03_00041_{number}.png'
                row = connection.execute('''SELECT q.id,q.question_text,q.source_id,q.question_filename,
                    q.source_topic_code,q.figure_text,t.source_topic_name FROM questions q
                    JOIN topics t USING(source_topic_code)
                    WHERE q.id=? AND q.grade=1 AND q.semester=1 AND q.unit_code='03' ''', (ident,)).fetchone()
                if (row is None or row['source_topic_code'] != code or row['figure_text']
                        or hashlib.sha256(canonical(row['question_text']).encode()).hexdigest() != digest
                        or canonical(equation) not in canonical(row['question_text'])
                        or _equation(equation, 'x')['root'] is None):
                    continue
                items.append({
                    'id': ident + ':equation', 'source_question_id': ident,
                    'source_id': row['source_id'], 'question_filename': row['question_filename'],
                    'source_question_text': row['question_text'], 'type_code': code,
                    'type_name': row['source_topic_name'], 'target_text': equation,
                    'question_text': '다음 일차방정식을 풀어보세요.\n' + equation,
                })
    except (sqlite3.Error, OSError, ValueError):
        return [], '문제은행을 읽지 못했어요. 학습 기록은 보존했으니 문제은행 설정을 확인한 뒤 다시 시도해 주세요.'
    return items, None


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
