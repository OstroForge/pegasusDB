"""Read an old Unicorn assessment-matrix workbook without writing to the database."""
from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from xml.etree import ElementTree as ET

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_EXCEL_EPOCH = datetime(1899, 12, 30)
_DNF_RE = re.compile(r"\s+dnf\s*$", re.IGNORECASE)


@dataclass
class MatrixDelegate:
    name: str
    did_not_finish: bool
    passed: bool | None
    ticks: dict[str, bool] = field(default_factory=dict)


@dataclass
class MatrixSheet:
    title: str
    course_date: datetime | None
    location: str
    instructor: str
    skills: list[str]
    delegates: list[MatrixDelegate]
    source: str = ""


def _norm(value: str) -> str:
    return " ".join((value or "").replace("\n", " ").split()).casefold()


def _column_index(letters: str) -> int:
    index = 0
    for char in letters:
        index = index * 26 + (ord(char.upper()) - 64)
    return index


def _split_ref(ref: str) -> tuple[int, int]:
    letters = "".join(ch for ch in ref if ch.isalpha())
    numbers = "".join(ch for ch in ref if ch.isdigit())
    return _column_index(letters), int(numbers)


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    strings = []
    for item in root.findall("m:si", _NS):
        strings.append("".join(node.text or "" for node in item.findall(".//m:t", _NS)))
    return strings


def _cell_text(cell, strings: list[str]) -> str:
    kind = cell.get("t")
    value = cell.find("m:v", _NS)
    if kind == "s" and value is not None and value.text:
        return strings[int(value.text)]
    if kind == "inlineStr":
        inline = cell.find("m:is", _NS)
        if inline is not None:
            return "".join(node.text or "" for node in inline.findall(".//m:t", _NS))
    if value is not None and value.text:
        return value.text
    return ""


def _sheet_grid(path: str) -> dict[tuple[int, int], str]:
    with zipfile.ZipFile(path) as archive:
        strings = _shared_strings(archive)
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        sheets = workbook.findall("m:sheets/m:sheet", _NS)
        if not sheets:
            raise ValueError("The workbook has no sheets.")
        # These matrices are a single sheet. Read the first one.
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        rel_ns = {"r": "http://schemas.openxmlformats.org/package/2006/relationships"}
        target = ""
        sheet_rid = sheets[0].get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
        for rel in rels.findall("r:Relationship", rel_ns):
            if rel.get("Id") == sheet_rid:
                target = rel.get("Target") or ""
                break
        sheet_path = target.lstrip("/")
        if not sheet_path.startswith("xl/"):
            sheet_path = "xl/" + sheet_path.lstrip("/")
        sheet = ET.fromstring(archive.read(sheet_path))

    grid: dict[tuple[int, int], str] = {}
    for cell in sheet.findall("m:sheetData/m:row/m:c", _NS):
        ref = cell.get("r") or ""
        text = _cell_text(cell, strings).strip()
        if ref and text:
            grid[_split_ref(ref)] = text
    return grid


def _excel_date(raw: str) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        serial = float(text)
    except ValueError:
        serial = None
    if serial and serial > 20000:
        return _EXCEL_EPOCH + timedelta(days=int(serial))
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text[:10], fmt)
        except ValueError:
            continue
    return None


def _label_below(grid: dict[tuple[int, int], str], label: str) -> str:
    wanted = _norm(label)
    for (col, row), value in grid.items():
        if _norm(value) == wanted:
            return grid.get((col, row + 1), "")
    return ""


def _is_tick(raw: str) -> bool | None:
    text = (raw or "").strip()
    if text in {"1", "1.0"}:
        return True
    if text in {"0", "0.0"}:
        return False
    return None


def course_code_from_title(title: str) -> str:
    """Best-effort course code from a matrix title such as 'EFaW Assessment Matrix'."""
    compact = "".join(ch for ch in (title or "").casefold() if ch.isalnum())
    if "emergencyfirstaid" in compact or compact.startswith("efaw") or "efaaw" in compact:
        return "EFAaW"
    if "requal" in compact or "faawr" in compact:
        return "FAAWR"
    if "firstaidatwork" in compact or compact.startswith("faaw") or "faaw" in compact:
        return "FAAW"
    return ""


def read_assessment_matrix(path: str) -> MatrixSheet:
    grid = _sheet_grid(path)
    if not grid:
        raise ValueError("The sheet is empty.")

    title = grid.get((1, 1), "")
    course_date = _excel_date(_label_below(grid, "Date"))
    location = _label_below(grid, "Location")
    instructor = _label_below(grid, "Instructor")

    skill_rows: list[tuple[int, str]] = []
    pass_row = None
    for (col, row), value in sorted(grid.items()):
        if col != 1 or row < 5:
            continue
        if _norm(value) in {"pass?", "pass"}:
            pass_row = row
            continue
        if _norm(value).startswith("if a delegate"):
            continue
        skill_rows.append((row, " ".join(value.replace("\n", " ").split())))

    name_row = min((row for row, _skill in skill_rows), default=5) - 1
    names: dict[int, str] = {}
    for (col, row), value in grid.items():
        if row == name_row and col >= 2 and _is_tick(value) is None:
            names[col] = " ".join(value.split())

    delegates: list[MatrixDelegate] = []
    for col, raw_name in sorted(names.items()):
        did_not_finish = bool(_DNF_RE.search(raw_name))
        name = _DNF_RE.sub("", raw_name).strip()
        ticks = {}
        for row, skill in skill_rows:
            tick = _is_tick(grid.get((col, row), ""))
            if tick is not None:
                ticks[skill] = tick
        passed = _is_tick(grid.get((col, pass_row), "")) if pass_row else None
        if did_not_finish:
            passed = False
        delegates.append(MatrixDelegate(
            name=name,
            did_not_finish=did_not_finish,
            passed=passed,
            ticks=ticks,
        ))

    return MatrixSheet(
        title=title,
        course_date=course_date,
        location=location,
        instructor=instructor,
        skills=[skill for _row, skill in skill_rows],
        delegates=delegates,
        source=path,
    )
