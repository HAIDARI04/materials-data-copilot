import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class IntegrityError(ValueError):
    """Raised when retained data no longer matches its recorded provenance."""


JSON_COLUMNS = (
    ("processing_runs", "parameters_json"),
    ("processing_runs", "summary_json"),
    ("reference_sources", "evidence_json"),
    ("training_examples", "evidence_json"),
    ("copilot_messages", "metadata_json"),
    ("clarification_responses", "question_json"),
    ("clarification_responses", "response_json"),
    ("import_metadata_revisions", "previous_json"),
    ("import_metadata_revisions", "updated_json"),
    ("analysis_recipes", "config_json"),
    ("instrument_presets", "config_json"),
    ("processing_run_tombstones", "record_json"),
)


def sha256_file(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            checksum.update(chunk)
    return checksum.hexdigest()


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone() is not None


def database_integrity_report(connection: sqlite3.Connection) -> dict:
    quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
    foreign_key_issues = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    invalid_json_records = 0
    for table_name, column_name in JSON_COLUMNS:
        if not _table_exists(connection, table_name):
            continue
        invalid_json_records += connection.execute(
            f"SELECT COUNT(*) FROM {table_name} "
            f"WHERE json_valid({column_name}) = 0"
        ).fetchone()[0]

    provenance_mismatches = 0
    if _table_exists(connection, "processing_runs"):
        provenance_mismatches = connection.execute(
            """
            SELECT COUNT(*)
            FROM processing_runs
            LEFT JOIN imported_files USING (file_id)
            WHERE imported_files.file_id IS NULL
               OR processing_runs.source_sha256 <> imported_files.sha256
            """
        ).fetchone()[0]

    return {
        "quick_check": quick_check,
        "foreign_key_issue_count": foreign_key_issues,
        "invalid_json_record_count": invalid_json_records,
        "provenance_mismatch_count": provenance_mismatches,
        "valid": (
            quick_check == "ok"
            and foreign_key_issues == 0
            and invalid_json_records == 0
            and provenance_mismatches == 0
        ),
    }


def _resolve_managed_path(path_value: str, root: Path, label: str) -> Path:
    stored_path = Path(path_value)
    if not stored_path.is_absolute():
        raise IntegrityError(f"{label} path is not absolute.")
    resolved = stored_path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise IntegrityError(f"{label} path is outside managed storage.") from error
    return resolved


def verify_file_record(
    path_value: str,
    root: Path,
    expected_sha256: str,
    label: str,
    expected_size: int | None = None,
) -> dict:
    path = _resolve_managed_path(path_value, root, label)
    if not path.is_file():
        raise IntegrityError(f"{label} is missing from managed storage.")
    actual_size = path.stat().st_size
    actual_sha256 = sha256_file(path)
    if expected_size is not None and actual_size != expected_size:
        raise IntegrityError(f"{label} byte count does not match its record.")
    if actual_sha256.lower() != expected_sha256.lower():
        raise IntegrityError(f"{label} checksum does not match its record.")
    return {
        "label": label,
        "path": path,
        "size_bytes": actual_size,
        "sha256": actual_sha256,
    }


def verify_catalog(
    connection: sqlite3.Connection,
    raw_data_dir: Path,
    processed_data_dir: Path,
    reference_data_dir: Path,
) -> dict:
    report = database_integrity_report(connection)
    if not report["valid"]:
        raise IntegrityError("Database integrity or provenance verification failed.")

    verified = {
        "raw": [],
        "attachments": [],
        "processed": [],
        "references": [],
    }
    for row in connection.execute(
        """
        SELECT file_id, original_filename, size_bytes, sha256, storage_path
        FROM imported_files ORDER BY file_id
        """
    ):
        verified["raw"].append(
            verify_file_record(
                row["storage_path"],
                raw_data_dir,
                row["sha256"],
                f"import {row['file_id']} ({row['original_filename']})",
                row["size_bytes"],
            )
        )
    for row in connection.execute(
        """
        SELECT attachment_id, original_filename, size_bytes, sha256, storage_path
        FROM file_attachments ORDER BY attachment_id
        """
    ):
        verified["attachments"].append(
            verify_file_record(
                row["storage_path"],
                raw_data_dir,
                row["sha256"],
                f"attachment {row['attachment_id']} ({row['original_filename']})",
                row["size_bytes"],
            )
        )
    for row in connection.execute(
        """
        SELECT processing_id, result_path, result_sha256
        FROM processing_runs ORDER BY processing_id
        """
    ):
        verified["processed"].append(
            verify_file_record(
                row["result_path"],
                processed_data_dir,
                row["result_sha256"],
                f"processing run {row['processing_id']}",
            )
        )
    for row in connection.execute(
        """
        SELECT reference_id, original_filename, size_bytes, sha256, storage_path
        FROM reference_sources ORDER BY reference_id
        """
    ):
        verified["references"].append(
            verify_file_record(
                row["storage_path"],
                reference_data_dir,
                row["sha256"],
                f"reference {row['reference_id']} ({row['original_filename']})",
                row["size_bytes"],
            )
        )
    return {
        "database": report,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "counts": {key: len(value) for key, value in verified.items()},
        "files": verified,
    }


def public_catalog_report(report: dict) -> dict:
    return {
        "database": report["database"],
        "verified_at": report["verified_at"],
        "counts": report["counts"],
    }


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
