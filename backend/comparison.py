import math
import re
from pathlib import Path

import database
import preview
import processing
from storage import RAW_DATA_DIR

SUPPORTED_NORMALIZATIONS = {"vector", "max", "area", "none"}
SUPPORTED_SERIES = {
    "raw",
    "artifact_corrected",
    "baseline",
    "corrected",
    "substrate_corrected",
    "substrate_fit",
    "deconvolved_fit",
    "deconvolution_residual",
    "selected_deconvolution_peaks",
}
MAX_OVERLAY_SPECTRA = 50
DEFAULT_CORRELATION_THRESHOLD = 0.9

MOS2_ANALYSIS_REFERENCES = [
    {
        "title": "Anomalous Lattice Vibrations of Single- and Few-Layer MoS2",
        "citation": "Lee et al., ACS Nano 4, 2695–2700 (2010)",
        "doi": "10.1021/nn1003937",
        "source_url": "https://doi.org/10.1021/nn1003937",
    }
]
GRAPHITIC_CARBON_ANALYSIS_REFERENCES = [
    {
        "title": (
            "Quantifying Defects in Graphene via Raman Spectroscopy at "
            "Different Excitation Energies"
        ),
        "citation": "Cançado et al., Nano Letters 11, 3190–3196 (2011)",
        "doi": "10.1021/nl201432g",
        "source_url": "https://doi.org/10.1021/nl201432g",
    },
    {
        "title": "Raman Spectrum of Graphene and Graphene Layers",
        "citation": (
            "Ferrari et al., Physical Review Letters 97, 187401 (2006)"
        ),
        "doi": "10.1103/PhysRevLett.97.187401",
        "source_url": "https://doi.org/10.1103/PhysRevLett.97.187401",
    },
]


class ComparisonError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def get_spectrum_data(file_id: str) -> tuple[dict, list[tuple[float, float]]]:
    """Retrieve metadata and parsed XY data points for an imported file."""
    with database.connect_database() as connection:
        cursor = connection.execute(
            f"SELECT {database.IMPORTED_FILE_COLUMNS} FROM imported_files WHERE file_id = ?",
            (file_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise ComparisonError(
                404,
                "file_not_found",
                f"Imported file with file_id '{file_id}' was not found.",
            )
        metadata = dict(row)

    try:
        preview_data = preview.build_file_preview(metadata, RAW_DATA_DIR)
    except preview.PreviewError as error:
        raise ComparisonError(
            error.status_code,
            error.code,
            error.message,
        ) from error

    points = [
        (float(pt[0]), float(pt[1])) if isinstance(pt, (tuple, list)) else (float(pt["x"]), float(pt["y"]))
        for pt in preview_data["points"]
    ]
    points.sort(key=lambda point: point[0])
    if len(points) < 2:
        raise ComparisonError(
            422,
            "insufficient_data",
            f"File '{file_id}' contains fewer than 2 data points.",
        )
    return metadata, points


def sample_family(metadata: dict) -> str | None:
    """Return a stable family key such as ``t-12`` from user metadata."""
    for value in (metadata.get("sample_id"), metadata.get("original_filename")):
        if not value:
            continue
        match = re.search(r"(?i)(?<![a-z0-9])([a-z]+)[\s_-]*(\d+)(?!\d)", value)
        if match:
            return f"{match.group(1).casefold()}-{int(match.group(2))}"
    return None


def normalized_material_system(value: str | None) -> str | None:
    if not value or value.strip().casefold() in {"unknown", "unspecified", "n/a"}:
        return None
    return "".join(character for character in value.casefold() if character.isalnum())


def normalized_technique(value: str | None) -> str:
    return "".join(character for character in (value or "").casefold() if character.isalnum())


def normalized_peak_label(value: str | None) -> str | None:
    if not value:
        return None
    normalized = "".join(character for character in value.casefold() if character.isalnum())
    return normalized or None


def deconvolution_fit_is_accepted(result: dict) -> bool:
    return (
        result.get("deconvolution", {})
        .get("quality", {})
        .get("accepted", True)
        is not False
    )


def deconvolution_peak_catalog(
    file_ids: list[str],
    processed_directory: Path,
    tolerance_cm_1: float = 12.0,
) -> dict:
    unique_file_ids = list(dict.fromkeys(file_id.strip() for file_id in file_ids))
    if not unique_file_ids:
        raise ComparisonError(422, "empty_peak_selection_scope", "Select spectra before choosing deconvoluted peaks.")
    groups = []
    unavailable_files = []
    for file_id in unique_file_ids:
        metadata = database.get_imported_file(file_id)
        if metadata is None:
            raise ComparisonError(404, "file_not_found", f"Imported file with file_id '{file_id}' was not found.")
        try:
            result = processing.latest_processing_result(file_id, processed_directory)
        except processing.ProcessingError:
            unavailable_files.append(
                {
                    "file_id": file_id,
                    "original_filename": metadata["original_filename"],
                    "reason": "not_processed",
                    "required_model_version": processing.MODEL_VERSION,
                }
            )
            continue
        result_model_version = result.get("model", {}).get("version")
        if (
            result_model_version
            and result_model_version != processing.MODEL_VERSION
        ):
            unavailable_files.append(
                {
                    "file_id": file_id,
                    "original_filename": metadata["original_filename"],
                    "reason": "outdated_processing_model",
                    "model_version": result_model_version,
                    "required_model_version": processing.MODEL_VERSION,
                }
            )
            continue
        components = result.get("deconvolution", {}).get("components") or []
        if components and not deconvolution_fit_is_accepted(result):
            quality = result.get("deconvolution", {}).get("quality", {})
            observed_ratios = [
                quality.get("global_residual_to_fit_ratio"),
                quality.get("maximum_component_residual_to_fit_ratio"),
            ]
            unavailable_files.append(
                {
                    "file_id": file_id,
                    "original_filename": metadata["original_filename"],
                    "reason": "deconvolution_fit_review_required",
                    "model_version": result_model_version,
                    "required_model_version": processing.MODEL_VERSION,
                    "maximum_residual_to_fit_ratio": max(
                        (
                            float(value)
                            for value in observed_ratios
                            if value is not None
                        ),
                        default=None,
                    ),
                    "acceptance_limit": quality.get(
                        "maximum_allowed_residual_to_fit_ratio"
                    ),
                }
            )
            continue
        if not components:
            unavailable_files.append(
                {
                    "file_id": file_id,
                    "original_filename": metadata["original_filename"],
                    "reason": "deconvolution_not_fitted",
                    "model_version": result_model_version,
                    "required_model_version": processing.MODEL_VERSION,
                }
            )
            continue
        for component in components:
            center = float(component["center_cm-1"])
            assignment = component.get("assignment") or {}
            label = assignment.get("label")
            label_key = normalized_peak_label(label)
            group = next(
                (
                    candidate
                    for candidate in groups
                    if abs(candidate["mean_center_cm_1"] - center) <= tolerance_cm_1
                    and (
                        candidate["label_key"] == label_key
                        or candidate["label_key"] is None
                        or label_key is None
                    )
                ),
                None,
            )
            if group is None:
                group = {
                    "centers": [],
                    "file_ids": [],
                    "labels": set(),
                    "materials": set(),
                    "label_key": label_key,
                    "mean_center_cm_1": center,
                }
                groups.append(group)
            if file_id in group["file_ids"]:
                continue
            group["centers"].append(center)
            group["file_ids"].append(file_id)
            group["mean_center_cm_1"] = sum(group["centers"]) / len(group["centers"])
            if label:
                group["labels"].add(label)
            if assignment.get("material_system"):
                group["materials"].add(assignment["material_system"])
    groups.sort(key=lambda item: item["mean_center_cm_1"])
    peaks = []
    for index, group in enumerate(groups):
        preferred_label = sorted(group["labels"], key=lambda value: (len(value), value))[0] if group["labels"] else None
        peaks.append(
            {
                "peak_id": f"peak-{index + 1}",
                "center_cm_1": round(group["mean_center_cm_1"], 3),
                "range_cm_1": [round(min(group["centers"]), 3), round(max(group["centers"]), 3)],
                "label": preferred_label,
                "labels": sorted(group["labels"]),
                "material_systems": sorted(group["materials"]),
                "support_count": len(group["file_ids"]),
                "supporting_file_ids": group["file_ids"],
                "tolerance_cm_1": tolerance_cm_1,
            }
        )
    return {
        "file_ids": unique_file_ids,
        "tolerance_cm_1": tolerance_cm_1,
        "peaks": peaks,
        "unavailable_files": unavailable_files,
    }


def component_matches_selection(component: dict, selection: dict) -> bool:
    center = float(component["center_cm-1"])
    target_center = float(selection["center_cm_1"])
    tolerance = float(selection.get("tolerance_cm_1", 12.0))
    if abs(center - target_center) > tolerance:
        return False
    target_label = normalized_peak_label(selection.get("label"))
    component_label = normalized_peak_label((component.get("assignment") or {}).get("label"))
    return target_label is None or component_label is None or target_label == component_label


def group_analysis_catalog(
    file_ids: list[str],
    processed_directory: Path,
) -> dict:
    """Build literature-backed and user-configurable group analysis inputs."""
    unique_file_ids = list(dict.fromkeys(file_id.strip() for file_id in file_ids))
    if len(unique_file_ids) < 2:
        raise ComparisonError(
            422,
            "insufficient_comparison_files",
            "Select at least two spectra for group analysis.",
        )
    if len(unique_file_ids) > MAX_OVERLAY_SPECTRA:
        raise ComparisonError(
            422,
            "too_many_comparison_files",
            f"Group analysis supports at most {MAX_OVERLAY_SPECTRA} spectra.",
        )

    spectra = []
    results = {}
    unavailable_files = []
    for file_id in unique_file_ids:
        metadata = database.get_imported_file(file_id)
        if metadata is None:
            raise ComparisonError(
                404,
                "file_not_found",
                f"Imported file with file_id '{file_id}' was not found.",
            )
        spectrum = {
            "file_id": file_id,
            "sample_id": metadata.get("sample_id"),
            "original_filename": metadata["original_filename"],
            "material_system": metadata.get("material_system"),
            "processing_status": "available",
        }
        try:
            result = processing.latest_processing_result(
                file_id,
                processed_directory,
            )
        except processing.ProcessingError:
            result = None
            spectrum["processing_status"] = "not_processed"
            unavailable_files.append(
                {
                    "file_id": file_id,
                    "original_filename": metadata["original_filename"],
                    "reason": "not_processed",
                }
            )
        if result is not None:
            results[file_id] = result
            spectrum["processing_id"] = result.get("processing_id")
        spectra.append(spectrum)

    spectra_by_file_id = {
        spectrum["file_id"]: spectrum for spectrum in spectra
    }

    def effective_material_system(file_id: str) -> str | None:
        result = results.get(file_id, {})
        summary = result.get("summary", {})
        identification = summary.get("identification", {})
        if isinstance(identification, dict):
            for key in (
                "confirmed_material_system",
                "material_system",
                "declared_material_system",
            ):
                material_system = identification.get(key)
                if material_system:
                    return str(material_system)
        return spectra_by_file_id[file_id].get("material_system")

    material_systems = {
        file_id: effective_material_system(file_id)
        for file_id in unique_file_ids
    }

    def analyses_for(file_id: str) -> list[dict]:
        result = results.get(file_id)
        if result is None:
            return []
        summary = result.get("summary", {})
        analyses = summary.get("material_analyses")
        if analyses is not None:
            return analyses
        legacy = summary.get("material_analysis")
        if legacy:
            return [legacy]

        corrected_series = result.get("series", {}).get("corrected", [])
        x_values = []
        intensities = []
        for point in corrected_series:
            if isinstance(point, (list, tuple)) and len(point) >= 2:
                x_values.append(float(point[0]))
                intensities.append(float(point[1]))
        parameters = {
            **processing.MODEL_PARAMETERS,
            **result.get("model", {}).get("parameters", {}),
        }
        return processing.material_system_analyses(
            material_systems[file_id],
            result.get("peaks", []),
            result.get("deconvolution", {}),
            x_values,
            intensities,
            parameters,
        )

    def observation(spectrum: dict, value) -> dict:
        numeric_value = None
        try:
            if value is not None and math.isfinite(float(value)):
                numeric_value = float(value)
        except (TypeError, ValueError):
            pass
        return {
            "file_id": spectrum["file_id"],
            "sample_id": spectrum.get("sample_id"),
            "original_filename": spectrum["original_filename"],
            "value": numeric_value,
        }

    def analysis_for(file_id: str, analysis_name: str) -> dict | None:
        compatible_names = {analysis_name}
        if analysis_name == "graphitic_carbon_raman_quality_ratios":
            compatible_names.add("carbon_d_g_peak_height_ratio")
        return next(
            (
                item
                for item in analyses_for(file_id)
                if item.get("analysis") in compatible_names
            ),
            None,
        )

    suggestions = []
    mos2_analyses = {
        spectrum["file_id"]: analysis_for(
            spectrum["file_id"],
            "mos2_layer_screening_from_mode_separation",
        )
        for spectrum in spectra
    }
    group_has_mos2 = any(
        processing.is_mos2_material(material_system)
        for material_system in material_systems.values()
    )
    if group_has_mos2 or any(mos2_analyses.values()):
        references = next(
            (
                item.get("references", [])
                for item in mos2_analyses.values()
                if item
            ),
            MOS2_ANALYSIS_REFERENCES,
        )
        suggestions.extend(
            [
                {
                    "analysis_id": "literature-mos2-mode-separation",
                    "title": "MoS2 A1g−E2g1 mode separation",
                    "description": (
                        "Compare the thickness-sensitive Raman-mode separation "
                        "across the selected spectra."
                    ),
                    "recommendation_reason": (
                        "Suggested because MoS2 is declared, confirmed, or "
                        "identified in the selected group."
                    ),
                    "y_axis_label": "Mode separation (cm⁻¹)",
                    "traces": [
                        {
                            "trace_id": "mode-separation",
                            "label": "A1g−E2g1",
                            "unit": "cm⁻¹",
                            "observations": [
                                observation(
                                    spectrum,
                                    (mos2_analyses[spectrum["file_id"]] or {}).get(
                                        "peak_separation_cm-1"
                                    ),
                                )
                                for spectrum in spectra
                            ],
                        }
                    ],
                    "references": references,
                },
                {
                    "analysis_id": "literature-mos2-mode-positions",
                    "title": "MoS2 E2g1 and A1g positions",
                    "description": (
                        "Compare both fitted mode positions to reveal shifts that "
                        "may accompany thickness, strain, or doping changes."
                    ),
                    "recommendation_reason": (
                        "Suggested from the MoS2 E2g1/A1g literature rule."
                    ),
                    "y_axis_label": "Raman shift (cm⁻¹)",
                    "traces": [
                        {
                            "trace_id": trace_id,
                            "label": label,
                            "unit": "cm⁻¹",
                            "observations": [
                                observation(
                                    spectrum,
                                    ((mos2_analyses[spectrum["file_id"]] or {}).get(
                                        band_key
                                    ) or {}).get("position_cm-1"),
                                )
                                for spectrum in spectra
                            ],
                        }
                        for trace_id, label, band_key in (
                            ("e2g1-position", "E2g1", "e2g1_band"),
                            ("a1g-position", "A1g", "a1g_band"),
                        )
                    ],
                    "references": references,
                },
            ]
        )

    carbon_analyses = {
        spectrum["file_id"]: analysis_for(
            spectrum["file_id"],
            "graphitic_carbon_raman_quality_ratios",
        )
        for spectrum in spectra
    }
    group_has_graphitic_carbon = any(
        processing.is_carbon_material(material_system)
        for material_system in material_systems.values()
    )
    if group_has_graphitic_carbon or any(carbon_analyses.values()):
        references = next(
            (
                item.get("references", [])
                for item in carbon_analyses.values()
                if item
            ),
            GRAPHITIC_CARBON_ANALYSIS_REFERENCES,
        )
        suggestions.extend(
            [
                {
                    "analysis_id": "literature-carbon-band-ratios",
                    "title": "Graphitic carbon D/G and 2D/G ratios",
                    "description": (
                        "Compare defect-activated D/G and relative 2D/G Raman "
                        "responses across the selected spectra."
                    ),
                    "recommendation_reason": (
                        "Suggested because graphitic carbon is declared, "
                        "confirmed, or identified in the selected group."
                    ),
                    "y_axis_label": "Peak-height ratio",
                    "traces": [
                        {
                            "trace_id": trace_id,
                            "label": label,
                            "unit": "ratio",
                            "observations": [
                                observation(
                                    spectrum,
                                    (carbon_analyses[spectrum["file_id"]] or {}).get(
                                        value_key
                                    ),
                                )
                                for spectrum in spectra
                            ],
                        }
                        for trace_id, label, value_key in (
                            ("d-g-ratio", "I(D)/I(G)", "id_ig_ratio"),
                            ("2d-g-ratio", "I(2D)/I(G)", "i2d_ig_ratio"),
                        )
                    ],
                    "references": references,
                },
                {
                    "analysis_id": "literature-carbon-band-positions",
                    "title": "Graphitic carbon D, G, and 2D positions",
                    "description": (
                        "Compare the fitted or detected Raman-band positions "
                        "across the selected spectra."
                    ),
                    "recommendation_reason": (
                        "Suggested from the graphitic-carbon Raman literature rule."
                    ),
                    "y_axis_label": "Raman shift (cm⁻¹)",
                    "traces": [
                        {
                            "trace_id": trace_id,
                            "label": label,
                            "unit": "cm⁻¹",
                            "observations": [
                                observation(
                                    spectrum,
                                    ((carbon_analyses[spectrum["file_id"]] or {}).get(
                                        band_key
                                    ) or {}).get("position_cm-1"),
                                )
                                for spectrum in spectra
                            ],
                        }
                        for trace_id, label, band_key in (
                            ("d-position", "D", "d_band"),
                            ("g-position", "G", "g_band"),
                            ("2d-position", "2D", "two_d_band"),
                        )
                    ],
                    "references": references,
                },
            ]
        )

    peak_catalog = deconvolution_peak_catalog(
        unique_file_ids,
        processed_directory,
    )
    custom_peak_options = []
    for peak in peak_catalog["peaks"]:
        selection = {
            "center_cm_1": peak["center_cm_1"],
            "label": peak.get("label"),
            "tolerance_cm_1": peak["tolerance_cm_1"],
        }
        peak_observations = []
        for spectrum in spectra:
            result = results.get(spectrum["file_id"])
            components = (
                result.get("deconvolution", {}).get("components", [])
                if result and deconvolution_fit_is_accepted(result)
                else []
            )
            component = next(
                (
                    item
                    for item in components
                    if component_matches_selection(item, selection)
                ),
                None,
            )
            peak_observations.append(
                {
                    "file_id": spectrum["file_id"],
                    "sample_id": spectrum.get("sample_id"),
                    "original_filename": spectrum["original_filename"],
                    "status": "fitted" if component else "not_fitted",
                    "center_cm_1": (
                        float(component["center_cm-1"]) if component else None
                    ),
                    "amplitude": (
                        float(component["amplitude"])
                        if component and component.get("amplitude") is not None
                        else None
                    ),
                    "fwhm_cm_1": (
                        float(component["fwhm_cm-1"])
                        if component and component.get("fwhm_cm-1") is not None
                        else None
                    ),
                    "area": (
                        float(component["area"])
                        if component and component.get("area") is not None
                        else None
                    ),
                }
            )
        custom_peak_options.append({**peak, "observations": peak_observations})

    for suggestion in suggestions:
        available_file_ids = {
            item["file_id"]
            for trace in suggestion["traces"]
            for item in trace["observations"]
            if item["value"] is not None
        }
        suggestion["support_count"] = len(available_file_ids)

    return {
        "file_ids": unique_file_ids,
        "spectra": spectra,
        "suggestions": suggestions,
        "custom_peak_options": custom_peak_options,
        "unavailable_files": unavailable_files,
    }


def _common_grid(
    point_sets: list[list[tuple[float, float]]],
    step_count: int = 500,
) -> tuple[list[float], float, float]:
    min_x = max(points[0][0] for points in point_sets)
    max_x = min(points[-1][0] for points in point_sets)
    if min_x >= max_x:
        raise ComparisonError(
            422,
            "no_wavenumber_overlap",
            "The selected spectra do not share an overlapping wavenumber region.",
        )
    step = (max_x - min_x) / (step_count - 1)
    return [min_x + index * step for index in range(step_count)], min_x, max_x


def _pair_correlation(
    first_points: list[tuple[float, float]],
    second_points: list[tuple[float, float]],
) -> float:
    common_x, _min_x, _max_x = _common_grid([first_points, second_points])
    first_y = interpolate_series(first_points, common_x)
    second_y = interpolate_series(second_points, common_x)
    return compute_pearson_correlation(first_y, second_y)


def find_correlated_spectra(
    anchor_file_id: str,
    correlation_threshold: float = DEFAULT_CORRELATION_THRESHOLD,
) -> dict:
    """Find compatible spectra and recommend likely members of an overlay group."""
    if not -1.0 <= correlation_threshold <= 1.0:
        raise ComparisonError(
            422,
            "invalid_correlation_threshold",
            "Correlation threshold must be between -1 and 1.",
        )

    anchor_meta, anchor_points = get_spectrum_data(anchor_file_id)
    anchor_family = sample_family(anchor_meta)
    anchor_material = normalized_material_system(anchor_meta.get("material_system"))
    anchor_technique = normalized_technique(anchor_meta.get("technique"))
    candidates = []

    for metadata in database.list_imported_files():
        if normalized_technique(metadata.get("technique")) != anchor_technique:
            continue
        is_anchor = metadata["file_id"] == anchor_file_id
        family_match = bool(
            anchor_family and sample_family(metadata) == anchor_family
        )
        material_match = bool(
            anchor_material
            and normalized_material_system(metadata.get("material_system"))
            == anchor_material
        )
        reasons = []
        if is_anchor:
            correlation = 1.0
            reasons.append("anchor_spectrum")
        else:
            try:
                _candidate_meta, candidate_points = get_spectrum_data(
                    metadata["file_id"]
                )
                correlation = _pair_correlation(anchor_points, candidate_points)
            except ComparisonError as error:
                if error.code == "no_wavenumber_overlap":
                    continue
                correlation = None
        if family_match:
            reasons.append("same_sample_family")
        if material_match:
            reasons.append("same_material_system")
        if correlation is not None and correlation >= correlation_threshold:
            reasons.append("spectral_correlation")

        automatically_selected = bool(
            is_anchor
            or family_match
            or (material_match and correlation is not None and correlation >= correlation_threshold)
            or (
                not anchor_material
                and correlation is not None
                and correlation >= correlation_threshold
            )
        )
        candidates.append(
            {
                "file_id": metadata["file_id"],
                "original_filename": metadata["original_filename"],
                "sample_id": metadata.get("sample_id"),
                "sample_family": sample_family(metadata),
                "material_system": metadata.get("material_system"),
                "technique": metadata.get("technique"),
                "pearson_correlation_r": (
                    round(correlation, 5) if correlation is not None else None
                ),
                "match_reasons": reasons,
                "automatically_selected": automatically_selected,
            }
        )

    candidates.sort(
        key=lambda item: (
            not item["automatically_selected"],
            -(item["pearson_correlation_r"] or -1.0),
            (item.get("sample_id") or "").casefold(),
        )
    )
    return {
        "anchor_file_id": anchor_file_id,
        "sample_family": anchor_family,
        "material_system": anchor_meta.get("material_system"),
        "technique": anchor_meta.get("technique"),
        "correlation_threshold": correlation_threshold,
        "method": (
            "Same sample family is selected automatically. Same-material spectra "
            "are selected when their Pearson correlation meets the threshold; "
            "correlation alone is used when material metadata is unknown."
        ),
        "candidates": candidates,
        "automatically_selected_file_ids": [
            item["file_id"] for item in candidates if item["automatically_selected"]
        ][:MAX_OVERLAY_SPECTRA],
    }


def build_spectra_overlay(
    file_ids: list[str],
    normalization: str = "max",
    series_name: str = "raw",
    offset: float = 0.0,
    processed_directory: Path | None = None,
    peak_selections: list[dict] | None = None,
) -> dict:
    """Interpolate selected spectra onto one grid for a comparison graph."""
    if normalization not in SUPPORTED_NORMALIZATIONS:
        raise ComparisonError(
            422,
            "invalid_normalization",
            f"Unsupported normalization '{normalization}'. Supported: {sorted(SUPPORTED_NORMALIZATIONS)}",
        )
    if series_name not in SUPPORTED_SERIES:
        raise ComparisonError(
            422,
            "invalid_series",
            f"Unsupported displayed series '{series_name}'. Supported: {sorted(SUPPORTED_SERIES)}",
        )
    if not math.isfinite(offset) or abs(offset) > 1e12:
        raise ComparisonError(
            422,
            "invalid_offset",
            "Spectrum offset must be a finite number between -1e12 and 1e12.",
        )
    if series_name == "selected_deconvolution_peaks" and not peak_selections:
        raise ComparisonError(
            422,
            "deconvolution_peak_selection_required",
            "Select at least one fitted deconvolution peak to plot.",
        )
    for selection in peak_selections or []:
        center = float(selection.get("center_cm_1", math.nan))
        tolerance = float(selection.get("tolerance_cm_1", 12.0))
        if not math.isfinite(center) or not math.isfinite(tolerance) or tolerance <= 0 or tolerance > 1000:
            raise ComparisonError(
                422,
                "invalid_deconvolution_peak_selection",
                "Every selected peak must have a finite center and a tolerance between 0 and 1000 cm-1.",
            )
    unique_file_ids = list(dict.fromkeys(file_id.strip() for file_id in file_ids))
    if len(unique_file_ids) < 2:
        raise ComparisonError(
            422,
            "insufficient_overlay_selection",
            "Select at least two spectra for comparison.",
        )
    if len(unique_file_ids) > MAX_OVERLAY_SPECTRA:
        raise ComparisonError(
            413,
            "overlay_too_large",
            f"A comparison graph may contain no more than {MAX_OVERLAY_SPECTRA} spectra.",
        )

    loaded = []
    for file_id in unique_file_ids:
        metadata, raw_points = get_spectrum_data(file_id)
        matched_components = []
        if series_name == "raw":
            points = raw_points
        else:
            if processed_directory is None:
                raise ComparisonError(
                    500,
                    "processed_directory_unavailable",
                    "Processed spectra storage is not configured.",
                )
            try:
                result = processing.latest_processing_result(
                    file_id,
                    processed_directory,
                )
            except processing.ProcessingError as error:
                raise ComparisonError(
                    422 if error.status_code == 404 else error.status_code,
                    "processing_required_for_series",
                    (
                        f"{metadata['original_filename']} must be processed before "
                        f"the '{series_name}' series can be compared."
                    ),
                ) from error
            if (
                series_name
                in {
                    "deconvolved_fit",
                    "deconvolution_residual",
                    "selected_deconvolution_peaks",
                }
                and (
                    result.get("deconvolution", {}).get("status") == "fitted"
                    or result.get("deconvolution", {}).get("components")
                )
                and not deconvolution_fit_is_accepted(result)
            ):
                raise ComparisonError(
                    422,
                    "deconvolution_fit_review_required",
                    (
                        f"{metadata['original_filename']} has a deconvolution "
                        "fit whose residual exceeds the acceptance limit. Its "
                        "fit and residual plots are withheld until the spectrum "
                        "is reprocessed."
                    ),
                )
            if series_name == "selected_deconvolution_peaks":
                components = result.get("deconvolution", {}).get("components") or []
                if not components:
                    raise ComparisonError(
                        422,
                        "series_unavailable",
                        f"{metadata['original_filename']} has no fitted deconvolution components. Reprocess it with a fitting recipe.",
                    )
                matched_components = [
                    component
                    for component in components
                    if any(component_matches_selection(component, selection) for selection in peak_selections or [])
                ]
                base_points = result.get("series", {}).get("corrected") or raw_points
                summed = {float(point[0]): 0.0 for point in base_points}
                for component in matched_components:
                    for x_value, y_value in component.get("series", []):
                        x_value = float(x_value)
                        if x_value in summed:
                            summed[x_value] += float(y_value)
                derived_points = [[x_value, summed[x_value]] for x_value in sorted(summed)]
            else:
                derived_points = result.get("series", {}).get(series_name)
            if not derived_points:
                raise ComparisonError(
                    422,
                    "series_unavailable",
                    (
                        f"{metadata['original_filename']} does not have the "
                        f"'{series_name}' series in its latest result. Reprocess it "
                        "with the required fitting or substrate-correction recipe."
                    ),
                )
            points = sorted(
                [(float(point[0]), float(point[1])) for point in derived_points],
                key=lambda point: point[0],
            )
        loaded.append((metadata, points, matched_components))
    common_x, min_x, max_x = _common_grid([points for _meta, points, _components in loaded])
    series = []
    for series_index, (metadata, points, matched_components) in enumerate(loaded):
        raw_y = interpolate_series(points, common_x)
        normalized_y = normalize_vector(raw_y, normalization)
        applied_offset = series_index * offset
        series.append(
            {
                "file_id": metadata["file_id"],
                "original_filename": metadata["original_filename"],
                "sample_id": metadata.get("sample_id"),
                "material_system": metadata.get("material_system"),
                "applied_offset": applied_offset,
                "matched_component_count": len(matched_components),
                "matched_components": [
                    {
                        "seed_center_cm_1": component.get("seed_center_cm-1"),
                        "center_cm_1": component.get("center_cm-1"),
                        "label": (component.get("assignment") or {}).get("label"),
                        "material_system": (component.get("assignment") or {}).get("material_system"),
                        "amplitude": component.get("amplitude"),
                        "fwhm_cm_1": component.get("fwhm_cm-1"),
                        "area": component.get("area"),
                        "mode_group": component.get("mode_group"),
                    }
                    for component in matched_components
                ],
                "points": [
                    [round(x_value, 4), y_value + applied_offset]
                    for x_value, y_value in zip(common_x, normalized_y)
                ],
            }
        )
    return {
        "file_ids": unique_file_ids,
        "normalization": normalization,
        "series_name": series_name,
        "offset": offset,
        "peak_selections": peak_selections or [],
        "wavenumber_range_cm_1": [round(min_x, 2), round(max_x, 2)],
        "points_count": len(common_x),
        "series": series,
    }


def interpolate_series(
    source_points: list[tuple[float, float]],
    target_x: list[float],
) -> list[float]:
    """Linear interpolation of XY points onto target_x grid."""
    if not source_points:
        return [0.0] * len(target_x)

    src_x = [pt[0] for pt in source_points]
    src_y = [pt[1] for pt in source_points]

    result = []
    src_idx = 0
    n_src = len(src_x)

    for x in target_x:
        if x <= src_x[0]:
            result.append(src_y[0])
            continue
        if x >= src_x[-1]:
            result.append(src_y[-1])
            continue

        while src_idx < n_src - 1 and src_x[src_idx + 1] < x:
            src_idx += 1

        x0, y0 = src_x[src_idx], src_y[src_idx]
        x1, y1 = src_x[src_idx + 1], src_y[src_idx + 1]
        if x1 == x0:
            result.append(y0)
        else:
            fraction = (x - x0) / (x1 - x0)
            result.append(y0 + fraction * (y1 - y0))

    return result


def normalize_vector(y_values: list[float], method: str) -> list[float]:
    """Apply vector, max, or area normalization to intensity series."""
    if method == "none":
        return list(y_values)

    if method == "vector":
        norm = math.sqrt(sum(v * v for v in y_values))
        if norm == 0:
            return list(y_values)
        return [v / norm for v in y_values]

    if method == "max":
        max_val = max(abs(v) for v in y_values) if y_values else 0
        if max_val == 0:
            return list(y_values)
        return [v / max_val for v in y_values]

    if method == "area":
        area = sum(abs(v) for v in y_values)
        if area == 0:
            return list(y_values)
        return [v / area for v in y_values]

    raise ComparisonError(
        422,
        "invalid_normalization",
        f"Unsupported normalization method '{method}'. Supported methods: {sorted(SUPPORTED_NORMALIZATIONS)}",
    )


def compute_pearson_correlation(
    vec1: list[float],
    vec2: list[float],
) -> float:
    """Compute Pearson correlation coefficient r between two vectors."""
    n = len(vec1)
    if n == 0:
        return 0.0

    mean1 = sum(vec1) / n
    mean2 = sum(vec2) / n

    cov = sum((v1 - mean1) * (v2 - mean2) for v1, v2 in zip(vec1, vec2))
    var1 = sum((v1 - mean1) ** 2 for v1 in vec1)
    var2 = sum((v2 - mean2) ** 2 for v2 in vec2)

    denom = math.sqrt(var1 * var2)
    if denom == 0:
        return 0.0
    return max(-1.0, min(1.0, cov / denom))


def compute_cosine_similarity(
    vec1: list[float],
    vec2: list[float],
) -> float:
    """Compute Cosine Similarity between two non-zero vectors."""
    dot = sum(v1 * v2 for v1, v2 in zip(vec1, vec2))
    norm1 = math.sqrt(sum(v1 * v1 for v1 in vec1))
    norm2 = math.sqrt(sum(v2 * v2 for v2 in vec2))

    if norm1 == 0 or norm2 == 0:
        return 0.0
    val = dot / (norm1 * norm2)
    return max(-1.0, min(1.0, val))


def compare_spectra(
    sample_file_id: str,
    reference_file_id: str,
    normalization: str = "vector",
) -> dict:
    """Perform comprehensive comparative and differential analysis between two spectra."""
    if normalization not in SUPPORTED_NORMALIZATIONS:
        raise ComparisonError(
            422,
            "invalid_normalization",
            f"Unsupported normalization '{normalization}'. Supported: {sorted(SUPPORTED_NORMALIZATIONS)}",
        )

    sample_meta, sample_points = get_spectrum_data(sample_file_id)
    ref_meta, ref_points = get_spectrum_data(reference_file_id)

    # Determine overlapping common wavenumber grid
    sample_x = [pt[0] for pt in sample_points]
    ref_x = [pt[0] for pt in ref_points]

    min_x = max(min(sample_x), min(ref_x))
    max_x = min(max(sample_x), max(ref_x))

    if min_x >= max_x:
        raise ComparisonError(
            422,
            "no_wavenumber_overlap",
            f"No overlapping wavenumber region between sample [{min(sample_x):.1f}, {max(sample_x):.1f}] and reference [{min(ref_x):.1f}, {max(ref_x):.1f}].",
        )

    # Build common grid with 500 resolution steps within overlap
    step_count = 500
    step = (max_x - min_x) / (step_count - 1)
    common_x = [min_x + i * step for i in range(step_count)]

    # Interpolate raw/preview series onto common grid
    sample_raw_y = interpolate_series(sample_points, common_x)
    ref_raw_y = interpolate_series(ref_points, common_x)

    # Apply normalization
    sample_y = normalize_vector(sample_raw_y, normalization)
    ref_y = normalize_vector(ref_raw_y, normalization)

    # Compute difference series ΔY = Y_sample - Y_ref
    diff_y = [s - r for s, r in zip(sample_y, ref_y)]

    # Similarity metrics
    pearson_r = compute_pearson_correlation(sample_y, ref_y)
    cosine_sim = compute_cosine_similarity(sample_y, ref_y)
    sam_rad = math.acos(max(-1.0, min(1.0, cosine_sim)))
    sam_deg = math.degrees(sam_rad)
    rmse = math.sqrt(sum(d * d for d in diff_y) / len(diff_y))

    # Peak difference points
    max_pos_idx = max(range(len(diff_y)), key=lambda i: diff_y[i])
    max_neg_idx = min(range(len(diff_y)), key=lambda i: diff_y[i])

    differential_series = [
        {"x": round(common_x[i], 2), "sample_y": sample_y[i], "reference_y": ref_y[i], "difference_y": diff_y[i]}
        for i in range(len(common_x))
    ]

    return {
        "sample_file_id": sample_file_id,
        "sample_filename": sample_meta["original_filename"],
        "reference_file_id": reference_file_id,
        "reference_filename": ref_meta["original_filename"],
        "normalization": normalization,
        "wavenumber_range_cm_1": [round(min_x, 2), round(max_x, 2)],
        "metrics": {
            "pearson_correlation_r": round(pearson_r, 5),
            "cosine_similarity": round(cosine_sim, 5),
            "spectral_angle_mapper_deg": round(sam_deg, 3),
            "rmse": round(rmse, 6),
        },
        "extrema": {
            "max_positive_diff": {
                "wavenumber_cm_1": round(common_x[max_pos_idx], 2),
                "difference_intensity": round(diff_y[max_pos_idx], 6),
            },
            "max_negative_diff": {
                "wavenumber_cm_1": round(common_x[max_neg_idx], 2),
                "difference_intensity": round(diff_y[max_neg_idx], 6),
            },
        },
        "points_count": len(differential_series),
        "differential_series": differential_series,
    }
