def validate_layout(rows, proposal):
    header_row = proposal.get("header_row")
    data_start_row = proposal.get("data_start_row")
    columns = proposal.get("columns")
    if not isinstance(header_row, int) or not isinstance(data_start_row, int):
        raise ValueError("header row and data start row must be integers")
    if header_row < 1 or header_row > len(rows):
        raise ValueError("header row outside source bounds")
    if data_start_row != header_row + 1:
        raise ValueError("data must begin immediately after the header; skipped rows are not allowed")
    candidates = candidate_layout(rows, allow_preamble=True)
    if header_row not in {item["header_row"] for item in candidates}:
        raise ValueError("header row is not a locally supported structural candidate")
    header = rows[header_row - 1]
    if any(len(row) > len(header) for row in rows[data_start_row - 1:]):
        raise ValueError("data row width exceeds header width; refusing to drop source columns")
    if not isinstance(columns, list) or not columns:
        raise ValueError("header columns must be a non-empty list")
    positions = [column.get("position") for column in columns]
    if positions != list(range(1, len(header) + 1)):
        raise ValueError("every source column must be mapped once in original order")
    headers = []
    for pos, column in enumerate(columns, 1):
        label = column.get("label")
        if label != header[pos - 1]:
            raise ValueError(f"header label mismatch at column {pos}")
        headers.append(label if label not in (None, "") else f"column_{pos}")
    preamble_rows = list(range(1, header_row))
    return {"header_row": header_row, "data_start_row": data_start_row, "headers": headers,
            "positions": positions, "preamble": rows[:header_row - 1],
            "records": [[row[p - 1] if p <= len(row) else None for p in positions] for row in rows[data_start_row - 1:]]}


def candidate_layout(rows, allow_preamble=False):
    candidates = []
    for index, row in enumerate(rows[:-1]):
        if index and not allow_preamble:
            break
        header = row + [None] * max(0, len(rows[index + 1]) - len(row))
        following = rows[index + 1] + [None] * max(0, len(header) - len(rows[index + 1]))
        populated = [str(value).strip() for value in header if value not in (None, "")]
        if len(populated) < 2 or len({value.casefold() for value in populated}) != len(populated):
            continue
        changed_types = sum(
            _cell_kind(value) != _cell_kind(label)
            for label, value in zip(header, following)
            if label not in (None, "") and value not in (None, "")
        )
        if changed_types:
            candidates.append({"header_row": index + 1, "labels": row})
    return candidates


def _cell_kind(value):
    if value in (None, ""):
        return "blank"
    try:
        float(str(value).replace(",", ""))
        return "number"
    except ValueError:
        return "text"
