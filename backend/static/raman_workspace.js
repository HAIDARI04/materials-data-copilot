/* The workspace uses the upload studio itself so Raman controls share one implementation. */
const RamanWorkspace = {
  enabled: new URLSearchParams(location.search).get('workspace') === 'raman',
  files(files) {
    return this.enabled ? files.filter(file => /raman/i.test(file.technique || '') &&
      !['image', 'document'].includes(file.data_category) &&
      /\.(wdf|csv|tsv|dat|txt|xy)$/i.test(file.original_filename || '')) : files;
  },
  initialFile(files, previous) {
    const requested = this.enabled ? new URLSearchParams(location.search).get('dataset') : null;
    return files.find(file => file.file_id === requested) ||
      files.find(file => file.file_id === previous?.fileId) || files[0];
  },
  bindLayout() {
    // Match Transport's outer viewport breakpoint, not the narrower embedded frame.
    const layoutViewport = window.parent === window ? window : window.parent;
    const wideLayout = layoutViewport.matchMedia('(min-width: 901px)');
    const updateLayout = () => document.body.classList.toggle('raman-wide', wideLayout.matches);
    updateLayout();
    wideLayout.addEventListener('change', updateLayout);
    window.addEventListener('pagehide', () => wideLayout.removeEventListener('change', updateLayout), {once: true});
  },
  setup() {
    if (!this.enabled) return;
    document.body.classList.add('raman-workspace');
    this.bindLayout();
    document.title = 'Raman analysis';
    const left = document.querySelector('#recent-panel');
    const heading = document.createElement('h2');
    heading.textContent = 'Analysis choices';
    left.prepend(heading);
    document.querySelector('#recent-heading').textContent = 'Imported measurements';
    document.querySelector('#technique-filter').closest('label').hidden = true;
    const choices = document.createElement('section');
    choices.className = 'raman-analysis-choices';
    choices.setAttribute('aria-label', 'Raman analysis choices');
    const title = document.createElement('h3');
    title.textContent = 'Raman processing';
    choices.append(title);
    // Move existing nodes, preserving the original controls and event bindings.
    for (const id of ['wdf-dataset-label', 'analysis-recipe-select', 'baseline-method-select',
      'deconvolution-select', 'substrate-correction-select', 'normalization-mode-select',
      'normalization-peak-label']) {
      const node = document.getElementById(id);
      choices.append(node.matches('label') ? node : node.closest('label'));
    }
    choices.append(document.querySelector('#advanced-processing-settings'));
    choices.append(document.querySelector('#planned-protocol'));
    for (const id of ['process-button', 'save-recipe-button', 'analysis-history-button']) {
      choices.append(document.getElementById(id));
    }
    document.querySelector('#optical-image-card').after(choices);
    document.querySelector('#process-button').textContent = 'Analyze and save spectrum';
    document.querySelector('#analysis-history-button').textContent = 'Saved analyses';
    const plot = document.querySelector('#plot-panel');
    const plotTitle = document.createElement('h2');
    plotTitle.textContent = 'Raman Plot';
    plot.prepend(plotTitle);
    const empty = document.createElement('section');
    empty.className = 'panel raman-empty';
    empty.innerHTML = '<h2>Raman Plot</h2><p>Select an imported Raman measurement to inspect its spectrum, or import data to begin.</p><a href="/upload#import-panel" target="_top">Import Raman data</a>';
    plot.after(empty);
    for (const link of document.querySelectorAll('a[href^="/"]')) link.target = '_top';
    if (window.parent !== window) {
      let scheduled = false;
      const reportSize = () => {
        if (scheduled) return;
        scheduled = true;
        requestAnimationFrame(() => {
          scheduled = false;
          window.parent.postMessage({type: 'raman-studio-size',
            height: Math.ceil(document.querySelector('main').getBoundingClientRect().height) + 32,
            expanded: plot.classList.contains('plot-panel-expanded')}, location.origin);
        });
      };
      new ResizeObserver(reportSize).observe(document.querySelector('main'));
      new MutationObserver(reportSize).observe(plot, {attributes: true, attributeFilter: ['class', 'hidden']});
      reportSize();
    }
  },
};
if (typeof module !== 'undefined') module.exports = RamanWorkspace;
if (typeof document !== 'undefined') RamanWorkspace.setup();
