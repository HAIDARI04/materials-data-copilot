import hashlib
import math
import re
import tempfile
from datetime import date
from pathlib import Path

import xlrd
import transport
import wdf_reader

MAX_PREVIEW_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MiB
MAX_WDF_PREVIEW_FILE_SIZE_BYTES = 100 * 1024 * 1024  # 100 MiB
MAX_PREVIEW_POINTS = 2000
SUPPORTED_SUFFIXES = {".csv", ".dat", ".tsv", ".txt", ".wdf", ".xy"}
RAMAN_HEADER_LIMIT = 200


class PreviewError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def resolve_stored_file(metadata: dict, raw_data_directory: Path) -> Path:
    raw_root = raw_data_directory.resolve()
    expected_directory = (raw_root / metadata["file_id"]).resolve()
    stored_path = Path(metadata["storage_path"])

    if not stored_path.is_absolute():
        raise PreviewError(
            409,
            "invalid_storage_path",
            "The stored file path is not an absolute managed path.",
        )

    resolved_path = stored_path.resolve()
    try:
        resolved_path.relative_to(expected_directory)
    except ValueError as error:
        raise PreviewError(
            409,
            "invalid_storage_path",
            "The stored file path is outside its managed import directory.",
        ) from error

    if not resolved_path.is_file():
        raise PreviewError(
            409,
            "stored_file_missing",
            "The metadata record does not have a corresponding stored file.",
        )
    return resolved_path


def split_fields(line: str) -> list[str]:
    for delimiter in (",", "\t", ";"):
        if delimiter in line:
            return [field.strip() for field in line.split(delimiter)]
    return line.split()


def numeric_pair(fields: list[str]) -> tuple[float, float] | None:
    if len(fields) < 2:
        return None
    try:
        x_value = float(fields[0])
        y_value = float(fields[1])
    except ValueError:
        return None
    if not math.isfinite(x_value) or not math.isfinite(y_value):
        return None
    return x_value, y_value


def axis_labels(
    headers: dict[str, str],
    column_labels: tuple[str, str] | None,
) -> tuple[str, str]:
    if column_labels is not None:
        return column_labels

    data_type = headers.get("TYPE", "").strip()
    if data_type.lower() == "ramanshift":
        x_label = "Raman shift"
    elif data_type:
        x_label = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", data_type)
    else:
        x_label = "X"

    x_units = headers.get("XUNITS", "").strip()
    if x_units:
        x_label = f"{x_label} ({x_units})"
    return x_label, "Intensity"


def downsample_points(
    points: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    if len(points) <= MAX_PREVIEW_POINTS:
        return points
    step = (len(points) - 1) / (MAX_PREVIEW_POINTS - 1)
    return [points[round(index * step)] for index in range(MAX_PREVIEW_POINTS)]


def extract_numeric_series(text: str) -> dict:
    headers = {}
    column_labels = None
    points = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "//")):
            continue

        if "=" in line and not points:
            key, value = line.split("=", 1)
            if key.strip():
                headers[key.strip().upper()] = value.strip()
                continue

        fields = split_fields(line)
        pair = numeric_pair(fields)
        if pair is not None:
            points.append(pair)
        elif not points and len(fields) >= 2:
            column_labels = (fields[0], fields[1])

    if len(points) < 2:
        raise PreviewError(
            422,
            "no_numeric_series",
            "No two-column numeric series was found in this file.",
        )

    x_label, y_label = axis_labels(headers, column_labels)
    return {
        "x_label": x_label,
        "y_label": y_label,
        "headers": headers,
        "points": points,
    }


def parse_numeric_text(text: str) -> dict:
    series = extract_numeric_series(text)
    displayed_points = downsample_points(series["points"])
    return {
        "x_label": series["x_label"],
        "y_label": series["y_label"],
        "point_count": len(series["points"]),
        "displayed_point_count": len(displayed_points),
        "points": displayed_points,
    }


def read_verified_text(
    metadata: dict,
    raw_data_directory: Path,
    dataset_index: int = 0,
) -> tuple[str, str]:
    stored_path = resolve_stored_file(metadata, raw_data_directory)
    if stored_path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise PreviewError(
            415,
            "unsupported_preview_format",
            "Plot previews support CSV, TSV, DAT, TXT, WDF, and XY files.",
        )

    size_bytes = stored_path.stat().st_size
    maximum_preview_size = (
        MAX_WDF_PREVIEW_FILE_SIZE_BYTES
        if stored_path.suffix.lower() == ".wdf"
        else MAX_PREVIEW_FILE_SIZE_BYTES
    )
    if size_bytes > maximum_preview_size:
        raise PreviewError(
            413,
            "preview_too_large",
            "This file is too large for an interactive plot preview.",
        )

    raw_bytes = stored_path.read_bytes()
    actual_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    if actual_sha256.lower() != metadata["sha256"].lower():
        raise PreviewError(
            409,
            "checksum_mismatch",
            "The stored file checksum does not match its metadata record.",
        )

    if stored_path.suffix.lower() == ".wdf":
        try:
            parsed_wdf = wdf_reader.parse_wdf(raw_bytes, include_points=True)
        except wdf_reader.WdfError as error:
            raise PreviewError(error.status_code, error.code, error.message) from error
        datasets = parsed_wdf["datasets"]
        if dataset_index < 0 or dataset_index >= len(datasets):
            raise PreviewError(
                422,
                "wdf_dataset_not_found",
                "The selected spectrum does not exist in this WDF file.",
            )
        dataset = datasets[dataset_index]
        lines = [
            f"FILETYPE={parsed_wdf['technique']}",
            f"XUNITS={parsed_wdf['x_axis']['unit'] or ''}",
            f"{dataset['x_label']},{dataset['y_label']}",
        ]
        lines.extend(f"{x_value:.12g},{y_value:.12g}" for x_value, y_value in dataset["points"])
        return "\n".join(lines), actual_sha256

    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise PreviewError(
            415,
            "unsupported_text_encoding",
            "The file is not UTF-8 text and cannot be plotted safely.",
        ) from error

    return text, actual_sha256


def verify_stored_file(metadata: dict, raw_data_directory: Path) -> tuple[Path, str]:
    stored_path = resolve_stored_file(metadata, raw_data_directory)
    actual_size = stored_path.stat().st_size
    checksum = hashlib.sha256()
    with stored_path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            checksum.update(chunk)
    actual_sha256 = checksum.hexdigest()
    if (
        actual_size != metadata["size_bytes"]
        or actual_sha256.lower() != metadata["sha256"].lower()
    ):
        raise PreviewError(
            409,
            "checksum_mismatch",
            "The stored file no longer matches its immutable import record.",
        )
    return stored_path, actual_sha256


def sample_id_from_path(
    filename: str,
    relative_path: str | None = None,
) -> str | None:
    path_hint = (relative_path or filename).replace("\\", "/")
    path_parts = [part for part in path_hint.split("/") if part]
    if not path_parts:
        return None

    sample_pattern = re.compile(
        r"^(?P<sample>[A-Za-z]\d+)(?=$|[_-])",
        flags=re.IGNORECASE,
    )
    token_pattern = re.compile(
        r"(?<![A-Za-z0-9])(?P<sample>[A-Za-z]\d+)(?=$|[_\-.])",
        flags=re.IGNORECASE,
    )

    # A device-like folder at any level owns all files below it.
    for part in reversed(path_parts[:-1]):
        match = sample_pattern.match(part)
        if match:
            return match.group("sample").upper()

    # Generic folders can contain the sample identifier in the filename.
    match = token_pattern.search(path_parts[-1])
    if match:
        return match.group("sample").upper()

    # If the filename is generic, use the nearest parent folder containing it.
    for part in reversed(path_parts[:-1]):
        match = token_pattern.search(part)
        if match:
            return match.group("sample").upper()
    return None


def measurement_date_from_metadata(
    filename: str,
    relative_path: str | None = None,
    text: str | None = None,
) -> str | None:
    metadata_text = " ".join(
        part for part in (relative_path or "", filename, text or "") if part
    )
    labeled_date = re.search(
        r"(?:date|datetime|acquired|acquisition|measured|measurement|created|executed)"
        r"\s*[:=]\s*['\"]?"
        r"(?P<date>\d{4}[-/.]\d{1,2}[-/.]\d{1,2})",
        metadata_text,
        flags=re.IGNORECASE,
    )
    date_match = labeled_date or re.search(
        r"(?<!\d)(?P<date>\d{4}[-/.]\d{1,2}[-/.]\d{1,2})(?!\d)",
        metadata_text,
    )
    if date_match:
        year, month, day = re.split(r"[-/.]", date_match.group("date"))
        try:
            return date(int(year), int(month), int(day)).isoformat()
        except ValueError:
            return None

    slash_date = re.search(
        r"(?<!\d)(?P<month>\d{1,2})/(?P<day>\d{1,2})/(?P<year>\d{4})(?!\d)",
        metadata_text,
    )
    if slash_date:
        try:
            return date(
                int(slash_date.group("year")),
                int(slash_date.group("month")),
                int(slash_date.group("day")),
            ).isoformat()
        except ValueError:
            return None
    return None


def extract_metadata_suggestions(
    filename: str,
    content: bytes,
    relative_path: str | None = None,
) -> dict[str, str]:
    path_hint = (relative_path or filename).replace("\\", "/")
    suggestions: dict[str, str] = {}
    sample_id = sample_id_from_path(filename, relative_path)
    if sample_id:
        suggestions["sample_id"] = sample_id
    path_date = measurement_date_from_metadata(filename, relative_path)
    if path_date:
        suggestions["measurement_date"] = path_date
    path_parts = [part for part in path_hint.split("/") if part]
    if len(path_parts) >= 3:
        suggestions["project"] = path_parts[-2][:200]
    if re.search(r"(?:^|[/_\-])raman(?:$|[/_\-.])", path_hint, re.IGNORECASE):
        suggestions["technique"] = "Raman spectroscopy"
    if Path(filename).suffix.casefold() in {".xls", ".xlsx"}:
        workbook_path = None
        try:
            if Path(filename).suffix.casefold() == ".xls":
                book = xlrd.open_workbook(file_contents=content, on_demand=True)
                sheets = [
                    {
                        "name": sheet.name,
                        "rows": [
                            [cell.value for cell in row]
                            for row in sheet.get_rows()
                        ],
                    }
                    for sheet in book.sheets()
                ]
                book.release_resources()
            else:
                with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as temporary:
                    temporary.write(content)
                    workbook_path = Path(temporary.name)
                sheets, _warnings = transport.read_workbook(workbook_path)
            settings = next(
                (
                    sheet
                    for sheet in sheets
                    if sheet["name"].casefold() == "settings"
                ),
                None,
            )
            if settings is not None:
                values: dict[str, list[str]] = {}
                for row in settings["rows"]:
                    cells = [str(value).strip() for value in row]
                    if not cells or not cells[0]:
                        continue
                    values[cells[0].casefold()] = [
                        value for value in cells[1:] if value and value.casefold() != "n/a"
                    ]
                last_executed = next(iter(values.get("last executed", [])), "")
                date_match = re.search(
                    r"(\d{1,2})/(\d{1,2})/(\d{4})",
                    last_executed,
                )
                if date_match:
                    month, day, year = date_match.groups()
                    suggestions["measurement_date"] = (
                        f"{year}-{int(month):02d}-{int(day):02d}"
                    )
                instruments = []
                for instrument in values.get("instrument", []):
                    if instrument not in instruments:
                        instruments.append(instrument)
                if instruments:
                    suggestions["instrument"] = ", ".join(instruments)[:200]
                if values.get("mode"):
                    suggestions["measurement_role"] = "sample_on_substrate"
                suggestions["technique"] = "Transport"
        except (OSError, ValueError, xlrd.XLRDError, transport.TransportError):
            pass
        finally:
            if workbook_path is not None:
                workbook_path.unlink(missing_ok=True)
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return suggestions

    headers: dict[str, str] = {}
    for line in text.splitlines()[:RAMAN_HEADER_LIMIT]:
        if line.strip().upper() == "XYDATA=":
            break
        key, separator, value = line.partition("=")
        if separator and key.strip() and value.strip():
            headers[key.strip().upper()] = value.strip()

    file_type = headers.get("FILETYPE", "").upper()
    if "RAMAN" in file_type or headers.get("TYPE", "").casefold() == "ramanshift":
        suggestions["technique"] = "Raman spectroscopy"
    timestamp = headers.get("DATETIME", "")
    text_date = measurement_date_from_metadata(
        filename,
        relative_path,
        " ".join(
            f"{key}={value}" for key, value in headers.items()
        ),
    )
    if text_date:
        suggestions["measurement_date"] = text_date
    instrument = headers.get("INSTRUMENT") or headers.get("INSTRUMENTNAME")
    if instrument:
        suggestions["instrument"] = instrument[:200]
    operator = headers.get("OPERATOR")
    if operator:
        suggestions["operator"] = operator[:100]
    material = headers.get("MATERIAL") or headers.get("MATERIALSYSTEM")
    if material:
        suggestions["material_system"] = material[:200]
    return suggestions


def measured_spectral_bounds(path: Path) -> dict | None:
    count = 0
    minimum = None
    maximum = None
    previous = None
    direction = 0
    monotonic = True
    in_xy_data = False
    found_xy_marker = False
    with path.open("r", encoding="utf-8-sig", errors="replace") as source:
        for raw_line in source:
            line = raw_line.strip()
            if not in_xy_data and line.upper() == "XYDATA=":
                found_xy_marker = True
                in_xy_data = True
                continue
            fields = split_fields(line)
            pair = numeric_pair(fields) if len(fields) == 2 else None
            if pair is None or (found_xy_marker and not in_xy_data):
                continue
            x_value, _y_value = pair
            if previous is not None:
                step_direction = 1 if x_value > previous else -1 if x_value < previous else 0
                if step_direction == 0 or (direction and step_direction != direction):
                    monotonic = False
                if not direction:
                    direction = step_direction
            previous = x_value
            minimum = x_value if minimum is None else min(minimum, x_value)
            maximum = x_value if maximum is None else max(maximum, x_value)
            count += 1
    if count < 2 or not monotonic:
        return None
    return {"minimum": minimum, "maximum": maximum, "point_count": count}


def inspect_upload(
    filename: str,
    content: bytes,
    size_bytes: int,
    relative_path: str | None = None,
) -> dict:
    suffix = Path(filename).suffix.lower()
    result = {
        "filename": filename,
        "size_bytes": size_bytes,
        "suffix": suffix,
        "suggested_metadata": extract_metadata_suggestions(
            filename,
            content,
            relative_path,
        ),
    }
    if suffix == ".wdf":
        result["detected_format"] = "Renishaw WDF"
        return result
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        result["detected_format"] = "binary"
        return result
    try:
        series = extract_numeric_series(text)
    except PreviewError:
        result["detected_format"] = "text"
    else:
        result.update(
            {
                "detected_format": "numeric_series",
                "x_label": series["x_label"],
                "y_label": series["y_label"],
                "preview_point_count": len(series["points"]),
            }
        )
    return result


def _numeric_matrix_row(line: str) -> list[float] | None:
    fields = split_fields(line.strip())
    if len(fields) < 2:
        return None
    try:
        values = [float(field) for field in fields]
    except ValueError:
        return None
    if not all(math.isfinite(value) for value in values):
        raise PreviewError(
            422,
            "mapping_non_finite",
            "Mapping matrices may contain only finite numeric values.",
        )
    return values


def build_mapping_preview(metadata: dict, raw_data_directory: Path) -> dict:
    stored_path, actual_sha256 = verify_stored_file(metadata, raw_data_directory)
    row_count = 0
    column_count = None
    minimum = None
    maximum = None
    with stored_path.open("r", encoding="utf-8-sig", errors="replace") as source:
        for line in source:
            row = _numeric_matrix_row(line)
            if row is None:
                continue
            if column_count is None:
                column_count = len(row)
            elif len(row) != column_count:
                raise PreviewError(
                    422,
                    "mapping_shape_invalid",
                    "Mapping rows must contain the same number of values.",
                )
            row_count += 1
            row_minimum = min(row)
            row_maximum = max(row)
            minimum = row_minimum if minimum is None else min(minimum, row_minimum)
            maximum = row_maximum if maximum is None else max(maximum, row_maximum)
    if row_count < 2 or column_count is None:
        raise PreviewError(
            422,
            "mapping_unavailable",
            "No rectangular numeric mapping matrix was found.",
        )

    row_step = max(1, math.ceil(row_count / 300))
    column_step = max(1, math.ceil(column_count / 300))
    sampled = []
    numeric_index = 0
    with stored_path.open("r", encoding="utf-8-sig", errors="replace") as source:
        for line in source:
            row = _numeric_matrix_row(line)
            if row is None:
                continue
            if numeric_index % row_step == 0 and len(sampled) < 300:
                sampled.append(row[::column_step][:300])
            numeric_index += 1
    return {
        "file_id": metadata["file_id"],
        "filename": metadata["original_filename"],
        "sha256": actual_sha256,
        "rows": row_count,
        "columns": column_count,
        "minimum": minimum,
        "maximum": maximum,
        "preview_rows": len(sampled),
        "coordinate_system": "matrix_indices",
        "physical_coordinates_known": False,
        "row_sampling_step": row_step,
        "column_sampling_step": column_step,
        "preview_columns": len(sampled[0]) if sampled else 0,
        "values": sampled,
    }


def build_file_preview(
    metadata: dict,
    raw_data_directory: Path,
    dataset_index: int = 0,
) -> dict:
    stored_path = resolve_stored_file(metadata, raw_data_directory)
    if stored_path.suffix.lower() == ".wdf":
        text, actual_sha256 = read_verified_text(
            metadata,
            raw_data_directory,
            dataset_index,
        )
        parsed_preview = parse_numeric_text(text)
        try:
            wdf = wdf_reader.read_wdf(stored_path, include_points=False)
        except wdf_reader.WdfError as error:
            raise PreviewError(error.status_code, error.code, error.message) from error
        selected_dataset = wdf["datasets"][dataset_index]
        return {
            "file_id": metadata["file_id"],
            "original_filename": metadata["original_filename"],
            "technique": wdf["technique"],
            "material_system": metadata.get("material_system") or "Unknown",
            "sample_id": metadata["sample_id"],
            "sha256": actual_sha256,
            "checksum_verified": True,
            "dataset_index": dataset_index,
            "dataset_id": selected_dataset["dataset_id"],
            "dataset_name": selected_dataset["name"],
            "available_datasets": wdf["datasets"],
            "wdf": {key: value for key, value in wdf.items() if key != "datasets"},
            **parsed_preview,
        }

    text, actual_sha256 = read_verified_text(metadata, raw_data_directory)
    parsed_preview = parse_numeric_text(text)
    return {
        "file_id": metadata["file_id"],
        "original_filename": metadata["original_filename"],
        "technique": metadata["technique"],
        "material_system": metadata.get("material_system") or "Unknown",
        "sample_id": metadata["sample_id"],
        "sha256": actual_sha256,
        "checksum_verified": True,
        **parsed_preview,
    }
