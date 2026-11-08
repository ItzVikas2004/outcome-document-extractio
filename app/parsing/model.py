"""Internal evidence records; deliberately separate from the extraction contract."""
from dataclasses import asdict, dataclass, field


@dataclass
class Cell:
    kind: str
    value: str | bool | None
    location: dict
    raw: str | None = None
    number_format: str | None = None
    formula: str | None = None


@dataclass
class Record:
    kind: str
    location: dict
    text: str | None = None
    cells: list[Cell] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        return cls(**{**value, "cells": [Cell(**cell) for cell in value.get("cells", [])]})


@dataclass(frozen=True)
class ParseLimits:
    max_file_bytes: int = 50 * 1024 * 1024
    max_zip_bytes: int = 128 * 1024 * 1024
    max_xml_bytes: int = 32 * 1024 * 1024
    max_shared_strings_bytes: int = 8 * 1024 * 1024
    max_pages: int = 2000
    max_rows: int = 200_000
    max_columns: int = 2048
    max_cells: int = 2_000_000
    max_records: int = 400_000
    max_record_bytes: int = 1024 * 1024
    max_output_bytes: int = 128 * 1024 * 1024
    max_memory_bytes: int = 768 * 1024 * 1024
    timeout_seconds: int = 120


class ParseError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)  # Never include source text or library exception messages.
