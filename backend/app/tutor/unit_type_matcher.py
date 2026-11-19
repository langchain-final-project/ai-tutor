"""중1 1학기 문자와 식 문제은행만 읽어 유형과 참고 문항을 제안한다."""

from collections import Counter, defaultdict
from contextlib import closing
from functools import lru_cache
import hashlib
import html
import json
import math
from pathlib import Path
import re
import sqlite3
import unicodedata


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_BANK = ROOT / "data/problem_bank/problem_bank.seed.sqlite3"
SCOPE = {"grade": 1, "semester": 1, "unit_code": "03", "unit_name": "문자와 식"}
VERSION = "middle1-1-03-retrieval-v1"
MIN_SIMILARITY = 0.10


def canonical(text: str) -> str:
    text = text.replace("²", "^2").replace("³", "^3")
    text = unicodedata.normalize("NFKC", html.unescape(text)).replace("−", "-").replace("‒", "-")
    text = re.sub(r"^\s*\d{1,3}(?:[.)][ \t]+|\s*\n+)", "", text)
    text = re.sub(r"\\[()\[\]]|\$|\\(?:left|right|displaystyle|textstyle)\b", "", text)
    for old, new in ((r"\times", "*"), (r"\cdot", "*"), (r"\div", "/"), (r"\dfrac", r"\frac"), (r"\tfrac", r"\frac")):
        text = text.replace(old, new)
    return re.sub(r"\s+", "", text)


def stem(text: str) -> str:
    # 선택지의 숫자를 계수나 주어진 조건으로 잘못 해석하지 않는다.
    return re.split(r"[①②③④⑤]|\\textcircled\s*\{1\}", text, maxsplit=1)[0]


def template(text: str) -> str:
    return re.sub(r"\d+(?:\.\d+)?", "#", canonical(text))


def features(text: str) -> Counter:
    terms = Counter()
    # 요구 문장은 더 강하게 반영하되, 선택지에만 있는 수식도 버리지 않는다.
    for part, weight in ((canonical(text), 1), (canonical(stem(text)), 2)):
        for size in (2, 3, 4):
            for index in range(max(0, len(part) - size + 1)):
                terms[part[index:index + size]] += weight
    return terms


def equation_focus(text: str) -> tuple[str | None, str | None]:
    main = re.sub(r"\([1-9]\)(?=\s*(?:\$|\\\())", "", stem(text))
    body = canonical(main)
    parameter = re.search(r"(?:상수|계수|미지수)[a-z]?(?:의)?값", body)
    solving = "방정식" in body and ("풀" in body or "해를구" in body)
    if not solving or parameter:
        full = canonical(text)
        multiplication = r"\times" in text or "곱셈" in body
        division = r"\div" in text or "나눗셈" in body
        variables = re.search(r"[a-z]", re.sub(r"\\[a-zA-Z]+", "", full))
        if not parameter and variables and "계산" in body and "값" not in body:
            if division:
                return "7103025", "일차식과 수의 나눗셈을 계산하는 표현을 확인했습니다."
            if multiplication:
                return "7103024", "일차식과 수의 곱셈을 계산하는 표현을 확인했습니다."
        if "생략된" in body and "사용" in body:
            return "7103005", "생략된 연산 기호를 다시 사용하는 요구를 확인했습니다."
        generic_comparison = body.rstrip("?？.") in {
            "다음중옳은것은", "다음중옳지않은것은",
            "다음중옳은것을모두고르면", "다음중옳지않은것을모두고르면",
        }
        if "생략" in body or (variables and "=" in full and multiplication and generic_comparison):
            if multiplication and division:
                return "7103004", "곱셈과 나눗셈 기호를 생략하는 표현을 비교합니다."
            if multiplication:
                return "7103002", "곱셈 기호를 생략한 표현을 비교합니다."
            if division:
                return "7103003", "나눗셈 기호를 생략한 표현을 비교합니다."
        return None, None
    if "등식의성질" in body:
        return "7103042", "등식의 성질을 이용해 방정식을 푸는 요구를 확인했습니다."
    if "절댓값" in body or re.search(r"\|[^|]+\|", body):
        return "7103055", "방정식 풀이에 절댓값이 포함되어 있습니다."
    if "비례식" in body or ":" in body:
        return "7103052", "비례식 형태의 방정식 풀이를 확인했습니다."
    decimal = bool(re.search(r"\d+\.\d+", body))
    fraction = bool(re.search(r"\\frac|\d\s*/\s*\d", body))
    if decimal and fraction:
        return "7103051", "방정식을 푸는 문제이며 소수와 분수 표현이 함께 있습니다."
    if decimal:
        return "7103049", "방정식을 푸는 문제이며 소수 계수가 있습니다."
    if fraction:
        return "7103050", "방정식을 푸는 문제이며 분수 표현이 있습니다."
    if "(" in body or "{" in body:
        return "7103048", "방정식을 푸는 문제이며 괄호가 포함되어 있습니다."
    return "7103047", "간단한 일차방정식의 해를 구하는 요구를 확인했습니다."


class UnitTypeMatcher:
    def __init__(self, rows: list[dict]):
        if not rows:
            raise ValueError("중1 1학기 문자와 식 문제은행 문항이 없습니다.")
        self.rows = rows
        self.catalog = {row["source_topic_code"]: row["source_topic_name"] for row in rows}
        self.data_version = hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        self.exact = defaultdict(list)
        document_features = []
        frequencies = Counter()
        for index, row in enumerate(rows):
            self.exact[canonical(row["question_text"])].append(index)
            terms = features(row["question_text"])
            document_features.append(terms)
            frequencies.update(terms.keys())
        self.idf = {term: math.log((1 + len(rows)) / (1 + count)) + 1 for term, count in frequencies.items()}
        self.postings = defaultdict(list)
        for index, terms in enumerate(document_features):
            weights = {term: (1 + math.log(count)) * self.idf[term] for term, count in terms.items()}
            norm = math.sqrt(sum(value * value for value in weights.values()))
            if norm:
                for term, value in weights.items():
                    self.postings[term].append((index, value / norm))

    @classmethod
    def load(cls, path: Path):
        with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            rows = [dict(row) for row in connection.execute(
                """SELECT q.id, q.question_text, q.figure_text, q.source_topic_code, t.source_topic_name
                   FROM questions q JOIN topics t USING(source_topic_code)
                   WHERE q.grade=1 AND q.semester=1 AND q.unit_code='03' ORDER BY q.id"""
            )]
        return cls(rows)

    def predict(
        self,
        text: str,
        *,
        has_visuals=False,
        excluded_templates=frozenset(),
        minimum_similarity=MIN_SIMILARITY,
    ) -> dict:
        result = {
            "classifier_version": VERSION, "data_version": self.data_version,
            "scope": dict(SCOPE), "bank_question_count": len(self.rows), "bank_type_count": len(self.catalog),
            "status": "needs_review", "method": "retrieval", "selected_type": None,
            "matched_question_id": None, "candidates": [], "reasons": [],
        }
        if not text.strip() or re.search(r"\[UNREADABLE\]|\[판독불가\]", text, re.I):
            result["reasons"] = ["문제 본문과 판독하지 못한 부분을 먼저 확인해 주세요."]
            return result
        substance = re.sub(r"다음|중|옳지않은|옳은|것은|것을|모두|고르면|구하여라|구하시오|알맞은|보기", "", canonical(re.sub(r"[①②③④⑤]", "", text)))
        if not re.search(r"[가-힣A-Za-z0-9]", substance):
            result["reasons"] = ["문제의 조건과 수식을 포함해서 본문을 확인해 주세요."]
            return result
        if re.search(r"소인수분해|최대공약수|최소공배수|이차함수|이차방정식|연립방정식|삼각비|제곱근|피타고라스|확률", canonical(stem(text))):
            result.update(status="unsupported", reasons=["현재 분류 범위는 중1 1학기 3단원 문자와 식입니다."])
            return result
        allowed = {index for index, row in enumerate(self.rows) if template(row["question_text"]) not in excluded_templates}
        matches = [index for index in self.exact.get(canonical(text), []) if index in allowed]
        focus, focus_reason = equation_focus(text)
        if matches:
            scores = {index: 1.0 for index in matches}
        else:
            terms = features(text)
            weights = {term: (1 + math.log(count)) * self.idf[term] for term, count in terms.items() if term in self.idf}
            norm = math.sqrt(sum(value * value for value in weights.values()))
            scores = defaultdict(float)
            if norm:
                for term, value in weights.items():
                    for index, weight in self.postings[term]:
                        if index in allowed:
                            scores[index] += (value / norm) * weight
        grouped = defaultdict(list)
        for index, score in scores.items():
            if score > 0:
                grouped[self.rows[index]["source_topic_code"]].append((score, index))
        ranked = []
        for code, members in grouped.items():
            members.sort(key=lambda member: (-member[0], self.rows[member[1]]["id"]))
            references = members[:3]
            score = 0.8 * references[0][0] + 0.2 * sum(member[0] for member in references) / len(references)
            ranked.append((code, score, references))
        ranked.sort(key=lambda item: (-(not matches and item[0] == focus), -item[1], item[0]))
        if not ranked or (not matches and ranked[0][1] < minimum_similarity and ranked[0][0] != focus):
            result["reasons"] = ["범위 안에서 충분히 비슷한 문항을 찾지 못했습니다. 본문과 단원을 확인해 주세요."]
            return result
        for code, score, references in ranked[:3]:
            reasons = ["확인된 본문·수식과 비슷한 문제은행 문항을 유형별로 묶었습니다."]
            if code == focus and not matches:
                reasons.insert(0, focus_reason)
            result["candidates"].append({
                "source_topic_code": code, "source_topic_name": self.catalog[code],
                "score": round(min(1.0, max(0.0, score)), 6), "score_name": "char_tfidf_cosine",
                "reasons": reasons,
                "reference_questions": [{"id": self.rows[index]["id"], "question_text": self.rows[index]["question_text"]}
                                        for _, index in references],
            })
        result.update(status="candidates", method="hybrid" if focus and not matches else "retrieval")
        result["reasons"] = ["유사도는 정답 확률이 아닙니다. 참고 문항과 비교해 유형을 선택해 주세요."]
        if matches and len(grouped) == 1 and not has_visuals and all(not self.rows[index]["figure_text"] for index in matches):
            first = result["candidates"][0]
            result.update(status="classified", method="exact_match", matched_question_id=self.rows[matches[0]]["id"],
                          selected_type={"source_topic_code": first["source_topic_code"], "source_topic_name": first["source_topic_name"]},
                          reasons=["형식과 공백을 정리한 본문이 같은 문제은행 문항이며 유형이 일치합니다."])
        return result


AUTOMATIC_POLICY_VERSION = "tutoring-top1-v1"


def classify_for_tutoring(matcher, text: str, *, has_visuals=False) -> dict:
    result = matcher.predict(
        text,
        has_visuals=has_visuals,
        minimum_similarity=0.0,
    )
    result = {
        **result,
        "selection_policy_version": AUTOMATIC_POLICY_VERSION,
        "purpose": "tutoring_reference",
        "is_provisional": False,
    }
    if result["status"] == "classified":
        return result
    if not result["candidates"]:
        return result

    best = result["candidates"][0]
    return {
        **result,
        "status": "classified",
        "selected_type": {
            "source_topic_code": best["source_topic_code"],
            "source_topic_name": best["source_topic_name"],
        },
        "matched_question_id": None,
        "is_provisional": True,
        "reasons": [
            "확인된 본문·수식으로 찾은 1순위 유형을 단계별 안내의 참고 유형으로 선택했습니다."
        ],
    }


@lru_cache(maxsize=4)
def _cached_matcher(path: str, modified: int, size: int):
    return UnitTypeMatcher.load(Path(path))


def get_unit_matcher(path: Path = DEFAULT_BANK):
    path = Path(path).resolve()
    stat = path.stat()
    return _cached_matcher(str(path), stat.st_mtime_ns, stat.st_size)
