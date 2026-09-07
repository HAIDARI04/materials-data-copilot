import hashlib
import html
import json
import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

ROD_BASE_URL = "https://solsa.crystallography.net/rod"
ROD_LICENSE = "CC0-1.0"
ROD_MAX_ENTRIES = 1200
ROD_SYNC_BATCH_SIZE = 20
ROD_DOWNLOAD_WORKERS = 4
HTTP_TIMEOUT_SECONDS = 8.0
ROD_ALLOWED_HOST = "solsa.crystallography.net"

CURATED_FINGERPRINTS = (
    {
        "reference_id": "literature-carbon-nanotube-popov-2006",
        "material_system": "Carbon nanotube",
        "title": "Radial breathing and G-band modes of carbon nanotubes",
        "citation": (
            "Popov, V. N.; Lambin, P. Phys. Rev. B 73, 085407 (2006). "
            "DOI: 10.1103/PhysRevB.73.085407"
        ),
        "source_url": "https://doi.org/10.1103/PhysRevB.73.085407",
        "peaks": [
            {"position_range_cm-1": [100.0, 350.0], "assignment": "RBM"},
            {"position_range_cm-1": [1560.0, 1610.0], "assignment": "G"},
        ],
        "match_tolerance_cm-1": 12.0,
    },
    {
        "reference_id": "literature-vo2-m1-shvets-2019",
        "material_system": "VO2 (M1)",
        "title": "Raman fingerprint of monoclinic vanadium dioxide",
        "citation": (
            "Shvets, P. et al. Journal of Raman Spectroscopy 50 (2019). "
            "DOI: 10.1002/jrs.5616"
        ),
        "source_url": "https://doi.org/10.1002/jrs.5616",
        "peaks": [
            {"position_cm-1": value}
            for value in (143, 193, 224, 261, 310, 339, 391, 443, 499, 614, 824)
        ],
        "match_tolerance_cm-1": 14.0,
    },
    {
        "reference_id": "literature-graphene-silicon-2018",
        "material_system": "Graphene on crystalline silicon substrate",
        "title": "Graphene G and 2D bands with silicon substrate peak",
        "citation": (
            "Lee, J. et al. Scientific Reports 8, 2018. "
            "DOI: 10.1038/s41598-017-19084-1"
        ),
        "source_url": "https://doi.org/10.1038/s41598-017-19084-1",
        "peaks": [
            {
                "position_range_cm-1": [510.0, 530.0],
                "assignment": "Si substrate",
                "material_system": "Crystalline silicon",
                "role": "substrate",
            },
            {"position_range_cm-1": [1560.0, 1610.0], "assignment": "G"},
            {"position_range_cm-1": [2650.0, 2730.0], "assignment": "2D"},
        ],
        "match_tolerance_cm-1": 12.0,
    },
    {
        "reference_id": "literature-crystalline-silicon-520",
        "material_system": "Crystalline silicon",
        "title": "Crystalline silicon first- and second-order Raman bands",
        "citation": (
            "Parker, J. H. Jr. et al. Raman Scattering by Silicon and "
            "Germanium, Physical Review 155, 712 (1967)."
        ),
        "source_url": "https://doi.org/10.1103/PhysRev.155.712",
        "peaks": [
            {
                "position_range_cm-1": [280.0, 320.0],
                "assignment": "Si acoustic multiphonon band",
            },
            {
                "position_range_cm-1": [510.0, 530.0],
                "assignment": "Si first-order optical phonon",
            },
            {
                "position_range_cm-1": [900.0, 1000.0],
                "assignment": "Si second-order 2TO envelope",
            },
        ],
        "match_tolerance_cm-1": 10.0,
    },
    {
        "reference_id": "literature-mos2-phonons-2022",
        "material_system": "MoS2",
        "title": "First-order and defect-activated Raman modes of MoS2",
        "citation": (
            "Mignuzzi, S. et al. Effect of disorder on Raman scattering of "
            "single-layer MoS2, Phys. Rev. B 91, 195411 (2015), "
            "DOI: 10.1103/PhysRevB.91.195411; Küttinger, M. et al. RSC Adv. "
            "11, 5218-5229 (2021), DOI: 10.1039/D0RA10721B."
        ),
        "source_url": (
            "https://journals.aps.org/prb/abstract/"
            "10.1103/PhysRevB.91.195411"
        ),
        "peaks": [
            {
                "position_range_cm-1": [223.0, 233.0],
                "assignment": "LA(M) defect-activated phonon",
            },
            {
                "position_range_cm-1": [246.0, 262.0],
                "assignment": (
                    "Unidentified disorder-related mode near 250-258 cm-1"
                ),
                "provisional": True,
                "possible_origins": [
                    "MoS2 disorder-induced mode with unresolved branch assignment",
                    "Polybromide-related vibration in bromide-containing systems",
                ],
            },
            {
                "position_range_cm-1": [347.0, 361.0],
                "assignment": "TO(M) defect-activated phonon",
            },
            {
                "position_range_cm-1": [369.0, 382.0],
                "assignment": "LO(M) defect-activated phonon",
            },
            {
                "position_range_cm-1": [380.0, 389.0],
                "assignment": "E2g1 in-plane mode",
            },
            {
                "position_range_cm-1": [400.0, 411.0],
                "assignment": "A1g out-of-plane mode",
            },
            {
                "position_range_cm-1": [445.0, 460.0],
                "assignment": "2LA(M) second-order mode",
            },
        ],
        "match_tolerance_cm-1": 8.0,
    },
    {
        "reference_id": "literature-cs2znbr4-raman-2026",
        "material_system": "Cs2ZnBr4",
        "title": "ZnBr4 tetrahedral stretching modes relevant to Cs2ZnBr4",
        "citation": (
            "Synthesis and characterization of lead-free cesium halide "
            "Cs2ZnBr4 nanomaterial, University of the Witwatersrand (2026); "
            "Srivastava, J. P.; Kulshreshtha, A.; Rauh, H. J. Opt. Soc. "
            "Am. B 8, 2379-2383 (1991), DOI: 10.1364/JOSAB.8.002379."
        ),
        "source_url": (
            "https://wiredspace.wits.ac.za/bitstreams/"
            "4094a020-bed7-42ac-90b9-12f63140d65c/download"
        ),
        "peaks": [
            {
                "position_range_cm-1": [172.0, 184.0],
                "assignment": (
                    "ν1(A1) totally symmetric Zn-Br breathing stretch "
                    "of [ZnBr4]2−"
                ),
            },
            {
                "position_range_cm-1": [204.0, 225.0],
                "assignment": (
                    "ν3 antisymmetric Zn-Br stretching mode of [ZnBr4]2−"
                ),
            },
            {
                "position_range_cm-1": [342.0, 356.0],
                "assignment": "Possible 2ν1 Zn-Br overtone",
                "provisional": True,
                "excluded_if_material_components": ["MoS2"],
                "possible_origins": [
                    (
                        "Second-order Zn-Br overtone only when the band occurs "
                        "in Cs2ZnBr4-only regions and tracks the 175-178 cm-1 "
                        "ν1 band"
                    ),
                    (
                        "MoS2 TO(M) disorder mode is preferred when MoS2 or its "
                        "227 and 373 cm-1 disorder bands are present"
                    ),
                ],
            },
        ],
        "match_tolerance_cm-1": 8.0,
    },
    {
        "reference_id": "literature-sio2-raman-bands",
        "material_system": "SiO2",
        "title": "Raman bands of vitreous silica",
        "citation": (
            "Tallant, D. R. et al. Raman Spectra of Rings in Silicate "
            "Materials, MRS Proceedings."
        ),
        "source_url": (
            "https://www.cambridge.org/core/journals/"
            "mrs-online-proceedings-library-archive/article/"
            "raman-spectra-of-rings-in-silicate-materials/"
            "0CCC3F41646F87FD4928132F143F780C"
        ),
        "peaks": [
            {
                "position_range_cm-1": [420.0, 470.0],
                "assignment": "Si-O-Si network bending band",
            },
            {
                "position_range_cm-1": [480.0, 502.0],
                "assignment": "D1 four-membered siloxane rings",
            },
            {
                "position_range_cm-1": [595.0, 615.0],
                "assignment": "D2 three-membered siloxane rings",
            },
            {
                "position_range_cm-1": [750.0, 860.0],
                "assignment": "SiO2 network band near 800 cm-1",
            },
            {
                "position_range_cm-1": [1000.0, 1150.0],
                "assignment": "SiO2 stretching envelope near 1070 cm-1",
            },
        ],
        "match_tolerance_cm-1": 8.0,
    },
)


class OnlineReferenceError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def validate_rod_response_url(response: httpx.Response) -> None:
    parsed = urlsplit(str(response.url))
    if (
        parsed.scheme != "https"
        or parsed.hostname != ROD_ALLOWED_HOST
        or not parsed.path.startswith("/rod/")
    ):
        raise OnlineReferenceError(
            502,
            "rod_redirect_rejected",
            "The Raman Open Database redirected outside its allowlisted host.",
        )


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def curated_references() -> list[dict]:
    references = []
    for fingerprint in CURATED_FINGERPRINTS:
        evidence = {
            "provider": "curated_primary_literature",
            "license": "citation-required",
            "match_tolerance_cm-1": fingerprint["match_tolerance_cm-1"],
            "peaks": fingerprint["peaks"],
            "status": "ready",
        }
        source_identity = canonical_json(fingerprint).encode("utf-8")
        references.append(
            {
                "reference_id": fingerprint["reference_id"],
                "original_filename": None,
                "content_type": "application/vnd.materials.reference+json",
                "size_bytes": len(source_identity),
                "sha256": hashlib.sha256(source_identity).hexdigest(),
                "storage_path": None,
                "imported_at": "curated",
                "source_kind": "online_fingerprint",
                "technique": "Raman",
                "material_system": fingerprint["material_system"],
                "title": fingerprint["title"],
                "citation": fingerprint["citation"],
                "source_url": fingerprint["source_url"],
                "extraction_status": "ready",
                "evidence_json": canonical_json(evidence),
            }
        )
    return references


def strip_html(value: str) -> str:
    without_tags = re.sub(r"<[^>]+>", "", value)
    return " ".join(html.unescape(without_tags).split())


def parse_rod_catalog(html_text: str, limit: int) -> list[dict]:
    entries = []
    for row in re.findall(r"<tr>(.*?)</tr>", html_text, flags=re.DOTALL):
        id_match = re.search(r'href=["\'](\d{7})\.html["\']', row)
        if id_match is None:
            continue
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.DOTALL)
        if len(cells) < 7:
            continue
        bibliography = [
            strip_html(part)
            for part in re.split(r"<br\s*/?>", cells[6])
            if strip_html(part)
        ]
        entries.append(
            {
                "rod_id": id_match.group(1),
                "formula": strip_html(cells[2]).replace(" ", ""),
                "authors": bibliography[0] if bibliography else None,
                "title": bibliography[1] if len(bibliography) > 1 else None,
                "citation": ". ".join(bibliography),
            }
        )
        if len(entries) >= limit:
            break
    return entries


def parse_jcamp_points(raw_bytes: bytes) -> list[tuple[float, float]]:
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw_bytes.decode("latin-1")
    marker = re.search(r"##(?:XYPOINTS|PEAK TABLE)\s*=.*?(?:\r?\n)", text, re.I)
    if marker is None:
        raise ValueError("JCAMP file has no supported XY point table")
    data_text = text[marker.end() :]
    next_header = re.search(r"(?:^|\r?\n)##", data_text)
    if next_header is not None:
        data_text = data_text[: next_header.start()]
    points = [
        (float(match.group(1)), float(match.group(2)))
        for match in re.finditer(
            r"(-?\d+(?:\.\d+)?(?:[Ee][+-]?\d+)?)\s*,\s*"
            r"(-?\d+(?:\.\d+)?(?:[Ee][+-]?\d+)?)",
            data_text,
        )
    ]
    if len(points) < 2:
        raise ValueError("JCAMP file has too few explicit XY points")
    return points


def fetch_rod_spectrum(entry: dict) -> tuple[dict, bytes]:
    url = f"{ROD_BASE_URL}/{entry['rod_id']}.jdx"
    response = httpx.get(
        url,
        timeout=HTTP_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers={"User-Agent": "Materials-Data-Copilot/0.2"},
    )
    response.raise_for_status()
    validate_rod_response_url(response)
    parse_jcamp_points(response.content)
    return entry, response.content


def rod_search_catalog(limit: int, page: int = 0) -> list[dict]:
    with httpx.Client(
        timeout=HTTP_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers={"User-Agent": "Materials-Data-Copilot/0.2"},
    ) as client:
        initial = client.post(
            f"{ROD_BASE_URL}/result.php",
            data={"text1": "a", "submit": "Send"},
        )
        initial.raise_for_status()
        validate_rod_response_url(initial)
        session_match = re.search(r"CODSESSION=([a-z0-9]+)", initial.text)
        if session_match is None:
            raise OnlineReferenceError(
                502,
                "rod_search_changed",
                "The Raman Open Database search response could not be parsed.",
            )
        response = client.get(
            f"{ROD_BASE_URL}/result.php",
            params={
                "CODSESSION": session_match.group(1),
                "count": min(limit, ROD_MAX_ENTRIES),
                "page": max(0, page),
                "order_by": "file",
                "order": "asc",
            },
        )
        response.raise_for_status()
        validate_rod_response_url(response)
    entries = parse_rod_catalog(response.text, limit)
    if not entries:
        raise OnlineReferenceError(
            502,
            "rod_catalog_empty",
            "The Raman Open Database returned no parseable catalog entries.",
        )
    return entries


def build_rod_reference(entry: dict, raw_bytes: bytes, stored_path: Path) -> dict:
    import processing

    points, _input_order = processing.normalize_x_order(parse_jcamp_points(raw_bytes))
    x_values = [point[0] for point in points]
    raw_values = [point[1] for point in points]
    baseline = processing.estimate_baseline(raw_values)
    corrected = [value - base for value, base in zip(raw_values, baseline)]
    peaks = [
        peak
        for peak in processing.detect_peaks(x_values, corrected)
        if peak["position_cm-1"] >= 50.0
    ]
    if not peaks:
        raise ValueError("No usable Raman peaks were detected")
    raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    evidence = {
        "provider": "Raman Open Database",
        "license": ROD_LICENSE,
        "match_tolerance_cm-1": 14.0,
        "point_count": len(points),
        "peaks": peaks,
        "status": "ready",
    }
    title = entry["title"] or f"ROD entry {entry['rod_id']}"
    return {
        "reference_id": f"rod-{entry['rod_id']}",
        "original_filename": f"{entry['rod_id']}.jdx",
        "content_type": "chemical/x-jcamp-dx",
        "size_bytes": len(raw_bytes),
        "sha256": raw_sha256,
        "storage_path": str(stored_path),
        "imported_at": datetime.now(timezone.utc).isoformat(),
        "source_kind": "online_spectrum",
        "technique": "Raman",
        "material_system": entry["formula"] or f"ROD {entry['rod_id']}",
        "title": title,
        "citation": entry["citation"],
        "source_url": f"{ROD_BASE_URL}/{entry['rod_id']}.html",
        "extraction_status": "ready",
        "evidence_json": canonical_json(evidence),
    }


def sync_rod_catalog(
    root_directory: Path,
    limit: int = ROD_SYNC_BATCH_SIZE,
) -> dict:
    limit = max(1, min(int(limit), ROD_SYNC_BATCH_SIZE))
    online_root = (root_directory / "online" / "rod").resolve()
    existing_references = load_synced_rod_references(root_directory)
    existing_status = online_reference_status(root_directory)
    if existing_status.get("catalog_complete"):
        return existing_status
    catalog_page = int(existing_status.get("next_catalog_page", 0))
    staging_root = online_root / ".staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    snapshot_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    staging_directory = staging_root / f"{snapshot_id}-{uuid4()}"
    staging_directory.mkdir()
    spectra_directory = staging_directory / "spectra"
    spectra_directory.mkdir()

    try:
        entries = rod_search_catalog(limit, catalog_page)
        downloaded = []
        with ThreadPoolExecutor(max_workers=ROD_DOWNLOAD_WORKERS) as executor:
            futures = {
                executor.submit(fetch_rod_spectrum, entry): entry
                for entry in entries
            }
            for future in as_completed(futures):
                try:
                    entry, raw_bytes = future.result()
                    path = spectra_directory / f"{entry['rod_id']}.jdx"
                    with path.open("xb") as output:
                        output.write(raw_bytes)
                    reference = build_rod_reference(entry, raw_bytes, path)
                    downloaded.append(reference)
                except (httpx.HTTPError, OSError, ValueError):
                    continue
        if not downloaded:
            raise OnlineReferenceError(
                502,
                "rod_download_empty",
                "No usable Raman spectra could be downloaded from ROD.",
            )
        merged = {
            reference["reference_id"]: reference
            for reference in existing_references
        }
        merged.update(
            {reference["reference_id"]: reference for reference in downloaded}
        )
        downloaded = sorted(merged.values(), key=lambda item: item["reference_id"])
        catalog_bytes = canonical_json(downloaded).encode("utf-8")
        catalog_path = staging_directory / "catalog.json"
        with catalog_path.open("xb") as output:
            output.write(catalog_bytes)
        snapshot_sha256 = hashlib.sha256(catalog_bytes).hexdigest()

        final_directory = online_root / snapshot_id
        staging_directory.rename(final_directory)
        # Stored paths must follow the snapshot after its atomic rename.
        catalog = json.loads((final_directory / "catalog.json").read_text("utf-8"))
        for reference in catalog:
            reference["storage_path"] = str(
                final_directory / "spectra" / reference["original_filename"]
            )
        catalog_bytes = canonical_json(catalog).encode("utf-8")
        (final_directory / "catalog.json").write_bytes(catalog_bytes)
        snapshot_sha256 = hashlib.sha256(catalog_bytes).hexdigest()

        pointer = {
            "provider": "Raman Open Database",
            "license": ROD_LICENSE,
            "snapshot_id": snapshot_id,
            "catalog_path": str(final_directory / "catalog.json"),
            "catalog_sha256": snapshot_sha256,
            "reference_count": len(catalog),
            "batch_downloaded_count": len(catalog) - len(existing_references),
            "next_catalog_page": catalog_page + 1,
            "catalog_complete": len(entries) < limit,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
        }
        pointer_path = online_root / "current.json"
        temporary_pointer = online_root / f".{snapshot_id}.current"
        temporary_pointer.write_text(canonical_json(pointer), encoding="utf-8")
        os.replace(temporary_pointer, pointer_path)
        return pointer
    except OnlineReferenceError:
        if staging_directory.exists():
            shutil.rmtree(staging_directory)
        raise
    except (httpx.HTTPError, OSError, ValueError, json.JSONDecodeError) as error:
        if staging_directory.exists():
            shutil.rmtree(staging_directory)
        raise OnlineReferenceError(
            502,
            "rod_sync_failed",
            "The Raman Open Database synchronization failed safely.",
        ) from error


def load_synced_rod_references(root_directory: Path) -> list[dict]:
    pointer_path = (root_directory / "online" / "rod" / "current.json").resolve()
    if not pointer_path.is_file():
        return []
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        catalog_path = Path(pointer["catalog_path"]).resolve()
        online_root = (root_directory / "online" / "rod").resolve()
        catalog_path.relative_to(online_root)
        catalog_bytes = catalog_path.read_bytes()
        if hashlib.sha256(catalog_bytes).hexdigest() != pointer["catalog_sha256"]:
            return []
        return json.loads(catalog_bytes.decode("utf-8"))
    except (KeyError, OSError, ValueError, json.JSONDecodeError):
        return []


def online_reference_status(root_directory: Path) -> dict:
    pointer_path = root_directory / "online" / "rod" / "current.json"
    if not pointer_path.is_file():
        return {
            "provider": "Raman Open Database",
            "synced": False,
            "reference_count": 0,
            "curated_reference_count": len(CURATED_FINGERPRINTS),
        }
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "provider": "Raman Open Database",
            "synced": False,
            "reference_count": 0,
            "curated_reference_count": len(CURATED_FINGERPRINTS),
        }
    return {
        **pointer,
        "synced": True,
        "curated_reference_count": len(CURATED_FINGERPRINTS),
    }
