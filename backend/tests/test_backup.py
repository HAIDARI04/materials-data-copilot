import hashlib
import json
import sqlite3

import database
import pytest
from backup import create_backup


def test_create_backup_copies_database_raw_bytes_and_manifest(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "source" / "materials.db"
    raw_directory = tmp_path / "source" / "raw"
    processed_directory = tmp_path / "source" / "processed"
    reference_directory = tmp_path / "source" / "references"
    destination_root = tmp_path / "backups"
    raw_file = raw_directory / "file-id" / "experiment.bin"
    original_bytes = b"\x00\xff\r\nimmutable experiment"

    monkeypatch.setattr(database, "DATABASE_PATH", database_path)
    database.initialize_database()
    raw_file.parent.mkdir(parents=True)
    raw_file.write_bytes(original_bytes)
    staging_file = raw_directory / ".staging" / "incomplete.upload"
    staging_file.parent.mkdir()
    staging_file.write_bytes(b"do not back up")
    processed_file = processed_directory / "file-id" / "run-id" / "result.json"
    processed_file.parent.mkdir(parents=True)
    processed_file.write_bytes(b'{"derived":true}')
    reference_file = reference_directory / "reference-id" / "paper.pdf"
    reference_file.parent.mkdir(parents=True)
    reference_file.write_bytes(b"%PDF-1.4\nimmutable reference\n%%EOF")

    metadata = {
        "file_id": "file-id",
        "original_filename": "experiment.bin",
        "content_type": "application/octet-stream",
        "size_bytes": len(original_bytes),
        "sha256": hashlib.sha256(original_bytes).hexdigest(),
        "storage_path": str(raw_file),
        "imported_at": "2026-07-29T00:00:00+00:00",
        "technique": "Raman",
        "material_system": "Unknown",
        "sample_id": "SAMPLE-001",
        "measurement_date": None,
        "instrument": None,
        "operator": None,
        "notes": None,
    }
    with database.connect_database() as connection:
        database.insert_imported_file(connection, metadata)

    backup_directory = create_backup(
        destination_root,
        database_path=database_path,
        raw_data_dir=raw_directory,
        processed_data_dir=processed_directory,
        reference_data_dir=reference_directory,
    )

    copied_raw_file = backup_directory / "raw" / "file-id" / "experiment.bin"
    assert copied_raw_file.read_bytes() == original_bytes
    assert not (backup_directory / "raw" / ".staging").exists()
    copied_processed_file = (
        backup_directory / "processed" / "file-id" / "run-id" / "result.json"
    )
    assert copied_processed_file.read_bytes() == b'{"derived":true}'
    copied_reference_file = (
        backup_directory / "references" / "reference-id" / "paper.pdf"
    )
    assert copied_reference_file.read_bytes() == reference_file.read_bytes()

    with sqlite3.connect(backup_directory / database_path.name) as connection:
        row_count = connection.execute(
            "SELECT COUNT(*) FROM imported_files"
        ).fetchone()[0]
    assert row_count == 1

    manifest = json.loads(
        (backup_directory / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["database"] == database_path.name
    assert len(manifest["database_sha256"]) == 64
    assert manifest["database_integrity"] == "ok"
    assert manifest["source_catalog_verification"]["database"]["valid"] is True
    assert manifest["catalog_file_count"] == 1
    assert manifest["raw_file_count"] == 1
    assert manifest["raw_files"][0]["path"] == "raw/file-id/experiment.bin"
    assert manifest["raw_files"][0]["size_bytes"] == len(original_bytes)
    assert manifest["processed_file_count"] == 1
    assert manifest["processed_files"][0]["path"] == (
        "processed/file-id/run-id/result.json"
    )
    assert manifest["reference_file_count"] == 1
    assert manifest["reference_files"][0]["path"] == (
        "references/reference-id/paper.pdf"
    )


def test_backup_refuses_checksum_mismatch_without_partial_output(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "source" / "materials.db"
    raw_directory = tmp_path / "source" / "raw"
    processed_directory = tmp_path / "source" / "processed"
    reference_directory = tmp_path / "source" / "references"
    destination_root = tmp_path / "backups"
    raw_file = raw_directory / "file-id" / "experiment.bin"
    original_bytes = b"immutable experiment"
    raw_file.parent.mkdir(parents=True)
    raw_file.write_bytes(original_bytes)

    monkeypatch.setattr(database, "DATABASE_PATH", database_path)
    database.initialize_database()
    metadata = {
        "file_id": "file-id",
        "original_filename": "experiment.bin",
        "content_type": "application/octet-stream",
        "size_bytes": len(original_bytes),
        "sha256": hashlib.sha256(original_bytes).hexdigest(),
        "storage_path": str(raw_file),
        "imported_at": "2026-08-26T00:00:00+00:00",
        "technique": "Raman",
        "material_system": "Unknown",
        "sample_id": "SAMPLE-001",
        "measurement_date": None,
        "instrument": None,
        "operator": None,
        "notes": None,
    }
    with database.connect_database() as connection:
        database.insert_imported_file(connection, metadata)
    raw_file.write_bytes(b"tampered experiment")

    with pytest.raises(ValueError, match="byte count|checksum"):
        create_backup(
            destination_root,
            database_path=database_path,
            raw_data_dir=raw_directory,
            processed_data_dir=processed_directory,
            reference_data_dir=reference_directory,
        )
    assert not destination_root.exists() or list(destination_root.iterdir()) == []
