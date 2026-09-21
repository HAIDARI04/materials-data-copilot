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

Open `http://127.0.0.1:8000/app` for the project, sample, dataset, and
technique workspaces. Use the global **Import data** action to add experimental
files with traceable metadata. Enter the known material system or leave the
default `Unknown` to request reference-based identification. The API
documentation is available at `http://127.0.0.1:8000/docs`.

Run the complete backend suite with:

```powershell
backend\.venv\Scripts\python.exe -m pytest -vv
```

## Use MDC on multiple PCs

Use GitHub for source code and Google Drive for measurement payloads. Keep the
active SQLite catalog and Python virtual environment local to each PC; SQLite
must not be opened from a synchronized folder.

1. Install Google Drive for desktop and make the `MDC-Data` folder available
   offline.
2. Copy `.env.example` to `.env` on each PC.
3. Set `MDC_SHARED_DATA_DIR` to that PC's mounted Google Drive folder, for
   example `G:/My Drive/MDC-Data`.
4. Leave `MDC_LOCAL_DATA_DIR=backend/data` so the active database remains local.
5. Close MDC and wait for Google Drive to finish syncing before switching PCs.

With this configuration, `raw`, `processed`, and `references` are stored under
the shared folder. The catalog remains at
`backend/data/materials_data_copilot.db`. Create a verified catalog backup in
the shared folder before moving to another PC:

```powershell
backend\.venv\Scripts\python.exe backend\backup.py "G:\My Drive\MDC-Data\backups"
```

Only one PC should run MDC at a time. The `.env` file, local database, raw data,
processed outputs, references, and virtual environment are excluded from Git.

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

## Transport workbook analysis

Excel workbooks labeled **Transport properties** or **Electrical transport**
can be analyzed from the Transport workspace. The local parser reads stored
worksheet values without recalculating or modifying the source workbook,
classifies time traces and voltage sweeps, reports channel statistics and
current drift, and fits ordinary least-squares I-V conductance, resistance, and
R-squared. Derived JSON results are checksum linked to the immutable source and
appear in the shared processing history. If an XLSX archive is truncated, only
complete recoverable worksheet streams are used and the result is prominently
marked partial.

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

## Transport plots and guided photodetector reports

Open `/app/analyze/transport` to plot saved Transport measurements. Select multiple
files to overlay compatible axes; select x and y variables independently. I-t
files use time on the x axis. Plot controls include per-series colors and line
styles, wheel zoom, left-drag pan and an optional inset (hidden initially).
Selections and plot preferences persist in this browser across refreshes.
Logarithmic ticks show current magnitudes, with zero values excluded.

Choose **Start photodetector recipe** to compare dark and illuminated I-V
workbooks. Matching dark/light filenames also produce suggestions on the upload
page. Confirm the same device, channel units and sweep branches, then enter the
known area, illumination and optional noise or diode-model inputs. Unknown
inputs remain blank; the result explains which metrics cannot yet be calculated.
Optical presets are applied only when explicitly selected.

Saved reports contain responsivity, on/off ratio, zero-bias current and, when
supported, detectivity, monochromatic EQE, ideality factor and apparent barrier
height. Tabs show vector plots with zoom/pan and a bias-point inspector. The
local evidence guide explains saved results; configured AI providers can receive
a derived summary only after the user enables sharing in the recipe dashboard.
The latest report and its chart views reopen after refresh.

**Download complete analysis** includes PDF and offline HTML reports, every
original source, SVG/PDF/300-dpi PNG figures, CSV and Excel tables, worked
calculations, method references and a SHA-256 manifest. **Preview report**,
**Report only (PDF)** and individual figure/table downloads are also available.
The ZIP retains the implementation, exact scientific dependencies, a runnable
`reproduce.py` and an explanatory notebook. Replay recalculates from originals
and verifies the saved values. Both source files are checksum-verified before
analysis and export. Originals remain unchanged. Shot-noise detectivity is
explicitly an estimate, and diode fits require a user-selected model and a
sufficiently linear forward window.

Transport fits retain source rows, exclusions, residuals and fit-only uncertainty.
Cross-channel slopes cannot become resistance. Optional rectangular-bar bulk
resistivity requires confirmed units, measured four-terminal voltage and geometry.
See [Transport export documentation](docs/transport-exports.md) for the package
contents, scientific conventions, API and reproduction instructions.

Run the plotting and recipe interface checks with
`node --test backend/tests/test_transport_plot.cjs backend/tests/test_plot_preferences.cjs backend/tests/test_photodetector_ui.cjs`.

## Deferred features

The standalone browser chat and external-provider portal are currently disabled. Existing
chat and training records and their compatibility APIs are retained; disabling
the interface does not delete provenance or experimental data.

## Development status

Phase 1: Pilot-ready local import, metadata catalog, and backup tooling.
