import argparse
import hashlib
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import integrity
from storage import DATABASE_PATH, PROCESSED_DATA_DIR, RAW_DATA_DIR, REFERENCE_DATA_DIR


PATH_COLUMNS = (
    ("imported_files", "file_id", "storage_path", "raw"),
    ("file_attachments", "attachment_id", "storage_path", "raw"),
    ("processing_runs", "processing_id", "result_path", "processed"),
    ("reference_sources", "reference_id", "storage_path", "references"),
)
PATH_MIGRATION_TRIGGERS = (
    "reject_processing_run_update",
    "protect_processed_import_identity",
)


def sha256_file(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            checksum.update(chunk)
    return checksum.hexdigest()


def _rebased_path(path_value: str, source_root: Path, target_root: Path) -> Path:
    source_path = Path(path_value).resolve()
    try:
        relative_path = source_path.relative_to(source_root.resolve())
    except ValueError as error:
        raise integrity.IntegrityError(
            f"Catalog path is outside its current managed root: {source_path}"
        ) from error
    return target_root / relative_path


def _verify_copied_records(
    source_report: dict,
    source_roots: dict[str, Path],
    target_roots: dict[str, Path],
) -> int:
    verified_count = 0
    category_roots = {
        "raw": "raw",
        "attachments": "raw",
        "processed": "processed",
        "references": "references",
    }
    for category, records in source_report["files"].items():
        root_name = category_roots[category]
        source_root = source_roots[root_name]
        target_root = target_roots[root_name]
        for record in records:
            copied_path = target_root / record["path"].relative_to(source_root)
            if (
                not copied_path.is_file()
                or copied_path.stat().st_size != record["size_bytes"]
                or sha256_file(copied_path) != record["sha256"]
            ):
                raise integrity.IntegrityError(
                    f"Copied file verification failed for {record['label']}."
                )
            verified_count += 1
    return verified_count


def migrate_shared_storage(
    destination_root: Path,
    database_path: Path = DATABASE_PATH,
    raw_data_dir: Path = RAW_DATA_DIR,
    processed_data_dir: Path = PROCESSED_DATA_DIR,
    reference_data_dir: Path = REFERENCE_DATA_DIR,
    reuse_verified_destination: bool = False,
) -> dict:
    destination_root = destination_root.resolve()
    database_path = database_path.resolve()
    source_roots = {
        "raw": raw_data_dir.resolve(),
        "processed": processed_data_dir.resolve(),
        "references": reference_data_dir.resolve(),
    }
    destination_roots = {
        name: destination_root / name for name in source_roots
    }

    destination_exists = destination_root.exists()
    if destination_exists and not reuse_verified_destination:
        raise FileExistsError(f"Destination already exists: {destination_root}")
    for source_root in source_roots.values():
        if destination_root == source_root or source_root in destination_root.parents:
            raise ValueError("Destination must be outside the current managed data folders.")
    if not database_path.is_file():
        raise FileNotFoundError(f"Database not found: {database_path}")

    source_uri = f"{database_path.as_uri()}?mode=ro"
    with closing(sqlite3.connect(source_uri, uri=True)) as source_connection:
        source_connection.row_factory = sqlite3.Row
        source_report = integrity.verify_catalog(
            source_connection,
            source_roots["raw"],
            source_roots["processed"],
            source_roots["references"],
        )

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    database_backup = database_path.with_name(
        f"{database_path.stem}.pre-shared-{timestamp}{database_path.suffix}"
    )
    with closing(sqlite3.connect(source_uri, uri=True)) as source_connection:
        with closing(sqlite3.connect(database_backup)) as backup_connection:
            source_connection.backup(backup_connection)

    if destination_exists:
        verified_count = _verify_copied_records(
            source_report,
            source_roots,
            destination_roots,
        )
    else:
        staging_root = destination_root.parent / (
            f".{destination_root.name}.partial-{uuid4().hex}"
        )
        staging_roots = {name: staging_root / name for name in source_roots}
        staging_root.mkdir(parents=True, exist_ok=False)
        try:
            for name, source_root in source_roots.items():
                if source_root.is_dir():
                    shutil.copytree(
                        source_root,
                        staging_roots[name],
                        ignore=shutil.ignore_patterns(".staging"),
                    )
                else:
                    staging_roots[name].mkdir()
            verified_count = _verify_copied_records(
                source_report,
                source_roots,
                staging_roots,
            )
            staging_root.rename(destination_root)
        except Exception:
            if staging_root.exists():
                shutil.rmtree(staging_root)
            raise

    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        with connection:
            trigger_rows = connection.execute(
                """
                SELECT name, sql FROM sqlite_master
                WHERE type = 'trigger' AND name IN (?, ?)
                """,
                PATH_MIGRATION_TRIGGERS,
            ).fetchall()
            if len(trigger_rows) != len(PATH_MIGRATION_TRIGGERS):
                raise integrity.IntegrityError(
                    "Expected path-protection triggers are missing from the catalog."
                )
            for trigger in trigger_rows:
                connection.execute(f"DROP TRIGGER {trigger['name']}")
            for table, id_column, path_column, root_name in PATH_COLUMNS:
                rows = connection.execute(
                    f"SELECT {id_column}, {path_column} FROM {table}"
                ).fetchall()
                for row in rows:
                    rebased = _rebased_path(
                        row[path_column],
                        source_roots[root_name],
                        destination_roots[root_name],
                    )
                    connection.execute(
                        f"UPDATE {table} SET {path_column} = ? WHERE {id_column} = ?",
                        (str(rebased), row[id_column]),
                    )
            for trigger in trigger_rows:
                connection.execute(trigger["sql"])
            destination_report = integrity.verify_catalog(
                connection,
                destination_roots["raw"],
                destination_roots["processed"],
                destination_roots["references"],
            )

    return {
        "destination": destination_root,
        "database_backup": database_backup,
        "verified_catalog_files": verified_count,
        "counts": destination_report["counts"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copy MDC managed files to shared storage and rebase catalog paths.",
    )
    parser.add_argument("destination", type=Path)
    parser.add_argument(
        "--reuse-verified-destination",
        action="store_true",
        help="Reuse an existing destination only after re-verifying every cataloged file.",
    )
    arguments = parser.parse_args()
    result = migrate_shared_storage(
        arguments.destination,
        reuse_verified_destination=arguments.reuse_verified_destination,
    )
    print(f"Shared storage created at: {result['destination']}")
    print(f"Local database backup: {result['database_backup']}")
    print(f"Verified cataloged files: {result['verified_catalog_files']}")


if __name__ == "__main__":
    main()
