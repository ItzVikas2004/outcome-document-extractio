"""Bounded local parsing. No extraction, model calls, or network access."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from dataclasses import asdict

from app.parsing.model import Cell, ParseError, ParseLimits, Record

__all__ = ["parse_document", "parse_to_jsonl", "render_pdf_page", "ParseLimits", "ParseError", "Record", "Cell"]


def _run(source, destination, limits=None, *, csv_encoding=None, csv_delimiter=None, render_page=None):
    """Atomically publish a complete evidence stream; errors never publish partial data.

    Linux (including Docker) additionally enforces an address-space limit. On
    other platforms the structural/output limits and process timeout still apply.
    """
    limits = limits or ParseLimits()
    if any(type(value) is not int or value <= 0 for value in asdict(limits).values()):
        raise ValueError("Parser limits must be positive integers")
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination:
        raise ValueError("Source and destination must differ")
    if source.stat().st_size > limits.max_file_bytes:
        raise ParseError("FILE_TOO_LARGE")
    if source.suffix.lower() not in {".pdf", ".docx", ".xlsx", ".csv"}:
        raise ParseError("UNSUPPORTED_FORMAT")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="parse-", dir=destination.parent) as temporary:
        root = Path(temporary)
        config = root / "config.json"
        config.write_text(json.dumps({"source": str(source), "output": str(root / "records.jsonl"),
            "error": str(root / "error.json"), "limits": asdict(limits),
            "csv_encoding": csv_encoding, "csv_delimiter": csv_delimiter,
            "render_page": render_page, "parent_pid": os.getpid()}), encoding="utf-8")
        try:
            completed = subprocess.run([sys.executable, "-m", "app.parsing.runner", str(config)],
                cwd=Path(__file__).resolve().parents[2], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=limits.timeout_seconds, check=False)
        except subprocess.TimeoutExpired:
            raise ParseError("PARSING_TIMEOUT") from None
        if completed.returncode:
            error = root / "error.json"
            code = json.loads(error.read_text())["code"] if error.exists() else "PARSER_RESOURCE_LIMIT"
            raise ParseError(code)
        os.replace(root / "records.jsonl", destination)


def parse_to_jsonl(source, destination, limits=None, *, csv_encoding=None, csv_delimiter=None):
    """Parse to an atomically published evidence spool; never publish partial output."""
    _run(source, destination, limits, csv_encoding=csv_encoding, csv_delimiter=csv_delimiter)


def render_pdf_page(source, page, destination, limits=None):
    """Render one fallback page locally to PNG at <=150 DPI and <=8 million pixels.

    A later local OCR or approved gateway vision adapter can consume this artifact.
    Rendering never performs OCR, extraction or network I/O.
    """
    if Path(source).suffix.lower() != ".pdf" or type(page) is not int or page < 1:
        raise ValueError("A PDF and a one-based page number are required")
    _run(source, destination, limits, render_page=page)


def parse_document(source, limits=None, **options):
    """Yield ordered records from a disk spool, never an in-memory whole document.

    Consume fully or close the iterator (e.g. contextlib.closing) to remove its spool.
    A document_end record reports complete versus needs_ocr and affected pages.
    """
    with tempfile.TemporaryDirectory(prefix="evidence-") as temporary:
        output = Path(temporary) / "records.jsonl"
        parse_to_jsonl(source, output, limits, **options)
        with output.open(encoding="utf-8") as stream:
            for line in stream:
                yield Record.from_dict(json.loads(line))
