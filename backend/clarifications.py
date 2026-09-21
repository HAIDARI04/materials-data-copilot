import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4

import database

POLICY_VERSION = "1.4.0"
POLICY_RULES = (
    {
        "question_key": "substrate_role",
        "purpose": "Determine whether SiO2 named in a material label is the substrate.",
        "trigger": (
            "The material label contains SiO2, but substrate metadata is unknown."
        ),
    },
    {
        "question_key": "substrate_identity",
        "purpose": "Resolve plausible substrate bands before subtracting them.",
        "trigger": (
            "Detection or automatic mode found curated evidence with score >= 0.25 "
            "or recurring cross-sample evidence, but no substrate was subtracted."
        ),
    },
    {
        "question_key": "material_conflict",
        "purpose": "Resolve a conflict between declared metadata and reference evidence.",
        "trigger": "A declared material conflicts with an accepted reference candidate.",
    },
    {
        "question_key": "material_identity",
        "purpose": "Create a user-confirmed training exemplar when identity is unresolved.",
        "trigger": "Material identification is unknown or ambiguous with at least two peaks.",
    },
    {
        "question_key": "unassigned_peak:<position>:<reference_snapshot>",
        "purpose": "Review detected peaks that remain without an evidence-backed label.",
        "trigger": (
            "A reportable detected peak remains unassigned after instrument-artifact "
            "and substrate handling. Recurrence in at least three spectra with the "
            "same material system prioritizes an online-reference recommendation; "
            "a noise-level singleton among multiple same-system spectra asks for "
            "confirmation before exclusion."
        ),
    },
)


class ClarificationError(Exception):
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


def question_was_resolved(file_id: str, question_key: str) -> bool:
    return database.get_latest_clarification_response(file_id, question_key) is not None


def clarification_questions(file_id: str, result: dict) -> list[dict]:
    questions = []
    substrate = result.get("substrate_correction") or {}
    imported_file = database.get_imported_file(file_id) or {}
    material_text = "".join(
        character
        for character in (imported_file.get("material_system") or "").casefold()
        if character.isalnum()
    )
    if (
        ("sio2" in material_text or "silicondioxide" in material_text)
        and imported_file.get("substrate") not in {
            "glass",
            "sio2_si",
            "no_substrate",
        }
        and not question_was_resolved(file_id, "substrate_role")
    ):
        questions.append(
            {
                "question_key": "substrate_role",
                "kind": "single_choice",
                "prompt": "What role does SiO2 have in this sample?",
                "explanation": (
                    "The material label contains SiO2, but that alone does not "
                    "show whether it is the substrate or a measured component."
                ),
                "options": [
                    {
                        "value": "sio2_si",
                        "label": "SiO2 / crystalline Si substrate",
                    },
                    {"value": "glass", "label": "Glass / SiO2 substrate"},
                    {
                        "value": "material_component",
                        "label": "SiO2 is a material component",
                    },
                    {"value": "unknown", "label": "I do not know"},
                ],
                "allows_text": False,
            }
        )
    learned_evidence = any(
        band.get("evidence_source") == "user_confirmed_cross_sample"
        for band in substrate.get("matched_bands", [])
    )
    if (
        substrate.get("requested_mode") in {"detect", "auto"}
        and substrate.get("status") == "detected"
        and substrate.get("correction") == "none"
        and (
            float(substrate.get("score") or 0.0) >= 0.25
            or learned_evidence
        )
        and imported_file.get("substrate") not in {
            "glass",
            "sio2_si",
            "no_substrate",
        }
        and not question_was_resolved(file_id, "substrate_identity")
    ):
        detected = substrate.get("detected_substrate", "unknown").replace("_", "/")
        questions.append(
            {
                "question_key": "substrate_identity",
                "kind": "single_choice",
                "prompt": "What substrate was used for this measurement?",
                "explanation": (
                    f"The spectrum has {detected} features"
                    + (
                        " also recurring in user-confirmed substrate samples"
                        if learned_evidence
                        else ""
                    )
                    + ", but the evidence is not strong enough for safe "
                    "automatic subtraction."
                ),
                "options": [
                    {"value": "glass", "label": "Glass / SiO2"},
                    {"value": "sio2_si", "label": "SiO2 / crystalline Si"},
                    {"value": "no_substrate", "label": "No substrate contribution"},
                    {"value": "unknown", "label": "I do not know"},
                ],
                "allows_text": False,
            }
        )

    identification = result.get("summary", {}).get("identification", {})
    if (
        identification.get("status") == "declared"
        and identification.get("reference_assessment") == "conflicting_candidate"
        and identification.get("candidates")
        and not question_was_resolved(file_id, "material_conflict")
    ):
        candidate = identification["candidates"][0]["material_system"]
        declared = identification["material_system"]
        questions.append(
            {
                "question_key": "material_conflict",
                "kind": "single_choice",
                "prompt": "Which material-system label should be confirmed?",
                "explanation": (
                    f"The imported label is {declared}, while the best reference "
                    f"candidate is {candidate}."
                ),
                "options": [
                    {"value": "keep_declared", "label": f"Keep {declared}"},
                    {"value": "use_candidate", "label": f"Use {candidate}"},
                    {"value": "unknown", "label": "Leave unresolved"},
                ],
                "allows_text": False,
                "declared_material_system": declared,
                "candidate_material_system": candidate,
            }
        )
    elif (
        identification.get("status") in {"unknown", "ambiguous"}
        and int(result.get("summary", {}).get("peak_count") or 0) >= 2
        and not question_was_resolved(file_id, "material_identity")
    ):
        questions.append(
            {
                "question_key": "material_identity",
                "kind": "material_text",
                "prompt": "Do you know the independently verified material system?",
                "explanation": (
                    "A confirmed name will create a versioned training exemplar; "
                    "choose unknown rather than guessing."
                ),
                "options": [
                    {"value": "known", "label": "Enter confirmed material"},
                    {"value": "unknown", "label": "I do not know"},
                ],
                "allows_text": True,
            }
        )
    reference_snapshot = (
        result.get("model", {})
        .get("parameters", {})
        .get("reference_library_sha256", "no-reference-snapshot")
    )
    for peak in result.get("summary", {}).get("unassigned_peaks", []):
        position = float(peak["position_cm-1"])
        recurrence = peak.get("recurrence")
        singleton_noise = peak.get("singleton_noise_candidate")
        recurrence_suffix = ""
        if recurrence:
            recurrence_sha256 = hashlib.sha256(
                canonical_json(recurrence).encode("utf-8")
            ).hexdigest()
            recurrence_suffix = f":recurring:{recurrence_sha256[:12]}"
        elif singleton_noise:
            noise_sha256 = hashlib.sha256(
                canonical_json(singleton_noise).encode("utf-8")
            ).hexdigest()
            recurrence_suffix = f":singleton-noise:{noise_sha256[:12]}"
        question_key = (
            f"unassigned_peak:{position:.3f}:{reference_snapshot[:12]}"
            f"{recurrence_suffix}"
        )
        if question_was_resolved(file_id, question_key):
            continue
        candidates = peak.get("candidate_assignments") or []
        online_search = peak.get("online_reference_search") or {}
        options = [
            {
                "value": f"candidate_{index}",
                "label": (
                    f"Confirm {candidate['material_system']} — "
                    f"{candidate['label']} (Δ {candidate['difference_cm-1']:.1f} cm-1)"
                ),
            }
            for index, candidate in enumerate(candidates)
        ]
        options.extend(
            [
                {
                    "value": "search_online",
                    "label": "Search / update the online Raman library",
                },
                {
                    "value": "leave_unassigned",
                    "label": "Keep this peak unassigned",
                },
            ]
        )
        if singleton_noise:
            options.insert(
                0,
                {
                    "value": "confirm_noise",
                    "label": "Confirm this singleton feature as noise",
                },
            )
        for index, candidate in enumerate(candidates):
            if candidate.get("recommended"):
                options[index]["label"] = (
                    f"Recommended online match: {candidate['material_system']} - "
                    f"{candidate['label']} "
                    f"(difference {candidate['difference_cm-1']:.1f} cm-1)"
                )
        if online_search.get("status") == "library_update_required":
            search_index = next(
                index
                for index, option in enumerate(options)
                if option["value"] == "search_online"
            )
            options.insert(0, options.pop(search_index))
        if singleton_noise:
            prompt = f"Is the peak at {position:.1f} cm-1 noise?"
            explanation = (
                f"This low-prominence unassigned feature appears only in this "
                f"spectrum among {singleton_noise['same_system_spectrum_count']} "
                "spectra with the same material system. Its prominence is within "
                "the measured noise-level boundary. Confirming noise will exclude "
                "it from derived peak analysis without changing any spectral series "
                "or the raw file."
            )
        elif recurrence:
            prompt = f"Confirm the recurring peak at {position:.1f} cm-1"
            if online_search.get("status") == "candidate_found":
                best = online_search["most_probable_assignment"]
                explanation = (
                    f"This unassigned peak appears in {recurrence['observed_count']} "
                    f"spectra with the same material system. The best current "
                    f"online-reference match is {best['material_system']} - "
                    f"{best['label']} from {best['reference']['title']}. "
                    "The assignment remains provisional until you confirm it."
                )
            else:
                explanation = (
                    f"This unassigned peak appears in {recurrence['observed_count']} "
                    "spectra with the same material system, but the current online "
                    "reference snapshot has no close assignment. Update the online "
                    "library, then review the new evidence before confirming."
                )
        else:
            prompt = f"Review the unassigned peak at {position:.1f} cm-1"
            explanation = (
                "This is a reportable non-artifact peak. "
                + (
                    "Reference matches are provisional until you confirm one."
                    if candidates
                    else "No current reference provides a close assignment."
                )
            )
        questions.append(
            {
                "question_key": question_key,
                "kind": "single_choice",
                "prompt": prompt,
                "explanation": explanation,
                "options": options,
                "allows_text": False,
                "peak": peak,
                "candidate_assignments": candidates,
                "reference_library_sha256": reference_snapshot,
                "recurrence": recurrence,
                "singleton_noise_candidate": singleton_noise,
                "online_reference_search": online_search,
            }
        )
        break
    for question in questions:
        question["allows_text"] = True
        if question["question_key"] != "material_identity":
            question["options"].append(
                {"value": "custom", "label": "Type my own answer"}
            )
    questions.sort(
        key=lambda question: (
            0
            if question["question_key"] in {"substrate_role", "substrate_identity"}
            else 1
            if question.get("recurrence")
            and question.get("online_reference_search", {}).get("status")
            in {"candidate_found", "library_update_required"}
            else 2
            if question.get("singleton_noise_candidate")
            else 3
        )
    )
    return questions[:1]


def validate_answer(question: dict, answer: str, value: str | None) -> dict:
    allowed = {option["value"] for option in question["options"]}
    if answer not in allowed:
        raise ClarificationError(
            422,
            "invalid_clarification_answer",
            "Choose one of the answers offered for this question.",
        )
    normalized_value = " ".join((value or "").split()) or None
    if answer == "custom":
        if normalized_value is None:
            raise ClarificationError(
                422,
                "custom_answer_required",
                "Type an answer before applying it.",
            )
        maximum = 200 if question["question_key"] == "material_conflict" else 500
        if len(normalized_value) > maximum:
            raise ClarificationError(
                422,
                "custom_answer_too_long",
                f"The answer must be {maximum} characters or fewer.",
            )
    if question["question_key"] == "material_identity" and answer == "known":
        if normalized_value is None:
            raise ClarificationError(
                422,
                "material_label_required",
                "Enter the independently verified material-system name.",
            )
        if len(normalized_value) > 200:
            raise ClarificationError(
                422,
                "material_label_too_long",
                "The material-system name must be 200 characters or fewer.",
            )
    return {"answer": answer, "value": normalized_value}


def response_record(
    file_id: str,
    processing_id: str,
    question: dict,
    response: dict,
    status: str = "answered",
) -> dict:
    return {
        "clarification_id": str(uuid4()),
        "file_id": file_id,
        "processing_id": processing_id,
        "question_key": question["question_key"],
        "question_json": canonical_json(question),
        "response_json": canonical_json(response),
        "status": status,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def public_response(record: dict) -> dict:
    public_record = {
        **record,
        "question": json.loads(record["question_json"]),
        "response": json.loads(record["response_json"]),
    }
    public_record.pop("question_json", None)
    public_record.pop("response_json", None)
    return public_record
