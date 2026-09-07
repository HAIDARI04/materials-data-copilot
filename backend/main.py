import hashlib
import json
import logging
import math
import mimetypes
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel

import ai_providers
import clarifications
import comparison
import copilot
import database
import jcamp_exporter
import integrity
import online_references
import preview
import processing
import references
import reproducibility
import wdf_reader

logger = logging.getLogger(__name__)


RAW_DATA_DIR = Path(__file__).parent / "data" / "raw"
PROCESSED_DATA_DIR = Path(__file__).parent / "data" / "processed"
REFERENCE_DATA_DIR = Path(__file__).parent / "data" / "references"
UPLOAD_INTERFACE_PATH = Path(__file__).parent / "static" / "upload.html"
COMPARISON_INTERFACE_PATH = Path(__file__).parent / "static" / "comparison.html"
CHUNK_SIZE = 1024 * 1024  # 1 MiB
MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024  # 100 MiB
MAX_REFERENCE_SIZE_BYTES = 20 * 1024 * 1024  # 20 MiB
MAX_OPTICAL_IMAGE_SIZE_BYTES = 25 * 1024 * 1024  # 25 MiB
MAX_FILENAME_LENGTH = 200
APP_VERSION = "0.8.0-dev"


class CompareSpectraRequest(BaseModel):
    model_config = {"extra": "forbid"}

    sample_file_id: str
    reference_file_id: str
    normalization: str = "vector"


class DeconvolutionPeakSelectionRequest(BaseModel):
    model_config = {"extra": "forbid"}

    center_cm_1: float
    label: str | None = None
    tolerance_cm_1: float = 12.0


class SpectraOverlayRequest(BaseModel):
    model_config = {"extra": "forbid"}

    file_ids: list[str]
    normalization: str = "max"
    series_name: str = "raw"
    offset: float = 0.0
    peak_selections: list[DeconvolutionPeakSelectionRequest] | None = None


class DeconvolutionPeakCatalogRequest(BaseModel):
    model_config = {"extra": "forbid"}

    file_ids: list[str]


class CopilotChatRequest(BaseModel):
    message: str
    file_id: str | None = None
    provider: str = "local"
    share_spectrum_context: bool = False


class CopilotTrainingRequest(BaseModel):
    file_id: str
    material_system: str


class ProcessingRequest(BaseModel):
    deconvolution: str = "none"
    substrate_correction: str = "none"
    dataset_index: int = 0
    baseline_method: str = "morphological_opening"
    substrate_reference_strategy: str = "automatic"
    quality_policy: str = "balanced"
    peak_review_policy: str = "withhold_uncertain"
    ensemble_minimum_coverage: float = 0.95
    ensemble_consensus_threshold: float = 0.6
    ensemble_minimum_r_squared: float = 0.75
    analysis_range_min_cm_1: float | None = None
    analysis_range_max_cm_1: float | None = None
    peak_detection_threshold_percent: float = 2.0
    normalization_mode: str = "none"
    normalization_peak_cm_1: float | None = None


class BatchProcessingRequest(BaseModel):
    model_config = {"extra": "forbid"}

    file_ids: list[str]
    deconvolution: str = "none"
    substrate_correction: str = "none"
    baseline_method: str = "morphological_opening"
    substrate_reference_strategy: str = "automatic"
    quality_policy: str = "balanced"
    peak_review_policy: str = "withhold_uncertain"
    ensemble_minimum_coverage: float = 0.95
    ensemble_consensus_threshold: float = 0.6
    ensemble_minimum_r_squared: float = 0.75
    analysis_range_min_cm_1: float | None = None
    analysis_range_max_cm_1: float | None = None
    peak_detection_threshold_percent: float = 2.0
    normalization_mode: str = "none"
    normalization_peak_cm_1: float | None = None


class BatchArchiveRequest(BaseModel):
    model_config = {"extra": "forbid"}

    file_ids: list[str]


class SubstrateResidualFeedbackRequest(BaseModel):
    model_config = {"extra": "forbid"}

    center_cm_1: float
    half_width_cm_1: float = 8.0
    action: str


class ClarificationResponseRequest(BaseModel):
    question_key: str
    answer: str | None = None
    value: str | None = None
    dismissed: bool = False


class PeakAssignmentConfirmationRequest(BaseModel):
    model_config = {"extra": "forbid"}

    processing_id: str
    component_kind: str
    position_cm_1: float
    material_system: str
    label: str
    learning_confirmed: bool = False
    related_reference_ids: list[str] | None = None


class ImportMetadataUpdateRequest(BaseModel):
    model_config = {"extra": "forbid"}

    technique: str | None = None
    material_system: str | None = None
    sample_id: str | None = None
    measurement_date: str | None = None
    instrument: str | None = None
    operator: str | None = None
    notes: str | None = None
    substrate: str | None = None
    measurement_role: str | None = None


class SampleRequest(BaseModel):
    model_config = {"extra": "forbid"}

    sample_id: str
    material_system: str | None = None
    substrate: str | None = None
    project: str | None = None
    notes: str | None = None


class ConfigRecordRequest(BaseModel):
    model_config = {"extra": "forbid"}

    name: str
    config: dict


@asynccontextmanager
async def lifespan(_app: FastAPI):
    database.initialize_database()
    yield


app = FastAPI(
    title="Materials Data Copilot API",
    version=APP_VERSION,
    lifespan=lifespan,
)


@app.middleware("http")
async def add_browser_security_headers(request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    )
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
        "connect-src 'self'; object-src 'none'; base-uri 'self'; "
        "frame-ancestors 'self'; form-action 'self'",
    )
    return response


def validation_error(code: str, message: str, status_code: int = 422):
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )


def extract_original_filename(filename: str | None) -> str:
    if not filename or not filename.strip():
        raise validation_error("invalid_filename", "A filename is required.")

    original_filename = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if original_filename in {"", ".", ".."}:
        raise validation_error("invalid_filename", "A valid filename is required.")
    return original_filename


def sanitize_filename(original_filename: str) -> str:
    sanitized = re.sub(
        r'[\x00-\x1f<>:"/\\|?*]+',
        "_",
        original_filename,
    )
    sanitized = sanitized.strip(" .")

    if not sanitized or sanitized in {".", ".."}:
        raise validation_error(
            "invalid_filename",
            "The filename does not contain any safe characters.",
        )

    reserved_names = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }
    if sanitized.split(".", 1)[0].upper() in reserved_names:
        sanitized = f"_{sanitized}"

    if len(sanitized) > MAX_FILENAME_LENGTH:
        suffix = Path(sanitized).suffix[:20]
        stem_length = MAX_FILENAME_LENGTH - len(suffix)
        sanitized = f"{Path(sanitized).stem[:stem_length]}{suffix}"

    return sanitized


def normalize_required_metadata(value: str, field_name: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized:
        raise validation_error(
            "missing_metadata",
            f"{field_name} is required.",
        )
    if len(normalized) > maximum:
        raise validation_error(
            "metadata_too_long",
            f"{field_name} must be {maximum} characters or fewer.",
        )
    return normalized


def normalize_optional_metadata(
    value: str | None,
    field_name: str,
    maximum: int,
) -> str | None:
    if value is None or not value.strip():
        return None
    normalized = value.strip()
    if len(normalized) > maximum:
        raise validation_error(
            "metadata_too_long",
            f"{field_name} must be {maximum} characters or fewer.",
        )
    return normalized


def normalize_measurement_date(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    normalized = value.strip()
    try:
        return date.fromisoformat(normalized).isoformat()
    except ValueError as error:
        raise validation_error(
            "invalid_measurement_date",
            "Measurement date must use YYYY-MM-DD format.",
        ) from error


async def stream_upload_to_staging(
    file: UploadFile,
    staging_path: Path,
    maximum_size_bytes: int | None = None,
) -> tuple[int, str]:
    if maximum_size_bytes is None:
        maximum_size_bytes = MAX_FILE_SIZE_BYTES
    checksum = hashlib.sha256()
    size_bytes = 0

    with staging_path.open("xb") as output_file:
        while chunk := await file.read(CHUNK_SIZE):
            if size_bytes + len(chunk) > maximum_size_bytes:
                raise validation_error(
                    "file_too_large",
                    f"Files must be no larger than {maximum_size_bytes} bytes.",
                    status_code=413,
                )
            output_file.write(chunk)
            checksum.update(chunk)
            size_bytes += len(chunk)

    if size_bytes == 0:
        raise validation_error("empty_file", "Empty files cannot be imported.")

    return size_bytes, checksum.hexdigest()


def cleanup_created_destination(
    destination_path: Path,
    destination_directory: Path,
) -> None:
    if destination_path.exists():
        destination_path.unlink()
    if destination_directory.exists():
        destination_directory.rmdir()


def optical_image_content_type(path: Path) -> str:
    with path.open("rb") as image_file:
        signature = image_file.read(16)
    if signature.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if signature.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if signature.startswith(b"RIFF") and signature[8:12] == b"WEBP":
        return "image/webp"
    raise validation_error(
        "invalid_optical_image",
        "Optical images must be valid JPEG, PNG, or WebP files.",
    )


def resolve_attachment_path(attachment: dict) -> Path:
    expected_directory = (
        RAW_DATA_DIR / attachment["file_id"] / "attachments"
    ).resolve()
    stored_path = Path(attachment["storage_path"])
    if not stored_path.is_absolute():
        raise validation_error(
            "invalid_attachment_path",
            "The attachment path is not an absolute managed path.",
            409,
        )
    resolved_path = stored_path.resolve()
    try:
        resolved_path.relative_to(expected_directory)
    except ValueError as error:
        raise validation_error(
            "invalid_attachment_path",
            "The attachment path is outside the managed raw-data directory.",
            409,
        ) from error
    if not resolved_path.is_file():
        raise validation_error(
            "attachment_missing",
            "The optical-image metadata has no corresponding stored file.",
            409,
        )
    contents = resolved_path.read_bytes()
    if len(contents) != attachment["size_bytes"] or hashlib.sha256(
        contents
    ).hexdigest() != attachment["sha256"]:
        raise validation_error(
            "attachment_checksum_mismatch",
            "The stored optical image no longer matches its imported checksum.",
            409,
        )
    return resolved_path


def normalize_source_url(value: str | None) -> str | None:
    normalized = normalize_optional_metadata(value, "Source URL", 2000)
    if normalized is None:
        return None
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise validation_error(
            "invalid_source_url",
            "Source URL must be an absolute HTTP or HTTPS URL.",
        )
    return normalized


def normalize_material_system(value: str | None) -> str:
    normalized = normalize_optional_metadata(value, "Material system", 200)
    if normalized is None or normalized.casefold() in {
        "unknown",
        "unknown system",
        "not known",
        "unspecified",
    }:
        return "Unknown"
    return normalized


def normalize_substrate(value: str | None) -> str:
    normalized = "_".join((value or "unknown").strip().casefold().split())
    aliases = {
        "unknown": "unknown",
        "glass": "glass",
        "glass_/_sio2": "glass",
        "sio2": "glass",
        "sio2_si": "sio2_si",
        "sio2_/_crystalline_si": "sio2_si",
        "no_substrate": "no_substrate",
        "none": "no_substrate",
    }
    if normalized not in aliases:
        raise validation_error(
            "invalid_substrate",
            "Substrate must be unknown, glass, sio2_si, or no_substrate.",
        )
    return aliases[normalized]


def normalize_measurement_role(value: str | None) -> str:
    normalized = (value or "unspecified").strip().casefold()
    allowed = {
        "unspecified",
        "sample",
        "sample_on_substrate",
        "pure_substrate_reference",
    }
    if normalized not in allowed:
        raise validation_error(
            "invalid_measurement_role",
            "Measurement role must be sample, sample_on_substrate, or pure_substrate_reference.",
        )
    return normalized


def advanced_processing_options(request: ProcessingRequest | BatchProcessingRequest) -> dict:
    return {
        "substrate_reference_strategy": request.substrate_reference_strategy,
        "quality_policy": request.quality_policy,
        "peak_review_policy": request.peak_review_policy,
        "substrate_ensemble_minimum_coverage_fraction": (
            request.ensemble_minimum_coverage
        ),
        "substrate_ensemble_support_fraction_threshold": (
            request.ensemble_consensus_threshold
        ),
        "substrate_auto_minimum_fit_r_squared": (
            request.ensemble_minimum_r_squared
        ),
        "analysis_range_min_cm-1": request.analysis_range_min_cm_1,
        "analysis_range_max_cm-1": request.analysis_range_max_cm_1,
        "minimum_prominence_fraction": (
            request.peak_detection_threshold_percent / 100.0
        ),
        "normalization_peak_cm-1": request.normalization_peak_cm_1,
        "normalization_mode": request.normalization_mode,
    }


def preserved_advanced_processing_options(result: dict) -> dict:
    parameters = result["model"]["parameters"]
    return {
        "substrate_reference_strategy": parameters.get(
            "substrate_reference_strategy", "automatic"
        ),
        "quality_policy": parameters.get("quality_policy", "balanced"),
        "peak_review_policy": parameters.get(
            "peak_review_policy", "withhold_uncertain"
        ),
        "substrate_ensemble_minimum_coverage_fraction": parameters.get(
            "substrate_ensemble_minimum_coverage_fraction", 0.95
        ),
        "substrate_ensemble_support_fraction_threshold": parameters.get(
            "substrate_ensemble_support_fraction_threshold", 0.6
        ),
        "substrate_auto_minimum_fit_r_squared": parameters.get(
            "substrate_auto_minimum_fit_r_squared", 0.75
        ),
        "analysis_range_min_cm-1": parameters.get("analysis_range_min_cm-1"),
        "analysis_range_max_cm-1": parameters.get("analysis_range_max_cm-1"),
        "minimum_prominence_fraction": parameters.get(
            "minimum_prominence_fraction", 0.02
        ),
    }


def normalize_config_object(
    config: dict,
    label: str,
    allowed_keys: set[str] | None = None,
) -> dict:
    if allowed_keys is not None and set(config) - allowed_keys:
        raise validation_error(
            "invalid_configuration",
            f"{label} contains unsupported settings.",
        )
    if any(
        isinstance(value, (dict, list))
        or not isinstance(value, (str, int, float, bool, type(None)))
        or (isinstance(value, float) and not math.isfinite(value))
        for value in config.values()
    ):
        raise validation_error(
            "invalid_configuration",
            f"{label} settings must be finite scalar values.",
        )
    serialized = integrity.canonical_json(config)
    if len(serialized.encode("utf-8")) > 100_000:
        raise validation_error(
            "configuration_too_large",
            f"{label} must be no larger than 100 KB.",
        )
    return config


def editable_metadata_snapshot(record: dict) -> dict:
    return {
        key: record.get(key)
        for key in (
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
        )
    }


def persist_metadata_update(
    file_id: str,
    updates: dict,
    action: str = "edit",
) -> dict:
    previous = database.get_imported_file(file_id)
    if previous is None:
        raise validation_error(
            "file_not_found",
            "No imported file has the requested ID.",
            404,
        )
    changed_at = datetime.now(timezone.utc).isoformat()
    stored_updates = {**updates, "updated_at": changed_at}
    updated = {**previous, **stored_updates}
    revision = {
        "revision_id": str(uuid4()),
        "file_id": file_id,
        "action": action,
        "previous_json": json.dumps(
            editable_metadata_snapshot(previous),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        "updated_json": json.dumps(
            editable_metadata_snapshot(updated),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        "changed_at": changed_at,
    }
    with database.connect_database() as connection:
        database.update_imported_file_metadata(
            connection,
            file_id,
            stored_updates,
        )
        database.insert_import_metadata_revision(connection, revision)
    return database.get_imported_file(file_id)


@app.get("/")
def read_root():
    return {
        "application": "Materials Data Copilot",
        "status": "running",
        "version": APP_VERSION,
    }


@app.get("/health")
def health_check():
    try:
        with database.connect_database() as connection:
            catalog_report = integrity.verify_catalog(
                connection,
                RAW_DATA_DIR,
                PROCESSED_DATA_DIR,
                REFERENCE_DATA_DIR,
            )
            import_count = connection.execute(
                "SELECT COUNT(*) FROM imported_files"
            ).fetchone()[0]
            processing_run_count = connection.execute(
                "SELECT COUNT(*) FROM processing_runs"
            ).fetchone()[0]
            reference_count = connection.execute(
                "SELECT COUNT(*) FROM reference_sources"
            ).fetchone()[0]
            substrate_feedback_count = connection.execute(
                "SELECT COUNT(*) FROM substrate_peak_feedback"
            ).fetchone()[0]
    except (integrity.IntegrityError, OSError, sqlite3.Error) as error:
        logger.error("Health verification failed: %s", error)
        raise HTTPException(
            status_code=503,
            detail={
                "code": "integrity_verification_failed",
                "message": str(error),
            },
        ) from error
    return {
        "status": "healthy",
        "version": APP_VERSION,
        "database": catalog_report["database"],
        "catalog": {
            "verified_at": catalog_report["verified_at"],
            "counts": catalog_report["counts"],
        },
        "record_counts": {
            "imports": import_count,
            "processing_runs": processing_run_count,
            "references": reference_count,
            "substrate_peak_feedback": substrate_feedback_count,
        },
        "storage": {
            "raw_available": RAW_DATA_DIR.is_dir(),
            "processed_available": PROCESSED_DATA_DIR.is_dir(),
            "references_available": REFERENCE_DATA_DIR.is_dir(),
        },
    }


@app.get("/upload", response_class=FileResponse)
def upload_interface():
    return FileResponse(UPLOAD_INTERFACE_PATH)


@app.get("/comparison", response_class=FileResponse)
def comparison_interface():
    return FileResponse(COMPARISON_INTERFACE_PATH)


@app.get("/files")
def list_imported_files(
    include_archived: bool = False,
    include_analysis: bool = False,
):
    return database.list_imported_files(include_archived, include_analysis)


@app.post("/files/archive-batch")
def archive_imported_files_batch(request: BatchArchiveRequest):
    file_ids = list(dict.fromkeys(file_id.strip() for file_id in request.file_ids))
    if not file_ids or any(not file_id for file_id in file_ids):
        raise validation_error(
            "invalid_batch_selection",
            "Select at least one imported file to archive.",
        )
    if len(file_ids) > 50:
        raise validation_error(
            "batch_too_large",
            "A batch may contain no more than 50 imported files.",
            413,
        )
    records = {file_id: database.get_imported_file(file_id) for file_id in file_ids}
    missing = [file_id for file_id, record in records.items() if record is None]
    if missing:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "batch_file_not_found",
                "message": "Every batch item must be an existing imported file.",
                "file_ids": missing,
            },
        )
    changed_at = datetime.now(timezone.utc).isoformat()
    archived = []
    with database.connect_database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        for file_id in file_ids:
            previous = records[file_id]
            if previous.get("archived_at") is not None:
                archived.append(previous)
                continue
            stored_updates = {
                "archived_at": changed_at,
                "updated_at": changed_at,
            }
            updated = {**previous, **stored_updates}
            database.update_imported_file_metadata(
                connection,
                file_id,
                stored_updates,
            )
            database.insert_import_metadata_revision(
                connection,
                {
                    "revision_id": str(uuid4()),
                    "file_id": file_id,
                    "action": "archive",
                    "previous_json": json.dumps(
                        editable_metadata_snapshot(previous),
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    "updated_json": json.dumps(
                        editable_metadata_snapshot(updated),
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    "changed_at": changed_at,
                },
            )
            archived.append(updated)
    return {
        "archived_count": len(archived),
        "file_ids": file_ids,
        "retention": (
            "Raw bytes, processing runs, checksums, and provenance were preserved; "
            "only active-list visibility changed."
        ),
    }


@app.get("/operators")
def list_operators():
    return database.list_operators()


@app.get("/samples")
def list_samples(q: str | None = None):
    query = normalize_optional_metadata(q, "Sample search", 200)
    return database.list_samples(query)


@app.post("/samples", status_code=201)
def save_sample(request: SampleRequest):
    sample_id = normalize_required_metadata(request.sample_id, "Sample ID", 100)
    now = datetime.now(timezone.utc).isoformat()
    sample = {
        "sample_id": sample_id,
        "material_system": normalize_optional_metadata(
            request.material_system,
            "Material system",
            200,
        ),
        "substrate": (
            normalize_substrate(request.substrate)
            if request.substrate is not None
            else None
        ),
        "project": normalize_optional_metadata(request.project, "Project", 200),
        "notes": normalize_optional_metadata(request.notes, "Notes", 2000),
        "created_at": now,
        "updated_at": now,
    }
    with database.connect_database() as connection:
        database.upsert_sample(connection, sample)
    return next(
        record
        for record in database.list_samples()
        if record["sample_id"] == sample_id
    )


def serialize_config_records(records: list[dict]) -> list[dict]:
    result = []
    for record in records:
        decoded = {**record, "config": json.loads(record["config_json"])}
        decoded.pop("config_json")
        result.append(decoded)
    return result


@app.get("/analysis/recipes")
def list_analysis_recipes():
    return serialize_config_records(database.list_analysis_recipes())


@app.put("/analysis/recipes/{recipe_id}")
def save_analysis_recipe(recipe_id: str, request: ConfigRecordRequest):
    normalized_id = normalize_required_metadata(recipe_id, "Recipe ID", 100)
    name = normalize_required_metadata(request.name, "Recipe name", 200)
    config = normalize_config_object(
        request.config,
        "Analysis recipe",
        {
            "deconvolution",
            "substrateCorrection",
            "baselineMethod",
            "substrateReferenceStrategy",
            "qualityPolicy",
            "peakReviewPolicy",
            "ensembleMinimumCoverage",
            "ensembleConsensusThreshold",
            "ensembleMinimumRSquared",
            "analysisRangeMinCm1",
            "analysisRangeMaxCm1",
            "peakDetectionThresholdPercent",
        },
    )
    baseline_method = config.get("baselineMethod", "morphological_opening")
    if config.get("deconvolution") not in {
        "none",
        "gaussian",
        "lorentzian",
        "pseudo_voigt",
    } or config.get("substrateCorrection") not in {
        "none",
        "detect",
        "auto",
        "glass",
        "sio2_si",
    } or baseline_method not in processing.BASELINE_METHODS:
        raise validation_error(
            "invalid_configuration",
            "Analysis recipe contains an unsupported processing mode.",
        )
    try:
        peak_detection_fraction = (
            float(config.get("peakDetectionThresholdPercent", 2.0)) / 100.0
        )
        processing.validate_advanced_processing_options(
            {
                "substrate_reference_strategy": config.get(
                    "substrateReferenceStrategy", "automatic"
                ),
                "quality_policy": config.get("qualityPolicy", "balanced"),
                "peak_review_policy": config.get(
                    "peakReviewPolicy", "withhold_uncertain"
                ),
                "substrate_ensemble_minimum_coverage_fraction": config.get(
                    "ensembleMinimumCoverage", 0.95
                ),
                "substrate_ensemble_support_fraction_threshold": config.get(
                    "ensembleConsensusThreshold", 0.6
                ),
                "substrate_auto_minimum_fit_r_squared": config.get(
                    "ensembleMinimumRSquared", 0.75
                ),
                "analysis_range_min_cm-1": config.get("analysisRangeMinCm1"),
                "analysis_range_max_cm-1": config.get("analysisRangeMaxCm1"),
                "minimum_prominence_fraction": peak_detection_fraction,
            }
        )
    except (processing.ProcessingError, TypeError, ValueError) as error:
        raise validation_error(
            "invalid_configuration",
            getattr(error, "message", "Peak detection threshold must be numeric."),
        ) from error
    now = datetime.now(timezone.utc).isoformat()
    record = {
        "recipe_id": normalized_id,
        "name": name,
        "config_json": integrity.canonical_json(config),
        "created_at": now,
        "updated_at": now,
    }
    try:
        with database.connect_database() as connection:
            database.upsert_analysis_recipe(connection, record)
    except sqlite3.IntegrityError as error:
        raise validation_error(
            "duplicate_recipe_name",
            "Another saved recipe already uses that name.",
            409,
        ) from error
    return next(
        item
        for item in serialize_config_records(database.list_analysis_recipes())
        if item["recipe_id"] == normalized_id
    )


@app.get("/presets")
def list_instrument_presets():
    return serialize_config_records(database.list_instrument_presets())


@app.put("/presets/{preset_id}")
def save_instrument_preset(preset_id: str, request: ConfigRecordRequest):
    normalized_id = normalize_required_metadata(preset_id, "Preset ID", 100)
    name = normalize_required_metadata(request.name, "Preset name", 200)
    config = normalize_config_object(request.config, "Instrument preset")
    now = datetime.now(timezone.utc).isoformat()
    record = {
        "preset_id": normalized_id,
        "name": name,
        "config_json": integrity.canonical_json(config),
        "created_at": now,
        "updated_at": now,
    }
    try:
        with database.connect_database() as connection:
            database.upsert_instrument_preset(connection, record)
    except sqlite3.IntegrityError as error:
        raise validation_error(
            "duplicate_preset_name",
            "Another preset already uses that name.",
            409,
        ) from error
    return next(
        item
        for item in serialize_config_records(database.list_instrument_presets())
        if item["preset_id"] == normalized_id
    )


@app.get("/files/{file_id}/attachments")
def list_imported_file_attachments(file_id: str):
    if database.get_imported_file(file_id) is None:
        raise validation_error(
            "file_not_found",
            "No imported file has the requested ID.",
            404,
        )
    records = database.list_file_attachments(file_id)
    return [
        {
            **record,
            "content_url": (
                f"/files/{file_id}/attachments/"
                f"{record['attachment_id']}/content"
            ),
        }
        for record in records
    ]


@app.post("/files/{file_id}/attachments", status_code=201)
async def attach_optical_image(
    file_id: str,
    file: UploadFile = File(...),
):
    if database.get_imported_file(file_id) is None:
        raise validation_error(
            "file_not_found",
            "No imported file has the requested ID.",
            404,
        )
    attachment_id = str(uuid4())
    staging_path = None
    destination_path = None
    attachment_directory = RAW_DATA_DIR / file_id / "attachments"
    try:
        original_filename = extract_original_filename(file.filename)
        stored_filename = sanitize_filename(original_filename)
        suffix = Path(stored_filename).suffix.casefold()
        if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
            raise validation_error(
                "unsupported_optical_image",
                "Optical images must use .jpg, .jpeg, .png, or .webp.",
                415,
            )
        staging_directory = RAW_DATA_DIR / ".staging"
        staging_directory.mkdir(parents=True, exist_ok=True)
        staging_path = staging_directory / f"{attachment_id}.image-upload"
        size_bytes, sha256 = await stream_upload_to_staging(
            file,
            staging_path,
            MAX_OPTICAL_IMAGE_SIZE_BYTES,
        )
        content_type = optical_image_content_type(staging_path)
        imported_at = datetime.now(timezone.utc).isoformat()
        destination_path = (
            attachment_directory / f"{attachment_id}-{stored_filename}"
        )
        record = {
            "attachment_id": attachment_id,
            "file_id": file_id,
            "kind": "optical_image",
            "original_filename": original_filename,
            "content_type": content_type,
            "size_bytes": size_bytes,
            "sha256": sha256,
            "storage_path": str(destination_path),
            "imported_at": imported_at,
        }
        with database.connect_database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = database.find_file_attachment_by_sha256(
                connection,
                file_id,
                sha256,
            )
            if duplicate is not None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "duplicate_attachment",
                        "message": (
                            "This optical image is already attached to the "
                            "measurement."
                        ),
                        "existing_attachment_id": duplicate["attachment_id"],
                    },
                )
            attachment_directory.mkdir(parents=True, exist_ok=True)
            os.replace(staging_path, destination_path)
            database.insert_file_attachment(connection, record)
        return {
            **record,
            "content_url": (
                f"/files/{file_id}/attachments/{attachment_id}/content"
            ),
        }
    except HTTPException:
        if destination_path is not None and destination_path.exists():
            destination_path.unlink()
        raise
    except (OSError, sqlite3.Error) as error:
        if destination_path is not None and destination_path.exists():
            try:
                destination_path.unlink()
            except OSError:
                logger.exception("Optical-image attachment cleanup failed")
        raise HTTPException(
            status_code=500,
            detail={
                "code": "attachment_import_failed",
                "message": (
                    "The optical-image attachment failed and was rolled back."
                ),
            },
        ) from error
    finally:
        await file.close()
        if staging_path is not None and staging_path.exists():
            try:
                staging_path.unlink()
            except OSError:
                logger.exception("Failed to remove a staged optical image")


@app.get("/files/{file_id}/attachments/{attachment_id}/content")
def retrieve_optical_image(file_id: str, attachment_id: str):
    attachment = database.get_file_attachment(attachment_id)
    if attachment is None or attachment["file_id"] != file_id:
        raise validation_error(
            "attachment_not_found",
            "No optical image has the requested ID for this measurement.",
            404,
        )
    path = resolve_attachment_path(attachment)
    return FileResponse(
        path,
        media_type=attachment["content_type"],
        headers={
            "Content-Disposition": (
                f"inline; filename={json.dumps(attachment['original_filename'])}"
            ),
            "X-Content-Type-Options": "nosniff",
            "ETag": f'"{attachment["sha256"]}"',
        },
    )


@app.get("/references")
def list_reference_sources():
    response = []
    sources = database.list_reference_sources()
    sources.extend(copilot.training_references())
    sources.extend(online_references.curated_references())
    sources.extend(
        online_references.load_synced_rod_references(REFERENCE_DATA_DIR)
    )
    for source in sources:
        evidence = json.loads(source.pop("evidence_json"))
        source["evidence"] = evidence
        response.append(source)
    return response


@app.get("/copilot/messages")
def list_copilot_messages(file_id: str | None = None):
    if file_id is not None and database.get_imported_file(file_id) is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "file_not_found",
                "message": "No imported file has that ID.",
            },
        )
    return copilot.message_history(file_id)


@app.get("/copilot/providers")
def list_copilot_providers():
    return ai_providers.provider_configuration()


@app.get("/copilot/training")
def list_copilot_training():
    return database.list_training_examples()


@app.post("/copilot/chat")
def chat_with_copilot(request: CopilotChatRequest):
    try:
        return copilot.chat(
            request.message,
            request.file_id,
            PROCESSED_DATA_DIR,
            request.provider,
            request.share_spectrum_context,
        )
    except copilot.CopilotError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code, "message": error.message},
        ) from error
    except processing.ProcessingError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code, "message": error.message},
        ) from error
    except ai_providers.ProviderError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code, "message": error.message},
        ) from error


@app.post("/copilot/train", status_code=201)
def train_copilot(request: CopilotTrainingRequest):
    try:
        return copilot.train_from_confirmed_file(
            request.file_id,
            request.material_system,
            PROCESSED_DATA_DIR,
        )
    except copilot.CopilotError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code, "message": error.message},
        ) from error
    except processing.ProcessingError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code, "message": error.message},
        ) from error


@app.get("/references/online/status")
def online_reference_status():
    return online_references.online_reference_status(REFERENCE_DATA_DIR)


@app.post("/references/online/sync")
def sync_online_references():
    try:
        return online_references.sync_rod_catalog(REFERENCE_DATA_DIR)
    except online_references.OnlineReferenceError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code, "message": error.message},
        ) from error


@app.post("/references/import", status_code=201)
async def import_reference_source(
    file: UploadFile = File(...),
):
    staging_path = None
    destination_directory = None
    destination_path = None
    destination_created = False
    try:
        original_filename = extract_original_filename(file.filename)
        stored_filename = sanitize_filename(original_filename)
        kind = references.source_kind(Path(stored_filename))
        reference_id = str(uuid4())
        staging_directory = REFERENCE_DATA_DIR / ".staging"
        staging_directory.mkdir(parents=True, exist_ok=True)
        staging_path = staging_directory / f"{reference_id}.upload"
        size_bytes, sha256 = await stream_upload_to_staging(
            file,
            staging_path,
            MAX_REFERENCE_SIZE_BYTES,
        )
        destination_directory = REFERENCE_DATA_DIR / reference_id
        destination_path = destination_directory / stored_filename
        imported_at = datetime.now(timezone.utc).isoformat()

        with database.connect_database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = database.find_reference_source_by_sha256(
                connection,
                sha256,
            )
            if duplicate is not None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "duplicate_reference",
                        "message": "An identical reference is already present.",
                        "existing_reference_id": duplicate["reference_id"],
                    },
                )

            destination_directory.mkdir(parents=True, exist_ok=False)
            destination_created = True
            os.replace(staging_path, destination_path)
            extraction_status, evidence_json, document = (
                references.extract_reference_evidence(destination_path, kind)
            )
            record = {
                "reference_id": reference_id,
                "original_filename": original_filename,
                "content_type": file.content_type,
                "size_bytes": size_bytes,
                "sha256": sha256,
                "storage_path": str(destination_path),
                "imported_at": imported_at,
                "source_kind": kind,
                "technique": normalize_required_metadata(
                    document["technique"], "Technique", 100
                ),
                "material_system": normalize_material_system(
                    document["material_system"]
                ),
                "title": normalize_required_metadata(
                    document["title"], "Title", 500
                ),
                "citation": normalize_optional_metadata(
                    document.get("citation"), "Citation", 2000
                ),
                "source_url": normalize_source_url(
                    document.get("source_url")
                ),
                "extraction_status": extraction_status,
                "evidence_json": evidence_json,
            }
            database.insert_reference_source(connection, record)

        response = {**record, "evidence": json.loads(evidence_json)}
        response.pop("evidence_json")
        return response
    except references.ReferenceError as error:
        if destination_created:
            cleanup_created_destination(
                destination_path,
                destination_directory,
            )
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code, "message": error.message},
        ) from error
    except HTTPException:
        if destination_created:
            cleanup_created_destination(
                destination_path,
                destination_directory,
            )
        raise
    except (OSError, sqlite3.Error) as error:
        if destination_created:
            try:
                cleanup_created_destination(
                    destination_path,
                    destination_directory,
                )
            except OSError:
                logger.exception("Reference import cleanup was incomplete")
        raise HTTPException(
            status_code=500,
            detail={
                "code": "reference_import_failed",
                "message": "The reference import failed and was rolled back.",
            },
        ) from error
    finally:
        await file.close()
        if staging_path is not None and staging_path.exists():
            try:
                staging_path.unlink()
            except OSError:
                logger.exception("Failed to remove a staged reference")


@app.post("/files/inspect")
async def inspect_file_upload(
    file: UploadFile = File(...),
    relative_path: str | None = Form(None),
):
    original_filename = extract_original_filename(file.filename)
    normalized_relative_path = normalize_optional_metadata(
        relative_path,
        "Relative path",
        1000,
    )
    content = await file.read(200_000)
    size_bytes = (
        file.size
        if isinstance(file.size, int) and file.size >= 0
        else len(content)
    )
    await file.close()
    return preview.inspect_upload(
        original_filename,
        content,
        size_bytes,
        normalized_relative_path,
    )


@app.post("/wdf/inspect")
async def inspect_wdf_upload(file: UploadFile = File(...)):
    staging_path = None
    try:
        original_filename = extract_original_filename(file.filename)
        if Path(original_filename).suffix.lower() != ".wdf":
            raise validation_error(
                "not_wdf",
                "WDF inspection accepts files with a .wdf extension.",
                415,
            )
        staging_directory = RAW_DATA_DIR / ".staging"
        staging_directory.mkdir(parents=True, exist_ok=True)
        staging_path = staging_directory / f"{uuid4()}.wdf-inspection"
        size_bytes, sha256 = await stream_upload_to_staging(file, staging_path)
        parsed = wdf_reader.read_wdf(staging_path, include_points=False)
        return {
            "original_filename": original_filename,
            "content_type": file.content_type,
            "size_bytes": size_bytes,
            "sha256": sha256,
            **parsed,
        }
    except wdf_reader.WdfError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code, "message": error.message},
        ) from error
    finally:
        await file.close()
        if staging_path is not None and staging_path.exists():
            try:
                staging_path.unlink()
            except OSError:
                logger.exception("Failed to remove a staged WDF inspection")


@app.get("/files/{file_id}/preview")
def preview_imported_file(file_id: str, dataset_index: int = 0):
    record = database.get_imported_file(file_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "file_not_found",
                "message": "No imported file has the requested ID.",
            },
        )

    try:
        return preview.build_file_preview(record, RAW_DATA_DIR, dataset_index)
    except preview.PreviewError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code, "message": error.message},
        ) from error


@app.get("/files/{file_id}/metadata-suggestions")
def imported_file_metadata_suggestions(file_id: str):
    imported_file = database.get_imported_file(file_id)
    if imported_file is None:
        raise validation_error(
            "file_not_found",
            "No imported file has the requested ID.",
            404,
        )
    try:
        stored_path, actual_sha256 = preview.verify_stored_file(
            imported_file,
            RAW_DATA_DIR,
        )
    except preview.PreviewError as error:
        raise validation_error(error.code, error.message, error.status_code) from error
    with stored_path.open("rb") as source:
        content = source.read(200_000)
    suggestions = preview.extract_metadata_suggestions(
        imported_file["original_filename"],
        content,
    )
    bounds = None
    if stored_path.suffix.lower() != ".wdf":
        bounds = preview.measured_spectral_bounds(stored_path)
    return {
        "file_id": file_id,
        "sha256": actual_sha256,
        "suggested_metadata": suggestions,
        "measured_spectral_bounds": bounds,
    }


@app.get("/files/{file_id}/mapping")
def imported_file_mapping(file_id: str):
    imported_file = database.get_imported_file(file_id)
    if imported_file is None:
        raise validation_error(
            "file_not_found",
            "No imported file has the requested ID.",
            404,
        )
    try:
        return preview.build_mapping_preview(imported_file, RAW_DATA_DIR)
    except preview.PreviewError as error:
        raise validation_error(error.code, error.message, error.status_code) from error


@app.post("/files/{file_id}/process")
def process_imported_file(
    file_id: str,
    request: ProcessingRequest | None = None,
):
    record = database.get_imported_file(file_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "file_not_found",
                "message": "No imported file has the requested ID.",
            },
        )
    try:
        return processing.process_imported_file(
            record,
            RAW_DATA_DIR,
            PROCESSED_DATA_DIR,
            REFERENCE_DATA_DIR,
            request.deconvolution if request is not None else "none",
            request.substrate_correction if request is not None else "none",
            request.dataset_index if request is not None else 0,
            (
                request.baseline_method
                if request is not None
                else "morphological_opening"
            ),
            advanced_processing_options(request) if request is not None else None,
        )
    except processing.ProcessingError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code, "message": error.message},
        ) from error


@app.post("/files/{file_id}/substrate-residual-feedback", status_code=201)
def save_substrate_residual_feedback(
    file_id: str,
    request: SubstrateResidualFeedbackRequest,
):
    record = database.get_imported_file(file_id)
    if record is None:
        raise validation_error(
            "file_not_found",
            "No imported file has the requested ID.",
            404,
        )
    if request.action not in {"keep", "remove"}:
        raise validation_error(
            "invalid_feedback_action",
            "Feedback action must be keep or remove.",
        )
    if not math.isfinite(request.center_cm_1) or not math.isfinite(
        request.half_width_cm_1
    ):
        raise validation_error(
            "invalid_residual_peak",
            "Residual position and width must be finite numbers.",
        )
    if not 1.0 <= request.half_width_cm_1 <= 50.0:
        raise validation_error(
            "invalid_residual_width",
            "Residual half-width must be between 1 and 50 cm-1.",
        )
    try:
        result = processing.latest_processing_result(file_id, PROCESSED_DATA_DIR)
    except processing.ProcessingError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code, "message": error.message},
        ) from error
    raw_points = result.get("series", {}).get("raw") or []
    if (
        not raw_points
        or request.center_cm_1 < float(raw_points[0][0])
        or request.center_cm_1 > float(raw_points[-1][0])
    ):
        raise validation_error(
            "residual_peak_out_of_range",
            "Residual position must lie within the processed Raman-shift range.",
        )
    substrate_result = result.get("substrate_correction") or {}
    substrate = (
        substrate_result.get("selected_substrate")
        or record.get("substrate")
    )
    if substrate not in {"glass", "sio2_si"}:
        raise validation_error(
            "substrate_not_selected",
            "Select or detect a glass or SiO2/Si substrate before saving residual feedback.",
        )
    feedback = {
        "feedback_id": str(uuid4()),
        "source_file_id": file_id,
        "material_system": record.get("material_system"),
        "substrate": substrate,
        "center_cm_1": request.center_cm_1,
        "half_width_cm_1": request.half_width_cm_1,
        "action": request.action,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with database.connect_database() as connection:
        database.insert_substrate_peak_feedback(connection, feedback)
    return {
        **feedback,
        "learned": True,
        "effect": (
            "Future matching spectra protect this interval as sample signal."
            if request.action == "keep"
            else "Future matching spectra include this interval as confirmed substrate evidence."
        ),
        "reprocess_required": True,
    }


@app.post("/files/process/batch")
def process_imported_files_batch(request: BatchProcessingRequest):
    if request.deconvolution not in processing.DECONVOLUTION_PROFILES:
        raise validation_error(
            "unsupported_deconvolution_profile",
            "Deconvolution must be none, gaussian, lorentzian, or pseudo_voigt.",
        )
    if (
        request.substrate_correction
        not in processing.SUBSTRATE_CORRECTION_MODES
    ):
        raise validation_error(
            "unsupported_substrate_correction",
            "Substrate correction must be none, detect, auto, glass, or sio2_si.",
        )
    if request.baseline_method not in processing.BASELINE_METHODS:
        raise validation_error(
            "unsupported_baseline_method",
            "Baseline method must be morphological_opening or rubber_band.",
        )
    try:
        validated_advanced_options = processing.validate_advanced_processing_options(
            advanced_processing_options(request)
        )
    except processing.ProcessingError as error:
        raise validation_error(error.code, error.message, error.status_code) from error
    file_ids = list(dict.fromkeys(file_id.strip() for file_id in request.file_ids))
    if not file_ids or any(not file_id for file_id in file_ids):
        raise validation_error(
            "invalid_batch_selection",
            "Select at least one imported file for batch processing.",
        )
    if len(file_ids) > 50:
        raise validation_error(
            "batch_too_large",
            "A batch may contain no more than 50 imported files.",
            413,
        )
    imported_files = {
        file_id: database.get_imported_file(file_id) for file_id in file_ids
    }
    missing_ids = [
        file_id for file_id, record in imported_files.items() if record is None
    ]
    if missing_ids:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "batch_file_not_found",
                "message": "Every batch item must be an existing imported file.",
                "file_ids": missing_ids,
            },
        )

    items = []
    completed_results = []
    for file_id in file_ids:
        try:
            result = processing.process_imported_file(
                imported_files[file_id],
                RAW_DATA_DIR,
                PROCESSED_DATA_DIR,
                REFERENCE_DATA_DIR,
                request.deconvolution,
                request.substrate_correction,
                baseline_method=request.baseline_method,
                advanced_options=validated_advanced_options,
            )
            completed_results.append(result)
            identification = result["summary"]["identification"]
            items.append(
                {
                    "file_id": file_id,
                    "status": "completed",
                    "processing_id": result["processing_id"],
                    "source_sha256": result["source_sha256"],
                    "result_sha256": result["result_sha256"],
                    "material_system": identification.get("material_system"),
                    "confidence": identification.get("confidence"),
                    "peak_count": result["summary"]["peak_count"],
                }
            )
        except processing.ProcessingError as error:
            items.append(
                {
                    "file_id": file_id,
                    "status": "failed",
                    "error": {"code": error.code, "message": error.message},
                }
            )
    completed_count = sum(item["status"] == "completed" for item in items)
    return {
        "requested_count": len(file_ids),
        "completed_count": completed_count,
        "failed_count": len(file_ids) - completed_count,
        "parameters": {
            "deconvolution": request.deconvolution,
            "substrate_correction": request.substrate_correction,
            "smoothing": "none",
        },
        "items": items,
        "series_comparison": (
            processing.compare_processing_results(completed_results)
            if len(completed_results) >= 2
            else None
        ),
    }


@app.get("/files/{file_id}/processing/latest")
def retrieve_latest_processing(file_id: str):
    if database.get_imported_file(file_id) is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "file_not_found",
                "message": "No imported file has the requested ID.",
            },
        )
    try:
        return processing.latest_processing_result(
            file_id,
            PROCESSED_DATA_DIR,
        )
    except processing.ProcessingError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code, "message": error.message},
        ) from error


def serialize_processing_run(run: dict) -> dict:
    decoded = {
        **run,
        "parameters": json.loads(run["parameters_json"]),
        "summary": json.loads(run["summary_json"]),
        "result_url": f"/processing-runs/{run['processing_id']}",
    }
    decoded.pop("parameters_json")
    decoded.pop("summary_json")
    decoded.pop("result_path")
    return decoded


@app.get("/files/{file_id}/processing-runs")
def list_file_processing_runs(file_id: str):
    if database.get_imported_file(file_id) is None:
        raise validation_error(
            "file_not_found",
            "No imported file has the requested ID.",
            404,
        )
    return [
        serialize_processing_run(run)
        for run in database.list_processing_runs(file_id)
    ]


@app.get("/processing-runs/{processing_id}")
def retrieve_processing_run(processing_id: str):
    run = database.get_processing_run(processing_id)
    if run is None:
        raise validation_error(
            "processing_not_found",
            "No processing run has the requested ID.",
            404,
        )
    try:
        result = processing.load_processing_result(run, PROCESSED_DATA_DIR)
    except processing.ProcessingError as error:
        raise validation_error(error.code, error.message, error.status_code) from error
    return {
        "run": serialize_processing_run(run),
        "result": result,
        "verification": "verified",
    }


@app.get("/samples/{sample_id}/raman-summary")
def summarize_sample_raman_spectra(sample_id: str):
    normalized_id = normalize_required_metadata(sample_id, "Sample ID", 100)
    imports = [
        record
        for record in database.list_imported_files()
        if record.get("sample_id") == normalized_id
        and "raman" in (record.get("technique") or "").casefold()
    ]
    if not imports:
        raise validation_error(
            "sample_not_found",
            "No active Raman imports use the requested sample ID.",
            404,
        )
    latest_runs = []
    for imported_file in imports:
        run = database.get_latest_processing_run(imported_file["file_id"])
        if run is not None:
            latest_runs.append(
                {
                    "file_id": imported_file["file_id"],
                    "filename": imported_file["original_filename"],
                    **serialize_processing_run(run),
                }
            )
    method_groups: dict[str, int] = {}
    for run in latest_runs:
        method_key = integrity.canonical_json(
            {
                "model_name": run["model_name"],
                "model_version": run["model_version"],
                "parameters": run["parameters"],
            }
        )
        method_groups[method_key] = method_groups.get(method_key, 0) + 1
    return {
        "sample_id": normalized_id,
        "import_count": len(imports),
        "processed_count": len(latest_runs),
        "unprocessed_count": len(imports) - len(latest_runs),
        "method_group_counts": sorted(method_groups.values(), reverse=True),
        "latest_runs": latest_runs,
    }


@app.get("/files/{file_id}/export/reproducibility")
def export_reproducibility_package(file_id: str):
    imported_file = database.get_imported_file(file_id)
    if imported_file is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "file_not_found",
                "message": "No imported file has the requested ID.",
            },
        )
    try:
        bundle, filename = reproducibility.build_bundle(
            imported_file,
            RAW_DATA_DIR,
            PROCESSED_DATA_DIR,
            Path(__file__).parent / "requirements.txt",
        )
    except reproducibility.ExportError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code, "message": error.message},
        ) from error
    return StreamingResponse(
        reproducibility.stream_bundle(bundle),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/files/{file_id}/export/jcamp")
def export_jcamp_file(file_id: str):
    try:
        content, filename = jcamp_exporter.export_jcamp_dx(file_id)
    except jcamp_exporter.JCAMPExportError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code, "message": error.message},
        ) from error
    return Response(
        content=content,
        media_type="application/x-jcamp-dx",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post("/files/compare")
def compare_spectra_endpoint(request: CompareSpectraRequest):
    try:
        return comparison.compare_spectra(
            request.sample_file_id,
            request.reference_file_id,
            request.normalization,
        )
    except comparison.ComparisonError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code, "message": error.message},
        ) from error


@app.get("/files/{file_id}/correlated")
def correlated_spectra_endpoint(file_id: str, correlation_threshold: float = 0.9):
    try:
        return comparison.find_correlated_spectra(file_id, correlation_threshold)
    except comparison.ComparisonError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code, "message": error.message},
        ) from error


@app.post("/files/compare/overlay")
def spectra_overlay_endpoint(request: SpectraOverlayRequest):
    try:
        return comparison.build_spectra_overlay(
            request.file_ids,
            request.normalization,
            request.series_name,
            request.offset,
            PROCESSED_DATA_DIR,
            [selection.model_dump() for selection in request.peak_selections]
            if request.peak_selections
            else None,
        )
    except comparison.ComparisonError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code, "message": error.message},
        ) from error


@app.post("/files/compare/deconvolution-peaks")
def deconvolution_peak_catalog_endpoint(request: DeconvolutionPeakCatalogRequest):
    try:
        return comparison.deconvolution_peak_catalog(
            request.file_ids,
            PROCESSED_DATA_DIR,
        )
    except comparison.ComparisonError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code, "message": error.message},
        ) from error


@app.post("/files/compare/analyses")
def group_analysis_catalog_endpoint(request: DeconvolutionPeakCatalogRequest):
    try:
        return comparison.group_analysis_catalog(
            request.file_ids,
            PROCESSED_DATA_DIR,
        )
    except comparison.ComparisonError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code, "message": error.message},
        ) from error


@app.post("/files/{file_id}/peaks/confirm")
def confirm_peak_assignment(
    file_id: str,
    request: PeakAssignmentConfirmationRequest,
):
    imported_file = database.get_imported_file(file_id)
    if imported_file is None:
        raise validation_error(
            "file_not_found",
            "No imported file has the requested ID.",
            404,
        )
    material_system = " ".join(request.material_system.split())
    label = " ".join(request.label.split())
    if not request.learning_confirmed:
        raise validation_error(
            "learning_confirmation_required",
            "Explicit user confirmation is required before this edit can become learning evidence.",
        )
    if not material_system or len(material_system) > 200:
        raise validation_error(
            "invalid_peak_material_system",
            "Enter a material-system name between 1 and 200 characters.",
        )
    if not label or len(label) > 200:
        raise validation_error(
            "invalid_peak_label",
            "Enter a peak label between 1 and 200 characters.",
        )
    related_reference_ids = list(
        dict.fromkeys(
            reference_id.strip()
            for reference_id in (request.related_reference_ids or [])
            if reference_id.strip()
        )
    )
    if len(related_reference_ids) > 10:
        raise validation_error(
            "too_many_related_references",
            "Attach no more than 10 related papers to one peak confirmation.",
        )
    related_references = []
    for reference_id in related_reference_ids:
        reference = database.get_reference_source(reference_id)
        if reference is None:
            raise validation_error(
                "related_reference_not_found",
                "Every related paper must be imported before confirmation.",
                404,
            )
        related_references.append(
            {
                key: reference.get(key)
                for key in (
                    "reference_id",
                    "original_filename",
                    "title",
                    "citation",
                    "source_url",
                    "sha256",
                    "extraction_status",
                )
            }
        )
    if request.component_kind not in {"deconvolution", "substrate"}:
        raise validation_error(
            "invalid_component_kind",
            "Component kind must be deconvolution or substrate.",
        )
    if not math.isfinite(request.position_cm_1):
        raise validation_error(
            "invalid_peak_position",
            "Peak position must be a finite Raman shift.",
        )
    try:
        current_result = processing.latest_processing_result(
            file_id,
            PROCESSED_DATA_DIR,
        )
    except processing.ProcessingError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code, "message": error.message},
        ) from error
    if request.processing_id != current_result["processing_id"]:
        raise validation_error(
            "stale_processing_result",
            "The fit changed. Review the latest components before confirming an edit.",
            409,
        )

    component_group = (
        current_result.get("deconvolution", {})
        if request.component_kind == "deconvolution"
        else current_result.get("substrate_correction", {})
    )
    components = component_group.get("components") or []
    target = min(
        components,
        key=lambda component: abs(
            float(component["seed_center_cm-1"]) - request.position_cm_1
        ),
        default=None,
    )
    if target is None or abs(
        float(target["seed_center_cm-1"]) - request.position_cm_1
    ) > 4.0:
        raise validation_error(
            "fit_component_not_found",
            "The requested fitted component is not present in the latest result.",
            409,
        )

    observed = float(target["seed_center_cm-1"])
    original_assignment = target.get("assignment") or {}
    confirmation_token = str(uuid4())
    evidence_identity = {
        "confirmation_token": confirmation_token,
        "file_id": file_id,
        "source_sha256": imported_file["sha256"],
        "processing_id": current_result["processing_id"],
        "component_kind": request.component_kind,
        "observed_cm-1": observed,
        "material_system": material_system,
        "label": label,
        "related_reference_ids": related_reference_ids,
    }
    evidence_sha256 = hashlib.sha256(
        clarifications.canonical_json(evidence_identity).encode("utf-8")
    ).hexdigest()
    confirmed_assignment = {
        "observed_cm-1": observed,
        "material_system": material_system,
        "label": label,
        "difference_cm-1": 0.0,
        "reference": {
            "reference_id": f"user-peak-confirmation-{confirmation_token}",
            "title": "User-confirmed fitted-component assignment",
            "citation": (
                "Private user confirmation from the local fit inset; source "
                "file and processing provenance retained."
            ),
            "source_url": None,
            "sha256": evidence_sha256,
            "provider": "user_confirmation",
        },
    }
    is_instrument_artifact = processing.is_instrument_artifact_confirmation(
        confirmed_assignment
    )
    if is_instrument_artifact:
        try:
            fitted_fwhm = float(target.get("fwhm_cm-1"))
        except (TypeError, ValueError):
            fitted_fwhm = processing.MODEL_PARAMETERS[
                "deconvolution_default_fwhm_cm-1"
            ]
        half_width = max(1.0, fitted_fwhm / 2)
        confirmed_assignment["role"] = "instrument_artifact"
        confirmed_assignment["affected_range_cm-1"] = [
            observed - half_width,
            observed + half_width,
        ]
    if related_references:
        confirmed_assignment["related_references"] = related_references
    substrate_names = {
        "glass",
        "silica",
        "sio2",
        "sio2si",
        "crystallinesilicon",
        "silicondioxide",
    }
    if (
        not is_instrument_artifact
        and original_assignment.get("role") == "substrate"
        and processing.normalized_material_name(material_system)
        in substrate_names
    ):
        confirmed_assignment["role"] = "substrate"
    learning_scope = (
        "instrument_artifact_for_current_file"
        if is_instrument_artifact
        else "peak_assignment_for_matching_material_systems"
    )
    question_explanation = (
        "This edit will be stored as a user-confirmed instrument artifact, "
        "reprocess the current spectrum, and filter the fitted band from the "
        "instrument-artifact-filtered and subsequent spectra."
        if is_instrument_artifact
        else "This edit will be stored as user-confirmed evidence, reprocess "
        "the current spectrum, and teach future spectra with the same "
        "material-system label."
    )
    question = {
        "question_key": (
            f"unassigned_peak:{observed:.3f}:manual:{confirmation_token[:12]}"
        ),
        "kind": "fit_assignment_confirmation",
        "prompt": f"Confirm the assignment at {observed:.1f} cm-1",
        "explanation": question_explanation,
        "component_kind": request.component_kind,
        "original_assignment": original_assignment,
        "proposed_assignment": confirmed_assignment,
        "options": [
            {"value": "confirm_edit", "label": "Confirm assignment edit"}
        ],
        "allows_text": False,
    }
    response_value = {
        "answer": "confirm_edit",
        "value": f"{material_system}: {label}",
        "confirmed_peak_assignment": confirmed_assignment,
        "learning_scope": learning_scope,
        "related_reference_ids": related_reference_ids,
    }
    record = clarifications.response_record(
        file_id,
        current_result["processing_id"],
        question,
        response_value,
    )
    with database.connect_database() as connection:
        database.insert_clarification_response(connection, record)
    try:
        processing_result = processing.process_imported_file(
            imported_file,
            RAW_DATA_DIR,
            PROCESSED_DATA_DIR,
            REFERENCE_DATA_DIR,
            current_result["model"]["parameters"].get(
                "deconvolution_profile", "none"
            ),
            current_result["model"]["parameters"].get(
                "substrate_correction_mode", "none"
            ),
            baseline_method=current_result["model"]["parameters"].get(
                "baseline_method", "morphological_opening"
            ),
            advanced_options=preserved_advanced_processing_options(
                current_result
            ),
        )
    except processing.ProcessingError as error:
        with database.connect_database() as connection:
            database.delete_clarification_response(
                connection,
                record["clarification_id"],
            )
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code, "message": error.message},
        ) from error
    learning = {
        "status": "excluded_as_instrument_artifact"
        if is_instrument_artifact
        else "active",
        "scope": learning_scope,
        "related_references": related_references,
    }
    if not is_instrument_artifact:
        learning["reference_id"] = f"peak-training-{record['clarification_id']}"
    return {
        "clarification": clarifications.public_response(record),
        "processing_result": processing_result,
        "learning": learning,
    }


@app.get("/clarifications/policy")
def clarification_policy():
    return {
        "version": clarifications.POLICY_VERSION,
        "maximum_questions_per_processing": 1,
        "rules": clarifications.POLICY_RULES,
    }


@app.get("/files/{file_id}/clarifications")
def list_file_clarifications(file_id: str):
    if database.get_imported_file(file_id) is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "file_not_found",
                "message": "No imported file has the requested ID.",
            },
        )
    try:
        result = processing.latest_processing_result(
            file_id,
            PROCESSED_DATA_DIR,
        )
    except processing.ProcessingError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code, "message": error.message},
        ) from error
    return {
        "policy_version": clarifications.POLICY_VERSION,
        "processing_id": result["processing_id"],
        "questions": clarifications.clarification_questions(file_id, result),
    }


@app.get("/files/{file_id}/clarifications/history")
def clarification_history(file_id: str):
    if database.get_imported_file(file_id) is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "file_not_found",
                "message": "No imported file has the requested ID.",
            },
        )
    return [
        clarifications.public_response(record)
        for record in database.list_clarification_responses(file_id)
    ]


@app.post("/files/{file_id}/clarifications/respond")
def respond_to_clarification(
    file_id: str,
    request: ClarificationResponseRequest,
):
    imported_file = database.get_imported_file(file_id)
    if imported_file is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "file_not_found",
                "message": "No imported file has the requested ID.",
            },
        )
    try:
        current_result = processing.latest_processing_result(
            file_id,
            PROCESSED_DATA_DIR,
        )
        questions = clarifications.clarification_questions(
            file_id,
            current_result,
        )
        question = next(
            (
                item
                for item in questions
                if item["question_key"] == request.question_key
            ),
            None,
        )
        if question is None:
            raise clarifications.ClarificationError(
                409,
                "clarification_not_pending",
                "This clarification is no longer pending.",
            )

        processing_result = None
        training_result = None
        metadata_result = None
        online_search_result = None
        record = None
        if request.dismissed:
            response_value = {"answer": "dismissed", "value": None}
            status = "dismissed"
        else:
            response_value = clarifications.validate_answer(
                question,
                request.answer or "",
                request.value,
            )
            status = "answered"
            confirmed_material = None
            if question["question_key"] == "material_identity":
                if response_value["answer"] == "known":
                    confirmed_material = response_value["value"]
            elif question["question_key"] == "material_conflict":
                if response_value["answer"] == "keep_declared":
                    confirmed_material = question["declared_material_system"]
                elif response_value["answer"] == "use_candidate":
                    confirmed_material = question["candidate_material_system"]
                elif response_value["answer"] == "custom":
                    confirmed_material = response_value["value"]

            if confirmed_material is not None:
                training_result = copilot.train_from_confirmed_file(
                    file_id,
                    confirmed_material,
                    PROCESSED_DATA_DIR,
                )
                processing_result = processing.process_imported_file(
                    imported_file,
                    RAW_DATA_DIR,
                    PROCESSED_DATA_DIR,
                    REFERENCE_DATA_DIR,
                    current_result["model"]["parameters"].get(
                        "deconvolution_profile",
                        "none",
                    ),
                    current_result["model"]["parameters"].get(
                        "substrate_correction_mode",
                        "none",
                    ),
                    baseline_method=current_result["model"]["parameters"].get(
                        "baseline_method",
                        "morphological_opening",
                    ),
                    advanced_options=preserved_advanced_processing_options(
                        current_result
                    ),
                )
                response_value["confirmed_material_system"] = confirmed_material

            if (
                question["question_key"] in {
                    "substrate_identity",
                    "substrate_role",
                }
                and response_value["answer"] in {"glass", "sio2_si"}
            ):
                metadata_result = persist_metadata_update(
                    file_id,
                    {"substrate": response_value["answer"]},
                    "substrate_confirmation",
                )
                imported_file = metadata_result
            if (
                question["question_key"] == "substrate_identity"
                and response_value["answer"] in {"glass", "sio2_si"}
            ):
                processing_result = processing.process_imported_file(
                    imported_file,
                    RAW_DATA_DIR,
                    PROCESSED_DATA_DIR,
                    REFERENCE_DATA_DIR,
                    current_result["model"]["parameters"].get(
                        "deconvolution_profile",
                        "none",
                    ),
                    response_value["answer"],
                    baseline_method=current_result["model"]["parameters"].get(
                        "baseline_method",
                        "morphological_opening",
                    ),
                    advanced_options=preserved_advanced_processing_options(
                        current_result
                    ),
                )
                response_value["applied_substrate_correction"] = response_value[
                    "answer"
                ]
            elif (
                question["question_key"] == "substrate_role"
                and response_value["answer"] in {"glass", "sio2_si"}
            ):
                processing_result = processing.process_imported_file(
                    imported_file,
                    RAW_DATA_DIR,
                    PROCESSED_DATA_DIR,
                    REFERENCE_DATA_DIR,
                    current_result["model"]["parameters"].get(
                        "deconvolution_profile",
                        "none",
                    ),
                    current_result["model"]["parameters"].get(
                        "substrate_correction_mode",
                        "none",
                    ),
                    baseline_method=current_result["model"]["parameters"].get(
                        "baseline_method",
                        "morphological_opening",
                    ),
                    advanced_options=preserved_advanced_processing_options(
                        current_result
                    ),
                )
            if question["question_key"].startswith("unassigned_peak:"):
                answer = response_value["answer"]
                if answer == "confirm_noise":
                    noise_evidence = question.get("singleton_noise_candidate")
                    if not isinstance(noise_evidence, dict):
                        raise clarifications.ClarificationError(
                            409,
                            "noise_candidate_changed",
                            "This peak is no longer a singleton noise candidate.",
                        )
                    response_value["confirmed_noise_exclusion"] = {
                        "observed_cm-1": float(
                            question["peak"]["position_cm-1"]
                        ),
                        "material_system": imported_file.get("material_system"),
                        "classification": "noise",
                        "scope": "current_file_derived_peak_analysis",
                        "evidence": noise_evidence,
                    }
                    response_value["learning_scope"] = (
                        "file_specific_noise_exclusion"
                    )
                    record = clarifications.response_record(
                        file_id,
                        current_result["processing_id"],
                        question,
                        response_value,
                        status,
                    )
                    with database.connect_database() as connection:
                        database.insert_clarification_response(connection, record)
                    processing_result = processing.process_imported_file(
                        imported_file,
                        RAW_DATA_DIR,
                        PROCESSED_DATA_DIR,
                        REFERENCE_DATA_DIR,
                        current_result["model"]["parameters"].get(
                            "deconvolution_profile",
                            "none",
                        ),
                        current_result["model"]["parameters"].get(
                            "substrate_correction_mode",
                            "none",
                        ),
                    baseline_method=current_result["model"]["parameters"].get(
                        "baseline_method",
                        "morphological_opening",
                    ),
                    advanced_options=preserved_advanced_processing_options(
                        current_result
                    ),
                )
                elif answer.startswith("candidate_"):
                    candidate_index = int(answer.rsplit("_", 1)[1])
                    candidates = question.get("candidate_assignments") or []
                    if candidate_index >= len(candidates):
                        raise clarifications.ClarificationError(
                            409,
                            "peak_candidate_changed",
                            "That peak candidate is no longer available; review the updated options.",
                        )
                    response_value["confirmed_peak_assignment"] = candidates[
                        candidate_index
                    ]
                    record = clarifications.response_record(
                        file_id,
                        current_result["processing_id"],
                        question,
                        response_value,
                        status,
                    )
                    with database.connect_database() as connection:
                        database.insert_clarification_response(connection, record)
                    processing_result = processing.process_imported_file(
                        imported_file,
                        RAW_DATA_DIR,
                        PROCESSED_DATA_DIR,
                        REFERENCE_DATA_DIR,
                        current_result["model"]["parameters"].get(
                            "deconvolution_profile",
                            "none",
                        ),
                        current_result["model"]["parameters"].get(
                            "substrate_correction_mode",
                            "none",
                        ),
                    baseline_method=current_result["model"]["parameters"].get(
                        "baseline_method",
                        "morphological_opening",
                    ),
                    advanced_options=preserved_advanced_processing_options(
                        current_result
                    ),
                )
                elif answer == "custom":
                    observed = float(question["peak"]["position_cm-1"])
                    confirmation_token = str(uuid4())
                    material_system = (
                        imported_file.get("material_system") or "Unknown"
                    )
                    evidence_identity = {
                        "confirmation_token": confirmation_token,
                        "file_id": file_id,
                        "source_sha256": imported_file["sha256"],
                        "processing_id": current_result["processing_id"],
                        "observed_cm-1": observed,
                        "material_system": material_system,
                        "label": response_value["value"],
                    }
                    response_value["confirmed_peak_assignment"] = {
                        "observed_cm-1": observed,
                        "material_system": material_system,
                        "label": response_value["value"],
                        "difference_cm-1": 0.0,
                        "reference": {
                            "reference_id": (
                                f"user-peak-confirmation-{confirmation_token}"
                            ),
                            "title": "User-confirmed peak assignment",
                            "citation": (
                                "Private user confirmation from an interactive "
                                "processing question; source file and processing "
                                "provenance retained."
                            ),
                            "source_url": None,
                            "sha256": hashlib.sha256(
                                clarifications.canonical_json(
                                    evidence_identity
                                ).encode("utf-8")
                            ).hexdigest(),
                            "provider": "user_confirmation",
                        },
                    }
                    response_value["learning_scope"] = (
                        "peak_assignment_for_matching_material_systems"
                    )
                    record = clarifications.response_record(
                        file_id,
                        current_result["processing_id"],
                        question,
                        response_value,
                        status,
                    )
                    with database.connect_database() as connection:
                        database.insert_clarification_response(connection, record)
                    processing_result = processing.process_imported_file(
                        imported_file,
                        RAW_DATA_DIR,
                        PROCESSED_DATA_DIR,
                        REFERENCE_DATA_DIR,
                        current_result["model"]["parameters"].get(
                            "deconvolution_profile",
                            "none",
                        ),
                        current_result["model"]["parameters"].get(
                            "substrate_correction_mode",
                            "none",
                        ),
                    baseline_method=current_result["model"]["parameters"].get(
                        "baseline_method",
                        "morphological_opening",
                    ),
                    advanced_options=preserved_advanced_processing_options(
                        current_result
                    ),
                )
                elif answer == "search_online":
                    online_search_result = online_references.sync_rod_catalog(
                        REFERENCE_DATA_DIR
                    )
                    response_value["online_reference_result"] = online_search_result
                    record = clarifications.response_record(
                        file_id,
                        current_result["processing_id"],
                        question,
                        response_value,
                        status,
                    )
                    with database.connect_database() as connection:
                        database.insert_clarification_response(connection, record)
                    processing_result = processing.process_imported_file(
                        imported_file,
                        RAW_DATA_DIR,
                        PROCESSED_DATA_DIR,
                        REFERENCE_DATA_DIR,
                        current_result["model"]["parameters"].get(
                            "deconvolution_profile",
                            "none",
                        ),
                        current_result["model"]["parameters"].get(
                            "substrate_correction_mode",
                            "none",
                        ),
                    baseline_method=current_result["model"]["parameters"].get(
                        "baseline_method",
                        "morphological_opening",
                    ),
                    advanced_options=preserved_advanced_processing_options(
                        current_result
                    ),
                )

        if record is None:
            record = clarifications.response_record(
                file_id,
                current_result["processing_id"],
                question,
                response_value,
                status,
            )
            with database.connect_database() as connection:
                database.insert_clarification_response(connection, record)
        return {
            "clarification": clarifications.public_response(record),
            "training": training_result,
            "metadata": metadata_result,
            "online_search": online_search_result,
            "processing_result": processing_result,
        }
    except clarifications.ClarificationError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code, "message": error.message},
        ) from error
    except copilot.CopilotError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code, "message": error.message},
        ) from error
    except processing.ProcessingError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code, "message": error.message},
        ) from error
    except online_references.OnlineReferenceError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code, "message": error.message},
        ) from error


def trusted_content_type(filename: str) -> tuple[str, bool]:
    suffix = Path(filename).suffix.casefold()
    inline_types = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }
    if suffix in inline_types:
        return inline_types[suffix], True
    guessed, _encoding = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream", False


@app.get("/files/{file_id}/integrity")
def verify_imported_file_integrity(file_id: str):
    record = database.get_imported_file(file_id)
    if record is None:
        raise validation_error(
            "file_not_found",
            "No imported file has the requested ID.",
            404,
        )
    try:
        stored_path = preview.resolve_stored_file(record, RAW_DATA_DIR)
        actual_size = stored_path.stat().st_size
        actual_sha256 = integrity.sha256_file(stored_path)
    except (preview.PreviewError, OSError) as error:
        if isinstance(error, preview.PreviewError):
            raise validation_error(error.code, error.message, error.status_code) from error
        raise validation_error(
            "stored_file_unavailable",
            "The retained raw file could not be verified.",
            409,
        ) from error
    return {
        "file_id": file_id,
        "filename": record["original_filename"],
        "valid": (
            actual_size == record["size_bytes"]
            and actual_sha256.lower() == record["sha256"].lower()
        ),
        "expected_sha256": record["sha256"],
        "actual_sha256": actual_sha256,
        "expected_size_bytes": record["size_bytes"],
        "actual_size_bytes": actual_size,
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/files/{file_id}/content")
def retrieve_imported_content(file_id: str):
    record = database.get_imported_file(file_id)
    if record is None:
        raise validation_error(
            "file_not_found",
            "No imported file has the requested ID.",
            404,
        )
    try:
        stored_path, actual_sha256 = preview.verify_stored_file(
            record,
            RAW_DATA_DIR,
        )
    except preview.PreviewError as error:
        raise validation_error(error.code, error.message, error.status_code) from error
    media_type, inline_allowed = trusted_content_type(record["original_filename"])
    response = FileResponse(
        stored_path,
        media_type=media_type,
        filename=record["original_filename"],
        content_disposition_type="inline" if inline_allowed else "attachment",
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
    response.headers["ETag"] = f'"{actual_sha256}"'
    return response


@app.get("/files/{file_id}")
def retrieve_imported_file(file_id: str):
    record = database.get_imported_file(file_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "file_not_found",
                "message": "No imported file has the requested ID.",
            },
        )
    return record


@app.patch("/files/{file_id}")
def edit_imported_file_metadata(
    file_id: str,
    request: ImportMetadataUpdateRequest,
):
    requested_fields = request.model_fields_set
    if not requested_fields:
        raise validation_error(
            "empty_metadata_update",
            "Provide at least one metadata field to edit.",
        )
    values = request.model_dump()
    updates = {}
    for field_name in requested_fields:
        value = values[field_name]
        if field_name == "technique":
            updates[field_name] = normalize_required_metadata(
                value or "", "Technique", 100
            )
        elif field_name == "sample_id":
            updates[field_name] = normalize_required_metadata(
                value or "", "Sample ID", 100
            )
        elif field_name == "material_system":
            updates[field_name] = normalize_material_system(value)
        elif field_name == "measurement_date":
            updates[field_name] = normalize_measurement_date(value)
        elif field_name == "instrument":
            updates[field_name] = normalize_optional_metadata(
                value, "Instrument", 200
            )
        elif field_name == "operator":
            updates[field_name] = normalize_optional_metadata(
                value, "Operator", 100
            )
        elif field_name == "notes":
            updates[field_name] = normalize_optional_metadata(value, "Notes", 2000)
        elif field_name == "substrate":
            updates[field_name] = normalize_substrate(value)
        elif field_name == "measurement_role":
            updates[field_name] = normalize_measurement_role(value)
    existing = database.get_imported_file(file_id)
    if existing is None:
        raise validation_error(
            "file_not_found",
            "No imported file has the requested ID.",
            404,
        )
    candidate = {**existing, **updates}
    if (
        candidate.get("measurement_role") == "pure_substrate_reference"
        and candidate.get("substrate") not in {"glass", "sio2_si"}
    ):
        raise validation_error(
            "substrate_required_for_reference",
            "A pure-substrate reference must identify glass or SiO2/Si as its substrate.",
        )
    return persist_metadata_update(file_id, updates)


@app.delete("/files/{file_id}")
def archive_imported_file(file_id: str):
    record = database.get_imported_file(file_id)
    if record is None:
        raise validation_error(
            "file_not_found",
            "No imported file has the requested ID.",
            404,
        )
    if record.get("archived_at") is not None:
        return record
    archived_at = datetime.now(timezone.utc).isoformat()
    updated = persist_metadata_update(
        file_id,
        {"archived_at": archived_at},
        "archive",
    )
    return {
        **updated,
        "retention": "Raw bytes and provenance were preserved; only list visibility changed.",
    }


@app.post("/files/{file_id}/restore")
def restore_imported_file(file_id: str):
    return persist_metadata_update(
        file_id,
        {"archived_at": None},
        "restore",
    )


@app.get("/files/{file_id}/metadata/history")
def imported_file_metadata_history(file_id: str):
    if database.get_imported_file(file_id) is None:
        raise validation_error(
            "file_not_found",
            "No imported file has the requested ID.",
            404,
        )
    response = []
    for record in database.list_import_metadata_revisions(file_id):
        response.append(
            {
                **record,
                "previous": json.loads(record["previous_json"]),
                "updated": json.loads(record["updated_json"]),
            }
        )
        response[-1].pop("previous_json")
        response[-1].pop("updated_json")
    return response


@app.post("/files/import", status_code=201)
async def import_file(
    file: UploadFile = File(...),
    relative_path: str | None = Form(None),
    technique: str = Form(...),
    material_system: str = Form("Unknown"),
    sample_id: str = Form(...),
    measurement_date: str | None = Form(None),
    instrument: str | None = Form(None),
    operator: str | None = Form(None),
    notes: str | None = Form(None),
    substrate: str = Form("unknown"),
    measurement_role: str = Form("unspecified"),
):
    staging_path = None
    destination_directory = None
    destination_path = None
    destination_created = False

    try:
        original_filename = extract_original_filename(file.filename)
        stored_filename = sanitize_filename(original_filename)
        normalized_technique = normalize_required_metadata(
            technique,
            "Technique",
            100,
        )
        normalized_relative_path = normalize_optional_metadata(
            relative_path,
            "Relative path",
            1000,
        )
        normalized_material_system = normalize_material_system(
            material_system
        )
        normalized_sample_id = normalize_required_metadata(
            sample_id,
            "Sample ID",
            100,
        )
        normalized_measurement_date = normalize_measurement_date(
            measurement_date
        )
        normalized_instrument = normalize_optional_metadata(
            instrument,
            "Instrument",
            200,
        )
        normalized_operator = normalize_optional_metadata(
            operator,
            "Operator",
            100,
        )
        normalized_notes = normalize_optional_metadata(notes, "Notes", 2000)
        normalized_substrate = normalize_substrate(substrate)
        normalized_measurement_role = normalize_measurement_role(
            measurement_role
        )
        if (
            normalized_measurement_role == "pure_substrate_reference"
            and normalized_substrate not in {"glass", "sio2_si"}
        ):
            raise validation_error(
                "substrate_required_for_reference",
                "A pure-substrate reference must identify glass or SiO2/Si as its substrate.",
            )

        file_id = str(uuid4())
        staging_directory = RAW_DATA_DIR / ".staging"
        staging_directory.mkdir(parents=True, exist_ok=True)
        staging_path = staging_directory / f"{file_id}.upload"

        size_bytes, sha256 = await stream_upload_to_staging(
            file,
            staging_path,
        )
        imported_at = datetime.now(timezone.utc).isoformat()
        content_type = file.content_type

        destination_directory = RAW_DATA_DIR / file_id
        destination_path = destination_directory / stored_filename
        metadata = {
            "file_id": file_id,
            "original_filename": original_filename,
            "content_type": content_type,
            "size_bytes": size_bytes,
            "sha256": sha256,
            "storage_path": str(destination_path),
            "imported_at": imported_at,
            "relative_path": normalized_relative_path,
            "technique": normalized_technique,
            "material_system": normalized_material_system,
            "sample_id": normalized_sample_id,
            "measurement_date": normalized_measurement_date,
            "instrument": normalized_instrument,
            "operator": normalized_operator,
            "notes": normalized_notes,
            "substrate": normalized_substrate,
            "measurement_role": normalized_measurement_role,
            "updated_at": None,
            "archived_at": None,
        }

        with database.connect_database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = database.find_imported_file_by_sha256(
                connection,
                sha256,
            )
            if duplicate is not None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "duplicate_file",
                        "message": "An identical file has already been imported.",
                        "existing_file_id": duplicate["file_id"],
                    },
                )

            destination_directory.mkdir(parents=True, exist_ok=False)
            destination_created = True
            os.replace(staging_path, destination_path)
            database.insert_imported_file(connection, metadata)
            database.upsert_sample(
                connection,
                {
                    "sample_id": normalized_sample_id,
                    "material_system": normalized_material_system,
                    "substrate": normalized_substrate,
                    "project": None,
                    "notes": normalized_notes,
                    "created_at": imported_at,
                    "updated_at": imported_at,
                },
            )

        return metadata
    except HTTPException:
        if destination_created:
            cleanup_created_destination(
                destination_path,
                destination_directory,
            )
        raise
    except (OSError, sqlite3.Error) as error:
        cleanup_failed = False
        if destination_created:
            try:
                cleanup_created_destination(
                    destination_path,
                    destination_directory,
                )
            except OSError:
                cleanup_failed = True
                logger.exception("Failed to roll back an incomplete file import")
        logger.exception("File import failed and was rolled back")
        message = "The import failed; no file or metadata was retained."
        if cleanup_failed:
            message = (
                "The import failed and automatic cleanup was incomplete. "
                "Inspect the raw-data directory before retrying."
            )
        raise HTTPException(
            status_code=500,
            detail={"code": "import_failed", "message": message},
        ) from error
    finally:
        await file.close()
        if staging_path is not None and staging_path.exists():
            try:
                staging_path.unlink()
            except OSError:
                logger.exception("Failed to remove a staged upload")
