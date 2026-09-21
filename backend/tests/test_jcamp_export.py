import hashlib
from fastapi.testclient import TestClient

import database
import main
from main import app

client = TestClient(app)


def setup_single_test_file(tmp_path, monkeypatch):
    db_path = tmp_path / "test_jcamp.db"
    raw_dir = tmp_path / "raw"

    import jcamp_exporter
    monkeypatch.setattr(database, "DATABASE_PATH", db_path)
    monkeypatch.setattr(main, "RAW_DATA_DIR", raw_dir)
    monkeypatch.setattr(jcamp_exporter, "RAW_DATA_DIR", raw_dir)

    database.initialize_database()

    file_id = "file-jcamp-123"
    dir_path = raw_dir / file_id
    dir_path.mkdir(parents=True, exist_ok=True)
    raw_file = dir_path / "silicon_sample.txt"

    lines = ["# Raman shift, Intensity"]
    for x in range(200, 800, 10):
        lines.append(f"{x}, {100 + x * 0.5}")
    bytes_content = "\n".join(lines).encode("utf-8")
    raw_file.write_bytes(bytes_content)

    meta = {
        "file_id": file_id,
        "original_filename": "silicon_sample.txt",
        "content_type": "text/plain",
        "size_bytes": len(bytes_content),
        "sha256": hashlib.sha256(bytes_content).hexdigest(),
        "storage_path": str(raw_file),
        "imported_at": "2026-08-01T12:00:00+00:00",
        "technique": "Raman",
        "material_system": "Silicon",
        "sample_id": "Si-100",
        "measurement_date": "2026-08-01",
        "instrument": "Renishaw inVia",
        "operator": "Dr. Scientist",
        "notes": "Silicon reference spectrum",
    }

    with database.connect_database() as conn:
        database.insert_imported_file(conn, meta)

    return file_id, meta


def test_export_jcamp_success(tmp_path, monkeypatch):
    file_id, meta = setup_single_test_file(tmp_path, monkeypatch)

    response = client.get(f"/files/{file_id}/export/jcamp")
    assert response.status_code == 200
    assert "application/x-jcamp-dx" in response.headers["content-type"]
    assert 'filename="silicon_sample_jcamp.dx"' in response.headers["content-disposition"]

    text = response.text
    assert "##TITLE=silicon_sample.txt" in text
    assert "##JCAMP-DX=4.24" in text
    assert "##DATA TYPE=RAMAN SPECTRUM" in text
    assert f"##SHA256={meta['sha256']}" in text
    assert "##MATERIAL SYSTEM=Silicon" in text
    assert "##XYDATA=(X++(Y..Y))" in text
    assert "##END=" in text


def test_export_jcamp_not_found(tmp_path, monkeypatch):
    setup_single_test_file(tmp_path, monkeypatch)

    response = client.get("/files/invalid-file-id-xyz/export/jcamp")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "file_not_found"
