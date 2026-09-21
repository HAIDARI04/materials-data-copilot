# Transport analysis exports

## Student workflow

1. Import a measurement and review its worksheet, channels, scales, polarity and wiring.
2. Select sweep directions and fit windows. Confirm measurement units and wiring when known.
3. Save the analysis. Changing the recipe creates a new run; previous runs remain available.
4. Preview the report, then choose **Download complete analysis**, **Report only (PDF)**,
   **Figures and tables**, or an individual figure/table.

Bulk resistivity is optional and requires a uniform rectangular bar, positive bounded
resistance, measured four-terminal voltage, confirmed wiring/units, voltage-probe spacing,
width and thickness. Unknown values stay unavailable. Hall analysis remains a separate,
unimplemented workflow and is not inferred from ordinary I-V measurements.

## Complete package

- `report.pdf`, `report.html`: step-by-step methods, worked substitutions, figures,
  uncertainty scope, missing inputs, interpretation and method references.
- `sources/`: original uploaded bytes, including the dark reference for photodetectors.
- `tables/`: original parsed worksheet values, converted observations, selection reasons,
  branch tables, fitted values, residuals, window statistics and extracted parameters.
- `analysis.xlsx`: a formatted, values-only snapshot with units and source identifiers.
- `figures/`: individual vector SVG/PDF and 300 dpi PNG figures for every selected branch,
  full measured channels, fit residuals and populated time windows.
- `result.json`: the exact saved application result.
- `calculation.json`: all scientific output fields, independently checked by the replay.
- `recipe.json`: settings, sources, exact scientific package versions and tolerances.
- `code/`, `reproduce.py`, `requirements.txt`: frozen implementation and replay entrypoint.
- `analysis.ipynb`: optional annotated notebook using the same replay entrypoint.
- `references.json`, `references.md`, `references.bib`: method-specific citations.
- `report-model.json`, `data-dictionary.json`: report content, units and conventions.
- `manifest.json`: SHA-256 and byte count of every other file in the package.

CSV retains floating-point precision. Potential spreadsheet formula strings receive an
apostrophe prefix; JSON and original uploads preserve the original values. Excel cells
are typed literal strings, never formulas originating from an uploaded file.

## Scientific conventions

Unweighted least squares fits a free intercept: `I = slope*x + intercept`. Fit statistics
include source-row selection, N, N-2 degrees of freedom, Sxx/Sxy, slope/intercept standard
errors, their covariance, residuals, RMSE and 95% Student-t intervals. The intervals
assume independent normal errors with constant variance and negligible voltage error.
They describe fit scatter; they are not total measurement uncertainty.

The reciprocal slope is exposed as resistance only for the current's corresponding
voltage channel and when its 95% interval excludes zero. Cross-channel response slopes
never become resistance. Drain-current versus gate-voltage slopes are identified as
transfer responses requiring confirmed drain-source control before interpreting as gm.

Optional rectangular-bar resistivity uses `rho = R*width*thickness/length`. Combining
fit, current calibration, voltage calibration and geometry standard uncertainties uses
first-order independent-input propagation. Supplied calibration terms must exclude
scatter already represented by the fit. Missing contributors keep combined uncertainty
unavailable. No expanded total-uncertainty interval is claimed.

Current integration uses the composite trapezoidal rule on adjacent observations with
strictly increasing time. Gaps and reversed/repeated times withhold the integral.
Population standard deviation describes only the observed window. No trapped-charge
interpretation is assigned automatically.

Photodetector exports retain both original sources, source rows, branch choices, unit
scales, repeated-voltage averaging, interpolation rules, optical inputs, constants and
noise assumptions. Unprovided optical-metric uncertainties remain explicitly unavailable.

## Reproduction and versions

The electrical model version is `2.0.0`; other research techniques retain their existing
model versions. Each electrical run retains the implementation source and its digest.
The source snapshot is captured with the loaded analyzers at application startup.
Restart/reload the app after editing analysis modules. Export never substitutes newer
code for the implementation stored with a run.

Run `python reproduce.py --verify-only` without third-party packages to verify hashes.
After installing `requirements.txt` into a separate virtual environment, run
`python reproduce.py` to recalculate from originals. Every integer, boolean, identifier,
string, sequence and field is compared. Floats use relative tolerance `1e-10` and
absolute tolerance `1e-30` in saved units. Mismatches exit nonzero with a field location.
Successful results are written under `reproduced/` without changing package inputs.

Dependency installation requires internet access or a populated package cache. Report
viewing and subsequent replay run offline. The notebook is optional; Jupyter is not a
dependency of the command-line replay. Numerical replay does not promise bit-identical
PDF rendering or pagination across operating systems.

Earlier runs without implementation snapshots remain readable. Creating a complete
package requires a new analysis; old results are never silently recomputed or overwritten.

## API

- `GET /research/analyses/{id}/export`: complete ZIP.
- `GET /research/analyses/{id}/export?format=report`: PDF report.
- `GET /research/analyses/{id}/export?format=figures`: figures/tables ZIP.
- `GET /research/analyses/{id}/export/files`: named asset catalog.
- `GET /research/analyses/{id}/export/file?name=report.html&inline=true`: preview.
- `GET /research/analyses/{id}/export/file?name=...`: an allowed catalog asset.

All routes verify saved results and every source checksum. Asset names come from the
catalog and never resolve arbitrary server filesystem paths. Download responses use
`no-store`; HTML reports have a restrictive content policy and escaped user text.

## Validation

Run the Python suite from the repository root. In restricted Windows environments,
use a workspace-local temporary directory:

```powershell
backend/.venv/Scripts/python.exe -m pytest -vv -p no:cacheprovider --basetemp=./.pytest-export-check
node --test backend/tests/test_transport_export_ui.cjs
```

Export tests compare fit statistics against SciPy, test physical prerequisites and
missing rows, reopen XLSX/PDF files, verify every manifest entry, extract and replay
both Transport and photodetector packages in a fresh subprocess, and reject altered
sources or mismatched numerical results. Import provenance is checked through a fresh
SQLite connection. Generated test fixtures never use production experimental data.
