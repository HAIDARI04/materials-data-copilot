const view = document.querySelector("#workspace-view");
const breadcrumb = document.querySelector("#breadcrumb");
const sidebar = document.querySelector("#sidebar");
const scrim = document.querySelector("#sidebar-scrim");
const menuButton = document.querySelector("#menu-button");
const globalSearch = document.querySelector("#global-search");
const globalSearchInput = document.querySelector("#global-search-input");
const serverIndicator = document.querySelector("#server-indicator");

const state = {
  files: [],
  samples: [],
  references: [],
  health: null,
  errors: [],
};

const techniqueDefinitions = {
  ...researchTechniqueDefinitions,
  raman: {
    name: "Raman spectroscopy",
    short: "Raman",
    symbol: "RAM",
    color: "#6f9419",
    description: "Inspect spectra, remove baseline and substrate signals, fit peaks, and review evidence-backed assignments.",
    capabilities: [
      ["Baseline correction", "Material-agnostic morphological baseline processing."],
      ["Substrate correction", "Measured-reference fitting with protected sample bands."],
      ["Peak fitting", "Conservative pseudo-Voigt deconvolution and quality gates."],
      ["Reference matching", "Traceable local and Raman Open Database evidence."],
    ],
    stages: [
      ["Inspect raw", "Axis, sampling and detector quality"],
      ["Preprocess", "Baseline and optional substrate correction"],
      ["Fit peaks", "Peak detection and deconvolution"],
      ["Interpret", "Assignments and material-specific metrics"],
      ["Save", "Versioned result with reproducible provenance"],
    ],
    ready: true,
  },
  transport: {
    name: "Transport properties",
    short: "Transport",
    symbol: "I-V",
    color: "#d47534",
    description: "Organize electrical measurements by device geometry, contact configuration, temperature, and magnetic field.",
    capabilities: [
      ["Resistance and resistivity", "Track units, geometry corrections, and uncertainty."],
      ["Temperature sweeps", "Compare metallic, semiconducting, and transition behavior."],
      ["Hall analysis", "Derive carrier type, density, and mobility from field sweeps."],
      ["Device curves", "Group I-V and transfer curves by device and measurement run."],
    ],
    stages: [
      ["Inspect raw", "Columns, units, sweep direction and compliance"],
      ["Configure", "Geometry, contacts and measurement conditions"],
      ["Transform", "Resistance, resistivity and conductivity"],
      ["Analyze", "Hall, mobility and transition metrics"],
      ["Save", "Results linked to device and raw data"],
    ],
    ready: true,
  },
  afm: {
    name: "Atomic force microscopy",
    short: "AFM",
    symbol: "AFM",
    color: "#3f8f84",
    description: "Review topography and auxiliary channels, then derive traceable surface and feature statistics.",
    capabilities: [
      ["Channel viewer", "Keep height, phase, amplitude, and metadata together."],
      ["Level and flatten", "Record plane removal, line correction, and masking choices."],
      ["Profiles", "Measure step heights and distances with saved line coordinates."],
      ["Surface statistics", "Calculate roughness, grain, and feature distributions."],
    ],
    stages: [
      ["Inspect raw", "Channels, scan size, pixels and units"],
      ["Prepare", "Level, flatten and mask"],
      ["Measure", "Profiles, regions and feature boundaries"],
      ["Quantify", "Roughness and feature statistics"],
      ["Save", "Derived maps and measurements with provenance"],
    ],
    ready: false,
  },
  xps: {
    name: "X-ray photoelectron spectroscopy",
    short: "XPS",
    symbol: "XPS",
    color: "#8465a5",
    description: "Move from survey spectra to constrained high-resolution fitting and auditable chemical-state assignments.",
    capabilities: [
      ["Survey review", "Identify elements while retaining acquisition context."],
      ["Region management", "Group high-resolution scans and replicate sweeps."],
      ["Peak fitting", "Track background, line shapes, constraints, and residuals."],
      ["Quantification", "Report atomic concentration and chemical-state evidence."],
    ],
    stages: [
      ["Inspect survey", "Energy axis, charge shift and detected elements"],
      ["Select regions", "High-resolution scans and backgrounds"],
      ["Fit", "Components, constraints and residual quality"],
      ["Quantify", "Atomic ratios and chemical states"],
      ["Save", "Fit model, evidence and derived tables"],
    ],
    ready: false,
  },
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function safeDecode(value) {
  try { return decodeURIComponent(value); } catch (_error) { return value; }
}

function formatDate(value, includeTime = false) {
  if (!value) return "Not recorded";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return escapeHtml(value);
  return new Intl.DateTimeFormat(undefined, includeTime
    ? { dateStyle: "medium", timeStyle: "short" }
    : { dateStyle: "medium" }).format(date);
}

function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / 1024 ** 2).toFixed(1)} MiB`;
}

function techniqueKey(value) {
  const normalized = String(value || "").toLowerCase();
  if (normalized === 'pl' || normalized.includes('photoluminescence')) return 'pl';
  if (normalized === 'xrd' || normalized.includes('diffraction')) return 'xrd';
  if (normalized === 'sem' || normalized.includes('scanning electron')) return 'sem';
  if (normalized === 'cvd' || normalized.includes('growth')) return 'cvd';
  if (normalized.includes("raman")) return "raman";
  if (normalized === "afm" || normalized.includes("atomic force")) return "afm";
  if (normalized.includes("xps") || normalized.includes("photoelectron")) return "xps";
  if (["transport", "resist", "hall", "electrical", "i-v", "iv curve"].some(term => normalized.includes(term))) return "transport";
  return "other";
}

function techniqueName(file) {
  const key = techniqueKey(file.technique);
  return key === "other" ? (file.technique || "Other") : techniqueDefinitions[key].short;
}

function techniqueChip(file) {
  const key = techniqueKey(file.technique);
  return `<span class="technique-chip ${key}">${escapeHtml(techniqueName(file))}</span>`;
}

function analysisHref(file) {
  const key = techniqueKey(file.technique);
  return key === "raman"
    ? `/upload?file_id=${encodeURIComponent(file.file_id)}#recent-panel`
    : `/app/analyze/${key}?dataset=${encodeURIComponent(file.file_id)}`;
}

function sampleFor(file) {
  return state.samples.find(sample => sample.sample_id === file.sample_id);
}

function projectNameFor(file) {
  return sampleFor(file)?.project || "Unassigned";
}

function noticeHtml() {
  if (!state.errors.length) return "";
  return `<div class="notice error">Some workspace data could not be loaded. Existing files and tools remain unchanged. ${escapeHtml(state.errors.join(" "))}</div>`;
}

function pageHeading(eyebrow, title, description, actions = "") {
  return `<div class="page-heading">
    <div><p class="eyebrow">${escapeHtml(eyebrow)}</p><h1>${escapeHtml(title)}</h1><p class="lede">${escapeHtml(description)}</p></div>
    ${actions ? `<div class="page-actions">${actions}</div>` : ""}
  </div>`;
}

function metric(label, value, note) {
  return `<article class="metric-card"><span class="metric-label">${escapeHtml(label)}<i class="metric-pip"></i></span><strong class="metric-value">${escapeHtml(value)}</strong><span class="metric-note">${escapeHtml(note)}</span></article>`;
}

function emptyState(title, message, action = "") {
  return `<div class="empty-state"><div><strong>${escapeHtml(title)}</strong><p>${escapeHtml(message)}</p>${action}</div></div>`;
}

function datasetRows(files, limit = null) {
  const rows = (limit ? files.slice(0, limit) : files).map(file => `
    <tr>
      <td class="primary-cell"><a class="text-link" href="/app/datasets/${encodeURIComponent(file.file_id)}">${escapeHtml(file.original_filename)}</a><small>${escapeHtml(file.file_id)}</small></td>
      <td>${techniqueChip(file)}</td>
      <td><a class="text-link" href="/app/samples/${encodeURIComponent(file.sample_id)}">${escapeHtml(file.sample_id || "Unassigned")}</a></td>
      <td>${escapeHtml(file.material_system || "Unknown")}</td>
      <td>${file.latest_processed_at ? '<span class="badge success">Analyzed</span>' : '<span class="badge neutral">Raw only</span>'}</td>
      <td>${formatDate(file.imported_at)}</td>
    </tr>`).join("");
  return `<div class="table-shell"><table><thead><tr><th>Dataset</th><th>Technique</th><th>Sample</th><th>Material</th><th>Status</th><th>Imported</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function techniqueCounts() {
  const counts = { raman: 0, transport: 0, afm: 0, xps: 0, pl: 0, xrd: 0, sem: 0, cvd: 0, other: 0 };
  state.files.forEach(file => { counts[techniqueKey(file.technique)] += 1; });
  return counts;
}

function renderDashboard() {
  const counts = techniqueCounts();
  const analyzed = state.files.filter(file => file.latest_processed_at).length;
  const projects = new Set(state.samples.map(sample => sample.project).filter(Boolean));
  const recent = [...state.files].sort((a, b) => String(b.imported_at).localeCompare(String(a.imported_at)));
  const needsReview = state.files.filter(file => techniqueKey(file.technique) === "raman" && !file.latest_processed_at);
  view.innerHTML = `${noticeHtml()}
    <section class="hero-panel">
      <div><p class="eyebrow">Local research workspace</p><h1>Every measurement, connected to its scientific context.</h1><p class="lede">Move from immutable raw data to technique-specific analysis, comparisons, and reviewer-ready exports.</p></div>
      <div class="hero-actions"><a class="button lime" href="/upload#import-panel">Import measurements</a><a class="button" href="/app/analyze">Choose an analysis</a></div>
    </section>
    <section class="metric-grid" aria-label="Workspace summary">
      ${metric("Datasets", state.files.length, `${analyzed} with saved analysis`)}
      ${metric("Samples", state.samples.length, `${state.files.filter(file => !file.sample_id).length} datasets need assignment`)}
      ${metric("Projects", projects.size, projects.size ? "Active sample groupings" : "Create through sample metadata")}
      ${metric("References", state.references.length, "Traceable local evidence")}
    </section>
    <section class="dashboard-grid">
      <div class="panel">
        <div class="panel-heading"><div><h2>Recent datasets</h2><p>Your latest imported measurements across every technique.</p></div><a class="text-link" href="/app/datasets">View all</a></div>
        ${recent.length ? datasetRows(recent, 6) : emptyState("No measurements yet", "Import a raw measurement to start the catalog.", '<a class="button primary" href="/upload#import-panel">Import data</a>')}
      </div>
      <div class="panel">
        <div class="panel-heading"><div><h2>Research flow</h2><p>A consistent path for every technique.</p></div></div>
        <div class="workflow-list">
          <div class="workflow-item"><span class="workflow-number">1</span><span><strong>Preserve</strong><small>Import immutable raw files</small></span><span class="workflow-state">${state.files.length}</span></div>
          <div class="workflow-item"><span class="workflow-number">2</span><span><strong>Contextualize</strong><small>Connect samples and projects</small></span><span class="workflow-state">${state.samples.length}</span></div>
          <div class="workflow-item"><span class="workflow-number">3</span><span><strong>Analyze</strong><small>Use technique-specific methods</small></span><span class="workflow-state">${analyzed}</span></div>
          <div class="workflow-item"><span class="workflow-number">4</span><span><strong>Review</strong><small>Resolve unprocessed Raman data</small></span><span class="workflow-state">${needsReview.length}</span></div>
          <div class="workflow-item"><span class="workflow-number">5</span><span><strong>Share</strong><small>Export data with provenance</small></span><span class="workflow-state">Ready</span></div>
        </div>
      </div>
    </section>
    <section class="section-block">
      <div class="section-title"><div><h2>Analyze by technique</h2><p>Each workspace uses the same provenance foundation.</p></div></div>
      ${techniqueCards(counts)}
    </section>`;
}

function techniqueCards(counts = techniqueCounts()) {
  return `<div class="card-grid tech-grid">${Object.entries(techniqueDefinitions).map(([key, item]) => `
    <a class="tech-card" style="--accent:${item.color}" href="/app/analyze/${key}">
      <span class="tech-accent"></span><h3>${escapeHtml(item.short)}</h3><p>${escapeHtml(item.description)}</p>
      <span class="card-footer"><span>${counts[key]} dataset${counts[key] === 1 ? "" : "s"}</span><span>Open workspace</span></span>
    </a>`).join("")}</div>`;
}

function allProjects() {
  const map = new Map();
  state.samples.forEach(sample => {
    const name = sample.project || "Unassigned";
    if (!map.has(name)) map.set(name, { name, samples: [], files: [] });
    map.get(name).samples.push(sample);
  });
  state.files.forEach(file => {
    const name = projectNameFor(file);
    if (!map.has(name)) map.set(name, { name, samples: [], files: [] });
    map.get(name).files.push(file);
  });
  return [...map.values()].sort((a, b) => (a.name === "Unassigned") - (b.name === "Unassigned") || a.name.localeCompare(b.name));
}

function renderProjects(projectName = null) {
  const projects = allProjects();
  if (projectName !== null) return renderProjectDetail(projectName, projects);
  view.innerHTML = `${noticeHtml()}${pageHeading("Research organization", "Projects", "Group samples and measurements by study, publication, device batch, or experimental campaign.", '<a class="button primary" href="/app/samples">Manage samples</a>')}
    ${projects.length ? `<div class="card-grid">${projects.map(project => {
      const techniques = [...new Set(project.files.map(file => techniqueName(file)))];
      return `<a class="project-card" href="/app/projects/${encodeURIComponent(project.name)}"><span class="badge ${project.name === "Unassigned" ? "warning" : "neutral"}">${project.name === "Unassigned" ? "Needs organization" : "Project"}</span><h3 style="margin-top:.75rem">${escapeHtml(project.name)}</h3><p>${project.samples.length} samples and ${project.files.length} datasets</p><span class="chip-row">${techniques.slice(0, 4).map(name => `<span class="badge neutral">${escapeHtml(name)}</span>`).join("") || '<span class="meta">No measurements</span>'}</span><span class="card-footer"><span>${techniques.length} techniques</span><span>Open project</span></span></a>`;
    }).join("")}</div>` : emptyState("No projects yet", "Add a project name to a sample to create the first project workspace.", '<a class="button primary" href="/app/samples">Open samples</a>')}`;
}

function renderProjectDetail(name, projects = allProjects()) {
  const project = projects.find(item => item.name === name);
  if (!project) return renderNotFound("Project not found", "The requested project is not present in the current sample catalog.");
  const techniques = [...new Set(project.files.map(file => techniqueName(file)))];
  view.innerHTML = `${noticeHtml()}${pageHeading("Project", project.name, `${project.samples.length} samples, ${project.files.length} datasets, and ${techniques.length} represented techniques.`, '<a class="button" href="/app/projects">All projects</a><a class="button primary" href="/upload#import-panel">Import data</a>')}
    <section class="metric-grid">
      ${metric("Samples", project.samples.length, "Project sample records")}
      ${metric("Datasets", project.files.length, "Immutable measurements")}
      ${metric("Analyzed", project.files.filter(file => file.latest_processed_at).length, "Saved processing results")}
      ${metric("Techniques", techniques.length, techniques.join(", ") || "None assigned")}
    </section>
    <section class="dashboard-grid">
      <div class="panel"><div class="panel-heading"><div><h2>Project measurements</h2><p>All datasets linked through project samples.</p></div></div>${project.files.length ? datasetRows(project.files) : emptyState("No project datasets", "Import a measurement using one of this project's sample IDs.")}</div>
      <div class="panel"><div class="panel-heading"><div><h2>Samples</h2><p>Materials and devices in this project.</p></div></div><div class="stack">${project.samples.map(sample => `<a class="sample-card" style="min-height:auto" href="/app/samples/${encodeURIComponent(sample.sample_id)}"><h3>${escapeHtml(sample.sample_id)}</h3><p>${escapeHtml(sample.material_system || "Unknown material")}</p><span class="card-footer"><span>${escapeHtml(sample.substrate || "Unknown substrate")}</span><span>Open</span></span></a>`).join("") || '<p class="muted">No explicit sample records.</p>'}</div></div>
    </section>`;
}

function renderSamples(sampleId = null) {
  if (sampleId !== null) return renderSampleDetail(sampleId);
  const cards = state.samples.map(sample => {
    const files = state.files.filter(file => file.sample_id === sample.sample_id);
    const techniques = [...new Set(files.map(file => techniqueName(file)))];
    return `<a class="sample-card" href="/app/samples/${encodeURIComponent(sample.sample_id)}"><span class="badge neutral">${escapeHtml(sample.project || "No project")}</span><h3 style="margin-top:.75rem">${escapeHtml(sample.sample_id)}</h3><p>${escapeHtml(sample.material_system || "Unknown material")} on ${escapeHtml(sample.substrate || "unknown substrate")}</p><span class="chip-row">${techniques.map(name => `<span class="badge neutral">${escapeHtml(name)}</span>`).join("") || '<span class="meta">No measurements</span>'}</span><span class="card-footer"><span>${files.length} datasets</span><span>Open sample</span></span></a>`;
  });
  const unregistered = [...new Set(state.files.map(file => file.sample_id).filter(id => id && !state.samples.some(sample => sample.sample_id === id)))];
  view.innerHTML = `${noticeHtml()}${pageHeading("Cross-technique context", "Samples", "Use a sample as the common thread between Raman, Transport, AFM, XPS, images, and fabrication notes.", '<a class="button primary" href="/upload#import-panel">Import measurement</a>')}
    ${unregistered.length ? `<div class="notice">${unregistered.length} sample ID${unregistered.length === 1 ? " is" : "s are"} present in imported data but do not yet have a detailed sample record.</div>` : ""}
    ${cards.length ? `<div class="card-grid">${cards.join("")}</div>` : emptyState("No sample records", "Samples are created automatically when measurements are imported, or through the sample API.", '<a class="button primary" href="/upload#import-panel">Import first measurement</a>')}`;
}

function renderSampleDetail(sampleId) {
  const sample = state.samples.find(item => item.sample_id === sampleId) || { sample_id: sampleId };
  const files = state.files.filter(file => file.sample_id === sampleId);
  if (!files.length && !state.samples.some(item => item.sample_id === sampleId)) return renderNotFound("Sample not found", "No sample or imported measurement uses this sample ID.");
  const counts = { raman: 0, transport: 0, afm: 0, xps: 0, other: 0 };
  files.forEach(file => { counts[techniqueKey(file.technique)] += 1; });
  view.innerHTML = `${noticeHtml()}${pageHeading("Sample", sampleId, `${sample.material_system || "Unknown material"}${sample.substrate ? ` on ${sample.substrate}` : ""}.`, '<a class="button" href="/app/samples">All samples</a><a class="button primary" href="/upload#import-panel">Add measurement</a>')}
    <section class="detail-grid">
      <div class="panel"><div class="panel-heading"><div><h2>Measurements</h2><p>Every technique attached to this sample.</p></div></div>${files.length ? datasetRows(files) : emptyState("No measurements", "Import data using this sample ID to connect it here.")}</div>
      <aside class="stack">
        <div class="panel"><div class="panel-heading"><div><h2>Sample context</h2></div></div><dl class="definition-list"><div><dt>Project</dt><dd>${escapeHtml(sample.project || "Unassigned")}</dd></div><div><dt>Material</dt><dd>${escapeHtml(sample.material_system || "Unknown")}</dd></div><div><dt>Substrate</dt><dd>${escapeHtml(sample.substrate || "Unknown")}</dd></div><div><dt>Updated</dt><dd>${formatDate(sample.updated_at)}</dd></div></dl>${sample.notes ? `<p class="provenance-note">${escapeHtml(sample.notes)}</p>` : ""}</div>
        <div class="panel"><div class="panel-heading"><div><h2>Technique coverage</h2><p>A cross-technique view of this sample.</p></div></div><div class="workflow-list">${Object.entries(techniqueDefinitions).map(([key, definition]) => `<div class="workflow-item"><span class="tech-dot ${key}"></span><span><strong>${definition.short}</strong><small>${counts[key]} linked datasets</small></span><a class="text-link" href="/app/analyze/${key}">Open</a></div>`).join("")}</div></div>
      </aside>
    </section>`;
}

function renderDatasets(fileId = null) {
  if (fileId !== null) return renderDatasetDetail(fileId);
  const params = new URLSearchParams(window.location.search);
  const query = params.get("q") || "";
  const keys = [...new Set(state.files.map(file => techniqueKey(file.technique)))];
  view.innerHTML = `${noticeHtml()}${pageHeading("Immutable catalog", "Datasets", "Search every imported file, inspect its provenance, and route it into the correct analysis workspace.", '<a class="button" href="/comparison">Compare datasets</a><a class="button primary" href="/upload#import-panel">Import data</a>')}
    <div class="filter-bar"><input id="dataset-filter" type="search" value="${escapeHtml(query)}" placeholder="Filter by filename, sample, material, or instrument" aria-label="Filter datasets"><select id="dataset-technique-filter" aria-label="Filter by technique"><option value="all">All techniques</option>${keys.map(key => `<option value="${key}">${escapeHtml(key === "other" ? "Other" : techniqueDefinitions[key].short)}</option>`).join("")}</select><label class="filter-group-label">Group by<select id="dataset-group-filter" aria-label="Group datasets by"><option value="sample" selected>Sample</option><option value="project">Project</option><option value="technique">Technique</option><option value="measurement_date">Measurement date</option><option value="instrument">Instrument</option></select></label><span class="filter-count" id="dataset-filter-count"></span></div>
    <div id="dataset-results"></div>`;
  const filter = document.querySelector("#dataset-filter");
  const techniqueFilter = document.querySelector("#dataset-technique-filter");
  const groupFilter = document.querySelector("#dataset-group-filter");
  const groupLabel = (file, grouping) => {
    if (grouping === "sample") return file.sample_id || "Unassigned sample";
    if (grouping === "project") return projectNameFor(file);
    if (grouping === "technique") return techniqueName(file);
    if (grouping === "measurement_date") return file.measurement_date || "Date not recorded";
    if (grouping === "instrument") return file.instrument || "Instrument not recorded";
    return "Unassigned";
  };
  const update = () => {
    const term = filter.value.trim().toLowerCase();
    const selected = techniqueFilter.value;
    const files = state.files.filter(file => {
      const haystack = [file.original_filename, file.sample_id, file.material_system, file.instrument, file.operator, file.technique].join(" ").toLowerCase();
      return (!term || haystack.includes(term)) && (selected === "all" || techniqueKey(file.technique) === selected);
    });
    document.querySelector("#dataset-filter-count").textContent = `${files.length} of ${state.files.length} datasets`;
    if (!files.length) {
      document.querySelector("#dataset-results").innerHTML = emptyState("No matching datasets", "Change the search or technique filter to see other measurements.");
      return;
    }
    const groups = new Map();
    files.forEach(file => {
      const label = groupLabel(file, groupFilter.value);
      if (!groups.has(label)) groups.set(label, []);
      groups.get(label).push(file);
    });
    document.querySelector("#dataset-results").innerHTML = [...groups.entries()]
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([label, groupedFiles]) => `<section class="panel dataset-group"><div class="panel-heading"><div><h2>${escapeHtml(label)}</h2><p>${groupedFiles.length} dataset${groupedFiles.length === 1 ? "" : "s"}</p></div><span class="badge neutral">${escapeHtml(groupFilter.options[groupFilter.selectedIndex].textContent)}</span></div>${datasetRows(groupedFiles)}</section>`)
      .join("");
  };
  filter.addEventListener("input", update);
  techniqueFilter.addEventListener("change", update);
  groupFilter.addEventListener("change", update);
  update();
}

function renderDatasetDetail(fileId) {
  const file = state.files.find(item => item.file_id === fileId);
  if (!file) return renderNotFound("Dataset not found", "The requested dataset is not in the active catalog. It may have been archived.");
  const technique = techniqueKey(file.technique);
  const project = projectNameFor(file);
  const fieldValue = name => escapeHtml(name === "project" ? project === "Unassigned" ? "" : project : file[name] || "");
  view.innerHTML = `${noticeHtml()}${pageHeading("Dataset", file.original_filename, `${file.technique || "Unspecified technique"} measurement for ${file.sample_id || "an unassigned sample"}.`, `<a class="button" href="/app/datasets">All datasets</a><a class="button primary" href="${analysisHref(file)}">Open analysis</a>`)}
    <section class="detail-grid">
      <div class="stack">
        <div class="panel"><div class="panel-heading"><div><h2>Measurement context</h2><p>Suggestions come from the original file and remain editable; the source identity remains protected.</p></div><span class="technique-chip ${technique}">${escapeHtml(techniqueName(file))}</span></div><form id="dataset-metadata-form" class="stack"><div class="form-grid"><label>Sample<input name="sample_id" required value="${fieldValue("sample_id")}"></label><label>Material system<input name="material_system" value="${fieldValue("material_system")}"></label><label>Measurement date<input name="measurement_date" type="date" value="${fieldValue("measurement_date")}"></label><label>Operator<input name="operator" value="${fieldValue("operator")}"></label><label>Project<input name="project" value="${fieldValue("project")}"></label><label>Substrate<input name="substrate" value="${fieldValue("substrate")}"></label><label>Instrument<input name="instrument" value="${fieldValue("instrument")}"></label><label>Measurement role<select name="measurement_role"><option value="unspecified">Unspecified</option><option value="sample">Sample</option><option value="sample_on_substrate">Sample on substrate</option><option value="pure_substrate_reference">Pure substrate reference</option></select></label></div><div class="page-actions" style="justify-content:flex-start"><button class="button primary" type="submit">Save context</button><span id="dataset-metadata-status" class="muted" role="status"></span></div></form></div>
        <div class="panel"><div class="panel-heading"><div><h2>Analysis history</h2><p>Checksum-verified processing runs for this dataset.</p></div></div><div id="dataset-history"><p class="muted">Loading saved analyses...</p></div></div>
      </div>
      <aside class="stack">
        <div class="panel"><div class="panel-heading"><div><h2>Raw source</h2><p>Immutable imported measurement.</p></div>${file.latest_processed_at ? '<span class="badge success">Analyzed</span>' : '<span class="badge neutral">Raw only</span>'}</div><dl class="definition-list"><div><dt>Size</dt><dd>${formatBytes(file.size_bytes)}</dd></div><div><dt>Imported</dt><dd>${formatDate(file.imported_at, true)}</dd></div><div><dt>Content type</dt><dd>${escapeHtml(file.content_type || "Unknown")}</dd></div><div><dt>Filename</dt><dd>${escapeHtml(file.original_filename)}</dd></div><div style="grid-column:1/-1"><dt>SHA-256</dt><dd class="checksum">${escapeHtml(file.sha256)}</dd></div></dl><p class="provenance-note">Processing creates separate derived files. The original bytes and checksum are never rewritten.</p><div class="page-actions" style="justify-content:flex-start"><a class="button" href="/files/${encodeURIComponent(file.file_id)}/content">Open raw file</a><a class="button" href="/files/${encodeURIComponent(file.file_id)}/integrity">Verify integrity</a></div></div>
        <div class="panel"><div class="panel-heading"><div><h2>Next action</h2></div></div><p class="muted" style="font-size:.8rem;line-height:1.55">${technique === "raman" ? "This measurement can use the complete Raman processing studio, including recipes, quality gates, peak fitting, references, and exports." : `Continue in the ${techniqueDefinitions[technique]?.short || "technique"} workspace to review supported inputs and planned processing stages.`}</p><a class="button primary" href="${analysisHref(file)}">Open ${escapeHtml(techniqueName(file))} analysis</a></div>
      </aside>
    </section>`;
  document.querySelector("#dataset-metadata-form select").value = file.measurement_role || "unspecified";
  document.querySelector("#dataset-metadata-form").addEventListener("submit", event => saveDatasetMetadata(event, file));
  loadMetadataSuggestions(file);
  loadDatasetHistory(file);
}

async function loadMetadataSuggestions(file) {
  const form = document.querySelector("#dataset-metadata-form");
  const status = document.querySelector("#dataset-metadata-status");
  if (!form || !status) return;
  try {
    const response = await fetch(`/files/${encodeURIComponent(file.file_id)}/metadata-suggestions`);
    if (!response.ok) throw new Error("Suggestions could not be loaded.");
    const result = await response.json();
    Object.entries(result.suggested_metadata || {}).forEach(([name, value]) => {
      const field = form.elements.namedItem(name);
      const genericSample = file.relative_path?.replaceAll("\\", "/").split("/")[0];
      const shouldReplace = !field?.value
        || field.value === "Unknown"
        || field.value === "unknown"
        || field.value === "unspecified"
        || (name === "sample_id" && field.value === genericSample)
        || (name === "project" && field.value === "Unassigned");
      if (field && shouldReplace) field.value = value;
    });
    status.textContent = Object.keys(result.suggested_metadata || {}).length ? "Suggestions loaded from the original file." : "No embedded metadata found.";
  } catch (error) {
    status.textContent = error.message;
  }
}

async function saveDatasetMetadata(event, file) {
  event.preventDefault();
  const form = event.currentTarget;
  const status = form.querySelector("#dataset-metadata-status");
  const payload = Object.fromEntries(new FormData(form).entries());
  try {
    const response = await fetch(`/files/${encodeURIComponent(file.file_id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail?.message || "Metadata could not be saved.");
    const index = state.files.findIndex(item => item.file_id === file.file_id);
    if (index >= 0) state.files[index] = { ...state.files[index], ...result };
    state.samples = await fetchJson("/samples");
    status.textContent = "Saved.";
    setTimeout(() => routeInfo().render(), 300);
  } catch (error) {
    status.textContent = error.message;
  }
}

async function loadDatasetHistory(file) {
  const container = document.querySelector("#dataset-history");
  if (!container) return;
  try {
    const response = await fetch(`/files/${encodeURIComponent(file.file_id)}/processing-runs`);
    if (!response.ok) throw new Error("History could not be loaded.");
    const runs = await response.json();
    container.innerHTML = runs.length ? `<div class="workflow-list">${runs.map((run, index) => `<div class="workflow-item"><span class="workflow-number">${runs.length - index}</span><span><strong>${escapeHtml(run.model_name)} ${escapeHtml(run.model_version || "")}</strong><small>${formatDate(run.processed_at, true)}</small></span><a class="text-link" href="/processing-runs/${encodeURIComponent(run.processing_id)}">JSON</a></div>`).join("")}</div>` : emptyState("No saved analysis", "Open the appropriate technique workspace to create a derived result.");
  } catch (error) {
    container.innerHTML = `<p class="muted">${escapeHtml(error.message)}</p>`;
  }
}

function renderAnalyzeHub() {
  view.innerHTML = `${noticeHtml()}${pageHeading("Technique workspaces", "Analyze", "Choose the scientific workflow that matches the measurement. Shared metadata, history, integrity, and exports remain consistent.", '<a class="button primary" href="/upload#import-panel">Import measurement</a>')}${techniqueCards()}`;
}

function renderTechnique(key) {
  if (['transport','afm','pl','xrd','sem','cvd','raman'].includes(key)) return renderResearchAnalysis(key);
  const definition = techniqueDefinitions[key];
  if (!definition) return renderNotFound("Technique not found", "Choose Raman, Transport, AFM, or XPS from the analysis navigation.");
  const files = state.files.filter(file => techniqueKey(file.technique) === key);
  const selectedId = new URLSearchParams(window.location.search).get("dataset");
  const selected = files.find(file => file.file_id === selectedId);
  view.innerHTML = `${noticeHtml()}
    <section class="tech-hero" style="--tech-color:${definition.color}">
      <div class="tech-hero-head"><div><p class="eyebrow">Technique workspace</p><h1>${escapeHtml(definition.name)}</h1><p class="lede">${escapeHtml(definition.description)}</p></div><span class="tech-symbol">${escapeHtml(definition.symbol)}</span></div>
      <div class="analysis-stages">${definition.stages.map(([title, detail], index) => `<div class="analysis-stage"><span>0${index + 1}</span><strong>${escapeHtml(title)}</strong><small>${escapeHtml(detail)}</small></div>`).join("")}</div>
    </section>
    ${selected ? `<div class="notice">Selected dataset: <strong>${escapeHtml(selected.original_filename)}</strong>. ${key === "raman" ? "Open the Raman studio to inspect and process it." : definition.ready ? "Its saved analysis is available below, or it can be processed now." : "Its raw data and metadata are safely cataloged while this technique's processing engine is added."}</div>` : ""}
    <section class="analysis-layout">
      <div class="panel">
        <div class="panel-heading"><div><h2>${definition.short} datasets</h2><p>${files.length} compatible measurement${files.length === 1 ? "" : "s"} in the active catalog.</p></div>${definition.ready ? '<span class="badge success">Processing available</span>' : '<span class="badge warning">Workspace foundation</span>'}</div>
        ${files.length ? datasetRows(files) : emptyState(`No ${definition.short} data`, `Import a measurement and label its technique as ${definition.name}.`, '<a class="button primary" href="/upload#import-panel">Import data</a>')}
        ${key === "raman" && files.length ? '<div class="page-actions" style="margin-top:1rem"><a class="button primary" href="/upload#recent-panel">Open Raman processing studio</a><a class="button" href="/comparison">Compare Raman spectra</a></div>' : ""}
      </div>
      <aside class="panel">
        <div class="panel-heading"><div><h2>Analysis modules</h2><p>${definition.ready ? "Available in the current application." : "The intended modules for this workspace."}</p></div></div>
        <div class="capability-list">${definition.capabilities.map(([title, detail], index) => `<div class="capability"><i>${index + 1}</i><span><strong>${escapeHtml(title)}</strong><small>${escapeHtml(detail)}</small></span></div>`).join("")}</div>
        ${!definition.ready ? '<p class="provenance-note">This page organizes compatible data without fabricating scientific results. Technique-specific parsers and validated analysis methods can be connected here incrementally.</p>' : ""}
      </aside>
    </section>
    ${key === "transport" && files.length ? '<section class="panel section-block" id="transport-analysis-panel"><p class="muted">Loading transport analysis...</p></section>' : ""}`;
  if (key === "transport" && files.length) initializeTransportPanel(files, selected || files[0]);
}

function formatEngineering(value, unit = "") {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "Not available";
  const number = Number(value);
  const magnitude = Math.abs(number);
  const scales = [
    [1e9, "G"], [1e6, "M"], [1e3, "k"], [1, ""],
    [1e-3, "m"], [1e-6, "u"], [1e-9, "n"], [1e-12, "p"], [1e-15, "f"],
  ];
  const [scale, prefix] = scales.find(([candidate]) => magnitude >= candidate) || [1e-15, "f"];
  return `${(number / scale).toPrecision(4)} ${prefix}${unit}`.trim();
}

function initializeTransportPanel(files, initialFile) {
  const panel = document.querySelector("#transport-analysis-panel");
  panel.innerHTML = `<div class="panel-heading"><div><h2>Transport analysis</h2><p>Stored workbook values, sheet classification, channel statistics, and ordinary least-squares I-V fits.</p></div><div class="page-actions"><select id="transport-file-select" aria-label="Transport dataset">${files.map(file => `<option value="${escapeHtml(file.file_id)}" ${file.file_id === initialFile.file_id ? "selected" : ""}>${escapeHtml(file.original_filename)}</option>`).join("")}</select><button class="button primary compact" id="transport-run-button" type="button">Analyze workbook</button></div></div><div id="transport-result"><p class="muted">Loading saved analysis...</p></div>`;
  const select = document.querySelector("#transport-file-select");
  select.addEventListener("change", () => loadTransportAnalysis(select.value));
  document.querySelector("#transport-run-button").addEventListener("click", () => loadTransportAnalysis(select.value, true));
  loadTransportAnalysis(initialFile.file_id);
}

async function loadTransportAnalysis(fileId, process = false) {
  const container = document.querySelector("#transport-result");
  const button = document.querySelector("#transport-run-button");
  if (!container || !button) return;
  container.innerHTML = '<p class="muted">Reading the workbook and verifying saved provenance...</p>';
  button.disabled = true;
  try {
    const response = await fetch(`/files/${encodeURIComponent(fileId)}/transport-analysis`, process ? { method: "POST" } : undefined);
    const result = await response.json();
    if (!response.ok) {
      if (!process && result.detail?.code === "transport_analysis_not_found") {
        container.innerHTML = `${emptyState("Analysis not run", "The raw workbook is preserved. Run the transport parser to create a separate derived result.", '<button class="button primary" id="transport-empty-run" type="button">Analyze workbook</button>')}`;
        document.querySelector("#transport-empty-run").addEventListener("click", () => loadTransportAnalysis(fileId, true));
        return;
      }
      throw new Error(result.detail?.message || "Transport analysis failed.");
    }
    renderTransportResult(result);
  } catch (error) {
    container.innerHTML = `<div class="notice error">${escapeHtml(error.message)}</div>`;
  } finally {
    button.disabled = false;
  }
}

function renderTransportResult(result) {
  const container = document.querySelector("#transport-result");
  if (!container) return;
  const summary = result.summary;
  const measurementSheets = result.sheets.filter(sheet => sheet.measurement_type !== "metadata");
  const warnings = result.workbook.warnings || [];
  container.innerHTML = `${warnings.map(warning => `<div class="notice ${warning.includes("partial") || warning.includes("damaged") ? "error" : ""}">${escapeHtml(warning)}</div>`).join("")}
    ${renderTransportSettings(result.workbook.settings)}
    <div class="metric-grid transport-metrics">
      ${metric("Measurement sheets", result.workbook.measurement_sheet_count, `${result.workbook.worksheet_count} total worksheets`)}
      ${metric("Data points", Number(summary.total_data_rows).toLocaleString(), "Stored values analyzed")}
      ${metric("Median I-V resistance", formatEngineering(summary.median_iv_fit_resistance_ohm, "ohm"), "OLS slope across readable I-V sweeps")}
      ${metric("Zero-bias AI", formatEngineering(summary.median_zero_bias_ai_a, "A"), "Median across zero-bias time traces")}
    </div>
    <div class="transport-viewer">
      <div class="plot-card"><div class="panel-heading"><div><h3>Measurement trace</h3><p>Choose a worksheet to inspect the stored channels.</p></div><select id="transport-sheet-select" aria-label="Measurement worksheet">${measurementSheets.map((sheet, index) => `<option value="${index}">${escapeHtml(sheet.name)}</option>`).join("")}</select></div><canvas id="transport-canvas" class="transport-canvas" height="380"></canvas><div id="transport-sheet-detail"></div></div>
      <aside class="panel compact-panel"><h3>Analysis protocol</h3><div class="workflow-list" style="margin-top:.7rem"><div class="workflow-item"><span class="workflow-number">1</span><span><strong>Stored values only</strong><small>No workbook formulas are recalculated</small></span></div><div class="workflow-item"><span class="workflow-number">2</span><span><strong>Sheet classification</strong><small>Time trace, voltage sweep, or operating point</small></span></div><div class="workflow-item"><span class="workflow-number">3</span><span><strong>Channel statistics</strong><small>Median, range, standard deviation, and drift</small></span></div><div class="workflow-item"><span class="workflow-number">4</span><span><strong>I-V fit</strong><small>OLS conductance, intercept, resistance, and R-squared</small></span></div></div><p class="provenance-note">Result ${escapeHtml(result.model_version)} is linked to source checksum ${escapeHtml(result.source_sha256.slice(0, 12))}...</p></aside>
    </div>`;
  const selector = document.querySelector("#transport-sheet-select");
  const update = () => drawTransportSheet(measurementSheets[Number(selector.value)]);
  selector.addEventListener("change", update);
  update();
}

function renderTransportSettings(settings = {}) {
  const terminals = settings.terminals || [];
  if (!terminals.length) return "";
  const rows = terminals.map(terminal => `<tr><td>${escapeHtml(terminal.device_terminal || "Not recorded")}</td><td>${escapeHtml(terminal.terminal_name || "Not recorded")}</td><td>${escapeHtml(terminal.operation_mode || "Not recorded")}</td><td>${escapeHtml(terminal.current_data || "Not recorded")}</td><td>${escapeHtml(terminal.voltage_data || "Not recorded")}</td></tr>`).join("");
  const biased = (settings.biased_terminals || []).map(terminal => `${terminal.device_terminal || "?"} (${terminal.terminal_name || "unnamed"})`).join(", ");
  const roleWarnings = (settings.role_warnings || []).map(warning => `<div class="notice">${escapeHtml(warning)}</div>`).join("");
  return `<div class="panel transport-settings"><div class="panel-heading"><div><h3>Instrument settings</h3><p>Read directly from the workbook Settings sheet.</p></div><span class="badge neutral">Immutable source metadata</span></div>${roleWarnings}<p class="provenance-note"><strong>Biased terminal:</strong> ${escapeHtml(biased || "Not identified")}</p><div class="table-shell"><table><thead><tr><th>Device terminal</th><th>Terminal name</th><th>Device role</th><th>Operation</th><th>Current data</th><th>Voltage data</th></tr></thead><tbody>${terminals.map(terminal => `<tr><td>${escapeHtml(terminal.device_terminal || "Not recorded")}${terminal.terminal_role ? ` (${escapeHtml(terminal.terminal_role)})` : ""}</td><td>${escapeHtml(terminal.terminal_name || "Not recorded")}${terminal.terminal_role ? ` (${escapeHtml(terminal.terminal_role)})` : ""}</td><td>${escapeHtml(terminal.terminal_role || "Ambiguous")}</td><td>${escapeHtml(terminal.operation_mode || "Not recorded")}</td><td>${escapeHtml(terminal.current_data || "Not recorded")}</td><td>${escapeHtml(terminal.voltage_data || "Not recorded")}</td></tr>`).join("")}</tbody></table></div></div>`;
}

function drawTransportSheet(sheet) {
  const canvas = document.querySelector("#transport-canvas");
  const detail = document.querySelector("#transport-sheet-detail");
  if (!canvas || !detail || !sheet) return;
  const series = sheet.preview_series || [];
  const voltageCandidates = ["AV", "BV"].map(key => ({ key, values: series.map(row => row[key]).filter(Number.isFinite) }));
  const voltageAxis = voltageCandidates.sort((a, b) => (Math.max(...b.values, 0) - Math.min(...b.values, 0)) - (Math.max(...a.values, 0) - Math.min(...a.values, 0)))[0];
  const xKey = ["iv_sweep", "voltage_sweep"].includes(sheet.measurement_type) && voltageAxis.values.length ? voltageAxis.key : "Time";
  const points = series.filter(row => Number.isFinite(row[xKey]) && (Number.isFinite(row.AI) || Number.isFinite(row.BI)));
  const fit = Object.values(sheet.iv_fits || {})[0];
  const ai = sheet.channel_statistics?.AI;
  detail.innerHTML = `<div class="transport-sheet-stats"><span><strong>Type</strong>${escapeHtml(sheet.measurement_type.replaceAll("_", " "))}</span><span><strong>Rows</strong>${sheet.row_count}</span><span><strong>AI median</strong>${formatEngineering(ai?.median, "A")}</span><span><strong>AI deviation</strong>${formatEngineering(ai?.standard_deviation, "A")}</span><span><strong>Duration</strong>${formatEngineering(sheet.time?.duration, "s")}</span><span><strong>Fit resistance</strong>${formatEngineering(fit?.resistance_ohm, "ohm")}</span><span><strong>Fit R-squared</strong>${fit?.r_squared === null || fit?.r_squared === undefined ? "Not available" : Number(fit.r_squared).toFixed(4)}</span></div>`;
  const width = Math.max(620, canvas.clientWidth || 620);
  const height = 380;
  const ratio = window.devicePixelRatio || 1;
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  const context = canvas.getContext("2d");
  context.scale(ratio, ratio);
  context.clearRect(0, 0, width, height);
  context.fillStyle = "#ffffff";
  context.fillRect(0, 0, width, height);
  if (!points.length) {
    context.fillStyle = "#66746f";
    context.font = "14px system-ui";
    context.fillText("No plottable current series in this worksheet.", 28, 48);
    return;
  }
  const xValues = points.map(row => row[xKey]);
  const yValues = points.flatMap(row => [row.AI, row.BI]).filter(Number.isFinite);
  let xMin = Math.min(...xValues), xMax = Math.max(...xValues), yMin = Math.min(...yValues), yMax = Math.max(...yValues);
  if (xMin === xMax) { xMin -= 1; xMax += 1; }
  if (yMin === yMax) { yMin -= 1; yMax += 1; }
  const yPad = (yMax - yMin) * .08;
  yMin -= yPad; yMax += yPad;
  const frame = { left: 72, right: width - 24, top: 24, bottom: height - 52 };
  const px = value => frame.left + (value - xMin) / (xMax - xMin) * (frame.right - frame.left);
  const py = value => frame.bottom - (value - yMin) / (yMax - yMin) * (frame.bottom - frame.top);
  context.strokeStyle = "#dfe6e2";
  context.lineWidth = 1;
  for (let index = 0; index <= 5; index += 1) {
    const y = frame.top + index / 5 * (frame.bottom - frame.top);
    context.beginPath(); context.moveTo(frame.left, y); context.lineTo(frame.right, y); context.stroke();
  }
  context.strokeStyle = "#8c9894";
  context.beginPath(); context.moveTo(frame.left, frame.top); context.lineTo(frame.left, frame.bottom); context.lineTo(frame.right, frame.bottom); context.stroke();
  [["AI", "#d47534"], ["BI", "#3f8f84"]].forEach(([key, color]) => {
    const channel = points.filter(row => Number.isFinite(row[key]));
    if (!channel.length) return;
    context.strokeStyle = color; context.lineWidth = 2; context.beginPath();
    channel.forEach((row, index) => { const x = px(row[xKey]), y = py(row[key]); if (index) context.lineTo(x, y); else context.moveTo(x, y); });
    context.stroke();
  });
  context.fillStyle = "#66746f"; context.font = "12px system-ui";
  context.fillText(`${xKey} (${xKey === "Time" ? "s inferred" : "V inferred"})`, frame.left, height - 18);
  context.fillText(formatEngineering(yMax, "A"), 8, frame.top + 4);
  context.fillText(formatEngineering(yMin, "A"), 8, frame.bottom);
  context.fillStyle = "#d47534"; context.fillText("AI", frame.right - 76, 16);
  context.fillStyle = "#3f8f84"; context.fillText("BI", frame.right - 38, 16);
}

function renderReferences() {
  const rows = state.references.map(reference => `<tr><td class="primary-cell"><strong>${escapeHtml(reference.title || reference.original_filename)}</strong><small>${escapeHtml(reference.citation || reference.original_filename)}</small></td><td>${escapeHtml(reference.technique)}</td><td>${escapeHtml(reference.material_system || "Unknown")}</td><td><span class="badge ${reference.extraction_status === "complete" ? "success" : "neutral"}">${escapeHtml(reference.extraction_status || "Stored")}</span></td><td>${formatDate(reference.imported_at)}</td></tr>`).join("");
  view.innerHTML = `${noticeHtml()}${pageHeading("Traceable evidence", "Reference library", "Keep literature, reference spectra, substrate measurements, and extracted evidence close to the analyses that use them.", '<a class="button primary" href="/upload#reference-panel">Add reference</a>')}
    <section class="metric-grid">${metric("Sources", state.references.length, "Preserved original files")}${metric("Techniques", new Set(state.references.map(item => item.technique).filter(Boolean)).size, "Represented in evidence")}${metric("Materials", new Set(state.references.map(item => item.material_system).filter(Boolean)).size, "Reference material systems")}${metric("Extracted", state.references.filter(item => item.extraction_status === "complete").length, "Machine-readable evidence")}</section>
    <section class="panel"><div class="panel-heading"><div><h2>Evidence catalog</h2><p>Sources are linked by technique and material system.</p></div></div>${rows ? `<div class="table-shell"><table><thead><tr><th>Source</th><th>Technique</th><th>Material</th><th>Extraction</th><th>Added</th></tr></thead><tbody>${rows}</tbody></table></div>` : emptyState("No references yet", "Add a paper or reference spectrum to support traceable interpretation.", '<a class="button primary" href="/upload#reference-panel">Add reference</a>')}</section>`;
}

function renderReports() {
  const processed = state.files.filter(file => file.latest_processed_at);
  const rows = processed.map(file => `<tr><td class="primary-cell"><a class="text-link" href="/app/datasets/${encodeURIComponent(file.file_id)}">${escapeHtml(file.original_filename)}</a><small>${escapeHtml(file.sample_id)}</small></td><td>${techniqueChip(file)}</td><td>${formatDate(file.latest_processed_at, true)}</td><td>${techniqueKey(file) === "transport" ? `<a class="button compact" href="/app/analyze/transport?dataset=${encodeURIComponent(file.file_id)}">View analysis</a>` : `<a class="button compact" href="/files/${encodeURIComponent(file.file_id)}/export/reproducibility">Reproducibility ZIP</a>`}</td></tr>`).join("");
  view.innerHTML = `${noticeHtml()}${pageHeading("Reviewer-ready outputs", "Reports & exports", "Download processed results together with raw data, exact parameters, environment details, and checksum manifests.", '<a class="button" href="/app/datasets">Browse datasets</a>')}
    <section class="dashboard-grid"><div class="panel"><div class="panel-heading"><div><h2>Available exports</h2><p>One reproducibility package per processed measurement.</p></div><span class="badge success">${processed.length} ready</span></div>${rows ? `<div class="table-shell"><table><thead><tr><th>Dataset</th><th>Technique</th><th>Processed</th><th>Export</th></tr></thead><tbody>${rows}</tbody></table></div>` : emptyState("No processed results", "Run an analysis before creating a reproducibility package.", '<a class="button primary" href="/app/analyze/raman">Open analysis</a>')}</div><aside class="panel"><div class="panel-heading"><div><h2>Package contents</h2><p>Designed for audit and replay.</p></div></div><div class="workflow-list"><div class="workflow-item"><span class="workflow-number">1</span><span><strong>Verified raw file</strong><small>Original bytes and SHA-256</small></span></div><div class="workflow-item"><span class="workflow-number">2</span><span><strong>Processing protocol</strong><small>Exact parameters and versions</small></span></div><div class="workflow-item"><span class="workflow-number">3</span><span><strong>Derived series</strong><small>Plotted and fitted data as CSV</small></span></div><div class="workflow-item"><span class="workflow-number">4</span><span><strong>Evidence</strong><small>Assignments and confirmations</small></span></div><div class="workflow-item"><span class="workflow-number">5</span><span><strong>Manifest</strong><small>Checksums for every artifact</small></span></div></div></aside></section>`;
}

function renderNotFound(title, message) {
  view.innerHTML = `${pageHeading("Workspace", title, message, '<a class="button primary" href="/app">Return home</a>')}`;
}

function routeInfo() {
  const path = window.location.pathname.replace(/^\/app\/?/, "");
  const parts = path.split("/").filter(Boolean).map(safeDecode);
  if (!parts.length) return { key: "home", label: "Overview", render: renderDashboard };
  if (parts[0] === 'research') return {key:'research',label:'Research library',render:renderResearchLibrary};
  if (parts[0] === "projects") return { key: "projects", label: parts[1] || "Projects", render: () => renderProjects(parts[1] ?? null) };
  if (parts[0] === "samples") return { key: "samples", label: parts[1] || "Samples", render: () => renderSamples(parts[1] ?? null) };
  if (parts[0] === "datasets") return { key: "datasets", label: parts[1] || "Datasets", render: () => renderDatasets(parts[1] ?? null) };
  if (parts[0] === "analyze" && !parts[1]) return { key: "raman", label: "Analyze", render: renderAnalyzeHub };
  if (parts[0] === "analyze") return { key: parts[1], label: techniqueDefinitions[parts[1]]?.short || "Analyze", render: () => renderTechnique(parts[1]) };
  if (parts[0] === "references") return { key: "references", label: "References", render: renderReferences };
  if (parts[0] === "reports") return { key: "reports", label: "Reports & exports", render: renderReports };
  return { key: "none", label: "Not found", render: () => renderNotFound("Page not found", "The requested workspace page does not exist.") };
}

function syncNavigation(route) {
  document.querySelectorAll("[data-route]").forEach(link => {
    const active = link.dataset.route === route.key;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
  });
  breadcrumb.textContent = `Workspace / ${route.label}`;
  document.title = `${route.label} - Materials Data Copilot`;
}

async function fetchJson(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${url} returned ${response.status}.`);
  return response.json();
}

async function initialize() {
  const route = routeInfo();
  syncNavigation(route);
  const requests = [
    ["files", "/files?include_analysis=true"],
    ["samples", "/samples"],
    ["references", "/references"],
    ["health", "/health"],
  ];
  const results = await Promise.allSettled(requests.map(([, url]) => fetchJson(url)));
  results.forEach((result, index) => {
    const [key] = requests[index];
    if (result.status === "fulfilled") state[key] = result.value;
    else state.errors.push(result.reason.message);
  });
  if (state.health?.status === "healthy") {
    serverIndicator.classList.add("online");
    serverIndicator.querySelector("span").textContent = "Local data verified";
  } else if (state.errors.length) {
    serverIndicator.classList.add("error");
    serverIndicator.querySelector("span").textContent = "Data check needs attention";
  }
  route.render();
}

function closeMenu() {
  sidebar.classList.remove("open");
  scrim.hidden = true;
  menuButton.setAttribute("aria-expanded", "false");
}

menuButton.addEventListener("click", () => {
  const open = !sidebar.classList.contains("open");
  sidebar.classList.toggle("open", open);
  scrim.hidden = !open;
  menuButton.setAttribute("aria-expanded", String(open));
});
scrim.addEventListener("click", closeMenu);
document.addEventListener("keydown", event => {
  if (event.key === "Escape") closeMenu();
  if (event.key === "/" && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) {
    event.preventDefault();
    globalSearchInput.focus();
  }
});
globalSearch.addEventListener("submit", event => {
  event.preventDefault();
  const query = globalSearchInput.value.trim();
  window.location.href = `/app/datasets${query ? `?q=${encodeURIComponent(query)}` : ""}`;
});

initialize();
