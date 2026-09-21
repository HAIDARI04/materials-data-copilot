import hashlib
import io
import zipfile
from pathlib import Path

import database
import main
import pytest
import transport
from fastapi.testclient import TestClient


client = TestClient(main.app)


@pytest.fixture
def isolated_storage(tmp_path, monkeypatch):
    database_path = tmp_path / "transport.db"
    raw_directory = tmp_path / "raw"
    processed_directory = tmp_path / "processed"
    reference_directory = tmp_path / "references"
    monkeypatch.setattr(database, "DATABASE_PATH", database_path)
    monkeypatch.setattr(main, "RAW_DATA_DIR", raw_directory)
    monkeypatch.setattr(main, "PROCESSED_DATA_DIR", processed_directory)
    monkeypatch.setattr(main, "REFERENCE_DATA_DIR", reference_directory)
    database.initialize_database()
    return database_path, raw_directory, processed_directory


def worksheet_xml(headers, rows):
    def cell(reference, value):
        if isinstance(value, str):
            return f'<c r="{reference}" t="str"><v>{value}</v></c>'
        return f'<c r="{reference}"><v>{value}</v></c>'

    row_xml = []
    for row_index, row in enumerate([headers, *rows], start=1):
        cells = "".join(
            cell(f"{chr(65 + column_index)}{row_index}", value)
            for column_index, value in enumerate(row)
        )
        row_xml.append(f'<row r="{row_index}">{cells}</row>')
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(row_xml)}</sheetData></worksheet>'
    )


def synthetic_transport_workbook() -> bytes:
    workbook = io.BytesIO()
    with zipfile.ZipFile(workbook, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="utf-8"?>'
            '<workbook xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
            'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheets>'
            '<sheet name="Dark IV" sheetId="1" r:id="rId1"/>'
            '<sheet name="Zero bias" sheetId="2" r:id="rId2"/>'
            '</sheets></workbook>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="utf-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="worksheet"/>'
            '<Relationship Id="rId2" Target="worksheets/sheet2.xml" Type="worksheet"/>'
            '</Relationships>',
        )
        headers = ["Time", "BI", "BV", "AI", "AV", "RES"]
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            worksheet_xml(
                headers,
                [
                    [0, 0, 0, -1e-6, -1, 1e6],
                    [1, 0, 0, 0, 0, 1e6],
                    [2, 0, 0, 1e-6, 1, 1e6],
                ],
            ),
        )
        archive.writestr(
            "xl/worksheets/sheet2.xml",
            worksheet_xml(
                headers,
                [
                    [0, 0, 0, 1e-12, 0, 0],
                    [1, 0, 0, 1.1e-12, 0, 0],
                    [2, 0, 0, 0.9e-12, 0, 0],
                ],
            ),
        )
    return workbook.getvalue()


def test_transport_analysis_preserves_source_and_persists_result(isolated_storage):
    source = synthetic_transport_workbook()
    imported = client.post(
        "/files/import",
        files={
            "file": (
                "transport.xlsx",
                source,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
        data={
            "technique": "Transport properties",
            "material_system": "Unknown",
            "sample_id": "D01-01",
            "measurement_date": "2026-09-01",
            "instrument": "Probe station",
        },
    )
    assert imported.status_code == 201

    response = client.post(
        f"/files/{imported.json()['file_id']}/transport-analysis"
    )
    assert response.status_code == 200
    result = response.json()
    assert result["workbook"]["worksheet_count"] == 2
    assert result["summary"]["measurement_types"] == {
        "iv_sweep": 1,
        "current_time": 1,
    }
    iv_fit = result["sheets"][0]["iv_fits"]["AV_AI"]
    assert iv_fit["resistance_ohm"] == pytest.approx(1e6)
    assert iv_fit["r_squared"] == pytest.approx(1.0)

    stored = Path(imported.json()["storage_path"])
    assert stored.read_bytes() == source
    assert hashlib.sha256(stored.read_bytes()).hexdigest() == imported.json()["sha256"]
    with database.connect_database() as connection:
        stored_record = connection.execute(
            "SELECT * FROM imported_files WHERE file_id = ?",
            (imported.json()["file_id"],),
        ).fetchone()
        run = connection.execute(
            "SELECT * FROM processing_runs WHERE file_id = ?",
            (imported.json()["file_id"],),
        ).fetchone()
    assert stored_record["original_filename"] == "transport.xlsx"
    assert stored_record["content_type"] == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert stored_record["size_bytes"] == len(source)
    assert stored_record["sha256"] == hashlib.sha256(source).hexdigest()
    assert Path(stored_record["storage_path"]) == stored
    assert stored_record["imported_at"]
    assert run["model_name"] == transport.MODEL_NAME
    assert Path(run["result_path"]).is_file()
    assert hashlib.sha256(Path(run["result_path"]).read_bytes()).hexdigest() == run["result_sha256"]

    replay = client.get(
        f"/files/{imported.json()['file_id']}/transport-analysis"
    )
    assert replay.status_code == 200
    assert replay.json()["processing_id"] == result["processing_id"]


def test_truncated_xlsx_is_recovered_with_explicit_partial_warning(tmp_path):
    source = synthetic_transport_workbook()
    central_directory = source.find(b"PK\x01\x02")
    assert central_directory > 0
    path = tmp_path / "truncated.xlsx"
    path.write_bytes(source[:central_directory])

    result = transport.analyze_workbook(path)

    assert result["workbook"]["worksheet_count"] == 2
    assert any("central directory" in warning for warning in result["workbook"]["warnings"])
    assert any("partial" in warning for warning in result["workbook"]["warnings"])


def test_transport_settings_capture_terminal_roles_and_data_modes():
    metadata = transport._settings_metadata(
        [
            {
                "name": "Settings",
                "rows": [
                    ["Device Terminal", "B", "A"],
                    ["Instrument", "SMU2", "SMU1"],
                    ["Name", "BV", "AV"],
                    ["Operation Mode", "Voltage Bias", "Voltage Linear Sweep"],
                    ["Measure Current", "Measured", "Measured"],
                    ["Measure Voltage", "Programmed", "Programmed"],
                ],
            }
        ]
    )

    assert metadata["biased_terminals"] == [
        {
            "device_terminal": "B",
            "terminal_name": "BV",
            "terminal_role": None,
            "role_basis": "not explicit in workbook",
            "operation_mode": "Voltage Bias",
            "current_data": "measured",
            "voltage_data": "programmed",
        }
    ]
    assert metadata["terminals"][1]["terminal_name"] == "AV"

    explicit = transport._settings_metadata(
        [
            {
                "name": "Settings",
                "rows": [
                    ["Device Terminal", "Drain", "Gate", "Source"],
                    ["Name", "DrainV", "GateV", "SourceV"],
                    ["Operation Mode", "Voltage Linear Sweep", "Voltage Bias", "Voltage Bias"],
                ],
            }
        ]
    )
    assert [item["terminal_role"] for item in explicit["terminals"]] == [
        "drain",
        "gate",
        "source",
    ]
    assert explicit["role_warnings"] == []


def test_transport_analysis_rejects_wrong_technique(isolated_storage):
    source = synthetic_transport_workbook()
    imported = client.post(
        "/files/import",
        files={"file": ("transport.xlsx", source, "application/octet-stream")},
        data={"technique": "AFM", "sample_id": "D01-01"},
    ).json()

    response = client.post(f"/files/{imported['file_id']}/transport-analysis")

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "not_transport_data"
