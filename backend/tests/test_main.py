import hashlib
import io
import json
import math
import sqlite3
import struct
import zipfile
from pathlib import Path

import ai_providers
import database
import main
import online_references
import processing
import pytest
import references
import preview
from fastapi.testclient import TestClient

from main import app


client = TestClient(app)

DEFAULT_METADATA = {
    "technique": "Raman spectroscopy",
    "material_system": "Unknown",
    "sample_id": "SAMPLE-001",
    "measurement_date": "2026-07-29",
    "instrument": "Lab Raman 532 nm",
    "operator": "Test Operator",
    "notes": "First pilot measurement",
}


@pytest.fixture
def isolated_storage(tmp_path, monkeypatch):
    temporary_database = tmp_path / "test.db"
    temporary_raw_directory = tmp_path / "raw"
    temporary_processed_directory = tmp_path / "processed"
    temporary_reference_directory = tmp_path / "references"
    monkeypatch.setattr(database, "DATABASE_PATH", temporary_database)
    monkeypatch.setattr(main, "RAW_DATA_DIR", temporary_raw_directory)
    monkeypatch.setattr(
        main,
        "PROCESSED_DATA_DIR",
        temporary_processed_directory,
    )
    monkeypatch.setattr(
        main,
        "REFERENCE_DATA_DIR",
        temporary_reference_directory,
    )
    database.initialize_database()
    return temporary_database, temporary_raw_directory


def post_import(
    filename="sample.bin",
    contents=b"experimental bytes",
    metadata=None,
):
    return client.post(
        "/files/import",
        files={"file": (filename, contents, "application/octet-stream")},
        data=DEFAULT_METADATA if metadata is None else metadata,
    )


def synthetic_wdf_bytes() -> bytes:
    def block(name: bytes, block_id: int, payload: bytes) -> bytes:
        return struct.pack("<4sIQ", name, block_id, len(payload) + 16) + payload

    x_values = [100.0, 200.0, 300.0]
    spectra = [[10.0, 25.0, 12.0], [8.0, 18.0, 30.0]]
    header = bytearray(512)
    struct.pack_into("<4sIQ", header, 0, b"WDF1", 1, len(header))
    struct.pack_into("<I", header, 60, len(x_values))
    struct.pack_into("<Q", header, 64, len(spectra))
    struct.pack_into("<Q", header, 72, len(spectra))
    struct.pack_into("<I", header, 80, 1)
    header[96:100] = b"WiRE"
    struct.pack_into("<4H", header, 120, 5, 5, 0, 0)
    struct.pack_into("<I", header, 128, 1)
    struct.pack_into("<I", header, 132, 2)
    struct.pack_into("<f", header, 156, 18796.99)
    header[208:213] = b"RAMAN"
    title = b"Synthetic WDF series"
    header[240 : 240 + len(title)] = title
    data = struct.pack(
        f"<{len(x_values) * len(spectra)}f",
        *(value for spectrum in spectra for value in spectrum),
    )
    x_axis = struct.pack("<II3f", 1, 1, *x_values)
    return bytes(header) + block(b"DATA", 0, data) + block(b"XLST", 0, x_axis)


def synthetic_raman_bytes() -> bytes:
    lines = [
        "FILETYPE=RAMAN SPECTRUM",
        "TYPE=RamanShift",
        "XUNITS=1/cm",
        "XYDATA=",
    ]
    for x_value in range(1000, 1801, 2):
        baseline = 100 + 0.01 * (x_value - 1000)
        d_band = 45 * math.exp(-((x_value - 1350) / 24) ** 2)
        g_band = 90 * math.exp(-((x_value - 1580) / 20) ** 2)
        lines.append(f"{x_value},{baseline + d_band + g_band:.8f}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def synthetic_perovskite_low_frequency_bytes() -> bytes:
    lines = [
        "FILETYPE=RAMAN SPECTRUM",
        "TYPE=RamanShift",
        "XUNITS=1/cm",
        "XYDATA=",
    ]
    for x_value in range(20, 301, 2):
        baseline = 70 + 0.01 * x_value
        lattice_one = 75 * math.exp(-((x_value - 74) / 7) ** 2)
        lattice_two = 90 * math.exp(-((x_value - 90) / 8) ** 2)
        resolved_mode = 110 * math.exp(-((x_value - 179) / 10) ** 2)
        intensity = baseline + lattice_one + lattice_two + resolved_mode
        lines.append(f"{x_value},{intensity:.8f}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def synthetic_glass_supported_raman_bytes() -> bytes:
    lines = [
        "FILETYPE=RAMAN SPECTRUM",
        "TYPE=RamanShift",
        "XUNITS=1/cm",
        "XYDATA=",
    ]
    for x_value in range(350, 1801, 2):
        baseline = 80 + 0.005 * x_value
        glass_800 = 90 * math.exp(-((x_value - 810) / 28) ** 2)
        glass_1070 = 110 * math.exp(-((x_value - 1065) / 38) ** 2)
        d_band = 70 * math.exp(-((x_value - 1350) / 22) ** 2)
        g_band = 140 * math.exp(-((x_value - 1580) / 18) ** 2)
        intensity = baseline + glass_800 + glass_1070 + d_band + g_band
        lines.append(f"{x_value},{intensity:.8f}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def synthetic_shared_substrate_bytes(material_peak: float) -> bytes:
    lines = [
        "FILETYPE=RAMAN SPECTRUM",
        "TYPE=RamanShift",
        "XUNITS=1/cm",
        "XYDATA=",
    ]
    for x_value in range(400, 1401, 2):
        baseline = 60 + 0.008 * x_value
        shared_substrate = 85 * math.exp(-((x_value - 700) / 14) ** 2)
        material = 110 * math.exp(-((x_value - material_peak) / 18) ** 2)
        lines.append(f"{x_value},{baseline + shared_substrate + material:.8f}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def synthetic_substrate_reference_bytes(
    shift: float = 0.0,
    intensity_scale: float = 1.0,
) -> bytes:
    lines = [
        "FILETYPE=RAMAN SPECTRUM",
        "TYPE=RamanShift",
        "XUNITS=1/cm",
        "XYDATA=",
    ]
    for index, x_value in enumerate(range(400, 1201, 2)):
        baseline = 55 + 0.006 * x_value
        noise = ((index % 5) - 2) * 0.18
        strong = 100 * intensity_scale * math.exp(
            -((x_value - (700 + shift)) / 13) ** 2
        )
        weak = 34 * intensity_scale * math.exp(
            -((x_value - (950 + shift)) / 17) ** 2
        )
        lines.append(f"{x_value},{baseline + noise + strong + weak:.8f}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def synthetic_t12_substrate_interval_bytes() -> bytes:
    lines = [
        "FILETYPE=RAMAN SPECTRUM",
        "TYPE=RamanShift",
        "XUNITS=1/cm",
        "XYDATA=",
    ]
    for x_value in range(450, 1001, 2):
        baseline = 70 + 0.004 * x_value
        substrate_background = 28 * math.exp(-((x_value - 670) / 145) ** 2)
        substrate_peaks = sum(
            amplitude * math.exp(-((x_value - center) / width) ** 2)
            for center, amplitude, width in (
                (520, 120, 10),
                (610, 65, 14),
                (720, 90, 13),
                (820, 55, 11),
            )
        )
        material_peak = 105 * math.exp(-((x_value - 880) / 12) ** 2)
        lines.append(
            f"{x_value},{baseline + substrate_background + substrate_peaks + material_peak:.8f}"
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def synthetic_two_peak_bytes(first_peak: float, second_peak: float) -> bytes:
    lines = [
        "FILETYPE=RAMAN SPECTRUM",
        "TYPE=RamanShift",
        "XUNITS=1/cm",
        "XYDATA=",
    ]
    for x_value in range(700, 1501, 2):
        baseline = 50 + 0.005 * x_value
        first = 120 * math.exp(-((x_value - first_peak) / 16) ** 2)
        second = 90 * math.exp(-((x_value - second_peak) / 18) ** 2)
        lines.append(f"{x_value},{baseline + first + second:.8f}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def synthetic_singleton_noise_bytes(weak_amplitude: float = 0.0) -> bytes:
    lines = [
        "FILETYPE=RAMAN SPECTRUM",
        "TYPE=RamanShift",
        "XUNITS=1/cm",
        "XYDATA=",
    ]
    for x_value in range(700, 1501, 2):
        baseline = 50 + 0.005 * x_value
        material_peak = 120 * math.exp(-((x_value - 1000) / 16) ** 2)
        singleton = weak_amplitude * math.exp(-((x_value - 1300) / 6) ** 2)
        lines.append(
            f"{x_value},{baseline + material_peak + singleton:.8f}"
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def insert_test_reference(
    reference_id: str,
    material_system: str,
    peak_position: float,
):
    evidence = {
        "provider": "test_reference",
        "match_tolerance_cm-1": 12.0,
        "peaks": [
            {
                "position_cm-1": peak_position,
                "assignment": f"{material_system} test band",
            }
        ],
    }
    record = {
        "reference_id": reference_id,
        "original_filename": f"{reference_id}.json",
        "content_type": "application/json",
        "size_bytes": 1,
        "sha256": hashlib.sha256(reference_id.encode()).hexdigest(),
        "storage_path": f"test/{reference_id}.json",
        "imported_at": "2026-07-31T00:00:00+00:00",
        "source_kind": "spectrum",
        "technique": "Raman",
        "material_system": material_system,
        "title": f"Reference for {material_system}",
        "citation": "Test reference",
        "source_url": "https://example.org/test-reference",
        "extraction_status": "ready",
        "evidence_json": json.dumps(evidence, separators=(",", ":"), sort_keys=True),
    }
    with database.connect_database() as connection:
        database.insert_reference_source(connection, record)


def post_reference(
    filename: str,
    contents: bytes,
    material_system: str = "Carbon nanotube",
    content_type: str = "text/plain",
):
    if not filename.lower().endswith(".pdf"):
        contents = (
            f"MATERIAL={material_system}\n"
            f"TITLE=Reference for {material_system}\n"
            "CITATION=Researcher et al. (2026)\n"
            "SOURCE_URL=https://example.org/reference\n"
        ).encode("utf-8") + contents
    return client.post(
        "/references/import",
        files={"file": (filename, contents, content_type)},
    )


def assert_no_import_artifacts(raw_directory):
    with database.connect_database() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM imported_files"
        ).fetchone()[0]
    assert count == 0
    if raw_directory.exists():
        assert [path for path in raw_directory.rglob("*") if path.is_file()] == []


def test_read_root():
    response = client.get("/")

    assert response.status_code == 200
    assert response.json() == {
        "application": "Materials Data Copilot",
        "status": "running",
        "version": main.APP_VERSION,
    }


def test_health_check(isolated_storage):
    response = client.get("/health")

    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "healthy"
    assert result["version"] == main.APP_VERSION
    assert result["database"]["valid"] is True
    assert result["record_counts"] == {
        "imports": 0,
        "processing_runs": 0,
        "references": 0,
        "substrate_peak_feedback": 0,
    }


def test_browser_security_headers_are_applied():
    response = client.get("/")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "object-src 'none'" in response.headers["content-security-policy"]


def test_persistent_samples_operators_recipes_and_presets(isolated_storage):
    imported = post_import().json()

    assert client.get("/operators").json() == [DEFAULT_METADATA["operator"]]
    samples = client.get("/samples").json()
    assert samples[0]["sample_id"] == DEFAULT_METADATA["sample_id"]
    assert samples[0]["material_system"] == "Unknown"

    sample_response = client.post(
        "/samples",
        json={
            "sample_id": "SAMPLE-002",
            "material_system": "MoS2",
            "substrate": "sio2_si",
            "project": "Pilot",
        },
    )
    assert sample_response.status_code == 201
    assert sample_response.json()["project"] == "Pilot"
    assert imported["sample_id"] == DEFAULT_METADATA["sample_id"]
    second_metadata = {**DEFAULT_METADATA, "sample_id": "SAMPLE-002"}
    assert post_import(
        "sample-002-repeat.bin",
        b"second experimental bytes",
        second_metadata,
    ).status_code == 201
    saved_sample = next(
        sample
        for sample in client.get("/samples").json()
        if sample["sample_id"] == "SAMPLE-002"
    )
    assert saved_sample["project"] == "Pilot"
    assert saved_sample["material_system"] == "MoS2"

    recipe = {
        "name": "My saved recipe",
        "config": {
            "deconvolution": "pseudo_voigt",
            "substrateCorrection": "detect",
        },
    }
    recipe_response = client.put("/analysis/recipes/personal", json=recipe)
    assert recipe_response.status_code == 200
    assert client.get("/analysis/recipes").json()[0]["config"] == recipe["config"]

    preset_response = client.put(
        "/presets/lab-raman",
        json={
            "name": "Lab Raman",
            "config": {"laser_nm": 532, "objective": "50x"},
        },
    )
    assert preset_response.status_code == 200
    assert client.get("/presets").json()[0]["config"]["laser_nm"] == 532


def test_import_inspection_integrity_content_suggestions_and_mapping(
    isolated_storage,
):
    spectrum_bytes = (
        "FILETYPE=RAMAN SPECTRUM\n"
        "TYPE=RamanShift\n"
        "DATETIME=2026-08-26 10:30:00\n"
        "INSTRUMENT=Renishaw inVia\n"
        "OPERATOR=Analyst\n"
        "XYDATA=\n"
        "100,10\n200,20\n300,30\n"
    ).encode()
    inspection = client.post(
        "/files/inspect",
        files={"file": ("Raman_A123.txt", spectrum_bytes, "text/plain")},
    )
    assert inspection.status_code == 200
    assert inspection.json()["suggested_metadata"] == {
        "sample_id": "A123",
        "technique": "Raman spectroscopy",
        "measurement_date": "2026-08-26",
        "instrument": "Renishaw inVia",
        "operator": "Analyst",
    }

    imported = post_import("Raman_A123.txt", spectrum_bytes).json()
    integrity_response = client.get(f"/files/{imported['file_id']}/integrity")
    assert integrity_response.status_code == 200
    assert integrity_response.json()["valid"] is True
    content_response = client.get(f"/files/{imported['file_id']}/content")
    assert content_response.content == spectrum_bytes
    assert content_response.headers["content-disposition"].startswith("attachment")
    suggestions = client.get(
        f"/files/{imported['file_id']}/metadata-suggestions"
    ).json()
    assert suggestions["measured_spectral_bounds"] == {
        "minimum": 100.0,
        "maximum": 300.0,
        "point_count": 3,
    }

    mapping_bytes = b"1,2,3\n4,5,6\n7,8,9\n"
    mapping_import = post_import("map.csv", mapping_bytes).json()
    mapping = client.get(f"/files/{mapping_import['file_id']}/mapping")
    assert mapping.status_code == 200
    assert mapping.json()["rows"] == 3
    assert mapping.json()["columns"] == 3
    assert mapping.json()["values"] == [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0], [7.0, 8.0, 9.0]]


def test_metadata_suggestions_use_relative_path_and_metadata_edit_updates_sample(
    isolated_storage,
):
    suggestions = preview.extract_metadata_suggestions(
        "D00_I-V-sweep.xls",
        b"not an xls workbook",
        "Probe-station-Data/D00_T12_2026.07.22/D00_I-V-sweep.xls",
    )
    assert suggestions["sample_id"] == "D00"
    assert suggestions["project"] == "D00_T12_2026.07.22"

    imported = post_import(
        "D00_I-V-sweep.xls",
        b"transport bytes",
        {
            **DEFAULT_METADATA,
            "technique": "Transport",
            "sample_id": "Probe-station-Data",
            "measurement_date": "",
            "instrument": "",
            "operator": "",
            "substrate": "unknown",
            "measurement_role": "unspecified",
        },
    ).json()
    response = client.patch(
        f"/files/{imported['file_id']}",
        json={
            "sample_id": "D00",
            "project": "D00_T12_2026.07.22",
            "measurement_date": "2026-07-22",
            "instrument": "SMU2, SMU1",
            "measurement_role": "sample_on_substrate",
        },
    )
    assert response.status_code == 200
    assert response.json()["sample_id"] == "D00"
    sample = next(item for item in client.get("/samples").json() if item["sample_id"] == "D00")
    assert sample["project"] == "D00_T12_2026.07.22"


def test_sample_id_suggestions_prioritize_device_folders_and_fallback_to_filename(
    isolated_storage,
):
    assert preview.sample_id_from_path(
        "measurement.xls",
        "D01_T12_2026/Probe-station-Data/measurement.xls",
    ) == "D01"
    assert preview.sample_id_from_path(
        "measurement.xls",
        "Test-Working-Devices/D02-01/measurement.xls",
    ) == "D02"
    assert preview.sample_id_from_path(
        "D03_I-V-sweep.xls",
        "Test-Working-Devices/D03_I-V-sweep.xls",
    ) == "D03"
    assert preview.sample_id_from_path(
        "measurement.xls",
        "Test-Working-Devices/D04-01/measurement.xls",
    ) == "D04"
    assert preview.sample_id_from_path(
        "measurement.xls",
        "Probe-station-Data/measurement.xls",
    ) is None


def test_measurement_date_suggestions_use_embedded_path_and_filename_metadata(
    isolated_storage,
):
    assert preview.measurement_date_from_metadata(
        "measurement.txt",
        "D01/2026.07.22/measurement.txt",
    ) == "2026-07-22"
    assert preview.measurement_date_from_metadata(
        "measurement_2026-08-03.csv",
    ) == "2026-08-03"
    assert preview.measurement_date_from_metadata(
        "measurement.txt",
        text="DATETIME=2026-09-14T12:00:00",
    ) == "2026-09-14"
    assert preview.measurement_date_from_metadata(
        "measurement.txt",
        text="Last Executed=9/14/2026 12:00",
    ) == "2026-09-14"


def test_import_assigns_date_when_upload_date_is_blank(isolated_storage):
    imported = post_import(
        "measurement_2026-08-03.csv",
        b"1,2\n2,3\n",
        {
            **DEFAULT_METADATA,
            "measurement_date": "",
        },
    ).json()
    assert imported["measurement_date"] == "2026-08-03"


def test_import_persists_and_edits_data_category(isolated_storage):
    imported = post_import(
        "micrograph.png",
        b"image bytes",
        {
            **DEFAULT_METADATA,
            "data_category": "image",
        },
    ).json()
    assert imported["data_category"] == "image"

    response = client.patch(
        f"/files/{imported['file_id']}",
        json={"data_category": "reference"},
    )
    assert response.status_code == 200
    assert response.json()["data_category"] == "reference"


def test_processing_history_replay_and_database_provenance_guards(
    isolated_storage,
):
    imported = post_import("history.txt", synthetic_raman_bytes()).json()
    processed = client.post(f"/files/{imported['file_id']}/process").json()

    history = client.get(
        f"/files/{imported['file_id']}/processing-runs"
    ).json()
    assert len(history) == 1
    assert history[0]["processing_id"] == processed["processing_id"]
    replay = client.get(history[0]["result_url"])
    assert replay.status_code == 200
    assert replay.json()["verification"] == "verified"
    assert replay.json()["result"]["result_sha256"] == processed["result_sha256"]

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with database.connect_database() as connection:
            connection.execute(
                "UPDATE processing_runs SET result_sha256 = ? "
                "WHERE processing_id = ?",
                ("0" * 64, processed["processing_id"]),
            )

    with database.connect_database() as connection:
        database.delete_processing_run(
            connection,
            processed["processing_id"],
            "test replacement",
        )
    tombstones = database.list_processing_run_tombstones()
    assert tombstones[0]["processing_id"] == processed["processing_id"]
    assert tombstones[0]["reason"] == "test replacement"


def test_health_fails_when_retained_raw_bytes_change(isolated_storage):
    imported = post_import(contents=b"immutable bytes").json()
    Path(imported["storage_path"]).write_bytes(b"tampered bytes")

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "integrity_verification_failed"


def test_upload_interface_is_available():
    response = client.get("/upload")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Drop files here" in response.text
    assert '<input id="file" name="file" type="file" multiple>' in response.text
    assert "Up to 50 files per batch" in response.text
    assert "selectedFiles = []" in response.text
    assert "for (const [index, file] of spectrumFiles.entries())" in response.text
    assert "same-named optical images" in response.text
    assert 'id="optical-image-card"' in response.text
    assert 'id="series-comparison-summary"' in response.text
    assert "/attachments" in response.text
    assert "pairingKey" in response.text
    assert 'name="technique"' in response.text
    assert '<option value="Transport properties">' in response.text
    assert '<option value="XPS">' in response.text
    assert 'name="material_system"' in response.text
    assert response.text.count('name="material_system"') == 2
    assert response.text.count('name="measurement_role"') == 2
    assert 'name="sample_id"' in response.text
    assert 'id="plot-canvas"' in response.text
    assert '<div class="analysis-workspace">' in response.text
    assert "--content-width: 1500px" in response.text
    assert "grid-template-columns: minmax(290px, 330px) minmax(0, 1fr)" in response.text
    assert "height: clamp(480px, 58vh, 720px)" in response.text
    assert "#edit-import-dialog {" in response.text
    assert "width: min(880px" in response.text
    assert "@media (max-width: 1180px)" in response.text
    assert '.analysis-workspace:has(#plot-panel[hidden])' in response.text
    assert '<h2 id="recent-heading">Imports</h2>' in response.text
    assert "Recent imports" not in response.text
    assert response.text.index('id="recent-panel"') < response.text.index(
        'id="plot-panel"'
    )
    assert 'id="plot-tooltip"' in response.text
    assert 'id="processing-details"' in response.text
    assert 'id="processing-details-summary"' in response.text
    assert '<details id="processing-details">' in response.text
    assert 'id="zoom-range"' in response.text
    assert 'id="zoom-number"' in response.text
    assert 'id="reset-view-button"' in response.text
    assert 'id="zoom-select"' not in response.text
    assert 'id="plot-interaction-mode"' not in response.text
    assert 'id="plot-drag-mode"' in response.text
    assert '<option value="pan">Pan</option>' in response.text
    assert "Shift + drag to pan" in response.text
    assert 'plotDragMode.value === "pan" || isMiddleButton || event.shiftKey' in response.text
    assert "mouse wheel to zoom around the cursor" in response.text
    assert "assignment.provisional" in response.text
    assert 'assignment.confidence !== "user_confirmed"' in response.text
    assert "zoomPlotWithWheel" in response.text
    assert '{ passive: false }' in response.text
    assert 'id="analysis-recipe-select"' in response.text
    assert 'id="save-recipe-button"' in response.text
    assert 'id="analysis-history-button"' in response.text
    assert 'id="instrument-preset-select"' in response.text
    assert 'id="save-instrument-preset"' in response.text
    assert 'list="sample-id-options"' in response.text
    assert 'list="operator-options"' in response.text
    assert 'id="batch-process-button"' in response.text
    assert 'id="batch-results"' in response.text
    assert 'id="export-batch-summary-button"' in response.text
    assert "/files/process/batch" in response.text
    assert "processableImports" in response.text
    assert "transportImports" in response.text
    assert "/transport-analysis" in response.text
    assert 'id="deconvolution-select"' in response.text
    assert 'id="substrate-correction-select"' in response.text
    assert 'id="baseline-method-select"' in response.text
    assert 'id="advanced-processing-settings"' in response.text
    assert 'id="substrate-reference-strategy"' in response.text
    assert 'id="quality-policy"' in response.text
    assert 'id="peak-review-policy"' in response.text
    assert 'id="ensemble-minimum-coverage"' in response.text
    assert 'id="planned-protocol-list"' in response.text
    assert 'value="rubber_band"' in response.text
    assert "Glass / SiO2" in response.text
    assert 'id="batch-archive-button"' in response.text
    assert 'id="substrate-feedback-panel"' in response.text
    assert "function pendingSubstrateResidualPeaks(result)" in response.text
    assert "row.remove();" in response.text
    assert '<option value="glass">' in response.text
    assert "Existing legacy substrate value" in response.text
    assert 'value="auto"' in response.text
    assert 'id="substrate-corrected-option"' in response.text
    assert 'id="substrate-fit-option"' in response.text
    assert 'value="pseudo_voigt"' in response.text
    assert 'id="deconvolved-series-option"' in response.text
    assert 'id="deconvolution-residual-option"' in response.text
    assert 'id="fit-inset-toggle"' in response.text
    assert 'id="fit-inset-panel"' in response.text
    assert 'id="fit-inset-handle"' in response.text
    assert 'id="fit-assignment-dialog"' in response.text
    assert "/peaks/confirm" in response.text
    assert "reviewFitAssignment" in response.text
    assert 'editButton.textContent = "Edit"' in response.text
    assert 'reviewButton.textContent = "Review"' in response.text
    assert 'id="fit-assignment-papers"' in response.text
    assert 'id="fit-assignment-learning-consent"' in response.text
    assert "uploadFitAssignmentPapers" in response.text
    assert "learning_confirmed: true" in response.text
    assert "related_reference_ids: relatedReferenceIds" in response.text
    assert 'id="interactive-guidance-toggle"' in response.text
    assert 'id="clarification-dialog"' in response.text
    assert 'id="clarification-value-title"' in response.text
    assert "Type your answer here" in response.text
    assert 'selectedAnswer === "custom"' in response.text
    assert 'id="edit-import-dialog"' in response.text
    assert 'id="folder"' in response.text
    assert "webkitdirectory" in response.text
    assert 'id="selected-imports"' in response.text
    assert 'id="choose-folder-button"' in response.text
    assert 'id="technique-filter"' in response.text
    assert 'id="import-sort"' in response.text
    assert 'id="import-group"' in response.text
    assert 'id="imports-page-status"' in response.text
    assert "IMPORTS_PAGE_SIZE = 8" in response.text
    assert 'id="import-search"' in response.text
    assert 'id="substrate-filter"' in response.text
    assert 'id="analysis-range-min"' in response.text
    assert 'id="analysis-range-max"' in response.text
    assert 'id="peak-detection-threshold"' in response.text
    assert 'id="expand-plot-button"' in response.text
    assert 'id="export-plot-button"' in response.text
    assert 'id="export-format"' in response.text
    assert 'value="reproducibility"' in response.text
    assert 'value="csv"' in response.text
    assert 'value="json"' in response.text
    assert "/export/reproducibility" in response.text
    assert 'id="server-state"' in response.text
    assert 'class="skip-link"' in response.text
    assert "togglePlotExpansion" in response.text
    assert "exportPlot" in response.text
    assert "Detected unassigned peaks requiring review" in response.text
    assert "collective modes (<150 cm⁻¹)" in response.text
    assert "Broad low-intensity substrate features recovered" in response.text
    assert 'selectedAnswer === "search_online"' in response.text
    assert 'method: "PATCH"' in response.text
    assert 'method: "DELETE"' in response.text
    assert "materialsCopilotInteractiveGuidance" in response.text
    assert "/clarifications/respond" in response.text
    assert 'id="plot-selection"' in response.text
    assert 'id="zoom-in-button"' not in response.text
    assert '<option value="smoothed">' not in response.text
    assert "beginPlotSelection" in response.text
    assert 'pointermove' in response.text
    assert "Detected peak (locked)" in response.text
    assert "assignment.material_system" in response.text
    assert "placedPeakLabelBoxes" in response.text
    assert 'peak["position_cm-1"].toFixed(1)' in response.text
    assert "peakLabelAppearance" in response.text
    assert 'color: "#1565c0"' in response.text
    assert 'color: "#c62828"' in response.text
    assert "Unknown peak" in response.text
    assert '"reference band"' in response.text
    assert '"measured reference peak"' in response.text
    assert "(provisional)" in response.text
    assert "Instrument artifact: Cosmic-ray spike removed" in response.text
    assert "excluded from the instrument-artifact filtered series" in response.text
    assert "deconvolutionSelect.value" in response.text
    assert "fit components" in response.text
    assert "Total fitted curve" in response.text
    assert "/preview" in response.text
    assert 'id="wdf-inspection"' in response.text
    assert 'id="wdf-dataset-select"' in response.text
    assert "/wdf/inspect" in response.text
    assert "available_datasets" in response.text
    assert 'id="copilot-panel"' not in response.text
    assert 'id="chat-log"' not in response.text
    assert 'id="training-label"' not in response.text
    assert "/copilot/chat" not in response.text
    assert "/copilot/train" not in response.text
    assert 'id="chat-provider"' not in response.text
    assert 'id="share-spectrum-context"' not in response.text
    assert "/copilot/providers" not in response.text
    assert 'id="reference-list"' not in response.text


def test_workspace_pages_and_assets_are_available():
    for asset, content_type in [('raman_workspace.js', 'javascript'), ('raman_workspace.css', 'text/css')]:
        response = client.get('/assets/' + asset)
        assert response.status_code == 200
        assert content_type in response.headers['content-type']
    studio = client.get('/upload?workspace=raman&dataset=example')
    assert studio.status_code == 200
    assert studio.headers['x-frame-options'] == 'SAMEORIGIN'
    assert '/assets/raman_workspace.js' in studio.text
    for control in ['plot-canvas', 'analysis-recipe-select', 'baseline-method-select',
                    'deconvolution-select', 'normalization-mode-select', 'plot-drag-mode',
                    'fit-inset-toggle', 'analysis-history-button', 'export-plot-button']:
        assert f'id="{control}"' in studio.text
    for path in (
        "/app",
        "/app/projects",
        "/app/samples",
        "/app/datasets",
        "/app/analyze/raman",
        "/app/analyze/transport",
        "/app/analyze/afm",
        "/app/analyze/xps",
        "/app/references",
        "/app/reports",
    ):
        response = client.get(path)
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "Materials Data Copilot" in response.text
        assert 'id="workspace-view"' in response.text

    styles = client.get("/assets/workspace.css")
    assert styles.status_code == 200
    assert "text/css" in styles.headers["content-type"]
    assert "--sidebar-width" in styles.text

    script = client.get("/assets/workspace.js")
    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]
    assert "techniqueDefinitions" in script.text
    assert '"/files?include_analysis=true"' in script.text


def test_list_imported_files_returns_empty_list(isolated_storage):
    response = client.get("/files")

    assert response.status_code == 200
    assert response.json() == []


def test_online_reference_status_and_sync_endpoint(
    isolated_storage,
    monkeypatch,
):
    status_response = client.get("/references/online/status")
    expected = {
        "provider": "Raman Open Database",
        "synced": False,
        "reference_count": 0,
        "curated_reference_count": len(
            online_references.CURATED_FINGERPRINTS
        ),
    }
    monkeypatch.setattr(
        online_references,
        "sync_rod_catalog",
        lambda _directory: {
            "provider": "Raman Open Database",
            "reference_count": 12,
            "batch_downloaded_count": 12,
            "catalog_complete": False,
        },
    )
    sync_response = client.post("/references/online/sync")

    assert status_response.status_code == 200
    assert status_response.json() == expected
    assert sync_response.status_code == 200
    assert sync_response.json()["reference_count"] == 12


def test_copilot_chat_and_confirmed_training_are_traceable_and_learn(
    isolated_storage,
):
    original_bytes = synthetic_raman_bytes()
    first_import = post_import("confirmed.txt", original_bytes).json()
    first_file_id = first_import["file_id"]
    first_result = client.post(f"/files/{first_file_id}/process").json()
    assert first_result["summary"]["identification"]["status"] == "unknown"

    invalid_training = client.post(
        "/copilot/train",
        json={"file_id": first_file_id, "material_system": "Unknown"},
    )
    missing_context = client.post(
        "/copilot/chat",
        json={"file_id": "missing", "message": "What is this?"},
    )
    assert invalid_training.status_code == 422
    assert invalid_training.json()["detail"]["code"] == (
        "invalid_training_label"
    )
    assert missing_context.status_code == 404
    assert missing_context.json()["detail"]["code"] == "file_not_found"

    chat_response = client.post(
        "/copilot/chat",
        json={"file_id": first_file_id, "message": "What material is this?"},
    )
    train_response = client.post(
        "/copilot/train",
        json={
            "file_id": first_file_id,
            "material_system": "Testium phase A",
        },
    )

    assert chat_response.status_code == 200
    assert "Unknown" in chat_response.json()["assistant_message"]["content"]
    assert train_response.status_code == 201
    training = train_response.json()
    assert training["material_system"] == "Testium phase A"
    assert training["source_sha256"] == first_import["sha256"]
    assert training["result_sha256"] == first_result["result_sha256"]
    assert Path(first_import["storage_path"]).read_bytes() == original_bytes

    correction = client.post(
        "/copilot/train",
        json={
            "file_id": first_file_id,
            "material_system": "Testium corrected phase",
        },
    )
    assert correction.status_code == 201
    examples = client.get("/copilot/training").json()
    assert len(examples) == 2
    assert sum(example["active"] for example in examples) == 1
    assert examples[0]["material_system"] == "Testium corrected phase"
    assert examples[1]["active"] == 0

    related_bytes = b"COMMENT=independent measurement\n" + original_bytes
    second_import = post_import("related.txt", related_bytes).json()
    second_result = client.post(
        f"/files/{second_import['file_id']}/process"
    ).json()
    identification = second_result["summary"]["identification"]
    assert identification["status"] == "identified"
    assert identification["material_system"] == "Testium corrected phase"
    assert identification["candidates"][0]["reference"]["provider"] == (
        "user_confirmed_training"
    )
    assert Path(second_import["storage_path"]).read_bytes() == related_bytes

    self_result = client.post(f"/files/{first_file_id}/process").json()
    self_identification = self_result["summary"]["identification"]
    assert self_identification["status"] == "confirmed"
    assert self_identification["material_system"] == "Testium corrected phase"
    assert self_identification["confirmation"]["training_id"] == examples[0][
        "training_id"
    ]
    history = client.get(
        f"/copilot/messages?file_id={first_file_id}"
    ).json()
    assert [message["role"] for message in history] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
    ]


def test_external_ai_portal_keeps_keys_server_side_and_never_sends_raw_bytes(
    isolated_storage,
    monkeypatch,
):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    initial_status = client.get("/copilot/providers")
    unconfigured = client.post(
        "/copilot/chat",
        json={"provider": "openai", "message": "Hello"},
    )
    assert initial_status.status_code == 200
    assert initial_status.json()["raw_files_shared"] is False
    assert all(
        not provider["configured"]
        for provider in initial_status.json()["providers"]
        if provider["provider"] != "local"
    )
    assert unconfigured.status_code == 503
    assert unconfigured.json()["detail"]["code"] == "provider_not_configured"

    raw_bytes = synthetic_raman_bytes()
    imported = post_import("portal-spectrum.txt", raw_bytes).json()
    client.post(f"/files/{imported['file_id']}/process")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-secret")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-secret")
    requests = []

    class FakeResponse:
        status_code = 200
        is_success = True

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    def fake_post(url, headers, json, timeout):
        requests.append(
            {"url": url, "headers": headers, "json": json, "timeout": timeout}
        )
        if url == ai_providers.OPENAI_RESPONSES_URL:
            return FakeResponse(
                {
                    "id": "resp-test",
                    "model": ai_providers.OPENAI_MODEL,
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {"type": "output_text", "text": "OpenAI analysis"}
                            ],
                        }
                    ],
                    "usage": {"input_tokens": 10, "output_tokens": 2},
                }
            )
        return FakeResponse(
            {
                "id": "gemini-test",
                "model": ai_providers.GEMINI_MODEL,
                "steps": [
                    {
                        "type": "model_output",
                        "content": [{"type": "text", "text": "Gemini analysis"}],
                    }
                ],
                "usage": {"input_tokens": 10},
            }
        )

    monkeypatch.setattr(ai_providers.httpx, "post", fake_post)
    openai_response = client.post(
        "/copilot/chat",
        json={
            "provider": "openai",
            "file_id": imported["file_id"],
            "share_spectrum_context": True,
            "message": "Review this spectrum.",
        },
    )
    gemini_response = client.post(
        "/copilot/chat",
        json={
            "provider": "gemini",
            "file_id": imported["file_id"],
            "share_spectrum_context": True,
            "message": "Give a second opinion.",
        },
    )

    assert openai_response.status_code == 200
    assert gemini_response.status_code == 200
    assert openai_response.json()["assistant_message"]["content"] == (
        "OpenAI analysis"
    )
    assert gemini_response.json()["assistant_message"]["content"] == (
        "Gemini analysis"
    )
    assert requests[0]["json"]["store"] is False
    assert requests[1]["url"] == ai_providers.GEMINI_INTERACTIONS_URL
    assert requests[1]["json"]["store"] is False
    gemini_parts = requests[1]["json"]["input"]["parts"]
    assert len(gemini_parts) == 1
    assert gemini_parts[0]["text"].startswith("Give a second opinion.")
    serialized_requests = json.dumps(requests)
    assert "<measurement_context>" in serialized_requests
    assert raw_bytes.decode("utf-8") not in serialized_requests
    assert "test-openai-secret" in requests[0]["headers"]["Authorization"]
    assert requests[1]["headers"]["x-goog-api-key"] == "test-gemini-secret"

    status_payload = json.dumps(client.get("/copilot/providers").json())
    history_payload = json.dumps(
        client.get(
            f"/copilot/messages?file_id={imported['file_id']}"
        ).json()
    )
    assert "test-openai-secret" not in status_payload + history_payload
    assert "test-gemini-secret" not in status_payload + history_payload


def test_list_and_retrieve_imported_file_metadata(isolated_storage):
    older_file = {
        "file_id": "11111111-1111-1111-1111-111111111111",
        "original_filename": "older.csv",
        "content_type": "text/csv",
        "size_bytes": 12,
        "sha256": "a" * 64,
        "storage_path": "raw/older.csv",
        "imported_at": "2026-07-28T01:02:03+00:00",
        "technique": "Raman",
        "material_system": "Unknown",
        "sample_id": "SAMPLE-OLD",
        "measurement_date": "2026-07-27",
        "instrument": None,
        "operator": None,
        "notes": None,
    }
    newer_file = {
        "file_id": "22222222-2222-2222-2222-222222222222",
        "original_filename": "newer.bin",
        "content_type": "application/octet-stream",
        "size_bytes": 256,
        "sha256": "b" * 64,
        "storage_path": "raw/newer.bin",
        "imported_at": "2026-07-29T04:05:06+00:00",
        "technique": "XRD",
        "material_system": "TiO2",
        "sample_id": "SAMPLE-NEW",
        "measurement_date": None,
        "instrument": "Diffractometer",
        "operator": "A. Researcher",
        "notes": "Baseline scan",
    }

    with database.connect_database() as connection:
        database.insert_imported_file(connection, newer_file)
        database.insert_imported_file(connection, older_file)

    list_response = client.get("/files")
    retrieve_response = client.get(f"/files/{older_file['file_id']}")

    assert list_response.status_code == 200
    default_state = {
        "relative_path": None,
        "substrate": "unknown",
        "measurement_role": "unspecified",
        "data_category": "raw_measurement",
        "updated_at": None,
        "archived_at": None,
    }
    assert list_response.json() == [
        {**newer_file, **default_state},
        {**older_file, **default_state},
    ]
    assert retrieve_response.status_code == 200
    assert retrieve_response.json() == {**older_file, **default_state}


def test_retrieve_imported_file_returns_clear_not_found(isolated_storage):
    response = client.get("/files/missing-id")

    assert response.status_code == 404
    assert response.json()["detail"] == {
        "code": "file_not_found",
        "message": "No imported file has the requested ID.",
    }


def test_metadata_edit_and_archive_are_traceable_and_preserve_raw_bytes(
    isolated_storage,
):
    raw_bytes = synthetic_raman_bytes()
    imported = post_import("editable.txt", raw_bytes).json()
    immutable = {
        key: imported[key]
        for key in (
            "original_filename",
            "content_type",
            "size_bytes",
            "sha256",
            "storage_path",
            "imported_at",
        )
    }

    edited_response = client.patch(
        f"/files/{imported['file_id']}",
        json={
            "sample_id": "EDITED-01",
            "notes": "Reviewed metadata only",
            "substrate": "sio2_si",
            "measurement_role": "sample_on_substrate",
        },
    )

    assert edited_response.status_code == 200
    edited = edited_response.json()
    assert edited["sample_id"] == "EDITED-01"
    assert edited["substrate"] == "sio2_si"
    assert edited["measurement_role"] == "sample_on_substrate"
    assert edited["updated_at"]
    assert {key: edited[key] for key in immutable} == immutable
    assert Path(imported["storage_path"]).read_bytes() == raw_bytes
    history = client.get(
        f"/files/{imported['file_id']}/metadata/history"
    ).json()
    assert len(history) == 1
    assert history[0]["action"] == "edit"
    assert history[0]["previous"]["sample_id"] == "SAMPLE-001"
    assert history[0]["updated"]["sample_id"] == "EDITED-01"
    assert history[0]["updated"]["measurement_role"] == "sample_on_substrate"

    invalid = client.patch(
        f"/files/{imported['file_id']}",
        json={"original_filename": "replacement.txt"},
    )
    archived_response = client.delete(f"/files/{imported['file_id']}")

    assert invalid.status_code == 422
    assert archived_response.status_code == 200
    assert archived_response.json()["archived_at"]
    assert "Raw bytes" in archived_response.json()["retention"]
    assert client.get("/files").json() == []
    archived_files = client.get("/files?include_archived=true").json()
    assert len(archived_files) == 1
    assert Path(imported["storage_path"]).read_bytes() == raw_bytes
    duplicate = post_import("duplicate-after-archive.txt", raw_bytes)
    assert duplicate.status_code == 409

    restored = client.post(f"/files/{imported['file_id']}/restore")
    assert restored.status_code == 200
    assert restored.json()["archived_at"] is None
    assert len(client.get("/files").json()) == 1
    actions = [
        revision["action"]
        for revision in client.get(
            f"/files/{imported['file_id']}/metadata/history"
        ).json()
    ]
    assert actions == ["edit", "archive", "restore"]
    assert Path(imported["storage_path"]).read_bytes() == raw_bytes


def test_import_preserves_bytes_sanitizes_name_and_persists_metadata(
    isolated_storage,
):
    _, temporary_raw_directory = isolated_storage
    original_bytes = (
        bytes(range(256)) * ((1024 * 1024 // 256) + 1)
        + b"\x00\xff\r\nRaman shift,intensity\n380.0,125\n"
    )
    expected_checksum = hashlib.sha256(original_bytes).hexdigest()

    response = post_import(
        "../../unsafe?.csv",
        original_bytes,
        {**DEFAULT_METADATA, "relative_path": "experiment-42/run-a/unsafe?.csv"},
    )

    assert response.status_code == 201
    result = response.json()
    assert result["original_filename"] == "unsafe?.csv"
    assert result["content_type"] == "application/octet-stream"
    assert result["size_bytes"] == len(original_bytes)
    assert result["sha256"] == expected_checksum
    assert result["imported_at"]
    assert result["relative_path"] == "experiment-42/run-a/unsafe?.csv"
    for field_name, value in DEFAULT_METADATA.items():
        assert result[field_name] == value
    assert result["substrate"] == "unknown"
    assert result["updated_at"] is None
    assert result["archived_at"] is None

    stored_file = (
        temporary_raw_directory
        / result["file_id"]
        / "unsafe_.csv"
    )
    assert result["storage_path"] == str(stored_file)
    assert stored_file.read_bytes() == original_bytes

    with database.connect_database() as connection:
        record = connection.execute(
            "SELECT * FROM imported_files WHERE file_id = ?",
            (result["file_id"],),
        ).fetchone()
    assert dict(record) == result
    assert client.get(f"/files/{result['file_id']}").json() == result


def test_optical_image_attachment_preserves_bytes_and_links_metadata(
    isolated_storage,
):
    raw_bytes = synthetic_raman_bytes()
    imported = post_import("paired-spectrum.txt", raw_bytes).json()
    image_bytes = b"\xff\xd8\xff\xe0immutable optical image bytes\xff\xd9"

    response = client.post(
        f"/files/{imported['file_id']}/attachments",
        files={"file": ("paired-spectrum.jpg", image_bytes, "image/jpeg")},
    )

    assert response.status_code == 201
    attachment = response.json()
    assert attachment["file_id"] == imported["file_id"]
    assert attachment["kind"] == "optical_image"
    assert attachment["original_filename"] == "paired-spectrum.jpg"
    assert attachment["content_type"] == "image/jpeg"
    assert attachment["size_bytes"] == len(image_bytes)
    assert attachment["sha256"] == hashlib.sha256(image_bytes).hexdigest()
    assert Path(attachment["storage_path"]).read_bytes() == image_bytes
    assert Path(imported["storage_path"]).read_bytes() == raw_bytes

    listed = client.get(f"/files/{imported['file_id']}/attachments")
    content = client.get(attachment["content_url"])
    duplicate = client.post(
        f"/files/{imported['file_id']}/attachments",
        files={"file": ("copy.jpg", image_bytes, "image/jpeg")},
    )

    assert listed.status_code == 200
    assert listed.json()[0]["attachment_id"] == attachment["attachment_id"]
    assert content.status_code == 200
    assert content.content == image_bytes
    assert content.headers["content-type"] == "image/jpeg"
    assert content.headers["x-content-type-options"] == "nosniff"
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "duplicate_attachment"
    with database.connect_database() as connection:
        stored = connection.execute(
            "SELECT * FROM file_attachments WHERE attachment_id = ?",
            (attachment["attachment_id"],),
        ).fetchone()
    assert stored["sha256"] == attachment["sha256"]
    assert stored["file_id"] == imported["file_id"]


def test_invalid_optical_image_and_database_failure_leave_no_attachment(
    isolated_storage,
    monkeypatch,
):
    imported = post_import("attachment-failure.txt", synthetic_raman_bytes()).json()
    invalid = client.post(
        f"/files/{imported['file_id']}/attachments",
        files={"file": ("not-an-image.jpg", b"plain text", "image/jpeg")},
    )

    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "invalid_optical_image"

    def fail_insert(_connection, _attachment):
        raise sqlite3.OperationalError("simulated attachment insert failure")

    monkeypatch.setattr(database, "insert_file_attachment", fail_insert)
    image_bytes = b"\xff\xd8\xff\xe0valid staged bytes\xff\xd9"
    failed = client.post(
        f"/files/{imported['file_id']}/attachments",
        files={"file": ("failure.jpg", image_bytes, "image/jpeg")},
    )

    assert failed.status_code == 500
    assert failed.json()["detail"]["code"] == "attachment_import_failed"
    attachment_directory = Path(imported["storage_path"]).parent / "attachments"
    assert not attachment_directory.exists() or not any(
        path.is_file() for path in attachment_directory.rglob("*")
    )
    with database.connect_database() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM file_attachments"
        ).fetchone()[0] == 0


def test_preview_parses_raman_xydata_and_verifies_checksum(isolated_storage):
    raman_bytes = b"""FILETYPE=RAMAN SPECTRUM
LASER=532
TYPE=RamanShift
XUNITS=1/cm
LEN=3
XYDATA=
20.285,350.21
22.537,352.36
24.789,350.57
"""
    import_response = post_import("CNT-01.txt", raman_bytes)

    response = client.get(
        f"/files/{import_response.json()['file_id']}/preview"
    )

    assert import_response.status_code == 201
    assert response.status_code == 200
    assert response.json() == {
        "file_id": import_response.json()["file_id"],
        "original_filename": "CNT-01.txt",
        "technique": DEFAULT_METADATA["technique"],
        "material_system": DEFAULT_METADATA["material_system"],
        "sample_id": DEFAULT_METADATA["sample_id"],
        "sha256": hashlib.sha256(raman_bytes).hexdigest(),
        "checksum_verified": True,
        "x_label": "Raman shift (1/cm)",
        "y_label": "Intensity",
        "point_count": 3,
        "displayed_point_count": 3,
        "points": [
            [20.285, 350.21],
            [22.537, 352.36],
            [24.789, 350.57],
        ],
    }


def test_wdf_inspection_lists_embedded_spectra_without_importing(isolated_storage):
    raw_bytes = synthetic_wdf_bytes()

    response = client.post(
        "/wdf/inspect",
        files={"file": ("series.wdf", raw_bytes, "application/octet-stream")},
    )

    assert response.status_code == 200
    result = response.json()
    assert result["original_filename"] == "series.wdf"
    assert result["size_bytes"] == len(raw_bytes)
    assert result["sha256"] == hashlib.sha256(raw_bytes).hexdigest()
    assert result["format"] == "Renishaw WiRE WDF"
    assert result["technique"] == "Raman spectroscopy"
    assert result["spectrum_count"] == 2
    assert result["point_count_per_spectrum"] == 3
    assert [dataset["dataset_index"] for dataset in result["datasets"]] == [0, 1]
    assert [dataset["point_count"] for dataset in result["datasets"]] == [3, 3]
    assert not list((isolated_storage[1] / ".staging").glob("*.wdf-inspection"))
    assert database.list_imported_files() == []


def test_wdf_import_preserves_raw_bytes_and_previews_selected_spectrum(
    isolated_storage,
):
    raw_bytes = synthetic_wdf_bytes()
    import_response = post_import("series.wdf", raw_bytes)
    assert import_response.status_code == 201
    imported = import_response.json()

    preview_response = client.get(
        f"/files/{imported['file_id']}/preview?dataset_index=1"
    )

    assert preview_response.status_code == 200
    result = preview_response.json()
    assert result["dataset_index"] == 1
    assert result["dataset_id"] == "spectrum-2"
    assert result["point_count"] == 3
    assert result["points"] == [[100.0, 8.0], [200.0, 18.0], [300.0, 30.0]]
    assert len(result["available_datasets"]) == 2
    assert result["wdf"]["technique"] == "Raman spectroscopy"

    stored_path = Path(imported["storage_path"])
    assert stored_path.read_bytes() == raw_bytes
    assert imported["size_bytes"] == len(raw_bytes)
    assert imported["sha256"] == hashlib.sha256(raw_bytes).hexdigest()
    assert imported["original_filename"] == "series.wdf"
    assert imported["content_type"] == "application/octet-stream"
    assert stored_path == isolated_storage[1] / imported["file_id"] / "series.wdf"
    with sqlite3.connect(isolated_storage[0]) as connection:
        row = connection.execute(
            "SELECT original_filename, content_type, size_bytes, sha256, "
            "storage_path, imported_at FROM imported_files WHERE file_id = ?",
            (imported["file_id"],),
        ).fetchone()
    assert row == (
        "series.wdf",
        "application/octet-stream",
        len(raw_bytes),
        hashlib.sha256(raw_bytes).hexdigest(),
        str(stored_path),
        imported["imported_at"],
    )


def test_wdf_preview_rejects_unknown_embedded_spectrum(isolated_storage):
    imported = post_import("series.wdf", synthetic_wdf_bytes()).json()

    response = client.get(
        f"/files/{imported['file_id']}/preview?dataset_index=2"
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "wdf_dataset_not_found"


def test_wdf_processing_uses_selected_embedded_spectrum(isolated_storage):
    imported = post_import("series.wdf", synthetic_wdf_bytes()).json()

    response = client.post(
        f"/files/{imported['file_id']}/process",
        json={"dataset_index": 1},
    )

    assert response.status_code == 200
    result = response.json()
    assert result["model"]["version"] == "2.39.0"
    assert result["model"]["parameters"]["wdf_dataset_index"] == 1
    assert result["series"]["raw"] == [
        [100.0, 8.0],
        [200.0, 18.0],
        [300.0, 30.0],
    ]


def test_preview_rejects_checksum_mismatch(isolated_storage):
    import_response = post_import("spectrum.csv", b"x,y\n1,2\n2,4\n")
    result = import_response.json()
    Path(result["storage_path"]).write_bytes(b"changed test bytes")

    response = client.get(f"/files/{result['file_id']}/preview")

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "checksum_mismatch",
        "message": "The stored file checksum does not match its metadata record.",
    }


def test_preview_rejects_file_outside_managed_raw_directory(
    isolated_storage,
    tmp_path,
):
    outside_file = tmp_path / "outside.csv"
    outside_bytes = b"x,y\n1,2\n2,4\n"
    outside_file.write_bytes(outside_bytes)
    metadata = {
        "file_id": "outside-file",
        "original_filename": outside_file.name,
        "content_type": "text/csv",
        "size_bytes": len(outside_bytes),
        "sha256": hashlib.sha256(outside_bytes).hexdigest(),
        "storage_path": str(outside_file),
        "imported_at": "2026-07-29T00:00:00+00:00",
        "technique": "Raman",
        "material_system": "Unknown",
        "sample_id": "SAMPLE-OUTSIDE",
        "measurement_date": None,
        "instrument": None,
        "operator": None,
        "notes": None,
    }
    with database.connect_database() as connection:
        database.insert_imported_file(connection, metadata)

    response = client.get("/files/outside-file/preview")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "invalid_storage_path"


def test_raman_processing_persists_provenance_and_is_idempotent(
    isolated_storage,
):
    raw_bytes = synthetic_raman_bytes()
    import_response = post_import("synthetic-raman.txt", raw_bytes)
    file_id = import_response.json()["file_id"]

    first_response = client.post(f"/files/{file_id}/process")
    second_response = client.post(f"/files/{file_id}/process")
    latest_response = client.get(f"/files/{file_id}/processing/latest")

    assert first_response.status_code == 200
    result = first_response.json()
    assert second_response.json() == result
    assert latest_response.json() == result
    assert result["source_sha256"] == hashlib.sha256(raw_bytes).hexdigest()
    assert result["result_sha256"]
    assert result["model"]["name"] == "raman_material_identification"
    assert result["model"]["version"] == "2.39.0"
    assert result["model"]["parameters"]["smoothing_method"] == "none"
    assert result["model"]["parameters"]["deconvolution_profile"] == "none"
    assert result["model"]["parameters"]["substrate_correction_mode"] == "none"
    assert result["substrate_correction"] == {
        "status": "disabled",
        "requested_mode": "none",
        "residual_peaks": [],
        "residual_peak_count": 0,
        "components": [],
    }
    assert result["deconvolution"] == {
        "status": "disabled",
        "profile": "none",
        "components": [],
    }
    assert "smoothed" not in result["series"]
    assert result["summary"]["artifacts"] == []
    assert result["point_count"] == 401
    assert result["summary"]["identification"]["status"] == "unknown"
    assert result["summary"]["identification"]["material_system"] is None
    assert result["summary"]["material_analysis"] is None
    assert result["spectrum_quality"]["badge"] == "good"
    assert result["spectrum_quality"]["interpretation_eligible"] is True
    assert result["processing_protocol"]["stage_order_is_mandatory"] is True
    assert [
        stage["id"] for stage in result["processing_protocol"]["stages"]
    ] == [
        "axis_validation",
        "instrument_artifact_correction",
        "baseline_subtraction",
        "reference_screening",
        "substrate_correction",
        "peak_assignment",
        "deconvolution",
        "material_interpretation",
        "quality_gate",
    ]
    assert Path(import_response.json()["storage_path"]).read_bytes() == raw_bytes

    with database.connect_database() as connection:
        runs = connection.execute("SELECT * FROM processing_runs").fetchall()
    assert len(runs) == 1
    result_path = Path(runs[0]["result_path"])
    assert result_path.is_file()
    assert hashlib.sha256(result_path.read_bytes()).hexdigest() == result[
        "result_sha256"
    ]


def test_spectrum_quality_gate_blocks_flat_topped_detector_saturation():
    x_values = [float(index) for index in range(101)]
    intensities = [float(index % 11) for index in range(101)]
    intensities[48:51] = [100.0, 100.0, 100.0]

    quality = processing.assess_spectrum_quality(
        x_values,
        intensities,
        intensities,
    )

    assert quality["badge"] == "review"
    assert quality["interpretation_eligible"] is False
    assert quality["blocking_check_ids"] == ["detector_saturation"]
    saturation_check = next(
        check
        for check in quality["checks"]
        if check["id"] == "detector_saturation"
    )
    assert saturation_check["status"] == "review_required"


def test_rubber_band_baseline_is_a_lower_piecewise_linear_hull():
    x_values = [0.0, 1.0, 2.0, 3.0, 4.0]
    intensities = [2.0, 5.0, 1.0, 4.0, 2.0]

    baseline = processing.rubber_band_baseline(x_values, intensities)

    assert baseline == pytest.approx([2.0, 1.5, 1.0, 1.5, 2.0])
    assert all(
        baseline_value <= intensity
        for baseline_value, intensity in zip(baseline, intensities)
    )


def test_processing_accepts_rubber_band_baseline_and_reports_protocol(
    isolated_storage,
):
    imported = post_import("rubber-band.txt", synthetic_raman_bytes()).json()

    response = client.post(
        f"/files/{imported['file_id']}/process",
        json={"baseline_method": "rubber_band"},
    )

    assert response.status_code == 200
    result = response.json()
    assert result["model"]["parameters"]["baseline_method"] == "rubber_band"
    baseline_stage = next(
        stage
        for stage in result["processing_protocol"]["stages"]
        if stage["id"] == "baseline_subtraction"
    )
    assert baseline_stage["method"] == "rubber_band"
    assert "lower convex hull" in baseline_stage["detail"]

    invalid = client.post(
        f"/files/{imported['file_id']}/process",
        json={"baseline_method": "polynomial_guess"},
    )
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "invalid_baseline_method"


def test_explicit_pure_substrate_measurement_role_is_persisted_and_validated(
    isolated_storage,
):
    metadata = {
        **DEFAULT_METADATA,
        "material_system": "Control wafer",
        "substrate": "sio2_si",
        "measurement_role": "pure_substrate_reference",
    }

    response = post_import(
        "explicit-pure-reference.txt",
        synthetic_raman_bytes(),
        metadata,
    )

    assert response.status_code == 201
    imported = response.json()
    assert imported["measurement_role"] == "pure_substrate_reference"
    assert processing.confirmed_substrate_reference(imported) == "sio2_si"

    invalid = post_import(
        "invalid-pure-reference.txt",
        b"different experimental bytes",
        {
            **metadata,
            "sample_id": "INVALID-REFERENCE",
            "substrate": "unknown",
        },
    )
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "substrate_required_for_reference"


def test_advanced_processing_policies_are_validated_and_recorded(
    isolated_storage,
):
    imported = post_import("advanced-policy.txt", synthetic_raman_bytes()).json()
    request = {
        "substrate_reference_strategy": "analytical_only",
        "quality_policy": "conservative",
        "peak_review_policy": "require_all_checks",
        "ensemble_minimum_coverage": 0.9,
        "ensemble_consensus_threshold": 0.7,
        "ensemble_minimum_r_squared": 0.8,
    }

    response = client.post(
        f"/files/{imported['file_id']}/process",
        json=request,
    )

    assert response.status_code == 200
    result = response.json()
    parameters = result["model"]["parameters"]
    assert parameters["substrate_reference_strategy"] == "analytical_only"
    assert parameters["quality_policy"] == "conservative"
    assert parameters["peak_review_policy"] == "require_all_checks"
    assert parameters["substrate_ensemble_minimum_coverage_fraction"] == 0.9
    assert parameters["substrate_ensemble_support_fraction_threshold"] == 0.7
    assert parameters["substrate_auto_minimum_fit_r_squared"] == 0.8
    quality_stage = next(
        stage
        for stage in result["processing_protocol"]["stages"]
        if stage["id"] == "quality_gate"
    )
    assert "conservative quality policy" in quality_stage["method"]

    invalid = client.post(
        f"/files/{imported['file_id']}/process",
        json={**request, "ensemble_minimum_coverage": 1.2},
    )
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == (
        "invalid_advanced_processing_threshold"
    )


def test_analysis_range_and_peak_threshold_are_applied_and_recorded(
    isolated_storage,
):
    raw_bytes = synthetic_raman_bytes()
    imported = post_import("bounded-analysis.txt", raw_bytes).json()

    response = client.post(
        f"/files/{imported['file_id']}/process",
        json={
            "analysis_range_min_cm_1": 1300,
            "analysis_range_max_cm_1": 1600,
            "peak_detection_threshold_percent": 5,
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert result["point_count"] == 151
    assert result["series"]["raw"][0][0] == 1300
    assert result["series"]["raw"][-1][0] == 1600
    assert result["model"]["parameters"]["analysis_range_min_cm-1"] == 1300
    assert result["model"]["parameters"]["analysis_range_max_cm-1"] == 1600
    assert result["model"]["parameters"]["minimum_prominence_fraction"] == 0.05
    assert Path(imported["storage_path"]).read_bytes() == raw_bytes
    axis_stage = result["processing_protocol"]["stages"][0]
    assert "1300.0 to 1600.0 cm-1" in axis_stage["detail"]

    invalid = client.post(
        f"/files/{imported['file_id']}/process",
        json={
            "analysis_range_min_cm_1": 1600,
            "analysis_range_max_cm_1": 1300,
        },
    )
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "invalid_analysis_range"


def test_peak_review_policy_controls_nonfatal_quality_reviews():
    x_values = [0.0, 1.0, 3.0, 4.0, 6.0]
    intensities = [1.0, 2.0, 4.0, 3.0, 2.0]

    flagged = processing.assess_spectrum_quality(
        x_values,
        intensities,
        intensities,
        {"quality_policy": "balanced", "peak_review_policy": "flag_only"},
    )
    required = processing.assess_spectrum_quality(
        x_values,
        intensities,
        intensities,
        {
            "quality_policy": "balanced",
            "peak_review_policy": "require_all_checks",
        },
    )

    assert flagged["badge"] == "review"
    assert flagged["interpretation_eligible"] is True
    assert required["interpretation_eligible"] is False
    assert required["blocking_check_ids"] == ["axis_spacing"]


def test_measured_substrate_reference_ensemble_preserves_material_peak():
    x_values = [float(value) for value in range(100, 701, 2)]

    def gaussian(center, width, height):
        return [
            height * math.exp(-0.5 * ((value - center) / width) ** 2)
            for value in x_values
        ]

    silicon = gaussian(520.0, 7.0, 100.0)
    substrate_band = gaussian(300.0, 20.0, 28.0)
    reference = [left + right for left, right in zip(silicon, substrate_band)]
    material_peak = gaussian(385.0, 5.0, 55.0)
    target = [
        1.6 * substrate + material
        for substrate, material in zip(reference, material_peak)
    ]
    result = processing.fit_substrate_reference_ensemble(
        x_values,
        target,
        [
            {
                "position_cm-1": 385.0,
                "intensity": max(material_peak),
                "prominence": max(material_peak),
                "assignments": [
                    {
                        "material_system": "MoS2",
                        "label": "E2g1 in-plane mode",
                    }
                ],
            }
        ],
        "sio2_si",
        [
            {
                "file_id": "pure-substrate",
                "filename": "pure-sio2-si.txt",
                "source_sha256": "a" * 64,
                "x_values": x_values,
                "corrected_values": reference,
            }
        ],
        {
            **processing.MODEL_PARAMETERS,
            "import_metadata": {"material_system": "MoS2"},
        },
    )

    assert result is not None
    assert result["quality"]["accepted"] is True
    assert result["reference_ensemble"]["reference_count"] == 1
    material_index = x_values.index(384.0)
    silicon_index = x_values.index(520.0)
    assert result["corrected_values"][material_index] > 40.0
    assert result["fit_values"][silicon_index] == pytest.approx(
        target[silicon_index],
        rel=0.03,
    )


def test_confirmed_pure_substrate_uses_leave_one_out_zero_residual(
    isolated_storage,
):
    metadata = {
        **DEFAULT_METADATA,
        "material_system": "SiO2 / crystalline Si",
        "substrate": "sio2_si",
    }
    first = post_import(
        "pure-substrate-one.txt",
        synthetic_raman_bytes(),
        metadata,
    ).json()
    second_bytes = synthetic_raman_bytes().replace(
        b"1000,100.00000000",
        b"1000,100.10000000",
    )
    second = post_import(
        "pure-substrate-two.txt",
        second_bytes,
        {**metadata, "sample_id": "SAMPLE-002"},
    ).json()

    result = client.post(
        f"/files/{first['file_id']}/process",
        json={"substrate_correction": "auto"},
    ).json()

    substrate = result["substrate_correction"]
    assert substrate["status"] == "subtracted"
    assert substrate["correction"] == "measured_reference_ensemble"
    assert substrate["substrate_only_validation"]["final_zero_residual_passed"] is True
    assert max(value for _x, value in result["series"]["substrate_corrected"]) == 0.0
    reference_ids = {
        reference["file_id"]
        for reference in substrate["reference_ensemble"]["references"]
    }
    assert first["file_id"] not in reference_ids
    assert second["file_id"] in reference_ids


def test_substrate_residual_feedback_is_persisted_and_reused(isolated_storage):
    imported = post_import("feedback-spectrum.txt", synthetic_raman_bytes()).json()
    processed = client.post(
        f"/files/{imported['file_id']}/process",
        json={"substrate_correction": "sio2_si"},
    )
    assert processed.status_code == 200

    response = client.post(
        f"/files/{imported['file_id']}/substrate-residual-feedback",
        json={
            "center_cm_1": 1350.0,
            "half_width_cm_1": 12.0,
            "action": "keep",
        },
    )

    assert response.status_code == 201
    feedback = response.json()
    assert feedback["action"] == "keep"
    assert feedback["reprocess_required"] is True
    stored = database.list_substrate_peak_feedback(
        material_system=imported["material_system"],
    )
    assert len(stored) == 1
    assert stored[0]["center_cm_1"] == pytest.approx(1350.0)

    reprocessed = client.post(
        f"/files/{imported['file_id']}/process",
        json={"substrate_correction": "sio2_si"},
    ).json()
    assert reprocessed["model"]["parameters"]["substrate_peak_feedback"][0][
        "action"
    ] == "keep"


def test_batch_archive_preserves_raw_files_and_processing_history(isolated_storage):
    first = post_import("archive-one.txt", synthetic_raman_bytes()).json()
    second_bytes = synthetic_raman_bytes().replace(
        b"1000,100.00000000",
        b"1000,100.10000000",
    )
    second = post_import("archive-two.txt", second_bytes).json()
    first_result = client.post(f"/files/{first['file_id']}/process").json()

    response = client.post(
        "/files/archive-batch",
        json={"file_ids": [first["file_id"], second["file_id"]]},
    )

    assert response.status_code == 200
    assert response.json()["archived_count"] == 2
    assert client.get("/files").json() == []
    assert Path(first["storage_path"]).read_bytes() == synthetic_raman_bytes()
    replay = client.get(f"/processing-runs/{first_result['processing_id']}")
    assert replay.status_code == 200
    assert replay.json()["result"]["result_sha256"] == first_result[
        "result_sha256"
    ]
    with database.connect_database() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM import_metadata_revisions WHERE action = 'archive'"
        ).fetchone()[0] == 2


def test_reproducibility_export_contains_verified_raw_data_and_analysis(
    isolated_storage,
):
    raw_bytes = synthetic_raman_bytes()
    imported = post_import("reproducible-spectrum.txt", raw_bytes).json()
    processed = client.post(
        f"/files/{imported['file_id']}/process",
        json={"deconvolution": "pseudo_voigt"},
    ).json()

    response = client.get(
        f"/files/{imported['file_id']}/export/reproducibility"
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "reproducibility.zip" in response.headers["content-disposition"]
    assert response.headers["x-content-type-options"] == "nosniff"
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        assert {
            "README.md",
            "raw/reproducible-spectrum.txt",
            "metadata/import.json",
            "metadata/revisions.json",
            "analysis/result.json",
            "analysis/processing-run.json",
            "analysis/plot-series.csv",
            "analysis/components.csv",
            "analysis/peaks.csv",
            "analysis/clarifications.json",
            "analysis/training.json",
            "environment/requirements.txt",
            "manifest.json",
        } <= names
        assert archive.read("raw/reproducible-spectrum.txt") == raw_bytes
        metadata = json.loads(archive.read("metadata/import.json"))
        assert metadata["sha256"] == hashlib.sha256(raw_bytes).hexdigest()
        assert metadata["size_bytes"] == len(raw_bytes)
        assert "storage_path" not in metadata
        result = json.loads(archive.read("analysis/result.json"))
        run = json.loads(archive.read("analysis/processing-run.json"))
        assert result["processing_id"] == processed["processing_id"]
        assert run["result_sha256"] == processed["result_sha256"]
        assert run["parameters"]["deconvolution_profile"] == "pseudo_voigt"
        plot_csv = archive.read("analysis/plot-series.csv").decode("utf-8")
        plot_columns = plot_csv.splitlines()[0].split(",")
        assert plot_columns[0] == "raman_shift_cm-1"
        assert {
            "raw",
            "artifact_corrected",
            "baseline",
            "corrected",
            "deconvolved_fit",
            "deconvolution_residual",
        } <= set(plot_columns)

        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["bundle_format_version"] == "1.0"
        assert manifest["file_id"] == imported["file_id"]
        for entry in manifest["files"]:
            contents = archive.read(entry["path"])
            assert len(contents) == entry["size_bytes"]
            assert hashlib.sha256(contents).hexdigest() == entry["sha256"]

    assert Path(imported["storage_path"]).read_bytes() == raw_bytes


def test_reproducibility_export_requires_processing_and_verified_raw_bytes(
    isolated_storage,
):
    raw_bytes = synthetic_raman_bytes()
    imported = post_import("export-validation.txt", raw_bytes).json()

    unprocessed = client.get(
        f"/files/{imported['file_id']}/export/reproducibility"
    )
    assert unprocessed.status_code == 409
    assert unprocessed.json()["detail"]["code"] == "processing_required"

    assert client.post(f"/files/{imported['file_id']}/process").status_code == 200
    tampered_bytes = bytearray(raw_bytes)
    tampered_bytes[-2] ^= 1
    Path(imported["storage_path"]).write_bytes(tampered_bytes)
    mismatch = client.get(
        f"/files/{imported['file_id']}/export/reproducibility"
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["detail"]["code"] == "checksum_mismatch"


def test_batch_processing_applies_one_traceable_recipe_without_changing_raw_data(
    isolated_storage,
):
    first_bytes = synthetic_raman_bytes()
    second_bytes = synthetic_two_peak_bytes(1010.0, 1320.0)
    first = post_import("batch-first.txt", first_bytes).json()
    second = post_import("batch-second.txt", second_bytes).json()

    response = client.post(
        "/files/process/batch",
        json={
            "file_ids": [first["file_id"], first["file_id"], second["file_id"]],
            "deconvolution": "pseudo_voigt",
            "substrate_correction": "detect",
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert result["requested_count"] == 2
    assert result["completed_count"] == 2
    assert result["failed_count"] == 0
    assert result["parameters"] == {
        "deconvolution": "pseudo_voigt",
        "substrate_correction": "detect",
        "smoothing": "none",
    }
    assert {item["file_id"] for item in result["items"]} == {
        first["file_id"],
        second["file_id"],
    }
    assert all(item["status"] == "completed" for item in result["items"])
    assert all(item["source_sha256"] for item in result["items"])
    assert all(item["result_sha256"] for item in result["items"])
    comparison = result["series_comparison"]
    assert comparison["compared_count"] == 2
    assert comparison["method"]["smoothing"] == "none"
    assert len(comparison["pairwise_similarity"]) == 1
    assert comparison["pairwise_similarity"][0]["correlation"] is not None
    assert isinstance(comparison["recurring_peaks"], list)
    assert comparison["variable_peaks"]
    with database.connect_database() as connection:
        runs = connection.execute(
            "SELECT file_id, parameters_json FROM processing_runs"
        ).fetchall()
    assert len(runs) == 2
    assert all(
        json.loads(run["parameters_json"])["deconvolution_profile"]
        == "pseudo_voigt"
        for run in runs
    )
    assert Path(first["storage_path"]).read_bytes() == first_bytes
    assert Path(second["storage_path"]).read_bytes() == second_bytes


def test_batch_processing_prevalidates_selection_and_parameters(
    isolated_storage,
):
    imported = post_import("batch-validation.txt", synthetic_raman_bytes()).json()

    missing = client.post(
        "/files/process/batch",
        json={"file_ids": [imported["file_id"], "missing-file"]},
    )
    invalid = client.post(
        "/files/process/batch",
        json={
            "file_ids": [imported["file_id"]],
            "deconvolution": "invented",
        },
    )

    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "batch_file_not_found"
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == (
        "unsupported_deconvolution_profile"
    )
    with database.connect_database() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM processing_runs"
        ).fetchone()[0] == 0


def test_optional_peak_deconvolution_is_traceable_and_non_destructive(
    isolated_storage,
):
    raw_bytes = synthetic_raman_bytes()
    imported = post_import("deconvolution.txt", raw_bytes).json()
    file_id = imported["file_id"]

    first = client.post(
        f"/files/{file_id}/process",
        json={"deconvolution": "pseudo_voigt"},
    )
    repeated = client.post(
        f"/files/{file_id}/process",
        json={"deconvolution": "pseudo_voigt"},
    )
    invalid = client.post(
        f"/files/{file_id}/process",
        json={"deconvolution": "unsupported"},
    )

    assert first.status_code == 200
    result = first.json()
    assert repeated.json() == result
    assert result["model"]["parameters"]["deconvolution_profile"] == (
        "pseudo_voigt"
    )
    assert result["deconvolution"]["status"] == "fitted"
    assert result["deconvolution"]["profile"] == "pseudo_voigt"
    assert len(result["deconvolution"]["components"]) == 2
    assert result["summary"]["deconvolution"]["component_count"] == 2
    assert result["summary"]["deconvolution"]["quality"]["r_squared"] > 0.8
    assert "deconvolved_fit" in result["series"]
    assert "deconvolution_residual" in result["series"]
    assert result["summary"]["deconvolution"]["quality"][
        "normalized_rmse"
    ] < 0.2
    quality = result["summary"]["deconvolution"]["quality"]
    assert quality["accepted"] is True
    assert quality["acceptance_status"] == "accepted"
    assert quality["maximum_component_residual_to_fit_ratio"] <= 0.25
    assert len(quality["component_checks"]) == 2
    assert result["summary"]["deconvolution"]["review_required"] is False
    assert all(
        component["fwhm_cm-1"] > 0
        and component["area"] > 0
        and abs(component["center_cm-1"] - component["seed_center_cm-1"]) <= 4
        for component in result["deconvolution"]["components"]
    )
    assert Path(imported["storage_path"]).read_bytes() == raw_bytes
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "invalid_deconvolution_profile"


def test_deconvolution_residual_gate_rejects_peak_scale_residuals():
    quality = processing.deconvolution_residual_quality(
        [0.0, 1.0, 2.0],
        [0.0, 10.0, 0.0],
        [0.0, 5.0, 0.0],
        [1.0],
        [1.0],
        [1.0],
        processing.MODEL_PARAMETERS,
    )

    assert quality["accepted"] is False
    assert quality["acceptance_status"] == "review_required"
    assert quality["global_residual_to_fit_ratio"] == pytest.approx(1.0)
    assert quality["component_checks"][0]["residual_to_fit_ratio"] == (
        pytest.approx(1.0)
    )
    assert quality["maximum_allowed_residual_to_fit_ratio"] == 0.25


def test_legacy_deconvolution_is_revalidated_without_changing_stored_data():
    result = {
        "model": {
            "name": processing.MODEL_NAME,
            "version": "2.32.0",
            "parameters": {},
        },
        "summary": {
            "deconvolution": {
                "status": "fitted",
                "quality": {"r_squared": 0.5},
            }
        },
        "series": {
            "corrected": [[0.0, 0.0], [1.0, 10.0], [2.0, 0.0]],
            "deconvolved_fit": [[0.0, 0.0], [1.0, 5.0], [2.0, 0.0]],
            "deconvolution_residual": [[0.0, 0.0], [1.0, 5.0], [2.0, 0.0]],
        },
        "deconvolution": {
            "status": "fitted",
            "quality": {"r_squared": 0.5},
            "components": [
                {
                    "seed_center_cm-1": 1.0,
                    "center_cm-1": 1.0,
                    "fwhm_cm-1": 1.0,
                }
            ],
        },
    }

    processing.revalidate_legacy_deconvolution(result)

    quality = result["deconvolution"]["quality"]
    assert quality["accepted"] is False
    assert quality["global_residual_to_fit_ratio"] == pytest.approx(1.0)
    assert quality["legacy_result_revalidated"] is True
    assert result["summary"]["deconvolution"]["review_required"] is True


def test_user_labelled_noise_peak_is_not_deconvoluted():
    result = processing.deconvolve_peaks(
        [0.0, 1.0, 2.0],
        [0.0, 10.0, 0.0],
        [
            {
                "position_cm-1": 1.0,
                "intensity": 10.0,
                "assignments": [{"label": "Noise", "material_system": "sample"}],
            }
        ],
        "pseudo_voigt",
        processing.MODEL_PARAMETERS,
    )

    assert result["status"] == "no_peaks"


def test_automatic_noise_gate_preserves_user_confirmed_peak():
    peak = {
        "position_cm-1": 3.0,
        "intensity": 1.0,
        "prominence": 0.1,
        "assignments": [
            {
                "material_system": "MoS2",
                "label": "Confirmed weak mode",
                "confidence": "user_confirmed",
            }
        ],
    }

    retained, excluded = processing.exclude_noise_indistinguishable_peaks(
        [peak],
        [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
        [0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0],
        processing.MODEL_PARAMETERS,
    )

    assert retained == [peak]
    assert excluded == []


def test_fit_component_edit_is_confirmed_reprocessed_and_learned(
    isolated_storage,
):
    raw_bytes = synthetic_raman_bytes()
    metadata = {
        **DEFAULT_METADATA,
        "material_system": "Carbon nanotube",
        "sample_id": "EDIT-PEAK-01",
    }
    imported = post_import("editable-fit.txt", raw_bytes, metadata).json()
    initial = client.post(
        f"/files/{imported['file_id']}/process",
        json={"deconvolution": "pseudo_voigt"},
    ).json()
    component = min(
        initial["deconvolution"]["components"],
        key=lambda item: abs(item["seed_center_cm-1"] - 1350),
    )
    insert_test_reference("related-peak-paper", "Carbon nanotube", 1350.0)

    confirmed_response = client.post(
        f"/files/{imported['file_id']}/peaks/confirm",
        json={
            "processing_id": initial["processing_id"],
            "component_kind": "deconvolution",
            "position_cm_1": component["seed_center_cm-1"],
            "material_system": "Carbon nanotube",
            "label": "User-confirmed D band",
            "learning_confirmed": True,
            "related_reference_ids": ["related-peak-paper"],
        },
    )

    assert confirmed_response.status_code == 200
    payload = confirmed_response.json()
    assert payload["learning"]["status"] == "active"
    assert payload["learning"]["related_references"][0]["reference_id"] == (
        "related-peak-paper"
    )
    updated = payload["processing_result"]
    updated_peak = min(
        updated["peaks"],
        key=lambda item: abs(item["position_cm-1"] - 1350),
    )
    assert updated_peak["assignments"][0]["label"] == (
        "User-confirmed D band"
    )
    assert updated_peak["assignments"][0]["confidence"] == "user_confirmed"
    assert updated_peak["assignments"][0]["related_references"][0][
        "reference_id"
    ] == "related-peak-paper"
    updated_component = min(
        updated["deconvolution"]["components"],
        key=lambda item: abs(item["seed_center_cm-1"] - 1350),
    )
    assert updated_component["assignment"]["label"] == (
        "User-confirmed D band"
    )
    history = client.get(
        f"/files/{imported['file_id']}/clarifications/history"
    ).json()
    assert history[-1]["question"]["kind"] == "fit_assignment_confirmation"
    assert history[-1]["response"]["learning_scope"] == (
        "peak_assignment_for_matching_material_systems"
    )
    assert history[-1]["response"]["related_reference_ids"] == [
        "related-peak-paper"
    ]
    training_references = processing.confirmed_peak_training_references()
    assert any(
        reference["material_system"] == "Carbon nanotube"
        and "User-confirmed D band" in reference["evidence_json"]
        and "related-peak-paper" in reference["evidence_json"]
        for reference in training_references
    )

    second_bytes = b"COMMENT=second matching spectrum\n" + raw_bytes
    second = post_import("second-fit.txt", second_bytes, metadata).json()
    second_result = client.post(f"/files/{second['file_id']}/process").json()
    second_peak = min(
        second_result["peaks"],
        key=lambda item: abs(item["position_cm-1"] - 1350),
    )
    assert any(
        assignment["label"] == "User-confirmed D band"
        and assignment["reference"]["provider"]
        == "user_confirmed_peak_training"
        for assignment in second_peak["assignments"]
    )

    stale = client.post(
        f"/files/{imported['file_id']}/peaks/confirm",
        json={
            "processing_id": initial["processing_id"],
            "component_kind": "deconvolution",
            "position_cm_1": component["seed_center_cm-1"],
            "material_system": "Carbon nanotube",
            "label": "Stale edit",
            "learning_confirmed": True,
        },
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "stale_processing_result"
    assert Path(imported["storage_path"]).read_bytes() == raw_bytes
    assert Path(second["storage_path"]).read_bytes() == second_bytes


def test_instrument_artifact_edit_persists_as_a_filtered_band(
    isolated_storage,
):
    raw_bytes = synthetic_raman_bytes()
    imported = post_import(
        "confirmed-instrument-artifact.txt",
        raw_bytes,
        {
            **DEFAULT_METADATA,
            "material_system": "Carbon nanotube",
            "sample_id": "ARTIFACT-EDIT-01",
        },
    ).json()
    initial = client.post(
        f"/files/{imported['file_id']}/process",
        json={"deconvolution": "pseudo_voigt"},
    ).json()
    component = min(
        initial["deconvolution"]["components"],
        key=lambda item: abs(item["seed_center_cm-1"] - 1350),
    )

    response = client.post(
        f"/files/{imported['file_id']}/peaks/confirm",
        json={
            "processing_id": initial["processing_id"],
            "component_kind": "deconvolution",
            "position_cm_1": component["seed_center_cm-1"],
            "material_system": "Carbon nanotube",
            "label": "Instrument artifact",
            "learning_confirmed": True,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["learning"] == {
        "status": "excluded_as_instrument_artifact",
        "scope": "instrument_artifact_for_current_file",
        "related_references": [],
    }
    updated = payload["processing_result"]
    artifact = next(
        item
        for item in updated["instrument_artifacts"]
        if item["type"] == "user_confirmed_instrument_artifact"
    )
    assert artifact["label"] == "Instrument artifact"
    assert artifact["confidence"] == "user_confirmed"
    assert artifact["affected_point_count"] > 0
    assert not any(
        assignment.get("label") == "Instrument artifact"
        for peak in updated["peaks"]
        for assignment in peak.get("assignments", [])
    )
    raw = dict(updated["series"]["raw"])
    artifact_corrected = dict(updated["series"]["artifact_corrected"])
    affected_positions = [
        position
        for position in raw
        if artifact["affected_range_cm-1"][0]
        <= position
        <= artifact["affected_range_cm-1"][1]
    ]
    assert any(
        artifact_corrected[position] != raw[position]
        for position in affected_positions
    )

    refreshed = client.get(
        f"/files/{imported['file_id']}/processing/latest"
    ).json()
    assert refreshed["processing_id"] == updated["processing_id"]
    assert any(
        item["type"] == "user_confirmed_instrument_artifact"
        and item["center_cm-1"] == pytest.approx(
            component["seed_center_cm-1"]
        )
        for item in refreshed["instrument_artifacts"]
    )
    history = client.get(
        f"/files/{imported['file_id']}/clarifications/history"
    ).json()
    assert history[-1]["response"]["learning_scope"] == (
        "instrument_artifact_for_current_file"
    )
    assert processing.confirmed_peak_training_references() == []
    assert Path(imported["storage_path"]).read_bytes() == raw_bytes


def test_fit_component_confirmation_rejects_a_missing_related_paper(
    isolated_storage,
):
    imported = post_import(
        "missing-related-paper.txt",
        synthetic_raman_bytes(),
    ).json()
    initial = client.post(
        f"/files/{imported['file_id']}/process",
        json={"deconvolution": "pseudo_voigt"},
    ).json()
    component = initial["deconvolution"]["components"][0]

    no_consent = client.post(
        f"/files/{imported['file_id']}/peaks/confirm",
        json={
            "processing_id": initial["processing_id"],
            "component_kind": "deconvolution",
            "position_cm_1": component["seed_center_cm-1"],
            "material_system": "Carbon nanotube",
            "label": "Must not be learned",
        },
    )

    response = client.post(
        f"/files/{imported['file_id']}/peaks/confirm",
        json={
            "processing_id": initial["processing_id"],
            "component_kind": "deconvolution",
            "position_cm_1": component["seed_center_cm-1"],
            "material_system": "Carbon nanotube",
            "label": "Unconfirmed assignment",
            "learning_confirmed": True,
            "related_reference_ids": ["missing-paper"],
        },
    )

    assert no_consent.status_code == 422
    assert no_consent.json()["detail"]["code"] == (
        "learning_confirmation_required"
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "related_reference_not_found"
    assert client.get(
        f"/files/{imported['file_id']}/clarifications/history"
    ).json() == []


def test_fit_component_confirmation_rolls_back_if_reprocessing_fails(
    isolated_storage,
    monkeypatch,
):
    raw_bytes = synthetic_raman_bytes()
    imported = post_import("failed-fit-edit.txt", raw_bytes).json()
    initial = client.post(
        f"/files/{imported['file_id']}/process",
        json={"deconvolution": "pseudo_voigt"},
    ).json()
    component = initial["deconvolution"]["components"][0]

    def fail_processing(*_args, **_kwargs):
        raise processing.ProcessingError(
            500,
            "forced_reprocessing_failure",
            "Forced reprocessing failure.",
        )

    monkeypatch.setattr(processing, "process_imported_file", fail_processing)
    response = client.post(
        f"/files/{imported['file_id']}/peaks/confirm",
        json={
            "processing_id": initial["processing_id"],
            "component_kind": "deconvolution",
            "position_cm_1": component["seed_center_cm-1"],
            "material_system": "Carbon nanotube",
            "label": "Must roll back",
            "learning_confirmed": True,
        },
    )

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == (
        "forced_reprocessing_failure"
    )
    history = client.get(
        f"/files/{imported['file_id']}/clarifications/history"
    ).json()
    assert history == []
    assert Path(imported["storage_path"]).read_bytes() == raw_bytes


def test_perovskite_low_frequency_modes_are_reported_collectively(
    isolated_storage,
):
    raw_bytes = synthetic_perovskite_low_frequency_bytes()
    metadata = {
        **DEFAULT_METADATA,
        "material_system": "Cs2ZnBr4",
        "sample_id": "PEROVSKITE-01",
    }
    imported = post_import("perovskite.txt", raw_bytes, metadata).json()

    result = client.post(
        f"/files/{imported['file_id']}/process",
        json={"deconvolution": "pseudo_voigt"},
    ).json()

    assert result["summary"]["collective_band_count"] == 1
    collective = result["collective_bands"][0]
    assert collective["material_system"] == "Cs2ZnBr4"
    assert collective["range_cm-1"] == [0.0, 150.0]
    assert collective["member_peak_count"] == 2
    assert [
        peak["position_cm-1"] for peak in collective["member_peaks"]
    ] == pytest.approx([74.0, 90.0], abs=3.0)
    assert all(peak["position_cm-1"] >= 150 for peak in result["peaks"])
    assert all(
        component["center_cm-1"] >= 150
        for component in result["deconvolution"]["components"]
    )
    assert result["deconvolution"]["excluded_ranges_cm-1"] == [[0.0, 150.0]]
    assert all(
        intensity == 0.0
        for position, intensity in result["series"]["deconvolution_residual"]
        if position <= 150.0
    )
    assert Path(imported["storage_path"]).read_bytes() == raw_bytes


def test_substrate_detection_and_subtraction_are_optional_and_traceable(
    isolated_storage,
):
    raw_bytes = synthetic_glass_supported_raman_bytes()
    metadata = {**DEFAULT_METADATA, "material_system": "Carbon nanotube"}
    imported = post_import("glass-supported.txt", raw_bytes, metadata).json()
    file_id = imported["file_id"]

    detection = client.post(
        f"/files/{file_id}/process",
        json={"substrate_correction": "detect"},
    ).json()
    corrected_response = client.post(
        f"/files/{file_id}/process",
        json={"substrate_correction": "auto"},
    )
    invalid = client.post(
        f"/files/{file_id}/process",
        json={"substrate_correction": "plastic"},
    )

    assert detection["substrate_correction"]["status"] == "detected"
    assert detection["substrate_correction"]["detected_substrate"] == "glass"
    assert detection["substrate_correction"]["correction"] == "none"
    assert "substrate_corrected" not in detection["series"]

    assert corrected_response.status_code == 200
    corrected = corrected_response.json()
    report = corrected["substrate_correction"]
    assert report["status"] == "subtracted"
    assert report["selected_substrate"] == "glass"
    assert report["correction"] == "fitted_components_and_residual_interpolation"
    assert report["fit_profile"] == "gaussian"
    assert report["quality"]["r_squared"] > 0.9
    assert report["review_required"] is False
    assert report["residual_broad_peak_search"]["method"] == (
        "width_gated_broad_peak_search_on_first_pass_fit_residual"
    )
    assert len(report["components"]) == 2
    assert "substrate_corrected" in corrected["series"]
    assert "substrate_fit" in corrected["series"]
    substrate_series = dict(corrected["series"]["substrate_corrected"])
    baseline_corrected_series = dict(corrected["series"]["corrected"])
    substrate_fit_series = dict(corrected["series"]["substrate_fit"])
    assert all(
        0.0
        <= substrate_series[x_value]
        <= max(baseline_corrected_series[x_value], 0.0)
        for x_value in substrate_series
    )
    assert all(
        substrate_fit_series[x_value]
        == pytest.approx(
            max(
                0.0,
                baseline_corrected_series[x_value]
                - substrate_series[x_value],
            )
        )
        for x_value in substrate_series
    )
    assert report["nonnegative_constraint"]["rule"] == (
        "substrate_corrected_intensity_is_floored_at_zero"
    )
    for lower, upper in report["smoothed_substrate_ranges_cm-1"]:
        interval = [
            (x_value, substrate_series[x_value])
            for x_value in sorted(substrate_series)
            if lower <= x_value <= upper
        ]
        slopes = [
            (right_y - left_y) / (right_x - left_x)
            for (left_x, left_y), (right_x, right_y) in zip(
                interval,
                interval[1:],
            )
        ]
        assert max(slopes, default=0.0) - min(slopes, default=0.0) < 1e-9
    remaining_positions = [peak["position_cm-1"] for peak in corrected["peaks"]]
    assert all(not 750 <= position <= 1150 for position in remaining_positions)
    assert any(1300 <= position <= 1400 for position in remaining_positions)
    assert any(1550 <= position <= 1620 for position in remaining_positions)
    assert Path(imported["storage_path"]).read_bytes() == raw_bytes

    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == (
        "invalid_substrate_correction_mode"
    )


def test_substrate_constraint_floors_negative_derived_residuals():
    baseline_corrected = [2.0, 1.0, -1.0, 0.0, 3.0]
    unconstrained = [-2.0, 0.5, -4.0, -1.0, 4.0]

    constrained, report = processing.enforce_substrate_nonnegative_constraint(
        [100.0, 101.0, 102.0, 103.0, 104.0],
        baseline_corrected,
        unconstrained,
        {"status": "subtracted", "fit_values": [0.0] * 5},
    )

    assert constrained == [0.0, 0.5, 0.0, 0.0, 3.0]
    assert baseline_corrected == [2.0, 1.0, -1.0, 0.0, 3.0]
    assert unconstrained == [-2.0, 0.5, -4.0, -1.0, 4.0]
    assert report["fit_values"] == [2.0, 0.5, 0.0, 0.0, 0.0]
    assert report["nonnegative_constraint"] == {
        "rule": "substrate_corrected_intensity_is_floored_at_zero",
        "positive_residual_floor": 0.0,
        "negative_residual_floor": 0.0,
        "signed_baseline_corrected_series_retained": True,
        "affected_point_count": 4,
        "affected_ranges_cm-1": [
            [100.0, 100.0],
            [102.0, 104.0],
        ],
        "minimum_unconstrained_intensity": -4.0,
    }


def test_substrate_residual_cleanup_preserves_overlapping_material_peak():
    x_values = [float(value) for value in range(11)]
    residual = [float(value) for value in range(11)]
    residual[5] += 20.0

    unprotected, _ = processing.smooth_substrate_residual_intervals(
        x_values,
        residual,
        [[2.0, 8.0]],
        [],
    )
    protected, smoothed_ranges = processing.smooth_substrate_residual_intervals(
        x_values,
        residual,
        [[2.0, 8.0]],
        [[4.0, 6.0]],
    )

    assert unprotected[5] == pytest.approx(5.0)
    assert protected[5] == pytest.approx(25.0)
    assert protected[2:4] == pytest.approx([2.0, 3.0])
    assert protected[7:9] == pytest.approx([7.0, 8.0])
    assert smoothed_ranges == [[2.0, 3.0], [7.0, 8.0]]
    assert processing.material_assignment_protects_substrate_overlap(
        {
            "material_system": "MoS2",
            "label": "A1g mode",
            "confidence": "high",
        },
        "MoS2/Cs2ZnBr4/SiO2",
    )
    assert not processing.material_assignment_protects_substrate_overlap(
        {
            "material_system": "Graphene on SiO2",
            "label": "Reference band",
            "confidence": "moderate",
        },
        "MoS2/Cs2ZnBr4/SiO2",
    )
    assert not processing.material_assignment_protects_substrate_overlap(
        {
            "material_system": "Crystalline silicon",
            "label": "Si first-order optical phonon",
            "confidence": "high",
        },
        "Graphene on SiO2",
    )


def test_substrate_search_detects_broad_low_intensity_band():
    x_values = [float(value) for value in range(300, 1401, 2)]
    intensities = []
    for x_value in x_values:
        broad_substrate = 8 * math.exp(-((x_value - 810) / 42) ** 2)
        strong_material = 1_000 * math.exp(-((x_value - 1350) / 12) ** 2)
        intensities.append(broad_substrate + strong_material)

    standard = processing.detect_peaks(x_values, intensities)
    broad = processing.broad_substrate_peak_candidates(
        x_values,
        intensities,
        "sio2_si",
        processing.MODEL_PARAMETERS,
    )

    assert all(abs(peak["position_cm-1"] - 810) > 10 for peak in standard)
    candidate = min(
        broad,
        key=lambda peak: abs(peak["position_cm-1"] - 810),
    )
    assert candidate["position_cm-1"] == pytest.approx(810, abs=4)
    assert candidate["estimated_fwhm_cm-1"] >= 12
    assert candidate["detection_source"] == "broad_substrate_search"


def test_material_assignment_overrides_substrate_range_overlap():
    x_values = [float(value) for value in range(350, 501, 2)]
    intensities = [
        100 * math.exp(-((x_value - 406) / 12) ** 2)
        for x_value in x_values
    ]
    peaks = [
        {
            "position_cm-1": 406.0,
            "intensity": 100.0,
            "prominence": 95.0,
            "assignments": [
                {
                    "material_system": "MoS2",
                    "label": "A1g out-of-plane mode",
                    "confidence": "high",
                }
            ],
        }
    ]
    parameters = {
        **processing.MODEL_PARAMETERS,
        "import_metadata": {"material_system": "MoS2/Cs2ZnBr4/SiO2"},
        "substrate_profiles": {},
        "confirmed_substrate": "sio2_si",
    }

    corrected, report = processing.detect_and_subtract_substrate(
        x_values,
        intensities,
        peaks,
        "sio2_si",
        parameters,
    )

    assert corrected == intensities
    assert report["status"] == "not_detected"
    assert report["correction"] == "none"
    protected = report["protected_material_overlaps"][0]
    assert protected["position_cm-1"] == 406.0
    assert protected["assignments"][0]["material_system"] == "MoS2"


def test_only_unopposed_peaks_near_substrate_bands_are_classified():
    parameters = processing.MODEL_PARAMETERS
    measured_material = "MoS2 / Cs2ZnBr4 / SiO2 / Si"
    generic_si_tail = {
        "position_cm-1": 549.0,
        "assignments": [
            {
                "material_system": measured_material,
                "label": "Reference band",
                "confidence": "high",
            }
        ],
    }
    silica_d2 = {
        "position_cm-1": 619.0,
        "assignments": [
            {
                "material_system": "SiO2",
                "label": "D2 three-membered siloxane rings",
                "confidence": "moderate",
            }
        ],
    }
    mos2_2la = {
        "position_cm-1": 455.0,
        "assignments": [
            {
                "material_system": "MoS2",
                "label": "2LA(M) second-order mode",
                "confidence": "high",
            }
        ],
    }

    assert processing.is_unopposed_substrate_peak(
        generic_si_tail,
        "sio2_si",
        measured_material,
        parameters,
    )
    assert processing.is_unopposed_substrate_peak(
        silica_d2,
        "sio2_si",
        measured_material,
        parameters,
    )
    assert not processing.is_unopposed_substrate_peak(
        mos2_2la,
        "sio2_si",
        measured_material,
        parameters,
    )


def test_short_si_component_does_not_admit_unrelated_silicate_references():
    components = processing.declared_material_components(
        "MoS2 / Cs2ZnBr4 / SiO2 / Si"
    )

    assert processing.reference_applies_to_material("MoS2", components)
    assert processing.reference_applies_to_material(
        "Crystalline silicon",
        components,
    )
    assert not processing.reference_applies_to_material(
        "Al0.122Ca1.802Fe0.575O24Si7.94",
        components,
    )
    assert not processing.reference_applies_to_material(
        "Graphene on SiO2",
        components,
    )


def test_clarification_policy_trains_only_from_confirmed_material_answer(
    isolated_storage,
):
    raw_bytes = synthetic_raman_bytes()
    imported = post_import("unresolved.txt", raw_bytes).json()
    file_id = imported["file_id"]
    processing_result = client.post(f"/files/{file_id}/process").json()

    policy = client.get("/clarifications/policy")
    pending = client.get(f"/files/{file_id}/clarifications")

    assert policy.status_code == 200
    assert policy.json()["maximum_questions_per_processing"] == 1
    assert pending.status_code == 200
    assert pending.json()["processing_id"] == processing_result["processing_id"]
    assert len(pending.json()["questions"]) == 1
    question = pending.json()["questions"][0]
    assert question["question_key"] == "material_identity"

    invalid = client.post(
        f"/files/{file_id}/clarifications/respond",
        json={"question_key": "material_identity", "answer": "known"},
    )
    answered = client.post(
        f"/files/{file_id}/clarifications/respond",
        json={
            "question_key": "material_identity",
            "answer": "known",
            "value": "Testium phase B",
        },
    )

    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "material_label_required"
    assert answered.status_code == 200
    payload = answered.json()
    assert payload["training"]["material_system"] == "Testium phase B"
    assert payload["processing_result"]["summary"]["identification"][
        "status"
    ] == "confirmed"
    assert payload["clarification"]["response"][
        "confirmed_material_system"
    ] == "Testium phase B"
    assert "question_json" not in payload["clarification"]
    assert "response_json" not in payload["clarification"]
    assert all(
        item["question_key"] != "material_identity"
        for item in client.get(f"/files/{file_id}/clarifications").json()[
            "questions"
        ]
    )
    history = client.get(f"/files/{file_id}/clarifications/history").json()
    assert len(history) == 1
    assert history[0]["status"] == "answered"
    assert Path(imported["storage_path"]).read_bytes() == raw_bytes


def test_substrate_clarification_can_apply_correction_or_be_dismissed(
    isolated_storage,
):
    raw_bytes = synthetic_glass_supported_raman_bytes()
    metadata = {**DEFAULT_METADATA, "material_system": "Carbon nanotube"}
    imported = post_import("uncertain-substrate.txt", raw_bytes, metadata).json()
    file_id = imported["file_id"]
    detected = client.post(
        f"/files/{file_id}/process",
        json={"substrate_correction": "detect"},
    ).json()

    pending = client.get(f"/files/{file_id}/clarifications").json()
    assert detected["substrate_correction"]["status"] == "detected"
    assert pending["questions"][0]["question_key"] == "substrate_identity"

    answered = client.post(
        f"/files/{file_id}/clarifications/respond",
        json={"question_key": "substrate_identity", "answer": "glass"},
    )
    assert answered.status_code == 200
    corrected = answered.json()["processing_result"]
    assert corrected["substrate_correction"]["status"] == "subtracted"
    assert corrected["substrate_correction"]["selected_substrate"] == "glass"
    assert all(
        item["question_key"] != "substrate_identity"
        for item in client.get(f"/files/{file_id}/clarifications").json()[
            "questions"
        ]
    )
    assert Path(imported["storage_path"]).read_bytes() == raw_bytes

    other_bytes = b"COMMENT=second spectrum\n" + synthetic_raman_bytes()
    other = post_import("dismissed.txt", other_bytes).json()
    other_id = other["file_id"]
    client.post(f"/files/{other_id}/process")
    dismissed = client.post(
        f"/files/{other_id}/clarifications/respond",
        json={
            "question_key": "material_identity",
            "dismissed": True,
        },
    )
    assert dismissed.status_code == 200
    assert dismissed.json()["clarification"]["status"] == "dismissed"
    assert all(
        item["question_key"] != "material_identity"
        for item in client.get(f"/files/{other_id}/clarifications").json()[
            "questions"
        ]
    )
    assert Path(other["storage_path"]).read_bytes() == other_bytes


def test_confirmed_cross_sample_substrate_profile_learns_recurring_bands(
    isolated_storage,
):
    source_records = []
    for index, (material, material_peak) in enumerate(
        (("Material alpha", 1100.0), ("Material beta", 1250.0)),
        start=1,
    ):
        raw_bytes = synthetic_shared_substrate_bytes(material_peak)
        metadata = {
            **DEFAULT_METADATA,
            "sample_id": f"SOURCE-{index}",
            "material_system": material,
            "substrate": "sio2_si",
        }
        imported = post_import(
            f"source-{index}.txt",
            raw_bytes,
            metadata,
        ).json()
        result = client.post(f"/files/{imported['file_id']}/process")
        assert result.status_code == 200
        source_records.append((imported, raw_bytes))

    target_bytes = synthetic_shared_substrate_bytes(1320.0)
    target_metadata = {
        **DEFAULT_METADATA,
        "sample_id": "TARGET",
        "material_system": "Material gamma",
        "substrate": "unknown",
    }
    target = post_import(
        "target.txt",
        target_bytes,
        target_metadata,
    ).json()
    detection_response = client.post(
        f"/files/{target['file_id']}/process",
        json={"substrate_correction": "detect"},
    )

    assert detection_response.status_code == 200
    detection = detection_response.json()["substrate_correction"]
    profile = detection["cross_sample_profile"]
    assert profile["sample_count"] == 2
    assert profile["background_effect"]["sample_count"] == 2
    learned = profile["learned_bands"]
    assert any(
        band["range_cm-1"][0] <= 700 <= band["range_cm-1"][1]
        for band in learned
    )
    learned_matches = [
        band
        for band in detection["matched_bands"]
        if band["evidence_source"] == "user_confirmed_cross_sample"
    ]
    assert learned_matches
    assert detection["score"] < processing.MODEL_PARAMETERS[
        "substrate_auto_minimum_score"
    ]
    questions = client.get(
        f"/files/{target['file_id']}/clarifications"
    ).json()["questions"]
    assert questions[0]["question_key"] == "substrate_identity"
    assert "recurring" in questions[0]["explanation"]
    assert Path(target["storage_path"]).read_bytes() == target_bytes
    for imported, raw_bytes in source_records:
        assert Path(imported["storage_path"]).read_bytes() == raw_bytes


def test_same_material_replicates_can_learn_a_recurring_substrate_band(
    isolated_storage,
):
    source_records = []
    for index, material_peak in enumerate((1100.0, 1200.0, 1300.0), start=1):
        raw_bytes = synthetic_shared_substrate_bytes(material_peak)
        imported = post_import(
            f"replicate-{index}.txt",
            raw_bytes,
            {
                **DEFAULT_METADATA,
                "sample_id": f"REPLICATE-{index}",
                "material_system": "Material alpha",
                "substrate": "sio2_si",
            },
        ).json()
        assert client.post(f"/files/{imported['file_id']}/process").status_code == 200
        source_records.append((imported, raw_bytes))

    profile = processing.cross_sample_substrate_profiles(
        main.PROCESSED_DATA_DIR
    )["sio2_si"]
    learned = [
        band
        for band in profile["learned_bands"]
        if band["range_cm-1"][0] <= 700 <= band["range_cm-1"][1]
    ]

    assert len(learned) == 1
    assert learned[0]["support_basis"] == "same_material_replicates"
    assert learned[0]["recurrence_fraction"] == 1.0
    assert profile["same_material_replicate_rule"] == {
        "eligible_only_when_profile_has_one_material_system": True,
        "minimum_samples": 3,
        "minimum_recurrence_fraction": 0.6,
        "exclude_peaks_with_specific_material_evidence": True,
    }
    for imported, raw_bytes in source_records:
        assert Path(imported["storage_path"]).read_bytes() == raw_bytes


def test_same_material_replicates_do_not_learn_a_known_material_peak(
    isolated_storage,
):
    insert_test_reference("mos2-700", "MoS2", 700.0)
    for index, material_peak in enumerate((1100.0, 1200.0, 1300.0), start=1):
        imported = post_import(
            f"protected-replicate-{index}.txt",
            synthetic_shared_substrate_bytes(material_peak),
            {
                **DEFAULT_METADATA,
                "sample_id": f"PROTECTED-{index}",
                "material_system": "MoS2",
                "substrate": "sio2_si",
            },
        ).json()
        assert client.post(f"/files/{imported['file_id']}/process").status_code == 200

    profile = processing.cross_sample_substrate_profiles(
        main.PROCESSED_DATA_DIR
    )["sio2_si"]

    assert not any(
        band["range_cm-1"][0] <= 700 <= band["range_cm-1"][1]
        for band in profile["learned_bands"]
    )


def test_pure_substrate_reference_transfers_noise_gated_shifted_peaks(
    isolated_storage,
):
    reference_bytes = synthetic_substrate_reference_bytes()
    reference = post_import(
        "SiO2_REF.txt",
        reference_bytes,
        {
            **DEFAULT_METADATA,
            "sample_id": "SiO2_REF",
            "material_system": "SiO2 / crystalline Si",
            "substrate": "sio2_si",
        },
    ).json()
    reference_result = client.post(
        f"/files/{reference['file_id']}/process",
        json={"substrate_correction": "none"},
    ).json()

    assert len(reference_result["peaks"]) == 2
    assert all(
        peak["assignments"][0]["role"] == "substrate"
        and peak["assignments"][0]["material_system"]
        == "SiO2 / crystalline Si"
        for peak in reference_result["peaks"]
    )

    insert_test_reference("material-alpha-958", "Material alpha", 958.0)
    target_bytes = synthetic_substrate_reference_bytes(
        shift=8.0,
        intensity_scale=0.35,
    )
    target = post_import(
        "mixed-material.txt",
        target_bytes,
        {
            **DEFAULT_METADATA,
            "sample_id": "MIXED-01",
            "material_system": "Material alpha / SiO2 / crystalline Si",
            "substrate": "sio2_si",
        },
    ).json()
    target_result = client.post(
        f"/files/{target['file_id']}/process",
        json={"substrate_correction": "auto"},
    ).json()

    profile = target_result["model"]["parameters"]["substrate_profiles"][
        "sio2_si"
    ]
    assert profile["reference_spectra"][0]["file_id"] == reference["file_id"]
    assert profile["reference_spectra"][0]["minimum_signal_to_noise"] == 3.0
    assert profile["reference_transfer"] == {
        "noise_gated": True,
        "absolute_intensity_required": False,
        "intensity_method": "within-spectrum_relative_prominence",
        "position_tolerance_cm-1": 18.0,
        "material_overlap_rule": "specific_non_substrate_assignment_wins",
    }
    empirical_matches = [
        match
        for match in target_result["substrate_correction"]["matched_bands"]
        if match["evidence_source"] == "user_confirmed_substrate_reference"
    ]
    assert any(
        abs(match["observed_peaks_cm-1"][0] - 708.0) <= 3.0
        for match in empirical_matches
    )
    assert any(
        abs(item["position_cm-1"] - 958.0) <= 3.0
        for item in target_result["substrate_correction"][
            "protected_material_overlaps"
        ]
    )
    assert any(
        abs(peak["position_cm-1"] - 958.0) <= 3.0
        for peak in target_result["peaks"]
    )
    assert Path(reference["storage_path"]).read_bytes() == reference_bytes
    assert Path(target["storage_path"]).read_bytes() == target_bytes


def test_t12_substrate_only_interval_is_removed_from_all_downstream_series(
    isolated_storage,
):
    raw_bytes = synthetic_t12_substrate_interval_bytes()
    imported = post_import(
        "T-12, interval-test.txt",
        raw_bytes,
        {
            **DEFAULT_METADATA,
            "sample_id": "T-12, interval-test",
            "material_system": "MoS2 / SiO2 / crystalline Si",
            "substrate": "sio2_si",
        },
    ).json()

    result = client.post(
        f"/files/{imported['file_id']}/process",
        json={
            "deconvolution": "pseudo_voigt",
            "substrate_correction": "auto",
        },
    ).json()

    correction = result["substrate_correction"]
    assert correction["correction"] == (
        "user_confirmed_substrate_only_interval_mask"
    )
    assert correction["substrate_only_ranges_cm-1"] == [[500.0, 835.0]]
    assert correction["interval_masked_point_count"] > 150
    assert correction["interval_excluded_peak_count"] >= 4
    assert all(
        peak["assignments"][0]["material_system"]
        == "SiO2 / crystalline Si"
        and peak["assignments"][0]["role"] == "substrate"
        for peak in correction["interval_excluded_peaks"]
    )
    assert all(
        not 500.0 <= peak["position_cm-1"] <= 835.0
        for peak in result["peaks"]
    )
    for series_name in (
        "substrate_corrected",
        "deconvolved_fit",
        "deconvolution_residual",
    ):
        assert all(
            value >= 0.0 for _x_value, value in result["series"][series_name]
        )
        assert all(
            value == 0.0
            for x_value, value in result["series"][series_name]
            if 500.0 <= x_value <= 835.0
        )
    assert all(
        value >= 0.0
        for _x_value, value in result["series"]["substrate_fit"]
    )
    assert result["deconvolution"]["residual_nonnegative_floor"]["floor"] == 0.0
    assert all(
        value == 0.0
        for component in result["deconvolution"]["components"]
        for x_value, value in component["series"]
        if 500.0 <= x_value <= 835.0
    )
    corrected = dict(result["series"]["corrected"])
    substrate_fit = dict(result["series"]["substrate_fit"])
    assert all(
        substrate_fit[x_value] == pytest.approx(max(value, 0.0))
        for x_value, value in corrected.items()
        if 500.0 <= x_value <= 835.0
    )
    assert Path(imported["storage_path"]).read_bytes() == raw_bytes


def test_sio2_material_label_asks_user_to_confirm_its_role(
    isolated_storage,
):
    raw_bytes = synthetic_raman_bytes()
    metadata = {
        **DEFAULT_METADATA,
        "material_system": "Graphene on SiO2",
        "substrate": "unknown",
    }
    imported = post_import("graphene-sio2.txt", raw_bytes, metadata).json()
    client.post(f"/files/{imported['file_id']}/process")

    pending = client.get(
        f"/files/{imported['file_id']}/clarifications"
    ).json()["questions"]
    assert pending[0]["question_key"] == "substrate_role"

    answered = client.post(
        f"/files/{imported['file_id']}/clarifications/respond",
        json={"question_key": "substrate_role", "answer": "sio2_si"},
    )
    assert answered.status_code == 200
    assert answered.json()["metadata"]["substrate"] == "sio2_si"
    assert answered.json()["processing_result"]["model"]["parameters"][
        "confirmed_substrate"
    ] == "sio2_si"
    assert answered.json()["processing_result"]["substrate_correction"][
        "status"
    ] == "disabled"
    assert all(
        item["question_key"] != "substrate_role"
        for item in client.get(
            f"/files/{imported['file_id']}/clarifications"
        ).json()["questions"]
    )
    assert Path(imported["storage_path"]).read_bytes() == raw_bytes


def test_significant_unassigned_peak_requires_user_confirmation(
    isolated_storage,
):
    insert_test_reference("targetium-reference", "Targetium", 1000.0)
    insert_test_reference("candidate-reference", "Candidateium", 1300.0)
    raw_bytes = synthetic_two_peak_bytes(1000.0, 1300.0)
    metadata = {
        **DEFAULT_METADATA,
        "material_system": "Targetium",
        "sample_id": "UNASSIGNED-01",
    }
    imported = post_import("unassigned-candidate.txt", raw_bytes, metadata).json()
    processed = client.post(f"/files/{imported['file_id']}/process").json()

    assert processed["summary"]["unassigned_peak_count"] == 1
    unassigned = processed["summary"]["unassigned_peaks"][0]
    assert unassigned["position_cm-1"] == pytest.approx(1300.0, abs=2.0)
    assert unassigned["candidate_assignments"][0]["material_system"] == (
        "Candidateium"
    )
    assert all(
        peak["assignments"] == []
        for peak in processed["peaks"]
        if abs(peak["position_cm-1"] - 1300.0) <= 2.0
    )

    pending = client.get(
        f"/files/{imported['file_id']}/clarifications"
    ).json()["questions"][0]
    assert pending["question_key"].startswith("unassigned_peak:1300")
    assert pending["options"][0]["value"] == "candidate_0"
    assert "provisional" in pending["explanation"]

    confirmed = client.post(
        f"/files/{imported['file_id']}/clarifications/respond",
        json={
            "question_key": pending["question_key"],
            "answer": "candidate_0",
        },
    )
    assert confirmed.status_code == 200
    payload = confirmed.json()
    assignment = payload["clarification"]["response"][
        "confirmed_peak_assignment"
    ]
    assert assignment["material_system"] == "Candidateium"
    updated = payload["processing_result"]
    confirmed_peak = next(
        peak
        for peak in updated["peaks"]
        if abs(peak["position_cm-1"] - 1300.0) <= 2.0
    )
    assert confirmed_peak["assignments"][0]["confidence"] == "user_confirmed"
    assert confirmed_peak["assignments"][0]["reference"]["reference_id"] == (
        "candidate-reference"
    )
    assert updated["summary"]["unassigned_peak_count"] == 0
    assert Path(imported["storage_path"]).read_bytes() == raw_bytes


def test_recurring_same_system_peak_recommends_online_assignment_for_confirmation(
    isolated_storage,
):
    insert_test_reference("recurring-target-reference", "Targetium", 1000.0)
    metadata = {
        **DEFAULT_METADATA,
        "material_system": "Targetium",
    }
    imports = []
    results = []
    for replicate in range(1, 4):
        raw_bytes = (
            f"COMMENT=recurring replicate {replicate}\n".encode("utf-8")
            + synthetic_two_peak_bytes(1000.0, 800.0)
        )
        imported = post_import(
            f"recurring-{replicate}.txt",
            raw_bytes,
            {**metadata, "sample_id": f"RECURRING-{replicate:02d}"},
        ).json()
        imports.append((imported, raw_bytes))
        results.append(
            client.post(f"/files/{imported['file_id']}/process").json()
        )

    recurring_peak = next(
        peak
        for peak in results[-1]["summary"]["unassigned_peaks"]
        if abs(peak["position_cm-1"] - 800.0) <= 2.0
    )
    assert recurring_peak["recurrence"]["status"] == (
        "recurring_same_material_system"
    )
    assert recurring_peak["recurrence"]["observed_count"] == 3
    assert len(recurring_peak["recurrence"]["supporting_file_ids"]) == 2
    online_search = recurring_peak["online_reference_search"]
    assert online_search["status"] == "candidate_found"
    assert online_search["confirmation_required"] is True
    recommended = online_search["most_probable_assignment"]
    assert recommended["recommended"] is True
    assert recommended["material_system"] == "SiO2"
    assert recommended["label"] == "SiO2 network band near 800 cm-1"
    assert recommended["reference"]["provider"] == (
        "curated_primary_literature"
    )

    question = client.get(
        f"/files/{imports[-1][0]['file_id']}/clarifications"
    ).json()["questions"][0]
    assert question["prompt"].startswith("Confirm the recurring peak")
    assert ":recurring:" in question["question_key"]
    assert "3 spectra with the same material system" in question["explanation"]
    assert question["options"][0]["value"] == "candidate_0"
    assert question["options"][0]["label"].startswith(
        "Recommended online match:"
    )

    confirmation = client.post(
        f"/files/{imports[-1][0]['file_id']}/clarifications/respond",
        json={
            "question_key": question["question_key"],
            "answer": "candidate_0",
        },
    )
    assert confirmation.status_code == 200
    assigned_peak = next(
        peak
        for peak in confirmation.json()["processing_result"]["peaks"]
        if abs(peak["position_cm-1"] - 800.0) <= 2.0
    )
    assert assigned_peak["assignments"][0]["confidence"] == "user_confirmed"
    assert assigned_peak["assignments"][0]["reference"]["provider"] == (
        "curated_primary_literature"
    )
    assert all(
        Path(imported["storage_path"]).read_bytes() == raw_bytes
        for imported, raw_bytes in imports
    )


def test_singleton_noise_level_peak_requires_confirmation_before_exclusion(
    isolated_storage,
):
    insert_test_reference("singleton-target-reference", "Targetium", 1000.0)
    metadata = {
        **DEFAULT_METADATA,
        "material_system": "Targetium",
    }
    comparison_bytes = (
        b"COMMENT=singleton comparison\n"
        + synthetic_singleton_noise_bytes()
    )
    comparison = post_import(
        "singleton-comparison.txt",
        comparison_bytes,
        {**metadata, "sample_id": "SINGLETON-COMPARISON"},
    ).json()
    client.post(f"/files/{comparison['file_id']}/process")

    sample_bytes = (
        b"COMMENT=singleton weak feature\n"
        + synthetic_singleton_noise_bytes(3.2)
    )
    sample = post_import(
        "singleton-noise.txt",
        sample_bytes,
        {**metadata, "sample_id": "SINGLETON-NOISE"},
    ).json()
    initial = client.post(f"/files/{sample['file_id']}/process").json()
    candidate = next(
        peak
        for peak in initial["summary"]["unassigned_peaks"]
        if abs(peak["position_cm-1"] - 1300.0) <= 2.0
    )
    noise_evidence = candidate["singleton_noise_candidate"]
    assert noise_evidence["status"] == "singleton_noise_level_candidate"
    assert noise_evidence["observed_count"] == 1
    assert noise_evidence["same_system_spectrum_count"] == 2
    assert noise_evidence["matching_other_file_ids"] == []
    assert noise_evidence["prominence"] <= noise_evidence[
        "noise_level_upper_prominence"
    ]
    assert any(
        abs(peak["position_cm-1"] - 1300.0) <= 2.0
        for peak in initial["peaks"]
    )

    question = client.get(
        f"/files/{sample['file_id']}/clarifications"
    ).json()["questions"][0]
    assert question["prompt"].startswith("Is the peak at 1300.0 cm-1 noise")
    assert ":singleton-noise:" in question["question_key"]
    assert question["options"][0] == {
        "value": "confirm_noise",
        "label": "Confirm this singleton feature as noise",
    }

    confirmed = client.post(
        f"/files/{sample['file_id']}/clarifications/respond",
        json={
            "question_key": question["question_key"],
            "answer": "confirm_noise",
        },
    )
    assert confirmed.status_code == 200
    payload = confirmed.json()
    exclusion = payload["clarification"]["response"][
        "confirmed_noise_exclusion"
    ]
    assert exclusion["classification"] == "noise"
    assert payload["clarification"]["response"]["learning_scope"] == (
        "file_specific_noise_exclusion"
    )
    updated = payload["processing_result"]
    assert updated["summary"]["confirmed_noise_peak_count"] == 1
    assert updated["summary"]["confirmed_noise_peaks"][0]["confidence"] == (
        "user_confirmed"
    )
    assert not any(
        abs(peak["position_cm-1"] - 1300.0) <= 2.0
        for peak in updated["peaks"]
    )
    for series_name in ("raw", "artifact_corrected", "baseline", "corrected"):
        assert updated["series"][series_name] == initial["series"][series_name]
    refreshed = client.get(
        f"/files/{sample['file_id']}/processing/latest"
    ).json()
    assert refreshed["processing_id"] == updated["processing_id"]
    assert refreshed["summary"]["confirmed_noise_peak_count"] == 1
    assert processing.confirmed_peak_training_references() == []
    assert Path(comparison["storage_path"]).read_bytes() == comparison_bytes
    assert Path(sample["storage_path"]).read_bytes() == sample_bytes


def test_user_confirmation_promotes_a_peak_omitted_by_generic_detection():
    x_values = [1340.0, 1342.0, 1344.0, 1346.0, 1348.0]
    intensities = [2.0, 5.0, 12.0, 5.0, 2.0]
    confirmation = {
        "observed_cm-1": 1344.0,
        "material_system": "Graphene",
        "label": "D band",
        "reference": {
            "reference_id": "user-confirmed-graphene-d-band",
            "title": "User-confirmed peak assignment",
            "citation": "User confirmation",
            "source_url": None,
            "sha256": "a" * 64,
            "provider": "user_confirmation",
        },
        "clarification_id": "clarification-1",
        "confirmed_at": "2026-07-31T00:00:00+00:00",
    }

    peaks = processing.include_user_confirmed_peaks(
        [],
        [confirmation],
        x_values,
        intensities,
    )
    peaks = [{**peak, "assignments": []} for peak in peaks]
    peaks = processing.apply_confirmed_peak_assignments(peaks, [confirmation])

    assert len(peaks) == 1
    assert peaks[0]["position_cm-1"] == 1344.0
    assert peaks[0]["detection_source"] == "user_confirmation"
    assert peaks[0]["assignments"][0]["material_system"] == "Graphene"
    assert peaks[0]["assignments"][0]["label"] == "D band"
    assert peaks[0]["assignments"][0]["confidence"] == "user_confirmed"


def test_unassigned_peak_accepts_and_learns_a_typed_answer(isolated_storage):
    insert_test_reference("typed-target-reference", "Targetium", 1000.0)
    raw_bytes = synthetic_two_peak_bytes(1000.0, 1390.0)
    metadata = {
        **DEFAULT_METADATA,
        "material_system": "Targetium",
        "sample_id": "TYPED-ANSWER-01",
    }
    imported = post_import("typed-answer.txt", raw_bytes, metadata).json()
    client.post(f"/files/{imported['file_id']}/process")
    question = client.get(
        f"/files/{imported['file_id']}/clarifications"
    ).json()["questions"][0]

    assert question["question_key"].startswith("unassigned_peak:")
    assert question["allows_text"] is True
    assert question["options"][-1] == {
        "value": "custom",
        "label": "Type my own answer",
    }

    missing_value = client.post(
        f"/files/{imported['file_id']}/clarifications/respond",
        json={"question_key": question["question_key"], "answer": "custom"},
    )
    answered = client.post(
        f"/files/{imported['file_id']}/clarifications/respond",
        json={
            "question_key": question["question_key"],
            "answer": "custom",
            "value": "User-observed secondary Raman mode",
        },
    )

    assert missing_value.status_code == 422
    assert missing_value.json()["detail"]["code"] == "custom_answer_required"
    assert answered.status_code == 200
    response = answered.json()["clarification"]["response"]
    assert response["value"] == "User-observed secondary Raman mode"
    assert response["confirmed_peak_assignment"]["label"] == (
        "User-observed secondary Raman mode"
    )
    assert response["confirmed_peak_assignment"]["reference"]["provider"] == (
        "user_confirmation"
    )
    assert Path(imported["storage_path"]).read_bytes() == raw_bytes


def test_unassigned_peak_can_request_online_library_update(
    isolated_storage,
    monkeypatch,
):
    insert_test_reference("online-target-reference", "Targetium", 1000.0)
    raw_bytes = synthetic_two_peak_bytes(1000.0, 1470.0)
    metadata = {
        **DEFAULT_METADATA,
        "material_system": "Targetium",
        "sample_id": "ONLINE-SEARCH-01",
    }
    imported = post_import("online-search.txt", raw_bytes, metadata).json()
    client.post(f"/files/{imported['file_id']}/process")
    question = client.get(
        f"/files/{imported['file_id']}/clarifications"
    ).json()["questions"][0]
    assert any(
        option["value"] == "search_online" for option in question["options"]
    )

    calls = []

    def fake_sync(_directory):
        calls.append(True)
        return {
            "provider": "Raman Open Database",
            "reference_count": 25,
            "batch_downloaded_count": 5,
            "catalog_complete": False,
        }

    monkeypatch.setattr(online_references, "sync_rod_catalog", fake_sync)
    response = client.post(
        f"/files/{imported['file_id']}/clarifications/respond",
        json={
            "question_key": question["question_key"],
            "answer": "search_online",
        },
    )

    assert response.status_code == 200
    assert calls == [True]
    assert response.json()["online_search"]["batch_downloaded_count"] == 5
    assert response.json()["clarification"]["response"]["answer"] == (
        "search_online"
    )
    assert Path(imported["storage_path"]).read_bytes() == raw_bytes


def test_unknown_substrate_is_not_automatically_subtracted_from_weak_evidence():
    x_values = [float(value) for value in range(700, 1201, 2)]
    intensities = [10.0 for _value in x_values]
    peak = {
        "position_cm-1": 1026.0,
        "intensity": 100.0,
        "prominence": 90.0,
    }

    corrected, report = processing.detect_and_subtract_substrate(
        x_values,
        intensities,
        [peak],
        "auto",
        processing.MODEL_PARAMETERS,
    )

    assert report["status"] == "detected"
    assert report["detected_substrate"] == "glass"
    assert report["score"] < processing.MODEL_PARAMETERS[
        "substrate_auto_minimum_score"
    ]
    assert report["correction"] == "none"
    assert corrected == intensities


def test_processing_detects_and_removes_rayleigh_line_from_derived_series(
    isolated_storage,
):
    points = []
    for x_value in range(-50, 301, 2):
        intensity = 500.0
        if x_value == 0:
            intensity = 50_000.0
        elif abs(x_value) == 2:
            intensity = 12_000.0
        elif abs(x_value) == 4:
            intensity = 2_000.0
        elif x_value == 100:
            intensity = 1_500.0
        elif x_value in {98, 102}:
            intensity = 900.0
        elif x_value == 200:
            intensity = 1_600.0
        points.append(f"{x_value},{intensity}")
    raw_bytes = (
        "FILETYPE=RAMAN SPECTRUM\nLASER=532\nTYPE=RamanShift\n"
        "XUNITS=1/cm\nXYDATA=\n"
        + "\n".join(points)
        + "\n"
    ).encode("utf-8")
    imported = post_import("rayleigh-leakage.txt", raw_bytes).json()

    result = client.post(f"/files/{imported['file_id']}/process").json()

    artifacts = result["summary"]["artifacts"]
    assert len(artifacts) == 2
    assert result["instrument_artifacts"] == artifacts
    assert result["summary"]["instrument_artifacts"] == artifacts
    artifact = artifacts[0]
    assert artifact["category"] == "instrument_artifact"
    assert artifact["type"] == "rayleigh_line_leakage"
    assert artifact["center_cm-1"] == 0
    assert artifact["confidence"] == "high"
    assert artifact["correction"] == (
        "linear_interpolation_for_derived_processing"
    )
    raw_by_x = dict(result["series"]["raw"])
    filtered_by_x = dict(result["series"]["artifact_corrected"])
    assert raw_by_x[0] == 50_000.0
    assert filtered_by_x[0] == pytest.approx(500.0)
    assert raw_by_x[200] == 1_600.0
    assert filtered_by_x[200] == pytest.approx(500.0)
    cosmic_artifact = artifacts[1]
    assert cosmic_artifact["category"] == "instrument_artifact"
    assert cosmic_artifact["type"] == "cosmic_ray_spike"
    assert cosmic_artifact["center_cm-1"] == 200
    assert cosmic_artifact["affected_point_count"] == 1
    assert cosmic_artifact["confidence"] == "high"
    assert max(value for x_value, value in filtered_by_x.items() if abs(x_value) <= 10) == pytest.approx(500.0)
    corrected_by_x = dict(result["series"]["corrected"])
    assert corrected_by_x[100] == pytest.approx(1_000.0)
    assert [peak["position_cm-1"] for peak in result["peaks"]] == [100]
    assert result["summary"]["peak_count"] == 1
    assert Path(imported["storage_path"]).read_bytes() == raw_bytes


def test_processing_includes_all_cosmic_spikes_in_instrument_artifacts(
    isolated_storage,
):
    points = []
    for x_value in range(100, 301, 2):
        intensity = 500.0
        if x_value == 150:
            intensity = 5_000.0
        elif x_value == 250:
            intensity = 5_500.0
        elif x_value == 252:
            intensity = 4_500.0
        points.append(f"{x_value},{intensity}")
    raw_bytes = (
        "FILETYPE=RAMAN SPECTRUM\nTYPE=RamanShift\n"
        "XUNITS=1/cm\nXYDATA=\n"
        + "\n".join(points)
        + "\n"
    ).encode("utf-8")
    imported = post_import("multiple-cosmic-spikes.txt", raw_bytes).json()

    response = client.post(f"/files/{imported['file_id']}/process")

    assert response.status_code == 200
    result = response.json()
    artifacts = result["instrument_artifacts"]
    assert result["summary"]["instrument_artifacts"] == artifacts
    assert result["summary"]["artifacts"] == artifacts
    assert [artifact["type"] for artifact in artifacts] == [
        "cosmic_ray_spike",
        "cosmic_ray_spike",
    ]
    assert all(
        artifact["category"] == "instrument_artifact"
        for artifact in artifacts
    )
    assert [artifact["center_cm-1"] for artifact in artifacts] == [150, 250]
    assert [artifact["affected_point_count"] for artifact in artifacts] == [1, 2]

    raw_by_x = dict(result["series"]["raw"])
    filtered_by_x = dict(result["series"]["artifact_corrected"])
    assert [raw_by_x[x_value] for x_value in (150, 250, 252)] == [
        5_000.0,
        5_500.0,
        4_500.0,
    ]
    assert [filtered_by_x[x_value] for x_value in (150, 250, 252)] == [
        pytest.approx(500.0),
        pytest.approx(500.0),
        pytest.approx(500.0),
    ]
    assert Path(imported["storage_path"]).read_bytes() == raw_bytes


def test_processing_filters_a_modest_isolated_cosmic_spike(
    isolated_storage,
):
    points = []
    for x_value in range(3000, 3121, 2):
        intensity = 347.0
        if x_value == 3064:
            intensity = 689.0
        points.append(f"{x_value},{intensity}")
    raw_bytes = (
        "FILETYPE=RAMAN SPECTRUM\nTYPE=RamanShift\n"
        "XUNITS=1/cm\nXYDATA=\n"
        + "\n".join(points)
        + "\n"
    ).encode("utf-8")
    imported = post_import("modest-cosmic-spike.txt", raw_bytes).json()

    result = client.post(f"/files/{imported['file_id']}/process").json()

    artifacts = result["instrument_artifacts"]
    assert len(artifacts) == 1
    assert artifacts[0]["category"] == "instrument_artifact"
    assert artifacts[0]["type"] == "cosmic_ray_spike"
    assert artifacts[0]["center_cm-1"] == 3064
    assert artifacts[0]["signal_to_background_ratio"] == pytest.approx(
        (689.0 - 347.0) / 347.0
    )
    assert dict(result["series"]["artifact_corrected"])[3064] == (
        pytest.approx(347.0)
    )
    assert all(
        peak["position_cm-1"] != 3064
        for peak in result["peaks"]
    )
    assert Path(imported["storage_path"]).read_bytes() == raw_bytes


def test_cosmic_error_confirmation_is_not_reinserted_as_a_raman_peak():
    confirmation = {
        "observed_cm-1": 3064.0,
        "material_system": "MoS2 / Cs2ZnBr4 / SiO2 / Si",
        "label": "its a cosmic error",
        "clarification_id": "cosmic-confirmation",
        "confirmed_at": "2026-07-31T00:00:00+00:00",
    }
    x_values = [3062.0, 3064.0, 3066.0]
    intensities = [10.0, 10.0, 10.0]

    promoted = processing.include_user_confirmed_peaks(
        [],
        [confirmation],
        x_values,
        intensities,
    )
    assigned = processing.apply_confirmed_peak_assignments(
        [],
        [confirmation],
    )

    assert processing.is_cosmic_artifact_confirmation(confirmation) is True
    assert promoted == []
    assert assigned == []

    instrument_confirmation = {
        **confirmation,
        "label": "Instrument artifact",
    }
    corrected, artifacts = processing.apply_confirmed_instrument_artifacts(
        [
            3054.0,
            3056.0,
            3058.0,
            3060.0,
            3062.0,
            3064.0,
            3066.0,
            3068.0,
            3070.0,
            3072.0,
        ],
        [10.0, 10.0, 10.0, 10.0, 20.0, 100.0, 20.0, 10.0, 10.0, 10.0],
        [instrument_confirmation],
        processing.MODEL_PARAMETERS,
    )

    assert processing.is_instrument_artifact_confirmation(
        instrument_confirmation
    ) is True
    assert corrected[5] == pytest.approx(10.0)
    assert artifacts[0]["type"] == "user_confirmed_instrument_artifact"


def test_processing_rebuilds_legacy_result_without_artifact_metadata(
    isolated_storage,
    monkeypatch,
):
    raw_bytes = (
        "FILETYPE=RAMAN SPECTRUM\nLASER=532\nTYPE=RamanShift\n"
        "XUNITS=1/cm\nXYDATA=\n1000,1200\n1002,1210\n"
    ).encode("utf-8")
    imported = post_import("legacy-missing-artifacts.txt", raw_bytes).json()

    legacy_result = {
        "processing_id": "legacy-processing",
        "file_id": imported["file_id"],
        "source_sha256": imported["sha256"],
        "processed_at": "2026-07-31T00:00:00+00:00",
        "model": {
            "name": processing.MODEL_NAME,
            "version": processing.MODEL_VERSION,
            "parameters": processing.MODEL_PARAMETERS,
        },
        "axes": {
            "x_label": "Raman shift (1/cm)",
            "raw_y_label": "Intensity",
            "corrected_y_label": "Baseline-corrected intensity",
            "substrate_corrected_y_label": "Substrate-corrected intensity",
        },
        "series": {
            "raw": [[1000.0, 1200.0]],
            "artifact_corrected": [[1000.0, 1200.0]],
            "baseline": [[1000.0, 100.0]],
            "corrected": [[1000.0, 1100.0]],
        },
        "peaks": [],
    }
    result_bytes = json.dumps(legacy_result).encode("utf-8")
    processed_directory = main.PROCESSED_DATA_DIR
    result_directory = processed_directory / imported["file_id"] / legacy_result["processing_id"]
    result_directory.mkdir(parents=True, exist_ok=True)
    result_path = result_directory / "result.json"
    result_path.write_bytes(result_bytes)

    existing_run = {
        "processing_id": legacy_result["processing_id"],
        "file_id": imported["file_id"],
        "model_name": processing.MODEL_NAME,
        "model_version": processing.MODEL_VERSION,
        "parameters_json": json.dumps({"legacy": True}),
        "source_sha256": imported["sha256"],
        "result_path": str(result_path),
        "result_sha256": hashlib.sha256(result_bytes).hexdigest(),
        "processed_at": legacy_result["processed_at"],
        "summary_json": json.dumps({"peak_count": 0}),
    }
    with database.connect_database() as connection:
        database.insert_processing_run(connection, existing_run)

    monkeypatch.setattr(
        database,
        "find_processing_run",
        lambda *args, **kwargs: existing_run,
    )

    result = processing.process_imported_file(
        imported,
        main.RAW_DATA_DIR,
        main.PROCESSED_DATA_DIR,
        main.REFERENCE_DATA_DIR,
    )

    assert result["processing_id"] != legacy_result["processing_id"]
    assert result["instrument_artifacts"] == []
    assert result["summary"]["instrument_artifacts"] == []
    assert result["summary"]["artifacts"] == []


def test_reference_spectrum_enables_evidence_based_carbon_analysis(
    isolated_storage,
):
    reference_response = post_reference(
        "known-cnt.txt",
        synthetic_raman_bytes(),
    )
    assert reference_response.status_code == 201
    reference = reference_response.json()
    assert reference["extraction_status"] == "ready"
    assert reference["evidence"]["peaks"]
    stored_reference = Path(reference["storage_path"]).read_bytes()
    assert stored_reference.endswith(synthetic_raman_bytes())
    assert b"MATERIAL=Carbon nanotube" in stored_reference
    assert reference["sha256"] == hashlib.sha256(stored_reference).hexdigest()

    import_response = post_import("unknown.txt", synthetic_raman_bytes())
    result = client.post(
        f"/files/{import_response.json()['file_id']}/process"
    ).json()

    identification = result["summary"]["identification"]
    assert identification["status"] == "identified"
    assert identification["material_system"] == "Carbon nanotube"
    assert identification["candidates"][0]["reference"]["reference_id"] == (
        reference["reference_id"]
    )
    analysis = result["summary"]["material_analysis"]
    assert analysis["d_band"]["position_cm-1"] == pytest.approx(1350, abs=8)
    assert analysis["g_band"]["position_cm-1"] == pytest.approx(1580, abs=8)
    assert analysis["id_ig_ratio"] == pytest.approx(0.5, abs=0.15)


def test_material_specific_analysis_reports_mos2_layer_screening():
    components = [
        {
            "center_cm-1": 384.5,
            "amplitude": 80.0,
            "fwhm_cm-1": 5.0,
            "area": 425.0,
            "assignment": {
                "material_system": "MoS2",
                "label": "E2g1 in-plane mode",
            },
        },
        {
            "center_cm-1": 406.0,
            "amplitude": 100.0,
            "fwhm_cm-1": 5.5,
            "area": 585.0,
            "assignment": {
                "material_system": "MoS2",
                "label": "A1g out-of-plane mode",
            },
        },
    ]

    analyses = processing.material_system_analyses(
        "MoS2",
        [],
        {"status": "fitted", "components": components},
        [float(value) for value in range(360, 431)],
        [0.1 * (value % 3) for value in range(360, 431)],
        processing.MODEL_PARAMETERS,
    )

    assert len(analyses) == 1
    analysis = analyses[0]
    assert analysis["status"] == "estimated"
    assert analysis["peak_separation_cm-1"] == pytest.approx(21.5)
    assert analysis["estimated_layer_count"] == "2 layers"
    assert analysis["interpretation_eligible"] is True
    assert analysis["peak_separation_uncertainty_cm-1"] > 0
    assert analysis["e2g1_band"]["measurement_source"] == (
        "deconvoluted_component"
    )
    assert analysis["references"][0]["doi"] == "10.1021/nn1003937"


def test_material_specific_analysis_reports_graphene_height_and_area_ratios():
    components = [
        {
            "center_cm-1": 1350.0,
            "amplitude": 20.0,
            "fwhm_cm-1": 30.0,
            "area": 200.0,
            "assignment": {"material_system": "Graphene", "label": "D band"},
        },
        {
            "center_cm-1": 1580.0,
            "amplitude": 100.0,
            "fwhm_cm-1": 20.0,
            "area": 800.0,
            "assignment": {"material_system": "Graphene", "label": "G band"},
        },
        {
            "center_cm-1": 2685.0,
            "amplitude": 180.0,
            "fwhm_cm-1": 28.0,
            "area": 1200.0,
            "assignment": {"material_system": "Graphene", "label": "2D band"},
        },
    ]

    analyses = processing.material_system_analyses(
        "Graphene",
        [],
        {"status": "fitted", "components": components},
        [1200.0, 1600.0, 2800.0],
        [0.0, 1.0, 0.0],
        processing.MODEL_PARAMETERS,
    )

    assert len(analyses) == 1
    analysis = analyses[0]
    assert analysis["status"] == "screening"
    assert analysis["id_ig_ratio"] == pytest.approx(0.2)
    assert analysis["i2d_ig_ratio"] == pytest.approx(1.8)
    assert analysis["area_d_g_ratio"] == pytest.approx(0.25)
    assert analysis["area_2d_g_ratio"] == pytest.approx(1.5)
    assert analysis["defect_signature"] == "moderate_d_band_activation"
    assert analysis["quantitative_defect_density"] is None
    assert analysis["references"][0]["doi"] == "10.1021/nl201432g"


def test_pdf_reference_preserves_bytes_and_page_evidence(
    isolated_storage,
    monkeypatch,
):
    pdf_bytes = b"%PDF-1.4\nexact user reference bytes\n%%EOF"

    class FakePage:
        def extract_text(self):
            return (
                "Raman study of anatase TiO2. Anatase bands occur at "
                "144 cm-1 and 639 cm\u22121. doi:10.1234/example.2026"
            )

    class FakeMetadata:
        title = "Raman spectroscopy of anatase TiO2"
        author = "A. Researcher; B. Scientist"

    class FakeReader:
        is_encrypted = False
        pages = [FakePage()]
        metadata = FakeMetadata()

        def __init__(self, _path, strict=False):
            assert strict is False

    monkeypatch.setattr(references, "PdfReader", FakeReader)
    response = post_reference(
        "anatase.pdf",
        pdf_bytes,
        material_system="Anatase TiO2",
        content_type="application/pdf",
    )

    assert response.status_code == 201
    result = response.json()
    assert Path(result["storage_path"]).read_bytes() == pdf_bytes
    assert result["sha256"] == hashlib.sha256(pdf_bytes).hexdigest()
    assert result["title"] == "Raman spectroscopy of anatase TiO2"
    assert result["material_system"] == "Anatase TiO2"
    assert result["technique"] == "Raman"
    assert result["source_url"] == "https://doi.org/10.1234/example.2026"
    assert "A. Researcher" in result["citation"]
    assert [peak["position_cm-1"] for peak in result["evidence"]["peaks"]] == [
        144.0,
        639.0,
    ]
    assert all(peak["page"] == 1 for peak in result["evidence"]["peaks"])


def test_declared_measurement_material_is_preserved_and_used(
    isolated_storage,
):
    metadata = {**DEFAULT_METADATA, "material_system": "Carbon nanotube"}
    import_response = post_import(
        "declared-cnt.txt",
        synthetic_raman_bytes(),
        metadata,
    )
    assert import_response.status_code == 201
    assert import_response.json()["material_system"] == "Carbon nanotube"

    result = client.post(
        f"/files/{import_response.json()['file_id']}/process"
    ).json()
    identification = result["summary"]["identification"]
    assert identification["status"] == "declared"
    assert identification["material_system"] == "Carbon nanotube"
    assert identification["reference_assessment"] == "no_reference_evidence"
    assert result["summary"]["material_analysis"] is not None


def test_duplicate_and_invalid_references_are_rejected(isolated_storage):
    contents = synthetic_raman_bytes()
    first = post_reference("one.txt", contents)
    duplicate = post_reference("two.txt", contents)
    invalid = post_reference("unsupported.exe", b"not a reference")
    malformed_pdf = post_reference(
        "malformed.pdf",
        b"not a PDF",
        content_type="application/pdf",
    )

    assert first.status_code == 201
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "duplicate_reference"
    assert invalid.status_code == 415
    assert invalid.json()["detail"]["code"] == "unsupported_reference_format"
    assert malformed_pdf.status_code == 422
    assert malformed_pdf.json()["detail"]["code"] == "invalid_reference_pdf"
    with database.connect_database() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM reference_sources"
        ).fetchone()[0] == 1


def test_reference_database_failure_removes_reference_file(
    isolated_storage,
    monkeypatch,
):
    def fail_insert(_connection, _reference):
        raise sqlite3.OperationalError("simulated reference insert failure")

    monkeypatch.setattr(database, "insert_reference_source", fail_insert)
    response = post_reference("known.txt", synthetic_raman_bytes())

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "reference_import_failed"
    assert [
        path
        for path in main.REFERENCE_DATA_DIR.rglob("*")
        if path.is_file()
    ] == []
    with database.connect_database() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM reference_sources"
        ).fetchone()[0] == 0


def test_reference_library_change_creates_new_identification_run(
    isolated_storage,
):
    import_response = post_import("sample.txt", synthetic_raman_bytes())
    file_id = import_response.json()["file_id"]
    first = client.post(f"/files/{file_id}/process").json()
    assert first["summary"]["identification"]["status"] == "unknown"

    assert post_reference("known.txt", synthetic_raman_bytes()).status_code == 201
    second = client.post(f"/files/{file_id}/process").json()

    assert second["processing_id"] != first["processing_id"]
    assert second["model"]["parameters"]["reference_library_sha256"] != (
        first["model"]["parameters"]["reference_library_sha256"]
    )
    assert second["summary"]["identification"]["status"] == "identified"
    with database.connect_database() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM processing_runs WHERE file_id = ?",
            (file_id,),
        ).fetchone()[0] == 2


def test_processing_rejects_non_raman_data(isolated_storage):
    metadata = {**DEFAULT_METADATA, "technique": "X-ray diffraction"}
    import_response = post_import(
        "xrd.csv",
        b"two_theta,intensity\n10,100\n11,120\n12,90\n",
        metadata,
    )

    response = client.post(
        f"/files/{import_response.json()['file_id']}/process"
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == (
        "unsupported_processing_technique"
    )


def test_processing_database_failure_removes_derived_result(
    isolated_storage,
    monkeypatch,
):
    import_response = post_import("raman.txt", synthetic_raman_bytes())

    def fail_processing_insert(_connection, _processing_run):
        raise sqlite3.OperationalError("simulated processing insert failure")

    monkeypatch.setattr(
        database,
        "insert_processing_run",
        fail_processing_insert,
    )
    response = client.post(
        f"/files/{import_response.json()['file_id']}/process"
    )

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "processing_failed"
    with database.connect_database() as connection:
        run_count = connection.execute(
            "SELECT COUNT(*) FROM processing_runs"
        ).fetchone()[0]
    assert run_count == 0
    processed_files = [
        path
        for path in main.PROCESSED_DATA_DIR.rglob("*")
        if path.is_file()
    ]
    assert processed_files == []


def test_duplicate_content_is_rejected_without_overwrite(isolated_storage):
    _, raw_directory = isolated_storage
    original_bytes = b"same immutable experiment"

    first_response = post_import("first.dat", original_bytes)
    duplicate_response = post_import("second.dat", original_bytes)

    assert first_response.status_code == 201
    assert duplicate_response.status_code == 409
    assert duplicate_response.json()["detail"] == {
        "code": "duplicate_file",
        "message": "An identical file has already been imported.",
        "existing_file_id": first_response.json()["file_id"],
    }
    assert database.list_imported_files() == [first_response.json()]
    raw_files = [
        path
        for path in raw_directory.rglob("*")
        if path.is_file() and ".staging" not in path.parts
    ]
    assert len(raw_files) == 1
    assert raw_files[0].read_bytes() == original_bytes
    assert list((raw_directory / ".staging").glob("*")) == []


def test_oversized_file_is_rejected_and_removed(
    isolated_storage,
    monkeypatch,
):
    _, raw_directory = isolated_storage
    monkeypatch.setattr(main, "MAX_FILE_SIZE_BYTES", 10)

    response = post_import(contents=b"01234567890")

    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "file_too_large"
    assert_no_import_artifacts(raw_directory)


def test_empty_file_is_rejected_and_removed(isolated_storage):
    _, raw_directory = isolated_storage

    response = post_import(contents=b"")

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "code": "empty_file",
        "message": "Empty files cannot be imported.",
    }
    assert_no_import_artifacts(raw_directory)


@pytest.mark.parametrize(
    ("metadata", "expected_code"),
    [
        ({**DEFAULT_METADATA, "technique": "   "}, "missing_metadata"),
        (
            {**DEFAULT_METADATA, "measurement_date": "29 July 2026"},
            "invalid_measurement_date",
        ),
    ],
)
def test_invalid_metadata_is_rejected(
    isolated_storage,
    metadata,
    expected_code,
):
    _, raw_directory = isolated_storage

    response = post_import(metadata=metadata)

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == expected_code
    assert_no_import_artifacts(raw_directory)


def test_database_failure_rolls_back_new_file(
    isolated_storage,
    monkeypatch,
):
    _, raw_directory = isolated_storage

    def fail_insert(_connection, _metadata):
        raise sqlite3.OperationalError("simulated insert failure")

    monkeypatch.setattr(database, "insert_imported_file", fail_insert)

    response = post_import()

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "import_failed"
    assert_no_import_artifacts(raw_directory)


def test_file_publish_failure_rolls_back_database(
    isolated_storage,
    monkeypatch,
):
    _, raw_directory = isolated_storage

    def fail_replace(_source, _destination):
        raise OSError("simulated file publish failure")

    monkeypatch.setattr(main.os, "replace", fail_replace)

    response = post_import()

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "import_failed"
    assert_no_import_artifacts(raw_directory)


def test_initialize_database_migrates_existing_metadata_table(
    tmp_path,
    monkeypatch,
):
    legacy_database = tmp_path / "legacy.db"
    monkeypatch.setattr(database, "DATABASE_PATH", legacy_database)
    with sqlite3.connect(legacy_database) as connection:
        connection.execute(
            """
            CREATE TABLE imported_files (
                file_id TEXT PRIMARY KEY,
                original_filename TEXT NOT NULL,
                content_type TEXT,
                size_bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                storage_path TEXT NOT NULL,
                imported_at TEXT NOT NULL
            )
            """
        )

    database.initialize_database()

    with database.connect_database() as connection:
        column_names = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(imported_files)")
        }
        indexes = {
            row["name"]
            for row in connection.execute("PRAGMA index_list(imported_files)")
        }
        processing_table = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name = 'processing_runs'
            """
        ).fetchone()
        reference_table = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name = 'reference_sources'
            """
        ).fetchone()
        training_table = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name = 'training_examples'
            """
        ).fetchone()
        chat_table = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name = 'copilot_messages'
            """
        ).fetchone()
        clarification_table = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name = 'clarification_responses'
            """
        ).fetchone()
        metadata_revision_table = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name = 'import_metadata_revisions'
            """
        ).fetchone()
        attachment_table = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name = 'file_attachments'
            """
        ).fetchone()
        substrate_feedback_table = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name = 'substrate_peak_feedback'
            """
        ).fetchone()
    assert {
        "relative_path",
        "technique",
        "material_system",
        "sample_id",
        "measurement_date",
        "instrument",
        "operator",
        "notes",
        "substrate",
        "measurement_role",
        "updated_at",
        "archived_at",
    }.issubset(column_names)
    assert "idx_imported_files_sha256" in indexes
    assert processing_table is not None
    assert reference_table is not None
    assert training_table is not None
    assert chat_table is not None
    assert clarification_table is not None
    assert metadata_revision_table is not None
    assert attachment_table is not None
    assert substrate_feedback_table is not None


def test_initialize_database_preserves_legacy_duplicate_records(
    tmp_path,
    monkeypatch,
):
    legacy_database = tmp_path / "legacy-duplicates.db"
    monkeypatch.setattr(database, "DATABASE_PATH", legacy_database)
    with sqlite3.connect(legacy_database) as connection:
        connection.execute(
            """
            CREATE TABLE imported_files (
                file_id TEXT PRIMARY KEY,
                original_filename TEXT NOT NULL,
                content_type TEXT,
                size_bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                storage_path TEXT NOT NULL,
                imported_at TEXT NOT NULL
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO imported_files VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("one", "one.dat", None, 1, "a" * 64, "raw/one", "2026-01-01"),
                ("two", "two.dat", None, 1, "a" * 64, "raw/two", "2026-01-02"),
            ],
        )

    database.initialize_database()

    with database.connect_database() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM imported_files"
        ).fetchone()[0]
        trigger = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'trigger'
              AND name = 'reject_duplicate_imported_file_sha256'
            """
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="duplicate sha256"):
            connection.execute(
                """
                INSERT INTO imported_files (
                    file_id,
                    original_filename,
                    size_bytes,
                    sha256,
                    storage_path,
                    imported_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("three", "three.dat", 1, "a" * 64, "raw/three", "2026-01-03"),
            )

    assert count == 2
    assert trigger is not None
