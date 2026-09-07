import json
import re
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError

import preview
import processing

SUPPORTED_SPECTRUM_SUFFIXES = {".csv", ".dat", ".tsv", ".txt", ".xy"}
MAX_PDF_PAGES = 500
MAX_EXTRACTED_CHARACTERS = 5_000_000
MAX_EXTRACTED_PEAKS = 1000
RAMAN_SHIFT_PATTERN = re.compile(
    r"(?<![\d.])(\d{2,4}(?:\.\d+)?)\s*"
    r"(?:cm\s*(?:[-\u2212\u2013]\s*1|\^\s*[-\u2212]?\s*1)|cm-1)",
    re.IGNORECASE,
)
DOI_PATTERN = re.compile(
    r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+",
    re.IGNORECASE,
)
KNOWN_MATERIAL_PATTERNS = (
    (re.compile(r"\b(?:single[- ]?wall(?:ed)?\s+|multi[- ]?wall(?:ed)?\s+)?carbon\s+nanotubes?\b", re.I), "Carbon nanotube"),
    (re.compile(r"\bCNTs?\b"), "Carbon nanotube"),
    (re.compile(r"\breduced\s+graphene\s+oxide\b", re.I), "Reduced graphene oxide"),
    (re.compile(r"\bgraphene\s+oxide\b", re.I), "Graphene oxide"),
    (re.compile(r"\bgraphene\b", re.I), "Graphene"),
    (re.compile(r"\bgraphite\b", re.I), "Graphite"),
    (re.compile(r"\banatase\b", re.I), "Anatase TiO2"),
    (re.compile(r"\brutile\b", re.I), "Rutile TiO2"),
)
FORMULA_PATTERN = re.compile(r"\b(?:[A-Z][a-z]?\d*(?:\.\d+)?){2,}\b")
ELEMENT_SYMBOLS = {
    "H", "He", "Li", "Be", "B", "C", "N", "O", "F", "Ne", "Na",
    "Mg", "Al", "Si", "P", "S", "Cl", "Ar", "K", "Ca", "Sc", "Ti",
    "V", "Cr", "Mn", "Fe", "Co", "Ni", "Cu", "Zn", "Ga", "Ge", "As",
    "Se", "Br", "Kr", "Rb", "Sr", "Y", "Zr", "Nb", "Mo", "Tc", "Ru",
    "Rh", "Pd", "Ag", "Cd", "In", "Sn", "Sb", "Te", "I", "Xe", "Cs",
    "Ba", "La", "Ce", "Pr", "Nd", "Pm", "Sm", "Eu", "Gd", "Tb", "Dy",
    "Ho", "Er", "Tm", "Yb", "Lu", "Hf", "Ta", "W", "Re", "Os", "Ir",
    "Pt", "Au", "Hg", "Tl", "Pb", "Bi", "Po", "At", "Rn", "Fr", "Ra",
    "Ac", "Th", "Pa", "U", "Np", "Pu", "Am", "Cm", "Bk", "Cf", "Es",
    "Fm", "Md", "No", "Lr", "Rf", "Db", "Sg", "Bh", "Hs", "Mt", "Ds",
    "Rg", "Cn", "Nh", "Fl", "Mc", "Lv", "Ts", "Og",
}


class ReferenceError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def source_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix in SUPPORTED_SPECTRUM_SUFFIXES:
        return "spectrum"
    raise ReferenceError(
        415,
        "unsupported_reference_format",
        "References must be PDF, CSV, TSV, DAT, TXT, or XY files.",
    )


def nearby_excerpt(text: str, start: int, end: int) -> str:
    excerpt = " ".join(
        text[max(0, start - 80) : min(len(text), end + 80)].split()
    )
    return excerpt[:240]


def clean_pdf_metadata(value: object) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(str(value).replace("\x00", "").split()).strip()
    return cleaned or None


def first_title_line(text: str) -> str | None:
    for raw_line in text.splitlines()[:40]:
        line = " ".join(raw_line.split()).strip()
        if 8 <= len(line) <= 500 and not DOI_PATTERN.fullmatch(line):
            return line
    return None


def valid_formula(candidate: str) -> bool:
    symbols = re.findall(r"[A-Z][a-z]?", candidate)
    return len(symbols) >= 2 and all(symbol in ELEMENT_SYMBOLS for symbol in symbols)


def material_candidates(title: str, text: str) -> list[str]:
    candidates = []
    search_text = f"{title}\n{text[:100_000]}"
    for pattern, canonical_name in KNOWN_MATERIAL_PATTERNS:
        if pattern.search(search_text) and canonical_name not in candidates:
            candidates.append(canonical_name)
    for scope in (title, text[:20_000]):
        for match in FORMULA_PATTERN.finditer(scope):
            formula = match.group(0)
            if valid_formula(formula) and formula not in candidates:
                candidates.append(formula)
            if len(candidates) >= 5:
                return candidates
    return candidates


def detected_technique(text: str) -> str:
    normalized = text.casefold()
    if "raman" in normalized:
        return "Raman"
    if "x-ray diffraction" in normalized or "xrd" in normalized:
        return "X-ray diffraction"
    if "infrared spectroscopy" in normalized or "ftir" in normalized:
        return "Infrared spectroscopy"
    return "Unknown"


def extracted_document_metadata(
    reader: PdfReader,
    page_texts: list[str],
    path: Path,
) -> dict:
    metadata = getattr(reader, "metadata", None)
    title = clean_pdf_metadata(getattr(metadata, "title", None))
    authors = clean_pdf_metadata(getattr(metadata, "author", None))
    combined_text = "\n".join(page_texts)
    if title is None:
        title = first_title_line(page_texts[0] if page_texts else "")
    if title is None:
        title = path.stem
    doi_match = DOI_PATTERN.search(combined_text)
    doi = doi_match.group(0).rstrip(".,;)") if doi_match else None
    source_url = f"https://doi.org/{doi}" if doi else None
    materials = material_candidates(title, combined_text)
    material_system = materials[0] if materials else "Unknown"
    citation_parts = [part for part in (authors, title, doi) if part]
    return {
        "title": title,
        "authors": authors,
        "citation": ". ".join(citation_parts),
        "source_url": source_url,
        "technique": detected_technique(f"{title}\n{combined_text}"),
        "material_system": material_system,
        "material_candidates": materials,
        "metadata_method": "pdf_metadata_and_text_heuristics",
    }


def extract_pdf_evidence(path: Path) -> dict:
    try:
        reader = PdfReader(path, strict=False)
        if reader.is_encrypted:
            raise ReferenceError(
                422,
                "encrypted_reference_pdf",
                "Encrypted PDFs cannot be used as reference evidence.",
            )
        if len(reader.pages) > MAX_PDF_PAGES:
            raise ReferenceError(
                413,
                "reference_pdf_too_long",
                f"Reference PDFs may contain at most {MAX_PDF_PAGES} pages.",
            )
        evidence = []
        page_texts = []
        pages_with_text = 0
        extracted_characters = 0
        seen_positions = set()
        for page_number, page in enumerate(reader.pages, start=1):
            if len(evidence) >= MAX_EXTRACTED_PEAKS:
                break
            text = page.extract_text() or ""
            page_texts.append(text)
            extracted_characters += len(text)
            if extracted_characters > MAX_EXTRACTED_CHARACTERS:
                raise ReferenceError(
                    413,
                    "reference_text_too_large",
                    "The PDF contains too much extracted text to process safely.",
                )
            if text.strip():
                pages_with_text += 1
            for match in RAMAN_SHIFT_PATTERN.finditer(text):
                position = float(match.group(1))
                if not 50.0 <= position <= 5000.0:
                    continue
                key = round(position, 1)
                if key in seen_positions:
                    continue
                seen_positions.add(key)
                evidence.append(
                    {
                        "position_cm-1": position,
                        "page": page_number,
                        "excerpt": nearby_excerpt(
                            text,
                            match.start(),
                            match.end(),
                        ),
                    }
                )
                if len(evidence) >= MAX_EXTRACTED_PEAKS:
                    break
    except ReferenceError:
        raise
    except (OSError, PdfReadError, ValueError) as error:
        raise ReferenceError(
            422,
            "invalid_reference_pdf",
            "The uploaded PDF could not be read safely.",
        ) from error

    document = extracted_document_metadata(reader, page_texts, path)
    if document["technique"] != "Raman":
        status = "unsupported_technique"
    elif document["material_system"] == "Unknown":
        status = "material_system_unresolved"
    else:
        status = "ready" if len(evidence) >= 2 else "insufficient_peak_evidence"
    return {
        "page_count": len(reader.pages),
        "pages_with_text": pages_with_text,
        "peaks": sorted(evidence, key=lambda item: item["position_cm-1"]),
        "document": document,
        "status": status,
    }


def extract_spectrum_evidence(path: Path) -> dict:
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ReferenceError(
            415,
            "unsupported_reference_encoding",
            "Reference spectra must be UTF-8 text.",
        ) from error
    try:
        series = preview.extract_numeric_series(text)
        points, _input_order = processing.normalize_x_order(series["points"])
        x_values = [point[0] for point in points]
        raw_values = [point[1] for point in points]
        rayleigh_corrected, _rayleigh_artifacts = (
            processing.detect_and_correct_rayleigh_line(
                x_values,
                raw_values,
                processing.MODEL_PARAMETERS,
            )
        )
        artifact_corrected, _cosmic_artifacts = (
            processing.detect_and_correct_cosmic_spikes(
                x_values,
                rayleigh_corrected,
                processing.MODEL_PARAMETERS,
            )
        )
        baseline = processing.estimate_baseline(artifact_corrected)
        corrected = [
            intensity - baseline_value
            for intensity, baseline_value in zip(artifact_corrected, baseline)
        ]
        peaks = processing.detect_peaks(x_values, corrected)
    except (preview.PreviewError, processing.ProcessingError) as error:
        raise ReferenceError(
            422,
            "invalid_reference_spectrum",
            str(error),
        ) from error
    return {
        "point_count": len(points),
        "peaks": peaks,
        "document": {
            "title": series["headers"].get("TITLE", path.stem),
            "authors": None,
            "citation": series["headers"].get("CITATION"),
            "source_url": series["headers"].get("SOURCE_URL"),
            "technique": "Raman",
            "material_system": series["headers"].get("MATERIAL", "Unknown"),
            "material_candidates": [],
            "metadata_method": "spectrum_headers",
        },
        "status": (
            "ready"
            if len(peaks) >= 2 and series["headers"].get("MATERIAL")
            else "material_system_unresolved"
        ),
    }


def extract_reference_evidence(path: Path, kind: str) -> tuple[str, str, dict]:
    evidence = (
        extract_pdf_evidence(path)
        if kind == "pdf"
        else extract_spectrum_evidence(path)
    )
    return (
        evidence["status"],
        json.dumps(
            evidence,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        evidence["document"],
    )
