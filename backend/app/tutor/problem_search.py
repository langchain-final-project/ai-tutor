import argparse
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3


ROOT = Path(__file__).resolve().parents[3]

LOCAL_DB = ROOT / "data/problem_bank/didgo.sqlite3"
SHARED_DB = ROOT / "data/problem_bank/problem_bank.seed.sqlite3"

# 공백과 일부 기호 정리
def normalize_text(text: str) -> str: 
    # 서로 다른 모양의 마이너스를 같은 문자로 맞춥니다.
    text = text.replace("−", "-").replace("‒", "-")

    # 연속된 공백과 줄바꿈을 공백 하나로 정리합니다.
    text = re.sub(r"\s+", " ", text).strip()

    # 연산 기호와 괄호 주변의 공백을 정리합니다.
    text = re.sub(r"\s*([=+\-*/()])\s*", r"\1", text)
    return text


def find_exact_candidates(question_text: str, database_path: Path,) -> list[dict]:
    """정규화한 본문이 같은 문항들을 모두 반환합니다."""

    normalized_input = normalize_text(question_text)

    if not normalized_input:
        raise ValueError("문제 본문을 입력해 주세요.")

    # mode=ro: 파일이 없어도 빈 DB를 만들지 않는 읽기 전용 연결
    database_uri = database_path.resolve().as_uri() + "?mode=ro"

    with closing(sqlite3.connect(database_uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row

        rows = connection.execute(
            """
            SELECT id, question_text, equation_latex, figure_text,
                   source_topic_code, source_topic_name
            FROM questions
            ORDER BY id
            """
        )

        candidates = []

        for row in rows:
            if normalize_text(row["question_text"]) == normalized_input:
                candidates.append(dict(row))

    return candidates

def make_bigrams(text: str) -> set[str]:
    """유사도 비교에 사용할 두 글자 묶음을 만듭니다."""

    # 공백을 제외하는 처리는 유사 검색용 특징 추출에만 사용합니다.
    compact_text = normalize_text(text).replace(" ", "")

    return {
        compact_text[index:index + 2]
        for index in range(len(compact_text) - 1)
    }


def find_similar_candidates(
    question_text: str,
    database_path: Path,
    top_k: int = 5,
) -> list[dict]:
    """문자 묶음의 겹침 정도로 유사 문항을 검색합니다."""

    input_bigrams = make_bigrams(question_text)

    if not input_bigrams:
        raise ValueError("문제 본문을 두 글자 이상 입력해 주세요.")

    if top_k < 1:
        raise ValueError("반환할 후보 수는 1 이상이어야 합니다.")

    database_uri = database_path.resolve().as_uri() + "?mode=ro"

    with closing(sqlite3.connect(database_uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row

        rows = connection.execute(
            """
            SELECT id, question_text, equation_latex, figure_text,
                   source_topic_code, source_topic_name
            FROM questions
            ORDER BY id
            """
        )

        candidates = []

        for row in rows:
            candidate_bigrams = make_bigrams(row["question_text"])

            if not candidate_bigrams:
                continue

            overlap_count = len(input_bigrams & candidate_bigrams)

            # Dice 유사도: 공통 묶음 수 × 2 / 양쪽 묶음 수의 합
            similarity = (
                2 * overlap_count
                / (len(input_bigrams) + len(candidate_bigrams))
            )

            if similarity == 0:
                continue

            candidate = dict(row)
            candidate["similarity_score"] = similarity
            candidates.append(candidate)

    # 점수가 높은 순서로 정렬하고, 동점이면 문항 ID로 정렬합니다.
    candidates.sort(
        key=lambda candidate: (
            -candidate["similarity_score"],
            candidate["id"],
        )
    )

    top_candidates = candidates[:top_k]

    for candidate in top_candidates:
        candidate["similarity_score"] = round(
            candidate["similarity_score"], 4
        )

    return top_candidates

def get_problem_features(
    question_text: str,
    equation_latex: str | None = None,
) -> dict:
    """문제 목표와 비교 가능한 수식 모양을 추출합니다."""

    # 수식 안의 글자는 제외하고 문제의 요구 문장을 살펴봅니다.
    prompt = re.sub(
        r"\$[^$]*\$",
        "",
        normalize_text(question_text),
    ).replace(" ", "")

    goals = set()

    if (
        "해를구" in prompt
        or ("방정식" in prompt and "풀어라" in prompt)
    ):
        goals.add("solve_equation")

    if re.search(
        r"(?:상수|계수)[a-z]?(?:의)?값",
        prompt,
    ):
        goals.add("find_parameter")

    # 목표가 없거나 여러 목표가 감지되면 판단을 보류합니다.
    goal = next(iter(goals)) if len(goals) == 1 else "unknown"

    # 별도 수식이 없으면 본문의 $...$에서 등식 하나를 찾습니다.
    if not equation_latex:
        equations = [
            part
            for part in re.findall(r"\$([^$]+)\$", question_text)
            if "=" in part
        ]

        equation_latex = (
            equations[0]
            if len(equations) == 1
            else None
        )

    equation_shape = None

    if equation_latex:
        compact_equation = normalize_text(
            equation_latex
        ).replace(" ", "")

        # 첫 버전은 x와 기본 기호로 적힌 등식만 비교합니다.
        if (
            compact_equation.count("=") == 1
            and re.fullmatch(
                r"[0-9x+\-*/().=]+",
                compact_equation,
            )
        ):
            equation_shape = re.sub(
                r"\d+(?:\.\d+)?",
                "#",
                compact_equation,
            )

    return {
        "goal": goal,
        "equation_shape": equation_shape,
    }

def rerank_candidates(
    question_text: str,
    candidates: list[dict],
) -> list[dict]:
    """목표와 수식 모양을 비교해 후보 순서를 조정합니다."""

    input_features = get_problem_features(question_text)
    ranked_candidates = []

    for candidate in candidates:
        features = get_problem_features(
            candidate["question_text"],
            candidate["equation_latex"],
        )

        input_goal = input_features["goal"]
        candidate_goal = features["goal"]

        if "unknown" in (input_goal, candidate_goal):
            goal_relation = "unknown"
        elif input_goal == candidate_goal:
            goal_relation = "same"
        else:
            goal_relation = "conflict"

        input_shape = input_features["equation_shape"]
        candidate_shape = features["equation_shape"]

        if not input_shape or not candidate_shape:
            shape_relation = "unknown"
        elif input_shape == candidate_shape:
            shape_relation = "same"
        else:
            shape_relation = "different"

        # 원래 후보에 비교 근거를 추가합니다.
        reviewed_candidate = {
            **candidate,
            "features": features,
            "goal_relation": goal_relation,
            "shape_relation": shape_relation,
        }

        ranked_candidates.append(reviewed_candidate)

    goal_priority = {
        "same": 2,
        "unknown": 1,
        "conflict": 0,
    }

    ranked_candidates.sort(
        key=lambda candidate: (
            -goal_priority[candidate["goal_relation"]],
            -(candidate["shape_relation"] == "same"),
            -candidate["similarity_score"],
            candidate["id"],
        )
    )

    return ranked_candidates

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", required=True)
    args = parser.parse_args()

    database_path = LOCAL_DB if LOCAL_DB.exists() else SHARED_DB

    # 먼저 동일 본문을 검색합니다.
    candidates = find_exact_candidates(args.text, database_path)

    if candidates:
        search_method = "exact_text"
        status = "exact_text_candidates_found"

    else:
        # 먼저 문자 유사도로 후보를 넉넉하게 가져옵니다.
        candidates = find_similar_candidates(
            question_text=args.text,
            database_path=database_path,
            top_k=30,
        )

        # 목표와 수식 모양을 비교한 뒤 상위 5개를 보여줍니다.
        candidates = rerank_candidates(
            question_text=args.text,
            candidates=candidates,
        )[:5]

        search_method = "bigram_with_goal_and_shape"
        status = (
            "similar_candidates_found"
            if candidates
            else "no_candidates"
        )

    result = {
        "status": status,
        "search_method": search_method,
        "normalized_text": normalize_text(args.text),
        "candidate_count": len(candidates),
        "needs_confirmation": True,
        "candidates": candidates,
        "input_features": get_problem_features(args.text),
    }

    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()