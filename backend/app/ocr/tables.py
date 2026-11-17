"""Paddle 표 전용 출력의 셀·병합·빈칸을 복원한다. 값을 계산하지 않는다."""

import re


def parse_table(text: str) -> dict:
    """SOTSL(fcel/ecel/lcel/ucel/xcel/nl)을 rows와 병합 정보를 가진 cells로 반환."""
    text = re.sub(r"^```[^\n]*\n|\n```\s*$", "", text.strip())
    rows, cells, owners = [], [], []
    for raw in text.split("<nl>"):
        if not raw.strip():
            continue
        matches = list(re.finditer(r"<(fcel|ecel|lcel|ucel|xcel)>", raw))
        if not matches or raw[:matches[0].start()].strip():
            raise ValueError("표 전용 출력의 셀 태그를 읽을 수 없습니다.")
        row, owner_row = [], []
        for j, match in enumerate(matches):
            kind = match.group(1)
            value = raw[match.end():matches[j + 1].start() if j + 1 < len(matches) else len(raw)].strip()
            r, c = len(rows), len(row)
            if kind in {"fcel", "ecel"}:
                if kind == "ecel" and value:
                    raise ValueError("빈 셀에 텍스트가 들어 있습니다.")
                owner = len(cells)
                cells.append({"row": r, "col": c, "text": value, "rowspan": 1, "colspan": 1})
            else:
                if value:
                    raise ValueError("병합 표시 안에 별도 텍스트가 있습니다.")
                if kind == "lcel" and c:
                    owner = owner_row[c - 1]
                elif kind == "ucel" and r and c < len(owners[r - 1]):
                    owner = owners[r - 1][c]
                elif (kind == "xcel" and r and c and c < len(owners[r - 1])
                      and owner_row[c - 1] == owners[r - 1][c]):
                    owner = owner_row[c - 1]
                else:
                    raise ValueError("표의 병합 위치가 올바르지 않습니다.")
                cell = cells[owner]
                cell["rowspan"] = max(cell["rowspan"], r - cell["row"] + 1)
                cell["colspan"] = max(cell["colspan"], c - cell["col"] + 1)
            owner_row.append(owner)
            row.append(cells[owner]["text"])
        rows.append(row)
        owners.append(owner_row)
    if not rows or any(len(row) != len(rows[0]) for row in rows):
        raise ValueError("표가 비어 있거나 행마다 열 수가 다릅니다.")
    # 잘못된 L자 병합을 정상적인 사각형 병합으로 바꾸지 않는다.
    for index, cell in enumerate(cells):
        for r in range(cell["row"], cell["row"] + cell["rowspan"]):
            for c in range(cell["col"], cell["col"] + cell["colspan"]):
                if r >= len(owners) or c >= len(owners[r]) or owners[r][c] != index:
                    raise ValueError("표의 병합 셀이 직사각형이 아닙니다.")
    return {"rows": rows, "cells": cells}
