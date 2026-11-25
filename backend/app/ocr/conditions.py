"""사진에서 읽은 조건의 공통 데이터 형식."""

from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)


ConditionId = Annotated[
    str,
    StringConstraints(
        strict=True,
        strip_whitespace=True,
        min_length=1,
        max_length=80,
    ),
]

ConditionLabel = Annotated[
    str,
    StringConstraints(
        strict=True,
        strip_whitespace=True,
        min_length=1,
        max_length=100,
    ),
]

ConditionValue = Annotated[
    str,
    StringConstraints(
        strict=True,
        strip_whitespace=True,
        min_length=1,
        max_length=1000,
    ),
]


class ProblemCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: ConditionId

    kind: Literal[
        "point",
        "radius",
        "length",
        "angle",
        "formula",
        "interval",
        "relation",
        "note",
    ]

    label: ConditionLabel
    value: ConditionValue

    # latex인 경우 \( \) 같은 바깥 구분 기호는 제외합니다.
    value_format: Literal["text", "latex"] = "text"

    # 모델이 사진에서 읽었다고 보고한 원래 표기입니다.
    # 사진과 일치한다고 검증된 근거를 의미하지는 않습니다.
    source_text: Annotated[
        str,
        Field(strict=True, max_length=1000),
    ] = ""


class ConditionExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["conditions-v1"] = "conditions-v1"

    status: Literal[
        "not_run",
        "completed",
        "failed",
    ] = "not_run"

    conditions: list[ProblemCondition] = Field(
        default_factory=list,
        max_length=32,
    )

    @model_validator(mode="after")
    def validate_conditions(self) -> Self:
        ids = [condition.id for condition in self.conditions]

        if len(ids) != len(set(ids)):
            raise ValueError("조건 ID가 중복됐습니다.")

        if self.status != "completed" and self.conditions:
            raise ValueError(
                "추출이 완료된 경우에만 조건을 포함할 수 있습니다."
            )

        return self