import hashlib
from pathlib import Path
from fastapi.testclient import TestClient

import comparison
import database
import main
from main import app

client = TestClient(app)


def test_comparison_page_is_available_as_a_dedicated_workspace():
    response = client.get("/comparison")

    assert response.status_code == 200
    assert "Comparison page" in response.text
    assert 'id="comparison-canvas"' in response.text
    assert 'id="candidate-list"' in response.text
    assert 'id="dataset-search"' not in response.text
    assert 'id="substrate-filter"' not in response.text
    assert 'id="select-filtered"' not in response.text
    assert 'id="process-dataset-selection"' not in response.text
    assert 'id="edit-dialog"' not in response.text
    assert 'id="series-select"' in response.text
    assert 'id="offset-input"' in response.text
    assert ".plot-controls > label { align-self: flex-start; }" in response.text
    assert 'id="zoom-range"' in response.text
    assert 'id="reset-view"' in response.text
    assert 'id="peak-picker"' in response.text
    assert 'id="reprocess-peaks"' in response.text
    assert "Selected deconvoluted peaks" in response.text
    assert 'id="plot-tooltip"' in response.text
    assert 'id="cursor-markers"' in response.text
    assert 'id="peak-details"' in response.text
    assert 'id="peak-details-body"' in response.text
    assert 'id="analysis-section"' in response.text
    assert 'id="analysis-list"' in response.text
    assert 'id="custom-analysis-form"' in response.text
    assert 'id="analysis-canvas"' in response.text
    assert "function loadGroupAnalyses" in response.text
    assert "function reprocessSelectedForPeaks" in response.text
    assert 'fetch("/files/process/batch"' in response.text
    assert "function addCustomAnalysis" in response.text
    assert "function inspectPlot" in response.text
    assert "function sampleSeriesAtX" in response.text
    assert "peak.seriesIndex!==activeSeries.seriesIndex" in response.text
    assert "Peak locked" in response.text


def setup_test_files(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    raw_dir = tmp_path / "raw"

    monkeypatch.setattr(database, "DATABASE_PATH", db_path)
    monkeypatch.setattr(main, "RAW_DATA_DIR", raw_dir)
    monkeypatch.setattr(comparison, "RAW_DATA_DIR", raw_dir)

    database.initialize_database()

    # File 1: Sample Raman file (G-band at ~1580 cm-1)
    file_id_1 = "file-sample-001"
    dir_1 = raw_dir / file_id_1
    dir_1.mkdir(parents=True, exist_ok=True)
    raw_path_1 = dir_1 / "graphene_sample.txt"

    lines_1 = ["# Raman shift, Intensity"]
    for x in range(1000, 2000, 5):
        # Peak near 1580
        y = 100.0 + 500.0 * (1.0 / (1.0 + ((x - 1580.0) / 15.0) ** 2))
        lines_1.append(f"{x:.1f}, {y:.2f}")
    bytes_1 = "\n".join(lines_1).encode("utf-8")
    raw_path_1.write_bytes(bytes_1)

    # File 2: Reference Raman file (G-band slightly shifted to ~1585 cm-1)
    file_id_2 = "file-ref-002"
    dir_2 = raw_dir / file_id_2
    dir_2.mkdir(parents=True, exist_ok=True)
    raw_path_2 = dir_2 / "graphene_ref.txt"

    lines_2 = ["# Raman shift, Intensity"]
    for x in range(1000, 2000, 5):
        # Peak near 1585
        y = 100.0 + 480.0 * (1.0 / (1.0 + ((x - 1585.0) / 15.0) ** 2))
        lines_2.append(f"{x:.1f}, {y:.2f}")
    bytes_2 = "\n".join(lines_2).encode("utf-8")
    raw_path_2.write_bytes(bytes_2)

    meta_1 = {
        "file_id": file_id_1,
        "original_filename": "graphene_sample.txt",
        "content_type": "text/plain",
        "size_bytes": len(bytes_1),
        "sha256": hashlib.sha256(bytes_1).hexdigest(),
        "storage_path": str(raw_path_1),
        "imported_at": "2026-08-01T00:00:00+00:00",
        "technique": "Raman",
        "material_system": "Graphene",
        "sample_id": "T-12, specimen A",
        "measurement_date": "2026-08-01",
        "instrument": "LabRAM",
        "operator": "Dr. A",
        "notes": "Test sample",
    }

    meta_2 = {
        "file_id": file_id_2,
        "original_filename": "graphene_ref.txt",
        "content_type": "text/plain",
        "size_bytes": len(bytes_2),
        "sha256": hashlib.sha256(bytes_2).hexdigest(),
        "storage_path": str(raw_path_2),
        "imported_at": "2026-08-01T00:00:00+00:00",
        "technique": "Raman",
        "material_system": "Graphene Reference",
        "sample_id": "T-12, specimen B",
        "measurement_date": "2026-08-01",
        "instrument": "LabRAM",
        "operator": "Dr. A",
        "notes": "Test ref",
    }

    with database.connect_database() as conn:
        database.insert_imported_file(conn, meta_1)
        database.insert_imported_file(conn, meta_2)

    return file_id_1, file_id_2


def test_compare_spectra_success(tmp_path, monkeypatch):
    file_id_1, file_id_2 = setup_test_files(tmp_path, monkeypatch)

    response = client.post(
        "/files/compare",
        json={
            "sample_file_id": file_id_1,
            "reference_file_id": file_id_2,
            "normalization": "vector",
        },
    )
    assert response.status_code == 200
    data = response.json()

    assert data["sample_file_id"] == file_id_1
    assert data["reference_file_id"] == file_id_2
    assert data["normalization"] == "vector"
    assert "metrics" in data
    assert data["metrics"]["pearson_correlation_r"] > 0.95
    assert data["metrics"]["cosine_similarity"] > 0.95
    assert "differential_series" in data
    assert len(data["differential_series"]) == 500


def test_compare_spectra_file_not_found(tmp_path, monkeypatch):
    file_id_1, _ = setup_test_files(tmp_path, monkeypatch)

    response = client.post(
        "/files/compare",
        json={
            "sample_file_id": file_id_1,
            "reference_file_id": "non-existent-file-id",
            "normalization": "vector",
        },
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "file_not_found"


def test_compare_spectra_invalid_normalization(tmp_path, monkeypatch):
    file_id_1, file_id_2 = setup_test_files(tmp_path, monkeypatch)

    response = client.post(
        "/files/compare",
        json={
            "sample_file_id": file_id_1,
            "reference_file_id": file_id_2,
            "normalization": "invalid_norm",
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_normalization"


def test_correlated_spectra_preselects_same_sample_family(tmp_path, monkeypatch):
    file_id_1, file_id_2 = setup_test_files(tmp_path, monkeypatch)

    response = client.get(f"/files/{file_id_1}/correlated")

    assert response.status_code == 200
    data = response.json()
    assert data["sample_family"] == "t-12"
    assert data["automatically_selected_file_ids"] == [file_id_1, file_id_2]
    related = next(
        candidate
        for candidate in data["candidates"]
        if candidate["file_id"] == file_id_2
    )
    assert related["automatically_selected"] is True
    assert "same_sample_family" in related["match_reasons"]
    assert related["pearson_correlation_r"] > 0.95


def test_multi_spectrum_overlay_returns_one_normalized_graph_payload(
    tmp_path,
    monkeypatch,
):
    file_id_1, file_id_2 = setup_test_files(tmp_path, monkeypatch)
    stored_before = {
        record["file_id"]: Path(record["storage_path"]).read_bytes()
        for record in database.list_imported_files()
    }

    response = client.post(
        "/files/compare/overlay",
        json={
            "file_ids": [file_id_1, file_id_2],
            "normalization": "max",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["file_ids"] == [file_id_1, file_id_2]
    assert data["normalization"] == "max"
    assert data["points_count"] == 500
    assert len(data["series"]) == 2
    assert all(len(series["points"]) == 500 for series in data["series"])
    assert max(point[1] for point in data["series"][0]["points"]) == 1.0
    for record in database.list_imported_files():
        assert Path(record["storage_path"]).read_bytes() == stored_before[
            record["file_id"]
        ]


def test_multi_spectrum_overlay_requires_two_files(tmp_path, monkeypatch):
    file_id_1, _file_id_2 = setup_test_files(tmp_path, monkeypatch)

    response = client.post(
        "/files/compare/overlay",
        json={"file_ids": [file_id_1], "normalization": "max"},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "insufficient_overlay_selection"


def test_overlay_supports_processed_series_and_per_spectrum_offset(
    tmp_path,
    monkeypatch,
):
    file_id_1, file_id_2 = setup_test_files(tmp_path, monkeypatch)

    def latest_result(file_id, _processed_directory):
        base = 10.0 if file_id == file_id_1 else 20.0
        return {
            "series": {
                "corrected": [
                    [1000.0, base],
                    [1500.0, base + 5.0],
                    [2000.0, base + 10.0],
                ]
            }
        }

    monkeypatch.setattr(
        comparison.processing,
        "latest_processing_result",
        latest_result,
    )

    response = client.post(
        "/files/compare/overlay",
        json={
            "file_ids": [file_id_1, file_id_2],
            "normalization": "none",
            "series_name": "corrected",
            "offset": 100.0,
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["series_name"] == "corrected"
    assert data["offset"] == 100.0
    assert data["series"][0]["applied_offset"] == 0.0
    assert data["series"][1]["applied_offset"] == 100.0
    assert data["series"][0]["points"][0][1] == 10.0
    assert data["series"][1]["points"][0][1] == 120.0


def test_selected_deconvolution_peaks_are_catalogued_and_plotted_across_spectra(
    tmp_path,
    monkeypatch,
):
    file_id_1, file_id_2 = setup_test_files(tmp_path, monkeypatch)
    x_values = [150.0, 178.0, 220.0, 405.0, 450.0]

    def latest_result(file_id, _processed_directory):
        shift = 0.0 if file_id == file_id_1 else 1.5
        return {
            "series": {"corrected": [[x_value, 0.0] for x_value in x_values]},
            "deconvolution": {
                "components": [
                    {
                        "seed_center_cm-1": 178.0,
                        "center_cm-1": 178.0 + shift,
                        "amplitude": 5.0,
                        "fwhm_cm-1": 8.0,
                        "area": 42.0,
                        "mode_group": "Ag",
                        "assignment": {
                            "material_system": "Cs2ZnBr4",
                            "label": "Ag symmetric Zn-Br mode",
                        },
                        "series": [
                            [x_value, 5.0 if x_value == 178.0 else 0.0]
                            for x_value in x_values
                        ],
                    },
                    {
                        "seed_center_cm-1": 405.0,
                        "center_cm-1": 405.0 - shift,
                        "amplitude": 7.0,
                        "fwhm_cm-1": 10.0,
                        "area": 70.0,
                        "mode_group": "A1g",
                        "assignment": {
                            "material_system": "MoS2",
                            "label": "A1g out-of-plane mode",
                        },
                        "series": [
                            [x_value, 7.0 if x_value == 405.0 else 0.0]
                            for x_value in x_values
                        ],
                    },
                ]
            },
        }

    monkeypatch.setattr(
        comparison.processing,
        "latest_processing_result",
        latest_result,
    )

    catalog_response = client.post(
        "/files/compare/deconvolution-peaks",
        json={"file_ids": [file_id_1, file_id_2]},
    )
    assert catalog_response.status_code == 200
    catalog = catalog_response.json()
    assert [peak["label"] for peak in catalog["peaks"]] == [
        "Ag symmetric Zn-Br mode",
        "A1g out-of-plane mode",
    ]
    assert all(peak["support_count"] == 2 for peak in catalog["peaks"])

    selections = [
        {
            "center_cm_1": peak["center_cm_1"],
            "label": peak["label"],
            "tolerance_cm_1": peak["tolerance_cm_1"],
        }
        for peak in catalog["peaks"]
    ]
    overlay_response = client.post(
        "/files/compare/overlay",
        json={
            "file_ids": [file_id_1, file_id_2],
            "normalization": "none",
            "series_name": "selected_deconvolution_peaks",
            "offset": 10.0,
            "peak_selections": selections,
        },
    )
    assert overlay_response.status_code == 200
    overlay = overlay_response.json()
    assert overlay["series_name"] == "selected_deconvolution_peaks"
    assert len(overlay["peak_selections"]) == 2
    assert [series["matched_component_count"] for series in overlay["series"]] == [
        2,
        2,
    ]
    assert overlay["series"][1]["applied_offset"] == 10.0
    first_component = overlay["series"][0]["matched_components"][0]
    assert first_component["seed_center_cm_1"] == 178.0
    assert first_component["label"] == "Ag symmetric Zn-Br mode"
    assert first_component["material_system"] == "Cs2ZnBr4"
    assert first_component["amplitude"] == 5.0
    assert first_component["fwhm_cm_1"] == 8.0
    assert first_component["area"] == 42.0
    assert first_component["mode_group"] == "Ag"


def test_group_analysis_catalog_suggests_literature_and_custom_peak_plots(
    tmp_path,
    monkeypatch,
):
    file_id_1, file_id_2 = setup_test_files(tmp_path, monkeypatch)

    def latest_result(file_id, _processed_directory):
        shift = 0.0 if file_id == file_id_1 else 1.0
        return {
            "processing_id": f"processing-{file_id}",
            "summary": {
                "material_analyses": [
                    {
                        "analysis": "mos2_layer_screening_from_mode_separation",
                        "peak_separation_cm-1": 21.5 + shift,
                        "e2g1_band": {"position_cm-1": 384.5 - shift},
                        "a1g_band": {"position_cm-1": 406.0},
                        "references": [
                            {
                                "title": "MoS2 layer reference",
                                "citation": "Lee et al. (2010)",
                                "doi": "10.1021/nn1003937",
                                "source_url": "https://doi.org/10.1021/nn1003937",
                            }
                        ],
                    }
                ]
            },
            "deconvolution": {
                "components": [
                    {
                        "center_cm-1": 384.5 - shift,
                        "amplitude": 80.0 + shift,
                        "fwhm_cm-1": 5.0,
                        "area": 425.0,
                        "assignment": {
                            "material_system": "MoS2",
                            "label": "E2g1 in-plane mode",
                        },
                    },
                    {
                        "center_cm-1": 406.0,
                        "amplitude": 100.0 + shift,
                        "fwhm_cm-1": 5.5,
                        "area": 585.0,
                        "assignment": {
                            "material_system": "MoS2",
                            "label": "A1g out-of-plane mode",
                        },
                    },
                ]
            },
        }

    monkeypatch.setattr(
        comparison.processing,
        "latest_processing_result",
        latest_result,
    )

    response = client.post(
        "/files/compare/analyses",
        json={"file_ids": [file_id_1, file_id_2]},
    )

    assert response.status_code == 200
    catalog = response.json()
    assert [item["analysis_id"] for item in catalog["suggestions"]][:2] == [
        "literature-mos2-mode-separation",
        "literature-mos2-mode-positions",
    ]
    separation = catalog["suggestions"][0]
    assert separation["support_count"] == 2
    assert [
        item["value"] for item in separation["traces"][0]["observations"]
    ] == [21.5, 22.5]
    assert separation["references"][0]["doi"] == "10.1021/nn1003937"
    assert len(catalog["custom_peak_options"]) == 2
    assert catalog["custom_peak_options"][0]["observations"][0][
        "amplitude"
    ] == 80.0


def test_group_analysis_catalog_suggests_graphitic_carbon_ratios(
    tmp_path,
    monkeypatch,
):
    file_id_1, file_id_2 = setup_test_files(tmp_path, monkeypatch)

    def latest_result(file_id, _processed_directory):
        ratio = 0.2 if file_id == file_id_1 else 0.35
        return {
            "processing_id": f"processing-{file_id}",
            "summary": {
                "material_analyses": [
                    {
                        "analysis": "graphitic_carbon_raman_quality_ratios",
                        "id_ig_ratio": ratio,
                        "i2d_ig_ratio": 1.8 - ratio,
                        "d_band": {"position_cm-1": 1350.0},
                        "g_band": {"position_cm-1": 1580.0},
                        "two_d_band": {"position_cm-1": 2685.0},
                        "references": [
                            {
                                "citation": "Cançado et al. (2011)",
                                "doi": "10.1021/nl201432g",
                                "source_url": "https://doi.org/10.1021/nl201432g",
                            }
                        ],
                    }
                ]
            },
            "deconvolution": {"components": []},
        }

    monkeypatch.setattr(
        comparison.processing,
        "latest_processing_result",
        latest_result,
    )

    response = client.post(
        "/files/compare/analyses",
        json={"file_ids": [file_id_1, file_id_2]},
    )

    assert response.status_code == 200
    suggestions = response.json()["suggestions"]
    assert [item["analysis_id"] for item in suggestions] == [
        "literature-carbon-band-ratios",
        "literature-carbon-band-positions",
    ]
    assert suggestions[0]["support_count"] == 2
    assert suggestions[0]["traces"][0]["observations"][1]["value"] == 0.35


def test_t12_composite_metadata_suggests_mos2_analysis_before_processing(
    tmp_path,
    monkeypatch,
):
    file_id_1, file_id_2 = setup_test_files(tmp_path, monkeypatch)
    with database.connect_database() as connection:
        connection.execute(
            """
            UPDATE imported_files
            SET material_system = ?
            WHERE file_id IN (?, ?)
            """,
            (
                "MoS2 / Cs2ZnBr4 / SiO2 / Si",
                file_id_1,
                file_id_2,
            ),
        )
        connection.commit()

    def no_processed_result(file_id, _processed_directory):
        raise comparison.processing.ProcessingError(
            404,
            "processing_not_found",
            f"No processed result exists for {file_id}.",
        )

    monkeypatch.setattr(
        comparison.processing,
        "latest_processing_result",
        no_processed_result,
    )

    response = client.post(
        "/files/compare/analyses",
        json={"file_ids": [file_id_1, file_id_2]},
    )

    assert response.status_code == 200
    catalog = response.json()
    assert [item["analysis_id"] for item in catalog["suggestions"]] == [
        "literature-mos2-mode-separation",
        "literature-mos2-mode-positions",
    ]
    assert all(item["support_count"] == 0 for item in catalog["suggestions"])
    assert catalog["suggestions"][0]["references"][0]["doi"] == (
        "10.1021/nn1003937"
    )
    assert len(catalog["unavailable_files"]) == 2


def test_review_required_deconvolution_is_excluded_from_peak_comparison(
    tmp_path,
    monkeypatch,
):
    file_id_1, file_id_2 = setup_test_files(tmp_path, monkeypatch)

    def latest_result(_file_id, _processed_directory):
        return {
            "processing_id": "poor-fit",
            "summary": {"material_analyses": []},
            "series": {"corrected": [[100.0, 0.0], [200.0, 1.0]]},
            "deconvolution": {
                "quality": {
                    "accepted": False,
                    "acceptance_status": "review_required",
                },
                "components": [
                    {
                        "center_cm-1": 178.0,
                        "amplitude": 5.0,
                        "fwhm_cm-1": 8.0,
                        "area": 42.0,
                        "assignment": {"label": "Rejected component"},
                        "series": [[100.0, 0.0], [200.0, 1.0]],
                    }
                ],
            },
        }

    monkeypatch.setattr(
        comparison.processing,
        "latest_processing_result",
        latest_result,
    )

    peak_catalog = client.post(
        "/files/compare/deconvolution-peaks",
        json={"file_ids": [file_id_1, file_id_2]},
    ).json()
    analysis_catalog = client.post(
        "/files/compare/analyses",
        json={"file_ids": [file_id_1, file_id_2]},
    ).json()
    overlay = client.post(
        "/files/compare/overlay",
        json={
            "file_ids": [file_id_1, file_id_2],
            "series_name": "selected_deconvolution_peaks",
            "peak_selections": [
                {
                    "center_cm_1": 178.0,
                    "label": "Rejected component",
                    "tolerance_cm_1": 12.0,
                }
            ],
        },
    )
    residual_overlay = client.post(
        "/files/compare/overlay",
        json={
            "file_ids": [file_id_1, file_id_2],
            "series_name": "deconvolution_residual",
        },
    )

    assert peak_catalog["peaks"] == []
    assert {
        item["reason"] for item in peak_catalog["unavailable_files"]
    } == {"deconvolution_fit_review_required"}
    assert analysis_catalog["custom_peak_options"] == []
    assert overlay.status_code == 422
    assert overlay.json()["detail"]["code"] == (
        "deconvolution_fit_review_required"
    )
    assert residual_overlay.status_code == 422
    assert residual_overlay.json()["detail"]["code"] == (
        "deconvolution_fit_review_required"
    )


def test_outdated_deconvolution_is_marked_for_current_model_reprocessing(
    tmp_path,
    monkeypatch,
):
    file_id_1, file_id_2 = setup_test_files(tmp_path, monkeypatch)

    def outdated_result(_file_id, _processed_directory):
        return {
            "model": {"version": "2.34.0"},
            "deconvolution": {
                "status": "fitted",
                "quality": {"accepted": True},
                "components": [
                    {
                        "center_cm-1": 405.0,
                        "amplitude": 10.0,
                        "fwhm_cm-1": 5.0,
                        "area": 53.0,
                        "assignment": {"label": "A1g"},
                    }
                ],
            },
        }

    monkeypatch.setattr(
        comparison.processing,
        "latest_processing_result",
        outdated_result,
    )

    catalog = client.post(
        "/files/compare/deconvolution-peaks",
        json={"file_ids": [file_id_1, file_id_2]},
    ).json()

    assert catalog["peaks"] == []
    assert {
        item["reason"] for item in catalog["unavailable_files"]
    } == {"outdated_processing_model"}
    assert {
        item["required_model_version"]
        for item in catalog["unavailable_files"]
    } == {comparison.processing.MODEL_VERSION}
