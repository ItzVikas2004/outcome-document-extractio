"""Isolated parser process; communicate only through bounded files."""
import json
import sys
from pathlib import Path

from app.parsing.model import ParseError, ParseLimits, Record


def main(config):
    limits = ParseLimits(**config["limits"])
    if sys.platform == "linux":
        import ctypes
        import os
        import signal
        if ctypes.CDLL(None).prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != config["parent_pid"]:
            raise ParseError("PARSER_INTERRUPTED")
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (limits.max_memory_bytes, limits.max_memory_bytes))
        resource.setrlimit(resource.RLIMIT_CPU, (limits.timeout_seconds, limits.timeout_seconds))
    if config.get("render_page") is not None:
        import math
        import pymupdf
        with pymupdf.open(config["source"]) as document:
            if document.needs_pass:
                raise ParseError("ENCRYPTED_DOCUMENT")
            if not 1 <= config["render_page"] <= document.page_count:
                raise ParseError("INVALID_PAGE")
            page = document[config["render_page"] - 1]
            scale = min(150 / 72, math.sqrt(8_000_000 / page.rect.get_area()) * 0.99)
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            if pixmap.width * pixmap.height > 8_000_000:
                raise ParseError("PARSER_RESOURCE_LIMIT")
            data = pixmap.tobytes("png")
            if len(data) > limits.max_output_bytes:
                raise ParseError("PARSER_RESOURCE_LIMIT")
            Path(config["output"]).write_bytes(data)
        return
    from app.parsing.formats import records
    count = cells = total = 0
    has_content = False
    ocr_pages = []
    with Path(config["output"]).open("w", encoding="utf-8", newline="\n") as output:
        def write(record):
            nonlocal count, cells, total
            encoded = json.dumps(record.to_dict(), ensure_ascii=False, allow_nan=False) + "\n"
            size = len(encoded.encode("utf-8"))
            count += 1
            cells += len(record.cells)
            total += size
            if (size > limits.max_record_bytes or total > limits.max_output_bytes
                    or count > limits.max_records or cells > limits.max_cells):
                raise ParseError("PARSER_RESOURCE_LIMIT")
            output.write(encoded)
        fmt = Path(config["source"]).suffix.lower()[1:]
        write(Record("document_start", {"format": fmt}, metadata={"version": 1}))
        for record in records(Path(config["source"]), limits, config):
            if record.kind == "ocr_required":
                ocr_pages.append(record.location["page"])
            has_content |= bool(record.text and record.text.strip()) or any(
                cell.value not in (None, "") or cell.formula is not None for cell in record.cells)
            write(record)
        if not has_content and not ocr_pages:
            raise ParseError("EMPTY_DOCUMENT")
        write(Record("document_end", {"format": fmt}, metadata={
            "status": "needs_ocr" if ocr_pages else "complete", "ocr_pages": ocr_pages,
            "records": count, "cells": cells}))


if __name__ == "__main__":
    config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    try:
        main(config)
    except BaseException as exc:
        code = exc.code if isinstance(exc, ParseError) else (
            "PARSER_RESOURCE_LIMIT" if isinstance(exc, MemoryError) else "INVALID_DOCUMENT")
        Path(config["error"]).write_text(json.dumps({"code": code}), encoding="utf-8")
        sys.exit(1)
