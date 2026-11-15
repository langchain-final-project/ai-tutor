"""OCR 검토 지점을 찾는다. 식의 정오나 인식 정확도를 판정하지 않는다."""

import re


def text_issues(text: str) -> list[str]:
    issues = []
    if not text.strip():
        return ["인식된 내용이 없습니다."]
    if "[판독불가]" in text:
        issues.append("판독하지 못한 부분을 확인해 주세요.")
    # 수식 구분용 \( \) \[ \]는 괄호 검사에서 제외한다.
    expression = re.sub(r"\\[()\[\]]", "", text)
    stack = []
    for char in expression:
        if char in "([{":
            stack.append(char)
        elif char in ")]}":
            if not stack or stack.pop() != {")": "(", "]": "[", "}": "{"}[char]:
                issues.append("괄호의 짝이 맞지 않습니다.")
                break
    else:
        if stack:
            issues.append("닫히지 않은 괄호가 있습니다.")
    if "보기" in text:
        view = re.split(r"①|\\textcircled\s*\{1\}", text, maxsplit=1)[0]
        labels = re.findall(r"\(([ㄱ-ㅎ])\)", view)
        if len(labels) != len(set(labels)) or re.search(r"(?m)^\s*[7LCDR2]\.", view):
            issues.append("보기 이름이 중복되거나 다른 문자로 읽혔을 수 있습니다.")
    return issues


def compare_readings(primary: str, secondary: str) -> list[str]:
    """표기 차이도 경보를 만들 수 있다. 일치하더라도 올바른 인식이라는 보장은 없다."""
    def normalize(text):
        text = text.translate(str.maketrans({"²": "^2", "³": "^3", "−": "-", "‒": "-"}))
        text = re.sub(r"\\textcircled\s*\{[1-5]\}", "", text)
        return re.sub(r"(?m)^\s*\([1-5]\)", "", text)

    a, b = normalize(primary), normalize(secondary)
    issues = []
    if re.findall(r"\d+(?:\.\d+)?", a) != re.findall(r"\d+(?:\.\d+)?", b):
        issues.append("두 판독의 숫자열이 다릅니다. 분수·지수·보기 번호도 확인해 주세요.")
    if a.count("-") != b.count("-"):
        issues.append("두 판독의 음수·뺄셈 부호 개수가 다릅니다.")
    if ("옳지않" in re.sub(r"\s", "", a)) != ("옳지않" in re.sub(r"\s", "", b)):
        issues.append("두 판독의 '옳지 않은' 표현이 다릅니다.")
    if "밑줄" in a and ("underline" in a) != ("underline" in b):
        issues.append("두 판독의 밑줄 표시가 다릅니다.")
    return issues
