import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import database
import processing
import research_store
import research_analysis

MAX_CHAT_MESSAGE_LENGTH = 4000
MAX_MATERIAL_SYSTEM_LENGTH = 200


class CopilotError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def normalize_message(value: str) -> str:
    normalized = " ".join(value.split())
    if not normalized:
        raise CopilotError(422, "empty_message", "Enter a message for the Copilot.")
    if len(normalized) > MAX_CHAT_MESSAGE_LENGTH:
        raise CopilotError(
            422,
            "message_too_long",
            f"Messages must not exceed {MAX_CHAT_MESSAGE_LENGTH} characters.",
        )
    return normalized


def normalize_material_system(value: str) -> str:
    normalized = " ".join(value.split())
    if not normalized or normalized.casefold() in {
        "unknown",
        "unknown system",
        "not known",
        "unspecified",
    }:
        raise CopilotError(
            422,
            "invalid_training_label",
            "A confirmed training label must name a known material system.",
        )
    if len(normalized) > MAX_MATERIAL_SYSTEM_LENGTH:
        raise CopilotError(
            422,
            "invalid_training_label",
            "The material-system label is too long.",
        )
    return normalized


def message_record(
    role: str,
    content: str,
    intent: str,
    file_id: str | None,
    metadata: dict | None = None,
) -> dict:
    return {
        "message_id": str(uuid4()),
        "file_id": file_id,
        "role": role,
        "content": content,
        "intent": intent,
        "metadata_json": canonical_json(metadata or {}),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def public_message(record: dict) -> dict:
    result = {**record, "metadata": json.loads(record["metadata_json"])}
    result.pop("metadata_json")
    return result


def latest_result(file_id: str, processed_directory: Path) -> dict | None:
    run = database.get_latest_processing_run(file_id)
    if run is None:
        return None
    if run['model_name'].startswith('research_'):
        return research_analysis.load(run, processed_directory.resolve())
    return processing.load_processing_result(run, processed_directory.resolve())


def answer_question(
    content: str,
    file_id: str | None,
    processed_directory: Path,
) -> tuple[str, str, dict]:
    lowered = content.casefold()
    if file_id is None or any(term in lowered for term in ('evidence','history','planned','correction','conversation')):
        matches = research_store.search(content, limit=5)
        if matches:
            passages = []
            for index, match in enumerate(matches, 1):
                source = match['citation'].get('url') or match['citation'].get('source') or match['citation'].get('processing_id')
                passages.append(f'[{index}] {match["kind"]}: {match["title"]}\n{match["excerpt"]}\nSource: {source}')
            return ('Relevant retained source passages follow. These preserve the original claims and roles; they do not establish a mechanism or completed planned work.\n\n'+'\n\n'.join(passages),
                    'research_evidence', {'citations':matches,'method':'local_source_retrieval'})
    if file_id is None:
        file_count = len(database.list_imported_files())
        training_count = len(database.list_training_examples(active_only=True))
        return (
            f"I can inspect {file_count} imported files and use "
            f"{training_count} active user-confirmed training exemplars. "
            "Select a file to discuss its identification, peaks, noise, "
            "baseline, or provenance.",
            "workspace_summary",
            {"file_count": file_count, "active_training_count": training_count},
        )

    metadata = database.get_imported_file(file_id)
    if metadata is None:
        raise CopilotError(404, "file_not_found", "No imported file has that ID.")
    result = latest_result(file_id, processed_directory)
    if result is None:
        return (
            f"{metadata['original_filename']} has not been processed yet. "
            "Run processing before asking about identification or peaks.",
            "processing_required",
            {},
        )

    if result.get('model_name', '').startswith('research_'):
        return (f'Saved {result["model_name"].replace("research_", "")} result for {metadata["original_filename"]}: '
                + json.dumps(result['summary'], ensure_ascii=False)
                + f'. Source SHA-256: {result["source_sha256"]}. Review acquisition choices and warnings in the measurement workspace.',
                'research_result_summary', {'processing_id':result['processing_id'],'source_sha256':result['source_sha256'],
                'result_sha256':result['result_sha256'],'warnings':result.get('warnings',[])})

    identification = result["summary"]["identification"]
    material = identification.get("material_system") or "Unknown"
    confidence = identification.get("confidence", "none")
    candidates = identification.get("candidates", [])
    top_score = candidates[0]["score"] if candidates else None
    if any(word in lowered for word in ("peak", "band", "shift")):
        positions = [round(peak["position_cm-1"], 2) for peak in result["peaks"]]
        answer = (
            f"I detected {len(positions)} peaks in {metadata['original_filename']}: "
            + ", ".join(f"{value:g}" for value in positions)
            + " cm⁻¹. These are derived values; the raw spectrum is unchanged."
        )
        return answer, "peak_summary", {"peak_positions_cm-1": positions}
    if any(word in lowered for word in ("noise", "baseline", "smooth", "denois")):
        parameters = result["model"]["parameters"]
        return (
            "The current result does not smooth measured intensities. It uses "
            "morphological baseline estimation after any traceable instrument-artifact "
            "correction. This preserves narrow peaks, while baseline removal does not "
            "remove random noise. All parameters remain recorded with the result.",
            "processing_explanation",
            {"model_parameters": parameters},
        )
    provenance_terms = ("checksum", "source", "provenance", "reference")
    if any(word in lowered for word in provenance_terms):
        reference = candidates[0].get("reference") if candidates else None
        return (
            f"The source SHA-256 is {result['source_sha256']}. "
            + (
                f"The leading evidence is {reference['title']} from "
                f"{reference.get('provider', 'the reference library')}."
                if reference
                else "No reference passed the identification threshold."
            ),
            "provenance_summary",
            {"source_sha256": result["source_sha256"], "reference": reference},
        )
    return (
        f"{metadata['original_filename']} is classified as {material} with "
        f"{confidence} confidence"
        + (f" (match score {top_score:.3f})." if top_score is not None else ".")
        + " You can ask about its peaks, processing, noise, or provenance. "
        "Use the explicit training control only when you have independently "
        "confirmed the material label.",
        "identification_summary",
        {"material_system": material, "confidence": confidence, "score": top_score},
    )


def chat(
    content: str,
    file_id: str | None,
    processed_directory: Path,
    provider: str = "local",
    share_context: bool = False,
) -> dict:
    normalized = normalize_message(content)
    if file_id is not None and database.get_imported_file(file_id) is None:
        raise CopilotError(404, "file_not_found", "No imported file has that ID.")
    if provider == "local":
        answer, intent, metadata = answer_question(
            normalized,
            file_id,
            processed_directory,
        )
        metadata = {
            **metadata,
            "provider": "local",
            "shared_spectrum_context": False,
            "raw_file_shared": False,
        }
    else:
        from ai_providers import external_chat

        answer, metadata = external_chat(
            provider,
            normalized,
            file_id,
            share_context,
            processed_directory,
        )
        intent = "external_provider_chat"
    user_message = message_record(
        "user",
        normalized,
        intent,
        file_id,
        {
            "provider": provider,
            "shared_spectrum_context": share_context if provider != "local" else False,
            "raw_file_shared": False,
        },
    )
    assistant_message = message_record(
        "assistant", answer, intent, file_id, metadata
    )
    with database.connect_database() as connection:
        database.insert_copilot_message(connection, user_message)
        database.insert_copilot_message(connection, assistant_message)
    return {
        "user_message": public_message(user_message),
        "assistant_message": public_message(assistant_message),
    }


def train_from_confirmed_file(
    file_id: str,
    material_system: str,
    processed_directory: Path,
) -> dict:
    label = normalize_material_system(material_system)
    imported_file = database.get_imported_file(file_id)
    if imported_file is None:
        raise CopilotError(404, "file_not_found", "No imported file has that ID.")
    run = database.get_latest_processing_run(file_id)
    if run is None:
        raise CopilotError(
            409,
            "processing_required",
            "Process the spectrum before using it as a training exemplar.",
        )
    result = processing.load_processing_result(run, processed_directory.resolve())
    peaks = [
        {
            "position_cm-1": peak["position_cm-1"],
            "prominence": peak.get("prominence"),
        }
        for peak in result.get("peaks", [])
    ]
    if len(peaks) < 2:
        raise CopilotError(
            422,
            "insufficient_training_evidence",
            "At least two detected peaks are required for a training exemplar.",
        )
    confirmed_at = datetime.now(timezone.utc).isoformat()
    training_id = str(uuid4())
    evidence = {
        "status": "ready",
        "provider": "user_confirmed_training",
        "license": "private",
        "peaks": peaks,
        "match_tolerance_cm-1": 12.0,
        "source_file_id": file_id,
        "source_processing_id": result["processing_id"],
        "source_sha256": result["source_sha256"],
        "result_sha256": result["result_sha256"],
    }
    example = {
        "training_id": training_id,
        "file_id": file_id,
        "processing_id": result["processing_id"],
        "material_system": label,
        "source_sha256": result["source_sha256"],
        "result_sha256": result["result_sha256"],
        "confirmed_at": confirmed_at,
        "confirmed_by": "local_user",
        "active": 1,
        "evidence_json": canonical_json(evidence),
    }
    user_text = f"Confirm {label} as the material system for this spectrum."
    assistant_text = (
        f"Saved {imported_file['original_filename']} as a private, versioned "
        f"training exemplar for {label}. It can support identification of other "
        "spectra, but it is excluded when evaluating itself."
    )
    user_message = message_record("user", user_text, "confirm_training", file_id)
    assistant_message = message_record(
        "assistant",
        assistant_text,
        "confirm_training",
        file_id,
        {"training_id": training_id, "material_system": label},
    )
    with database.connect_database() as connection:
        database.insert_training_example(connection, example)
        database.insert_copilot_message(connection, user_message)
        database.insert_copilot_message(connection, assistant_message)
    return {
        "training_id": training_id,
        "file_id": file_id,
        "material_system": label,
        "source_sha256": result["source_sha256"],
        "result_sha256": result["result_sha256"],
        "confirmed_at": confirmed_at,
        "active": True,
        "assistant_message": public_message(assistant_message),
    }


def training_references(exclude_file_id: str | None = None) -> list[dict]:
    references = []
    for example in database.list_training_examples(
        active_only=True,
        exclude_file_id=exclude_file_id,
    ):
        identity = canonical_json(
            {
                "training_id": example["training_id"],
                "material_system": example["material_system"],
                "source_sha256": example["source_sha256"],
                "result_sha256": example["result_sha256"],
                "evidence_json": example["evidence_json"],
            }
        ).encode("utf-8")
        references.append(
            {
                "reference_id": f"training-{example['training_id']}",
                "original_filename": None,
                "content_type": "application/vnd.materials.training+json",
                "size_bytes": len(identity),
                "sha256": hashlib.sha256(identity).hexdigest(),
                "storage_path": None,
                "imported_at": example["confirmed_at"],
                "source_kind": "confirmed_measurement",
                "technique": "Raman",
                "material_system": example["material_system"],
                "title": "User-confirmed Raman training exemplar",
                "citation": (
                    "Private user-confirmed measurement; source file and "
                    "processing checksums retained."
                ),
                "source_url": None,
                "extraction_status": "ready",
                "evidence_json": example["evidence_json"],
            }
        )
    return references


def message_history(file_id: str | None = None) -> list[dict]:
    return [
        public_message(record)
        for record in database.list_copilot_messages(file_id)
    ]
