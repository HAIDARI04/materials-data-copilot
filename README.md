# Materials Data Copilot

A local-first application for organizing, processing, analyzing, and documenting materials-science experimental data.

## Initial objectives

- Preserve imported raw data without modification
- Generate and display SHA-256 checksums
- Maintain complete processing provenance
- Perform reproducible Raman analysis
- Identify material systems from traceable reference evidence
- Export processed data and reviewer-auditable reports
- Support synchronized project storage across multiple computers

## Run locally

From the repository root on Windows:

```powershell
backend\.venv\Scripts\python.exe -m uvicorn main:app --app-dir backend --reload
```

Open `http://127.0.0.1:8000/upload` to import a file with experimental
metadata. Enter the known material system or leave the default `Unknown` to
request reference-based identification. The API documentation is available at
`http://127.0.0.1:8000/docs`.

Run the complete backend suite with:

```powershell
backend\.venv\Scripts\python.exe -m pytest -vv
```

## Local backups

Stop the API before taking a pilot-data backup so the SQLite snapshot and raw
files represent the same point in time. Choose a destination outside
`backend/data`:

```powershell
backend\.venv\Scripts\python.exe backend\backup.py D:\Materials-Backups
```

The command creates a timestamped directory containing a safe SQLite backup,
exact copies of imported raw files and derived processing results, and
`manifest.json` with sizes and SHA-256 checksums. Before publishing the backup,
the command verifies SQLite integrity, foreign keys, structured JSON,
source-to-processing provenance, every cataloged raw/reference/attachment/
derived checksum, and the copied artifacts. Failed attempts do not publish a
partial backup. Copy the backup to a second
physical device or approved synchronized storage and periodically test
restoration.

`GET /health` performs the same catalog and provenance checks and reports
record counts, storage availability, and the application version. Use
`GET /files/{file_id}/integrity` for a targeted raw-byte check and
`GET /files/{file_id}/content` for checksum-verified retrieval with a trusted
extension-derived media type and sandboxed inline image handling.

## Reproducible exports

For a processed measurement, choose **Reproducibility ZIP (recommended)** from
the plot export menu. The package includes the checksum-verified raw file,
portable metadata and edit history, the exact derived result, all plotted and
fitted series as CSV, peak assignments, confirmation/training evidence, pinned
requirements, and a SHA-256 manifest. PNG, analysis JSON, and all-series CSV
remain available for lightweight sharing. An export never changes stored raw or
derived data.

## Reusable recipes and batch review

The analysis toolbar provides conservative review, peak-fitting,
substrate-corrected fitting, and raw-inspection recipes. Each recipe maps to
explicit baseline, deconvolution, substrate-reference, ensemble-threshold,
quality, and review parameters; smoothing remains disabled. The planned protocol
can be reviewed before processing, and the executed protocol is stored afterward.
Save the current settings locally as **My saved recipe** when a
repeatable custom workflow is needed. The recipe is persisted in SQLite, with
a browser-local fallback when the API is temporarily unavailable. In
**Imports**, select up to 50
datasets and apply the current recipe as a batch. The review table reports each
file independently, retains failures for review, and exports a provenance-bearing
summary CSV without changing raw measurements. The same selection can be archived
as a batch; archiving changes list visibility only and preserves raw bytes,
checksums, processing runs, and replayable provenance.

Each completed processing run is available through the analysis-history control
and `GET /files/{file_id}/processing-runs`. Historical results are checksum
verified before replay. SQLite prevents processing-run mutation and protects
the identity and checksum of processed imports; exceptional replacement of an
unusable legacy run creates an append-only tombstone.

Every new Raman result also stores and displays the mandatory executed protocol
and a spectrum-quality gate. Axis regularity, sampling, flat-topped detector
saturation, signal contrast, substrate fit quality, and deconvolution residuals
are reported separately so measured values remain inspectable when an automated
interpretation is withheld.

## Raman reference library

Raman preprocessing is material-agnostic. The application reports an unknown
material unless uploaded reference evidence produces a sufficiently distinct
match. Add a self-describing known spectrum or PDF in the **Reference library**
section. PDF title, authors, DOI URL, technique, material candidates, and Raman
shifts are extracted locally; unsupported or unresolved papers are retained but
not used for identification. Original bytes, extracted evidence, and SHA-256
are retained. Material-specific rules run only for declared, confirmed, or
reference-identified systems. MoS2 reports fitted E2g1/A1g separation with
sampling/fit uncertainty, FWHM, and signal-to-noise gates before a
literature-calibrated layer-count screening result. Graphene and related
graphitic carbon report D/G and 2D/G peak-height ratios plus fitted-area ratios
when deconvolution is available. These reports retain their literature sources,
prerequisites, and limitations; the application does not infer quantitative
graphene defect density without excitation and defect-regime evidence.

Imports can be explicitly classified as a sample, a sample on a substrate, or a
pure-substrate reference. Confirmed pure glass and SiO2/Si measurements form a measured
full-trace substrate library. Compatible references are aligned and fitted as a
target-adaptive nonnegative ensemble, while assigned material bands and
user-classified sample residuals are protected. Pure-substrate targets are
evaluated leave-one-out before their confirmed zero-residual metadata constraint
is applied. Residual decisions can be saved as reusable substrate or sample
evidence without modifying the original spectrum.

For online literature, download a legally reusable source from a curated
repository and upload it with its canonical URL. Useful starting points include
[RRUFF](https://rruff.info/), the
[NIST Materials Data Repository](https://materialsdata.nist.gov/), and the
[Raman Open Database](https://solsa.crystallography.net/rod/). The backend does
not fetch arbitrary URLs or silently retrain itself; this avoids untraceable
claims, licensing mistakes, and server-side request forgery.

The **Sync Raman Open Database** button downloads the allowlisted CC0 library
in bounded batches. Each batch is validated and promoted atomically, and later
processing records the exact library snapshot used. Repeat synchronization to
expand the local catalog without blocking on the entire public database.

## Deferred features

The browser chat and external-provider portal are currently disabled. Existing
chat and training records and their compatibility APIs are retained; disabling
the interface does not delete provenance or experimental data.

## Development status

Phase 1: Pilot-ready local import, metadata catalog, and backup tooling.
