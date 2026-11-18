"""문제 내부 영역의 종류·좌표·읽기 순서·소속 관계."""

from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)


Identifier = Annotated[
    str,
    StringConstraints(
        strict=True,
        strip_whitespace=True,
        min_length=1,
        max_length=80,
    ),
]

Pixel = Annotated[int, Field(strict=True, ge=0)]
PositivePixel = Annotated[int, Field(strict=True, gt=0)]


class LayoutRegion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: Identifier

    # group은 여러 영역을 포함하는 묶음입니다.
    kind: Literal[
        "group",
        "text",
        "formula",
        "table",
        "figure",
        "graph",
        "unknown",
    ]

    # 수식도 문제 본문이나 학생 풀이에 속할 수 있습니다.
    role: Literal[
        "statement",
        "choice",
        "context",
        "student_work",
        "annotation",
        "unknown",
    ] = "unknown"

    # [왼쪽, 위, 오른쪽, 아래]
    # 오른쪽·아래 경계는 포함하지 않습니다.
    bbox: tuple[Pixel, Pixel, Pixel, Pixel]

    parent_id: Identifier | None = None

    # 같은 부모를 가진 영역끼리 순서를 비교합니다.
    reading_order: Annotated[int, Field(strict=True, ge=0)]


class ProblemLayout(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["layout-v1"] = "layout-v1"

    # 선택 영역 자르기와 방향 보정이 끝난 이미지 기준입니다.
    # 영역별 확대·여백 추가 등의 전처리 전 좌표입니다.
    coordinate_space: Literal["ocr_image"] = "ocr_image"

    image_size: tuple[PositivePixel, PositivePixel]

    regions: list[LayoutRegion] = Field(
        min_length=1,
        max_length=256,
    )

    @model_validator(mode="after")
    def validate_regions(self) -> Self:
        width, height = self.image_size
        by_id = {region.id: region for region in self.regions}

        if len(by_id) != len(self.regions):
            raise ValueError("영역 ID가 중복됐습니다.")

        orders = set()

        for region in self.regions:
            left, top, right, bottom = region.bbox

            if not (
                0 <= left < right <= width
                and 0 <= top < bottom <= height
            ):
                raise ValueError(
                    f"{region.id}: 영역이 이미지 안에 있어야 합니다."
                )

            key = (region.parent_id, region.reading_order)

            if key in orders:
                raise ValueError(
                    "같은 부모 안에서 읽기 순서가 중복됐습니다."
                )

            orders.add(key)

            if region.parent_id is not None:
                parent = by_id.get(region.parent_id)

                if parent is None:
                    raise ValueError(
                        f"{region.id}: 상위 영역이 없습니다."
                    )

                pl, pt, pr, pb = parent.bbox

                if not (
                    pl <= left < right <= pr
                    and pt <= top < bottom <= pb
                ):
                    raise ValueError(
                        f"{region.id}: 상위 영역 안에 있어야 합니다."
                    )

            # 자기 자신이나 하위 영역을 부모로 지정하지 못하게 합니다.
            seen = {region.id}
            parent_id = region.parent_id

            while parent_id is not None:
                if parent_id in seen:
                    raise ValueError(
                        "영역의 소속 관계가 순환하고 있습니다."
                    )

                seen.add(parent_id)
                parent = by_id.get(parent_id)

                if parent is None:
                    raise ValueError("상위 영역이 없습니다.")

                parent_id = parent.parent_id

        return self