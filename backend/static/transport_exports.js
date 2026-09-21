/* Downloads always identify an immutable saved run. */
function transportExportControls(result) {
  const escape = value => String(value ?? '').replace(/[&<>"']/g, character => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[character]));
  const id = encodeURIComponent(result.processing_id);
  const root = `/research/analyses/${id}/export`;
  if (!result.implementation) return '<p class="notice">This saved analysis predates complete exports. Analyze and save again to create the report and reproducibility package. The earlier result remains available.</p>';
  return `<section class="transport-export-panel" aria-label="Analysis downloads">
    <div><h4>Your analysis package</h4><p>A step-by-step report, original measurements, figures, tables, references and an executable analysis.</p></div>
    <div class="page-actions"><a class="button primary" href="${root}" data-transport-download>Download complete analysis</a>
      <a class="button" href="${root}/file?name=report.html&amp;inline=true" target="_blank" rel="noopener">Preview report</a>
      <a class="button" href="${root}?format=report" data-transport-download>Report only (PDF)</a>
      <a class="button" href="${root}?format=figures" data-transport-download>Figures and tables</a></div>
    <details data-transport-files="${escape(result.processing_id)}"><summary>Individual figures and tables</summary><div class="transport-file-list"><p>Open to load available downloads.</p></div></details>
    <p class="transport-export-status" role="status" aria-live="polite"></p>
  </section>`;
}

function bindTransportExports(host) {
  host.querySelectorAll('[data-transport-files]').forEach(details => {
    details.addEventListener('toggle', async () => {
      if (!details.open || details.dataset.loaded === 'true' || details.dataset.loading === 'true') return;
      const list = details.querySelector('.transport-file-list');
      details.dataset.loading = 'true';
      list.textContent = 'Loading available files…';
      try {
        const root = `/research/analyses/${encodeURIComponent(details.dataset.transportFiles)}/export`;
        const response = await fetch(`${root}/files`);
        const data = await response.json();
        if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : data.detail?.message || 'Could not load files.');
        list.replaceChildren();
        const ul = document.createElement('ul');
        for (const file of data.files) {
          const li = document.createElement('li');
          const link = document.createElement('a');
          link.href = `${root}/file?name=${encodeURIComponent(file.name)}`;
          link.textContent = file.label;
          link.dataset.transportDownload = '';
          li.append(link);
          ul.append(li);
        }
        list.append(ul);
        details.dataset.loaded = 'true';
      } catch (error) {
        list.textContent = error.message + ' Close and reopen to retry.';
      } finally {
        details.dataset.loading = 'false';
      }
    });
  });
  if (host.dataset.transportDownloadsBound) return;
  host.dataset.transportDownloadsBound = 'true';
  host.addEventListener('click', async event => {
    const link = event.target.closest('[data-transport-download]');
    if (!link || !host.contains(link) || event.ctrlKey || event.metaKey || event.shiftKey) return;
    event.preventDefault();
    if (link.getAttribute('aria-disabled') === 'true') return;
    const panel = link.closest('.transport-export-panel');
    const status = panel.querySelector('.transport-export-status');
    status.textContent = 'Preparing your saved analysis. Large measurements may take a moment…';
    link.setAttribute('aria-disabled', 'true');
    try {
      const response = await fetch(link.href);
      if (!response.ok) {
        const error = await response.json();
        throw new Error(typeof error.detail === 'string' ? error.detail : error.detail?.message || 'Download failed.');
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const download = document.createElement('a');
      download.href = url;
      download.download = response.headers.get('Content-Disposition')?.match(/filename="([^"]+)"/)?.[1] || 'transport-analysis.zip';
      document.body.append(download);
      download.click();
      download.remove();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
      status.textContent = 'Download prepared. The report explains missing inputs beside the affected results.';
    } catch (error) {
      status.textContent = error.message;
    } finally {
      link.removeAttribute('aria-disabled');
    }
  });
}

if (typeof module !== 'undefined' && module.exports) module.exports = {transportExportControls};
