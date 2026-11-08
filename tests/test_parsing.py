"""Synthetic fixtures are generated in temporary directories, never committed."""
from datetime import datetime
import json
from zipfile import ZipFile, ZIP_DEFLATED

import pytest

from app.parsing import ParseError, ParseLimits, parse_document, parse_to_jsonl, render_pdf_page


@pytest.fixture
def csv_file(tmp_path):
    path = tmp_path / "table.csv"
    path.write_text('name,amount,date,empty\nA,12345678901234567890.00100,2026-01-02,\n\n"B\ncontinued",0.0100,,\n', encoding="utf-8")
    return path


@pytest.fixture
def docx_file(tmp_path):
    from docx import Document
    document = Document()
    document.add_heading("First section", 1)
    document.add_paragraph("Before table")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Header"
    table.cell(0, 1).text = "Amount"
    table.cell(1, 0).text = "A"
    table.cell(1, 1).text = "123.4500"
    document.add_paragraph("After table")
    document.add_section()
    document.add_paragraph("Next section")
    path = tmp_path / "ordered.docx"
    document.save(path)
    return path


@pytest.fixture
def xlsx_file(tmp_path):
    from openpyxl import Workbook
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Rent Roll"
    sheet.merge_cells("A1:C1")
    sheet["A1"] = "Merged header"
    sheet.append(["Unit", "Amount", "Date"])
    sheet.append(["001", 123.45, datetime(2026, 1, 2, 12, 30)])
    sheet["B3"].number_format = "0.0000"
    sheet["A5"] = "after blank row"
    sheet["B5"] = "=B3*2"
    workbook.create_sheet("Second").append(["Other sheet", True])
    workbook.create_sheet("Empty")
    path = tmp_path / "book.xlsx"
    workbook.save(path)
    # Preserve a precision-sensitive lexical number which Python float cannot hold.
    with ZipFile(path) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    parts["xl/worksheets/sheet1.xml"] = parts["xl/worksheets/sheet1.xml"].replace(b"<v>123.45</v>", b"<v>12345678901234567890.00100</v>")
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return path


@pytest.fixture
def pdf_file(tmp_path):
    import pymupdf
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((40, 40), "Lease heading")
    page.insert_text((40, 70), "Rent 123.4500")
    # A ruled table with two columns and two rows.
    for y in (110, 140, 170):
        page.draw_line((40, y), (300, y))
    for x in (40, 170, 300):
        page.draw_line((x, 110), (x, 170))
    for position, text in [((50, 130), "Unit"), ((180, 130), "Rent"), ((50, 160), "A"), ((180, 160), "100.00")]:
        page.insert_text(position, text)
    page = document.new_page()
    page.insert_text((40, 40), "Second page")
    path = tmp_path / "digital.pdf"
    document.save(path)
    document.close()
    return path


def test_csv_preserves_precision_blanks_and_multiline_locations(csv_file):
    records = list(parse_document(csv_file))
    rows = [record for record in records if record.kind == "table_row"]
    assert len(rows) == 4 and rows[2].cells == []
    assert rows[1].cells[1].value == "12345678901234567890.00100"
    assert rows[1].cells[2].value == "2026-01-02"
    assert rows[1].cells[3].value == "" and rows[1].cells[3].kind == "empty"
    assert rows[3].cells[0].value == "B\ncontinued"
    assert rows[3].location["line_start"] == 4 and rows[3].location["line_end"] == 5
    assert rows[3].cells[1].location["column"] == 2
    assert records[-1].metadata["status"] == "complete"


def test_csv_utf16_semicolon(tmp_path):
    path = tmp_path / "encoded.csv"
    path.write_text("name;amount\nCafé;1,25\n", encoding="utf-16")
    rows = [r for r in parse_document(path) if r.kind == "table_row"]
    assert rows[1].cells[0].value == "Café" and rows[1].cells[1].value == "1,25"


def test_docx_body_order_tables_and_sections(docx_file):
    records = list(parse_document(docx_file))
    paragraphs = [r for r in records if r.kind == "paragraph" and "table" not in r.location]
    assert [r.text for r in paragraphs] == ["First section", "Before table", "After table", "", "Next section"]
    first_table = next(i for i, r in enumerate(records) if r.kind == "table_start")
    assert records[first_table - 1].text == "Before table"
    rows = [r for r in records if r.kind == "table_row"]
    assert rows[1].cells[1].value == "123.4500"
    assert rows[1].cells[1].location["row"] == 2
    assert len([r for r in records if r.kind == "section_start"]) == 2
    assert paragraphs[-1].location["section"] == 2


def test_xlsx_sheets_merges_exact_numbers_dates_and_blanks(xlsx_file):
    records = list(parse_document(xlsx_file))
    sheets = [r for r in records if r.kind == "sheet_start"]
    assert [r.location["sheet"] for r in sheets] == ["Rent Roll", "Second", "Empty"]
    assert sheets[0].metadata["merged_ranges"] == ["A1:C1"]
    rows = [r for r in records if r.kind == "table_row" and r.location["sheet"] == "Rent Roll"]
    assert len(rows) == 5
    assert rows[0].cells[1].kind == "empty"  # Never duplicate the merged anchor value.
    assert rows[2].cells[1].value == "12345678901234567890.00100"
    assert rows[2].cells[1].number_format == "0.0000"
    assert rows[2].cells[0].value == "001"
    assert rows[2].cells[2].kind == "date" and rows[2].cells[2].value.startswith("2026-01-02T12:30")
    assert rows[2].cells[2].raw is not None
    assert len(rows[3].cells) == 3 and all(c.kind == "empty" for c in rows[3].cells)
    assert rows[4].cells[1].formula == "=B3*2" and rows[4].cells[1].value is None
    assert rows[4].cells[1].location["address"] == "B5"


def test_pdf_pages_text_and_table_rows(pdf_file):
    records = list(parse_document(pdf_file))
    assert [r.location["page"] for r in records if r.kind == "page_start"] == [1, 2]
    assert any("Rent 123.4500" in (r.text or "") for r in records)
    rows = [r for r in records if r.kind == "table_row"]
    assert [[c.value for c in r.cells] for r in rows] == [["Unit", "Rent"], ["A", "100.00"]]
    assert rows[1].location["page"] == 1 and rows[1].cells[1].location["bbox"]
    assert records[-1].metadata["status"] == "complete"


@pytest.mark.parametrize("with_page_number", [False, True])
def test_scanned_pdf_is_explicitly_queued_for_fallback(tmp_path, with_page_number):
    import pymupdf
    original = pymupdf.open()
    page = original.new_page()
    page.insert_text((40, 40), "Synthetic scanned lease")
    raster = page.get_pixmap().tobytes("png")
    scanned = pymupdf.open()
    page = scanned.new_page()
    page.insert_image(page.rect, stream=raster)
    if with_page_number:
        page.insert_text((40, 810), "1")
    path = tmp_path / "scan.pdf"
    scanned.save(path)
    scanned.close()
    original.close()
    records = list(parse_document(path))
    fallback = next(r for r in records if r.kind == "ocr_required")
    assert fallback.location["page"] == 1
    assert fallback.metadata["executed"] is False
    assert fallback.metadata["render"]["max_pixels"] == 8_000_000
    assert records[-1].metadata == {"status": "needs_ocr", "ocr_pages": [1], "records": len(records)-1, "cells": 0}


@pytest.mark.parametrize("extension", ["pdf", "docx", "xlsx"])
def test_corrupt_files_fail_safely(tmp_path, extension):
    path = tmp_path / f"broken.{extension}"
    path.write_bytes(b"secret document contents")
    with pytest.raises(ParseError) as error:
        list(parse_document(path))
    assert "secret" not in str(error.value)


def test_empty_csv_does_not_silently_succeed(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_bytes(b"")
    with pytest.raises(ParseError, match="EMPTY_DOCUMENT"):
        list(parse_document(path))


@pytest.mark.parametrize("limit", [{"max_rows": 1}, {"max_columns": 1}, {"max_output_bytes": 100}, {"max_record_bytes": 100}, {"max_cells": 1}])
def test_limits_fail_without_publishing_partial_output(csv_file, tmp_path, limit):
    destination = tmp_path / "output.jsonl"
    destination.write_text("previous artifact")
    with pytest.raises(ParseError, match="PARSER_RESOURCE_LIMIT"):
        parse_to_jsonl(csv_file, destination, ParseLimits(**limit))
    assert destination.read_text() == "previous artifact"


def test_office_expansion_limits(xlsx_file):
    with pytest.raises(ParseError, match="PARSER_RESOURCE_LIMIT"):
        list(parse_document(xlsx_file, ParseLimits(max_zip_bytes=100)))


def test_record_serialization(csv_file, tmp_path):
    destination = tmp_path / "records.jsonl"
    parse_to_jsonl(csv_file, destination)
    records = [json.loads(line) for line in destination.read_text().splitlines()]
    assert records[0]["kind"] == "document_start" and records[-1]["kind"] == "document_end"


def test_fallback_render_is_local_and_pixel_bounded(pdf_file, tmp_path):
    import pymupdf
    image = tmp_path / "page.png"
    render_pdf_page(pdf_file, 1, image)
    pixmap = pymupdf.Pixmap(str(image))
    assert 0 < pixmap.width * pixmap.height <= 8_000_000
    with pytest.raises(ParseError, match="INVALID_PAGE"):
        render_pdf_page(pdf_file, 100, image)
    assert image.read_bytes().startswith(b"\x89PNG")


def test_document_page_limit(pdf_file):
    with pytest.raises(ParseError, match="PARSER_RESOURCE_LIMIT"):
        list(parse_document(pdf_file, ParseLimits(max_pages=1)))


def test_file_limit_before_process_start(csv_file):
    with pytest.raises(ParseError, match="FILE_TOO_LARGE"):
        list(parse_document(csv_file, ParseLimits(max_file_bytes=1)))


def test_timeout_preserves_existing_artifact(csv_file, tmp_path, monkeypatch):
    import subprocess
    import app.parsing
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("parser", 1)
    monkeypatch.setattr(app.parsing.subprocess, "run", timeout)
    output = tmp_path / "output"
    output.write_text("previous artifact")
    with pytest.raises(ParseError, match="PARSING_TIMEOUT"):
        parse_to_jsonl(csv_file, output)
    assert output.read_text() == "previous artifact"


def test_docx_headers_and_nested_tables_have_distinct_locations(tmp_path):
    from docx import Document
    document = Document()
    document.sections[0].header.paragraphs[0].text = "Property header"
    document.add_paragraph("Body")
    outer = document.add_table(rows=1, cols=1)
    outer.cell(0, 0).text = "Outer"
    inner = outer.cell(0, 0).add_table(rows=1, cols=2)
    inner.cell(0, 0).text = "Nested A"
    inner.cell(0, 1).text = "Nested B"
    path = tmp_path / "nested.docx"
    document.save(path)
    records = list(parse_document(path))
    header = next(r for r in records if r.text == "Property header")
    assert header.location["region"] == "header"
    rows = [r for r in records if r.kind == "table_row"]
    assert len(rows) == 2
    assert rows[0].location["table"] != rows[1].location["table"]
    assert rows[1].cells[1].value == "Nested B"


def test_xlsx_incorrect_dimension_hint_does_not_hide_cells(xlsx_file, tmp_path):
    with ZipFile(xlsx_file) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    parts["xl/worksheets/sheet1.xml"] = parts["xl/worksheets/sheet1.xml"].replace(b'<dimension ref="A1:C5"', b'<dimension ref="A1:A1"')
    path = tmp_path / "incorrect-dimensions.xlsx"
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    rows = [r for r in parse_document(path) if r.kind == "table_row" and r.location["sheet"] == "Rent Roll"]
    assert len(rows) == 5 and rows[-1].cells[1].formula == "=B3*2"
