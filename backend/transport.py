import hashlib
import json
import math
import os
import posixpath
import re
import sqlite3
import statistics
import struct
import xml.etree.ElementTree as ET
import zipfile
import zlib
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


MODEL_NAME = "transport_workbook_summary"
MODEL_VERSION = "1.4.0"
MODEL_PARAMETERS = {
    "source_format": "xlsx_or_xls",
    "cell_values": "stored_values_only",
    "current_columns": ["AI", "BI", "DrainI", "GateI", "SourceI"],
    "voltage_columns": ["AV", "BV", "DrainV", "GateV", "SourceV"],
    "resistance_column": "RES",
    "maximum_preview_points_per_sheet": 240,
    "linear_fit": "ordinary_least_squares",
}
XML_NAMESPACE = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NAMESPACE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NAMESPACE = "http://schemas.openxmlformats.org/package/2006/relationships"
LOCAL_FILE_HEADER = struct.Struct("<IHHHHHIIIHH")
LOCAL_FILE_SIGNATURE = 0x04034B50
DATA_DESCRIPTOR_SIGNATURE = b"PK\x07\x08"
MAX_WORKBOOK_MEMBER_BYTES = 64 * 1024 * 1024
MAX_WORKBOOK_EXPANDED_BYTES = 256 * 1024 * 1024


def _inflate_member(compressed):
    decompressor = zlib.decompressobj(-15)
    content = decompressor.decompress(compressed, MAX_WORKBOOK_MEMBER_BYTES+1)
    if len(content)>MAX_WORKBOOK_MEMBER_BYTES or decompressor.unconsumed_tail:
        raise TransportError(422, 'workbook_expansion_limit', 'Workbook member exceeds the 64 MiB expanded-size limit.')
    return content,decompressor


class TransportError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(message)


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _recovered_zip_entries(path: Path) -> tuple[dict[str, bytes], list[str]]:
    data = path.read_bytes()
    entries: dict[str, bytes] = {}
    offset = 0
    while offset + LOCAL_FILE_HEADER.size <= len(data):
        if data[offset : offset + 4] != b"PK\x03\x04":
            next_offset = data.find(b"PK\x03\x04", offset + 1)
            if next_offset < 0:
                break
            offset = next_offset
        (
            signature,
            _version,
            flags,
            compression,
            _modified_time,
            _modified_date,
            _crc32,
            compressed_size,
            _uncompressed_size,
            filename_length,
            extra_length,
        ) = LOCAL_FILE_HEADER.unpack_from(data, offset)
        if signature != LOCAL_FILE_SIGNATURE:
            break
        name_start = offset + LOCAL_FILE_HEADER.size
        name_end = name_start + filename_length
        content_start = name_end + extra_length
        if content_start > len(data):
            break
        filename = data[name_start:name_end].decode("utf-8", "replace")
        try:
            if flags & 0x08:
                if compression == 8:
                    content, decompressor = _inflate_member(data[content_start:])
                    if not decompressor.eof:
                        break
                    consumed = len(data[content_start:]) - len(
                        decompressor.unused_data
                    )
                elif compression == 0:
                    next_header = data.find(b"PK\x03\x04", content_start)
                    if next_header < 0:
                        break
                    descriptor_start = max(content_start, next_header - 16)
                    content = data[content_start:descriptor_start]
                    consumed = len(content)
                else:
                    break
                offset = content_start + consumed
                if data[offset : offset + 4] == DATA_DESCRIPTOR_SIGNATURE:
                    offset += 16
                else:
                    offset += 12
            else:
                compressed = data[content_start : content_start + compressed_size]
                if len(compressed) != compressed_size:
                    break
                if compression == 8:
                    content, decompressor = _inflate_member(compressed)
                    if not decompressor.eof:
                        break
                elif compression == 0:
                    content = compressed
                else:
                    break
                offset = content_start + compressed_size
        except zlib.error:
            break
        if len(content)>MAX_WORKBOOK_MEMBER_BYTES or sum(map(len,entries.values()))+len(content)>MAX_WORKBOOK_EXPANDED_BYTES or len(entries)>=4096:
            raise TransportError(422,'workbook_expansion_limit','Workbook exceeds supported expanded-size limits.')
        if not flags & 0x08 and (len(content)!=_uncompressed_size or zlib.crc32(content)!=_crc32):
            break
        entries[filename] = content
    if not entries:
        raise TransportError(
            422,
            "unreadable_workbook",
            "The workbook archive could not be read or partially recovered.",
        )
    warnings = [
        "The XLSX ZIP central directory is missing or damaged.",
        f"Recovered {len(entries)} complete archive members before the truncated region.",
        "Results from this workbook are partial and must not be treated as a complete acquisition.",
    ]
    return entries, warnings


def _read_zip_entries(path: Path) -> tuple[dict[str, bytes], list[str]]:
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members)>4096 or any(m.file_size>MAX_WORKBOOK_MEMBER_BYTES for m in members) or sum(m.file_size for m in members)>MAX_WORKBOOK_EXPANDED_BYTES:
                raise TransportError(422,'workbook_expansion_limit','Workbook exceeds supported expanded-size limits.')
            bad_member = archive.testzip()
            if bad_member is not None:
                raise zipfile.BadZipFile(f"CRC failure in {bad_member}")
            return {
                name: archive.read(name)
                for name in archive.namelist()
                if not name.endswith("/")
            }, []
    except (zipfile.BadZipFile, EOFError):
        return _recovered_zip_entries(path)


def _shared_strings(entries: dict[str, bytes]) -> list[str]:
    content = entries.get("xl/sharedStrings.xml")
    if content is None:
        return []
    root = ET.fromstring(content.decode("utf-8-sig"))
    return [
        "".join(node.text or "" for node in item.iter(f"{{{XML_NAMESPACE}}}t"))
        for item in root.findall(f"{{{XML_NAMESPACE}}}si")
    ]


def _worksheet_paths(entries: dict[str, bytes]) -> list[tuple[str, str]]:
    workbook_content = entries.get("xl/workbook.xml")
    relationships_content = entries.get("xl/_rels/workbook.xml.rels")
    if workbook_content is not None and relationships_content is not None:
        relationships_root = ET.fromstring(
            relationships_content.decode("utf-8-sig")
        )
        relationships = {
            relation.attrib["Id"]: relation.attrib["Target"]
            for relation in relationships_root.findall(
                f"{{{PACKAGE_REL_NAMESPACE}}}Relationship"
            )
        }
        workbook_root = ET.fromstring(workbook_content.decode("utf-8-sig"))
        paths = []
        for sheet in workbook_root.findall(
            f".//{{{XML_NAMESPACE}}}sheet"
        ):
            target = relationships.get(sheet.attrib.get(f"{{{REL_NAMESPACE}}}id"))
            if target is None:
                continue
            if target.startswith("/"):
                path = target.lstrip("/")
            else:
                path = posixpath.normpath(posixpath.join("xl", target))
            if path in entries:
                paths.append((sheet.attrib["name"], path))
        if paths:
            return paths
    recovered = []
    for path in entries:
        match = re.fullmatch(r"xl/worksheets/sheet(\d+)\.xml", path)
        if match:
            recovered.append((int(match.group(1)), path))
    return [(f"Recovered sheet {number}", path) for number, path in sorted(recovered)]


def _column_index(cell_reference: str) -> int:
    letters = re.match(r"[A-Z]+", cell_reference.upper())
    if letters is None:
        return 0
    value = 0
    for character in letters.group(0):
        value = value * 26 + ord(character) - ord("A") + 1
    return value - 1


def _cell_value(cell: ET.Element, shared_strings: list[str]) -> object:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(
            node.text or "" for node in cell.iter(f"{{{XML_NAMESPACE}}}t")
        )
    value_node = cell.find(f"{{{XML_NAMESPACE}}}v")
    if value_node is None or value_node.text is None:
        return None
    value = value_node.text
    if cell_type in {"str", "e"}:
        return value
    if cell_type == "s":
        try:
            return shared_strings[int(value)]
        except (ValueError, IndexError):
            return value
    if cell_type == "b":
        return value == "1"
    try:
        numeric = float(value)
        return int(numeric) if numeric.is_integer() else numeric
    except ValueError:
        return value


def _worksheet_rows(content: bytes, shared_strings: list[str]) -> list[list[object]]:
    root = ET.fromstring(content.decode("utf-8-sig"))
    rows: list[list[object]] = []
    for row in root.findall(f".//{{{XML_NAMESPACE}}}row"):
        try:
            row_number = int(row.attrib.get('r', len(rows) + 1))
        except ValueError as error:
            raise TransportError(422, 'invalid_worksheet_coordinates', 'Invalid worksheet row number.') from error
        if not len(rows) < row_number <= 1_048_576:
            raise TransportError(422, 'invalid_worksheet_coordinates', 'Worksheet rows must have unique increasing Excel row numbers.')
        cells: dict[int, object] = {}
        for cell in row.findall(f"{{{XML_NAMESPACE}}}c"):
            reference = cell.attrib.get('r')
            coordinate = re.fullmatch(r'([A-Za-z]+)([1-9][0-9]*)', reference) if reference else None
            if reference and not coordinate:
                raise TransportError(422, 'invalid_worksheet_coordinates', 'Invalid worksheet cell reference.')
            index = _column_index(reference) if reference else max(cells, default=-1) + 1
            if not 0 <= index < 16_384 or index in cells:
                raise TransportError(422, 'invalid_worksheet_coordinates', 'Worksheet columns exceed Excel limits or repeat within a row.')
            if coordinate and int(coordinate[2]) != row_number:
                raise TransportError(422, 'invalid_worksheet_coordinates', 'A cell reference disagrees with its worksheet row.')
            cells[index] = _cell_value(cell, shared_strings)
        # XLSX omits empty rows from XML. Keep their original positions so a
        # missing observation cannot disappear from branch or source provenance.
        rows.extend([] for _ in range(row_number - len(rows) - 1))
        width = max(cells, default=-1) + 1
        rows.append([cells.get(index) for index in range(width)])
    return rows


def read_workbook(path: Path) -> tuple[list[dict], list[str]]:
    with path.open('rb') as source:
        signature = source.read(8)
    if signature == bytes.fromhex('d0cf11e0a1b11ae1'):
        import xlrd
        try:
            book = xlrd.open_workbook(str(path), on_demand=True)
            sheets = []
            for sheet in book.sheets():
                rows = []
                for row in sheet.get_rows():
                    rows.append([
                        xlrd.xldate.xldate_as_datetime(cell.value, book.datemode).isoformat()
                        if cell.ctype == xlrd.XL_CELL_DATE else
                        bool(cell.value) if cell.ctype == xlrd.XL_CELL_BOOLEAN else
                        None if cell.ctype in {xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_ERROR} else cell.value
                        for cell in row
                    ])
                sheets.append({'name': sheet.name, 'rows': rows})
            book.release_resources()
            return sheets, ['Legacy XLS: stored cell values are read; formulas are not recalculated.']
        except (xlrd.XLRDError, ValueError, OSError) as error:
            raise TransportError(422, 'invalid_xls', 'The legacy workbook could not be read.') from error
    entries, warnings = _read_zip_entries(path)
    shared_strings = _shared_strings(entries)
    sheets = []
    for name, worksheet_path in _worksheet_paths(entries):
        try:
            rows = _worksheet_rows(entries[worksheet_path], shared_strings)
        except (ET.ParseError, UnicodeDecodeError):
            warnings.append(f"Worksheet {name!r} could not be parsed and was skipped.")
            continue
        sheets.append({"name": name, "rows": rows})
    if not sheets:
        raise TransportError(
            422,
            "no_readable_worksheets",
            "The workbook does not contain a readable worksheet.",
        )
    return sheets, warnings


def _numeric_values(rows: list[dict], column: str) -> list[float]:
    values = []
    for row in rows:
        value = row.get(column)
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            values.append(float(value))
    return values


def _percentile(sorted_values: list[float], fraction: float) -> float | None:
    if not sorted_values:
        return None
    position = (len(sorted_values) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def _statistics(values: list[float]) -> dict | None:
    if not values:
        return None
    ordered = sorted(values)
    return {
        "count": len(values),
        "minimum": min(values),
        "p05": _percentile(ordered, 0.05),
        "median": statistics.median(values),
        "mean": statistics.fmean(values),
        "p95": _percentile(ordered, 0.95),
        "maximum": max(values),
        "standard_deviation": statistics.pstdev(values) if len(values) > 1 else 0.0,
    }


def _linear_fit(x_values: list[float], y_values: list[float]) -> dict | None:
    count = min(len(x_values), len(y_values))
    if count < 3:
        return None
    x_values = x_values[:count]
    y_values = y_values[:count]
    x_mean = statistics.fmean(x_values)
    y_mean = statistics.fmean(y_values)
    denominator = sum((value - x_mean) ** 2 for value in x_values)
    if denominator <= 0:
        return None
    slope = sum(
        (x_value - x_mean) * (y_value - y_mean)
        for x_value, y_value in zip(x_values, y_values)
    ) / denominator
    intercept = y_mean - slope * x_mean
    residual_sum = sum(
        (y_value - (intercept + slope * x_value)) ** 2
        for x_value, y_value in zip(x_values, y_values)
    )
    total_sum = sum((value - y_mean) ** 2 for value in y_values)
    r_squared = 1.0 - residual_sum / total_sum if total_sum > 0 else None
    return {
        "point_count": count,
        "slope": slope,
        "intercept": intercept,
        "r_squared": r_squared,
    }


def _measurement_type(name: str, rows: list[dict]) -> str:
    normalized_name = name.lower()
    if normalized_name in {"calc", "settings"} or not rows:
        return "metadata"
    if "iv" in normalized_name and "i-t" not in normalized_name:
        return "iv_sweep"
    for voltage_column in ("AV", "BV", "DrainV", "GateV", "SourceV"):
        values = _numeric_values(rows, voltage_column)
        if len(values) >= 3 and max(values) - min(values) > 1e-9:
            return "voltage_sweep"
    if len(_numeric_values(rows, "Time")) >= 2:
        return "current_time"
    if len(rows) == 1:
        return "operating_point"
    return "tabular_measurement"


def _downsample(rows: list[dict], maximum_points: int) -> list[dict]:
    if len(rows) <= maximum_points:
        selected = rows
    else:
        indexes = {
            round(index * (len(rows) - 1) / (maximum_points - 1))
            for index in range(maximum_points)
        }
        selected = [row for index, row in enumerate(rows) if index in indexes]
    return [
        {
            key: value
            for key, value in row.items()
            if key in {"Time", "AI", "BI", "AV", "BV", "RES", "DrainI", "DrainV", "GateI", "GateV", "SourceI", "SourceV"}
            and isinstance(value, (int, float))
        }
        for row in selected
    ]


def _analyze_sheet(sheet: dict) -> dict:
    raw_rows = sheet["rows"]
    if not raw_rows:
        return {
            "name": sheet["name"],
            "measurement_type": "metadata",
            "row_count": 0,
            "columns": [],
        }
    headers = [str(value).strip() if value is not None else "" for value in raw_rows[0]]
    data_rows = []
    for raw_row in raw_rows[1:]:
        record = {
            header: raw_row[index] if index < len(raw_row) else None
            for index, header in enumerate(headers)
            if header
        }
        if any(value is not None for value in record.values()):
            data_rows.append(record)
    measurement_type = _measurement_type(sheet["name"], data_rows)
    result = {
        "name": sheet["name"],
        "measurement_type": measurement_type,
        "row_count": len(data_rows),
        "columns": headers,
    }
    if measurement_type == "metadata":
        result['settings_rows'] = raw_rows
        return result
    channel_statistics = {}
    for column in ("AI", "BI", "AV", "BV", "RES", "DrainI", "DrainV", "GateI", "GateV", "SourceI", "SourceV"):
        summary = _statistics(_numeric_values(data_rows, column))
        if summary is not None:
            channel_statistics[column] = summary
    result["channel_statistics"] = channel_statistics
    time_values = _numeric_values(data_rows, "Time")
    if time_values:
        result["time"] = {
            "start": min(time_values),
            "end": max(time_values),
            "duration": max(time_values) - min(time_values),
            "median_step": (
                statistics.median(
                    later - earlier
                    for earlier, later in zip(time_values, time_values[1:])
                    if later > earlier
                )
                if any(later > earlier for earlier, later in zip(time_values, time_values[1:]))
                else None
            ),
        }
        for current_column in ("AI", "BI", "DrainI", "GateI", "SourceI"):
            pairs = [(row['Time'], row[current_column]) for row in data_rows
                     if all(isinstance(row.get(k), (int, float)) and math.isfinite(row[k])
                            for k in ('Time', current_column))]
            fit = _linear_fit([p[0] for p in pairs], [p[1] for p in pairs])
            if fit is not None:
                result.setdefault("current_drift", {})[current_column] = fit
    if measurement_type in {"iv_sweep", "voltage_sweep"}:
        fits = {}
        for voltage_column, current_column in (("AV", "AI"), ("BV", "BI"), ("DrainV", "DrainI"), ("GateV", "GateI"), ("SourceV", "SourceI")):
            pairs = [
                (float(row[voltage_column]), float(row[current_column]))
                for row in data_rows
                if isinstance(row.get(voltage_column), (int, float))
                and isinstance(row.get(current_column), (int, float))
                and math.isfinite(float(row[voltage_column]))
                and math.isfinite(float(row[current_column]))
            ]
            if len(pairs) < 3:
                continue
            voltage_values = [pair[0] for pair in pairs]
            current_values = [pair[1] for pair in pairs]
            fit = _linear_fit(voltage_values, current_values)
            if fit is None:
                continue
            fit["voltage_span"] = max(voltage_values) - min(voltage_values)
            fit["resistance_ohm"] = (
                1.0 / fit["slope"] if abs(fit["slope"]) > 0 else None
            )
            fits[f"{voltage_column}_{current_column}"] = fit
        if fits:
            result["iv_fits"] = fits
    result["preview_series"] = _downsample(
        data_rows,
        MODEL_PARAMETERS["maximum_preview_points_per_sheet"],
    )
    return result


def _settings_metadata(sheets: list[dict]) -> dict:
    settings = next(
        (
            sheet
            for sheet in sheets
            if sheet["name"].casefold() == "settings"
        ),
        None,
    )
    if settings is None:
        return {}
    rows: dict[str, list[object]] = {}
    for row in settings.get("rows", []):
        if not row or row[0] in {None, ""}:
            continue
        key = str(row[0]).strip().casefold()
        rows[key] = list(row[1:])
    terminals = rows.get("device terminal", [])
    names = rows.get("name", [])
    operations = rows.get("operation mode", [])
    current_modes = rows.get("measure current", [])
    voltage_modes = rows.get("measure voltage", [])
    terminal_count = max(
        len(terminals),
        len(names),
        len(operations),
        len(current_modes),
        len(voltage_modes),
    )
    terminal_details = []
    explicit_roles = {"source", "drain", "gate"}
    for index in range(terminal_count):
        terminal = str(terminals[index]).strip() if index < len(terminals) else ""
        name = str(names[index]).strip() if index < len(names) else ""
        operation = (
            str(operations[index]).strip()
            if index < len(operations)
            else ""
        )
        current_mode = (
            str(current_modes[index]).strip()
            if index < len(current_modes)
            else ""
        )
        voltage_mode = (
            str(voltage_modes[index]).strip()
            if index < len(voltage_modes)
            else ""
        )
        if not any((terminal, name, operation, current_mode, voltage_mode)):
            continue
        role_candidates = {
            value.casefold()
            for value in (terminal, name)
            if value and value.casefold() in explicit_roles
        }
        role = next(iter(role_candidates), None)
        role_basis = "explicit workbook label" if role else "not explicit in workbook"
        terminal_details.append(
            {
                "device_terminal": terminal or None,
                "terminal_name": name or None,
                "terminal_role": role,
                "role_basis": role_basis,
                "operation_mode": operation or None,
                "current_data": current_mode.casefold() or None,
                "voltage_data": voltage_mode.casefold() or None,
            }
        )
    biased = [
        detail
        for detail in terminal_details
        if detail["operation_mode"]
        and "bias" in detail["operation_mode"].casefold()
    ]
    role_counts = {
        role: sum(detail["terminal_role"] == role for detail in terminal_details)
        for role in explicit_roles
    }
    warnings = []
    if any(role_counts.values()) and any(
        detail["terminal_role"] is None for detail in terminal_details
    ):
        warnings.append(
            "Some terminal roles are explicit while other terminals remain ambiguous."
        )
    if not any(role_counts.values()) and terminal_details:
        warnings.append(
            "Source, drain, and gate roles are not explicit in this workbook; "
            "A/B or AV/BV labels cannot be safely mapped to device roles."
        )
    return {
        "terminals": terminal_details,
        "biased_terminals": biased,
        "role_warnings": warnings,
        "last_executed": next(
            (
                str(value).strip()
                for value in rows.get("last executed", [])
                if value not in {None, ""}
            ),
            None,
        ),
    }


def analyze_workbook(path: Path) -> dict:
    sheets, warnings = read_workbook(path)
    analyzed_sheets = [_analyze_sheet(sheet) for sheet in sheets]
    settings_metadata = _settings_metadata(sheets)
    measurement_sheets = [
        sheet for sheet in analyzed_sheets if sheet["measurement_type"] != "metadata"
    ]
    measurement_types: dict[str, int] = {}
    resistance_values = []
    iv_resistance_values = []
    zero_bias_ai_medians = []
    zero_bias_bi_medians = []
    for sheet in measurement_sheets:
        kind = sheet["measurement_type"]
        measurement_types[kind] = measurement_types.get(kind, 0) + 1
        resistance = sheet.get("channel_statistics", {}).get("RES")
        if resistance and resistance["median"] not in {None, 0}:
            resistance_values.append(resistance["median"])
        for fit in sheet.get("iv_fits", {}).values():
            value = fit.get("resistance_ohm")
            if isinstance(value, (int, float)) and math.isfinite(value):
                iv_resistance_values.append(value)
        av = sheet.get("channel_statistics", {}).get("AV")
        bv = sheet.get("channel_statistics", {}).get("BV")
        if (
            sheet["measurement_type"] == "current_time"
            and av
            and bv
            and abs(av["mean"]) <= 1e-9
            and abs(bv["mean"]) <= 1e-9
        ):
            ai = sheet.get("channel_statistics", {}).get("AI")
            bi = sheet.get("channel_statistics", {}).get("BI")
            if ai:
                zero_bias_ai_medians.append(ai["median"])
            if bi:
                zero_bias_bi_medians.append(bi["median"])
    warnings = list(warnings)
    if any(column in {"Time", "AI", "BI", "AV", "BV", "RES"} for sheet in analyzed_sheets for column in sheet.get("columns", [])):
        warnings.append(
            "Column units are not explicit in the worksheets; the UI labels conventional probe-station units as inferred (s, A, V, ohm)."
        )
    return {
        "model_name": MODEL_NAME,
        "model_version": MODEL_VERSION,
        "parameters": MODEL_PARAMETERS,
        "workbook": {
            "filename": path.name,
            "worksheet_count": len(analyzed_sheets),
            "measurement_sheet_count": len(measurement_sheets),
            "warnings": warnings,
            "settings": settings_metadata,
        },
        "summary": {
            "measurement_types": measurement_types,
            "total_data_rows": sum(sheet["row_count"] for sheet in measurement_sheets),
            "median_recorded_resistance_ohm": (
                statistics.median(resistance_values) if resistance_values else None
            ),
            "median_iv_fit_resistance_ohm": (
                statistics.median(iv_resistance_values) if iv_resistance_values else None
            ),
            "median_zero_bias_ai_a": (
                statistics.median(zero_bias_ai_medians) if zero_bias_ai_medians else None
            ),
            "median_zero_bias_bi_a": (
                statistics.median(zero_bias_bi_medians) if zero_bias_bi_medians else None
            ),
        },
        "sheets": analyzed_sheets,
    }


def _verified_source(metadata: dict, raw_data_directory: Path) -> tuple[Path, str]:
    raw_root = raw_data_directory.resolve()
    source_path = Path(metadata["storage_path"]).resolve()
    if source_path != raw_root and raw_root not in source_path.parents:
        raise TransportError(
            409,
            "invalid_source_path",
            "The imported file is outside the managed raw-data directory.",
        )
    try:
        source_bytes = source_path.read_bytes()
    except OSError as error:
        raise TransportError(404, "source_unavailable", "The imported file is unavailable.") from error
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    if source_sha256.lower() != metadata["sha256"].lower():
        raise TransportError(
            409,
            "source_checksum_mismatch",
            "The imported file no longer matches its recorded SHA-256 checksum.",
        )
    return source_path, source_sha256


def load_transport_result(processing_run: dict, processed_directory: Path) -> dict:
    processed_root = processed_directory.resolve()
    result_path = Path(processing_run["result_path"]).resolve()
    if result_path != processed_root and processed_root not in result_path.parents:
        raise TransportError(409, "invalid_result_path", "The result is outside managed storage.")
    try:
        result_bytes = result_path.read_bytes()
    except OSError as error:
        raise TransportError(404, "result_unavailable", "The transport result is unavailable.") from error
    result_sha256 = hashlib.sha256(result_bytes).hexdigest()
    if result_sha256.lower() != processing_run["result_sha256"].lower():
        raise TransportError(409, "result_checksum_mismatch", "The transport result checksum does not match.")
    result = json.loads(result_bytes.decode("utf-8"))
    result["result_sha256"] = result_sha256
    return result


def process_imported_transport_file(
    metadata: dict,
    raw_data_directory: Path,
    processed_directory: Path,
) -> dict:
    import database

    if "transport" not in str(metadata.get("technique", "")).lower():
        raise TransportError(422, "not_transport_data", "The dataset is not labeled as Transport properties.")
    source_path, source_sha256 = _verified_source(metadata, raw_data_directory)
    parameters = {
        **MODEL_PARAMETERS,
        "import_metadata": {
            key: metadata.get(key)
            for key in (
                "file_id",
                "technique",
                "material_system",
                "sample_id",
                "substrate",
                "measurement_role",
                "updated_at",
            )
        },
    }
    parameters_json = canonical_json(parameters)
    with database.connect_database() as connection:
        existing = database.find_processing_run(
            connection,
            metadata["file_id"],
            source_sha256,
            MODEL_NAME,
            MODEL_VERSION,
            parameters_json,
        )
    if existing is not None:
        return load_transport_result(existing, processed_directory)

    result = analyze_workbook(source_path)
    result["parameters"] = parameters
    result.update(
        {
            "processing_id": str(uuid4()),
            "file_id": metadata["file_id"],
            "sample_id": metadata.get("sample_id"),
            "source_sha256": source_sha256,
            "processed_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    result_bytes = canonical_json(result).encode("utf-8")
    result_sha256 = hashlib.sha256(result_bytes).hexdigest()
    processed_root = processed_directory.resolve()
    staging_path = None
    destination_directory = None
    destination_path = None
    destination_created = False
    try:
        staging_directory = processed_root / ".staging"
        staging_directory.mkdir(parents=True, exist_ok=True)
        staging_path = staging_directory / f"{result['processing_id']}.json"
        with staging_path.open("xb") as staged:
            staged.write(result_bytes)
        destination_directory = processed_root / metadata["file_id"] / result["processing_id"]
        destination_path = destination_directory / "transport-result.json"
        processing_run = {
            "processing_id": result["processing_id"],
            "file_id": metadata["file_id"],
            "model_name": MODEL_NAME,
            "model_version": MODEL_VERSION,
            "parameters_json": parameters_json,
            "source_sha256": source_sha256,
            "result_path": str(destination_path),
            "result_sha256": result_sha256,
            "processed_at": result["processed_at"],
            "summary_json": canonical_json(result["summary"]),
        }
        with database.connect_database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = database.find_processing_run(
                connection,
                metadata["file_id"],
                source_sha256,
                MODEL_NAME,
                MODEL_VERSION,
                parameters_json,
            )
            if existing is not None:
                return load_transport_result(existing, processed_root)
            destination_directory.mkdir(parents=True, exist_ok=False)
            destination_created = True
            os.replace(staging_path, destination_path)
            database.insert_processing_run(connection, processing_run)
        result["result_sha256"] = result_sha256
        return result
    except TransportError:
        raise
    except (OSError, sqlite3.Error, ET.ParseError, zlib.error) as error:
        if destination_created and destination_path is not None:
            destination_path.unlink(missing_ok=True)
            try:
                destination_directory.rmdir()
            except OSError:
                pass
        raise TransportError(500, "transport_processing_failed", str(error)) from error
    finally:
        if staging_path is not None:
            staging_path.unlink(missing_ok=True)
