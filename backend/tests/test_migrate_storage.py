import hashlib
import sqlite3

import database
from migrate_storage import migrate_shared_storage


def test_migrate_shared_storage_copies_and_rebases_catalog(tmp_path, monkeypatch):
    database_path = tmp_path / "local" / "materials.db"
    raw_directory = tmp_path / "local" / "raw"
    processed_directory = tmp_path / "local" / "processed"
    reference_directory = tmp_path / "local" / "references"
    destination = tmp_path / "drive" / "MDC-Data"
    original = b"immutable measurement"
    raw_file = raw_directory / "file-id" / "measurement.bin"
    raw_file.parent.mkdir(parents=True)
    raw_file.write_bytes(original)

    monkeypatch.setattr(database, "DATABASE_PATH", database_path)
    database.initialize_database()
    with database.connect_database() as connection:
        database.insert_imported_file(
            connection,
            {
                "file_id": "file-id",
                "original_filename": "measurement.bin",
                "content_type": "application/octet-stream",
                "size_bytes": len(original),
                "sha256": hashlib.sha256(original).hexdigest(),
                "storage_path": str(raw_file),
                "imported_at": "2026-09-22T00:00:00+00:00",
                "technique": "Raman",
                "material_system": "Unknown",
                "sample_id": "SAMPLE-001",
                "measurement_date": None,
                "instrument": None,
                "operator": None,
                "notes": None,
            },
        )

    result = migrate_shared_storage(
        destination,
        database_path=database_path,
        raw_data_dir=raw_directory,
        processed_data_dir=processed_directory,
        reference_data_dir=reference_directory,
    )

    copied_file = destination / "raw" / "file-id" / "measurement.bin"
    assert copied_file.read_bytes() == original
    assert raw_file.read_bytes() == original
    assert result["verified_catalog_files"] == 1
    assert result["database_backup"].is_file()
    with sqlite3.connect(database_path) as connection:
        stored_path = connection.execute(
            "SELECT storage_path FROM imported_files WHERE file_id = 'file-id'"
        ).fetchone()[0]
    assert stored_path == str(copied_file.resolve())
