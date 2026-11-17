"""유형 분류의 입력·출력 계약. 모델 실행·검색·DB 저장은 하지 않는다.

사용: request = ClassificationRequest.model_validate(payload)
      result = validate_decision(request, decision_payload)
      result.model_dump(mode="json")

확정은 문항 자체의 일치 여부와 구분한다. matched_question_id가 없어도 유형은
확정할 수 있다. 상태의 형식 검사는 분류 결과가 수학적으로 맞다는 보증이 아니다.
"""

from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator, BaseModel, ConfigDict, Field, StrictBool, StrictStr,
    StringConstraints, TypeAdapter, model_validator,
)


SUPPORTED_GRADES = (1, 2, 3)
Grade = Annotated[int, Field(strict=True, ge=1, le=3)]
Semester = Annotated[int, Field(strict=True, ge=1, le=2)]
TopicCode = Annotated[StrictStr, Field(pattern=r"^[0-9]{7}$")]
ClarificationField = Literal[
    "grade", "confirmation", "question_text", "equations", "conditions",
    "figures", "tables", "targets",
]


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("공백만 있는 문자열은 사용할 수 없습니다.")
    # 수식·기호·숫자는 정규화하거나 고치지 않고 그대로 보존한다.
    return value


Text = Annotated[StrictStr, Field(min_length=1, max_length=16000), AfterValidator(_not_blank)]
Identifier = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=255)]


class ContractModel(BaseModel):
    # 잘못된 필드 이름이나 학생 풀이·정답의 별도 입력을 묵시적으로 무시하지 않는다.
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False, revalidate_instances="always")


class FigureInput(ContractModel):
    id: Identifier
    description: StrictStr = Field(default="", max_length=16000)
    conditions: list[Text] = Field(default_factory=list, max_length=64)
    unreadable_parts: list[Text] = Field(default_factory=list, max_length=32)
    region_id: Identifier | None = None


class TableInput(ContractModel):
    id: Identifier
    # 병합 머리글을 펼친 직사각형 격자. 의도적인 빈칸은 ""로 보존한다.
    rows: list[Annotated[list[StrictStr], Field(min_length=1, max_length=100)]] = Field(min_length=1, max_length=200)
    unreadable_parts: list[Text] = Field(default_factory=list, max_length=32)
    region_id: Identifier | None = None

    @model_validator(mode="after")
    def rectangular(self) -> Self:
        if any(len(row) != len(self.rows[0]) for row in self.rows):
            raise ValueError("표의 모든 행은 같은 열 수를 가져야 합니다.")
        return self


class ClassificationRequest(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    grade: Grade | None = None
    semester: Semester | None = None
    question_text: StrictStr = Field(default="", max_length=16000)
    equations: list[Text] = Field(default_factory=list, max_length=64)
    conditions: list[Text] = Field(default_factory=list, max_length=64)
    targets: list[Text] = Field(default_factory=list, max_length=16)
    # None은 존재 여부를 아직 확인하지 않은 상태다. 데이터 없음과 구분한다.
    has_figure: StrictBool | None = None
    has_table: StrictBool | None = None
    figures: list[FigureInput] = Field(default_factory=list, max_length=16)
    tables: list[TableInput] = Field(default_factory=list, max_length=16)
    user_confirmed: StrictBool = False
    missing_information: list[ClarificationField] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def consistent_input(self) -> Self:
        if not (self.question_text.strip() or self.equations or self.conditions
                or self.figures or self.tables or self.targets):
            raise ValueError("본문·수식·조건 등 분류할 내용을 입력해 주세요.")
        if self.has_figure is False and self.figures:
            raise ValueError("도형 없음 표시와 도형 데이터가 충돌합니다.")
        if self.has_table is False and self.tables:
            raise ValueError("표 없음 표시와 표 데이터가 충돌합니다.")
        for items in (self.figures, self.tables):
            if len({item.id for item in items}) != len(items):
                raise ValueError("같은 종류의 도형·표 ID는 중복할 수 없습니다.")
        return self

    def blocking_fields(self) -> list[ClarificationField]:
        """명시적으로 빠진 정보를 찾는다. 본문에서 필요한 도형을 추론하지는 않는다."""
        missing = list(self.missing_information)
        if self.grade is None:
            missing.append("grade")
        if not self.user_confirmed:
            missing.append("confirmation")
        if self.has_figure is None or (self.has_figure and not self.figures):
            missing.append("figures")
        if any((not f.description.strip() and not f.conditions) or f.unreadable_parts for f in self.figures):
            missing.append("figures")
        if self.has_table is None or (self.has_table and not self.tables):
            missing.append("tables")
        if any(table.unreadable_parts for table in self.tables):
            missing.append("tables")
        if not self.question_text.strip() and not self.targets:
            missing.append("targets")
        return list(dict.fromkeys(missing))


class TypeCandidate(ContractModel):
    source_topic_code: TopicCode
    source_topic_name: Text
    reference_question_ids: list[Identifier] = Field(default_factory=list, max_length=5)
    # LinearSVC 점수는 음수이거나 1보다 클 수 있다. 확률 필드가 아니다.
    score: Annotated[float, Field(strict=True, allow_inf_nan=False)] | None = None
    score_name: Identifier | None = None
    reasons: list[Text] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def named_score(self) -> Self:
        if (self.score is None) != (self.score_name is None):
            raise ValueError("점수와 점수 이름은 함께 지정해야 합니다.")
        if len(set(self.reference_question_ids)) != len(self.reference_question_ids):
            raise ValueError("참고 문항 ID가 중복됐습니다.")
        return self


CandidateList = Annotated[list[TypeCandidate], Field(max_length=5)]


class GradeCandidate(ContractModel):
    grade: Grade
    score: Annotated[float, Field(strict=True, allow_inf_nan=False)]
    score_name: Literal["linear_svc_grade_margin"] = "linear_svc_grade_margin"


class DecisionBase(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    reasons: list[Text] = Field(min_length=1, max_length=16)
    grade: Grade | None = None
    grade_source: Literal["provided", "predicted"] | None = None
    grade_candidates: list[GradeCandidate] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def consistent_grade(self) -> Self:
        if (self.grade is None) != (self.grade_source is None):
            raise ValueError("학년과 학년의 출처는 함께 지정해야 합니다.")
        if len({c.grade for c in self.grade_candidates}) != len(self.grade_candidates):
            raise ValueError("학년 후보가 중복됐습니다.")
        if self.grade_source == "predicted" and (
            not self.grade_candidates or self.grade_candidates[0].grade != self.grade
        ):
            raise ValueError("추정 학년은 학년 후보 1순위와 같아야 합니다.")
        return self


class ClassifiedResult(DecisionBase):
    status: Literal["classified"] = "classified"
    source_topic_code: TopicCode
    source_topic_name: Text
    # 유형만 확정되고 같은 문항을 못 찾았으면 None이다.
    matched_question_id: Identifier | None = None
    method: Literal["exact_match", "retrieval", "model", "hybrid", "manual"]
    candidates: CandidateList = Field(default_factory=list)

    @model_validator(mode="after")
    def selected_type(self) -> Self:
        if self.method == "exact_match" and self.matched_question_id is None:
            raise ValueError("동일 문항 매칭에는 실제 매칭 문항 ID가 필요합니다.")
        if self.candidates and self.candidates[0].source_topic_code != self.source_topic_code:
            raise ValueError("최종 선택 유형이 후보 1순위와 다릅니다. 최종 순서를 반영해 주세요.")
        return self


class CandidatesResult(DecisionBase):
    status: Literal["candidates"] = "candidates"
    candidates: Annotated[list[TypeCandidate], Field(min_length=1, max_length=5)]


class ClarificationQuestion(ContractModel):
    field: ClarificationField
    question: Text


class ClarificationResult(DecisionBase):
    status: Literal["needs_clarification"] = "needs_clarification"
    questions: list[ClarificationQuestion] = Field(min_length=1, max_length=16)
    candidates: CandidateList = Field(default_factory=list)


class UnsupportedResult(DecisionBase):
    status: Literal["unsupported"] = "unsupported"


ClassificationDecision = Annotated[
    ClassifiedResult | CandidatesResult | ClarificationResult | UnsupportedResult,
    Field(discriminator="status"),
]
DECISION_ADAPTER = TypeAdapter(ClassificationDecision)


def validate_decision(
    request: ClassificationRequest | dict,
    decision: ClassificationDecision | dict,
) -> ClassificationDecision:
    """입력과 결과의 일관성을 검사한다. 유형·문항의 DB 실재 여부는 저장 단계에서 검증한다."""
    request = ClassificationRequest.model_validate(request)
    result = DECISION_ADAPTER.validate_python(decision)
    effective = request
    if result.grade_source == "provided" and result.grade != request.grade:
        raise ValueError("결과의 학년과 입력한 학년이 다릅니다.")
    if result.grade_source == "predicted":
        if request.grade is not None:
            raise ValueError("입력한 학년을 추정 학년으로 덮어쓸 수 없습니다.")
        effective = ClassificationRequest.model_validate({
            **request.model_dump(), "grade": result.grade,
            "missing_information": [f for f in request.missing_information if f != "grade"],
        })
    if request.grade is not None and request.grade not in SUPPORTED_GRADES:
        if not isinstance(result, UnsupportedResult):
            raise ValueError("현재 지원 범위는 중1~중3입니다. 지원 범위 밖 결과로 반환해야 합니다.")
    if isinstance(result, (ClassifiedResult, CandidatesResult)):
        missing = effective.blocking_fields()
        if missing:
            raise ValueError("입력 확인·정보 보완이 필요합니다: " + ", ".join(missing))
    candidates = getattr(result, "candidates", [])
    if len({c.source_topic_code for c in candidates}) != len(candidates):
        raise ValueError("후보는 문항별이 아닌 유형별로 합쳐야 합니다.")
    return result


def classification_json_schema() -> dict:
    """프런트엔드·API가 같은 계약을 사용하도록 JSON Schema를 제공한다."""
    return {"input": ClassificationRequest.model_json_schema(), "output": DECISION_ADAPTER.json_schema()}
