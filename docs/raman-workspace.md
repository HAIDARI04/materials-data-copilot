# Raman analysis workspace

`/app/analyze/raman` uses the Transport workspace's two-column outline:

- Analysis choices: Raman imports, search/grouping and batch selection.
- Raman Plot: selected measurement and optical image, followed by Raman processing
  (recipes, baseline, deconvolution, substrate correction, normalization, advanced
  settings, planned protocol and saved analyses), then spectral series, pan/zoom, fit inset and
  component visibility, peak review, quality checks, executed protocol and exports.

The workspace embeds `/upload?workspace=raman` in a same-origin, automatically
resized frame. This intentionally runs the existing Raman studio, rather than a
second plot implementation. Controls are moved as DOM nodes before the studio
binds its listeners. Scientific processing, review/learning consent, history,
exports and per-dataset plot preferences remain shared with `/upload`.

The workspace adapter filters to Raman spectra and honors `?dataset=<file_id>`.
The normal upload page keeps its full import list and previous-dataset behavior.
Scoped CSS hides the duplicate navigation and import form only in Raman mode.
Frame messages require the same origin and the active studio window. Expanded
plot mode covers the parent viewport; Escape uses the existing studio handler.
The two-column breakpoint uses the parent browser viewport, matching Transport;
the embedded frame's narrower width does not force a desktop page to stack.

Validation: run `node --test backend/tests/test_raman_workspace.cjs
backend/tests/test_plot_preferences.cjs` and the workspace/upload route tests in
`backend/tests/test_main.py`. Live visual verification requires a connected browser.
