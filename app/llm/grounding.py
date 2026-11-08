"""Build citations only from verified parser evidence, never model-supplied locations."""
import math
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def empty_field():
    return {"value": None, "citation": "", "confidence": 0.0}


def label(location):
    fmt = location["format"]
    if fmt == "pdf":
        return f"Page {location['page']}"
    if fmt == "xlsx":
        return f"{location['sheet']}!{location['address']}"
    if fmt == "csv":
        # Excel-style column letters are a deterministic location, not inferred data.
        number, letters = location["column"], ""
        while number:
            number, remainder = divmod(number - 1, 26)
            letters = chr(65 + remainder) + letters
        return f"CSV!{letters}{location['row']}"
    result = f"Section {location.get('section', 1)}"
    if "region" in location:
        result += f" {location['region']}"
    if "table" in location:
        result += f" Table {location['table']} Row {location['row']} Column {location['column']}"
    elif "paragraph" in location:
        result += f" Paragraph {location['paragraph']}"
    return result


def numeric_tokens(text):
    output = []
    for token in re.findall(r"(?<![\w.])\(?[-+]?\d[\d,]*(?:\.\d+)?\)?", text):
        try:
            number = Decimal(token.replace(",", "").strip("()"))
            if token.startswith("(") and token.endswith(")"):
                number = -number
            output.append(number)
        except InvalidOperation:
            pass
    return output


def supported(value, text, kind):
    if value is None:
        return False
    if kind in {"number", "percentage"}:
        return type(value) in (int, float) and math.isfinite(value) and Decimal(str(value)) in numeric_tokens(text)
    if kind == "boolean":
        return type(value) is bool and bool(re.search(r"\b(?:" + ("true|yes" if value else "false|no") + r")\b", text, re.I))
    if kind == "date":
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return False
        try:
            target = date.fromisoformat(value)
        except ValueError:
            return False
        if value in text:
            return True
        for pattern, formats in [
            (r"\b\d{1,2}[/-]\d{1,2}[/-]\d{4}\b", ("%m/%d/%Y", "%m-%d-%Y", "%d/%m/%Y", "%d-%m-%Y")),
            (r"\b[A-Za-z]+ \d{1,2},? \d{4}\b", ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y")),
        ]:
            for token in re.findall(pattern, text):
                for fmt in formats:
                    try:
                        if datetime.strptime(token, fmt).date() == target:
                            return True
                    except ValueError:
                        pass
        return False
    if kind == "list":
        return isinstance(value, list) and all(supported(v, text, "number" if type(v) in (int, float) else "boolean" if type(v) is bool else "string") for v in value)
    return isinstance(value, str) and bool(value.strip()) and " ".join(value.split()).casefold() in " ".join(text.split()).casefold()


def verified_evidence(items, evidence):
    output = []
    for item in items:
        span = evidence.get(item["source_id"])
        quote = item["quote"]
        if span is None or not isinstance(quote, str) or not quote.strip() or quote not in span["text"]:
            return None
        # A quote of "12" cut out of "1234" cannot ground the number 12.
        matches = [match for match in re.finditer(re.escape(quote), span["text"])
                   if not (quote[0].isdigit() and match.start() and span["text"][match.start()-1] in "0123456789.,")
                   and not (quote[-1].isdigit() and match.end() < len(span["text"]) and span["text"][match.end()] in "0123456789.,")]
        if not matches:
            return None
        output.append((quote, span["location"]))
    return output or None


def calculate(spec, evidence):
    numbers, citations = [], []
    for operand in spec["operands"]:
        verified = verified_evidence(operand["evidence"], evidence)
        if not verified or not supported(operand["value"], " ".join(q for q, _ in verified), "number"):
            return None
        numbers.append(Decimal(str(operand["value"])))
        citations.extend(verified)
    if not numbers:
        return None
    op = spec["operation"]
    try:
        if op == "sum":
            value = sum(numbers)
        elif op == "average":
            value = sum(numbers) / len(numbers)
        elif op == "product":
            value = math.prod(numbers)
        elif op == "difference" and len(numbers) == 2:
            value = numbers[0] - numbers[1]
        elif op in {"ratio", "percent"} and len(numbers) == 2:
            value = numbers[0] / numbers[1] * (100 if op == "percent" else 1)
        else:
            return None
        places = spec.get("decimal_places")
        if places is not None:
            if type(places) is not int or not 0 <= places <= 8:
                return None
            value = value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
        if not value.is_finite():
            return None
        return value, citations
    except (ArithmeticError, ValueError):
        return None


def ground(field, kind, evidence):
    if field["value"] is None:
        return empty_field(), False
    verified = verified_evidence(field["evidence"], evidence)
    if field.get("calculation"):
        result = calculate(field["calculation"], evidence)
        if (kind not in {"number", "percentage"} or result is None
                or type(field["value"]) not in (int, float) or Decimal(str(field["value"])) != result[0]):
            return empty_field(), True
        verified = result[1]
    elif not verified or not supported(field["value"], " ".join(quote for quote, _ in verified), kind):
        return empty_field(), True
    citation = "; ".join(dict.fromkeys(f"[{label(loc)}] {quote}" for quote, loc in verified))
    value = {"value": field["value"], "confidence": field["confidence"], "citation": citation}
    pages = sorted({loc["page"] for _, loc in verified if loc["format"] == "pdf"})
    cells = list(dict.fromkeys(label(loc) for _, loc in verified if loc["format"] in {"xlsx", "csv"}))
    if pages:
        value["page_numbers"] = pages
    if cells:
        value["source_locations"] = cells
    return value, False
