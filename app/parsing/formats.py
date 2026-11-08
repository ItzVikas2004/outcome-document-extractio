"""Format readers. Called only inside the bounded parser process."""
import csv
from datetime import date, datetime, time, timedelta
from zipfile import ZipFile

from app.parsing.model import Cell, ParseError, Record


def check_zip(path, limits):
    with ZipFile(path) as archive:
        members = archive.infolist()
        if len(members) > 10000 or sum(item.file_size for item in members) > limits.max_zip_bytes:
            raise ParseError("PARSER_RESOURCE_LIMIT")
        for item in members:
            if item.filename.endswith(".xml") and item.file_size > limits.max_xml_bytes:
                raise ParseError("PARSER_RESOURCE_LIMIT")
            if item.filename.endswith("sharedStrings.xml") and item.file_size > limits.max_shared_strings_bytes:
                raise ParseError("PARSER_RESOURCE_LIMIT")


def records(path, limits, options):
    suffix = path.suffix.lower()
    if suffix in {".docx", ".xlsx"}:
        check_zip(path, limits)
    if suffix == ".pdf":
        yield from pdf_records(path, limits)
    elif suffix == ".docx":
        yield from docx_records(path, limits)
    elif suffix == ".xlsx":
        yield from xlsx_records(path, limits)
    else:
        yield from csv_records(path, limits, options)


def csv_records(path, limits, options):
    with path.open("rb") as stream:
        prefix = stream.read(4)
    encoding = options.get("csv_encoding") or ("utf-16" if prefix.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig")
    csv.field_size_limit(limits.max_record_bytes)
    with path.open(encoding=encoding, newline="") as stream:
        sample = stream.read(8192)
        stream.seek(0)
        delimiter = options.get("csv_delimiter")
        if delimiter is None:
            try:
                delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
            except csv.Error:
                delimiter = ","
        if len(delimiter) != 1:
            raise ParseError("INVALID_CSV_DELIMITER")
        reader = csv.reader(stream, delimiter=delimiter, strict=True)
        yield Record("table_start", {"format": "csv", "table": 1}, metadata={"encoding": encoding, "delimiter": delimiter})
        previous_line = 0
        for row_number, row in enumerate(reader, 1):
            if row_number > limits.max_rows or len(row) > limits.max_columns:
                raise ParseError("PARSER_RESOURCE_LIMIT")
            loc = {"format": "csv", "table": 1, "row": row_number,
                   "line_start": previous_line + 1, "line_end": reader.line_num}
            previous_line = reader.line_num
            cells = [Cell("empty" if value == "" else "text", value,
                     {**loc, "column": column}, raw=value) for column, value in enumerate(row, 1)]
            yield Record("table_row", loc, cells=cells)
        yield Record("table_end", {"format": "csv", "table": 1})


def docx_records(path, limits):
    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    document = Document(path)
    section = 1
    paragraph = table = row_count = 0
    yield Record("section_start", {"format": "docx", "section": section})

    def walk(container, parent, base):
        nonlocal paragraph, table, row_count
        for position, element in enumerate(container, 1):
            loc = {**base, "position": position}
            if element.tag == qn("w:p"):
                paragraph += 1
                item = Paragraph(element, parent)
                yield Record("paragraph", {**loc, "paragraph": paragraph}, text=item.text,
                             metadata={"style": item.style.name if item.style else None})
            elif element.tag == qn("w:tbl"):
                table += 1
                table_id = table
                item = Table(element, parent)
                yield Record("table_start", {**loc, "table": table_id})
                for row_index, row in enumerate(item.rows, 1):
                    row_count += 1
                    if row_count > limits.max_rows or len(row.cells) > limits.max_columns:
                        raise ParseError("PARSER_RESOURCE_LIMIT")
                    row_loc = {**loc, "table": table_id, "row": row_index}
                    cells = []
                    seen = {}
                    for column, cell in enumerate(row.cells, 1):
                        cell_loc = {**row_loc, "column": column}
                        if cell._tc in seen:
                            cell_loc["merged_anchor_column"] = seen[cell._tc]
                        else:
                            seen[cell._tc] = column
                        cells.append(Cell("text" if cell.text else "empty", cell.text, cell_loc))
                    yield Record("table_row", row_loc, cells=cells)
                    seen.clear()
                    for column, cell in enumerate(row.cells, 1):
                        if cell._tc not in seen:
                            seen[cell._tc] = column
                            yield from walk(cell._tc, cell, {**row_loc, "column": column})
                yield Record("table_end", {**loc, "table": table_id})
            elif element.tag in {qn("w:sdt"), qn("w:sdtContent"), qn("w:customXml"), qn("w:ins")}:
                yield from walk(element, parent, loc)

    # Section properties on paragraphs delimit body sections; tables remain interleaved.
    for position, element in enumerate(document.element.body, 1):
        yield from walk([element], document, {"format": "docx", "section": section, "body_position": position})
        if element.tag == qn("w:p") and element.find(".//" + qn("w:sectPr")) is not None:
            yield Record("section_end", {"format": "docx", "section": section})
            section += 1
            yield Record("section_start", {"format": "docx", "section": section})
    yield Record("section_end", {"format": "docx", "section": section})
    # Header/footer stories have their own order, not a fabricated body position.
    for section_number, item in enumerate(document.sections, 1):
        for region in ("header", "first_page_header", "even_page_header", "footer", "first_page_footer", "even_page_footer"):
            story = getattr(item, region)
            if not story._has_definition:
                continue
            base = {"format": "docx", "section": section_number, "region": region}
            yield Record("region_start", base)
            yield from walk(story._element, story, base)
            yield Record("region_end", base)


def pdf_records(path, limits):
    import pymupdf
    with pymupdf.open(path) as document:
        if document.needs_pass:
            raise ParseError("ENCRYPTED_DOCUMENT")
        if document.page_count > limits.max_pages:
            raise ParseError("PARSER_RESOURCE_LIMIT")
        for page_index in range(document.page_count):
            page = document[page_index]
            loc = {"format": "pdf", "page": page_index + 1}
            yield Record("page_start", loc, metadata={"width": page.rect.width, "height": page.rect.height})
            blocks = page.get_text("blocks", flags=pymupdf.TEXTFLAGS_BLOCKS & ~pymupdf.TEXT_PRESERVE_IMAGES, sort=True)
            text_blocks = [block for block in blocks if block[6] == 0 and block[4].strip()]
            image_area = sum((pymupdf.Rect(image["bbox"]) & page.rect).get_area()
                             for image in page.get_image_info())
            sparse_scan = image_area > page.rect.get_area() * 0.5 and sum(len(b[4].strip()) for b in text_blocks) < 40
            if not text_blocks or sparse_scan:
                yield Record("ocr_required", loc, metadata={
                    "reason": "missing_text_layer" if not text_blocks else "image_with_sparse_text",
                    "fallback": "local_ocr_or_configured_approved_vision",
                    "render": {"page": page_index + 1, "dpi": 150, "max_pixels": 8_000_000},
                    "executed": False})
            # Geometric table detection is evidence organization, not field extraction.
            tables = page.find_tables().tables if text_blocks else []
            items = [(block[1], block[0], "paragraph", block) for block in text_blocks]
            items += [(table.bbox[1], table.bbox[0], "table", table) for table in tables]
            for position, (_, _, kind, item) in enumerate(sorted(items, key=lambda v: (v[0], v[1])), 1):
                if kind == "paragraph":
                    bbox = item[:4]
                    if any(pymupdf.Rect(bbox) in pymupdf.Rect(table.bbox) for table in tables):
                        continue
                    yield Record("paragraph", {**loc, "position": position, "paragraph": item[5] + 1, "bbox": list(bbox)}, text=item[4])
                else:
                    table_loc = {**loc, "position": position, "table": position, "bbox": list(item.bbox)}
                    yield Record("table_start", table_loc)
                    for row_index, values in enumerate(item.extract(), 1):
                        if row_index > limits.max_rows or len(values) > limits.max_columns:
                            raise ParseError("PARSER_RESOURCE_LIMIT")
                        cells = [Cell("empty" if value in (None, "") else "text", value,
                                 {**table_loc, "row": row_index, "column": column,
                                  "bbox": item.rows[row_index - 1].cells[column - 1]})
                                 for column, value in enumerate(values, 1)]
                        yield Record("table_row", {**table_loc, "row": row_index}, cells=cells)
                    yield Record("table_end", table_loc)
            yield Record("page_end", loc)


def sheet_xml(archive, name):
    from defusedxml.ElementTree import iterparse
    with archive.open(name) as stream:
        for _, element in iterparse(stream, events=("end",)):
            tag = element.tag.rsplit("}", 1)[-1]
            if tag == "row":
                values = {}
                for cell in element:
                    children = {child.tag.rsplit("}", 1)[-1]: child.text for child in cell}
                    values[cell.attrib["r"]] = {"raw": children.get("v"), "formula": children.get("f")}
                yield int(element.attrib["r"]), values
                element.clear()


def sheet_metadata(archive, name, limits):
    from defusedxml.ElementTree import iterparse
    from openpyxl.utils.cell import range_boundaries, coordinate_from_string, column_index_from_string
    merges = []
    max_row = max_column = 0
    with archive.open(name) as stream:
        for _, element in iterparse(stream, events=("end",)):
            tag = element.tag.rsplit("}", 1)[-1]
            if tag == "row":
                max_row = max(max_row, int(element.attrib["r"]))
                for cell in element:
                    column, _ = coordinate_from_string(cell.attrib["r"])
                    max_column = max(max_column, column_index_from_string(column))
                element.clear()
            elif tag == "mergeCell":
                reference = element.attrib["ref"]
                merges.append(reference)
                _, _, column, row = range_boundaries(reference)
                max_row, max_column = max(max_row, row), max(max_column, column)
                element.clear()
            if max_row > limits.max_rows or max_column > limits.max_columns or len(merges) > limits.max_rows:
                raise ParseError("PARSER_RESOURCE_LIMIT")
    return merges, max_row, max_column


def xlsx_records(path, limits):
    from openpyxl import load_workbook
    from openpyxl.utils import get_column_letter
    workbook = load_workbook(path, read_only=True, data_only=False, keep_links=False)
    try:
        with ZipFile(path) as archive:
            for sheet_index, sheet in enumerate(workbook.worksheets, 1):
                loc = {"format": "xlsx", "sheet": sheet.title, "sheet_index": sheet_index}
                # Read-only openpyxl omits merged ranges and rounds numeric/date values.
                # Supplement it with streaming OOXML, preserving the original scalar.
                name = sheet._worksheet_path
                merges, max_row, max_column = sheet_metadata(archive, name, limits)
                yield Record("sheet_start", loc, metadata={"merged_ranges": merges,
                    "state": sheet.sheet_state, "date_epoch": workbook.epoch.isoformat()})
                # Ignore inaccurate producer dimension hints; discover actual occupied rows.
                sheet.reset_dimensions()
                raw_rows = iter(sheet_xml(archive, name))
                raw_current = next(raw_rows, None)
                for row_index, row in enumerate(sheet.iter_rows(max_row=max_row, max_col=max_column) if max_row and max_column else (), 1):
                    if row_index > limits.max_rows or len(row) > limits.max_columns:
                        raise ParseError("PARSER_RESOURCE_LIMIT")
                    raw = raw_current[1] if raw_current and raw_current[0] == row_index else {}
                    if raw_current and raw_current[0] == row_index:
                        raw_current = next(raw_rows, None)
                    cells = []
                    for column, cell in enumerate(row, 1):
                        address = f"{get_column_letter(column)}{row_index}"
                        cell_loc = {**loc, "row": row_index, "column": column, "address": address}
                        evidence = raw.get(address, {})
                        original = evidence.get("raw")
                        value = cell.value
                        formula = value if cell.data_type == "f" else None
                        if formula:
                            kind, value = "formula", original
                        elif value is None:
                            kind = "empty"
                        elif isinstance(value, bool):
                            kind = "boolean"
                        elif isinstance(value, (datetime, date, time)):
                            kind, value = "date", value.isoformat()
                        elif isinstance(value, timedelta):
                            kind, value = "duration", str(value)
                        elif cell.data_type == "n":
                            kind, value = "number", original if original is not None else str(value)
                        elif cell.data_type == "e":
                            kind = "error"
                        else:
                            kind, value = "text", str(value)
                        cells.append(Cell(kind, value, cell_loc, raw=original,
                                          number_format=getattr(cell, "number_format", None), formula=formula))
                    yield Record("table_row", {**loc, "row": row_index}, cells=cells)
                yield Record("sheet_end", loc)
    finally:
        workbook.close()
