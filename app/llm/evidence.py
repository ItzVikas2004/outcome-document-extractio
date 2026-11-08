"""Disk-backed evidence index. Context selection never discards target rows."""
import json
import re
import sqlite3
from decimal import Decimal, InvalidOperation

from app.extraction import ExtractionFailure, FailureCode
from app.parsing import parse_document, ParseError


def display_cell(cell):
    if cell.value is None:
        return ""
    value = str(cell.value)
    fmt = cell.number_format or ""
    if cell.kind == "number" and "%" in fmt and "\\%" not in fmt:
        try:
            places = len(re.search(r"\.([0#]+)%", fmt).group(1)) if re.search(r"\.([0#]+)%", fmt) else 0
            value = format((Decimal(value) * 100).quantize(Decimal(1).scaleb(-places)), "f") + "%"
        except (InvalidOperation, ValueError):
            pass
    return value


class EvidenceStore:
    def __init__(self, database, source, schema):
        self.connection = sqlite3.connect(database)
        self.connection.execute("CREATE TABLE units (id INTEGER PRIMARY KEY, body TEXT, score INTEGER, group_id TEXT)")
        self.count = 0
        words = set(re.findall(r"\w+", json.dumps(schema, ensure_ascii=False).lower()))
        try:
            for record in parse_document(source):
                if record.kind == "ocr_required":
                    raise ExtractionFailure(FailureCode.DOCUMENT_REQUIRES_OCR, retryable=False)
                if record.kind == "table_row":
                    spans = [{"text": display_cell(cell), "location": cell.location}
                             for cell in record.cells if cell.value is not None and display_cell(cell) != ""]
                    self.add("row", record.location, spans, words)
                elif record.kind == "paragraph" and "table" not in record.location and record.text:
                    # Slices remain verbatim substrings with the original paragraph location.
                    for start in range(0, len(record.text), 1200):
                        self.add("paragraph", record.location,
                                 [{"text": record.text[start:start + 1200], "location": record.location}], words)
            self.connection.commit()
        except ParseError:
            self.connection.close()
            raise ExtractionFailure(FailureCode.DOCUMENT_PARSE_FAILED, retryable=False) from None
        except BaseException:
            self.connection.close()
            raise
        if not self.count:
            self.connection.close()
            raise ExtractionFailure(FailureCode.DOCUMENT_PARSE_FAILED, retryable=False)

    def add(self, kind, location, spans, words):
        self.count += 1
        identifier = f"s{self.count}"
        for index, span in enumerate(spans, 1):
            span["id"] = f"{identifier}:e{index}"
        unit = {"id": identifier, "kind": kind, "location": location, "spans": spans}
        text = " ".join(span["text"] for span in spans)
        score = len(words & set(re.findall(r"\w+", text.lower())))
        group = json.dumps({key: location[key] for key in ("format", "page", "sheet", "section", "table") if key in location}, sort_keys=True)
        self.connection.execute("INSERT INTO units VALUES (?, ?, ?, ?)",
                                (self.count, json.dumps(unit, ensure_ascii=False), score, group))

    def close(self):
        self.connection.close()

    def context(self, budget=2400, group=None):
        output, size = [], 0
        # First rows preserve headings; high lexical scores add relevant global facts.
        queries = []
        if group:
            queries.append(("SELECT body FROM units WHERE group_id=? ORDER BY id LIMIT 2", (group,)))
        queries += [("SELECT body FROM units ORDER BY id LIMIT 2", ()),
                    ("SELECT body FROM units ORDER BY score DESC, id LIMIT 64", ())]
        seen = set()
        for sql, params in queries:
            for (body,) in self.connection.execute(sql, params):
                unit = json.loads(body)
                if unit["id"] in seen:
                    continue
                seen.add(unit["id"])
                length = len(body.encode("utf-8"))
                if size + length <= budget:
                    size += length
                    output.append(unit)
        return output

    def batches(self, budget, max_units):
        batch, size, current_group = [], 0, None
        for body, group in self.connection.execute("SELECT body, group_id FROM units ORDER BY id"):
            length = len(body.encode("utf-8"))
            if length > budget:
                raise ExtractionFailure(FailureCode.EXTRACTION_BUDGET_EXCEEDED, retryable=False)
            if batch and (size + length > budget or len(batch) >= max_units or group != current_group):
                yield batch, current_group
                batch, size = [], 0
            current_group = group
            batch.append(json.loads(body))
            size += length
        if batch:
            yield batch, current_group
