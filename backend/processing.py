import hashlib
import json
import logging
import math
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from uuid import uuid4

import database
import preview

logger = logging.getLogger(__name__)

MODEL_NAME = "raman_material_identification"
MODEL_VERSION = "2.39.0"
MODEL_PARAMETERS = {
    "artifact_correction_method": "classified_interval_linear_interpolation",
    "baseline_method": "morphological_opening",
    "baseline_window_points": 101,
    "baseline_smoothing_points": 31,
    "cosmic_spike_context_points": 5,
    "cosmic_spike_exclusion_points": 2,
    "cosmic_spike_maximum_width_points": 2,
    "cosmic_spike_minimum_background_ratio": 0.75,
    "cosmic_spike_minimum_robust_sigma": 12.0,
    "cosmic_spike_maximum_neighbor_fraction": 0.25,
    "cosmic_spike_tail_fraction": 0.1,
    "d_band_window_cm-1": [1250.0, 1450.0],
    "two_d_band_window_cm-1": [2550.0, 2850.0],
    "deconvolution_default_fwhm_cm-1": 12.0,
    "deconvolution_maximum_fwhm_cm-1": 150.0,
    "material_deconvolution_maximum_fwhm_cm-1": 40.0,
    "deconvolution_maximum_residual_to_fit_ratio": 0.25,
    "deconvolution_minimum_fwhm_points": 2.0,
    "deconvolution_nnls_iterations": 40,
    "deconvolution_profile": "none",
    "deconvolution_pseudo_voigt_fraction": 0.5,
    "deconvolution_refinement_passes": 2,
    "deconvolution_width_scale_candidates": [0.75, 1.0, 1.3333333333],
    "g_band_window_cm-1": [1500.0, 1650.0],
    "mos2_layer_calibration_cm-1": [
        {"layer_label": "1 layer", "separation_cm-1": 19.0},
        {"layer_label": "2 layers", "separation_cm-1": 21.5},
        {"layer_label": "3 layers", "separation_cm-1": 23.0},
        {"layer_label": "4 layers", "separation_cm-1": 23.7},
        {"layer_label": "5–6 layers", "separation_cm-1": 24.6},
        {"layer_label": "bulk-like (>6 layers)", "separation_cm-1": 25.5},
    ],
    "maximum_reported_peaks": 20,
    "minimum_peak_distance_cm-1": 8.0,
    "minimum_prominence_fraction": 0.02,
    "noise_multiplier": 5.0,
    "noise_indistinguishable_minimum_snr": 3.0,
    "noise_local_radius_points": 12,
    "noise_peak_guard_points": 2,
    "recurring_unassigned_peak_minimum_spectra": 3,
    "recurring_unassigned_peak_tolerance_cm-1": 8.0,
    "singleton_noise_minimum_same_system_spectra": 2,
    "singleton_noise_prominence_threshold_multiplier": 1.5,
    "perovskite_collective_range_cm-1": [0.0, 150.0],
    "rayleigh_center_tolerance_cm-1": 5.0,
    "rayleigh_context_range_cm-1": [15.0, 50.0],
    "rayleigh_minimum_background_ratio": 10.0,
    "rayleigh_minimum_robust_sigma": 20.0,
    "rayleigh_replacement_padding_points": 2,
    "rayleigh_tail_fraction": 0.01,
    "reference_match_tolerance_cm-1": 12.0,
    "reference_guided_noise_multiplier": 3.0,
    "minimum_identification_score": 0.5,
    "minimum_candidate_margin": 0.05,
    "smoothing_method": "none",
    "substrate_auto_minimum_score": 0.6,
    "substrate_auto_minimum_fit_r_squared": 0.75,
    "substrate_ensemble_minimum_coverage_fraction": 0.95,
    "substrate_ensemble_support_fraction_threshold": 0.6,
    "substrate_reference_strategy": "automatic",
    "quality_policy": "balanced",
    "peak_review_policy": "withhold_uncertain",
    "substrate_broad_minimum_fwhm_cm-1": 12.0,
    "substrate_broad_minimum_range_fraction": 0.002,
    "substrate_broad_noise_multiplier": 2.5,
    "substrate_broad_smoothing_points": 9,
    "substrate_residual_band_tolerance_cm-1": 12.0,
    "substrate_profile_cluster_tolerance_cm-1": 12.0,
    "substrate_profile_same_material_minimum_samples": 3,
    "substrate_profile_same_material_minimum_recurrence_fraction": 0.6,
    "substrate_reference_noise_multiplier": 3.0,
    "substrate_reference_position_tolerance_cm-1": 18.0,
    "t12_sio2_substrate_only_ranges_cm-1": [[500.0, 835.0]],
    "substrate_correction_mode": "none",
    "substrate_nonnegative_constraint": (
        "floor_substrate_corrected_and_downstream_residuals_at_zero"
    ),
}
MAX_RESULT_POINTS = 2000
DECONVOLUTION_PROFILES = {"none", "gaussian", "lorentzian", "pseudo_voigt"}
SUBSTRATE_CORRECTION_MODES = {"none", "detect", "auto", "glass", "sio2_si"}
BASELINE_METHODS = {"morphological_opening", "rubber_band"}
SUBSTRATE_REFERENCE_STRATEGIES = {"automatic", "measured_only", "analytical_only"}
QUALITY_POLICIES = {"conservative", "balanced", "exploratory"}
PEAK_REVIEW_POLICIES = {"withhold_uncertain", "flag_only", "require_all_checks"}
QUALITY_POLICY_THRESHOLDS = {
    "conservative": {
        "maximum_axis_spacing_relative_mad": 0.005,
        "minimum_point_count": 10,
        "minimum_signal_to_noise": 5.0,
        "saturation_minimum_run_points": 2,
        "saturation_maximum_fraction": 0.003,
    },
    "balanced": {
        "maximum_axis_spacing_relative_mad": 0.01,
        "minimum_point_count": 5,
        "minimum_signal_to_noise": None,
        "saturation_minimum_run_points": 3,
        "saturation_maximum_fraction": 0.005,
    },
    "exploratory": {
        "maximum_axis_spacing_relative_mad": 0.03,
        "minimum_point_count": 3,
        "minimum_signal_to_noise": None,
        "saturation_minimum_run_points": 4,
        "saturation_maximum_fraction": 0.01,
    },
}
SUBSTRATE_BANDS = {
    "glass": (
        {
            "range_cm-1": [400.0, 475.0],
            "label": "Si-O-Si network bending band",
            "weight": 0.2,
        },
        {
            "range_cm-1": [480.0, 502.0],
            "label": "D1 four-membered siloxane rings",
            "weight": 0.1,
        },
        {
            "range_cm-1": [590.0, 615.0],
            "label": "D2 three-membered siloxane rings",
            "weight": 0.1,
        },
        {
            "range_cm-1": [750.0, 860.0],
            "label": "SiO2 network band near 800 cm-1",
            "weight": 0.25,
        },
        {
            "range_cm-1": [1000.0, 1150.0],
            "label": "SiO2 stretching envelope near 1070 cm-1",
            "weight": 0.35,
        },
    ),
    "sio2_si": (
        {
            "range_cm-1": [280.0, 320.0],
            "label": "crystalline-Si acoustic multiphonon band",
            "weight": 0.25,
        },
        {
            "range_cm-1": [500.0, 540.0],
            "label": "crystalline-Si first-order optical phonon",
            "weight": 0.5,
        },
        {
            "range_cm-1": [400.0, 475.0],
            "label": "SiO2 network bending band",
            "weight": 0.1,
        },
        {
            "range_cm-1": [480.0, 502.0],
            "label": "SiO2 D1 siloxane-ring band",
            "weight": 0.05,
        },
        {
            "range_cm-1": [590.0, 615.0],
            "label": "SiO2 D2 siloxane-ring band",
            "weight": 0.05,
        },
        {
            "range_cm-1": [750.0, 860.0],
            "label": "SiO2 network band near 800 cm-1",
            "weight": 0.1,
        },
        {
            "range_cm-1": [900.0, 1000.0],
            "label": "crystalline-Si second-order 2TO envelope",
            "weight": 0.25,
            "allow_multiple": True,
        },
        {
            "range_cm-1": [1000.0, 1150.0],
            "label": "SiO2 stretching envelope near 1070 cm-1",
            "weight": 0.1,
        },
    ),
}


class ProcessingError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def validate_advanced_processing_options(options: dict | None = None) -> dict:
    configured = options or {}
    normalization_mode = configured.get("normalization_mode", "none")
    if normalization_mode not in {"none", "maximum", "area", "peak"}:
        raise ProcessingError(
            422,
            "invalid_normalization_mode",
            "Normalization mode must be none, maximum, area, or peak.",
        )
    reference_strategy = configured.get(
        "substrate_reference_strategy", "automatic"
    )
    quality_policy = configured.get("quality_policy", "balanced")
    peak_review_policy = configured.get(
        "peak_review_policy", "withhold_uncertain"
    )
    if reference_strategy not in SUBSTRATE_REFERENCE_STRATEGIES:
        raise ProcessingError(
            422,
            "invalid_substrate_reference_strategy",
            "Substrate reference strategy must be automatic, measured_only, or analytical_only.",
        )
    if quality_policy not in QUALITY_POLICIES:
        raise ProcessingError(
            422,
            "invalid_quality_policy",
            "Quality policy must be conservative, balanced, or exploratory.",
        )
    if peak_review_policy not in PEAK_REVIEW_POLICIES:
        raise ProcessingError(
            422,
            "invalid_peak_review_policy",
            "Peak review policy must be withhold_uncertain, flag_only, or require_all_checks.",
        )
    numeric_options = {
        "substrate_ensemble_minimum_coverage_fraction": (
            configured.get("substrate_ensemble_minimum_coverage_fraction", 0.95),
            0.5,
            1.0,
        ),
        "substrate_ensemble_support_fraction_threshold": (
            configured.get("substrate_ensemble_support_fraction_threshold", 0.6),
            0.1,
            1.0,
        ),
        "substrate_auto_minimum_fit_r_squared": (
            configured.get("substrate_auto_minimum_fit_r_squared", 0.75),
            0.0,
            1.0,
        ),
        "minimum_prominence_fraction": (
            configured.get("minimum_prominence_fraction", 0.02),
            0.001,
            0.2,
        ),
    }
    validated = {
        "substrate_reference_strategy": reference_strategy,
        "quality_policy": quality_policy,
        "peak_review_policy": peak_review_policy,
        "normalization_mode": normalization_mode,
    }
    for name, (value, minimum, maximum) in numeric_options.items():
        try:
            numeric_value = float(value)
        except (TypeError, ValueError) as error:
            raise ProcessingError(
                422,
                "invalid_advanced_processing_threshold",
                f"{name} must be a finite number between {minimum} and {maximum}.",
            ) from error
        if not math.isfinite(numeric_value) or not minimum <= numeric_value <= maximum:
            raise ProcessingError(
                422,
                "invalid_advanced_processing_threshold",
                f"{name} must be a finite number between {minimum} and {maximum}.",
            )
        validated[name] = numeric_value
    range_values = {}
    for name in ("analysis_range_min_cm-1", "analysis_range_max_cm-1"):
        value = configured.get(name)
        if value is None:
            range_values[name] = None
            continue
        try:
            numeric_value = float(value)
        except (TypeError, ValueError) as error:
            raise ProcessingError(
                422,
                "invalid_analysis_range",
                "Analysis range bounds must be finite numbers or omitted.",
            ) from error
        if not math.isfinite(numeric_value):
            raise ProcessingError(
                422,
                "invalid_analysis_range",
                "Analysis range bounds must be finite numbers or omitted.",
            )
        range_values[name] = numeric_value
    if (
        range_values["analysis_range_min_cm-1"] is not None
        and range_values["analysis_range_max_cm-1"] is not None
        and range_values["analysis_range_min_cm-1"]
        >= range_values["analysis_range_max_cm-1"]
    ):
        raise ProcessingError(
            422,
            "invalid_analysis_range",
            "Analysis range minimum must be less than its maximum.",
        )
    validated.update(range_values)
    normalization_peak = configured.get("normalization_peak_cm-1")
    if normalization_peak is not None:
        try:
            normalization_peak = float(normalization_peak)
        except (TypeError, ValueError) as error:
            raise ProcessingError(
                422,
                "invalid_normalization_peak",
                "Normalization peak must be a finite Raman shift or omitted.",
            ) from error
        if not math.isfinite(normalization_peak):
            raise ProcessingError(
                422,
                "invalid_normalization_peak",
                "Normalization peak must be a finite Raman shift or omitted.",
            )
    validated["normalization_peak_cm-1"] = normalization_peak
    return validated


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def reflected_index(index: int, length: int) -> int:
    while index < 0 or index >= length:
        if index < 0:
            index = -index
        if index >= length:
            index = 2 * length - index - 2
    return index


def moving_average(values: list[float], window: int) -> list[float]:
    radius = max(0, window // 2)
    smoothed = []
    for index in range(len(values)):
        local_values = [
            values[reflected_index(offset, len(values))]
            for offset in range(index - radius, index + radius + 1)
        ]
        smoothed.append(sum(local_values) / len(local_values))
    return smoothed


def rolling_extreme(
    values: list[float],
    window: int,
    select_extreme,
) -> list[float]:
    radius = max(0, window // 2)
    return [
        select_extreme(
            values[max(0, index - radius) : min(len(values), index + radius + 1)]
        )
        for index in range(len(values))
    ]


def rubber_band_baseline(
    x_values: list[float],
    values: list[float],
) -> list[float]:
    """Return the piecewise-linear lower convex hull of an ordered spectrum."""
    if len(values) < 2:
        return list(values)
    hull = []
    for index in range(len(values)):
        while len(hull) >= 2:
            first, second = hull[-2], hull[-1]
            cross = (
                (x_values[second] - x_values[first])
                * (values[index] - values[first])
                - (values[second] - values[first])
                * (x_values[index] - x_values[first])
            )
            if cross > 0:
                break
            hull.pop()
        hull.append(index)
    baseline = [0.0] * len(values)
    for left, right in zip(hull, hull[1:]):
        span = x_values[right] - x_values[left]
        for index in range(left, right + 1):
            fraction = (
                (x_values[index] - x_values[left]) / span if span else 0.0
            )
            baseline[index] = values[left] + fraction * (
                values[right] - values[left]
            )
    return baseline


def estimate_baseline(
    values: list[float],
    x_values: list[float] | None = None,
    parameters: dict | None = None,
) -> list[float]:
    configured = parameters or MODEL_PARAMETERS
    method = configured.get("baseline_method", "morphological_opening")
    if method not in BASELINE_METHODS:
        raise ProcessingError(
            422,
            "invalid_baseline_method",
            "Baseline method must be morphological_opening or rubber_band.",
        )
    if method == "rubber_band":
        if x_values is None or len(x_values) != len(values):
            raise ProcessingError(
                422,
                "baseline_axis_required",
                "Rubber-band baseline estimation requires the ordered X axis.",
            )
        return rubber_band_baseline(x_values, values)
    eroded = rolling_extreme(
        values,
        configured["baseline_window_points"],
        min,
    )
    opened = rolling_extreme(
        eroded,
        configured["baseline_window_points"],
        max,
    )
    return moving_average(
        opened,
        configured["baseline_smoothing_points"],
    )


def detect_and_correct_rayleigh_line(
    x_values: list[float],
    intensities: list[float],
    parameters: dict,
) -> tuple[list[float], list[dict]]:
    """Remove a confidently detected zero-shift laser line from derived data."""
    center_tolerance = parameters["rayleigh_center_tolerance_cm-1"]
    context_inner, context_outer = parameters["rayleigh_context_range_cm-1"]
    center_indices = [
        index
        for index, x_value in enumerate(x_values)
        if abs(x_value) <= center_tolerance
    ]
    context_values = [
        intensity
        for x_value, intensity in zip(x_values, intensities)
        if context_inner <= abs(x_value) <= context_outer
    ]
    if not center_indices or len(context_values) < 5:
        return list(intensities), []

    peak_index = max(center_indices, key=lambda index: intensities[index])
    peak_intensity = intensities[peak_index]
    background = median(context_values)
    median_deviation = median(
        abs(value - background) for value in context_values
    )
    robust_sigma = max(1.4826 * median_deviation, 1e-12)
    signal = peak_intensity - background
    background_ratio = signal / max(abs(background), robust_sigma, 1e-12)
    if (
        signal
        < parameters["rayleigh_minimum_robust_sigma"] * robust_sigma
        or background_ratio < parameters["rayleigh_minimum_background_ratio"]
    ):
        return list(intensities), []

    tail_threshold = background + max(
        parameters["rayleigh_minimum_robust_sigma"] * robust_sigma,
        parameters["rayleigh_tail_fraction"] * signal,
    )
    left = peak_index
    while left > 0 and intensities[left - 1] > tail_threshold:
        left -= 1
    right = peak_index
    while (
        right < len(intensities) - 1
        and intensities[right + 1] > tail_threshold
    ):
        right += 1

    padding = parameters["rayleigh_replacement_padding_points"]
    left_anchor = max(0, left - padding - 1)
    right_anchor = min(len(intensities) - 1, right + padding + 1)
    if left_anchor == right_anchor:
        return list(intensities), []

    corrected = list(intensities)
    x_left = x_values[left_anchor]
    x_right = x_values[right_anchor]
    y_left = intensities[left_anchor]
    y_right = intensities[right_anchor]
    for index in range(left_anchor + 1, right_anchor):
        fraction = (x_values[index] - x_left) / (x_right - x_left)
        corrected[index] = y_left + fraction * (y_right - y_left)

    artifact = {
        "category": "instrument_artifact",
        "type": "rayleigh_line_leakage",
        "center_cm-1": x_values[peak_index],
        "observed_intensity": peak_intensity,
        "estimated_background_intensity": background,
        "signal_to_background_ratio": background_ratio,
        "affected_range_cm-1": [
            x_values[left_anchor + 1],
            x_values[right_anchor - 1],
        ],
        "correction": "linear_interpolation_for_derived_processing",
        "confidence": "high",
    }
    return corrected, [artifact]


def detect_and_correct_cosmic_spikes(
    x_values: list[float],
    intensities: list[float],
    parameters: dict,
) -> tuple[list[float], list[dict]]:
    """Interpolate isolated one- or two-channel positive CCD spikes."""
    context_points = parameters["cosmic_spike_context_points"]
    exclusion = parameters["cosmic_spike_exclusion_points"]
    maximum_width = parameters["cosmic_spike_maximum_width_points"]
    corrected = list(intensities)
    artifacts = []
    claimed_indices = set()

    for index in range(context_points, len(intensities) - context_points):
        if index in claimed_indices:
            continue
        context = (
            intensities[index - context_points : index - exclusion]
            + intensities[index + exclusion + 1 : index + context_points + 1]
        )
        if len(context) < 4:
            continue
        background = median(context)
        deviation = median(abs(value - background) for value in context)
        robust_sigma = max(1.4826 * deviation, 1e-12)
        signal = intensities[index] - background
        background_ratio = signal / max(abs(background), robust_sigma, 1e-12)
        if (
            signal
            < parameters["cosmic_spike_minimum_robust_sigma"] * robust_sigma
            or background_ratio
            < parameters["cosmic_spike_minimum_background_ratio"]
        ):
            continue

        threshold = background + max(
            parameters["cosmic_spike_minimum_robust_sigma"] * robust_sigma,
            parameters["cosmic_spike_tail_fraction"] * signal,
        )
        left = index
        while left > 0 and intensities[left - 1] > threshold:
            left -= 1
        right = index
        while right < len(intensities) - 1 and intensities[right + 1] > threshold:
            right += 1
        width = right - left + 1
        if width > maximum_width or left == 0 or right == len(intensities) - 1:
            continue
        group = set(range(left, right + 1))
        if group & claimed_indices:
            continue

        left_anchor = left - 1
        right_anchor = right + 1
        maximum_anchor_deviation = max(
            abs(intensities[left_anchor] - background),
            abs(intensities[right_anchor] - background),
        )
        if maximum_anchor_deviation > (
            parameters["cosmic_spike_maximum_neighbor_fraction"] * signal
        ):
            # Real narrow Raman bands retain elevated shoulders; CCD strikes do not.
            continue
        x_left = x_values[left_anchor]
        x_right = x_values[right_anchor]
        for spike_index in range(left, right + 1):
            fraction = (x_values[spike_index] - x_left) / (x_right - x_left)
            corrected[spike_index] = intensities[left_anchor] + fraction * (
                intensities[right_anchor] - intensities[left_anchor]
            )
        peak_index = max(group, key=lambda item: intensities[item])
        artifacts.append(
            {
                "category": "instrument_artifact",
                "type": "cosmic_ray_spike",
                "center_cm-1": x_values[peak_index],
                "observed_intensity": intensities[peak_index],
                "estimated_background_intensity": background,
                "signal_to_background_ratio": background_ratio,
                "affected_range_cm-1": [x_values[left], x_values[right]],
                "affected_point_count": width,
                "correction": "linear_interpolation_for_derived_processing",
                "confidence": "high",
            }
        )
        claimed_indices.update(group)

    return corrected, artifacts


def is_cosmic_artifact_confirmation(confirmation: dict) -> bool:
    """Recognize user labels that identify a peak as a cosmic artifact."""
    label = re.sub(
        r"[^a-z0-9]+",
        " ",
        str(confirmation.get("label") or "").casefold(),
    ).strip()
    words = set(label.split())
    return "cosmic" in words and bool(
        words & {"artifact", "error", "ray", "spike"}
    )


def is_instrument_artifact_confirmation(confirmation: dict) -> bool:
    """Recognize every user-confirmed instrument-artifact label."""
    if confirmation.get("role") == "instrument_artifact":
        return True
    text_value = " ".join(
        str(confirmation.get(key) or "")
        for key in ("label", "material_system")
    )
    words = set(
        re.sub(r"[^a-z0-9]+", " ", text_value.casefold()).strip().split()
    )
    artifact_words = {"artifact", "artifacts", "artefact", "artefacts"}
    instrument_words = {"instrument", "instrumental", "detector", "measurement"}
    return is_cosmic_artifact_confirmation(confirmation) or bool(
        words & artifact_words and words & instrument_words
    )


def apply_confirmed_instrument_artifacts(
    x_values: list[float],
    intensities: list[float],
    confirmations: list[dict],
    parameters: dict,
) -> tuple[list[float], list[dict]]:
    """Interpolate user-confirmed artifact bands in derived spectra only."""
    corrected = list(intensities)
    artifacts = []
    if len(x_values) < 3:
        return corrected, artifacts

    for confirmation in confirmations:
        try:
            center = float(confirmation["observed_cm-1"])
        except (KeyError, TypeError, ValueError):
            continue
        configured_range = confirmation.get("affected_range_cm-1")
        if (
            isinstance(configured_range, list)
            and len(configured_range) == 2
        ):
            try:
                lower, upper = sorted(float(value) for value in configured_range)
            except (TypeError, ValueError):
                configured_range = None
        if not configured_range:
            half_width = max(
                1.0,
                float(parameters["deconvolution_default_fwhm_cm-1"]) / 2,
            )
            lower, upper = center - half_width, center + half_width

        affected = [
            index
            for index, x_value in enumerate(x_values)
            if lower <= x_value <= upper
        ]
        if not affected:
            continue
        left_anchor = affected[0] - 1
        right_anchor = affected[-1] + 1
        if left_anchor < 0 or right_anchor >= len(x_values):
            continue

        x_left = x_values[left_anchor]
        x_right = x_values[right_anchor]
        y_left = corrected[left_anchor]
        y_right = corrected[right_anchor]
        observed_intensity = max(intensities[index] for index in affected)
        for index in affected:
            fraction = (x_values[index] - x_left) / (x_right - x_left)
            corrected[index] = y_left + fraction * (y_right - y_left)

        artifacts.append(
            {
                "category": "instrument_artifact",
                "type": "user_confirmed_instrument_artifact",
                "center_cm-1": center,
                "observed_intensity": observed_intensity,
                "affected_range_cm-1": [
                    x_values[affected[0]],
                    x_values[affected[-1]],
                ],
                "affected_point_count": len(affected),
                "correction": "linear_interpolation_for_derived_processing",
                "confidence": "user_confirmed",
                "label": confirmation.get("label"),
                "material_system": confirmation.get("material_system"),
                "confirmation": {
                    "clarification_id": confirmation.get("clarification_id"),
                    "confirmed_at": confirmation.get("confirmed_at"),
                },
            }
        )
    return corrected, artifacts


def strongest_band(
    x_values: list[float],
    intensities: list[float],
    lower_bound: float,
    upper_bound: float,
) -> dict | None:
    candidates = [
        (intensity, x_value)
        for x_value, intensity in zip(x_values, intensities)
        if lower_bound <= x_value <= upper_bound
    ]
    if not candidates:
        return None
    intensity, position = max(candidates)
    return {"position_cm-1": position, "intensity": intensity}


def detect_peaks(
    x_values: list[float],
    intensities: list[float],
    parameters: dict | None = None,
) -> list[dict]:
    configured = parameters or MODEL_PARAMETERS
    maximum_intensity = max(intensities)
    differences = [
        abs(intensities[index] - intensities[index - 1])
        for index in range(1, len(intensities))
    ]
    noise_estimate = median(differences) if differences else 0.0
    prominence_threshold = max(
        maximum_intensity * configured["minimum_prominence_fraction"],
        noise_estimate * configured["noise_multiplier"],
    )

    candidates = []
    local_radius = 12
    for index in range(1, len(intensities) - 1):
        intensity = intensities[index]
        if intensity <= intensities[index - 1] or intensity < intensities[index + 1]:
            continue
        left_values = intensities[max(0, index - local_radius) : index]
        right_values = intensities[index + 1 : index + local_radius + 1]
        if not left_values or not right_values:
            continue
        prominence = intensity - max(min(left_values), min(right_values))
        if prominence >= prominence_threshold:
            candidates.append(
                {
                    "position_cm-1": x_values[index],
                    "intensity": intensity,
                    "prominence": prominence,
                }
            )

    selected = []
    for candidate in sorted(
        candidates,
        key=lambda item: item["prominence"],
        reverse=True,
    ):
        if all(
            abs(candidate["position_cm-1"] - peak["position_cm-1"])
            >= configured["minimum_peak_distance_cm-1"]
            for peak in selected
        ):
            selected.append(candidate)
        if len(selected) >= configured["maximum_reported_peaks"]:
            break
    return sorted(selected, key=lambda item: item["position_cm-1"])


def reference_guided_peak_candidates(
    x_values: list[float],
    intensities: list[float],
    reference_sources: list[dict],
    material_system: str | None,
    parameters: dict,
) -> list[dict]:
    """Recover locally significant bands expected for a declared material."""
    components = declared_material_components(material_system)
    if not components or len(x_values) < 3:
        return []
    differences = [
        abs(intensities[index] - intensities[index - 1])
        for index in range(1, len(intensities))
    ]
    noise_estimate = median(differences) if differences else 0.0
    prominence_threshold = max(
        noise_estimate * parameters["reference_guided_noise_multiplier"],
        1e-12,
    )
    local_radius = 12
    candidates = []
    for source in reference_sources:
        if source.get("extraction_status") != "ready" or not (
            reference_applies_to_material(
                source.get("material_system") or "",
                components,
            )
        ):
            continue
        try:
            evidence = json.loads(source["evidence_json"])
        except (KeyError, TypeError, json.JSONDecodeError):
            continue
        tolerance = float(
            evidence.get(
                "match_tolerance_cm-1",
                parameters["reference_match_tolerance_cm-1"],
            )
        )
        for reference_peak in evidence.get("peaks", []):
            if reference_peak.get("position_range_cm-1") is not None:
                lower, upper = map(
                    float,
                    reference_peak["position_range_cm-1"],
                )
            elif reference_peak.get("position_cm-1") is not None:
                position = float(reference_peak["position_cm-1"])
                lower, upper = position - tolerance, position + tolerance
            else:
                continue
            band_indices = [
                index
                for index, x_value in enumerate(x_values)
                if lower <= x_value <= upper
            ]
            if not band_indices:
                continue
            peak_index = max(
                band_indices,
                key=lambda index: intensities[index],
            )
            if peak_index == 0 or peak_index == len(intensities) - 1:
                continue
            intensity = intensities[peak_index]
            if (
                intensity <= intensities[peak_index - 1]
                or intensity < intensities[peak_index + 1]
            ):
                continue
            left_values = intensities[
                max(0, peak_index - local_radius) : peak_index
            ]
            right_values = intensities[
                peak_index + 1 : peak_index + local_radius + 1
            ]
            if not left_values or not right_values:
                continue
            prominence = intensity - max(
                min(left_values),
                min(right_values),
            )
            if prominence < prominence_threshold:
                continue
            candidates.append(
                {
                    "position_cm-1": x_values[peak_index],
                    "intensity": intensity,
                    "prominence": prominence,
                    "detection_source": "reference_guided_local_prominence",
                }
            )
    return merge_peak_candidates([], candidates)


def interpolate_level_crossing(
    x_one: float,
    y_one: float,
    x_two: float,
    y_two: float,
    level: float,
) -> float:
    if y_one == y_two:
        return (x_one + x_two) / 2
    fraction = (level - y_one) / (y_two - y_one)
    return x_one + min(1.0, max(0.0, fraction)) * (x_two - x_one)


def estimate_peak_fwhm(
    x_values: list[float],
    intensities: list[float],
    peak: dict,
    parameters: dict,
) -> float:
    peak_index = min(
        range(len(x_values)),
        key=lambda index: abs(x_values[index] - peak["position_cm-1"]),
    )
    peak_height = max(float(peak.get("intensity", 0.0)), 0.0)
    half_height = peak_height / 2
    left_crossing = None
    for index in range(peak_index - 1, -1, -1):
        if intensities[index] <= half_height:
            left_crossing = interpolate_level_crossing(
                x_values[index],
                intensities[index],
                x_values[index + 1],
                intensities[index + 1],
                half_height,
            )
            break
    right_crossing = None
    for index in range(peak_index + 1, len(x_values)):
        if intensities[index] <= half_height:
            right_crossing = interpolate_level_crossing(
                x_values[index - 1],
                intensities[index - 1],
                x_values[index],
                intensities[index],
                half_height,
            )
            break

    spacings = [
        x_values[index] - x_values[index - 1]
        for index in range(1, len(x_values))
    ]
    spacing = median(spacings) if spacings else 1.0
    minimum_fwhm = max(
        spacing * parameters["deconvolution_minimum_fwhm_points"],
        1e-9,
    )
    if left_crossing is None or right_crossing is None:
        estimated = parameters["deconvolution_default_fwhm_cm-1"]
    else:
        estimated = right_crossing - left_crossing
    return min(
        parameters["deconvolution_maximum_fwhm_cm-1"],
        max(minimum_fwhm, estimated),
    )


def peak_profile_value(
    x_value: float,
    center: float,
    fwhm: float,
    profile: str,
    pseudo_voigt_fraction: float,
) -> float:
    scaled = (x_value - center) / max(fwhm, 1e-12)
    gaussian = math.exp(-4 * math.log(2) * scaled * scaled)
    lorentzian = 1 / (1 + 4 * scaled * scaled)
    if profile == "gaussian":
        return gaussian
    if profile == "lorentzian":
        return lorentzian
    return (
        pseudo_voigt_fraction * lorentzian
        + (1 - pseudo_voigt_fraction) * gaussian
    )


def profile_area(
    amplitude: float,
    fwhm: float,
    profile: str,
    pseudo_voigt_fraction: float,
) -> float:
    gaussian_area = amplitude * fwhm * math.sqrt(math.pi) / (
        2 * math.sqrt(math.log(2))
    )
    lorentzian_area = amplitude * math.pi * fwhm / 2
    if profile == "gaussian":
        return gaussian_area
    if profile == "lorentzian":
        return lorentzian_area
    return (
        pseudo_voigt_fraction * lorentzian_area
        + (1 - pseudo_voigt_fraction) * gaussian_area
    )


def solve_nonnegative_amplitudes(
    target: list[float],
    basis: list[list[float]],
    initial_amplitudes: list[float],
    iterations: int,
) -> tuple[list[float], list[float]]:
    amplitudes = list(initial_amplitudes)
    fitted = [
        sum(
            amplitude * component[index]
            for amplitude, component in zip(amplitudes, basis)
        )
        for index in range(len(target))
    ]
    for _iteration in range(iterations):
        for component_index, component in enumerate(basis):
            old_amplitude = amplitudes[component_index]
            numerator = sum(
                value
                * (target[index] - fitted[index] + old_amplitude * value)
                for index, value in enumerate(component)
            )
            denominator = sum(value * value for value in component)
            new_amplitude = max(0.0, numerator / max(denominator, 1e-12))
            if new_amplitude == old_amplitude:
                continue
            difference = new_amplitude - old_amplitude
            amplitudes[component_index] = new_amplitude
            for index, value in enumerate(component):
                fitted[index] += difference * value
    return amplitudes, fitted


def deconvolution_residual_quality(
    x_values: list[float],
    target: list[float],
    fitted: list[float],
    seed_centers: list[float],
    centers: list[float],
    widths: list[float],
    parameters: dict,
) -> dict:
    signed_residuals = [
        observed - estimate for observed, estimate in zip(target, fitted)
    ]
    threshold = float(
        parameters["deconvolution_maximum_residual_to_fit_ratio"]
    )
    spacings = [
        x_values[index] - x_values[index - 1]
        for index in range(1, len(x_values))
    ]
    minimum_radius = 2 * median(spacings) if spacings else 1.0
    component_checks = []
    for seed_center, center, width in zip(seed_centers, centers, widths):
        radius = max(float(width), minimum_radius)
        indices = [
            index
            for index, x_value in enumerate(x_values)
            if abs(x_value - center) <= radius
        ]
        if not indices:
            indices = [
                min(
                    range(len(x_values)),
                    key=lambda index: abs(x_values[index] - center),
                )
            ]
        fitted_peak = max(abs(fitted[index]) for index in indices)
        residual_peak = max(abs(signed_residuals[index]) for index in indices)
        ratio = residual_peak / fitted_peak if fitted_peak > 1e-12 else None
        component_checks.append(
            {
                "seed_center_cm-1": seed_center,
                "center_cm-1": center,
                "window_cm-1": [center - radius, center + radius],
                "maximum_fitted_intensity": fitted_peak,
                "maximum_absolute_residual": residual_peak,
                "residual_to_fit_ratio": ratio,
                "accepted": ratio is not None and ratio <= threshold,
            }
        )
    maximum_fitted = max((abs(value) for value in fitted), default=0.0)
    maximum_residual = max(
        (abs(value) for value in signed_residuals),
        default=0.0,
    )
    global_ratio = (
        maximum_residual / maximum_fitted if maximum_fitted > 1e-12 else None
    )
    accepted = (
        global_ratio is not None
        and global_ratio <= threshold
        and all(check["accepted"] for check in component_checks)
    )
    finite_ratios = [
        check["residual_to_fit_ratio"]
        for check in component_checks
        if check["residual_to_fit_ratio"] is not None
    ]
    maximum_component_ratio = max(finite_ratios, default=None)
    return {
        "accepted": accepted,
        "acceptance_status": "accepted" if accepted else "review_required",
        "maximum_allowed_residual_to_fit_ratio": threshold,
        "global_maximum_absolute_residual": maximum_residual,
        "global_maximum_fitted_intensity": maximum_fitted,
        "global_residual_to_fit_ratio": global_ratio,
        "maximum_component_residual_to_fit_ratio": maximum_component_ratio,
        "component_checks": component_checks,
        "reason": (
            "Every component window and the full fit satisfy the residual-to-fit ceiling."
            if accepted
            else "At least one component window or the full fit exceeds the residual-to-fit ceiling; the fit requires review and must not be used quantitatively."
        ),
    }


def deconvolve_peaks(
    x_values: list[float],
    intensities: list[float],
    peaks: list[dict],
    profile: str,
    parameters: dict,
) -> dict:
    if profile not in DECONVOLUTION_PROFILES:
        raise ProcessingError(
            422,
            "invalid_deconvolution_profile",
            "Deconvolution must be none, gaussian, lorentzian, or pseudo_voigt.",
        )
    if profile == "none":
        return {"status": "disabled", "profile": "none", "components": []}
    explicitly_rejected_noise_peaks = [
        peak
        for peak in peaks
        if any(
            normalized_material_name(assignment.get("label") or "") == "noise"
            for assignment in peak.get("assignments", [])
        )
    ]
    positive_peaks = [
        peak
        for peak in peaks
        if peak.get("intensity", 0) > 0
        and peak not in explicitly_rejected_noise_peaks
    ]
    if not positive_peaks:
        return {"status": "no_peaks", "profile": profile, "components": []}

    fraction = parameters["deconvolution_pseudo_voigt_fraction"]
    centers = [float(peak["position_cm-1"]) for peak in positive_peaks]
    seed_centers = list(centers)
    widths = []
    for peak in positive_peaks:
        width = estimate_peak_fwhm(x_values, intensities, peak, parameters)
        mode_group = peak.get("mode_group")
        if mode_group:
            lower, upper = map(float, mode_group["range_cm-1"])
            width = max(width, (upper - lower) * 1.5)
        widths.append(width)
    basis = [
        [
            peak_profile_value(
                x_value,
                center,
                width,
                profile,
                fraction,
            )
            for x_value in x_values
        ]
        for center, width in zip(centers, widths)
    ]
    target = [max(0.0, value) for value in intensities]
    initial_amplitudes = [
        max(0.0, float(peak["intensity"])) for peak in positive_peaks
    ]
    amplitudes, fitted = solve_nonnegative_amplitudes(
        target,
        basis,
        initial_amplitudes,
        max(10, parameters["deconvolution_nnls_iterations"] // 2),
    )
    spacings = [
        x_values[index] - x_values[index - 1]
        for index in range(1, len(x_values))
    ]
    center_step = median(spacings) if spacings else 1.0
    minimum_fwhm = max(
        center_step * parameters["deconvolution_minimum_fwhm_points"],
        1e-9,
    )
    maximum_fwhm = parameters["deconvolution_maximum_fwhm_cm-1"]
    for _pass in range(parameters["deconvolution_refinement_passes"]):
        for component_index, component in enumerate(basis):
            old_amplitude = amplitudes[component_index]
            residual_without_component = [
                target[index] - fitted[index] + old_amplitude * value
                for index, value in enumerate(component)
            ]
            best = None
            for center_offset in (-center_step, 0.0, center_step):
                candidate_center = centers[component_index] + center_offset
                maximum_shift = (
                    parameters["deconvolution_refinement_passes"] * center_step
                )
                candidate_center = min(
                    seed_centers[component_index] + maximum_shift,
                    max(
                        seed_centers[component_index] - maximum_shift,
                        candidate_center,
                    ),
                )
                for width_scale in parameters[
                    "deconvolution_width_scale_candidates"
                ]:
                    candidate_width = min(
                        maximum_fwhm,
                        max(minimum_fwhm, widths[component_index] * width_scale),
                    )
                    candidate_basis = [
                        peak_profile_value(
                            x_value,
                            candidate_center,
                            candidate_width,
                            profile,
                            fraction,
                        )
                        for x_value in x_values
                    ]
                    denominator = sum(value * value for value in candidate_basis)
                    candidate_amplitude = max(
                        0.0,
                        sum(
                            value * residual_without_component[index]
                            for index, value in enumerate(candidate_basis)
                        )
                        / max(denominator, 1e-12),
                    )
                    error = sum(
                        (
                            residual_without_component[index]
                            - candidate_amplitude * value
                        )
                        ** 2
                        for index, value in enumerate(candidate_basis)
                    )
                    candidate = (
                        error,
                        candidate_center,
                        candidate_width,
                        candidate_amplitude,
                        candidate_basis,
                    )
                    if best is None or candidate[0] < best[0]:
                        best = candidate
            _error, best_center, best_width, best_amplitude, best_basis = best
            centers[component_index] = best_center
            widths[component_index] = best_width
            amplitudes[component_index] = best_amplitude
            basis[component_index] = best_basis
            fitted = [
                fitted[index]
                - old_amplitude * component[index]
                + best_amplitude * best_basis[index]
                for index in range(len(target))
            ]

    amplitudes, fitted = solve_nonnegative_amplitudes(
        target,
        basis,
        amplitudes,
        parameters["deconvolution_nnls_iterations"],
    )

    components = []
    for peak, seed_center, center, width, amplitude, component_basis in zip(
        positive_peaks,
        seed_centers,
        centers,
        widths,
        amplitudes,
        basis,
    ):
        components.append(
            {
                "seed_center_cm-1": seed_center,
                "center_cm-1": center,
                "amplitude": amplitude,
                "fwhm_cm-1": width,
                "area": profile_area(amplitude, width, profile, fraction),
                "assignment": (peak.get("assignments") or [None])[0],
                "mode_group": peak.get("mode_group"),
                "values": [amplitude * value for value in component_basis],
            }
        )
    residual_quality = deconvolution_residual_quality(
        x_values,
        target,
        fitted,
        seed_centers,
        centers,
        widths,
        parameters,
    )
    rejected_components = []
    global_ratio = residual_quality["global_residual_to_fit_ratio"]
    threshold = residual_quality["maximum_allowed_residual_to_fit_ratio"]
    accepted_indices = [
        index
        for index, check in enumerate(residual_quality["component_checks"])
        if check["accepted"]
    ]
    if (
        global_ratio is not None
        and global_ratio <= threshold
        and accepted_indices
        and len(accepted_indices) < len(components)
    ):
        rejected_components = [
            {
                **{
                    key: value
                    for key, value in components[index].items()
                    if key != "values"
                },
                "quality": residual_quality["component_checks"][index],
                "rejection_reason": (
                    "The local residual-to-fit ratio exceeds the component "
                    "acceptance limit."
                ),
            }
            for index in range(len(components))
            if index not in accepted_indices
        ]
        components = [components[index] for index in accepted_indices]
        fitted = [
            sum(component["values"][index] for component in components)
            for index in range(len(target))
        ]
        residual_quality = deconvolution_residual_quality(
            x_values,
            target,
            fitted,
            [component["seed_center_cm-1"] for component in components],
            [component["center_cm-1"] for component in components],
            [component["fwhm_cm-1"] for component in components],
            parameters,
        )

    residual_sum_squares = sum(
        (observed - estimate) ** 2
        for observed, estimate in zip(target, fitted)
    )
    mean_target = sum(target) / len(target)
    total_sum_squares = sum((value - mean_target) ** 2 for value in target)
    r_squared = (
        1 - residual_sum_squares / total_sum_squares
        if total_sum_squares > 0
        else None
    )
    target_range = max(target) - min(target)
    rmse = math.sqrt(residual_sum_squares / len(target))
    return {
        "status": "fitted",
        "profile": profile,
        "pseudo_voigt_fraction": fraction if profile == "pseudo_voigt" else None,
        "components": components,
        "rejected_components": rejected_components,
        "explicit_noise_exclusions": [
            {
                "position_cm-1": peak["position_cm-1"],
                "intensity": peak.get("intensity"),
                "assignments": peak.get("assignments", []),
                "reason": "A user-confirmed assignment labels this feature as noise.",
            }
            for peak in explicitly_rejected_noise_peaks
        ],
        "fit_values": fitted,
        "residual_values": [
            observed - estimate for observed, estimate in zip(target, fitted)
        ],
        "quality": {
            "rmse": rmse,
            "normalized_rmse": rmse / target_range if target_range > 0 else None,
            "r_squared": r_squared,
            **residual_quality,
        },
    }


def broad_substrate_peak_candidates(
    x_values: list[float],
    intensities: list[float],
    substrate: str,
    parameters: dict,
    substrate_profiles: dict | None = None,
    detection_source: str = "broad_substrate_search",
) -> list[dict]:
    """Find low-prominence, broad maxima only inside substrate evidence bands."""
    if len(x_values) < 5:
        return []
    smoothing_points = max(
        3,
        int(parameters["substrate_broad_smoothing_points"]),
    )
    if smoothing_points % 2 == 0:
        smoothing_points += 1
    smoothed = moving_average(intensities, smoothing_points)
    bands = [
        *SUBSTRATE_BANDS[substrate],
        *((substrate_profiles or {}).get(substrate, {}).get("learned_bands", [])),
    ]
    relevant_indices = [
        index
        for index, x_value in enumerate(x_values)
        if any(
            band["range_cm-1"][0] <= x_value <= band["range_cm-1"][1]
            for band in bands
        )
    ]
    if not relevant_indices:
        return []
    relevant_values = [smoothed[index] for index in relevant_indices]
    relevant_range = max(relevant_values) - min(relevant_values)
    overall_range = max(smoothed) - min(smoothed)
    differences = [
        abs(smoothed[index] - smoothed[index - 1])
        for index in range(1, len(smoothed))
    ]
    noise = median(differences) if differences else 0.0
    threshold = max(
        noise * float(parameters["substrate_broad_noise_multiplier"]),
        relevant_range
        * float(parameters["substrate_broad_minimum_range_fraction"]),
        overall_range
        * float(parameters["substrate_broad_minimum_range_fraction"]),
    )
    candidates = []
    for band in bands:
        lower, upper = map(float, band["range_cm-1"])
        indices = [
            index
            for index, x_value in enumerate(x_values)
            if lower <= x_value <= upper
        ]
        if len(indices) < 5:
            continue
        first = indices[0]
        last = indices[-1]
        x_first = float(x_values[first])
        x_last = float(x_values[last])
        span = x_last - x_first
        if span <= 0:
            continue
        excess = {}
        for index in indices:
            fraction = (float(x_values[index]) - x_first) / span
            local_line = smoothed[first] + fraction * (
                smoothed[last] - smoothed[first]
            )
            excess[index] = smoothed[index] - local_line
        peak_index = max(indices, key=lambda index: excess[index])
        prominence = float(excess[peak_index])
        if prominence < threshold:
            continue
        half_level = prominence / 2
        above_half = [
            index for index in indices if excess[index] >= half_level
        ]
        if not above_half:
            continue
        fwhm = float(x_values[max(above_half)]) - float(
            x_values[min(above_half)]
        )
        if fwhm < float(parameters["substrate_broad_minimum_fwhm_cm-1"]):
            continue
        candidates.append(
            {
                "position_cm-1": x_values[peak_index],
                "intensity": intensities[peak_index],
                "prominence": prominence,
                "estimated_fwhm_cm-1": fwhm,
                "detection_source": detection_source,
                "substrate_band_label": band["label"],
                "substrate_band_range_cm-1": [lower, upper],
            }
        )
    selected = []
    for candidate in sorted(
        candidates,
        key=lambda item: item["prominence"],
        reverse=True,
    ):
        if all(
            abs(
                float(candidate["position_cm-1"])
                - float(existing["position_cm-1"])
            )
            >= float(parameters["minimum_peak_distance_cm-1"])
            for existing in selected
        ):
            selected.append(candidate)
    return sorted(selected, key=lambda item: item["position_cm-1"])


def empirical_substrate_peak_candidates(
    x_values: list[float],
    intensities: list[float],
    substrate_profiles: dict | None,
    parameters: dict,
) -> list[dict]:
    """Recover reference-expected peaks using the target spectrum's noise."""
    if len(x_values) < 5:
        return []
    differences = [
        abs(intensities[index] - intensities[index - 1])
        for index in range(1, len(intensities))
    ]
    noise = median(differences) if differences else 0.0
    threshold = noise * float(parameters["substrate_reference_noise_multiplier"])
    candidates = []
    for substrate, profile in (substrate_profiles or {}).items():
        for band in profile.get("learned_bands", []):
            if band.get("evidence_source") != "user_confirmed_substrate_reference":
                continue
            lower, upper = map(float, band["range_cm-1"])
            band_indices = [
                index
                for index, x_value in enumerate(x_values)
                if lower <= x_value <= upper
            ]
            if not band_indices:
                continue
            peak_index = max(
                band_indices,
                key=lambda index: intensities[index],
            )
            context_span = max(upper - lower, 8.0)
            left = [
                intensities[index]
                for index, x_value in enumerate(x_values)
                if lower - context_span <= x_value < lower
            ]
            right = [
                intensities[index]
                for index, x_value in enumerate(x_values)
                if upper < x_value <= upper + context_span
            ]
            if not left or not right:
                continue
            prominence = float(intensities[peak_index]) - max(min(left), min(right))
            if prominence < threshold:
                continue
            candidates.append(
                {
                    "position_cm-1": x_values[peak_index],
                    "intensity": intensities[peak_index],
                    "prominence": prominence,
                    "detection_source": "substrate_reference_noise_gated",
                    "substrate": substrate,
                    "substrate_band_label": band["label"],
                    "substrate_band_range_cm-1": [lower, upper],
                    "target_signal_to_noise": (
                        prominence / noise if noise > 0 else None
                    ),
                    "reference_relative_prominence": band.get(
                        "reference_relative_prominence"
                    ),
                }
            )
    return merge_peak_candidates([], candidates)


def merge_peak_candidates(
    primary: list[dict],
    additional: list[dict],
    tolerance_cm_1: float = 4.0,
) -> list[dict]:
    merged = [dict(peak) for peak in primary]
    for candidate in additional:
        if any(
            abs(
                float(peak["position_cm-1"])
                - float(candidate["position_cm-1"])
            )
            <= tolerance_cm_1
            for peak in merged
        ):
            continue
        merged.append(candidate)
    return sorted(merged, key=lambda peak: peak["position_cm-1"])


def substrate_band_matches(
    peaks: list[dict],
    substrate: str,
    substrate_profiles: dict | None = None,
) -> list[dict]:
    matches = []
    learned_bands = (substrate_profiles or {}).get(substrate, {}).get(
        "learned_bands", []
    )
    for band in (*SUBSTRATE_BANDS[substrate], *learned_bands):
        lower, upper = band["range_cm-1"]
        candidates = [
            peak
            for peak in peaks
            if lower <= float(peak["position_cm-1"]) <= upper
        ]
        if not candidates:
            continue
        selected = (
            candidates
            if band.get("allow_multiple")
            else [max(candidates, key=lambda peak: peak.get("prominence", 0.0))]
        )
        matches.append(
            {
                "label": band["label"],
                "range_cm-1": [lower, upper],
                "weight": band["weight"],
                "peaks": selected,
                "evidence_source": band.get("evidence_source", "curated"),
            }
        )
    return matches


def substrate_detection(
    peaks: list[dict],
    substrate_profiles: dict | None = None,
) -> dict:
    candidates = []
    for substrate in ("sio2_si", "glass"):
        matches = substrate_band_matches(peaks, substrate, substrate_profiles)
        score = sum(match["weight"] for match in matches)
        candidates.append(
            {
                "substrate": substrate,
                "score": score,
                "confidence": (
                    "high" if score >= 0.6 else "moderate" if score >= 0.4 else "low"
                ),
                "matched_bands": [
                    {
                        "label": match["label"],
                        "reference_range_cm-1": match["range_cm-1"],
                        "observed_peaks_cm-1": [
                            peak["position_cm-1"] for peak in match["peaks"]
                        ],
                        "evidence_source": match["evidence_source"],
                    }
                    for match in matches
                ],
            }
        )
    return max(candidates, key=lambda candidate: candidate["score"])


def interpolate_trace(
    source_x: list[float],
    source_y: list[float],
    target_x: list[float],
) -> list[float]:
    """Linearly interpolate an ordered trace without extrapolating its edges."""
    if len(source_x) != len(source_y) or not source_x:
        return []
    result = []
    right = 1
    for target in target_x:
        if target <= source_x[0]:
            result.append(float(source_y[0]))
            continue
        if target >= source_x[-1]:
            result.append(float(source_y[-1]))
            continue
        while right < len(source_x) - 1 and source_x[right] < target:
            right += 1
        left = right - 1
        span = float(source_x[right]) - float(source_x[left])
        fraction = (target - float(source_x[left])) / span if span else 0.0
        result.append(
            float(source_y[left])
            + fraction * (float(source_y[right]) - float(source_y[left]))
        )
    return result


def trace_peak_position(
    x_values: list[float],
    intensities: list[float],
    lower: float,
    upper: float,
) -> float | None:
    indices = [
        index for index, value in enumerate(x_values) if lower <= value <= upper
    ]
    if not indices:
        return None
    peak_index = max(indices, key=lambda index: intensities[index])
    return float(x_values[peak_index])


def fit_substrate_reference_ensemble(
    x_values: list[float],
    intensities: list[float],
    peaks: list[dict],
    substrate: str,
    reference_traces: list[dict],
    parameters: dict,
) -> dict | None:
    """Fit a target-adaptive nonnegative mixture of pure-substrate traces."""
    if not reference_traces or len(x_values) < 3:
        return None
    target_span = float(x_values[-1]) - float(x_values[0])
    if target_span <= 0.0:
        return None
    target_calibration_peak = (
        trace_peak_position(x_values, intensities, 500.0, 540.0)
        if substrate == "sio2_si"
        else None
    )
    aligned_references = []
    excluded_references = []
    for reference in reference_traces:
        reference_x = [float(value) for value in reference.get("x_values", [])]
        reference_y = [
            max(0.0, float(value))
            for value in reference.get("corrected_values", [])
        ]
        if len(reference_x) != len(reference_y) or len(reference_x) < 3:
            continue
        reference_peak = (
            trace_peak_position(reference_x, reference_y, 500.0, 540.0)
            if substrate == "sio2_si"
            else None
        )
        shift = (
            target_calibration_peak - reference_peak
            if target_calibration_peak is not None and reference_peak is not None
            else 0.0
        )
        aligned_x = [value + shift for value in reference_x]
        overlap_start = max(float(x_values[0]), aligned_x[0])
        overlap_end = min(float(x_values[-1]), aligned_x[-1])
        coverage = max(0.0, overlap_end - overlap_start) / target_span
        minimum_coverage = float(
            parameters["substrate_ensemble_minimum_coverage_fraction"]
        )
        if coverage < minimum_coverage:
            excluded_references.append(
                {
                    "file_id": reference.get("file_id"),
                    "filename": reference.get("filename"),
                    "reason": "below_minimum_target_coverage",
                    "coverage_fraction": coverage,
                    "minimum_coverage_fraction": minimum_coverage,
                }
            )
            continue
        interpolated = interpolate_trace(aligned_x, reference_y, x_values)
        calibration_indices = [
            index
            for index, value in enumerate(x_values)
            if (500.0 <= value <= 540.0 if substrate == "sio2_si" else True)
        ]
        normalizer = max(
            (interpolated[index] for index in calibration_indices),
            default=max(interpolated, default=0.0),
        )
        if normalizer <= 0.0:
            continue
        aligned_references.append(
            {
                **reference,
                "alignment_shift_cm-1": shift,
                "coverage_fraction": coverage,
                "basis": [value / normalizer for value in interpolated],
            }
        )
    if not aligned_references:
        return None

    basis = [reference["basis"] for reference in aligned_references]
    consensus = [median(values) for values in zip(*basis)]
    consensus_peak = max(consensus, default=0.0)
    if consensus_peak <= 0.0:
        return None
    support_fraction = [
        sum(profile[index] >= 0.02 * consensus_peak for profile in basis)
        / len(basis)
        for index in range(len(x_values))
    ]
    protected_ranges = []
    measured_material_system = parameters.get("import_metadata", {}).get(
        "material_system"
    )
    for peak in peaks:
        assignments = [
            assignment
            for assignment in peak.get("assignments", [])
            if material_assignment_protects_substrate_overlap(
                assignment,
                measured_material_system,
            )
        ]
        if not assignments:
            continue
        width = estimate_peak_fwhm(
            x_values,
            intensities,
            peak,
            parameters,
        )
        half_width = max(
            float(parameters["minimum_peak_distance_cm-1"]),
            min(float(width), 40.0),
        )
        protected_ranges.append(
            {
                "position_cm-1": float(peak["position_cm-1"]),
                "protected_range_cm-1": [
                    float(peak["position_cm-1"]) - half_width,
                    float(peak["position_cm-1"]) + half_width,
                ],
                "assignments": assignments,
            }
        )
    for feedback in parameters.get("substrate_peak_feedback") or []:
        if (
            feedback.get("substrate") != substrate
            or feedback.get("action") != "keep"
        ):
            continue
        center = float(feedback["center_cm_1"])
        half_width = float(feedback["half_width_cm_1"])
        protected_ranges.append(
            {
                "position_cm-1": center,
                "protected_range_cm-1": [
                    center - half_width,
                    center + half_width,
                ],
                "assignments": [],
                "feedback_action": "keep",
                "reason": "User feedback classifies this interval as sample signal.",
            }
        )
    support_threshold = float(
        parameters["substrate_ensemble_support_fraction_threshold"]
    )
    anchors = [
        index
        for index, (x_value, signal) in enumerate(zip(x_values, consensus))
        if x_value >= 50.0
        and support_fraction[index] >= support_threshold
        and signal >= 0.04 * consensus_peak
        and not any(
            lower <= x_value <= upper
            for lower, upper in (
                item["protected_range_cm-1"] for item in protected_ranges
            )
        )
    ]
    if len(anchors) < 3:
        return None

    target = [max(0.0, float(value)) for value in intensities]
    coefficients = [0.0] * len(basis)
    fitted = [0.0] * len(target)
    for _iteration in range(80):
        largest_change = 0.0
        for basis_index, profile in enumerate(basis):
            previous = coefficients[basis_index]
            numerator = sum(
                profile[index]
                * (target[index] - fitted[index] + previous * profile[index])
                for index in anchors
            )
            denominator = sum(profile[index] ** 2 for index in anchors) or 1.0
            updated = max(0.0, numerator / denominator)
            delta = updated - previous
            if delta:
                coefficients[basis_index] = updated
                for index, value in enumerate(profile):
                    fitted[index] += delta * value
            largest_change = max(largest_change, abs(delta))
        if largest_change < 1e-7:
            break

    fitted = [min(max(0.0, value), target[index]) for index, value in enumerate(fitted)]
    for protected in protected_ranges:
        lower, upper = protected["protected_range_cm-1"]
        indices = [
            index for index, value in enumerate(x_values) if lower <= value <= upper
        ]
        if not indices or indices[0] == 0 or indices[-1] >= len(x_values) - 1:
            continue
        left = indices[0] - 1
        right = indices[-1] + 1
        span = float(x_values[right]) - float(x_values[left])
        for index in indices:
            fraction = (
                (float(x_values[index]) - float(x_values[left])) / span
                if span
                else 0.0
            )
            fitted[index] = fitted[left] + fraction * (fitted[right] - fitted[left])
            fitted[index] = min(max(0.0, fitted[index]), target[index])

    fitted_anchor_values = [fitted[index] for index in anchors]
    target_anchor_values = [target[index] for index in anchors]
    residual_sum_squares = sum(
        (observed - predicted) ** 2
        for observed, predicted in zip(target_anchor_values, fitted_anchor_values)
    )
    target_mean = sum(target_anchor_values) / len(target_anchor_values)
    total_sum_squares = sum(
        (observed - target_mean) ** 2 for observed in target_anchor_values
    )
    rmse = math.sqrt(residual_sum_squares / len(anchors))
    target_range = max(target_anchor_values) - min(target_anchor_values)
    r_squared = (
        1.0 - residual_sum_squares / total_sum_squares
        if total_sum_squares > 0.0
        else None
    )
    quality = {
        "r_squared": r_squared,
        "rmse": rmse,
        "normalized_rmse": rmse / target_range if target_range > 0.0 else None,
        "evaluated_point_count": len(anchors),
        "accepted": (
            r_squared is not None
            and r_squared
            >= float(parameters["substrate_auto_minimum_fit_r_squared"])
        ),
    }

    component_series = [
        [coefficient * value for value in reference["basis"]]
        for coefficient, reference in zip(coefficients, aligned_references)
    ]
    for index in range(len(x_values)):
        component_total = sum(values[index] for values in component_series)
        scale = fitted[index] / component_total if component_total > 0.0 else 0.0
        for values in component_series:
            values[index] *= scale
    components = []
    for coefficient, reference, component_values in zip(
        coefficients,
        aligned_references,
        component_series,
    ):
        components.append(
            {
                "profile": "measured_reference_trace",
                "center_cm-1": (
                    target_calibration_peak
                    if target_calibration_peak is not None
                    else float(x_values[max(range(len(component_values)), key=component_values.__getitem__)])
                ),
                "amplitude": max(component_values, default=0.0),
                "fwhm_cm-1": None,
                "area": sum(
                    (component_values[index] + component_values[index - 1])
                    * (float(x_values[index]) - float(x_values[index - 1]))
                    / 2
                    for index in range(1, len(x_values))
                ),
                "values": component_values,
                "source_file_id": reference.get("file_id"),
                "source_filename": reference.get("filename"),
                "source_sha256": reference.get("source_sha256"),
                "coefficient": coefficient,
                "alignment_shift_cm-1": reference["alignment_shift_cm-1"],
                "coverage_fraction": reference["coverage_fraction"],
                "assignment": {
                    "material_system": (
                        "SiO2 / crystalline Si"
                        if substrate == "sio2_si"
                        else "Glass / SiO2"
                    ),
                    "label": "Measured pure-substrate reference",
                    "role": "substrate",
                    "confidence": "reference_ensemble",
                },
            }
        )
    coefficient_total = sum(coefficients)
    return {
        "corrected_values": [
            max(0.0, observed - substrate_value)
            for observed, substrate_value in zip(target, fitted)
        ],
        "fit_values": fitted,
        "components": components,
        "quality": quality,
        "protected_material_peaks": protected_ranges,
        "reference_ensemble": {
            "method": "target_adaptive_nonnegative_measured_reference_mixture",
            "reference_count": len(aligned_references),
            "anchor_point_count": len(anchors),
            "support_fraction_threshold": support_threshold,
            "minimum_target_coverage_fraction": minimum_coverage,
            "references": [
                {
                    "file_id": reference.get("file_id"),
                    "filename": reference.get("filename"),
                    "source_sha256": reference.get("source_sha256"),
                    "coefficient": coefficient,
                    "weight": coefficient / coefficient_total if coefficient_total else 0.0,
                    "alignment_shift_cm-1": reference["alignment_shift_cm-1"],
                    "coverage_fraction": reference["coverage_fraction"],
                }
                for coefficient, reference in zip(coefficients, aligned_references)
            ],
            "excluded_references": excluded_references,
        },
    }


def detect_and_subtract_substrate(
    x_values: list[float],
    intensities: list[float],
    peaks: list[dict],
    mode: str,
    parameters: dict,
    reference_traces: dict | None = None,
) -> tuple[list[float], dict]:
    if mode not in SUBSTRATE_CORRECTION_MODES:
        raise ProcessingError(
            422,
            "invalid_substrate_correction_mode",
            (
                "Substrate correction must be none, detect, auto, glass, "
                "or sio2_si."
            ),
        )
    if mode == "none":
        return list(intensities), {
            "status": "disabled",
            "requested_mode": mode,
            "components": [],
        }

    substrate_profiles = parameters.get("substrate_profiles") or {}
    reference_strategy = parameters.get(
        "substrate_reference_strategy", "automatic"
    )
    substrate_feedback = parameters.get("substrate_peak_feedback") or []
    detection = substrate_detection(peaks, substrate_profiles)
    confirmed_substrate = parameters.get("confirmed_substrate")
    feedback_substrates = {
        feedback.get("substrate")
        for feedback in substrate_feedback
        if feedback.get("action") == "remove"
        and feedback.get("substrate") in {"glass", "sio2_si"}
    }
    feedback_substrate = (
        next(iter(feedback_substrates)) if len(feedback_substrates) == 1 else None
    )
    selected_substrate = (
        confirmed_substrate
        if mode in {"detect", "auto"}
        and confirmed_substrate in {"glass", "sio2_si"}
        else feedback_substrate
        if mode in {"detect", "auto"} and feedback_substrate is not None
        else detection["substrate"]
        if mode in {"detect", "auto"}
        else mode
    )
    matches = substrate_band_matches(
        peaks,
        selected_substrate,
        substrate_profiles,
    )
    keep_feedback = [
        feedback
        for feedback in substrate_feedback
        if feedback.get("substrate") == selected_substrate
        and feedback.get("action") == "keep"
    ]
    remove_feedback = [
        feedback
        for feedback in substrate_feedback
        if feedback.get("substrate") == selected_substrate
        and feedback.get("action") == "remove"
    ]
    for feedback in remove_feedback:
        center = float(feedback["center_cm_1"])
        half_width = float(feedback["half_width_cm_1"])
        indices = [
            index
            for index, value in enumerate(x_values)
            if center - half_width <= value <= center + half_width
        ]
        if not indices:
            continue
        peak_index = max(indices, key=lambda index: intensities[index])
        matches.append(
            {
                "label": f"User-confirmed substrate residual near {center:.1f} cm-1",
                "range_cm-1": [center - half_width, center + half_width],
                "weight": 0.3,
                "evidence_source": "user_confirmed_substrate_residual_feedback",
                "peaks": [
                    {
                        "position_cm-1": float(x_values[peak_index]),
                        "intensity": float(intensities[peak_index]),
                        "prominence": max(0.0, float(intensities[peak_index])),
                        "detection_source": "user_confirmed_substrate_residual_feedback",
                    }
                ],
            }
        )
    protected_overlap_peaks = []
    filtered_matches = []
    measured_material_system = parameters.get("import_metadata", {}).get(
        "material_system"
    )
    for match in matches:
        retained_peaks = []
        for peak in match["peaks"]:
            if any(
                abs(float(peak["position_cm-1"]) - float(feedback["center_cm_1"]))
                <= float(feedback["half_width_cm_1"])
                for feedback in keep_feedback
            ):
                protected_overlap_peaks.append(
                    {
                        "position_cm-1": peak["position_cm-1"],
                        "substrate_band_label": match["label"],
                        "assignments": [],
                        "reason": "User feedback classifies this residual as sample signal.",
                        "feedback_action": "keep",
                    }
                )
                continue
            protected_assignments = [
                assignment
                for assignment in peak.get("assignments", [])
                if material_assignment_protects_substrate_overlap(
                    assignment,
                    measured_material_system,
                )
            ]
            if protected_assignments:
                protected_overlap_peaks.append(
                    {
                        "position_cm-1": peak["position_cm-1"],
                        "substrate_band_label": match["label"],
                        "assignments": protected_assignments,
                        "reason": "Specific material evidence overrides substrate-range overlap.",
                    }
                )
            else:
                retained_peaks.append(peak)
        if retained_peaks:
            filtered_matches.append({**match, "peaks": retained_peaks})
    matches = filtered_matches
    selected_score = sum(match["weight"] for match in matches)
    should_subtract = mode in {"glass", "sio2_si"} or (
        mode == "auto"
        and (
            confirmed_substrate == selected_substrate
            or selected_score >= parameters["substrate_auto_minimum_score"]
        )
    )
    report = {
        "status": "detected" if matches else "not_detected",
        "requested_mode": mode,
        "detected_substrate": detection["substrate"],
        "selected_substrate": selected_substrate,
        "confidence": (
            "user_confirmed"
            if confirmed_substrate == selected_substrate
            or feedback_substrate == selected_substrate
            else detection["confidence"]
        ),
        "score": selected_score,
        "matched_bands": [
            {
                "label": match["label"],
                "reference_range_cm-1": match["range_cm-1"],
                "observed_peaks_cm-1": [
                    peak["position_cm-1"] for peak in match["peaks"]
                ],
                "evidence_source": match["evidence_source"],
            }
            for match in matches
        ],
        "components": [],
        "cross_sample_profile": substrate_profiles.get(selected_substrate),
        "protected_material_overlaps": protected_overlap_peaks,
        "applied_substrate_feedback": substrate_feedback,
        "reference_strategy": reference_strategy,
        "correction": "none",
        "reason": (
            "Detection-only mode does not change the derived spectrum."
            if mode == "detect"
            else "No substrate was subtracted because automatic evidence was below the threshold."
            if mode == "auto" and not should_subtract
            else "No matching substrate bands were detected."
            if not matches
            else "User-selected substrate bands were fitted before subtraction."
        ),
    }
    if not should_subtract:
        return list(intensities), report

    selected_reference_traces = (
        []
        if reference_strategy == "analytical_only"
        else (reference_traces or {}).get(selected_substrate, [])
    )
    current_file_id = parameters.get("import_metadata", {}).get("file_id")
    selected_reference_traces = [
        reference
        for reference in selected_reference_traces
        if reference.get("file_id") != current_file_id
    ]
    ensemble_result = fit_substrate_reference_ensemble(
        x_values,
        intensities,
        peaks,
        selected_substrate,
        selected_reference_traces,
        parameters,
    )
    if ensemble_result is not None:
        pure_substrate_target = (
            confirmed_substrate_reference(parameters.get("import_metadata", {}))
            == selected_substrate
        )
        ensemble_quality = ensemble_result["quality"]
        use_ensemble = (
            pure_substrate_target
            or mode in {"glass", "sio2_si"}
            or ensemble_quality["accepted"]
        )
        report["reference_ensemble_attempt"] = {
            **ensemble_result["reference_ensemble"],
            "quality": ensemble_quality,
            "accepted": use_ensemble,
        }
        if use_ensemble:
            if pure_substrate_target:
                ensemble_result["quality"] = {
                    **ensemble_quality,
                    "leave_one_out_evaluated_before_metadata_constraint": True,
                }
                ensemble_result["fit_values"] = [
                    max(0.0, value) for value in intensities
                ]
                ensemble_result["corrected_values"] = [0.0] * len(intensities)
                ensemble_result["components"] = [
                    {
                        "profile": "confirmed_pure_substrate_trace",
                        "center_cm-1": trace_peak_position(
                            x_values,
                            intensities,
                            500.0,
                            540.0,
                        )
                        or float(x_values[0]),
                        "amplitude": max(intensities, default=0.0),
                        "fwhm_cm-1": None,
                        "area": sum(
                            (max(0.0, intensities[index]) + max(0.0, intensities[index - 1]))
                            * (float(x_values[index]) - float(x_values[index - 1]))
                            / 2
                            for index in range(1, len(x_values))
                        ),
                        "values": [max(0.0, value) for value in intensities],
                        "assignment": {
                            "material_system": (
                                "SiO2 / crystalline Si"
                                if selected_substrate == "sio2_si"
                                else "Glass / SiO2"
                            ),
                            "label": "User-confirmed pure-substrate measurement",
                            "role": "substrate",
                            "confidence": "user_confirmed",
                        },
                    }
                ]
            report.update(
                {
                    "status": "subtracted",
                    "correction": "measured_reference_ensemble",
                    "fit_profile": "measured_reference_trace_mixture",
                    "reason": (
                        "Confirmed substrate-only metadata assigns the complete derived trace to the substrate after leave-one-out ensemble evaluation."
                        if pure_substrate_target
                        else "A target-adaptive nonnegative mixture of compatible measured pure-substrate references was fitted and subtracted."
                    ),
                    "components": ensemble_result["components"],
                    "fit_values": ensemble_result["fit_values"],
                    "quality": ensemble_result["quality"],
                    "reference_ensemble": ensemble_result["reference_ensemble"],
                    "protected_material_peaks": ensemble_result[
                        "protected_material_peaks"
                    ],
                    "review_required": not ensemble_quality["accepted"],
                    "substrate_only_validation": {
                        "applied": pure_substrate_target,
                        "final_zero_residual_max_abs": max(
                            map(abs, ensemble_result["corrected_values"]),
                            default=0.0,
                        ),
                        "final_zero_residual_passed": all(
                            value == 0.0
                            for value in ensemble_result["corrected_values"]
                        ),
                    },
                }
            )
            return ensemble_result["corrected_values"], report

    if reference_strategy == "measured_only":
        report.update(
            {
                "status": "not_subtracted",
                "review_required": True,
                "reason": (
                    "Measured-reference-only correction was requested, but no "
                    "compatible measured ensemble passed the configured checks."
                ),
            }
        )
        return list(intensities), report

    if not matches:
        return list(intensities), report

    substrate_peaks = []
    seen_positions = set()
    material_system = "Glass / SiO2" if selected_substrate == "glass" else "SiO2 / crystalline Si"
    for match in matches:
        for peak in match["peaks"]:
            position = float(peak["position_cm-1"])
            if position in seen_positions:
                continue
            seen_positions.add(position)
            substrate_peaks.append(
                {
                    **peak,
                    "assignments": [
                        {
                            "material_system": material_system,
                            "label": match["label"],
                            "confidence": (
                                "user_selected"
                                if mode in {"glass", "sio2_si"}
                                else detection["confidence"]
                            ),
                            "role": "substrate",
                        }
                    ],
                }
            )
    fit_profile = "gaussian" if selected_substrate == "glass" else "pseudo_voigt"
    fitted_substrate = deconvolve_peaks(
        x_values,
        intensities,
        sorted(substrate_peaks, key=lambda peak: peak["position_cm-1"]),
        fit_profile,
        parameters,
    )
    if fitted_substrate["status"] != "fitted":
        report["reason"] = "Detected substrate bands could not be fitted safely."
        return list(intensities), report

    first_pass_residual = [
        observed - fitted
        for observed, fitted in zip(
            intensities,
            fitted_substrate["fit_values"],
        )
    ]
    residual_candidates = broad_substrate_peak_candidates(
        x_values,
        first_pass_residual,
        selected_substrate,
        parameters,
        substrate_profiles,
        "broad_fit_residual_search",
    )
    residual_substrate_peaks = []
    for candidate in residual_candidates:
        position = float(candidate["position_cm-1"])
        if any(
            abs(float(peak["position_cm-1"]) - position)
            < float(parameters["minimum_peak_distance_cm-1"])
            for peak in substrate_peaks
        ):
            continue
        half_width = max(
            float(candidate.get("estimated_fwhm_cm-1") or 0.0) / 2,
            float(parameters["minimum_peak_distance_cm-1"]),
        )
        overlaps_material = any(
            abs(float(peak["position_cm-1"]) - position) <= half_width
            and any(
                material_assignment_protects_substrate_overlap(
                    assignment,
                    parameters.get("import_metadata", {}).get(
                        "material_system"
                    ),
                )
                for assignment in peak.get("assignments", [])
            )
            for peak in peaks
        )
        if overlaps_material:
            continue
        matching_band = next(
            (
                band
                for band in (
                    *SUBSTRATE_BANDS[selected_substrate],
                    *substrate_profiles.get(selected_substrate, {}).get(
                        "learned_bands", []
                    ),
                )
                if band["range_cm-1"][0]
                <= position
                <= band["range_cm-1"][1]
                and band["label"] == candidate["substrate_band_label"]
            ),
            None,
        )
        if matching_band is None:
            continue
        existing_match = next(
            (
                match
                for match in matches
                if match["label"] == matching_band["label"]
                and match["range_cm-1"] == matching_band["range_cm-1"]
            ),
            None,
        )
        if existing_match is not None and not matching_band.get(
            "allow_multiple", False
        ):
            continue
        substrate_peak = {
            **candidate,
            "assignments": [
                {
                    "material_system": material_system,
                    "label": matching_band["label"],
                    "confidence": "residual_supported",
                    "role": "substrate",
                }
            ],
        }
        substrate_peaks.append(substrate_peak)
        residual_substrate_peaks.append(substrate_peak)
        if existing_match is None:
            matches.append(
                {
                    "label": matching_band["label"],
                    "range_cm-1": list(matching_band["range_cm-1"]),
                    "weight": matching_band["weight"],
                    "peaks": [substrate_peak],
                    "evidence_source": matching_band.get(
                        "evidence_source", "curated"
                    ),
                }
            )
        else:
            existing_match["peaks"].append(substrate_peak)

    if residual_substrate_peaks:
        refined_substrate = deconvolve_peaks(
            x_values,
            intensities,
            sorted(substrate_peaks, key=lambda peak: peak["position_cm-1"]),
            fit_profile,
            parameters,
        )
        if refined_substrate["status"] == "fitted":
            fitted_substrate = refined_substrate
    feedback_protected_ranges = []
    for feedback in keep_feedback:
        center = float(feedback["center_cm_1"])
        half_width = float(feedback["half_width_cm_1"])
        indices = [
            index
            for index, value in enumerate(x_values)
            if center - half_width <= value <= center + half_width
        ]
        if not indices or indices[0] == 0 or indices[-1] >= len(x_values) - 1:
            continue
        left = indices[0] - 1
        right = indices[-1] + 1
        span = float(x_values[right]) - float(x_values[left])
        for component in fitted_substrate["components"]:
            values = component["values"]
            for index in indices:
                fraction = (
                    (float(x_values[index]) - float(x_values[left])) / span
                    if span
                    else 0.0
                )
                values[index] = values[left] + fraction * (
                    values[right] - values[left]
                )
        fitted_substrate["fit_values"] = [
            sum(component["values"][index] for component in fitted_substrate["components"])
            for index in range(len(x_values))
        ]
        feedback_protected_ranges.append([center - half_width, center + half_width])
    if feedback_protected_ranges:
        report["feedback_protected_ranges_cm-1"] = feedback_protected_ranges
    report["residual_broad_peak_search"] = {
        "method": "width_gated_broad_peak_search_on_first_pass_fit_residual",
        "minimum_fwhm_cm-1": parameters[
            "substrate_broad_minimum_fwhm_cm-1"
        ],
        "candidate_count": len(residual_candidates),
        "assigned_count": len(residual_substrate_peaks),
        "assigned_peaks": [
            {
                key: value
                for key, value in peak.items()
                if key != "assignments"
            }
            | {"assignment": peak["assignments"][0]}
            for peak in residual_substrate_peaks
        ],
    }
    report["matched_bands"] = [
        {
            "label": match["label"],
            "reference_range_cm-1": match["range_cm-1"],
            "observed_peaks_cm-1": [
                peak["position_cm-1"] for peak in match["peaks"]
            ],
            "detection_methods": sorted(
                {
                    peak.get("detection_source", "standard_peak_detection")
                    for peak in match["peaks"]
                }
            ),
            "evidence_source": match["evidence_source"],
        }
        for match in matches
    ]

    band_indices = [
        index
        for index, x_value in enumerate(x_values)
        if any(
            match["range_cm-1"][0] <= x_value <= match["range_cm-1"][1]
            for match in matches
        )
    ]
    observed_in_bands = [intensities[index] for index in band_indices]
    fitted_in_bands = [
        fitted_substrate["fit_values"][index] for index in band_indices
    ]
    local_residual_sum_squares = sum(
        (observed - fitted) ** 2
        for observed, fitted in zip(observed_in_bands, fitted_in_bands)
    )
    local_mean = sum(observed_in_bands) / len(observed_in_bands)
    local_total_sum_squares = sum(
        (observed - local_mean) ** 2 for observed in observed_in_bands
    )
    local_rmse = math.sqrt(local_residual_sum_squares / len(band_indices))
    local_range = max(observed_in_bands) - min(observed_in_bands)
    local_quality = {
        "rmse": local_rmse,
        "normalized_rmse": local_rmse / local_range if local_range > 0 else None,
        "r_squared": (
            1 - local_residual_sum_squares / local_total_sum_squares
            if local_total_sum_squares > 0
            else None
        ),
        "evaluated_point_count": len(band_indices),
    }
    if (
        mode == "auto"
        and (
            local_quality["r_squared"] is None
            or local_quality["r_squared"]
            < parameters["substrate_auto_minimum_fit_r_squared"]
        )
    ):
        report.update(
            {
                "quality": local_quality,
                "review_required": True,
                "reason": (
                    "Automatic subtraction was withheld because the fitted "
                    "substrate did not explain the candidate bands reliably."
                ),
            }
        )
        return list(intensities), report

    component_corrected = [
        intensity - substrate_value
        for intensity, substrate_value in zip(
            intensities,
            fitted_substrate["fit_values"],
        )
    ]
    protected_material_peaks = []
    for peak in peaks:
        position = float(peak["position_cm-1"])
        if not any(
            lower <= position <= upper
            for lower, upper in (
                match["range_cm-1"] for match in matches
            )
        ):
            continue
        material_assignments = [
            assignment
            for assignment in peak.get("assignments", [])
            if material_assignment_protects_substrate_overlap(
                assignment,
                parameters.get("import_metadata", {}).get("material_system"),
            )
        ]
        if not material_assignments:
            continue
        fwhm = estimate_peak_fwhm(
            x_values,
            intensities,
            peak,
            parameters,
        )
        half_width = max(
            float(parameters["minimum_peak_distance_cm-1"]),
            min(float(fwhm), 75.0),
        )
        protected_material_peaks.append(
            {
                "position_cm-1": position,
                "protected_range_cm-1": [
                    position - half_width,
                    position + half_width,
                ],
                "assignments": material_assignments,
            }
        )
    corrected, smoothed_ranges = smooth_substrate_residual_intervals(
        x_values,
        component_corrected,
        [match["range_cm-1"] for match in matches],
        [
            peak["protected_range_cm-1"]
            for peak in protected_material_peaks
        ],
    )
    total_substrate_effect = [
        intensity - corrected_value
        for intensity, corrected_value in zip(intensities, corrected)
    ]
    report.update(
        {
            "status": "subtracted",
            "correction": "fitted_components_and_residual_interpolation",
            "fit_profile": fit_profile,
            "reason": (
                "The explicitly selected substrate bands were fitted, subtracted, and their substrate-only residual intervals were interpolated."
                if mode in {"glass", "sio2_si"}
                else "High-confidence substrate bands were fitted and substrate-only residual intervals were interpolated automatically."
            ),
            "components": fitted_substrate["components"],
            "fit_values": total_substrate_effect,
            "quality": local_quality,
            "residual_cleanup_method": "piecewise_linear_interpolation",
            "smoothed_substrate_ranges_cm-1": smoothed_ranges,
            "protected_material_peaks": protected_material_peaks,
            "review_required": (
                local_quality["r_squared"] is None
                or local_quality["r_squared"]
                < parameters["substrate_auto_minimum_fit_r_squared"]
            ),
        }
    )
    return corrected, report


def smooth_substrate_residual_intervals(
    x_values: list[float],
    values: list[float],
    substrate_ranges: list[list[float]],
    protected_ranges: list[list[float]],
) -> tuple[list[float], list[list[float]]]:
    """Interpolate substrate-only residuals while retaining material peaks."""
    corrected = list(values)
    smoothed_ranges = []
    for lower, upper in substrate_ranges:
        replace = [
            lower <= x_value <= upper
            and not any(
                protected_lower <= x_value <= protected_upper
                for protected_lower, protected_upper in protected_ranges
            )
            for x_value in x_values
        ]
        index = 0
        while index < len(replace):
            if not replace[index]:
                index += 1
                continue
            start = index
            while index + 1 < len(replace) and replace[index + 1]:
                index += 1
            end = index
            left = start - 1
            right = end + 1
            if left >= 0 and right < len(corrected):
                x_left = float(x_values[left])
                x_right = float(x_values[right])
                y_left = float(corrected[left])
                y_right = float(corrected[right])
                span = x_right - x_left
                for target in range(start, end + 1):
                    fraction = (float(x_values[target]) - x_left) / span
                    corrected[target] = y_left + fraction * (y_right - y_left)
                smoothed_ranges.append(
                    [float(x_values[start]), float(x_values[end])]
                )
            index += 1
    return corrected, smoothed_ranges


def enforce_substrate_nonnegative_constraint(
    x_values: list[float],
    baseline_corrected: list[float],
    substrate_corrected: list[float],
    substrate_result: dict,
) -> tuple[list[float], dict]:
    """Floor substrate-corrected signal while retaining the signed input."""
    if substrate_result.get("status") != "subtracted":
        return substrate_corrected, substrate_result

    constrained = list(substrate_corrected)
    affected_indices = []
    for index, (before, after) in enumerate(
        zip(baseline_corrected, substrate_corrected)
    ):
        # Substrate-corrected and later analytical graphs are nonnegative.
        # The signed baseline-corrected series remains separately available.
        bounded = min(max(before, 0.0), max(after, 0.0))
        if bounded != after:
            constrained[index] = bounded
            affected_indices.append(index)
    unconstrained_minimum = min(
        (substrate_corrected[index] for index in affected_indices),
        default=None,
    )
    affected_ranges = []
    if affected_indices:
        start = previous = affected_indices[0]
        for index in affected_indices[1:]:
            if index != previous + 1:
                affected_ranges.append([x_values[start], x_values[previous]])
                start = index
            previous = index
        affected_ranges.append([x_values[start], x_values[previous]])

    updated_result = {
        **substrate_result,
        "fit_values": [
            max(0.0, before - after)
            for before, after in zip(baseline_corrected, constrained)
        ],
        "nonnegative_constraint": {
            "rule": "substrate_corrected_intensity_is_floored_at_zero",
            "positive_residual_floor": 0.0,
            "negative_residual_floor": 0.0,
            "signed_baseline_corrected_series_retained": True,
            "affected_point_count": len(affected_indices),
            "affected_ranges_cm-1": affected_ranges,
            "minimum_unconstrained_intensity": unconstrained_minimum,
        },
    }
    return constrained, updated_result


def material_assignment_protects_substrate_overlap(
    assignment: dict,
    measured_material_system: str | None,
) -> bool:
    """Require specific evidence that an overlapping band is not substrate."""
    if assignment.get("role") == "substrate":
        return False
    if assignment.get("confidence") == "user_confirmed":
        return True
    label = normalized_material_name(assignment.get("label") or "")
    if label in {"referenceband", "measuredreferencepeak"}:
        return False
    assigned = normalized_material_name(assignment.get("material_system") or "")
    if not assigned or assigned in {
        "glass",
        "silica",
        "sio2",
        "sio2si",
        "crystallinesilicon",
        "silicondioxide",
    }:
        return False
    measured = normalized_material_name(measured_material_system or "")
    material_families = (
        "graphene",
        "graphite",
        "carbonnanotube",
        "cnt",
        "mos2",
        "vo2",
        "cs2znbr4",
    )
    if any(family in assigned and family in measured for family in material_families):
        return True
    substrate_components = {
        "glass",
        "silica",
        "sio2",
        "sio2si",
        "crystallinesilicon",
        "silicondioxide",
    }
    components = declared_material_components(measured_material_system)
    non_substrate_components = components - substrate_components
    return any(
        component in assigned or assigned in component
        for component in non_substrate_components
    )


def confirmed_substrate_reference(metadata: dict) -> str | None:
    """Recognize a measurement containing only a confirmed substrate stack."""
    substrate = metadata.get("substrate")
    if substrate not in {"glass", "sio2_si"}:
        return None
    measurement_role = metadata.get("measurement_role", "unspecified")
    if measurement_role == "pure_substrate_reference":
        return substrate
    if measurement_role in {"sample", "sample_on_substrate"}:
        return None
    components = declared_material_components(metadata.get("material_system"))
    if not components:
        return None
    allowed = (
        {"glass", "silica", "sio2", "silicondioxide"}
        if substrate == "glass"
        else {
            "silica",
            "sio2",
            "sio2si",
            "silicondioxide",
            "si",
            "silicon",
            "crystallinesi",
            "crystallinesilicon",
        }
    )
    return substrate if components.issubset(allowed) else None


def user_defined_substrate_only_ranges(
    metadata: dict,
    parameters: dict,
) -> list[list[float]]:
    """Return explicitly confirmed substrate-only intervals for named series."""
    if metadata.get("substrate") != "sio2_si":
        return []
    identity = normalized_material_name(
        f"{metadata.get('sample_id') or ''} {metadata.get('original_filename') or ''}"
    )
    if not (
        identity.startswith("t12")
        or "sio2ref" in identity
        or "sioref" in identity
    ):
        return []
    return [
        [float(lower), float(upper)]
        for lower, upper in parameters["t12_sio2_substrate_only_ranges_cm-1"]
    ]


def apply_substrate_only_ranges(
    x_values: list[float],
    substrate_corrected: list[float],
    substrate_result: dict,
    initial_peaks: list[dict],
    metadata: dict,
    parameters: dict,
) -> tuple[list[float], dict]:
    """Remove user-confirmed substrate-only signal before downstream analysis."""
    ranges = user_defined_substrate_only_ranges(metadata, parameters)
    if not ranges or parameters["substrate_correction_mode"] in {"none", "detect"}:
        return substrate_corrected, substrate_result
    corrected = list(substrate_corrected)
    affected_indices = []
    for index, x_value in enumerate(x_values):
        if any(lower <= x_value <= upper for lower, upper in ranges):
            corrected[index] = 0.0
            affected_indices.append(index)
    excluded_peaks = []
    for peak in initial_peaks:
        position = float(peak["position_cm-1"])
        if not any(lower <= position <= upper for lower, upper in ranges):
            continue
        excluded_peaks.append(
            {
                **peak,
                "assignments": [
                    {
                        "material_system": "SiO2 / crystalline Si",
                        "label": "User-confirmed substrate-only interval 500–835 cm-1",
                        "confidence": "user_confirmed_range",
                        "role": "substrate",
                    }
                ],
                "exclusion_reason": (
                    "User assigned all peaks and background from 500 to 835 cm-1 "
                    "in the T-12 / SiO2_REF series to the substrate."
                ),
            }
        )
    updated = {
        **substrate_result,
        "status": "subtracted",
        "selected_substrate": "sio2_si",
        "correction": "user_confirmed_substrate_only_interval_mask",
        "substrate_only_ranges_cm-1": ranges,
        "interval_mask_value": 0.0,
        "interval_masked_point_count": len(affected_indices),
        "interval_excluded_peaks": excluded_peaks,
        "interval_excluded_peak_count": len(excluded_peaks),
        "reason": (
            "All signal from 500 to 835 cm-1 is user-confirmed SiO2 / "
            "crystalline Si substrate and is excluded before downstream analysis."
        ),
    }
    fit_values = updated.get("fit_values")
    if isinstance(fit_values, list) and len(fit_values) == len(corrected):
        affected_index_set = set(affected_indices)
        updated["fit_values"] = [
            original
            + (
                substrate_corrected[index]
                if index in affected_index_set
                else 0.0
            )
            for index, original in enumerate(fit_values)
        ]
    return corrected, updated


def mask_values_in_ranges(
    x_values: list[float],
    values: list[float],
    ranges: list[list[float]],
) -> list[float]:
    return [
        0.0
        if any(lower <= x_value <= upper for lower, upper in ranges)
        else value
        for x_value, value in zip(x_values, values)
    ]


def label_confirmed_substrate_reference_peaks(
    peaks: list[dict],
    metadata: dict,
    source_sha256: str,
) -> list[dict]:
    substrate = confirmed_substrate_reference(metadata)
    if substrate is None:
        return peaks
    material_system = (
        "Glass / SiO2" if substrate == "glass" else "SiO2 / crystalline Si"
    )
    assignment = {
        "material_system": material_system,
        "label": "Measured substrate-reference peak",
        "confidence": "user_provided_reference",
        "role": "substrate",
        "reference": {
            "reference_id": f"substrate-reference-{metadata['file_id']}",
            "title": metadata.get("sample_id") or metadata["original_filename"],
            "citation": "User-provided pure substrate reference measurement.",
            "source_url": None,
            "sha256": source_sha256,
            "provider": "user_substrate_reference",
        },
    }
    return [
        {**peak, "assignments": [assignment, *peak.get("assignments", [])]}
        for peak in peaks
    ]


def noise_gated_substrate_reference_peaks(
    peaks: list[dict],
    metadata: dict,
    parameters: dict,
) -> list[dict]:
    """Suppress reference-guided noise maxima not present in the raw control."""
    substrate = confirmed_substrate_reference(metadata)
    if substrate is None:
        return peaks
    profile = (parameters.get("substrate_profiles") or {}).get(substrate, {})
    source = next(
        (
            item
            for item in profile.get("reference_spectra", [])
            if item.get("file_id") == metadata.get("file_id")
        ),
        None,
    )
    if source is None:
        return peaks
    accepted_positions = [
        float(item["position_cm-1"]) for item in source.get("peaks", [])
    ]
    accepted_positions.extend(
        float(item["observed_cm-1"])
        for item in parameters.get("confirmed_peak_assignments") or []
        if item.get("observed_cm-1") is not None
    )
    tolerance = min(
        4.0,
        float(parameters["minimum_peak_distance_cm-1"]) / 2,
    )
    return [
        peak
        for peak in peaks
        if any(
            abs(float(peak["position_cm-1"]) - position) <= tolerance
            for position in accepted_positions
        )
    ]


def is_unopposed_substrate_peak(
    peak: dict,
    substrate: str | None,
    measured_material_system: str | None,
    parameters: dict,
) -> bool:
    """Classify a peak near a substrate band unless material evidence opposes it."""
    if substrate not in SUBSTRATE_BANDS:
        return False
    position = float(peak["position_cm-1"])
    tolerance = float(parameters["substrate_residual_band_tolerance_cm-1"])
    bands = (
        *SUBSTRATE_BANDS[substrate],
        *(parameters.get("substrate_profiles") or {})
        .get(substrate, {})
        .get("learned_bands", []),
    )
    within_substrate_band = any(
        float(band["range_cm-1"][0]) - tolerance
        <= position
        <= float(band["range_cm-1"][1]) + tolerance
        for band in bands
    )
    if not within_substrate_band:
        return False
    return not any(
        material_assignment_protects_substrate_overlap(
            assignment,
            measured_material_system,
        )
        for assignment in peak.get("assignments", [])
    )


def reference_library_snapshot(reference_sources: list[dict]) -> str:
    identity = [
        {
            "reference_id": source["reference_id"],
            "sha256": source["sha256"],
            "material_system": source["material_system"],
            "extraction_status": source["extraction_status"],
        }
        for source in sorted(
            reference_sources,
            key=lambda item: item["reference_id"],
        )
    ]
    return hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()


def matched_reference_peaks(
    observed_peaks: list[dict],
    reference_peaks: list[dict],
    tolerance: float,
) -> list[dict]:
    available = set(range(len(observed_peaks)))
    matches = []
    for reference_peak in reference_peaks:
        if reference_peak.get("position_range_cm-1") is not None:
            lower_bound, upper_bound = [
                float(value)
                for value in reference_peak["position_range_cm-1"]
            ]
            reference_position = (lower_bound + upper_bound) / 2
        else:
            lower_bound = upper_bound = float(reference_peak["position_cm-1"])
            reference_position = lower_bound
        candidates = [
            (
                (
                    lower_bound - float(observed_peaks[index]["position_cm-1"])
                    if float(observed_peaks[index]["position_cm-1"])
                    < lower_bound
                    else float(observed_peaks[index]["position_cm-1"])
                    - upper_bound
                    if float(observed_peaks[index]["position_cm-1"])
                    > upper_bound
                    else 0.0
                ),
                index,
            )
            for index in available
        ]
        if not candidates:
            break
        difference, index = min(candidates)
        if difference > tolerance:
            continue
        available.remove(index)
        matches.append(
            {
                "observed_cm-1": observed_peaks[index]["position_cm-1"],
                "reference_cm-1": reference_position,
                "difference_cm-1": difference,
                **(
                    {"page": reference_peak["page"]}
                    if reference_peak.get("page") is not None
                    else {}
                ),
            }
        )
    return matches


def identify_material_system(
    observed_peaks: list[dict],
    reference_sources: list[dict],
    parameters: dict,
    declared_material_system: str | None = None,
    material_confirmation: dict | None = None,
) -> dict:
    candidates_by_material = {}
    for source in reference_sources:
        if source["extraction_status"] != "ready":
            continue
        try:
            evidence = json.loads(source["evidence_json"])
            reference_peaks = evidence["peaks"]
        except (KeyError, TypeError, json.JSONDecodeError):
            continue
        if evidence.get("training_scope") == "peak_assignment_only":
            continue
        if not reference_peaks or not observed_peaks:
            continue
        matches = matched_reference_peaks(
            observed_peaks,
            reference_peaks,
            float(
                evidence.get(
                    "match_tolerance_cm-1",
                    parameters["reference_match_tolerance_cm-1"],
                )
            ),
        )
        recall = len(matches) / len(reference_peaks)
        precision = len(matches) / len(observed_peaks)
        if not matches:
            continue
        score = 0.7 * recall + 0.3 * precision
        if len(matches) < 2:
            score = min(score, 0.49)
        candidate = {
            "material_system": source["material_system"],
            "score": score,
            "matched_peak_count": len(matches),
            "reference_peak_count": len(reference_peaks),
            "observed_peak_count": len(observed_peaks),
            "matched_peaks": matches,
            "reference": {
                "reference_id": source["reference_id"],
                "title": source["title"],
                "citation": source["citation"],
                "source_url": source["source_url"],
                "sha256": source["sha256"],
                "provider": evidence.get("provider", "user_reference"),
                "license": evidence.get("license"),
            },
        }
        key = source["material_system"].casefold()
        previous = candidates_by_material.get(key)
        if previous is None or candidate["score"] > previous["score"]:
            candidates_by_material[key] = candidate

    candidates = sorted(
        candidates_by_material.values(),
        key=lambda item: item["score"],
        reverse=True,
    )[:5]
    if material_confirmation is not None:
        confirmed = material_confirmation["material_system"]
        return {
            "status": "confirmed",
            "material_system": confirmed,
            "confirmed_material_system": confirmed,
            "confidence": "user_confirmed",
            "reason": (
                "The local user confirmed this material system; reference "
                "candidates remain available as supporting evidence."
            ),
            "confirmation": {
                "training_id": material_confirmation["training_id"],
                "confirmed_at": material_confirmation["confirmed_at"],
                "confirmed_by": material_confirmation["confirmed_by"],
                "source_sha256": material_confirmation["source_sha256"],
                "result_sha256": material_confirmation["result_sha256"],
            },
            "candidates": candidates,
        }
    declared = (declared_material_system or "").strip()
    if declared and declared.casefold() not in {
        "unknown",
        "unknown system",
        "unspecified",
    }:
        assessment = "no_reference_evidence"
        reason = "The material system was supplied with the measurement."
        if candidates:
            top = candidates[0]
            if top["score"] >= parameters["minimum_identification_score"]:
                if top["material_system"].casefold() == declared.casefold():
                    assessment = "supported"
                    reason = (
                        "The user-declared material system is supported by "
                        "the best reference match."
                    )
                else:
                    assessment = "conflicting_candidate"
                    reason = (
                        "The best reference match differs from the "
                        "user-declared material system; review both labels."
                    )
        return {
            "status": "declared",
            "material_system": declared,
            "declared_material_system": declared,
            "reference_assessment": assessment,
            "confidence": "user_provided",
            "reason": reason,
            "candidates": candidates,
        }
    if not candidates:
        return {
            "status": "unknown",
            "material_system": None,
            "confidence": "none",
            "reason": "No usable reference evidence matched this spectrum.",
            "candidates": [],
        }

    top = candidates[0]
    runner_up_score = candidates[1]["score"] if len(candidates) > 1 else 0.0
    minimum_score = parameters["minimum_identification_score"]
    minimum_margin = parameters["minimum_candidate_margin"]
    if top["score"] < minimum_score:
        status = "unknown"
        material_system = None
        confidence = "low"
        reason = "The best reference match is below the acceptance threshold."
    elif top["score"] - runner_up_score < minimum_margin:
        status = "ambiguous"
        material_system = None
        confidence = "low"
        reason = "Multiple material systems have similarly plausible matches."
    else:
        status = "identified"
        material_system = top["material_system"]
        confidence = "high" if top["score"] >= 0.8 else "moderate"
        reason = "The candidate passed the score and separation thresholds."
    return {
        "status": status,
        "material_system": material_system,
        "confidence": confidence,
        "reason": reason,
        "candidates": candidates,
    }


def normalized_material_name(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def declared_material_components(value: str | None) -> set[str]:
    if not value:
        return set()
    components = set()
    current = []
    for character in value.casefold().replace(" on ", "/"):
        if character in "/,+;":
            component = normalized_material_name("".join(current))
            if component:
                components.add(component)
            current = []
        else:
            current.append(character)
    component = normalized_material_name("".join(current))
    if component:
        components.add(component)
    return components - {"unknown", "unknownsystem", "unspecified"}


def reference_applies_to_material(
    reference_material: str,
    components: set[str],
) -> bool:
    normalized_reference = normalized_material_name(reference_material)
    reference_components = declared_material_components(reference_material)
    if len(reference_components) >= 2:
        measured_components = set(components)
        if "sio2" in measured_components:
            measured_components.discard("si")
        if "sio2" in reference_components:
            reference_components.discard("si")
        return all(
            any(
                reference_component == measured_component
                or (
                    len(reference_component) >= 4
                    and len(measured_component) >= 4
                    and (
                        reference_component in measured_component
                        or measured_component in reference_component
                    )
                )
                for measured_component in measured_components
            )
            or (
                "silicon" in reference_component
                and "sio2" in measured_components
            )
            for reference_component in reference_components
        )
    if any(
        component == normalized_reference
        or (
            len(component) >= 4
            and len(normalized_reference) >= 4
            and (
                component in normalized_reference
                or normalized_reference in component
            )
        )
        for component in components
    ):
        return True
    component_text = "".join(components)
    shared_families = (
        "graphene",
        "carbonnanotube",
        "mos2",
        "vo2",
        "cs2znbr4",
        "sio2",
    )
    if any(
        family in component_text and family in normalized_reference
        for family in shared_families
    ):
        return True
    if "si" in components and "silicon" in normalized_reference:
        return True
    # A measured SiO2 layer commonly includes a crystalline Si substrate signal.
    return "sio2" in component_text and normalized_reference == "crystallinesilicon"


def assign_peak_labels(
    peaks: list[dict],
    reference_sources: list[dict],
    declared_material_system: str | None,
    identification: dict,
    parameters: dict,
) -> list[dict]:
    """Attach reference-backed assignments without changing detected positions."""
    components = declared_material_components(declared_material_system)
    identified_material = identification.get("material_system")
    if not components and identified_material:
        components = {normalized_material_name(identified_material)}

    labelled_peaks = [{**peak, "assignments": []} for peak in peaks]
    if not components:
        return labelled_peaks
    confirmed_material = (
        identification.get("confirmed_material_system")
        if identification.get("status") == "confirmed"
        else None
    )

    for source in reference_sources:
        if source.get("extraction_status") != "ready" or not reference_applies_to_material(
            source.get("material_system") or "",
            components,
        ):
            continue
        try:
            evidence = json.loads(source["evidence_json"])
            reference_peaks = evidence["peaks"]
        except (KeyError, TypeError, json.JSONDecodeError):
            continue
        tolerance = float(
            evidence.get(
                "match_tolerance_cm-1",
                parameters["reference_match_tolerance_cm-1"],
            )
        )
        for peak in labelled_peaks:
            observed = float(peak["position_cm-1"])
            for reference_peak in reference_peaks:
                excluded_components = {
                    normalized_material_name(value)
                    for value in reference_peak.get(
                        "excluded_if_material_components", []
                    )
                }
                if components & excluded_components:
                    continue
                if reference_peak.get("position_range_cm-1") is not None:
                    lower, upper = map(
                        float,
                        reference_peak["position_range_cm-1"],
                    )
                    center = (lower + upper) / 2
                    difference = (
                        lower - observed
                        if observed < lower
                        else observed - upper
                        if observed > upper
                        else 0.0
                    )
                    range_width = upper - lower
                    reference_position = None
                else:
                    center = float(reference_peak["position_cm-1"])
                    lower = upper = center
                    difference = abs(observed - center)
                    range_width = 0.0
                    reference_position = center
                if difference > tolerance:
                    continue
                assigned_material = reference_peak.get(
                    "material_system",
                    source["material_system"],
                )
                if (
                    confirmed_material
                    and "graphene" in normalized_material_name(confirmed_material)
                    and "graphene" in normalized_material_name(
                        source["material_system"]
                    )
                    and reference_peak.get("material_system") is None
                ):
                    assigned_material = confirmed_material
                assignment = {
                    "material_system": assigned_material,
                    "label": reference_peak.get("assignment", "Reference band"),
                    "difference_cm-1": difference,
                    "confidence": "high" if difference == 0 else "moderate",
                    "reference": {
                        "reference_id": source["reference_id"],
                        "title": source["title"],
                        "citation": source["citation"],
                        "source_url": source["source_url"],
                        "sha256": source["sha256"],
                        "provider": evidence.get("provider", "user_reference"),
                    },
                    "_sort": [
                        difference,
                        1
                        if normalized_material_name(
                            reference_peak.get("assignment", "Reference band")
                        )
                        in {"referenceband", "measuredreferencepeak"}
                        else 0,
                        range_width,
                        abs(observed - center),
                    ],
                }
                if reference_position is None:
                    assignment["reference_range_cm-1"] = [lower, upper]
                else:
                    assignment["reference_cm-1"] = reference_position
                if reference_peak.get("role"):
                    assignment["role"] = reference_peak["role"]
                if reference_peak.get("provisional"):
                    assignment["provisional"] = True
                    assignment["confidence"] = "provisional"
                if reference_peak.get("possible_origins"):
                    assignment["possible_origins"] = reference_peak[
                        "possible_origins"
                    ]
                if (
                    normalized_material_name(assigned_material)
                    == "crystallinesilicon"
                    and "sio2" in components
                ):
                    assignment["role"] = "substrate"
                peak["assignments"].append(assignment)

    for peak in labelled_peaks:
        peak["assignments"].sort(key=lambda item: item["_sort"])
        unique_assignments = []
        seen_assignments = set()
        for assignment in peak["assignments"]:
            identity = (
                normalized_material_name(assignment["material_system"]),
                normalized_material_name(assignment["label"]),
            )
            if identity in seen_assignments:
                continue
            seen_assignments.add(identity)
            assignment.pop("_sort")
            unique_assignments.append(assignment)
            if len(unique_assignments) == 5:
                break
        peak["assignments"] = unique_assignments
    return labelled_peaks


def reference_candidates_for_unassigned_peak(
    peak: dict,
    reference_sources: list[dict],
    parameters: dict,
) -> list[dict]:
    observed = float(peak["position_cm-1"])
    candidates = []
    seen = set()
    for source in reference_sources:
        if source.get("extraction_status") != "ready":
            continue
        try:
            evidence = json.loads(source["evidence_json"])
            reference_peaks = evidence["peaks"]
        except (KeyError, TypeError, json.JSONDecodeError):
            continue
        tolerance = float(
            evidence.get(
                "match_tolerance_cm-1",
                parameters["reference_match_tolerance_cm-1"],
            )
        )
        for reference_peak in reference_peaks:
            if reference_peak.get("position_range_cm-1") is not None:
                lower, upper = map(
                    float,
                    reference_peak["position_range_cm-1"],
                )
                center = (lower + upper) / 2
                difference = (
                    lower - observed
                    if observed < lower
                    else observed - upper
                    if observed > upper
                    else 0.0
                )
                reference_position = None
                reference_range = [lower, upper]
            else:
                center = float(reference_peak["position_cm-1"])
                difference = abs(observed - center)
                reference_position = center
                reference_range = None
            if difference > tolerance:
                continue
            material_system = reference_peak.get(
                "material_system",
                source.get("material_system") or "Unknown",
            )
            label = reference_peak.get("assignment", "Reference band")
            identity = (
                normalized_material_name(material_system),
                label.casefold(),
                source["reference_id"],
            )
            if identity in seen:
                continue
            seen.add(identity)
            candidate = {
                "material_system": material_system,
                "label": label,
                "observed_cm-1": observed,
                "difference_cm-1": difference,
                "reference": {
                    "reference_id": source["reference_id"],
                    "title": source["title"],
                    "citation": source.get("citation"),
                    "source_url": source.get("source_url"),
                    "sha256": source["sha256"],
                    "provider": evidence.get("provider", "user_reference"),
                    "source_kind": source.get("source_kind"),
                },
                "_sort": [difference, abs(observed - center)],
            }
            if reference_range is not None:
                candidate["reference_range_cm-1"] = reference_range
            else:
                candidate["reference_cm-1"] = reference_position
            candidates.append(candidate)
    candidates.sort(key=lambda item: item["_sort"])
    for candidate in candidates:
        candidate.pop("_sort")
    return candidates[:3]


def apply_confirmed_peak_assignments(
    peaks: list[dict],
    confirmed_assignments: list[dict],
) -> list[dict]:
    for confirmation in confirmed_assignments:
        if is_instrument_artifact_confirmation(confirmation):
            continue
        observed = float(confirmation.get("observed_cm-1", -1))
        target = min(
            peaks,
            key=lambda peak: abs(float(peak["position_cm-1"]) - observed),
            default=None,
        )
        if target is None or abs(float(target["position_cm-1"]) - observed) > 2.0:
            continue
        assignment = {
            key: value
            for key, value in confirmation.items()
            if key not in {"observed_cm-1", "confirmed_at", "clarification_id"}
        }
        assignment["confidence"] = "user_confirmed"
        assignment["confirmation"] = {
            "clarification_id": confirmation.get("clarification_id"),
            "confirmed_at": confirmation.get("confirmed_at"),
        }
        target["assignments"].insert(0, assignment)
    return peaks


def include_user_confirmed_peaks(
    peaks: list[dict],
    confirmed_assignments: list[dict],
    x_values: list[float],
    intensities: list[float],
) -> list[dict]:
    """Promote a user-confirmed spectral feature omitted by generic detection."""
    promoted = [dict(peak) for peak in peaks]
    for confirmation in confirmed_assignments:
        if is_instrument_artifact_confirmation(confirmation):
            continue
        try:
            observed = float(confirmation["observed_cm-1"])
        except (KeyError, TypeError, ValueError):
            continue
        if any(
            abs(float(peak["position_cm-1"]) - observed) <= 2.0
            for peak in promoted
        ):
            continue
        nearby_indices = [
            index
            for index, x_value in enumerate(x_values)
            if abs(float(x_value) - observed) <= 2.0
        ]
        if not nearby_indices:
            continue
        peak_index = max(nearby_indices, key=lambda index: intensities[index])
        left_values = intensities[max(0, peak_index - 12) : peak_index]
        right_values = intensities[peak_index + 1 : peak_index + 13]
        prominence = 0.0
        if left_values and right_values:
            prominence = intensities[peak_index] - max(
                min(left_values),
                min(right_values),
            )
        promoted.append(
            {
                "position_cm-1": x_values[peak_index],
                "intensity": intensities[peak_index],
                "prominence": max(0.0, prominence),
                "detection_source": "user_confirmation",
            }
        )
    return sorted(promoted, key=lambda peak: peak["position_cm-1"])


def exclude_confirmed_noise_peaks(
    peaks: list[dict],
    confirmed_exclusions: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Remove user-confirmed noise candidates from derived peak analysis."""
    retained = list(peaks)
    excluded = []
    for confirmation in confirmed_exclusions:
        try:
            observed = float(confirmation["observed_cm-1"])
        except (KeyError, TypeError, ValueError):
            continue
        target = min(
            retained,
            key=lambda peak: abs(float(peak["position_cm-1"]) - observed),
            default=None,
        )
        if target is None or abs(
            float(target["position_cm-1"]) - observed
        ) > 2.0:
            continue
        retained.remove(target)
        excluded.append(
            {
                "position_cm-1": target["position_cm-1"],
                "intensity": target.get("intensity"),
                "prominence": target.get("prominence"),
                "classification": "noise",
                "confidence": "user_confirmed",
                "reason": "singleton_noise_level_peak_confirmed_by_user",
                "confirmation": {
                    "clarification_id": confirmation.get("clarification_id"),
                    "confirmed_at": confirmation.get("confirmed_at"),
                },
                "evidence": confirmation.get("evidence"),
            }
        )
    return retained, excluded


def exclude_noise_indistinguishable_peaks(
    peaks: list[dict],
    x_values: list[float],
    intensities: list[float],
    parameters: dict,
) -> tuple[list[dict], list[dict]]:
    """Exclude candidates whose local intensity contrast cannot clear noise."""
    if not peaks or len(intensities) < 3:
        return peaks, []
    global_differences = [
        abs(intensities[index] - intensities[index - 1])
        for index in range(1, len(intensities))
    ]
    global_noise = median(global_differences) if global_differences else 0.0
    radius = max(3, int(parameters["noise_local_radius_points"]))
    guard = max(1, int(parameters["noise_peak_guard_points"]))
    minimum_snr = float(parameters["noise_indistinguishable_minimum_snr"])
    retained = []
    excluded = []
    for peak in peaks:
        user_confirmed = any(
            assignment.get("confidence") == "user_confirmed"
            for assignment in peak.get("assignments", [])
        )
        if user_confirmed:
            retained.append(peak)
            continue
        peak_index = min(
            range(len(x_values)),
            key=lambda index: abs(
                float(x_values[index]) - float(peak["position_cm-1"])
            ),
        )
        lower = max(0, peak_index - radius)
        upper = min(len(intensities) - 1, peak_index + radius)
        local_differences = [
            abs(intensities[index] - intensities[index - 1])
            for index in range(lower + 1, upper + 1)
            if not (
                peak_index - guard <= index <= peak_index + guard
                or peak_index - guard <= index - 1 <= peak_index + guard
            )
        ]
        local_noise = (
            median(local_differences) if local_differences else global_noise
        )
        effective_noise = max(global_noise, local_noise)
        prominence = max(0.0, float(peak.get("prominence") or 0.0))
        signal_to_noise = (
            prominence / effective_noise if effective_noise > 0.0 else None
        )
        if signal_to_noise is None or signal_to_noise >= minimum_snr:
            retained.append(peak)
            continue
        excluded.append(
            {
                "position_cm-1": peak["position_cm-1"],
                "intensity": peak.get("intensity"),
                "prominence": prominence,
                "classification": "noise",
                "confidence": "automatic_intensity_contrast",
                "reason": (
                    "peak_prominence_not_distinguishable_from_local_noise"
                ),
                "signal_to_noise": signal_to_noise,
                "minimum_signal_to_noise": minimum_snr,
                "noise_estimate": effective_noise,
                "local_noise_estimate": local_noise,
                "global_noise_estimate": global_noise,
                "detection_source": peak.get("detection_source"),
                "assignments": peak.get("assignments", []),
                "treatment": (
                    "excluded_from_peak_assignment_and_fitting; spectral "
                    "series preserved"
                ),
            }
        )
    return retained, excluded


def unassigned_peaks_for_review(
    peaks: list[dict],
    reference_sources: list[dict],
    parameters: dict,
    analysis_intensities: list[float],
) -> list[dict]:
    if not peaks:
        return []
    unassigned = [
        peak
        for peak in peaks
        if not peak.get("assignments")
    ]
    unassigned.sort(key=lambda peak: float(peak.get("prominence") or 0.0), reverse=True)
    reviewed_peaks = []
    recurring_profiles = parameters.get("recurring_unassigned_peaks") or {}
    recurring_clusters = recurring_profiles.get("clusters") or []
    recurrence_tolerance = float(
        parameters["recurring_unassigned_peak_tolerance_cm-1"]
    )
    minimum_spectra = int(
        parameters["recurring_unassigned_peak_minimum_spectra"]
    )
    measured_material = parameters.get("import_metadata", {}).get(
        "material_system"
    )
    measured_components = declared_material_components(measured_material)
    differences = [
        abs(analysis_intensities[index] - analysis_intensities[index - 1])
        for index in range(1, len(analysis_intensities))
    ]
    noise_estimate = median(differences) if differences else 0.0
    detection_threshold = max(
        max(analysis_intensities, default=0.0)
        * float(parameters["minimum_prominence_fraction"]),
        noise_estimate * float(parameters["noise_multiplier"]),
    )
    comparison_files = recurring_profiles.get("comparison_files") or []
    minimum_same_system_spectra = int(
        parameters["singleton_noise_minimum_same_system_spectra"]
    )
    noise_threshold_multiplier = float(
        parameters["singleton_noise_prominence_threshold_multiplier"]
    )
    for peak in unassigned:
        candidates = reference_candidates_for_unassigned_peak(
            peak,
            reference_sources,
            parameters,
        )
        matching_cluster = min(
            recurring_clusters,
            key=lambda cluster: abs(
                float(cluster["mean_position_cm-1"])
                - float(peak["position_cm-1"])
            ),
            default=None,
        )
        if matching_cluster is not None and abs(
            float(matching_cluster["mean_position_cm-1"])
            - float(peak["position_cm-1"])
        ) <= recurrence_tolerance:
            observed_count = int(matching_cluster["observed_count"]) + 1
        else:
            observed_count = 1
        recurrence = None
        online_search = None
        if observed_count >= minimum_spectra:
            positions = list(matching_cluster["positions_cm-1"])
            positions.append(float(peak["position_cm-1"]))
            recurrence = {
                "status": "recurring_same_material_system",
                "material_system": measured_material,
                "observed_count": observed_count,
                "minimum_spectra": minimum_spectra,
                "mean_position_cm-1": round(sum(positions) / len(positions), 3),
                "range_cm-1": [round(min(positions), 3), round(max(positions), 3)],
                "supporting_file_ids": matching_cluster["file_ids"],
                "current_file_id": parameters.get("import_metadata", {}).get(
                    "file_id"
                ),
                "tolerance_cm-1": recurrence_tolerance,
            }
            online_candidates = [
                candidate
                for candidate in candidates
                if candidate.get("reference", {}).get("provider")
                in {"curated_primary_literature", "Raman Open Database"}
                and normalized_material_name(candidate.get("label") or "")
                not in {"referenceband", "measuredreferencepeak"}
            ]
            online_candidates.sort(
                key=lambda candidate: (
                    0
                    if reference_applies_to_material(
                        candidate["material_system"], measured_components
                    )
                    else 1,
                    0
                    if normalized_material_name(candidate["label"])
                    not in {"referenceband", "measuredreferencepeak"}
                    else 1,
                    0
                    if candidate["reference"]["provider"]
                    == "curated_primary_literature"
                    else 1,
                    float(candidate["difference_cm-1"]),
                )
            )
            if online_candidates:
                best_candidate = online_candidates[0]
                best_candidate["recommended"] = True
                candidates = [best_candidate] + [
                    candidate
                    for candidate in candidates
                    if candidate is not best_candidate
                ]
                online_search = {
                    "status": "candidate_found",
                    "most_probable_assignment": best_candidate,
                    "candidate_count": len(online_candidates),
                    "selection_method": (
                        "same-material recurrence, material compatibility, "
                        "specific mode label, source provenance, and Raman-shift proximity"
                    ),
                    "confirmation_required": True,
                }
            else:
                online_search = {
                    "status": "library_update_required",
                    "candidate_count": 0,
                    "selection_method": (
                        "No close assignment is present in the current online "
                        "reference snapshot."
                    ),
                    "confirmation_required": True,
                }
        observed_position = float(peak["position_cm-1"])
        matching_other_files = [
            comparison_file["file_id"]
            for comparison_file in comparison_files
            if any(
                abs(float(position) - observed_position)
                <= recurrence_tolerance
                for position in comparison_file.get("peak_positions_cm-1", [])
            )
        ]
        prominence = float(peak.get("prominence") or 0.0)
        total_same_system_spectra = len(comparison_files) + 1
        singleton_noise_candidate = None
        if (
            recurrence is None
            and total_same_system_spectra >= minimum_same_system_spectra
            and not matching_other_files
            and detection_threshold > 0.0
            and prominence <= detection_threshold * noise_threshold_multiplier
        ):
            singleton_noise_candidate = {
                "status": "singleton_noise_level_candidate",
                "material_system": measured_material,
                "observed_count": 1,
                "same_system_spectrum_count": total_same_system_spectra,
                "compared_file_ids": [
                    comparison_file["file_id"]
                    for comparison_file in comparison_files
                ],
                "matching_other_file_ids": matching_other_files,
                "position_tolerance_cm-1": recurrence_tolerance,
                "prominence": prominence,
                "noise_estimate": noise_estimate,
                "detection_threshold": detection_threshold,
                "noise_level_upper_prominence": (
                    detection_threshold * noise_threshold_multiplier
                ),
                "threshold_multiplier": noise_threshold_multiplier,
                "confirmation_required": True,
                "effect_on_confirmation": (
                    "exclude_from_derived_peak analysis; preserve all spectral series"
                ),
            }
        reviewed_peak = {
            "position_cm-1": peak["position_cm-1"],
            "prominence": peak.get("prominence"),
            "candidate_assignments": candidates,
        }
        if recurrence is not None:
            reviewed_peak["recurrence"] = recurrence
            reviewed_peak["online_reference_search"] = online_search
        if singleton_noise_candidate is not None:
            reviewed_peak["singleton_noise_candidate"] = (
                singleton_noise_candidate
            )
        reviewed_peaks.append(reviewed_peak)
    reviewed_peaks.sort(
        key=lambda peak: (
            0
            if peak.get("online_reference_search", {}).get("status")
            == "candidate_found"
            else 1
            if peak.get("singleton_noise_candidate")
            else 2,
            -float(peak.get("prominence") or 0.0),
        )
    )
    return reviewed_peaks


def is_carbon_material(material_system: str | None) -> bool:
    if material_system is None:
        return False
    normalized = material_system.casefold()
    return any(
        keyword in normalized
        for keyword in (
            "carbon",
            "graphene",
            "graphite",
            "nanotube",
            "cnt",
        )
    )


def is_mos2_material(material_system: str | None) -> bool:
    return "mos2" in normalized_material_name(material_system or "")


def assignment_matches_band(
    assignment: dict | None,
    aliases: set[str],
) -> bool:
    if not assignment:
        return False
    label = normalized_material_name(str(assignment.get("label") or ""))
    return label in aliases or any(
        len(alias) > 2 and label.startswith(alias) for alias in aliases
    )


def peak_measurement_quality(
    position: float,
    height: float,
    fwhm: float | None,
    x_values: list[float],
    intensities: list[float],
) -> dict:
    peak_index = min(
        range(len(x_values)),
        key=lambda index: abs(float(x_values[index]) - position),
    )
    lower = max(1, peak_index - 20)
    upper = min(len(intensities) - 1, peak_index + 20)
    differences = [
        abs(float(intensities[index]) - float(intensities[index - 1]))
        for index in range(lower, upper + 1)
        if not peak_index - 2 <= index <= peak_index + 2
        and not peak_index - 2 <= index - 1 <= peak_index + 2
    ]
    noise = median(differences) if differences else 0.0
    signal_to_noise = max(0.0, height) / noise if noise > 0.0 else None
    spacings = [
        float(x_values[index]) - float(x_values[index - 1])
        for index in range(1, len(x_values))
        if float(x_values[index]) > float(x_values[index - 1])
    ]
    spacing = median(spacings) if spacings else 0.0
    sampling_uncertainty = spacing / math.sqrt(12) if spacing > 0.0 else 0.0
    fit_uncertainty = (
        float(fwhm) / (2.355 * signal_to_noise)
        if fwhm is not None and signal_to_noise not in {None, 0.0}
        else 0.0
    )
    position_uncertainty = math.sqrt(
        sampling_uncertainty**2 + fit_uncertainty**2
    )
    return {
        "signal_to_noise": signal_to_noise,
        "noise_estimate": noise,
        "position_uncertainty_cm-1": position_uncertainty,
        "position_uncertainty_method": (
            "quadrature_of_grid_quantization_and_fwhm_over_2.355_snr"
        ),
        "includes_calibration_systematics": False,
    }


def material_band_measurement(
    peaks: list[dict],
    components: list[dict],
    aliases: set[str],
    x_values: list[float],
    intensities: list[float],
    window: list[float] | tuple[float, float] | None = None,
) -> dict | None:
    fitted = [
        component
        for component in components
        if assignment_matches_band(component.get("assignment"), aliases)
    ]
    if fitted:
        component = max(
            fitted,
            key=lambda item: float(item.get("amplitude") or 0.0),
        )
        measurement = {
            "position_cm-1": float(component["center_cm-1"]),
            "intensity": float(component.get("amplitude") or 0.0),
            "area": (
                float(component["area"])
                if component.get("area") is not None
                else None
            ),
            "fwhm_cm-1": (
                float(component["fwhm_cm-1"])
                if component.get("fwhm_cm-1") is not None
                else None
            ),
            "measurement_source": "deconvoluted_component",
        }
        return measurement | peak_measurement_quality(
            measurement["position_cm-1"],
            measurement["intensity"],
            measurement["fwhm_cm-1"],
            x_values,
            intensities,
        )

    assigned = [
        peak
        for peak in peaks
        if any(
            assignment_matches_band(assignment, aliases)
            for assignment in peak.get("assignments", [])
        )
    ]
    if assigned:
        peak = max(
            assigned,
            key=lambda item: float(item.get("intensity") or 0.0),
        )
        measurement = {
            "position_cm-1": float(peak["position_cm-1"]),
            "intensity": float(peak.get("intensity") or 0.0),
            "area": None,
            "fwhm_cm-1": (
                peak.get("estimated_fwhm_cm-1")
                or estimate_peak_fwhm(
                    x_values,
                    intensities,
                    peak,
                    MODEL_PARAMETERS,
                )
            ),
            "measurement_source": "assigned_detected_peak",
        }
        return measurement | peak_measurement_quality(
            measurement["position_cm-1"],
            measurement["intensity"],
            measurement["fwhm_cm-1"],
            x_values,
            intensities,
        )

    if window is None:
        return None
    maximum = strongest_band(x_values, intensities, *window)
    if maximum is None:
        return None
    measurement = {
        **maximum,
        "area": None,
        "fwhm_cm-1": estimate_peak_fwhm(
            x_values,
            intensities,
            {
                "position_cm-1": maximum["position_cm-1"],
                "intensity": maximum["intensity"],
            },
            MODEL_PARAMETERS,
        ),
        "measurement_source": "window_maximum",
    }
    return measurement | peak_measurement_quality(
        measurement["position_cm-1"],
        measurement["intensity"],
        measurement["fwhm_cm-1"],
        x_values,
        intensities,
    )


def safe_band_ratio(numerator: dict | None, denominator: dict | None, key: str):
    if numerator is None or denominator is None:
        return None
    denominator_value = denominator.get(key)
    numerator_value = numerator.get(key)
    if denominator_value is None or numerator_value is None:
        return None
    denominator_value = float(denominator_value)
    numerator_value = float(numerator_value)
    if denominator_value <= 0 or numerator_value < 0:
        return None
    return numerator_value / denominator_value


def mos2_layer_analysis(
    peaks: list[dict],
    components: list[dict],
    x_values: list[float],
    intensities: list[float],
    parameters: dict,
) -> dict:
    e2g = material_band_measurement(
        peaks,
        components,
        {"e2g1", "e2g1inplanemode", "eprimeinplanemode"},
        x_values,
        intensities,
    )
    a1g = material_band_measurement(
        peaks,
        components,
        {"a1g", "a1goutofplanemode", "a1primeoutofplanemode"},
        x_values,
        intensities,
    )
    result = {
        "analysis": "mos2_layer_screening_from_mode_separation",
        "rule_id": "mos2_e2g1_a1g_separation_v1",
        "material_system": "MoS2",
        "status": "insufficient_data",
        "e2g1_band": e2g,
        "a1g_band": a1g,
        "peak_separation_cm-1": None,
        "peak_separation_uncertainty_cm-1": None,
        "estimated_layer_count": None,
        "interpretation_confidence": "none",
        "interpretation_eligible": False,
        "interpretation_reasons": [],
        "method_scope": (
            "Screening calibration for nominal 2H-MoS2; strain, doping, "
            "temperature, substrate, disorder, and spectral calibration can "
            "shift both modes. Confirm borderline results with AFM or PL."
        ),
        "references": [
            {
                "title": "Anomalous Lattice Vibrations of Single- and Few-Layer MoS2",
                "citation": "Lee et al., ACS Nano 4, 2695–2700 (2010)",
                "doi": "10.1021/nn1003937",
                "source_url": "https://doi.org/10.1021/nn1003937",
            }
        ],
    }
    if e2g is None or a1g is None:
        result["reason"] = (
            "Both assigned E2g1 and A1g modes are required for layer screening."
        )
        return result
    separation = float(a1g["position_cm-1"]) - float(e2g["position_cm-1"])
    separation_uncertainty = math.sqrt(
        float(e2g.get("position_uncertainty_cm-1") or 0.0) ** 2
        + float(a1g.get("position_uncertainty_cm-1") or 0.0) ** 2
    )
    result["peak_separation_cm-1"] = separation
    result["peak_separation_uncertainty_cm-1"] = separation_uncertainty
    spacings = [
        float(x_values[index]) - float(x_values[index - 1])
        for index in range(1, len(x_values))
        if float(x_values[index]) > float(x_values[index - 1])
    ]
    median_spacing = median(spacings) if spacings else 0.0
    interpretation_reasons = []
    for label, band in (("E2g1", e2g), ("A1g", a1g)):
        signal_to_noise = band.get("signal_to_noise")
        if signal_to_noise is None or float(signal_to_noise) < 5.0:
            interpretation_reasons.append(f"{label} signal-to-noise is below 5")
        fwhm = band.get("fwhm_cm-1")
        if fwhm is None:
            interpretation_reasons.append(f"{label} FWHM is unavailable")
        elif median_spacing > 0.0 and float(fwhm) < 3.0 * median_spacing:
            interpretation_reasons.append(
                f"{label} is sampled by fewer than about three points across its FWHM"
            )
    result["interpretation_reasons"] = interpretation_reasons
    if interpretation_reasons:
        result.update(
            {
                "status": "review_required",
                "interpretation_confidence": "review_required",
                "reason": (
                    "Layer screening was withheld: "
                    + "; ".join(interpretation_reasons)
                    + "."
                ),
            }
        )
        return result
    if separation < 17.0 or separation > 27.0:
        result["reason"] = (
            "The observed mode separation is outside the calibration range."
        )
        return result
    calibration = parameters["mos2_layer_calibration_cm-1"]
    ranked = sorted(
        calibration,
        key=lambda item: abs(float(item["separation_cm-1"]) - separation),
    )
    nearest = ranked[0]
    alternative = ranked[1]
    nearest_distance = abs(float(nearest["separation_cm-1"]) - separation)
    alternative_distance = abs(
        float(alternative["separation_cm-1"]) - separation
    )
    category_boundary = (
        float(nearest["separation_cm-1"])
        + float(alternative["separation_cm-1"])
    ) / 2
    lower_bound = separation - separation_uncertainty
    upper_bound = separation + separation_uncertainty
    crosses_boundary = lower_bound <= category_boundary <= upper_bound
    if crosses_boundary:
        result.update(
            {
                "status": "review_required",
                "alternative_layer_count": alternative["layer_label"],
                "interpretation_confidence": "uncertainty_crosses_category_boundary",
                "interpretation_reasons": [
                    "Peak-separation uncertainty crosses the nearest layer-calibration boundary"
                ],
                "reason": (
                    "The measured separation is retained, but a layer label is withheld "
                    "because its uncertainty overlaps adjacent calibration categories."
                ),
            }
        )
        return result
    result.update(
        {
            "status": "estimated",
            "estimated_layer_count": nearest["layer_label"],
            "calibration_separation_cm-1": nearest["separation_cm-1"],
            "interpretation_confidence": (
                "low_borderline"
                if alternative_distance - nearest_distance < 0.5
                else "screening"
            ),
            "interpretation_eligible": True,
            "alternative_layer_count": (
                alternative["layer_label"]
                if alternative_distance - nearest_distance < 0.5
                else None
            ),
            "reason": (
                "The layer estimate is the nearest literature calibration "
                "value to the fitted A1g−E2g1 separation."
            ),
        }
    )
    return result


def graphitic_carbon_raman_analysis(
    peaks: list[dict],
    components: list[dict],
    x_values: list[float],
    intensities: list[float],
    parameters: dict,
) -> dict:
    d_band = material_band_measurement(
        peaks,
        components,
        {"d", "dband"},
        x_values,
        intensities,
        parameters["d_band_window_cm-1"],
    )
    g_band = material_band_measurement(
        peaks,
        components,
        {"g", "gband"},
        x_values,
        intensities,
        parameters["g_band_window_cm-1"],
    )
    two_d_band = material_band_measurement(
        peaks,
        components,
        {"2d", "2dband"},
        x_values,
        intensities,
        parameters["two_d_band_window_cm-1"],
    )
    id_ig = safe_band_ratio(d_band, g_band, "intensity")
    i2d_ig = safe_band_ratio(two_d_band, g_band, "intensity")
    area_d_g = safe_band_ratio(d_band, g_band, "area")
    area_2d_g = safe_band_ratio(two_d_band, g_band, "area")
    if g_band is None:
        status = "insufficient_data"
        disorder = "unavailable"
        order = "unavailable"
        reason = "An assigned or detectable G band is required."
    else:
        status = "screening"
        if id_ig is None:
            disorder = "unavailable"
            order = "unavailable"
        elif id_ig < 0.1:
            disorder = "low_detected_d_band_activation"
            order = "higher_graphitic_order_signature"
        elif id_ig < 0.5:
            disorder = "moderate_d_band_activation"
            order = "intermediate_graphitic_order_signature"
        else:
            disorder = "strong_d_band_activation"
            order = "lower_graphitic_order_or_high_defect_activation"
        reason = (
            "D/G screens disorder activation and 2D/G reports the relative "
            "2D-band response. These ratios are not unique measures of defect "
            "density, crystallite size, or layer count."
        )
    return {
        "analysis": "graphitic_carbon_raman_quality_ratios",
        "rule_id": "graphitic_carbon_d_g_2d_g_ratios_v1",
        "material_system": "Graphitic carbon",
        "status": status,
        "d_band": d_band,
        "g_band": g_band,
        "two_d_band": two_d_band,
        "id_ig_ratio": id_ig,
        "i2d_ig_ratio": i2d_ig,
        "area_d_g_ratio": area_d_g,
        "area_2d_g_ratio": area_2d_g,
        "defect_signature": disorder,
        "crystalline_order_screening": order,
        "quantitative_defect_density": None,
        "quantitative_defect_density_reason": (
            "Laser excitation wavelength and the applicable defect-density "
            "regime are required; I(D)/I(G) is excitation-dependent and "
            "non-monotonic across the amorphization trajectory."
        ),
        "reason": reason,
        "method_scope": (
            "Peak-height ratios are always reported when available; integrated-area "
            "ratios are additionally reported for fitted components. Doping, strain, "
            "stacking, substrate, polarization, and excitation energy can alter the ratios."
        ),
        "references": [
            {
                "title": "Quantifying Defects in Graphene via Raman Spectroscopy at Different Excitation Energies",
                "citation": "Cançado et al., Nano Letters 11, 3190–3196 (2011)",
                "doi": "10.1021/nl201432g",
                "source_url": "https://doi.org/10.1021/nl201432g",
            },
            {
                "title": "Raman Spectrum of Graphene and Graphene Layers",
                "citation": "Ferrari et al., Physical Review Letters 97, 187401 (2006)",
                "doi": "10.1103/PhysRevLett.97.187401",
                "source_url": "https://doi.org/10.1103/PhysRevLett.97.187401",
            },
        ],
    }


MATERIAL_ANALYSIS_RULES = (
    {
        "rule_id": "mos2_e2g1_a1g_separation_v1",
        "applies": is_mos2_material,
        "analyze": mos2_layer_analysis,
    },
    {
        "rule_id": "graphitic_carbon_d_g_2d_g_ratios_v1",
        "applies": is_carbon_material,
        "analyze": graphitic_carbon_raman_analysis,
    },
)


def material_system_analyses(
    material_system: str | None,
    peaks: list[dict],
    deconvolution_result: dict,
    x_values: list[float],
    intensities: list[float],
    parameters: dict,
) -> list[dict]:
    components = (
        deconvolution_result.get("components", [])
        if deconvolution_result.get("status") == "fitted"
        and deconvolution_result.get("quality", {}).get("accepted", True)
        else []
    )
    analyses = []
    for rule in MATERIAL_ANALYSIS_RULES:
        if not rule["applies"](material_system):
            continue
        arguments = (
            peaks,
            components,
            x_values,
            intensities,
            parameters,
        )
        analysis = rule["analyze"](*arguments)
        analysis["material_system"] = material_system or analysis["material_system"]
        analyses.append(analysis)
    return analyses


def collective_perovskite_modes(
    peaks: list[dict],
    material_system: str | None,
    parameters: dict,
) -> tuple[list[dict], list[dict]]:
    """Represent unresolved low-frequency perovskite modes as one envelope."""
    normalized = normalized_material_name(material_system or "")
    perovskite_components = [
        label
        for token, label in (
            ("cs2znbr4", "Cs2ZnBr4"),
            ("cspbbr3", "CsPbBr3"),
            ("cspbi3", "CsPbI3"),
            ("mapbbr3", "MAPbBr3"),
            ("mapbi3", "MAPbI3"),
            ("fapbbr3", "FAPbBr3"),
            ("fapbi3", "FAPbI3"),
        )
        if token in normalized
    ]
    if "perovskite" in normalized and not perovskite_components:
        perovskite_components = [material_system or "Perovskite"]
    if not perovskite_components:
        return [], peaks

    lower, upper = map(
        float,
        parameters["perovskite_collective_range_cm-1"],
    )
    members = [
        peak
        for peak in peaks
        if lower <= float(peak["position_cm-1"]) < upper
    ]
    if not members:
        return [], peaks
    remaining = [peak for peak in peaks if peak not in members]
    return [
        {
            "type": "collective_mode_envelope",
            "material_system": "/".join(perovskite_components),
            "label": "Collective low-frequency crystalline modes",
            "range_cm-1": [lower, upper],
            "member_peak_count": len(members),
            "member_peaks": [
                {
                    "position_cm-1": peak["position_cm-1"],
                    "intensity": peak.get("intensity"),
                    "prominence": peak.get("prominence"),
                    "assignments": peak.get("assignments", []),
                }
                for peak in members
            ],
            "treatment": "collective_excluded_from_individual_deconvolution",
            "confidence": "user_defined_material_policy",
            "reason": (
                "Closely spaced perovskite lattice modes below 150 cm-1 are "
                "reported collectively rather than over-interpreted individually. "
                "Their range is excluded from individual-component fit residuals."
            ),
        }
    ], remaining


def consolidate_mos2_to_m_modes(
    peaks: list[dict],
    material_system: str | None,
) -> tuple[list[dict], list[dict]]:
    """Use one fit seed for nearby maxima from the same broad MoS2 TO(M) mode."""
    target_label = normalized_material_name("TO(M) defect-activated phonon")
    eligible = []
    for peak in peaks:
        assignment = (peak.get("assignments") or [None])[0]
        if not assignment:
            continue
        if (
            normalized_material_name(assignment.get("material_system") or "")
            == "mos2"
            and normalized_material_name(assignment.get("label") or "")
            == target_label
        ):
            eligible.append(peak)
    if len(eligible) < 2:
        return [], peaks

    clusters: list[list[dict]] = []
    for peak in sorted(eligible, key=lambda item: item["position_cm-1"]):
        if (
            not clusters
            or float(peak["position_cm-1"])
            - float(clusters[-1][-1]["position_cm-1"])
            > 16.0
        ):
            clusters.append([peak])
        else:
            clusters[-1].append(peak)

    groups = []
    replaced_peak_ids: set[int] = set()
    representatives = []
    normalized_system = normalized_material_name(material_system or "")
    for cluster in clusters:
        if len(cluster) < 2:
            continue
        representative = max(
            cluster,
            key=lambda item: (
                float(item.get("prominence") or 0.0),
                float(item.get("intensity") or 0.0),
            ),
        )
        positions = [float(item["position_cm-1"]) for item in cluster]
        group = {
            "type": "broad_disorder_mode",
            "material_system": "MoS2",
            "label": "TO(M) defect-activated phonon",
            "range_cm-1": [min(positions), max(positions)],
            "member_peak_count": len(cluster),
            "member_peaks": [
                {
                    "position_cm-1": item["position_cm-1"],
                    "intensity": item.get("intensity"),
                    "prominence": item.get("prominence"),
                    "detection_source": item.get("detection_source"),
                    "assignments": item.get("assignments", []),
                }
                for item in cluster
            ],
            "representative_position_cm-1": representative["position_cm-1"],
            "treatment": "single_broad_mode_single_fit_seed",
            "confidence": "reference_backed_mode_family",
            "reason": (
                "Nearby fitted maxima with the same MoS2 TO(M) assignment are "
                "treated as structure within one disorder-broadened mode rather "
                "than as separate vibrations."
            ),
        }
        if "cs2znbr4" in normalized_system:
            group["conditional_alternative"] = {
                "material_system": "Cs2ZnBr4",
                "label": "Possible 2ν1 Zn-Br overtone",
                "status": "not_selected",
                "reason": (
                    "A second-order Zn-Br overtone is retained only as a "
                    "conditional alternative for Cs2ZnBr4-only regions that "
                    "track the 175-178 cm-1 ν1 band and lack MoS2 disorder-mode "
                    "evidence."
                ),
            }
        groups.append(group)
        representatives.append({**representative, "mode_group": group})
        replaced_peak_ids.update(id(item) for item in cluster)

    if not groups:
        return [], peaks
    consolidated = [
        peak for peak in peaks if id(peak) not in replaced_peak_ids
    ] + representatives
    return groups, sorted(
        consolidated,
        key=lambda peak: peak["position_cm-1"],
    )


def normalize_x_order(
    points: list[tuple[float, float]],
) -> tuple[list[tuple[float, float]], str]:
    x_values = [point[0] for point in points]
    if all(x_values[index] < x_values[index + 1] for index in range(len(points) - 1)):
        return points, "ascending_preserved"
    if all(x_values[index] > x_values[index + 1] for index in range(len(points) - 1)):
        return list(reversed(points)), "descending_reversed"
    raise ProcessingError(
        422,
        "non_monotonic_axis",
        "Raman processing requires a strictly monotonic X axis.",
    )


def display_indices(length: int) -> list[int]:
    if length <= MAX_RESULT_POINTS:
        return list(range(length))
    step = (length - 1) / (MAX_RESULT_POINTS - 1)
    return [round(index * step) for index in range(MAX_RESULT_POINTS)]


def assess_spectrum_quality(
    x_values: list[float],
    raw_intensities: list[float],
    analysis_intensities: list[float],
    parameters: dict | None = None,
) -> dict:
    """Report instrument-independent checks that gate automated interpretation."""
    configured = parameters or MODEL_PARAMETERS
    quality_policy = configured.get("quality_policy", "balanced")
    thresholds = QUALITY_POLICY_THRESHOLDS[quality_policy]
    peak_review_policy = configured.get(
        "peak_review_policy", "withhold_uncertain"
    )
    spacings = [
        x_values[index] - x_values[index - 1]
        for index in range(1, len(x_values))
    ]
    median_spacing = median(spacings) if spacings else None
    spacing_mad = (
        median(abs(spacing - median_spacing) for spacing in spacings)
        if spacings and median_spacing is not None
        else None
    )
    relative_spacing_mad = (
        spacing_mad / abs(median_spacing)
        if spacing_mad is not None and median_spacing not in {None, 0.0}
        else None
    )

    maximum = max(raw_intensities)
    maximum_run = 0
    current_run = 0
    for intensity in raw_intensities:
        current_run = current_run + 1 if intensity == maximum else 0
        maximum_run = max(maximum_run, current_run)
    saturation_fraction = maximum_run / len(raw_intensities)
    saturation_detected = (
        maximum_run >= thresholds["saturation_minimum_run_points"]
        and saturation_fraction > thresholds["saturation_maximum_fraction"]
    )

    raw_range = maximum - min(raw_intensities)
    adjacent_differences = [
        abs(analysis_intensities[index] - analysis_intensities[index - 1])
        for index in range(1, len(analysis_intensities))
    ]
    noise_estimate = median(adjacent_differences) if adjacent_differences else 0.0
    strongest_signal = max(analysis_intensities, default=0.0)
    strongest_signal_to_noise = (
        strongest_signal / noise_estimate if noise_estimate > 0.0 else None
    )

    checks = [
        {
            "id": "axis_monotonicity",
            "label": "Strictly increasing Raman-shift axis",
            "status": "passed",
            "value": True,
        },
        {
            "id": "axis_spacing",
            "label": "Regular Raman-shift spacing",
            "status": (
                "review_required"
                if relative_spacing_mad is not None
                and relative_spacing_mad
                > thresholds["maximum_axis_spacing_relative_mad"]
                else "passed"
            ),
            "value": relative_spacing_mad,
            "maximum_relative_mad": thresholds[
                "maximum_axis_spacing_relative_mad"
            ],
        },
        {
            "id": "detector_saturation",
            "label": "No flat-topped detector maximum",
            "status": "review_required" if saturation_detected else "passed",
            "value": saturation_detected,
        },
        {
            "id": "minimum_sampling",
            "label": "Enough points for automated interpretation",
            "status": (
                "failed"
                if len(x_values) < thresholds["minimum_point_count"]
                else "passed"
            ),
            "value": len(x_values),
            "minimum_point_count": thresholds["minimum_point_count"],
        },
        {
            "id": "nonconstant_signal",
            "label": "Non-constant measured intensity",
            "status": "failed" if raw_range <= 0.0 else "passed",
            "value": raw_range,
        },
    ]
    minimum_signal_to_noise = thresholds["minimum_signal_to_noise"]
    if minimum_signal_to_noise is not None:
        checks.append(
            {
                "id": "signal_contrast",
                "label": "Derived signal-to-noise meets policy",
                "status": (
                    "review_required"
                    if strongest_signal_to_noise is None
                    or strongest_signal_to_noise < minimum_signal_to_noise
                    else "passed"
                ),
                "value": strongest_signal_to_noise,
                "minimum_signal_to_noise": minimum_signal_to_noise,
            }
        )
    failed = [check for check in checks if check["status"] == "failed"]
    review = [
        check for check in checks if check["status"] == "review_required"
    ]
    badge = "poor" if failed else "review" if review else "good"
    blocking_review_ids = (
        set()
        if peak_review_policy == "flag_only"
        else {check["id"] for check in review}
        if peak_review_policy == "require_all_checks"
        else {"detector_saturation", "signal_contrast"}
    )
    blocking_checks = failed + [
        check for check in review if check["id"] in blocking_review_ids
    ]
    return {
        "quality_policy": quality_policy,
        "peak_review_policy": peak_review_policy,
        "badge": badge,
        "interpretation_eligible": not blocking_checks,
        "review_required": bool(failed or review),
        "checks": checks,
        "blocking_check_ids": [check["id"] for check in blocking_checks],
        "metrics": {
            "point_count": len(x_values),
            "median_spacing_cm-1": median_spacing,
            "spacing_relative_mad": relative_spacing_mad,
            "raw_intensity_minimum": min(raw_intensities),
            "raw_intensity_maximum": maximum,
            "raw_intensity_range": raw_range,
            "maximum_plateau_points": maximum_run,
            "maximum_plateau_fraction": saturation_fraction,
            "adjacent_difference_noise_estimate": noise_estimate,
            "strongest_derived_signal_to_noise": strongest_signal_to_noise,
        },
        "uncertainty_scope": {
            "includes_sampling_statistics": True,
            "includes_fit_statistics": False,
            "includes_calibration_systematics": False,
            "note": (
                "Quality metrics screen sampling, saturation, and derived-signal "
                "contrast. Instrument calibration and model-choice uncertainty "
                "must be assessed separately."
            ),
        },
    }


def build_processing_protocol(
    input_order: str,
    artifacts: list[dict],
    substrate_result: dict,
    deconvolution_result: dict,
    material_analyses: list[dict],
    quality: dict,
    parameters: dict,
) -> dict:
    """Expose the executed order, methods, and stage outcomes for audit and UI."""
    rayleigh_count = sum(
        artifact.get("type") == "rayleigh_line_leakage" for artifact in artifacts
    )
    cosmic_count = sum(
        artifact.get("type") == "cosmic_ray_spike" for artifact in artifacts
    )
    confirmed_count = sum(
        artifact.get("type") == "user_confirmed_instrument_artifact"
        for artifact in artifacts
    )
    substrate_status = substrate_result.get("status", "not_run")
    deconvolution_status = deconvolution_result.get("status", "not_run")
    quality_status = {
        "good": "passed",
        "review": "review_required",
        "poor": "failed",
    }[quality["badge"]]
    stages = [
        {
            "order": 1,
            "id": "axis_validation",
            "label": "Validate and order axis",
            "status": "completed",
            "method": "strict monotonicity validation and optional bounded selection",
            "detail": (
                f"{input_order.replace('_', ' ')}; derived analysis range "
                f"{parameters.get('analysis_range_min_cm-1') if parameters.get('analysis_range_min_cm-1') is not None else 'measured minimum'} to "
                f"{parameters.get('analysis_range_max_cm-1') if parameters.get('analysis_range_max_cm-1') is not None else 'measured maximum'} cm-1"
            ),
        },
        {
            "order": 2,
            "id": "instrument_artifact_correction",
            "label": "Correct instrument artifacts",
            "status": "applied" if artifacts else "not_detected",
            "method": parameters["artifact_correction_method"],
            "detail": (
                f"{rayleigh_count} Rayleigh leakage interval(s), "
                f"{cosmic_count} cosmic spike(s), and "
                f"{confirmed_count} user-confirmed artifact interval(s) corrected"
                if artifacts
                else "No Rayleigh leakage, cosmic spikes, or confirmed artifacts detected"
            ),
        },
        {
            "order": 3,
            "id": "baseline_subtraction",
            "label": "Estimate and subtract baseline",
            "status": "applied",
            "method": parameters["baseline_method"],
            "detail": (
                "Piecewise-linear lower convex hull across the ordered spectrum"
                if parameters["baseline_method"] == "rubber_band"
                else (
                    f"{parameters['baseline_window_points']}-point opening with "
                    f"{parameters['baseline_smoothing_points']}-point baseline smoothing"
                )
            ),
        },
        {
            "order": 4,
            "id": "reference_screening",
            "label": "Screen peaks and reference evidence",
            "status": "completed",
            "method": "noise-gated peak detection plus reference-guided candidates",
            "detail": (
                "Preliminary evidence protects material/substrate overlaps; "
                f"minimum prominence is {parameters['minimum_prominence_fraction'] * 100:.3g}% of signal maximum or the noise gate, whichever is greater"
            ),
        },
        {
            "order": 5,
            "id": "substrate_correction",
            "label": "Model substrate contribution",
            "status": "skipped" if substrate_status == "disabled" else substrate_status,
            "method": (
                substrate_result.get("fit_profile")
                or substrate_result.get("correction")
                or parameters["substrate_correction_mode"]
            ),
            "detail": (
                f"{substrate_result.get('reason') or 'Substrate correction disabled'} "
                f"Reference strategy: {parameters['substrate_reference_strategy'].replace('_', ' ')}."
            ),
        },
        {
            "order": 6,
            "id": "peak_assignment",
            "label": "Detect and assign final peaks",
            "status": "completed",
            "method": "noise-gated evidence assignment with user confirmations",
            "detail": "Assignments remain traceable to their evidence sources",
        },
        {
            "order": 7,
            "id": "deconvolution",
            "label": "Fit overlapping peak components",
            "status": "skipped" if deconvolution_status == "disabled" else deconvolution_status,
            "method": parameters["deconvolution_profile"],
            "detail": (
                "Fit accepted only after signed-residual quality checks"
                if deconvolution_status == "fitted"
                else "Optional stage was not fitted"
            ),
        },
        {
            "order": 8,
            "id": "material_interpretation",
            "label": "Run material-specific interpretation",
            "status": "completed" if material_analyses else "not_applicable",
            "method": "registered evidence-backed material rules",
            "detail": (
                f"{len(material_analyses)} material-specific analysis rule(s) executed"
                if material_analyses
                else "No registered rule applies to the effective material system"
            ),
        },
        {
            "order": 9,
            "id": "quality_gate",
            "label": "Apply spectrum quality gate",
            "status": quality_status,
            "method": (
                f"{parameters['quality_policy']} quality policy; "
                f"{parameters['peak_review_policy'].replace('_', ' ')} review policy"
            ),
            "detail": (
                "Automated interpretation is eligible"
                if quality["interpretation_eligible"]
                else "Automated interpretation requires review"
            ),
        },
    ]
    return {
        "protocol_id": "raman_material_identification_protocol",
        "protocol_version": MODEL_VERSION,
        "stage_order_is_mandatory": True,
        "stages": stages,
        "invariants": [
            "Raw measurement bytes and raw plotted values are never modified.",
            "Instrument artifacts are corrected before baseline estimation so isolated spikes do not bias the background model.",
            "Smoothing is disabled for the material-analysis trace; method-specific smoothing is confined to diagnostic baseline or substrate models.",
            "Substrate and deconvolution residuals are constrained nonnegative only in derived display/analysis series.",
            "Automated material interpretations remain screening estimates and require scientific review.",
        ],
    }


def build_raman_result(
    metadata: dict,
    text: str,
    source_sha256: str,
    parameters: dict,
    reference_sources: list[dict],
    substrate_reference_traces: dict | None = None,
) -> dict:
    series = preview.extract_numeric_series(text)
    technique = (metadata.get("technique") or "").lower()
    file_type = series["headers"].get("FILETYPE", "").lower()
    if "raman" not in technique and "raman" not in file_type:
        raise ProcessingError(
            422,
            "unsupported_processing_technique",
            "The current processing model supports Raman spectra only.",
        )

    ordered_points, input_order = normalize_x_order(series["points"])
    range_minimum = parameters.get("analysis_range_min_cm-1")
    range_maximum = parameters.get("analysis_range_max_cm-1")
    if range_minimum is not None or range_maximum is not None:
        ordered_points = [
            point
            for point in ordered_points
            if (range_minimum is None or point[0] >= range_minimum)
            and (range_maximum is None or point[0] <= range_maximum)
        ]
        if len(ordered_points) < 5:
            raise ProcessingError(
                422,
                "insufficient_analysis_range",
                "The selected analysis range must contain at least five measured points.",
            )
    x_values = [point[0] for point in ordered_points]
    raw_intensities = [point[1] for point in ordered_points]
    rayleigh_corrected, rayleigh_artifacts = detect_and_correct_rayleigh_line(
        x_values,
        raw_intensities,
        parameters,
    )
    confirmed_artifact_corrected, confirmed_artifacts = (
        apply_confirmed_instrument_artifacts(
            x_values,
            rayleigh_corrected,
            parameters.get("confirmed_instrument_artifacts") or [],
            parameters,
        )
    )
    artifact_corrected, cosmic_artifacts = detect_and_correct_cosmic_spikes(
        x_values,
        confirmed_artifact_corrected,
        parameters,
    )
    artifacts = rayleigh_artifacts + confirmed_artifacts + cosmic_artifacts
    baseline = estimate_baseline(artifact_corrected, x_values, parameters)
    corrected = [
        intensity - baseline_value
        for intensity, baseline_value in zip(artifact_corrected, baseline)
    ]
    initial_peaks = [
        peak
        for peak in detect_peaks(x_values, corrected, parameters)
        if peak["position_cm-1"] >= 50.0
    ]
    initial_peaks = merge_peak_candidates(
        initial_peaks,
        empirical_substrate_peak_candidates(
            x_values,
            corrected,
            parameters.get("substrate_profiles") or {},
            parameters,
        ),
    )
    broad_substrate_candidates = []
    for substrate_name in ("glass", "sio2_si"):
        broad_substrate_candidates.extend(
            broad_substrate_peak_candidates(
                x_values,
                corrected,
                substrate_name,
                parameters,
                parameters.get("substrate_profiles") or {},
            )
        )
    initial_peaks = merge_peak_candidates(
        initial_peaks,
        broad_substrate_candidates,
    )
    confirmed_or_declared_material = (
        (parameters.get("material_confirmation") or {}).get("material_system")
        or metadata.get("material_system")
    )
    initial_peaks = merge_peak_candidates(
        initial_peaks,
        reference_guided_peak_candidates(
            x_values,
            corrected,
            reference_sources,
            confirmed_or_declared_material,
            parameters,
        ),
    )
    initial_peaks, _preliminary_noise_exclusions = exclude_confirmed_noise_peaks(
        initial_peaks,
        parameters.get("confirmed_noise_exclusions") or [],
    )
    preliminary_identification = identify_material_system(
        initial_peaks,
        reference_sources,
        parameters,
        metadata.get("material_system"),
        metadata.get("material_confirmation"),
    )
    preliminary_material_system = (
        preliminary_identification.get("confirmed_material_system")
        or metadata.get("material_system")
    )
    initial_peaks = assign_peak_labels(
        initial_peaks,
        reference_sources,
        preliminary_material_system,
        preliminary_identification,
        parameters,
    )
    substrate_corrected, substrate_result = detect_and_subtract_substrate(
        x_values,
        corrected,
        initial_peaks,
        parameters["substrate_correction_mode"],
        parameters,
        substrate_reference_traces,
    )
    substrate_corrected, substrate_result = (
        enforce_substrate_nonnegative_constraint(
            x_values,
            corrected,
            substrate_corrected,
            substrate_result,
        )
    )
    substrate_corrected, substrate_result = apply_substrate_only_ranges(
        x_values,
        substrate_corrected,
        substrate_result,
        initial_peaks,
        metadata,
        parameters,
    )
    analysis_intensities = (
        substrate_corrected
        if substrate_result["status"] == "subtracted"
        else corrected
    )
    peaks = [
        peak
        for peak in detect_peaks(x_values, analysis_intensities, parameters)
        if peak["position_cm-1"] >= 50.0
    ]
    peaks = merge_peak_candidates(
        peaks,
        reference_guided_peak_candidates(
            x_values,
            analysis_intensities,
            reference_sources,
            preliminary_material_system,
            parameters,
        ),
    )
    peaks, confirmed_noise_exclusions = exclude_confirmed_noise_peaks(
        peaks,
        parameters.get("confirmed_noise_exclusions") or [],
    )
    substrate_residual_peaks = []
    if substrate_result["status"] == "subtracted":
        protected_ranges = [
            peak["protected_range_cm-1"]
            for peak in substrate_result.get("protected_material_peaks", [])
        ]
        provisionally_labelled_peaks = assign_peak_labels(
            peaks,
            reference_sources,
            preliminary_material_system,
            preliminary_identification,
            parameters,
        )
        provisionally_labelled_peaks = apply_confirmed_peak_assignments(
            provisionally_labelled_peaks,
            parameters.get("confirmed_peak_assignments") or [],
        )
        substrate_residual_peaks = [
            peak
            for peak in provisionally_labelled_peaks
            if not any(
                lower <= peak["position_cm-1"] <= upper
                for lower, upper in protected_ranges
            )
            and is_unopposed_substrate_peak(
                peak,
                substrate_result.get("selected_substrate"),
                preliminary_material_system,
                parameters,
            )
        ]
        residual_positions = {
            peak["position_cm-1"] for peak in substrate_residual_peaks
        }
        peaks = [
            peak
            for peak in peaks
            if peak["position_cm-1"] not in residual_positions
        ]
    substrate_result["residual_peaks"] = substrate_residual_peaks
    substrate_result["residual_peak_count"] = len(substrate_residual_peaks)
    if substrate_result["status"] == "subtracted":
        substrate_result["residual_peak_rule"] = (
            "known_substrate_band_with_tolerance_unless_material_overlap"
        )
        substrate_result["residual_band_tolerance_cm-1"] = parameters[
            "substrate_residual_band_tolerance_cm-1"
        ]
    identification = identify_material_system(
        peaks,
        reference_sources,
        parameters,
        metadata.get("material_system"),
        metadata.get("material_confirmation"),
    )
    effective_material_system = (
        identification.get("confirmed_material_system")
        or metadata.get("material_system")
    )
    peaks = include_user_confirmed_peaks(
        peaks,
        parameters.get("confirmed_peak_assignments") or [],
        x_values,
        analysis_intensities,
    )
    peaks = assign_peak_labels(
        peaks,
        reference_sources,
        effective_material_system,
        identification,
        parameters,
    )
    peaks = apply_confirmed_peak_assignments(
        peaks,
        parameters.get("confirmed_peak_assignments") or [],
    )
    peaks = noise_gated_substrate_reference_peaks(
        peaks,
        metadata,
        parameters,
    )
    peaks = label_confirmed_substrate_reference_peaks(
        peaks,
        metadata,
        source_sha256,
    )
    peaks, automatic_noise_exclusions = exclude_noise_indistinguishable_peaks(
        peaks,
        x_values,
        analysis_intensities,
        parameters,
    )
    disorder_mode_groups, peaks = consolidate_mos2_to_m_modes(
        peaks,
        identification.get("confirmed_material_system")
        or identification.get("material_system")
        or metadata.get("material_system"),
    )
    collective_bands, peaks = collective_perovskite_modes(
        peaks,
        identification.get("confirmed_material_system")
        or identification.get("material_system")
        or metadata.get("material_system"),
        parameters,
    )
    unassigned_peaks = unassigned_peaks_for_review(
        peaks,
        reference_sources,
        parameters,
        analysis_intensities,
    )
    collective_ranges = [band["range_cm-1"] for band in collective_bands]
    substrate_only_ranges = substrate_result.get(
        "substrate_only_ranges_cm-1",
        [],
    )
    substrate_residual_ranges = [
        peak["substrate_band_range_cm-1"]
        for peak in substrate_result.get("residual_broad_peak_search", {}).get(
            "assigned_peaks",
            [],
        )
        if peak.get("substrate_band_range_cm-1")
    ]
    for peak in substrate_result.get("residual_peaks", []):
        for assignment in peak.get("assignments", []):
            reference_range = assignment.get("reference_range_cm-1")
            if reference_range:
                substrate_residual_ranges.append(reference_range)
    deconvolution_excluded_ranges = list(
        dict.fromkeys(
            tuple(map(float, item))
            for item in (
                collective_ranges
                + substrate_only_ranges
                + substrate_residual_ranges
            )
        )
    )
    deconvolution_excluded_ranges = [
        list(item) for item in deconvolution_excluded_ranges
    ]
    deconvolution_intensities = mask_values_in_ranges(
        x_values,
        analysis_intensities,
        deconvolution_excluded_ranges,
    )
    material_deconvolution_parameters = {
        **parameters,
        "deconvolution_maximum_fwhm_cm-1": parameters[
            "material_deconvolution_maximum_fwhm_cm-1"
        ],
    }
    deconvolution_result = deconvolve_peaks(
        x_values,
        deconvolution_intensities,
        peaks,
        parameters["deconvolution_profile"],
        material_deconvolution_parameters,
    )
    if deconvolution_result["status"] == "fitted":
        signed_residuals = deconvolution_result["residual_values"]
        negative_residual_indices = [
            index
            for index, value in enumerate(signed_residuals)
            if value < 0.0
        ]
        deconvolution_result["residual_values"] = [
            max(0.0, value) for value in signed_residuals
        ]
        deconvolution_result["residual_nonnegative_floor"] = {
            "floor": 0.0,
            "affected_point_count": len(negative_residual_indices),
            "minimum_signed_residual": min(signed_residuals, default=None),
            "quality_metrics_use_signed_residual": True,
        }
    if deconvolution_excluded_ranges and deconvolution_result["status"] == "fitted":
        deconvolution_result["excluded_ranges_cm-1"] = (
            deconvolution_excluded_ranges
        )
        deconvolution_result["fit_values"] = mask_values_in_ranges(
            x_values,
            deconvolution_result["fit_values"],
            deconvolution_excluded_ranges,
        )
        deconvolution_result["residual_values"] = mask_values_in_ranges(
            x_values,
            deconvolution_result["residual_values"],
            deconvolution_excluded_ranges,
        )
        for component in deconvolution_result["components"]:
            component["values"] = mask_values_in_ranges(
                x_values,
                component["values"],
                deconvolution_excluded_ranges,
            )

    analysis_material_system = (
        identification.get("confirmed_material_system")
        or identification.get("material_system")
        or metadata.get("material_system")
    )
    spectrum_quality = assess_spectrum_quality(
        x_values,
        raw_intensities,
        analysis_intensities,
        parameters,
    )
    material_analyses = material_system_analyses(
        analysis_material_system,
        peaks,
        deconvolution_result,
        x_values,
        analysis_intensities,
        parameters,
    )
    for analysis in material_analyses:
        analysis["quality_gate"] = {
            "interpretation_eligible": spectrum_quality[
                "interpretation_eligible"
            ],
            "blocking_check_ids": spectrum_quality["blocking_check_ids"],
            "note": (
                "Reported measurements may be inspected, but automated "
                "interpretation requires review."
                if not spectrum_quality["interpretation_eligible"]
                else "Spectrum-level checks permit automated screening interpretation."
            ),
        }
    material_analysis = material_analyses[0] if material_analyses else None
    processing_protocol = build_processing_protocol(
        input_order,
        artifacts,
        substrate_result,
        deconvolution_result,
        material_analyses,
        spectrum_quality,
        parameters,
    )

    indices = display_indices(len(x_values))
    processed_at = datetime.now(timezone.utc).isoformat()
    processing_id = str(uuid4())
    summary = {
        "artifacts": artifacts,
        "instrument_artifacts": artifacts,
        "deconvolution": {
            "status": deconvolution_result["status"],
            "profile": deconvolution_result["profile"],
            "component_count": len(deconvolution_result["components"]),
            "quality": deconvolution_result.get("quality"),
            "review_required": (
                deconvolution_result.get("quality", {}).get("accepted") is False
            ),
        },
        "identification": identification,
        "material_analysis": material_analysis,
        "material_analyses": material_analyses,
        "spectrum_quality": spectrum_quality,
        "processing_protocol": processing_protocol,
        "collective_band_count": len(collective_bands),
        "collective_bands": collective_bands,
        "disorder_mode_group_count": len(disorder_mode_groups),
        "disorder_mode_groups": disorder_mode_groups,
        "peak_count": len(peaks),
        "unassigned_peak_count": len(unassigned_peaks),
        "unassigned_peaks": unassigned_peaks,
        "recurring_unassigned_peak_count": sum(
            1 for peak in unassigned_peaks if peak.get("recurrence")
        ),
        "confirmed_noise_peak_count": len(confirmed_noise_exclusions),
        "confirmed_noise_peaks": confirmed_noise_exclusions,
        "automatic_noise_peak_count": len(automatic_noise_exclusions),
        "automatic_noise_peaks": automatic_noise_exclusions,
        "substrate_correction": {
            key: value
            for key, value in substrate_result.items()
            if key not in {"fit_values", "components"}
        }
        | {"component_count": len(substrate_result["components"])},
    }
    result_series = {
        "raw": [[x_values[index], raw_intensities[index]] for index in indices],
        "artifact_corrected": [
            [x_values[index], artifact_corrected[index]] for index in indices
        ],
        "baseline": [[x_values[index], baseline[index]] for index in indices],
        "corrected": [[x_values[index], corrected[index]] for index in indices],
    }
    normalization_reference = None
    normalization_mode = parameters.get("normalization_mode", "none")
    normalization_peak = parameters.get("normalization_peak_cm-1")
    normalization_values = (
        substrate_corrected
        if substrate_result.get("status") == "subtracted"
        else corrected
    )
    reference_intensity = None
    reference_description = None
    if normalization_mode == "maximum":
        reference_index = max(range(len(normalization_values)), key=lambda index: abs(normalization_values[index]))
        reference_intensity = normalization_values[reference_index]
        reference_description = {
            "source": "maximum_corrected_intensity",
            "applied_position_cm-1": x_values[reference_index],
        }
    elif normalization_mode == "area":
        reference_intensity = sum(
            abs(x_values[index] - x_values[index - 1])
            * (abs(normalization_values[index]) + abs(normalization_values[index - 1]))
            / 2
            for index in range(1, len(x_values))
        )
        reference_description = {"source": "integrated_absolute_corrected_area"}
    elif normalization_mode == "peak" and normalization_peak is not None:
        candidates = [
            {"position_cm-1": float(peak["position_cm-1"]), "source": "material_peak"}
            for peak in peaks
        ]
        for band in substrate_result.get("matched_bands", []):
            candidates.extend(
                {
                    "position_cm-1": float(position),
                    "source": "substrate_peak",
                }
                for position in band.get("observed_peaks_cm-1", [])
            )
        if candidates:
            normalization_reference = min(
                candidates,
                key=lambda candidate: abs(candidate["position_cm-1"] - normalization_peak),
            )
            reference_index = min(
                range(len(x_values)),
                key=lambda index: abs(x_values[index] - normalization_reference["position_cm-1"]),
            )
            reference_intensity = (
                substrate_result.get("fit_values", [])[reference_index]
                if normalization_reference["source"] == "substrate_peak"
                and substrate_result.get("fit_values")
                else normalization_values[reference_index]
            )
            if abs(reference_intensity) > 1e-12:
                normalization_reference = {
                    **normalization_reference,
                    "requested_position_cm-1": normalization_peak,
                    "applied_position_cm-1": x_values[reference_index],
                    "intensity": reference_intensity,
                    "method": (
                        "substrate_fit_nearest_selected_peak"
                        if normalization_reference["source"] == "substrate_peak"
                        else "baseline_corrected_nearest_selected_peak"
                    ),
                }
    if reference_intensity is not None and abs(reference_intensity) > 1e-12:
        result_series["normalized"] = [
            [x_values[index], normalization_values[index] / reference_intensity]
            for index in indices
        ]
        normalization_reference = {
            **(normalization_reference or {}),
            **(reference_description or {}),
            "mode": normalization_mode,
            "intensity": reference_intensity,
        }
    summary["normalization"] = normalization_reference or {"status": "not_applied"}
    normalized_label = "Normalized intensity"
    if normalization_reference:
        normalized_label += (
            f" (reference {normalization_reference.get('source', 'peak').replace('_', ' ')}"
            f" {normalization_reference.get('applied_position_cm-1', '')} cm-1"
            f" = {normalization_reference['intensity']:.4g})"
        )
    deconvolution = {
        key: value
        for key, value in deconvolution_result.items()
        if key not in {"fit_values", "residual_values", "components"}
    }
    substrate_correction = {
        key: value
        for key, value in substrate_result.items()
        if key not in {"fit_values", "components"}
    }
    substrate_correction["components"] = []
    if substrate_result["status"] == "subtracted":
        result_series["substrate_corrected"] = [
            [x_values[index], substrate_corrected[index]] for index in indices
        ]
        result_series["substrate_fit"] = [
            [x_values[index], substrate_result["fit_values"][index]]
            for index in indices
        ]
        for component in substrate_result["components"]:
            substrate_correction["components"].append(
                {
                    key: value
                    for key, value in component.items()
                    if key != "values"
                }
                | {
                    "series": [
                        [x_values[index], component["values"][index]]
                        for index in indices
                    ]
                }
            )
    deconvolution["components"] = []
    if deconvolution_result["status"] == "fitted":
        result_series["deconvolved_fit"] = [
            [x_values[index], deconvolution_result["fit_values"][index]]
            for index in indices
        ]
        result_series["deconvolution_residual"] = [
            [x_values[index], deconvolution_result["residual_values"][index]]
            for index in indices
        ]
        for component in deconvolution_result["components"]:
            deconvolution["components"].append(
                {
                    key: value
                    for key, value in component.items()
                    if key != "values"
                }
                | {
                    "series": [
                        [x_values[index], component["values"][index]]
                        for index in indices
                    ]
                }
            )

    return {
        "processing_id": processing_id,
        "file_id": metadata["file_id"],
        "source_sha256": source_sha256,
        "processed_at": processed_at,
        "model": {
            "name": MODEL_NAME,
            "version": MODEL_VERSION,
            "parameters": parameters,
        },
        "axes": {
            "x_label": series["x_label"],
            "raw_y_label": series["y_label"],
            "corrected_y_label": "Baseline-corrected intensity",
            "substrate_corrected_y_label": "Substrate-corrected intensity",
            "normalized_y_label": normalized_label,
        },
        "input_order": input_order,
        "point_count": len(x_values),
        "displayed_point_count": len(indices),
        "series": result_series,
        "instrument_artifacts": artifacts,
        "peaks": peaks,
        "automatic_noise_peaks": automatic_noise_exclusions,
        "collective_bands": collective_bands,
        "disorder_mode_groups": disorder_mode_groups,
        "spectrum_quality": spectrum_quality,
        "processing_protocol": processing_protocol,
        "substrate_correction": substrate_correction,
        "deconvolution": deconvolution,
        "summary": summary,
    }


def resolve_result_path(processing_run: dict, processed_directory: Path) -> Path:
    expected_directory = (
        processed_directory
        / processing_run["file_id"]
        / processing_run["processing_id"]
    ).resolve()
    result_path = Path(processing_run["result_path"])
    if not result_path.is_absolute():
        raise ProcessingError(
            409,
            "invalid_result_path",
            "The processing result path is not an absolute managed path.",
        )
    resolved_path = result_path.resolve()
    try:
        resolved_path.relative_to(expected_directory)
    except ValueError as error:
        raise ProcessingError(
            409,
            "invalid_result_path",
            "The processing result is outside its managed directory.",
        ) from error
    if not resolved_path.is_file():
        raise ProcessingError(
            409,
            "processing_result_missing",
            "The processing record does not have a corresponding result file.",
        )
    return resolved_path


def load_processing_result(
    processing_run: dict,
    processed_directory: Path,
) -> dict:
    result_path = resolve_result_path(processing_run, processed_directory)
    result_bytes = result_path.read_bytes()
    result_sha256 = hashlib.sha256(result_bytes).hexdigest()
    if result_sha256.lower() != processing_run["result_sha256"].lower():
        raise ProcessingError(
            409,
            "processing_checksum_mismatch",
            "The derived result checksum does not match its provenance record.",
        )
    try:
        result = json.loads(result_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProcessingError(
            409,
            "invalid_processing_result",
            "The derived processing result cannot be decoded.",
        ) from error
    revalidate_legacy_deconvolution(result)
    result["result_sha256"] = result_sha256
    return result


def revalidate_legacy_deconvolution(result: dict) -> None:
    """Apply the current residual gate to older immutable fit results in memory."""
    deconvolution = result.get("deconvolution", {})
    if deconvolution.get("status") != "fitted":
        return
    existing_quality = deconvolution.get("quality") or {}
    if "accepted" in existing_quality:
        return

    series = result.get("series", {})
    target_points = series.get("substrate_corrected") or series.get("corrected")
    fitted_points = series.get("deconvolved_fit")
    components = deconvolution.get("components") or []
    quality = dict(existing_quality)
    threshold = float(MODEL_PARAMETERS["deconvolution_maximum_residual_to_fit_ratio"])
    if not target_points or not fitted_points or not components:
        quality.update(
            {
                "accepted": False,
                "acceptance_status": "review_required",
                "maximum_allowed_residual_to_fit_ratio": threshold,
                "global_residual_to_fit_ratio": None,
                "maximum_component_residual_to_fit_ratio": None,
                "component_checks": [],
                "legacy_result_revalidated": True,
                "reason": (
                    "This legacy fitted result lacks the stored series or components "
                    "required by the current residual acceptance gate. Reprocess it "
                    "before using or plotting the deconvolution."
                ),
            }
        )
    else:
        fitted_by_x = {
            float(point[0]): float(point[1]) for point in fitted_points
        }
        aligned = [
            (float(point[0]), float(point[1]), fitted_by_x[float(point[0])])
            for point in target_points
            if float(point[0]) in fitted_by_x
        ]
        if len(aligned) < 2:
            quality.update(
                {
                    "accepted": False,
                    "acceptance_status": "review_required",
                    "maximum_allowed_residual_to_fit_ratio": threshold,
                    "global_residual_to_fit_ratio": None,
                    "maximum_component_residual_to_fit_ratio": None,
                    "component_checks": [],
                    "legacy_result_revalidated": True,
                    "reason": (
                        "This legacy result's fitted and corrected series cannot be "
                        "aligned for the current residual acceptance gate. Reprocess "
                        "it before using or plotting the deconvolution."
                    ),
                }
            )
        else:
            parameters = {
                **MODEL_PARAMETERS,
                **result.get("model", {}).get("parameters", {}),
            }
            current_quality = deconvolution_residual_quality(
                [point[0] for point in aligned],
                [max(0.0, point[1]) for point in aligned],
                [point[2] for point in aligned],
                [
                    float(
                        component.get(
                            "seed_center_cm-1",
                            component["center_cm-1"],
                        )
                    )
                    for component in components
                ],
                [float(component["center_cm-1"]) for component in components],
                [float(component["fwhm_cm-1"]) for component in components],
                parameters,
            )
            quality.update(current_quality)
            quality["legacy_result_revalidated"] = True

    deconvolution["quality"] = quality
    summary_deconvolution = result.get("summary", {}).get("deconvolution")
    if isinstance(summary_deconvolution, dict):
        summary_deconvolution["quality"] = quality
        summary_deconvolution["review_required"] = quality["accepted"] is False


def has_compatible_processing_payload(result: dict) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("model", {}).get("name") != MODEL_NAME:
        return False
    if result.get("model", {}).get("version") != MODEL_VERSION:
        return False
    series = result.get("series")
    if not isinstance(series, dict):
        return False
    required_series = {"raw", "artifact_corrected", "baseline", "corrected"}
    if not required_series.issubset(series):
        return False
    if "instrument_artifacts" not in result:
        return False
    summary = result.get("summary")
    if not isinstance(summary, dict):
        return False
    if "artifacts" not in summary or "instrument_artifacts" not in summary:
        return False
    return True


def cross_sample_substrate_profiles(processed_directory: Path) -> dict:
    """Build conservative profiles from user-confirmed, immutable measurements."""
    samples_by_substrate = {"glass": [], "sio2_si": []}
    for imported_file in database.list_imported_files():
        substrate = imported_file.get("substrate")
        if substrate not in samples_by_substrate:
            continue
        source_result = None
        source_run = None
        for run in database.list_processing_runs(imported_file["file_id"]):
            try:
                parameters = json.loads(run["parameters_json"])
            except json.JSONDecodeError:
                continue
            if parameters.get("substrate_correction_mode") not in {"none", "detect"}:
                continue
            try:
                source_result = load_processing_result(run, processed_directory)
            except ProcessingError:
                logger.warning(
                    "Skipping an invalid processing result in substrate learning",
                    exc_info=True,
                )
                continue
            source_run = run
            break
        if source_result is None:
            continue
        peak_observations = {}
        for peak in source_result.get("peaks", []):
            position = round(float(peak["position_cm-1"]), 6)
            if position < 50.0:
                continue
            has_material_evidence = any(
                material_assignment_protects_substrate_overlap(
                    assignment,
                    imported_file.get("material_system"),
                )
                for assignment in peak.get("assignments", [])
            )
            peak_observations[position] = (
                peak_observations.get(position, False) or has_material_evidence
            )
        raw_values = [point[1] for point in source_result.get("series", {}).get("artifact_corrected", [])]
        baseline_points = source_result.get("series", {}).get("baseline", [])
        baseline_values = [point[1] for point in baseline_points]
        signal_range = max(raw_values) - min(raw_values) if raw_values else 0.0
        background = None
        if baseline_values and signal_range > 0:
            background = {
                "level_fraction": median(baseline_values) / signal_range,
                "slope_fraction": (
                    baseline_values[-1] - baseline_values[0]
                ) / signal_range,
            }
        samples_by_substrate[substrate].append(
            {
                "file_id": imported_file["file_id"],
                "material_system": imported_file.get("material_system") or "Unknown",
                "result_sha256": source_run["result_sha256"],
                "peak_observations": [
                    {
                        "position_cm-1": position,
                        "has_material_evidence": has_material_evidence,
                    }
                    for position, has_material_evidence in sorted(
                        peak_observations.items()
                    )
                ],
                "background": background,
            }
        )

    profiles = {}
    tolerance = float(
        MODEL_PARAMETERS["substrate_profile_cluster_tolerance_cm-1"]
    )
    same_material_minimum_samples = int(
        MODEL_PARAMETERS["substrate_profile_same_material_minimum_samples"]
    )
    same_material_minimum_recurrence_fraction = float(
        MODEL_PARAMETERS[
            "substrate_profile_same_material_minimum_recurrence_fraction"
        ]
    )
    for substrate, samples in samples_by_substrate.items():
        clusters = []
        for sample in samples:
            for peak_observation in sample["peak_observations"]:
                position = peak_observation["position_cm-1"]
                cluster = next(
                    (
                        candidate
                        for candidate in clusters
                        if abs(position - candidate["center"]) <= tolerance
                    ),
                    None,
                )
                if cluster is None:
                    cluster = {"center": position, "observations": []}
                    clusters.append(cluster)
                if sample["file_id"] not in {
                    item["file_id"] for item in cluster["observations"]
                }:
                    cluster["observations"].append(
                        {
                            "file_id": sample["file_id"],
                            "material_system": sample["material_system"],
                            "position_cm-1": position,
                            "has_material_evidence": peak_observation[
                                "has_material_evidence"
                            ],
                        }
                    )
                    cluster["center"] = sum(
                        item["position_cm-1"] for item in cluster["observations"]
                    ) / len(cluster["observations"])

        profile_material_systems = {
            sample["material_system"].casefold()
            for sample in samples
            if sample["material_system"].casefold() != "unknown"
        }
        learned_bands = []
        for cluster in clusters:
            observations = cluster["observations"]
            material_systems = {
                item["material_system"].casefold()
                for item in observations
                if item["material_system"].casefold() != "unknown"
            }
            recurrence_fraction = (
                len(observations) / len(samples) if samples else 0.0
            )
            distinct_material_support = (
                len(observations) >= 2 and len(material_systems) >= 2
            )
            same_material_replicate_support = (
                len(profile_material_systems) == 1
                and len(observations) >= same_material_minimum_samples
                and recurrence_fraction
                >= same_material_minimum_recurrence_fraction
            )
            if not (
                distinct_material_support or same_material_replicate_support
            ):
                continue
            if any(
                item["has_material_evidence"] for item in observations
            ):
                continue
            center = median(
                [item["position_cm-1"] for item in observations]
            )
            if any(
                band["range_cm-1"][0] <= center <= band["range_cm-1"][1]
                for band in SUBSTRATE_BANDS[substrate]
            ):
                continue
            learned_bands.append(
                {
                    "range_cm-1": [center - tolerance, center + tolerance],
                    "label": f"Cross-sample substrate feature near {center:.1f} cm-1",
                    "weight": min(0.15, 0.05 * len(observations)),
                    "evidence_source": "user_confirmed_cross_sample",
                    "support_basis": (
                        "distinct_material_systems"
                        if distinct_material_support
                        else "same_material_replicates"
                    ),
                    "recurrence_fraction": recurrence_fraction,
                    "supporting_file_ids": [
                        item["file_id"] for item in observations
                    ],
                    "observed_centers_cm-1": [
                        item["position_cm-1"] for item in observations
                    ],
                }
            )
        backgrounds = [
            sample["background"] for sample in samples if sample["background"]
        ]
        profiles[substrate] = {
            "method": "confirmed_substrate_cross_sample_recurrence",
            "sample_count": len(samples),
            "source_file_ids": [sample["file_id"] for sample in samples],
            "source_result_sha256": [sample["result_sha256"] for sample in samples],
            "required_distinct_material_systems": 2,
            "same_material_replicate_rule": {
                "eligible_only_when_profile_has_one_material_system": True,
                "minimum_samples": same_material_minimum_samples,
                "minimum_recurrence_fraction": (
                    same_material_minimum_recurrence_fraction
                ),
                "exclude_peaks_with_specific_material_evidence": True,
            },
            "learned_bands": learned_bands,
            "background_effect": (
                {
                    "sample_count": len(backgrounds),
                    "median_level_fraction": median(
                        item["level_fraction"] for item in backgrounds
                    ),
                    "median_slope_fraction": median(
                        item["slope_fraction"] for item in backgrounds
                    ),
                    "use": "diagnostic_prior_only",
                }
                if backgrounds
                else None
            ),
        }
    return profiles


def imported_substrate_reference_profiles(raw_data_directory: Path) -> dict:
    """Extract noise-gated peak templates from confirmed pure substrates."""
    profiles = {
        "glass": {"reference_spectra": [], "learned_bands": []},
        "sio2_si": {"reference_spectra": [], "learned_bands": []},
    }
    tolerance = float(
        MODEL_PARAMETERS["substrate_reference_position_tolerance_cm-1"]
    )
    minimum_snr = float(MODEL_PARAMETERS["substrate_reference_noise_multiplier"])
    for imported_file in database.list_imported_files():
        substrate = confirmed_substrate_reference(imported_file)
        if substrate is None:
            continue
        try:
            source_text, source_sha256 = preview.read_verified_text(
                imported_file,
                raw_data_directory,
            )
            series = preview.extract_numeric_series(source_text)
            points, _input_order = normalize_x_order(series["points"])
        except (preview.PreviewError, ProcessingError):
            logger.warning(
                "Skipping an invalid pure-substrate reference spectrum",
                exc_info=True,
            )
            continue
        x_values = [point[0] for point in points]
        raw_values = [point[1] for point in points]
        rayleigh_corrected, rayleigh_artifacts = detect_and_correct_rayleigh_line(
            x_values,
            raw_values,
            MODEL_PARAMETERS,
        )
        artifact_corrected, cosmic_artifacts = detect_and_correct_cosmic_spikes(
            x_values,
            rayleigh_corrected,
            MODEL_PARAMETERS,
        )
        baseline = estimate_baseline(
            artifact_corrected,
            x_values,
            MODEL_PARAMETERS,
        )
        corrected = [
            intensity - baseline_value
            for intensity, baseline_value in zip(artifact_corrected, baseline)
        ]
        differences = [
            abs(corrected[index] - corrected[index - 1])
            for index in range(1, len(corrected))
        ]
        noise = median(differences) if differences else 0.0
        candidates = merge_peak_candidates(
            detect_peaks(x_values, corrected),
            broad_substrate_peak_candidates(
                x_values,
                corrected,
                substrate,
                MODEL_PARAMETERS,
                None,
                "substrate_reference_broad_search",
            ),
        )
        candidates = [
            peak
            for peak in candidates
            if float(peak.get("prominence") or 0.0)
            >= noise * minimum_snr
            and float(peak["position_cm-1"]) >= 50.0
        ]
        for confirmation in confirmed_peak_assignments(imported_file["file_id"]):
            confirmed_position = float(confirmation["observed_cm-1"])
            if any(
                abs(float(peak["position_cm-1"]) - confirmed_position) <= 4.0
                for peak in candidates
            ):
                continue
            nearest_index = min(
                range(len(x_values)),
                key=lambda index: abs(x_values[index] - confirmed_position),
            )
            local_radius = 12
            left = corrected[max(0, nearest_index - local_radius) : nearest_index]
            right = corrected[
                nearest_index + 1 : nearest_index + local_radius + 1
            ]
            prominence = (
                float(corrected[nearest_index]) - max(min(left), min(right))
                if left and right
                else 0.0
            )
            candidates.append(
                {
                    "position_cm-1": confirmed_position,
                    "intensity": float(corrected[nearest_index]),
                    "prominence": max(0.0, prominence),
                    "user_confirmed_substrate_peak": True,
                    "confirmation_label": confirmation.get("label"),
                }
            )
        maximum_prominence = max(
            (float(peak["prominence"]) for peak in candidates),
            default=0.0,
        )
        reference_peaks = []
        for peak in candidates:
            position = float(peak["position_cm-1"])
            prominence = float(peak["prominence"])
            relative_prominence = (
                prominence / maximum_prominence if maximum_prominence > 0 else 0.0
            )
            reference_peak = {
                "position_cm-1": position,
                "intensity": float(peak["intensity"]),
                "prominence": prominence,
                "signal_to_noise": prominence / noise if noise > 0 else None,
                "relative_prominence": relative_prominence,
                "user_confirmed": bool(
                    peak.get("user_confirmed_substrate_peak")
                ),
            }
            reference_peaks.append(reference_peak)
            profiles[substrate]["learned_bands"].append(
                {
                    "range_cm-1": [position - tolerance, position + tolerance],
                    "label": (
                        "Measured SiO2 / crystalline Si substrate peak near "
                        f"{position:.1f} cm-1"
                        if substrate == "sio2_si"
                        else f"Measured glass substrate peak near {position:.1f} cm-1"
                    ),
                    "weight": (
                        0.1
                        if peak.get("user_confirmed_substrate_peak")
                        and prominence < noise * minimum_snr
                        else min(
                            0.3,
                            0.12 + 0.18 * math.sqrt(relative_prominence),
                        )
                    ),
                    "evidence_source": "user_confirmed_substrate_reference",
                    "source_file_id": imported_file["file_id"],
                    "source_sha256": source_sha256,
                    "reference_center_cm-1": position,
                    "reference_relative_prominence": relative_prominence,
                    "reference_signal_to_noise": reference_peak["signal_to_noise"],
                    "reference_noise_status": (
                        "below_threshold_user_confirmed"
                        if peak.get("user_confirmed_substrate_peak")
                        and prominence < noise * minimum_snr
                        else "noise_gated"
                    ),
                    "position_tolerance_cm-1": tolerance,
                    "intensity_comparison": "relative_prominence_only",
                }
            )
        profiles[substrate]["reference_spectra"].append(
            {
                "file_id": imported_file["file_id"],
                "filename": imported_file["original_filename"],
                "sample_id": imported_file.get("sample_id"),
                "source_sha256": source_sha256,
                "noise_estimate": noise,
                "noise_method": "median_absolute_adjacent_difference",
                "minimum_signal_to_noise": minimum_snr,
                "artifact_count": len(rayleigh_artifacts) + len(cosmic_artifacts),
                "peaks": reference_peaks,
            }
        )
    return profiles


def imported_substrate_reference_traces(
    raw_data_directory: Path,
    baseline_method: str = "morphological_opening",
) -> dict:
    """Load complete corrected traces for target-adaptive substrate fitting."""
    traces = {"glass": [], "sio2_si": []}
    for imported_file in database.list_imported_files():
        substrate = confirmed_substrate_reference(imported_file)
        if substrate is None:
            continue
        try:
            source_text, source_sha256 = preview.read_verified_text(
                imported_file,
                raw_data_directory,
            )
            series = preview.extract_numeric_series(source_text)
            points, _input_order = normalize_x_order(series["points"])
        except (preview.PreviewError, ProcessingError):
            logger.warning(
                "Skipping an invalid full-trace substrate reference",
                exc_info=True,
            )
            continue
        x_values = [float(point[0]) for point in points]
        raw_values = [float(point[1]) for point in points]
        rayleigh_corrected, rayleigh_artifacts = detect_and_correct_rayleigh_line(
            x_values,
            raw_values,
            MODEL_PARAMETERS,
        )
        artifact_corrected, cosmic_artifacts = detect_and_correct_cosmic_spikes(
            x_values,
            rayleigh_corrected,
            MODEL_PARAMETERS,
        )
        baseline = estimate_baseline(
            artifact_corrected,
            x_values,
            {**MODEL_PARAMETERS, "baseline_method": baseline_method},
        )
        corrected = [
            intensity - baseline_value
            for intensity, baseline_value in zip(artifact_corrected, baseline)
        ]
        differences = [
            abs(corrected[index] - corrected[index - 1])
            for index in range(1, len(corrected))
        ]
        traces[substrate].append(
            {
                "file_id": imported_file["file_id"],
                "filename": imported_file["original_filename"],
                "sample_id": imported_file.get("sample_id"),
                "source_sha256": source_sha256,
                "x_values": x_values,
                "corrected_values": corrected,
                "noise_estimate": median(differences) if differences else 0.0,
                "artifact_count": len(rayleigh_artifacts) + len(cosmic_artifacts),
            }
        )
    return traces


def combined_substrate_profiles(
    raw_data_directory: Path,
    processed_directory: Path,
) -> dict:
    profiles = cross_sample_substrate_profiles(processed_directory)
    references = imported_substrate_reference_profiles(raw_data_directory)
    for substrate, reference_profile in references.items():
        profile = profiles[substrate]
        profile["method"] = (
            "confirmed_substrate_cross_sample_and_reference_spectrum"
        )
        profile["reference_spectra"] = reference_profile["reference_spectra"]
        profile["learned_bands"].extend(reference_profile["learned_bands"])
        profile["reference_transfer"] = {
            "noise_gated": True,
            "absolute_intensity_required": False,
            "intensity_method": "within-spectrum_relative_prominence",
            "position_tolerance_cm-1": MODEL_PARAMETERS[
                "substrate_reference_position_tolerance_cm-1"
            ],
            "material_overlap_rule": "specific_non_substrate_assignment_wins",
        }
    return profiles


def recurring_unassigned_peak_profiles(
    current_file_id: str,
    material_system: str | None,
    processed_directory: Path,
) -> dict:
    """Collect recurring unassigned peaks from other same-system spectra."""
    material_components = declared_material_components(material_system)
    profile = {
        "method": "same_material_system_unassigned_peak_recurrence",
        "material_system": material_system,
        "comparison_file_count": 0,
        "comparison_files": [],
        "clusters": [],
    }
    if not material_components:
        return profile

    clusters = []
    tolerance = float(MODEL_PARAMETERS["recurring_unassigned_peak_tolerance_cm-1"])
    for imported_file in database.list_imported_files():
        if imported_file["file_id"] == current_file_id:
            continue
        if declared_material_components(
            imported_file.get("material_system")
        ) != material_components:
            continue
        processing_run = database.get_latest_processing_run(
            imported_file["file_id"]
        )
        if processing_run is None:
            continue
        try:
            result = load_processing_result(processing_run, processed_directory)
        except ProcessingError:
            logger.warning(
                "Skipping invalid processing result in recurring-peak review",
                exc_info=True,
            )
            continue
        profile["comparison_file_count"] += 1
        profile["comparison_files"].append(
            {
                "file_id": imported_file["file_id"],
                "peak_positions_cm-1": [
                    round(float(peak["position_cm-1"]), 3)
                    for peak in result.get("peaks", [])
                    if peak.get("position_cm-1") is not None
                ],
            }
        )
        for peak in result.get("summary", {}).get("unassigned_peaks", []):
            try:
                position = float(peak["position_cm-1"])
            except (KeyError, TypeError, ValueError):
                continue
            cluster = min(
                clusters,
                key=lambda item: abs(item["mean_position_cm-1"] - position),
                default=None,
            )
            if cluster is None or abs(
                cluster["mean_position_cm-1"] - position
            ) > tolerance:
                clusters.append(
                    {
                        "mean_position_cm-1": position,
                        "positions_cm-1": [position],
                        "file_ids": [imported_file["file_id"]],
                    }
                )
                continue
            if imported_file["file_id"] in cluster["file_ids"]:
                continue
            cluster["positions_cm-1"].append(position)
            cluster["file_ids"].append(imported_file["file_id"])
            cluster["mean_position_cm-1"] = sum(
                cluster["positions_cm-1"]
            ) / len(cluster["positions_cm-1"])

    minimum_prior_observations = max(
        1,
        int(MODEL_PARAMETERS["recurring_unassigned_peak_minimum_spectra"]) - 1,
    )
    profile["clusters"] = [
        {
            "mean_position_cm-1": round(cluster["mean_position_cm-1"], 3),
            "positions_cm-1": [
                round(position, 3) for position in cluster["positions_cm-1"]
            ],
            "observed_count": len(cluster["file_ids"]),
            "file_ids": cluster["file_ids"],
        }
        for cluster in clusters
        if len(cluster["file_ids"]) >= minimum_prior_observations
    ]
    profile["clusters"].sort(key=lambda item: item["mean_position_cm-1"])
    return profile


def latest_confirmed_peak_edits(file_id: str) -> list[dict]:
    latest_by_position = {}
    for record in database.list_clarification_responses(file_id):
        if record["status"] != "answered" or not record["question_key"].startswith(
            "unassigned_peak:"
        ):
            continue
        try:
            response = json.loads(record["response_json"])
        except json.JSONDecodeError:
            continue
        assignment = response.get("confirmed_peak_assignment")
        if not isinstance(assignment, dict):
            continue
        try:
            position_key = round(float(assignment["observed_cm-1"]), 3)
        except (KeyError, TypeError, ValueError):
            continue
        latest_by_position[position_key] = {
            **assignment,
            "clarification_id": record["clarification_id"],
            "confirmed_at": record["created_at"],
        }
    return list(latest_by_position.values())


def confirmed_peak_assignments(file_id: str) -> list[dict]:
    return [
        assignment
        for assignment in latest_confirmed_peak_edits(file_id)
        if not is_instrument_artifact_confirmation(assignment)
    ]


def confirmed_instrument_artifacts(file_id: str) -> list[dict]:
    return [
        assignment
        for assignment in latest_confirmed_peak_edits(file_id)
        if is_instrument_artifact_confirmation(assignment)
    ]


def confirmed_noise_exclusions(file_id: str) -> list[dict]:
    latest_by_position = {}
    for record in database.list_clarification_responses(file_id):
        if record["status"] != "answered" or not record["question_key"].startswith(
            "unassigned_peak:"
        ):
            continue
        try:
            response = json.loads(record["response_json"])
        except json.JSONDecodeError:
            continue
        exclusion = response.get("confirmed_noise_exclusion")
        if not isinstance(exclusion, dict):
            continue
        try:
            position_key = round(float(exclusion["observed_cm-1"]), 3)
        except (KeyError, TypeError, ValueError):
            continue
        latest_by_position[position_key] = {
            **exclusion,
            "clarification_id": record["clarification_id"],
            "confirmed_at": record["created_at"],
        }
    return list(latest_by_position.values())


def confirmed_peak_training_references() -> list[dict]:
    """Expose latest user-confirmed peak edits as label-only training evidence."""
    latest_by_peak = {}
    for record in database.list_all_clarification_responses():
        if record["status"] != "answered" or not record["question_key"].startswith(
            "unassigned_peak:"
        ):
            continue
        try:
            response = json.loads(record["response_json"])
        except json.JSONDecodeError:
            continue
        assignment = response.get("confirmed_peak_assignment")
        if not isinstance(assignment, dict):
            continue
        if is_instrument_artifact_confirmation(assignment):
            continue
        try:
            position = float(assignment["observed_cm-1"])
        except (KeyError, TypeError, ValueError):
            continue
        material_system = " ".join(
            str(assignment.get("material_system") or "").split()
        )
        label = " ".join(str(assignment.get("label") or "").split())
        if not material_system or not label:
            continue
        latest_by_peak[(record["file_id"], round(position, 3))] = {
            "record": record,
            "position": position,
            "material_system": material_system,
            "label": label,
            "role": assignment.get("role"),
            "related_references": assignment.get("related_references") or [],
        }

    references = []
    for item in latest_by_peak.values():
        record = item["record"]
        peak = {
            "position_cm-1": item["position"],
            "assignment": item["label"],
        }
        if item["role"]:
            peak["role"] = item["role"]
        evidence = {
            "provider": "user_confirmed_peak_training",
            "training_scope": "peak_assignment_only",
            "match_tolerance_cm-1": 6.0,
            "peaks": [peak],
            "status": "ready",
            "source_file_id": record["file_id"],
            "source_processing_id": record["processing_id"],
            "clarification_id": record["clarification_id"],
        }
        if item["related_references"]:
            evidence["related_references"] = item["related_references"]
        identity = canonical_json(
            {
                "material_system": item["material_system"],
                "label": item["label"],
                "position_cm-1": item["position"],
                "evidence": evidence,
            }
        ).encode("utf-8")
        references.append(
            {
                "reference_id": (
                    f"peak-training-{record['clarification_id']}"
                ),
                "original_filename": None,
                "content_type": "application/vnd.materials.peak-training+json",
                "size_bytes": len(identity),
                "sha256": hashlib.sha256(identity).hexdigest(),
                "storage_path": None,
                "imported_at": record["created_at"],
                "source_kind": "user_confirmed_peak_assignment",
                "technique": "Raman",
                "material_system": item["material_system"],
                "title": "User-confirmed Raman peak assignment",
                "citation": (
                    "Private user-confirmed component edit; source file and "
                    "processing provenance retained."
                ),
                "source_url": None,
                "extraction_status": "ready",
                "evidence_json": canonical_json(evidence),
            }
        )
    return references


def cleanup_result(destination_path: Path, destination_directory: Path) -> None:
    if destination_path.exists():
        destination_path.unlink()
    if destination_directory.exists():
        destination_directory.rmdir()


def process_imported_file(
    metadata: dict,
    raw_data_directory: Path,
    processed_directory: Path,
    reference_data_directory: Path | None = None,
    deconvolution_profile: str = "none",
    substrate_correction_mode: str = "none",
    wdf_dataset_index: int = 0,
    baseline_method: str = "morphological_opening",
    advanced_options: dict | None = None,
) -> dict:
    raw_data_directory = raw_data_directory.resolve()
    processed_directory = processed_directory.resolve()
    try:
        text, source_sha256 = preview.read_verified_text(
            metadata,
            raw_data_directory,
            wdf_dataset_index,
        )
    except preview.PreviewError as error:
        raise ProcessingError(
            error.status_code,
            error.code,
            error.message,
        ) from error

    if deconvolution_profile not in DECONVOLUTION_PROFILES:
        raise ProcessingError(
            422,
            "invalid_deconvolution_profile",
            "Deconvolution must be none, gaussian, lorentzian, or pseudo_voigt.",
        )
    if substrate_correction_mode not in SUBSTRATE_CORRECTION_MODES:
        raise ProcessingError(
            422,
            "invalid_substrate_correction_mode",
            (
                "Substrate correction must be none, detect, auto, glass, "
                "or sio2_si."
            ),
        )
    if baseline_method not in BASELINE_METHODS:
        raise ProcessingError(
            422,
            "invalid_baseline_method",
            "Baseline method must be morphological_opening or rubber_band.",
        )
    validated_advanced_options = validate_advanced_processing_options(
        advanced_options
    )

    reference_sources = database.list_reference_sources("Raman")
    from copilot import training_references
    from online_references import curated_references, load_synced_rod_references

    reference_sources.extend(training_references(metadata["file_id"]))
    reference_sources.extend(curated_references())
    reference_sources.extend(confirmed_peak_training_references())
    if reference_data_directory is not None:
        reference_sources.extend(
            load_synced_rod_references(reference_data_directory.resolve())
        )
    substrate_profiles = combined_substrate_profiles(
        raw_data_directory,
        processed_directory,
    )
    substrate_reference_traces = (
        {"glass": [], "sio2_si": []}
        if validated_advanced_options["substrate_reference_strategy"]
        == "analytical_only"
        else imported_substrate_reference_traces(
            raw_data_directory,
            baseline_method,
        )
    )
    substrate_trace_library_identity = {
        substrate: [
            {
                "file_id": trace["file_id"],
                "source_sha256": trace["source_sha256"],
            }
            for trace in traces
        ]
        for substrate, traces in substrate_reference_traces.items()
    }
    recurring_peak_profiles = recurring_unassigned_peak_profiles(
        metadata["file_id"],
        metadata.get("material_system"),
        processed_directory,
    )
    peak_confirmations = confirmed_peak_assignments(metadata["file_id"])
    artifact_confirmations = confirmed_instrument_artifacts(metadata["file_id"])
    noise_confirmations = confirmed_noise_exclusions(metadata["file_id"])
    stored_substrate_feedback = database.list_substrate_peak_feedback(
        material_system=metadata.get("material_system"),
    )
    latest_substrate_feedback = {}
    for feedback in stored_substrate_feedback:
        latest_substrate_feedback[
            (
                feedback["substrate"],
                round(float(feedback["center_cm_1"]), 3),
            )
        ] = feedback
    substrate_peak_feedback = list(latest_substrate_feedback.values())
    material_confirmation = database.get_active_training_example(
        metadata["file_id"]
    )
    confirmation_identity = None
    if material_confirmation is not None:
        confirmation_identity = {
            key: material_confirmation[key]
            for key in (
                "training_id",
                "material_system",
                "source_sha256",
                "result_sha256",
                "confirmed_at",
                "confirmed_by",
            )
        }
    parameters = {
        **MODEL_PARAMETERS,
        **validated_advanced_options,
        "baseline_method": baseline_method,
        "deconvolution_profile": deconvolution_profile,
        "substrate_correction_mode": substrate_correction_mode,
        "confirmed_substrate": (
            metadata.get("substrate")
            if metadata.get("substrate") in {"glass", "sio2_si"}
            else None
        ),
        "import_metadata": {
            key: metadata.get(key)
            for key in (
                "file_id",
                "technique",
                "material_system",
                "sample_id",
                "substrate",
                "measurement_role",
                "updated_at",
            )
        },
        "substrate_profiles": substrate_profiles,
        "substrate_trace_library_sha256": hashlib.sha256(
            canonical_json(substrate_trace_library_identity).encode("utf-8")
        ).hexdigest(),
        "recurring_unassigned_peaks": recurring_peak_profiles,
        "confirmed_peak_assignments": peak_confirmations,
        "confirmed_instrument_artifacts": artifact_confirmations,
        "confirmed_noise_exclusions": noise_confirmations,
        "substrate_peak_feedback": substrate_peak_feedback,
        "material_confirmation": confirmation_identity,
        "reference_library_sha256": reference_library_snapshot(
            reference_sources
        ),
    }
    if Path(metadata["original_filename"]).suffix.lower() == ".wdf":
        parameters["wdf_dataset_index"] = wdf_dataset_index
    parameters_json = canonical_json(parameters)
    with database.connect_database() as connection:
        existing_run = database.find_processing_run(
            connection,
            metadata["file_id"],
            source_sha256,
            MODEL_NAME,
            MODEL_VERSION,
            parameters_json,
        )
    if existing_run is not None:
        existing_result = load_processing_result(existing_run, processed_directory)
        if has_compatible_processing_payload(existing_result):
            return existing_result

    processing_metadata = {
        **metadata,
        "material_confirmation": material_confirmation,
    }
    result = build_raman_result(
        processing_metadata,
        text,
        source_sha256,
        parameters,
        reference_sources,
        substrate_reference_traces,
    )
    result_bytes = canonical_json(result).encode("utf-8")
    result_sha256 = hashlib.sha256(result_bytes).hexdigest()
    staging_path = None
    destination_directory = None
    destination_path = None
    destination_created = False

    try:
        staging_directory = processed_directory / ".staging"
        staging_directory.mkdir(parents=True, exist_ok=True)
        staging_path = staging_directory / f"{result['processing_id']}.json"
        with staging_path.open("xb") as staged_result:
            staged_result.write(result_bytes)

        destination_directory = (
            processed_directory
            / metadata["file_id"]
            / result["processing_id"]
        )
        destination_path = destination_directory / "result.json"
        processing_run = {
            "processing_id": result["processing_id"],
            "file_id": metadata["file_id"],
            "model_name": MODEL_NAME,
            "model_version": MODEL_VERSION,
            "parameters_json": parameters_json,
            "source_sha256": source_sha256,
            "result_path": str(destination_path),
            "result_sha256": result_sha256,
            "processed_at": result["processed_at"],
            "summary_json": canonical_json(result["summary"]),
        }

        with database.connect_database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_run = database.find_processing_run(
                connection,
                metadata["file_id"],
                source_sha256,
                MODEL_NAME,
                MODEL_VERSION,
                parameters_json,
            )
            if existing_run is not None:
                existing_result = load_processing_result(
                    existing_run,
                    processed_directory,
                )
                if has_compatible_processing_payload(existing_result):
                    return existing_result
                database.delete_processing_run(connection, existing_run["processing_id"])

            destination_directory.mkdir(parents=True, exist_ok=False)
            destination_created = True
            os.replace(staging_path, destination_path)
            database.insert_processing_run(connection, processing_run)

        result["result_sha256"] = result_sha256
        return result
    except ProcessingError:
        if destination_created:
            cleanup_result(destination_path, destination_directory)
        raise
    except (OSError, sqlite3.Error) as error:
        if destination_created:
            try:
                cleanup_result(destination_path, destination_directory)
            except OSError:
                raise ProcessingError(
                    500,
                    "processing_cleanup_failed",
                    "Processing failed and derived-result cleanup was incomplete.",
                ) from error
        raise ProcessingError(
            500,
            "processing_failed",
            "Processing failed; no derived result was retained.",
        ) from error
    finally:
        if staging_path is not None and staging_path.exists():
            try:
                staging_path.unlink()
            except OSError:
                logger.exception("Failed to remove a staged processing result")


def latest_processing_result(file_id: str, processed_directory: Path) -> dict:
    processed_directory = processed_directory.resolve()
    processing_run = database.get_latest_processing_run(file_id)
    if processing_run is None:
        raise ProcessingError(
            404,
            "processing_not_found",
            "This file has not been processed yet.",
        )
    return load_processing_result(processing_run, processed_directory)


def _interpolated_value(points: list[list[float]], x_value: float) -> float:
    if x_value <= points[0][0]:
        return float(points[0][1])
    if x_value >= points[-1][0]:
        return float(points[-1][1])
    low = 0
    high = len(points) - 1
    while high - low > 1:
        middle = (low + high) // 2
        if points[middle][0] <= x_value:
            low = middle
        else:
            high = middle
    x_low, y_low = points[low]
    x_high, y_high = points[high]
    fraction = (x_value - x_low) / (x_high - x_low)
    return float(y_low + fraction * (y_high - y_low))


def _spectral_correlation(first: dict, second: dict) -> dict:
    def comparison_series(result: dict) -> list[list[float]]:
        series = result["series"]
        return series.get("substrate_corrected") or series["corrected"]

    first_points = comparison_series(first)
    second_points = comparison_series(second)
    lower = max(float(first_points[0][0]), float(second_points[0][0]))
    upper = min(float(first_points[-1][0]), float(second_points[-1][0]))
    if upper <= lower:
        return {"correlation": None, "overlap_cm-1": None}
    sample_count = min(512, max(32, len(first_points), len(second_points)))
    step = (upper - lower) / (sample_count - 1)
    first_values = []
    second_values = []
    for index in range(sample_count):
        x_value = lower + index * step
        first_values.append(_interpolated_value(first_points, x_value))
        second_values.append(_interpolated_value(second_points, x_value))
    first_mean = sum(first_values) / sample_count
    second_mean = sum(second_values) / sample_count
    numerator = sum(
        (first_value - first_mean) * (second_value - second_mean)
        for first_value, second_value in zip(first_values, second_values)
    )
    first_power = sum((value - first_mean) ** 2 for value in first_values)
    second_power = sum((value - second_mean) ** 2 for value in second_values)
    denominator = math.sqrt(first_power * second_power)
    correlation = numerator / denominator if denominator > 0 else None
    return {
        "correlation": round(correlation, 6) if correlation is not None else None,
        "overlap_cm-1": [round(lower, 3), round(upper, 3)],
        "sample_count": sample_count,
    }


def compare_processing_results(results: list[dict]) -> dict:
    """Compare derived spectra without modifying or replacing individual results."""
    pairwise = []
    for first_index, first in enumerate(results):
        for second in results[first_index + 1 :]:
            correlation = _spectral_correlation(first, second)
            pairwise.append(
                {
                    "first_file_id": first["file_id"],
                    "second_file_id": second["file_id"],
                    **correlation,
                }
            )

    peak_clusters = []
    for result in results:
        for peak in result.get("peaks", []):
            position = float(peak["position_cm-1"])
            cluster = next(
                (
                    candidate
                    for candidate in peak_clusters
                    if abs(candidate["mean_position_cm-1"] - position) <= 8.0
                ),
                None,
            )
            if cluster is None:
                cluster = {
                    "positions": [],
                    "file_ids": [],
                    "assignments": set(),
                    "mean_position_cm-1": position,
                }
                peak_clusters.append(cluster)
            if result["file_id"] in cluster["file_ids"]:
                continue
            cluster["positions"].append(position)
            cluster["file_ids"].append(result["file_id"])
            cluster["mean_position_cm-1"] = sum(cluster["positions"]) / len(
                cluster["positions"]
            )
            for assignment in peak.get("assignments", []):
                material = assignment.get("material_system")
                label = assignment.get("label")
                if material and label:
                    cluster["assignments"].add(f"{material}: {label}")

    compared_count = len(results)
    clusters = []
    for cluster in peak_clusters:
        observed_count = len(cluster["file_ids"])
        clusters.append(
            {
                "mean_position_cm-1": round(cluster["mean_position_cm-1"], 3),
                "range_cm-1": [
                    round(min(cluster["positions"]), 3),
                    round(max(cluster["positions"]), 3),
                ],
                "observed_count": observed_count,
                "prevalence": round(observed_count / compared_count, 4),
                "file_ids": cluster["file_ids"],
                "assignments": sorted(cluster["assignments"]),
            }
        )
    clusters.sort(key=lambda item: item["mean_position_cm-1"])

    material_groups = {}
    for result in results:
        material = result["summary"]["identification"].get(
            "material_system"
        ) or "Unknown"
        components = declared_material_components(material)
        if "sio2" in components:
            components.discard("si")
        identity = tuple(sorted(components)) or ("unknown",)
        group = material_groups.setdefault(
            identity,
            {"count": 0, "labels": set()},
        )
        group["count"] += 1
        group["labels"].add(material)
    public_material_groups = [
        {
            "components": list(identity),
            "count": group["count"],
            "labels": sorted(group["labels"]),
        }
        for identity, group in material_groups.items()
    ]
    public_material_groups.sort(
        key=lambda group: (-group["count"], group["components"])
    )
    return {
        "method": {
            "spectral_similarity": (
                "Pearson correlation of linearly interpolated baseline-corrected "
                "spectra over their common Raman-shift range; substrate-corrected "
                "series are used when available"
            ),
            "peak_clustering_tolerance_cm-1": 8.0,
            "smoothing": "none",
        },
        "compared_count": compared_count,
        "material_counts": {
            " / ".join(group["components"]): group["count"]
            for group in public_material_groups
        },
        "material_groups": public_material_groups,
        "pairwise_similarity": pairwise,
        "recurring_peaks": [
            cluster for cluster in clusters if cluster["observed_count"] >= 2
        ],
        "variable_peaks": [
            cluster
            for cluster in clusters
            if cluster["observed_count"] < compared_count
        ],
    }
