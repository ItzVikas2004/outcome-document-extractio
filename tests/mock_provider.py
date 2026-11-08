"""Synthetic in-memory gateway only. No real network or assignment answers."""
import json
import httpx


def missing():
    return {"value": None, "confidence": 0, "evidence": []}


def answer(value, span):
    return {"value": value, "confidence": 0.9,
            "evidence": [{"source_id": span["id"], "quote": span["text"]}]}


TABLE_SCHEMA = {"name": "synthetic rent table", "fields": {
    "property_custom": {"name": "Property", "description": "Property name, same for every row"},
    "unit_custom": {"name": "Unit", "description": "Unit identifier, one per row", "primary": True},
    "amount_custom": {"name": "Rent", "description": "Rent as a number for each unit"},
    "date_custom": {"name": "Date", "description": "Date per unit in YYYY-MM-DD"},
}}
TABLE_PLAN = {"mode": "table", "fields": {
    "property_custom": {"scope": "document", "type": "string"},
    "unit_custom": {"scope": "row", "type": "string"},
    "amount_custom": {"scope": "row", "type": "number"},
    "date_custom": {"scope": "row", "type": "date"},
}}


def table_response(payload):
    documents = {"property_custom": missing()}
    for unit in payload["context"] + payload["targets"]:
        for span in unit["spans"]:
            if span["text"].startswith("Property: "):
                documents["property_custom"] = answer(span["text"].removeprefix("Property: "), span)
    rows, skipped = [], []
    for unit in payload["targets"]:
        cells = {span["location"].get("column"): span for span in unit["spans"]}
        first = cells.get(1, {}).get("text", "")
        if not first or first == "Unit" or first.startswith("Property: "):
            skipped.append({"source_id": unit["id"], "reason": "blank" if not first else "header"})
            continue
        rows.append({"source_ids": [unit["id"]], "continuation_of": None, "fields": {
            "unit_custom": answer(first, cells[1]),
            "amount_custom": answer(float(cells[2]["text"]), cells[2]) if 2 in cells else missing(),
            "date_custom": answer(cells[3]["text"], cells[3]) if 3 in cells else missing(),
        }})
    return {"documents": documents, "rows": rows, "skipped": skipped}


class Provider:
    def __init__(self, plan=TABLE_PLAN, extractor=table_response):
        self.plan, self.extractor, self.requests = plan, extractor, []

    def __call__(self, request):
        body = json.loads(request.content)
        payload = json.loads(body["messages"][1]["content"])
        self.requests.append((request, body, payload))
        result = self.plan if payload["task"] == "plan" else self.extractor(payload)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 40, "cost": 0.0007}})


def worker_mock_extract(lease, usage_sink=None):
    from app.llm.config import LLMConfig
    from app.llm.pipeline import extract_document
    return extract_document(lease, LLMConfig("https://mock.invalid/v1", "test-key"),
                            transport=httpx.MockTransport(Provider()), usage_sink=usage_sink)
