"""원문은 보존하고 명확한 단일 등식과 선택이 필요한 소문항만 구분한다."""

import re

from .step_verifier import MAX_TEXT, _equation


MAX_SOURCE = 24000
MAX_CHOICES = 10
MAX_CHOICE_TEXT = 2000
_NUMBER = r'[+-]?(?:\d{1,32}(?:\.\d{0,32})?|\.\d{1,32})'
_FRACTION = re.compile(r'\\(?:dfrac|tfrac|frac)\s*\{\s*(' + _NUMBER + r')\s*\}\s*\{\s*(' + _NUMBER + r')\s*\}')
_MARKER = re.compile(r'(?<![A-Za-z0-9\\])\(([1-9]\d?)\)')
_HEADER = re.compile(
    r'\s*(?:\d{1,3}[.)]\s*)?(?:(?:다음|각)\s*)?(?:일차\s*)?방정식(?:들)?\s*'
    r'(?:을\s*풀(?:어라|어보세요|어보자|으시오)|의\s*해를\s*구(?:하여라|하시오|하세요))\s*[.:：]?\s*'
)
_JOINT_OR_CHOICE = re.compile(r'연립|동시에|동시|조건|일\s*때|다음\s*중|고르|옳|알맞|보기|객관식|[①-⑳]')


def _open(message='이번에 풀 부분을 확인해 주세요. 이 문제는 지원되는 단일 일차방정식으로 자동 확정하지 않았어요.'):
    return {'status': 'open_problem', 'normalized_text': None, 'choices': [], 'message': message}


def _normalize(text):
    """숫자 분수와 수식 표식만 제한 변환하고 뜻이 모호한 나눗셈은 고치지 않는다."""
    if len(text) > MAX_CHOICE_TEXT:
        return None
    value = text.strip().translate(str.maketrans({
        '−': '-', '‒': '-', '﹣': '-', '－': '-', '×': '*', '÷': '/',
        '（': '(', '）': ')', '＝': '=',
    }))
    value = value.replace(r'\{', '{').replace(r'\}', '}')
    value = re.sub(r'\\(?:left|right)\s*(?=[()[\]{}])', '', value)
    value = re.sub(r'\\[()[\]]', ' ', value).replace('$', ' ')
    value = re.sub(r'\\(?:displaystyle|textstyle|qquad|quad)\b|\\[,;! ]', ' ', value)
    value = re.sub(r'\\(?:cdot|times)\b', '*', value)
    # 숫자 바로 뒤 분수는 대분수인지 곱셈인지 확인 없이 정하지 않는다.
    for match in _FRACTION.finditer(value):
        before = value[:match.start()].rstrip()
        if before and (before[-1].isdigit() or before[-1] == '.'):
            return None

    def fraction(match):
        numerator, denominator = match.groups()
        before, after = value[:match.start()].rstrip(), value[match.end():].lstrip()
        grouped = '(' + numerator + '/' + denominator + ')'
        # LaTeX의 분수 계수만 명시 곱셈으로 옮긴다. 평문 4/3x는 그대로 보류한다.
        if after and (after[0].isalpha() or after[0] == '('):
            return grouped if before.endswith('/') else grouped + '*'
        if not before or before[-1] in '+-=(':
            return numerator + '/' + denominator
        return grouped

    value = _FRACTION.sub(fraction, value)
    value = value.translate(str.maketrans({'[': '(', ']': ')', '{': '(', '}': ')'}))
    value = value.strip()
    if not value or len(value) > MAX_TEXT:
        return None
    letters = set(re.findall(r'[A-Za-z]', value))
    if len(letters) != 1:
        return None
    try:
        parsed = _equation(value, next(iter(letters)))
    except ValueError:
        return None
    # 파싱 전에 공백을 합치면 '1 2x'가 '12x'로 바뀔 수 있어 검증 뒤에만 정리한다.
    return re.sub(r'\s+', '', value) if parsed['root'] is not None else None


def _single(text):
    normalized = _normalize(text)
    if normalized is None:
        return _open()
    return {'status': 'single_equation', 'normalized_text': normalized, 'choices': [],
            'message': '확인한 단일 일차방정식을 지원 범위에서 검증할 수 있어요.'}


def analyze_target(text):
    """소문항 선택은 풀이 대상 확정이며 원문 전체의 정오·인식 정확도 판정이 아니다."""
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_SOURCE:
        return _open('풀 문제를 24000자 이내로 확인해 주세요.')
    original = text.strip()
    markers = list(_MARKER.finditer(original))
    if len(markers) > MAX_CHOICES:
        return _open('소문항이 많아요. 이번에 풀 소문항 하나를 직접 선택해 주세요.')
    if len(markers) >= 2:
        numbers = [int(match.group(1)) for match in markers]
        prefix = original[:markers[0].start()]
        clear_header = _HEADER.fullmatch(prefix) is not None
        separate = not prefix.strip() or clear_header
        sequential = numbers == list(range(numbers[0], numbers[0] + len(numbers)))
        if separate and sequential and not _JOINT_OR_CHOICE.search(original):
            choices, valid = [], True
            for index, marker in enumerate(markers):
                end = markers[index + 1].start() if index + 1 < len(markers) else len(original)
                part = original[marker.end():end].strip(' \t\r\n;；')
                normalized = _normalize(part)
                # 명확한 풀이 지시가 있으면 표기 미지원도 먼저 소문항을 고르게 한다.
                # 이때 모호한 4/3x 등에 괄호를 넣어 정답 검증 대상으로 바꾸지 않는다.
                if (not part or len(part) > MAX_CHOICE_TEXT
                        or (normalized is None and not (clear_header and part.count('=') + part.count('＝') == 1))):
                    valid = False
                    break
                number = marker.group(1)
                choices.append({'id': 'subquestion-' + number, 'label': '(' + number + ')',
                                'display_text': part, 'target_text': normalized or part})
            if valid:
                return {'status': 'selection_required', 'normalized_text': None, 'choices': choices,
                        'message': '여러 소문항이 있어요. 이번에 풀 번호를 골라 주세요.'}
    # 명확한 풀이 지시 뒤의 단일 식만 분리한다. 일반 서술의 조건은 지우지 않는다.
    header = _HEADER.match(original)
    if header is not None and not _JOINT_OR_CHOICE.search(original):
        body = original[header.end():].strip()
        marker = _MARKER.match(body)
        if marker is not None:
            body = body[marker.end():].strip()
        return _single(body)
    if len(markers) == 1 and markers[0].start() == 0:
        after = original[markers[0].end():].lstrip()
        # 번호 뒤 수식 구분자가 있을 때만 소문항 번호로 간주한다.
        if after.startswith(('$', r'\(', r'\[')):
            return _single(after)
    return _single(original)
