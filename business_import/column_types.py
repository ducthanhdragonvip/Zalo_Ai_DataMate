"""Validate model type hints and convert whole columns without lossy casts."""
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext

TYPES = {"VARCHAR", "DATE", "TIMESTAMP", "BIGINT", "DECIMAL", "BOOLEAN"}


def validate_specs(specs, count):
    if not isinstance(specs, list) or len(specs) != count:
        raise ValueError("column_types must include one specification per source column in order")
    for spec in specs:
        if not isinstance(spec, dict) or not isinstance(spec.get("type"), str) or spec["type"] not in TYPES:
            raise ValueError("unsupported column type")
        if any(key not in {"type", "format", "formats", "decimal_separator", "thousands_separator", "description"} for key in spec):
            raise ValueError("unknown column type option")
        if "formats" in spec:
            formats = spec["formats"]
            if (spec["type"] not in {"DATE", "TIMESTAMP"} or "format" in spec
                    or not isinstance(formats, list) or not 1 <= len(formats) <= 10
                    or any(not isinstance(fmt, str) or not 1 <= len(fmt) <= 100 for fmt in formats)):
                raise ValueError("invalid date formats")
        if "description" in spec and not isinstance(spec["description"], str):
            raise ValueError("invalid column type description")
        if "format" in spec and (not isinstance(spec["format"], str) or len(spec["format"]) > 100):
            raise ValueError("invalid date format")
        if "format" in spec and spec["type"] not in {"DATE", "TIMESTAMP"}:
            if spec["type"] != "DECIMAL" or spec["format"] != "VND":
                raise ValueError("unsupported numeric format")
            if "decimal_separator" in spec or "thousands_separator" in spec:
                raise ValueError("VND format cannot include numeric separators")
        decimal, thousands = spec.get("decimal_separator", "."), spec.get("thousands_separator", "")
        if decimal not in (".", ",") or thousands not in ("", ".", ",", " ") or decimal == thousands:
            raise ValueError("invalid numeric separators")


def _convert(value, spec):
    if value is None or spec["type"] == "VARCHAR":
        return value
    if value == "" and spec["type"] in {"BIGINT", "DECIMAL"}:
        return None
    if not isinstance(value, (str, int, float, bool)):
        raise ValueError("unsupported source value")
    text = str(value).strip()
    kind = spec["type"]
    # ponytail: alphanumeric unit tokens only; keep the leading quantity, never scale or evaluate expressions.
    if kind == "DECIMAL" and spec.get("format") != "VND":
        unit = re.fullmatch(r"(.+?) +([^\W\d_][^\W_]*(?: +[^\W_]+)*)", text)
        if unit:
            text = unit[1]
    if kind in ("DATE", "TIMESTAMP"):
        fmt = spec.get("format")
        # Exact aliases only: do not infer day/month order from ambiguous values.
        fmt = {"dd/mm/yyyy": "%d/%m/%Y", "mm/dd/yyyy": "%m/%d/%Y",
               "yyyy-mm-dd": "%Y-%m-%d", "dd-mm-yyyy": "%d-%m-%Y",
               "yyyy/mm/dd": "%Y/%m/%d"}.get(fmt, fmt)
        if not fmt:
            raise ValueError("explicit date format required")
        parsed = datetime.strptime(text, fmt)
        if not all(token in fmt for token in ("%Y", "%m", "%d")):
            raise ValueError("complete date format required")
        if kind == "DATE" and any((parsed.hour, parsed.minute, parsed.second, parsed.microsecond, parsed.tzinfo)):
            raise ValueError("DATE would discard time information")
        if parsed.tzinfo is not None:
            raise ValueError("timezone-aware timestamps require an explicit timezone policy")
        return parsed.date() if kind == "DATE" else parsed
    if kind == "BOOLEAN":
        if text.casefold() not in ("true", "false"):
            raise ValueError("ambiguous boolean")
        return text.casefold() == "true"
    decimal, thousands = spec.get("decimal_separator", "."), spec.get("thousands_separator", "")
    integer = r"(?:0|[1-9][0-9]*)"
    if thousands:
        integer = rf"(?:{integer}|[1-9][0-9]{{0,2}}(?:{re.escape(thousands)}[0-9]{{3}})+)"
    fraction = rf"(?:{re.escape(decimal)}[0-9]+)?" if kind == "DECIMAL" else ""
    # ponytail: only single ASCII spaces between three-digit groups; other spacing stays text.
    if re.fullmatch(rf"[+-]?[1-9][0-9]{{0,2}}(?: [0-9]{{3}})+{fraction}", text):
        text = text.replace(" ", "")
    if not re.fullmatch(rf"[+-]?{integer}{fraction}", text):
        raise ValueError("ambiguous numeric value or padded identifier")
    normalized = text.replace(thousands, "") if thousands else text
    normalized = normalized.replace(decimal, ".")
    if kind == "BIGINT":
        number = int(normalized)
        if not -(2**63) <= number < 2**63:
            raise ValueError("BIGINT overflow")
        return number
    number = Decimal(normalized)
    # ponytail: fixed DECIMAL(38,10); keep higher precision as text until variable scales are needed.
    if not number.is_finite() or number.as_tuple().exponent < -10 or abs(number) >= Decimal("1e28"):
        raise ValueError("DECIMAL precision would be lost")
    return number


def _date_from_formats(value, formats, excel_epoch=None, sheet_period=None):
    # ponytail: empty cells become NULL only for date-format conversion; other types retain strict blank checks.
    if value is None or value == "":
        return None
    if sheet_period and (short := re.fullmatch(r"([0-9]{1,2})/([0-9]{1,2})", str(value).strip())):
        month, year = sheet_period
        if int(short[2]) != month:
            raise ValueError("short date conflicts with worksheet month")
        return datetime(year, month, int(short[1]))
    matches = set()
    for fmt in formats:
        try:
            if fmt == "excel_serial":
                from openpyxl.utils.datetime import from_excel
                if excel_epoch not in {"1899-12-30", "1904-01-01"}:
                    raise ValueError("verified workbook epoch required")
                text = str(value).strip()
                if not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", text):
                    raise ValueError("invalid Excel serial")
                serial = Decimal(text)
                if not 1 <= serial < 2957004 or (excel_epoch == "1899-12-30" and 60 <= serial < 61):
                    raise ValueError("invalid or fictitious Excel date")
                parsed = from_excel(float(serial), epoch=datetime.fromisoformat(excel_epoch))
                if not isinstance(parsed, datetime):
                    raise ValueError("complete date required")
                matches.add(parsed)
            else:
                matches.add(_convert(value, {"type": "TIMESTAMP", "format": fmt}))
        except (ValueError, OverflowError, InvalidOperation, re.error):
            pass
    if len(matches) != 1:
        raise ValueError("unsupported or ambiguous date")
    return matches.pop()


def _vnd_amount(value):
    if value is None or value == "":
        return None
    text = str(value).strip().casefold()
    if not text:
        raise ValueError("whitespace-only amount")
    # ponytail: embedded millions require three thousand digits; expand only with documented examples.
    if match := re.fullmatch(r"([+-]?(?:0|[1-9][0-9]*))tr([0-9]{3})", text):
        text = f"{match[1]}.{match[2]}tr"
    if text.endswith(("k", "tr")):
        # tr means millions; its comma is decimal (1,287tr = 1.287 million).
        millions = text.endswith("tr")
        amount = text[:-2] if millions else text[:-1]
        separator = "," if millions and "," in amount else "."
        with localcontext() as context:
            context.prec = 42
            number = _convert(amount, {"type": "DECIMAL", "decimal_separator": separator}) * Decimal(1000000 if millions else 1000)
        return _convert(format(number, "f"), {"type": "DECIMAL"})
    if text.endswith(("đ", "₫")):
        text = text[:-1].strip()
    # VND grouping may mix dots and commas; every group must have three digits.
    if re.fullmatch(r"[+-]?[1-9][0-9]{0,2}(?:[.,][0-9]{3})+", text):
        return _convert(text.replace(".", "").replace(",", ""), {"type": "DECIMAL"})
    return _convert(text, {"type": "DECIMAL", "thousands_separator": ","})


def convert_columns(columns, rows, specs, default_types, excel_epoch=None, sheet_name=None):
    """Return SQL types, converted rows and non-sensitive fallback warnings."""
    if specs is None:
        specs = [{"type": "VARCHAR"} for _ in columns]
    validate_specs(specs, len(columns))
    types, converted, warnings = list(default_types), [list(row) for row in rows], []
    # ponytail: yearless day/month dates require an explicit T<month>_<year> worksheet period.
    period = re.fullmatch(r"T(0?[1-9]|1[0-2])[-_]([0-9]{4})", sheet_name or "", re.IGNORECASE)
    for index, (column, spec) in enumerate(zip(columns, specs)):
        sheet_period = None
        if spec["type"] == "TIMESTAMP" and "format" not in spec and "formats" not in spec:
            # ponytail: Vietnamese dates default to day-first; add other orders only with explicit format hints.
            formats = ["%d/%m/%Y", "%d-%m-%Y", "%d thg %m %Y", "%Y-%m-%d", "%Y/%m/%d"]
            formats += ["%Y-%m-%d" + sep + "%H:%M:%S" + fraction
                        for sep in ("T", " ") for fraction in ("", ".%f")]
            formats += ["%d/%m/%Y %H:%M:%S"]
            if excel_epoch is not None:
                formats.append("excel_serial")
            spec = {"type": "TIMESTAMP", "formats": formats}
            if period:
                sheet_period = (int(period[1]), int(period[2]))
        if spec["type"] == "DATE":
            spec = {**spec, "type": "TIMESTAMP"}
        if spec["type"] == "TIMESTAMP" and ("formats" in spec or spec.get("format") == "excel_serial"):
            try:
                dates = [_date_from_formats(row[index], spec.get("formats", [spec.get("format")]), excel_epoch, sheet_period) for row in rows]
                types[index] = "TIMESTAMP"
                for row, value in zip(converted, dates):
                    row[index] = value
                continue
            except (ValueError, TypeError, OverflowError, InvalidOperation, re.error):
                warnings.append(f"{column}: kept {default_types[index]}; not all values safely match date formats")
                continue
        if spec["type"] in {"DATE", "TIMESTAMP"} and any(
                isinstance(row[index], str) and re.fullmatch(
                    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.[0-9]{1,6})?", row[index].strip())
                for row in rows):
            try:
                dates = []
                for row in rows:
                    value = row[index]
                    try:
                        parsed = _convert(value, {**spec, "type": "TIMESTAMP"})
                    except ValueError:
                        text = str(value).strip()
                        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.[0-9]{1,6})?)?", text):
                            raise
                        fmt = "%Y-%m-%d" + ("T%H:%M:%S" if "T" in text else "") + (".%f" if "." in text else "")
                        parsed = _convert(value, {"type": "TIMESTAMP", "format": fmt})
                    dates.append(parsed)
                types[index] = "TIMESTAMP"
                for row, value in zip(converted, dates):
                    row[index] = value
            except (ValueError, TypeError, OverflowError, re.error):
                warnings.append(f"{column}: kept {default_types[index]}; not all values safely match timestamps")
            continue
        if spec["type"] == "DECIMAL" and (spec.get("format") == "VND" or any(
                isinstance(row[index], str) and (row[index].strip().casefold().endswith(("k", "tr", "đ", "₫"))
                                                or re.fullmatch(r"[+-]?(?:0|[1-9][0-9]*)tr[0-9]{3}", row[index].strip().casefold()))
                for row in rows)):
            try:
                amounts = [_vnd_amount(row[index]) for row in rows]
                types[index] = "DECIMAL(38,10)"
                for row, value in zip(converted, amounts):
                    row[index] = value
                continue
            except (ValueError, TypeError, OverflowError, InvalidOperation):
                warnings.append(f"{column}: kept {default_types[index]}; not all values safely match VND amounts")
                continue
        if spec["type"] == "VARCHAR":
            continue
        corrected = False
        if (spec["type"] == "DECIMAL" and "decimal_separator" not in spec
                and "thousands_separator" not in spec and any(
                    isinstance(row[index], str) and re.fullmatch(
                        r"[+-]?[1-9][0-9]{0,2}(?:,[0-9]{3})+(?:\.[0-9]+)?", row[index].strip()) for row in rows)):
            spec = {**spec, "thousands_separator": ","}
            corrected = True
        if spec["type"] in ("DECIMAL", "BIGINT"):
            # Repeated grouping is evidence, unlike an ambiguous single comma in 35,000.
            for separator, decimal in ((",", "."), (".", ",")):
                if any(isinstance(row[index], str) and re.fullmatch(
                        rf"[+-]?[1-9][0-9]{{0,2}}(?:{re.escape(separator)}[0-9]{{3}}){{2,}}", row[index].strip())
                       for row in rows):
                    corrected = spec.get("thousands_separator", "") != separator or spec.get("decimal_separator", ".") != decimal
                    spec = {**spec, "decimal_separator": decimal, "thousands_separator": separator}
                    break
        try:
            values = [_convert(row[index], spec) for row in rows]
        except (ValueError, TypeError, OverflowError, InvalidOperation, re.error):
            warnings.append(f"{column}: kept {default_types[index]}; not all values safely match {spec['type']}")
            continue
        if corrected:
            warnings.append(f"{column}: corrected numeric separator hint using source grouping")
        types[index] = "DECIMAL(38,10)" if spec["type"] == "DECIMAL" else spec["type"]
        for row, value in zip(converted, values):
            row[index] = value
    return types, converted, warnings
