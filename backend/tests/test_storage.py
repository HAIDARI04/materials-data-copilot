from pathlib import Path

import storage


def test_storage_defaults_keep_database_and_files_local():
    paths = storage.resolve_storage_paths({})

    assert paths.local_data_dir == (storage.BACKEND_DIR / "data").resolve()
    assert paths.shared_data_dir == paths.local_data_dir
    assert paths.database_path == paths.local_data_dir / "materials_data_copilot.db"
    assert paths.raw_data_dir == paths.local_data_dir / "raw"
    assert paths.processed_data_dir == paths.local_data_dir / "processed"
    assert paths.reference_data_dir == paths.local_data_dir / "references"


def test_shared_storage_moves_payloads_but_not_database(tmp_path: Path):
    shared = tmp_path / "Google Drive" / "MDC-Data"
    local = tmp_path / "local"

    paths = storage.resolve_storage_paths(
        {
            "MDC_LOCAL_DATA_DIR": str(local),
            "MDC_SHARED_DATA_DIR": str(shared),
        }
    )

    assert paths.database_path == local / "materials_data_copilot.db"
    assert paths.raw_data_dir == shared / "raw"
    assert paths.processed_data_dir == shared / "processed"
    assert paths.reference_data_dir == shared / "references"
