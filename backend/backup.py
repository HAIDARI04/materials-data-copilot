import argparse
import hashlib
import json
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from database import DATABASE_PATH
import integrity

RAW_DATA_DIR = Path(__file__).parent / "data" / "raw"
PROCESSED_DATA_DIR = Path(__file__).parent / "data" / "processed"
REFERENCE_DATA_DIR = Path(__file__).parent / "data" / "references"


def sha256_file(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            checksum.update(chunk)
    return checksum.hexdigest()


def create_backup(
    destination_root: Path,
    database_path: Path = DATABASE_PATH,
    raw_data_dir: Path = RAW_DATA_DIR,
    processed_data_dir: Path = PROCESSED_DATA_DIR,
    reference_data_dir: Path = REFERENCE_DATA_DIR,
) -> Path:
    database_path = database_path.resolve()
    raw_data_dir = raw_data_dir.resolve()
    processed_data_dir = processed_data_dir.resolve()
    reference_data_dir = reference_data_dir.resolve()
    destination_root = destination_root.resolve()

    if not database_path.is_file():
        raise FileNotFoundError(f"Database not found: {database_path}")

    data_directory = database_path.parent.resolve()
    if destination_root == data_directory or data_directory in destination_root.parents:
        raise ValueError("Choose a backup destination outside backend/data.")

    source_uri = f"{database_path.as_uri()}?mode=ro"
    with closing(sqlite3.connect(source_uri, uri=True)) as source_connection:
        source_connection.row_factory = sqlite3.Row
        catalog_report = integrity.verify_catalog(
            source_connection,
            raw_data_dir,
            processed_data_dir,
            reference_data_dir,
        )

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_name = f"materials-data-copilot-{timestamp}"
    backup_directory = destination_root / backup_name
    partial_backup_directory = destination_root / f".{backup_name}.partial"
    partial_backup_directory.mkdir(parents=True, exist_ok=False)

    try:
        backup_database = partial_backup_directory / database_path.name
        with closing(sqlite3.connect(source_uri, uri=True)) as source_connection:
            with closing(sqlite3.connect(backup_database)) as backup_connection:
                source_connection.backup(backup_connection)
        with closing(sqlite3.connect(backup_database)) as backup_connection:
            backup_integrity = backup_connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0]
        if backup_integrity != "ok":
            raise integrity.IntegrityError(
                f"Backup database integrity check failed: {backup_integrity}"
            )

        backup_raw_directory = partial_backup_directory / "raw"
        if raw_data_dir.is_dir():
            shutil.copytree(
                raw_data_dir,
                backup_raw_directory,
                ignore=shutil.ignore_patterns(".staging"),
            )
        else:
            backup_raw_directory.mkdir()

        backup_processed_directory = partial_backup_directory / "processed"
        if processed_data_dir.is_dir():
            shutil.copytree(
                processed_data_dir,
                backup_processed_directory,
                ignore=shutil.ignore_patterns(".staging"),
            )
        else:
            backup_processed_directory.mkdir()

        backup_reference_directory = partial_backup_directory / "references"
        if reference_data_dir.is_dir():
            shutil.copytree(
                reference_data_dir,
                backup_reference_directory,
                ignore=shutil.ignore_patterns(".staging"),
            )
        else:
            backup_reference_directory.mkdir()

        raw_files = []
        for path in sorted(backup_raw_directory.rglob("*")):
            if path.is_file():
                raw_files.append(
                    {
                        "path": path.relative_to(partial_backup_directory).as_posix(),
                        "size_bytes": path.stat().st_size,
                        "sha256": sha256_file(path),
                    }
                )

        processed_files = []
        for path in sorted(backup_processed_directory.rglob("*")):
            if path.is_file():
                processed_files.append(
                    {
                        "path": path.relative_to(partial_backup_directory).as_posix(),
                        "size_bytes": path.stat().st_size,
                        "sha256": sha256_file(path),
                    }
                )

        reference_files = []
        for path in sorted(backup_reference_directory.rglob("*")):
            if path.is_file():
                reference_files.append(
                    {
                        "path": path.relative_to(partial_backup_directory).as_posix(),
                        "size_bytes": path.stat().st_size,
                        "sha256": sha256_file(path),
                    }
                )

        storage_roots = {
            "raw": (raw_data_dir, backup_raw_directory),
            "attachments": (raw_data_dir, backup_raw_directory),
            "processed": (processed_data_dir, backup_processed_directory),
            "references": (reference_data_dir, backup_reference_directory),
        }
        copied_catalog_count = 0
        for category, records in catalog_report["files"].items():
            source_root, copied_root = storage_roots[category]
            for record in records:
                relative_path = record["path"].relative_to(source_root)
                copied_path = copied_root / relative_path
                if (
                    not copied_path.is_file()
                    or copied_path.stat().st_size != record["size_bytes"]
                    or sha256_file(copied_path) != record["sha256"]
                ):
                    raise integrity.IntegrityError(
                        f"Backup copy verification failed for {record['label']}."
                    )
                copied_catalog_count += 1

        manifest = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "database": backup_database.name,
            "database_sha256": sha256_file(backup_database),
            "database_integrity": backup_integrity,
            "source_catalog_verification": integrity.public_catalog_report(
                catalog_report
            ),
            "catalog_file_count": copied_catalog_count,
            "raw_file_count": len(raw_files),
            "raw_files": raw_files,
            "processed_file_count": len(processed_files),
            "processed_files": processed_files,
            "reference_file_count": len(reference_files),
            "reference_files": reference_files,
        }
        (partial_backup_directory / "manifest.json").write_text(
            json.dumps(manifest, indent=2),
            encoding="utf-8",
        )
        partial_backup_directory.rename(backup_directory)
        return backup_directory
    except Exception:
        if partial_backup_directory.exists():
            shutil.rmtree(partial_backup_directory)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a local SQLite and raw-file backup.",
    )
    parser.add_argument(
        "destination",
        type=Path,
        help="Directory in which a timestamped backup will be created.",
    )
    arguments = parser.parse_args()
    backup_directory = create_backup(arguments.destination)
    print(f"Backup created at: {backup_directory}")


if __name__ == "__main__":
    main()
