import hashlib
import json
import math
from pathlib import Path

import httpx
import pytest

import online_references
import processing


def jcamp_bytes(peak_position: float = 520.0) -> bytes:
    lines = [
        "##TITLE=TEST",
        "##JCAMP-DX=4.24",
        "##DATA TYPE=RAMAN SPECTRUM",
        "##XUNITS=1/CM",
        "##XYPOINTS=(XY..XY)",
    ]
    for x_value in range(100, 901, 2):
        intensity = 10 + 1000 * math.exp(
            -((x_value - peak_position) / 12) ** 2
        )
        lines.append(f"{x_value},{intensity:.8f}")
    lines.append("##END=")
    return ("\n".join(lines) + "\n").encode("utf-8")


def test_parse_rod_catalog_and_jcamp_points():
    html = """
    <table id="result_table"><tr>
      <td><a href="1000001.html">1000001</a></td>
      <td><a href="1000001.rod">ROD</a></td>
      <td>Fe2 O3</td><td>R -3 c</td><td></td><td></td>
      <td>A. Author<br/>Hematite Raman spectrum<br/><i>Journal</i>, 2020</td>
    </tr></table>
    """

    catalog = online_references.parse_rod_catalog(html, 10)
    points = online_references.parse_jcamp_points(jcamp_bytes())

    assert catalog == [
        {
            "rod_id": "1000001",
            "formula": "Fe2O3",
            "authors": "A. Author",
            "title": "Hematite Raman spectrum",
            "citation": "A. Author. Hematite Raman spectrum. Journal, 2020",
        }
    ]
    assert len(points) == 401
    assert max(points, key=lambda point: point[1])[0] == 520.0


def test_rod_redirect_must_remain_on_allowlisted_https_host():
    allowed = httpx.Response(
        200,
        request=httpx.Request(
            "GET", "https://solsa.crystallography.net/rod/1000000.jdx"
        ),
    )
    rejected = httpx.Response(
        200,
        request=httpx.Request("GET", "https://example.org/1000000.jdx"),
    )

    online_references.validate_rod_response_url(allowed)
    with pytest.raises(online_references.OnlineReferenceError) as error:
        online_references.validate_rod_response_url(rejected)

    assert error.value.code == "rod_redirect_rejected"


def test_curated_fingerprints_identify_current_material_families():
    references = online_references.curated_references()
    parameters = processing.MODEL_PARAMETERS

    cnt = processing.identify_material_system(
        [
            {"position_cm-1": 271.0},
            {"position_cm-1": 1589.0},
        ],
        references,
        parameters,
    )
    vo2 = processing.identify_material_system(
        [
            {"position_cm-1": value}
            for value in (140, 195, 223, 260, 308, 388, 438, 498, 614, 821)
        ],
        references,
        parameters,
    )
    graphene = processing.identify_material_system(
        [
            {"position_cm-1": 522.0},
            {"position_cm-1": 1593.0},
            {"position_cm-1": 2681.0},
        ],
        references,
        parameters,
    )

    assert cnt["material_system"] == "Carbon nanotube"
    assert vo2["material_system"] == "VO2 (M1)"
    assert graphene["material_system"] == (
        "Graphene on crystalline silicon substrate"
    )


def test_composite_material_peaks_receive_reference_backed_labels():
    references = online_references.curated_references()
    peaks = [
        {"position_cm-1": value, "intensity": 100.0, "prominence": 50.0}
        for value in (178.8, 384.6, 406.2, 453.4, 519.6, 3147.1)
    ]
    identification = {
        "status": "declared",
        "material_system": "MoS2/Cs2ZnBr4/SiO2",
    }

    labelled = processing.assign_peak_labels(
        peaks,
        references,
        "MoS2/Cs2ZnBr4/SiO2",
        identification,
        processing.MODEL_PARAMETERS,
    )

    assert labelled[0]["assignments"][0]["material_system"] == "Cs2ZnBr4"
    assert labelled[0]["assignments"][0]["label"] == (
        "ν1(A1) totally symmetric Zn-Br breathing stretch of [ZnBr4]2−"
    )
    assert labelled[1]["assignments"][0]["label"] == "E2g1 in-plane mode"
    assert labelled[2]["assignments"][0]["label"] == (
        "A1g out-of-plane mode"
    )
    assert labelled[3]["assignments"][0]["label"] == (
        "2LA(M) second-order mode"
    )
    assert any(
        assignment["material_system"] == "SiO2"
        for assignment in labelled[3]["assignments"]
    )
    assert labelled[4]["assignments"][0]["material_system"] == (
        "Crystalline silicon"
    )
    assert labelled[4]["assignments"][0]["role"] == "substrate"
    assert labelled[5]["assignments"] == []
    assert labelled[1]["assignments"][0]["reference"]["source_url"].startswith(
        "https://"
    )


def test_reference_guided_detection_recovers_weak_mos2_doublet():
    x_values = [float(value) for value in range(340, 561, 2)]
    intensities = []
    for x_value in x_values:
        noise = 2.0 if int(x_value) % 8 == 0 else -2.0
        weak_e2g = 90 * math.exp(-((x_value - 384) / 5) ** 2)
        weak_a1g = 150 * math.exp(-((x_value - 406) / 6) ** 2)
        saturated_silicon = 10_000 * math.exp(-((x_value - 520) / 5) ** 2)
        intensities.append(noise + weak_e2g + weak_a1g + saturated_silicon)

    generic = processing.detect_peaks(x_values, intensities)
    guided = processing.reference_guided_peak_candidates(
        x_values,
        intensities,
        online_references.curated_references(),
        "MoS2 / Cs2ZnBr4 / SiO2 / Si",
        processing.MODEL_PARAMETERS,
    )
    labelled = processing.assign_peak_labels(
        guided,
        online_references.curated_references(),
        "MoS2 / Cs2ZnBr4 / SiO2 / Si",
        {"status": "declared", "material_system": "MoS2"},
        processing.MODEL_PARAMETERS,
    )

    assert all(
        not 380 <= peak["position_cm-1"] <= 411
        for peak in generic
    )
    mos2 = [
        peak
        for peak in labelled
        if 380 <= peak["position_cm-1"] <= 411
    ]
    assert [peak["position_cm-1"] for peak in mos2] == [384.0, 406.0]
    assert [peak["assignments"][0]["label"] for peak in mos2] == [
        "E2g1 in-plane mode",
        "A1g out-of-plane mode",
    ]
    assert all(
        peak["detection_source"] == "reference_guided_local_prominence"
        for peak in mos2
    )


def test_specific_mos2_mode_outranks_a_generic_exact_training_band():
    generic_evidence = {
        "provider": "user_confirmed_training",
        "match_tolerance_cm-1": 6.0,
        "peaks": [
            {
                "position_cm-1": 383.95,
                "assignment": "Reference band",
            }
        ],
    }
    generic_reference = {
        "reference_id": "generic-composite-training",
        "material_system": "MoS2 / Cs2ZnBr4 / SiO2 / Si",
        "title": "Generic training spectrum",
        "citation": "User-confirmed measurement",
        "source_url": None,
        "sha256": "a" * 64,
        "extraction_status": "ready",
        "evidence_json": json.dumps(generic_evidence),
    }

    labelled = processing.assign_peak_labels(
        [{"position_cm-1": 383.95, "intensity": 40.0, "prominence": 35.0}],
        [generic_reference, *online_references.curated_references()],
        "MoS2 / Cs2ZnBr4 / SiO2 / Si",
        {"status": "declared", "material_system": "MoS2"},
        processing.MODEL_PARAMETERS,
    )

    assert labelled[0]["assignments"][0]["material_system"] == "MoS2"
    assert labelled[0]["assignments"][0]["label"] == "E2g1 in-plane mode"


def test_cs2znbr4_nu3_mode_outranks_a_generic_training_band():
    generic_evidence = {
        "provider": "user_confirmed_training",
        "match_tolerance_cm-1": 6.0,
        "peaks": [
            {
                "position_cm-1": 216.4,
                "assignment": "Reference band",
            }
        ],
    }
    generic_reference = {
        "reference_id": "generic-cs2znbr4-training",
        "material_system": "MoS2 / Cs2ZnBr4 / SiO2 / Si",
        "title": "Generic training spectrum",
        "citation": "User-confirmed measurement",
        "source_url": None,
        "sha256": "b" * 64,
        "extraction_status": "ready",
        "evidence_json": json.dumps(generic_evidence),
    }

    labelled = processing.assign_peak_labels(
        [{"position_cm-1": 216.4, "intensity": 40.0, "prominence": 35.0}],
        [generic_reference, *online_references.curated_references()],
        "MoS2 / Cs2ZnBr4 / SiO2 / Si",
        {"status": "declared", "material_system": "Cs2ZnBr4"},
        processing.MODEL_PARAMETERS,
    )

    assignment = labelled[0]["assignments"][0]
    assert assignment["material_system"] == "Cs2ZnBr4"
    assert assignment["label"] == (
        "ν3 antisymmetric Zn-Br stretching mode of [ZnBr4]2−"
    )
    assert assignment["reference_range_cm-1"] == [204.0, 225.0]
    assert assignment["reference"]["provider"] == (
        "curated_primary_literature"
    )


def test_mos2_disorder_family_is_specific_and_keeps_258_provisional():
    peaks = [
        {"position_cm-1": value, "intensity": 40.0, "prominence": 35.0}
        for value in (227.0, 258.0, 351.0, 373.0, 385.0, 407.0)
    ]
    labelled = processing.assign_peak_labels(
        peaks,
        online_references.curated_references(),
        "MoS2 / Cs2ZnBr4 / SiO2 / Si",
        {"status": "declared", "material_system": "MoS2"},
        processing.MODEL_PARAMETERS,
    )

    assert [peak["assignments"][0]["label"] for peak in labelled] == [
        "LA(M) defect-activated phonon",
        "Unidentified disorder-related mode near 250-258 cm-1",
        "TO(M) defect-activated phonon",
        "LO(M) defect-activated phonon",
        "E2g1 in-plane mode",
        "A1g out-of-plane mode",
    ]
    provisional = labelled[1]["assignments"][0]
    assert provisional["provisional"] is True
    assert provisional["confidence"] == "provisional"
    assert provisional["possible_origins"] == [
        "MoS2 disorder-induced mode with unresolved branch assignment",
        "Polybromide-related vibration in bromide-containing systems",
    ]


def test_nearby_mos2_to_m_maxima_become_one_traceable_fit_seed():
    labelled = processing.assign_peak_labels(
        [
            {
                "position_cm-1": 347.2,
                "intensity": 18.0,
                "prominence": 4.0,
                "detection_source": "generic",
            },
            {
                "position_cm-1": 351.5,
                "intensity": 31.0,
                "prominence": 12.0,
                "detection_source": "reference_guided",
            },
            {
                "position_cm-1": 385.0,
                "intensity": 42.0,
                "prominence": 20.0,
                "detection_source": "generic",
            },
        ],
        online_references.curated_references(),
        "MoS2 / Cs2ZnBr4",
        {"status": "declared", "material_system": "MoS2 / Cs2ZnBr4"},
        processing.MODEL_PARAMETERS,
    )

    groups, consolidated = processing.consolidate_mos2_to_m_modes(
        labelled,
        "MoS2 / Cs2ZnBr4",
    )

    assert len(groups) == 1
    assert groups[0]["member_peak_count"] == 2
    assert [
        peak["position_cm-1"] for peak in groups[0]["member_peaks"]
    ] == [347.2, 351.5]
    assert groups[0]["representative_position_cm-1"] == 351.5
    assert groups[0]["conditional_alternative"]["status"] == "not_selected"
    assert [peak["position_cm-1"] for peak in consolidated] == [351.5, 385.0]
    assert consolidated[0]["mode_group"] == groups[0]


def test_peak_indistinguishable_from_local_noise_is_excluded_from_analysis():
    x_values = [float(value) for value in range(31)]
    intensities = [2.0 if value % 2 else 0.0 for value in range(31)]
    peaks = [
        {
            "position_cm-1": 10.0,
            "intensity": 4.0,
            "prominence": 4.0,
            "assignments": [{"material_system": "Test", "label": "weak"}],
        },
        {
            "position_cm-1": 20.0,
            "intensity": 10.0,
            "prominence": 10.0,
            "assignments": [{"material_system": "Test", "label": "strong"}],
        },
    ]

    retained, excluded = processing.exclude_noise_indistinguishable_peaks(
        peaks,
        x_values,
        intensities,
        processing.MODEL_PARAMETERS,
    )

    assert [peak["position_cm-1"] for peak in retained] == [20.0]
    assert [peak["position_cm-1"] for peak in excluded] == [10.0]
    assert excluded[0]["classification"] == "noise"
    assert excluded[0]["signal_to_noise"] == pytest.approx(2.0)
    assert excluded[0]["minimum_signal_to_noise"] == 3.0
    assert excluded[0]["assignments"][0]["label"] == "weak"
    assert "spectral series preserved" in excluded[0]["treatment"]


def test_cs2znbr4_overtone_is_provisional_only_without_mos2_context():
    references = online_references.curated_references()
    peak = [{"position_cm-1": 351.0, "intensity": 20.0, "prominence": 8.0}]

    cs_only = processing.assign_peak_labels(
        peak,
        references,
        "Cs2ZnBr4",
        {"status": "declared", "material_system": "Cs2ZnBr4"},
        processing.MODEL_PARAMETERS,
    )
    heterostructure = processing.assign_peak_labels(
        peak,
        references,
        "MoS2 / Cs2ZnBr4",
        {"status": "declared", "material_system": "MoS2 / Cs2ZnBr4"},
        processing.MODEL_PARAMETERS,
    )

    assert cs_only[0]["assignments"][0]["label"] == (
        "Possible 2ν1 Zn-Br overtone"
    )
    assert cs_only[0]["assignments"][0]["provisional"] is True
    assert heterostructure[0]["assignments"][0]["label"] == (
        "TO(M) defect-activated phonon"
    )
    assert all(
        assignment["label"] != "Possible 2ν1 Zn-Br overtone"
        for assignment in heterostructure[0]["assignments"]
    )


def test_cosmic_filter_preserves_a_sharp_raman_band_with_shoulders():
    x_values = [float(value) for value in range(13)]
    intensities = [
        1000.0,
        800.0,
        1000.0,
        1200.0,
        2000.0,
        4000.0,
        10_000.0,
        3800.0,
        1900.0,
        800.0,
        1000.0,
        1200.0,
        1000.0,
    ]

    corrected, artifacts = processing.detect_and_correct_cosmic_spikes(
        x_values,
        intensities,
        processing.MODEL_PARAMETERS,
    )

    assert artifacts == []
    assert corrected == intensities


def test_confirmed_graphene_on_sio2_name_is_used_for_peak_labels():
    peaks = [
        {"position_cm-1": value, "intensity": 100.0, "prominence": 50.0}
        for value in (522.0, 1600.0, 2688.0)
    ]
    identification = {
        "status": "confirmed",
        "material_system": "Graphene on SiO2",
        "confirmed_material_system": "Graphene on SiO2",
    }

    labelled = processing.assign_peak_labels(
        peaks,
        online_references.curated_references(),
        "Graphene on SiO2",
        identification,
        processing.MODEL_PARAMETERS,
    )

    assert labelled[0]["assignments"][0]["material_system"] == (
        "Crystalline silicon"
    )
    assert labelled[0]["assignments"][0]["role"] == "substrate"
    assert labelled[1]["assignments"][0]["material_system"] == (
        "Graphene on SiO2"
    )
    assert labelled[1]["assignments"][0]["label"] == "G"
    assert labelled[2]["assignments"][0]["material_system"] == (
        "Graphene on SiO2"
    )
    assert labelled[2]["assignments"][0]["label"] == "2D"


def test_sync_rod_catalog_preserves_download_and_loads_verified_cache(
    tmp_path,
    monkeypatch,
):
    raw_jcamp = jcamp_bytes()
    entry = {
        "rod_id": "1000999",
        "formula": "Si",
        "authors": "A. Author",
        "title": "Silicon Raman spectrum",
        "citation": "A. Author. Silicon Raman spectrum.",
    }
    monkeypatch.setattr(
        online_references,
        "rod_search_catalog",
        lambda _limit, _page=0: [entry],
    )
    monkeypatch.setattr(
        online_references,
        "fetch_rod_spectrum",
        lambda requested_entry: (requested_entry, raw_jcamp),
    )

    status = online_references.sync_rod_catalog(tmp_path, limit=1)
    references = online_references.load_synced_rod_references(tmp_path)

    assert status["reference_count"] == 1
    assert len(references) == 1
    reference = references[0]
    stored_path = Path(reference["storage_path"])
    assert stored_path.read_bytes() == raw_jcamp
    assert reference["sha256"] == hashlib.sha256(raw_jcamp).hexdigest()
    evidence = json.loads(reference["evidence_json"])
    assert evidence["provider"] == "Raman Open Database"
    assert evidence["license"] == "CC0-1.0"
    assert evidence["peaks"]
