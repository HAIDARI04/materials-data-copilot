import csv
import hashlib
import io
import json
import re
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterator

import database
import preview
import processing


BUNDLE_FORMAT_VERSION = "1.0"
STREAM_CHUNK_SIZE = 1024 * 1024


class ExportError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def safe_bundle_name(imported_file: dict) -> str:
    preferred = imported_file.get("sample_id") or Path(
        imported_file["original_filename"]
    ).stem
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", preferred).strip("-.")
    return f"{safe or 'materials-data'}-reproducibility.zip"


def portable_metadata(imported_file: dict) -> dict:
    return {
        key: imported_file.get(key)
        for key in (
            "file_id",
            "original_filename",
            "content_type",
            "size_bytes",
            "sha256",
            "imported_at",
            "technique",
            "material_system",
            "sample_id",
            "measurement_date",
            "instrument",
            "operator",
            "notes",
            "substrate",
            "updated_at",
            "archived_at",
        )
    }


def plot_series_csv(result: dict) -> bytes:
    series = result.get("series", {})
    names = list(series)
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["raman_shift_cm-1", *names])
    maximum_length = max((len(points) for points in series.values()), default=0)
    for index in range(maximum_length):
        x_value = ""
        row = []
        for name in names:
            points = series[name]
            if index < len(points):
                if x_value == "":
                    x_value = points[index][0]
                row.append(points[index][1])
            else:
                row.append("")
        writer.writerow([x_value, *row])
    return output.getvalue().encode("utf-8")


def component_series_csv(result: dict) -> bytes | None:
    components = []
    for kind in ("deconvolution", "substrate_correction"):
        for index, component in enumerate(
            result.get(kind, {}).get("components", [])
        ):
            components.append(
                (
                    f"{kind}_{index + 1}_{component.get('center_cm-1', 'unknown')}",
                    component.get("series", []),
                )
            )
    if not components:
        return None
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["raman_shift_cm-1", *(name for name, _ in components)])
    maximum_length = max(len(points) for _, points in components)
    for index in range(maximum_length):
        x_value = ""
        row = []
        for _, points in components:
            if index < len(points):
                if x_value == "":
                    x_value = points[index][0]
                row.append(points[index][1])
            else:
                row.append("")
        writer.writerow([x_value, *row])
    return output.getvalue().encode("utf-8")


def peaks_csv(result: dict) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        [
            "position_cm-1",
            "intensity",
            "prominence",
            "assignments_json",
        ]
    )
    for peak in result.get("peaks", []):
        writer.writerow(
            [
                peak.get("position_cm-1"),
                peak.get("intensity"),
                peak.get("prominence"),
                json.dumps(
                    peak.get("assignments", []),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            ]
        )
    return output.getvalue().encode("utf-8")


def readme_text(raw_name: str) -> bytes:
    return f"""# Materials Data Copilot reproducibility package

This archive is a portable, checksum-audited export of one imported measurement
and its latest analysis. It does not modify the original experimental file.

## Contents

- `raw/{raw_name}`: exact imported bytes.
- `metadata/import.json`: portable import metadata (local storage paths omitted).
- `metadata/revisions.json`: traceable metadata edit history.
- `analysis/result.json`: complete derived result used by the UI.
- `analysis/processing-run.json`: model, parameters, timestamps, and checksums.
- `analysis/plot-series.csv`: every derived series displayed by the plot.
- `analysis/components.csv`: fitted component curves, when present.
- `analysis/peaks.csv`: detected peaks and assignment evidence.
- `analysis/clarifications.json` and `training.json`: user-confirmed evidence.
- `environment/requirements.txt`: pinned backend environment.
- `manifest.json`: SHA-256 and byte count for every payload file.

To reproduce a plot, open `analysis/plot-series.csv`, use
`raman_shift_cm-1` as X, and select the desired series as Y. Use the axis labels,
peak annotations, collective bands, and component definitions in `result.json`.
Verify a payload with `certutil -hashfile <path> SHA256` on Windows and compare it
with `manifest.json` before using the export for reporting.
""".encode("utf-8")


def add_bytes(
    archive: zipfile.ZipFile,
    manifest_files: list[dict],
    archive_path: str,
    contents: bytes,
) -> None:
    archive.writestr(archive_path, contents)
    manifest_files.append(
        {
            "path": archive_path,
            "size_bytes": len(contents),
            "sha256": hashlib.sha256(contents).hexdigest(),
        }
    )


def add_verified_file(
    archive: zipfile.ZipFile,
    archive_path: str,
    source_path: Path,
) -> tuple[int, str]:
    checksum = hashlib.sha256()
    size_bytes = 0
    with source_path.open("rb") as source, archive.open(
        archive_path,
        "w",
    ) as target:
        while chunk := source.read(STREAM_CHUNK_SIZE):
            target.write(chunk)
            checksum.update(chunk)
            size_bytes += len(chunk)
    return size_bytes, checksum.hexdigest()


def build_bundle(
    imported_file: dict,
    raw_data_directory: Path,
    processed_data_directory: Path,
    requirements_path: Path,
) -> tuple[BinaryIO, str]:
    processing_run = database.get_latest_processing_run(imported_file["file_id"])
    if processing_run is None:
        raise ExportError(
            409,
            "processing_required",
            "Process this measurement before exporting a reproducibility package.",
        )
    try:
        raw_path = preview.resolve_stored_file(imported_file, raw_data_directory)
        result = processing.load_processing_result(
            processing_run,
            processed_data_directory.resolve(),
        )
        result_path = processing.resolve_result_path(
            processing_run,
            processed_data_directory.resolve(),
        )
    except preview.PreviewError as error:
        raise ExportError(error.status_code, error.code, error.message) from error
    except processing.ProcessingError as error:
        raise ExportError(error.status_code, error.code, error.message) from error

    result_bytes = result_path.read_bytes()
    if hashlib.sha256(result_bytes).hexdigest().lower() != processing_run[
        "result_sha256"
    ].lower():
        raise ExportError(
            409,
            "processing_checksum_mismatch",
            "The derived result checksum changed while preparing the export.",
        )
    if (
        processing_run["source_sha256"].lower()
        != imported_file["sha256"].lower()
    ):
        raise ExportError(
            409,
            "processing_source_mismatch",
            "The latest analysis does not match the imported raw-file checksum.",
        )
    raw_archive_name = f"raw/{raw_path.name}"
    payloads = {
        "README.md": readme_text(raw_path.name),
        "metadata/import.json": canonical_json(portable_metadata(imported_file)),
        "metadata/revisions.json": canonical_json(
            database.list_import_metadata_revisions(imported_file["file_id"])
        ),
        "analysis/result.json": result_bytes,
        "analysis/processing-run.json": canonical_json(
            {
                "processing_id": processing_run["processing_id"],
                "file_id": processing_run["file_id"],
                "model_name": processing_run["model_name"],
                "model_version": processing_run["model_version"],
                "parameters": json.loads(processing_run["parameters_json"]),
                "source_sha256": processing_run["source_sha256"],
                "result_sha256": processing_run["result_sha256"],
                "processed_at": processing_run["processed_at"],
                "summary": json.loads(processing_run["summary_json"]),
            }
        ),
        "analysis/plot-series.csv": plot_series_csv(result),
        "analysis/peaks.csv": peaks_csv(result),
        "analysis/clarifications.json": canonical_json(
            database.list_clarification_responses(imported_file["file_id"])
        ),
        "analysis/training.json": canonical_json(
            [
                record
                for record in database.list_training_examples()
                if record["file_id"] == imported_file["file_id"]
            ]
        ),
        "environment/requirements.txt": requirements_path.read_bytes(),
    }
    components = component_series_csv(result)
    if components is not None:
        payloads["analysis/components.csv"] = components

    bundle = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
    manifest_files = []
    try:
        with zipfile.ZipFile(
            bundle,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        ) as archive:
            raw_size, raw_sha256 = add_verified_file(
                archive,
                raw_archive_name,
                raw_path,
            )
            if raw_size != imported_file["size_bytes"]:
                raise ExportError(
                    409,
                    "raw_size_mismatch",
                    "The stored raw-file size does not match its metadata record.",
                )
            if raw_sha256.lower() != imported_file["sha256"].lower():
                raise ExportError(
                    409,
                    "checksum_mismatch",
                    "The stored raw-file checksum does not match its metadata record.",
                )
            manifest_files.append(
                {
                    "path": raw_archive_name,
                    "size_bytes": raw_size,
                    "sha256": raw_sha256,
                }
            )
            for archive_path, contents in payloads.items():
                add_bytes(archive, manifest_files, archive_path, contents)
            manifest = {
                "bundle_format_version": BUNDLE_FORMAT_VERSION,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "application": "Materials Data Copilot",
                "file_id": imported_file["file_id"],
                "processing_id": processing_run["processing_id"],
                "files": sorted(manifest_files, key=lambda item: item["path"]),
            }
            archive.writestr("manifest.json", canonical_json(manifest))
    except Exception:
        bundle.close()
        raise
    bundle.seek(0)
    return bundle, safe_bundle_name(imported_file)


def stream_bundle(bundle: BinaryIO) -> Iterator[bytes]:
    try:
        while chunk := bundle.read(STREAM_CHUNK_SIZE):
            yield chunk
    finally:
        bundle.close()
