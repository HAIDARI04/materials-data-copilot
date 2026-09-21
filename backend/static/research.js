const researchTechniqueDefinitions = Object.fromEntries([
  ['pl','Photoluminescence','PL','Review energy or wavelength spectra, fit peaks, and retain axis units.'],
  ['xrd','X-ray diffraction','XRD','Process RAS patterns and compare imported reference patterns.'],
  ['sem','Scanning electron microscopy','SEM','Review instrument settings and measure calibrated image regions.'],
  ['cvd','Growth records','CVD','Read growth logs and link individual rows to devices.'],
].map(([key,name,short,description]) => [key,{name,short,symbol:short,color:'#347b72',description,ready:true,
  capabilities:[['Source review','Original measurements and explicit analysis choices.']],stages:[['Analyze','Save a traceable result.']]}]));

async function researchRequest(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : data.detail?.message || JSON.stringify(data.detail || data));
  return data;
}

function researchField(label,name,value='',type='text',extra='') {
  return `<label>${escapeHtml(label)}<input name="${name}" type="${type}" value="${escapeHtml(value)}" ${extra}></label>`;
}
function researchSelect(label,name,choices) {
  return `<label>${escapeHtml(label)}<select name="${name}">${choices.map(([v,t])=>`<option value="${escapeHtml(v)}">${escapeHtml(t)}</option>`).join('')}</select></label>`;
}
function inferredSampleGroup(file) {
  const path = (file.relative_path || file.original_filename || '').replaceAll('\\', '/');
  const parts = path.split('/').filter(Boolean);
  const deviceFolder = /^[A-Za-z]\d+(?=$|[_-])/i;
  const token = /(^|[^A-Za-z0-9])([A-Za-z]\d+)(?=$|[_\-.])/i;
  for (let index = parts.length - 2; index >= 0; index -= 1) {
    const match = parts[index].match(deviceFolder);
    if (match) return match[0].toUpperCase();
  }
  const filenameMatch = (parts.at(-1) || '').match(token);
  if (filenameMatch) return filenameMatch[2].toUpperCase();
  for (let index = parts.length - 2; index >= 0; index -= 1) {
    const match = parts[index].match(token);
    if (match) return match[2].toUpperCase();
  }
  return file.sample_id || 'Unassigned';
}
function importedMeasurementGroupValue(file, mode) {
  const values = {
    sample: inferredSampleGroup(file),
    project: file.project,
    technique: file.technique,
    date: file.measurement_date,
    instrument: file.instrument,
    folder: file.source_path || file.relative_path,
  };
  return values[mode] || 'Unassigned';
}
function importedMeasurementGroups(files, mode, selectedIds) {
  const groups = new Map();
  files.forEach(file => {
    const label = importedMeasurementGroupValue(file, mode) || 'Unassigned';
    if (!groups.has(label)) groups.set(label, []);
    groups.get(label).push(file);
  });
  return [...groups.entries()].sort(([left], [right]) => left.localeCompare(right)).map(([label, members]) => {
    members.sort((left, right) => left.original_filename.localeCompare(right.original_filename));
    const checkedCount = members.filter(file => selectedIds.has(file.file_id)).length;
    const groupChecked = checkedCount === members.length;
    return `<section class="imported-measurement-group" data-group="${escapeHtml(label)}">
      <label class="imported-measurement-group-header"><input type="checkbox" data-group-toggle ${groupChecked ? 'checked' : ''}><strong>${escapeHtml(label)}</strong><small>${checkedCount}/${members.length} selected</small></label>
      <div class="imported-measurement-members">${members.map(file => `<label class="imported-measurement-member"><input type="checkbox" name="file_id" value="${escapeHtml(file.file_id)}" data-member-toggle ${selectedIds.has(file.file_id) ? 'checked' : ''}><span>${escapeHtml(file.original_filename)}</span></label>`).join('')}</div>
    </section>`;
  }).join('');
}
function researchFileIsRelevant(file, key) {
  const filename = String(file.original_filename || '').toLowerCase();
  const extension = filename.includes('.') ? filename.slice(filename.lastIndexOf('.')) : '';
  const imageExtensions = new Set(['.bmp', '.gif', '.jpeg', '.jpg', '.png', '.tif', '.tiff']);
  const textExtensions = new Set(['.csv', '.dat', '.txt', '.tsv', '.xy']);
  const workbookExtensions = new Set(['.xls', '.xlsx']);
  const category = String(file.data_category || 'raw_measurement').toLowerCase();
  if (key === 'sem' || key === 'afm') {
    return imageExtensions.has(extension) || category === 'image';
  }
  if (category === 'image' || imageExtensions.has(extension)) return false;
  if (key === 'transport' || key === 'cvd') {
    return workbookExtensions.has(extension) || textExtensions.has(extension);
  }
  if (key === 'pl' || key === 'xrd' || key === 'raman') {
    return textExtensions.has(extension) || extension === '.wdf';
  }
  return true;
}
function researchJson(value) { return `<pre class="research-json">${escapeHtml(JSON.stringify(value,null,2))}</pre>`; }
function researchMessage(text,error=false) {
  const node = document.querySelector('#research-status');
  if (node) {node.className=error?'notice error':'notice';node.textContent=text;node.hidden=false;}
}
function bindResearchForm(id,action) {
  document.querySelector(id)?.addEventListener('submit',async event=>{
    event.preventDefault();
    const button=event.target.querySelector('button[type="submit"]');
    if(button)button.disabled=true;
    try { await action(new FormData(event.target),event.target); }
    catch(error){researchMessage(error.message,true);}
    finally{if(button)button.disabled=false;}
  });
}
function safeResearchUrl(value) {
  try {const url=new URL(value);return ['http:','https:'].includes(url.protocol)?url.href:null;}catch{return null;}
}

async function renderResearchLibrary() {
  view.innerHTML=pageHeading('Connected research','Research library','Connect devices, historical states, measurement runs and cited notes. Review folder imports before assigning files.')+'<div id="research-status" role="status" hidden></div><div id="research-library"></div>';
  try {
    const entities=await researchRequest('/research/entities');
    const choices=[['','Choose a record'],...entities.map(e=>[e.entity_id,`${e.kind} · ${e.name}`])];
    document.querySelector('#research-library').innerHTML=`<div class="research-grid">
      <section class="panel"><h2>Research records</h2><p>Create a project, then its substrates, devices and states. New states preserve earlier conditions.</p>
      <form id="entity-create" class="research-form">${researchSelect('Record type','kind',['project','substrate','device','state','run','region','growth'].map(k=>[k,k]))}${researchField('Name','name','','text','required maxlength="200"')}${researchSelect('Parent record','parent_id',[['','No parent (project only)'],...choices.slice(1)])}${researchField('Aliases, separated by commas','aliases')}${researchField('Material or device condition','condition')}${researchField('Fabrication or measurement date','date','','date')}<label>Notes and evidence<textarea name="notes" rows="3"></textarea></label><button type="submit" class="button primary">Create record</button></form>
      <div class="research-records">${entities.map(e=>`<button class="button" type="button" data-entity="${escapeHtml(e.entity_id)}">${escapeHtml(e.kind)} · ${escapeHtml(e.name)}</button>`).join('') || '<p>No records yet.</p>'}</div><div id="entity-detail"></div></section>
      <section class="panel"><h2>Cited evidence</h2><p>Search retained conversations, corrections and saved analysis summaries. Prior assistant interpretations stay labeled.</p>
      <form id="evidence-search" class="research-form">${researchField('Search source text','query','','search','required')}${researchSelect('Scope','entity_id',[['','All sources'],...choices.slice(1)])}<button class="button primary" type="submit">Find evidence</button></form><div id="evidence-matches"></div>
      <details><summary>Add a source-backed note</summary><form id="evidence-create" class="research-form">${researchField('Title','title','','text','required')}${researchSelect('Classification','kind',['observation','correction','hypothesis','plan','interpretation','reference'].map(k=>[k,k]))}${researchSelect('Related record','entity_id',[['','General research'],...choices.slice(1)])}<label>Source text<textarea name="body" required rows="5"></textarea></label>${researchField('Source URL, filename, or reference','source','','text','required')}<button class="button" type="submit">Save evidence</button></form></details>
      <details><summary>Import conversation snapshots</summary><form id="chat-import" class="research-form">${researchField('Folder containing extracted chat JSON snapshots','path','','text','required')}<p>These are retained snapshots, with source links and message roles. They do not provide continuous account access.</p><button class="button" type="submit">Import snapshots</button></form></details></section>
      </div><section class="panel"><h2>Review a folder</h2><form id="folder-scan" class="research-form">${researchField('Research folder on this computer','path','E:\\ASDNL\\Data-Files','text','required')}<button class="button primary" type="submit">Scan without importing</button></form><div id="folder-review"></div></section>
      <section class="panel"><h2>Link an existing measurement</h2><form id="file-link" class="research-form">${researchSelect('Research record','entity_id',choices)}${researchSelect('Imported file','file_id',state.files.map(f=>[f.file_id,f.original_filename]))}${researchField('Worksheet, row or spatial region (leave blank for whole file)','selector')}${researchSelect('Source role','role',['raw','derived','reference','preview','control','protocol'].map(k=>[k,k]))}<button class="button" type="submit">Save association</button></form><p>Use worksheet names for workbooks covering multiple devices. Link optical, Raman and AFM measurements to the same region only when their spatial relationship has been established.</p></section>`;
    document.querySelector('#research-library').insertAdjacentHTML('beforeend',`
      <section class="panel"><h2>Index experiment notes</h2><form id="document-index" class="research-form">${researchSelect('Imported note or document','file_id',state.files.filter(f=>/\.(txt|md|csv|pdf|docx|pptx)$/i.test(f.original_filename)).map(f=>[f.file_id,f.original_filename]))}${researchSelect('Related research record','entity_id',[['','General research'],...choices.slice(1)])}${researchSelect('Evidence classification','kind',['reference','observation','correction','hypothesis','plan','interpretation'].map(k=>[k,k]))}<button class="button" type="submit">Index retained document text</button></form><p>Text is cited by file checksum and page or section. Embedded images are not interpreted.</p></section>
      <section class="panel"><h2>Register spatial regions</h2><p>Use matched landmarks to relate optical, AFM, Raman or SEM coordinates. Save the correspondence before assigning material regions.</p><form id="spatial-register" class="research-form">${researchSelect('Region record','entity_id',entities.filter(e=>e.kind==='region').map(e=>[e.entity_id,e.name]))}${researchSelect('Source measurement','source_file',state.files.map(f=>[f.file_id,f.original_filename]))}${researchSelect('Target measurement','target_file',state.files.map(f=>[f.file_id,f.original_filename]))}<label>Matched landmarks: source x, source y, target x, target y (one pair per line)<textarea name="landmarks" rows="5" required></textarea></label><p>Use at least three non-collinear pairs. Record the coordinate units and orientation in the region notes; additional pairs allow an alignment error estimate.</p><button class="button" type="submit">Save spatial correspondence</button></form><div id="spatial-result"></div></section>`);
    bindResearchForm('#document-index',async data=>{const r=await researchRequest(`/research/files/${data.get('file_id')}/evidence`,{entity_id:data.get('entity_id')||null,kind:data.get('kind')});researchMessage(`${r.passages} cited passages indexed.`);});
    bindResearchForm('#spatial-register',async data=>{
      if(!data.get('entity_id'))throw new Error('Create a region under a device, state or run first.');
      if(data.get('source_file')===data.get('target_file'))throw new Error('Choose two different measurements.');
      const landmarks=data.get('landmarks').trim().split(/\r?\n/).map(row=>row.trim().split(/[\s,]+/).map(Number));
      if(landmarks.some(row=>row.length!==4||row.some(v=>!Number.isFinite(v))))throw new Error('Each landmark row requires four numeric coordinates.');
      const result=await researchRequest(`/research/files/${data.get('source_file')}/analyze`,{technique:'spatial',options:{landmarks,reference_file_ids:[data.get('target_file')]}});
      for(const file of [data.get('source_file'),data.get('target_file')])await researchRequest('/research/links',{entity_id:data.get('entity_id'),file_id:file,selector:`spatial registration ${result.processing_id}`,role:'raw'});
      document.querySelector('#spatial-result').innerHTML=researchJson({summary:result.summary,matrix:result.affine_matrix,warnings:result.warnings})+`<a class="button" href="/research/analyses/${result.processing_id}/export">Export registration</a>`;researchMessage('Correspondence saved and linked to the region.');
    });
    bindResearchForm('#entity-create',async data=>{
      await researchRequest('/research/entities',{kind:data.get('kind'),name:data.get('name'),parent_id:data.get('parent_id')||null,
        aliases:data.get('aliases').split(',').map(a=>a.trim()).filter(Boolean),metadata:{condition:data.get('condition'),date:data.get('date'),notes:data.get('notes')}});
      await renderResearchLibrary();researchMessage('Record created.');
    });
    document.querySelectorAll('[data-entity]').forEach(button=>button.addEventListener('click',async()=>{
      try {
        const record=await researchRequest(`/research/entities/${encodeURIComponent(button.dataset.entity)}`);
        document.querySelector('#entity-detail').innerHTML=`<h3>${escapeHtml(record.name)}</h3><p>${escapeHtml(record.aliases.join(', '))}</p>${researchJson(record.metadata)}
          <h4>Measurements and descendants</h4>${record.links.map(l=>`<p><a href="/app/datasets/${encodeURIComponent(l.file_id)}">${escapeHtml(l.original_filename)}</a> · ${escapeHtml(l.selector||'whole file')} · ${escapeHtml(l.role)}</p>`).join('')||'<p>No measurements linked.</p>'}
          <details><summary>History and cited notes (${record.history.length} corrections)</summary>${researchJson({history:record.history,evidence:record.evidence,children:record.children})}</details>
          ${record.kind==='state'?'<p>Create another state under the device for a material change.</p>':`<form id="record-correction" class="research-form">${researchField('Corrected condition','condition',record.metadata.condition||'')}<label>Updated notes<textarea name="notes">${escapeHtml(record.metadata.notes||'')}</textarea></label>${researchField('Reason and source for correction','reason','','text','required')}<button class="button" type="submit">Save correction in history</button></form>`}`;
        bindResearchForm('#record-correction',async data=>{
          await researchRequest(`/research/entities/${record.entity_id}/revisions`,{metadata:{...record.metadata,condition:data.get('condition'),notes:data.get('notes')},reason:data.get('reason')});
          researchMessage('Correction saved; earlier metadata remains in history.');button.click();
        });
      } catch(error){researchMessage(error.message,true);}
    }));
    bindResearchForm('#file-link',async data=>{await researchRequest('/research/links',Object.fromEntries(data));researchMessage('Measurement association saved.');});
    bindResearchForm('#evidence-create',async data=>{
      await researchRequest('/research/evidence',{title:data.get('title'),body:data.get('body'),kind:data.get('kind'),entity_id:data.get('entity_id')||null,citation:{source:data.get('source')}});researchMessage('Evidence saved with its source and classification.');
    });
    bindResearchForm('#evidence-search',async data=>{
      const response=await researchRequest(`/research/evidence?q=${encodeURIComponent(data.get('query'))}${data.get('entity_id')?'&entity_id='+encodeURIComponent(data.get('entity_id')):''}`);
      document.querySelector('#evidence-matches').innerHTML=response.matches.map(m=>{
        const url=safeResearchUrl(m.citation.url);
        return `<article class="research-evidence"><span class="badge">${escapeHtml(m.kind)}</span><h3>${escapeHtml(m.title)}</h3><p class="source-excerpt">${escapeHtml(m.excerpt)}</p>${url?`<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">Open original conversation</a>`:''}<details><summary>Citation and checksum</summary>${researchJson(m.citation)}<small>${escapeHtml(m.sha256)}</small></details></article>`;
      }).join('')||'<p>No matching retained source text.</p>';
    });
    bindResearchForm('#chat-import',async data=>{researchMessage('Importing retained source text…');const response=await researchRequest('/research/evidence/import-chats',{path:data.get('path')});researchMessage(`${response.snapshots} snapshots; ${response.message_occurrences} message occurrences retained. ${response.note}`);});
    bindResearchForm('#folder-scan',async data=>{
      researchMessage('Reading file signatures and checksums. A large folder may take several minutes.');
      const plan=await researchRequest('/research/import/scan',{path:data.get('path')});
      renderFolderReview(plan,choices);researchMessage(`${plan.entries.length} files reviewed. Select the files and metadata to import.`);
    });
  }catch(error){researchMessage(error.message,true);}
}

function renderFolderReview(plan,entityChoices) {
  const target=document.querySelector('#folder-review');
  target.innerHTML=`<form id="folder-commit" class="research-form">${researchSelect('Attach selected files to','entity_id',entityChoices)}${researchField('Reviewed sample ID for selected files','sample_id','','text','required maxlength="100"')}${researchField('Material system','material_system','Unknown')}${researchField('Filter filenames','filter','','search')}<p>Up to 100 matching files are shown. Every row has its own technique, role and metadata-conflict acknowledgement. Existing raw metadata is retained.</p><div class="table-shell"><table><thead><tr><th>Select</th><th>Source and review flags</th><th>Technique</th><th>Role</th><th>Keep existing metadata</th></tr></thead><tbody id="folder-rows"></tbody></table></div><button class="button primary" type="submit">Import selected files with reviewed assignments</button></form>`;
  const display=()=>{
    const filter=target.querySelector('[name=filter]').value.toLowerCase();
    const rows=plan.entries.filter(e=>e.relative_path.toLowerCase().includes(filter)).slice(0,100);
    document.querySelector('#folder-rows').innerHTML=rows.map(e=>`<tr data-entry="${e.entry_id}"><td><input aria-label="Select ${escapeHtml(e.relative_path)}" type="checkbox" name="selected" value="${e.entry_id}" ${e.error?'disabled':''}></td><td>${escapeHtml(e.relative_path)}<small class="research-flags">${escapeHtml(e.error||e.flags.join(' '))}</small><small>${escapeHtml(e.existing_metadata?JSON.stringify(e.existing_metadata):'')}${e.duplicate_of_entry!==undefined?' · Duplicate bytes within this folder':''}</small></td><td><input aria-label="Technique" name="technique-${e.entry_id}" value="${escapeHtml(e.technique)}" required></td><td><select aria-label="Role" name="role-${e.entry_id}">${['raw','derived','reference','preview','control','protocol'].map(r=>`<option ${r===e.role?'selected':''}>${r}</option>`).join('')}</select></td><td><input type="checkbox" aria-label="Acknowledge retaining existing metadata" name="ack-${e.entry_id}"></td></tr>`).join('');
  };
  target.querySelector('[name=filter]').addEventListener('input',display);display();
  bindResearchForm('#folder-commit',async data=>{
    const choices=data.getAll('selected').map(id=>({entry_id:Number(id),entity_id:data.get('entity_id'),sample_id:data.get('sample_id'),material_system:data.get('material_system'),technique:data.get(`technique-${id}`),role:data.get(`role-${id}`),acknowledge_existing_metadata:data.has(`ack-${id}`)}));
    if(!choices.length)throw new Error('Select at least one file.');
    const response=await researchRequest(`/research/import/${plan.plan_id}/commit`,{choices});
    const failures=response.outcomes.filter(o=>o.status==='error');
    researchMessage(`${response.outcomes.length-failures.length} files imported or linked. ${failures.length} need attention. ${failures.map(f=>plan.entries[f.entry_id].relative_path+': '+f.message).join(' ')}`,failures.length>0);
    state.files=await researchRequest('/files?include_analysis=true');
  });
}

function researchTechniqueOptions(key) {
  const fileOptions=[['','None'],...state.files.map(f=>[f.file_id,f.original_filename])];
  if(key==='transport')return `${researchField('Worksheet (blank means all)','sheet')}${researchSelect('Sweep direction','sweep_direction',[['dual','Dual sweep (increasing and decreasing)'],['increasing','Increasing only'],['decreasing','Decreasing only']])}<label>Terminal channels<select name="terminal_roles" multiple size="4"><option value="Source">Source</option><option value="Drain">Drain</option><option value="Gate">Gate</option><option value="instrument">Instrument channels (AI / BI)</option></select><small>Hold Ctrl to select any combination. Leave empty for all available channels.</small></label>${researchField('Fit from voltage (V)','fit_min','','number','step="any"')}${researchField('Fit to voltage (V)','fit_max','','number','step="any"')}
    <details><summary>Acquisition context and report inputs</summary>${researchSelect('Gate connection','gate_connection',['unknown','zero_bias','floating','disconnected','previously_connected'].map(k=>[k,k.replaceAll('_',' ')]))}${researchSelect('Illumination','light_condition',['unknown','dark','white_light','controlled_pulses'].map(k=>[k,k.replaceAll('_',' ')]))}${researchField('Biased terminal','biased_terminal','unknown')}${researchField('Temperature (K), if measured','temperature_k','','number','step="any" min="0"')}${researchField('Contacts, probes, chuck and conditioning history','configuration_notes')}
      <label class="research-check"><input type="checkbox" name="units_confirmed"> I checked the measurement units and conversion scales</label>
      <label class="research-check"><input type="checkbox" name="wiring_confirmed"> I checked the terminal roles, wiring and voltage reference</label>
      ${researchSelect('Voltage measurement configuration','measurement_configuration',[['unknown','Unknown'],['two_terminal','Two terminal (includes contacts and leads)'],['four_terminal','Four terminal (separate voltage probes)']])}</details>
    <details><summary>Resistivity and measurement uncertainty</summary><p>Enable this only for a uniform rectangular bar with measured four-terminal voltage. Missing inputs are explained in the report.</p>
      ${researchSelect('Sample geometry model','geometry_model',[['none','Do not calculate bulk resistivity'],['rectangular_bar','Uniform rectangular bar']])}
      ${[['length_m','Voltage-probe spacing (m)'],['width_m','Sample width (m)'],['thickness_m','Sample thickness (m)'],['length_standard_uncertainty_m','Spacing standard uncertainty (m)'],['width_standard_uncertainty_m','Width standard uncertainty (m)'],['thickness_standard_uncertainty_m','Thickness standard uncertainty (m)'],['current_relative_standard_uncertainty','Additional current calibration uncertainty (fraction, e.g. 0.01)'],['voltage_relative_standard_uncertainty','Additional voltage calibration uncertainty (fraction)']].map(([name,label])=>researchField(label,name,'','number','step="any" min="0"')).join('')}
      <small>Enter standard uncertainties, not expanded limits. Leave unknown values blank. The combination assumes independent inputs.</small></details>
    <details><summary>Ambiguous terminal mapping</summary><p>Use this only when the workbook labels instrument channels rather than Source/Drain/Gate. The mapping is saved with this derived analysis; the raw workbook is not changed.</p>${['AI','BI','DrainI','GateI','SourceI'].map(channel=>researchSelect(`${channel} physical role`,`terminal_mapping_${channel}`,[['','Unknown'],['Source','Source'],['Drain','Drain'],['Gate','Gate']])).join('')}</details>
    <details><summary>Channel mapping and units</summary><p>Leave current blank to preserve the detected channel names and inferred A/V/s units.</p>${researchField('Current column','current')}${researchField('Voltage column','voltage')}${researchField('Terminal name','terminal','unknown')}${researchField('Multiply current by to obtain A','current_scale',1,'number','step="any" min="0"')}${researchField('Multiply voltage by to obtain V','voltage_scale',1,'number','step="any" min="0"')}${researchSelect('Current polarity','polarity',[[1,'Retain sign'],[-1,'Reverse sign']])}${researchSelect('Voltage represents','voltage_status',[['unknown','Unknown'],['measured','Measured'],['programmed','Programmed']])}${researchField('Time column','time_column','Time')}${researchField('Multiply time by to obtain seconds','time_scale',1,'number','step="any" min="0"')}</details>
    <details><summary>Additional channels for signed diagnostics</summary>${[2,3].map(i=>`<h4>Channel ${i}</h4>${researchField('Current column '+i,'current'+i)}${researchField('Voltage column '+i,'voltage'+i)}${researchField('Terminal '+i,'terminal'+i,'unknown')}${researchSelect('Polarity '+i,'polarity'+i,[[1,'Retain sign'],[-1,'Reverse sign']])}`).join('')}<p>Additional channels use the current and voltage scale factors above.</p></details>
    <details><summary>Light, retention or prebias windows</summary>${[1,2,3,4].map(i=>`<h4>Window ${i}</h4>${researchField('Window label '+i,'window_label'+i)}${researchSelect('Window condition '+i,'window_kind'+i,['baseline','prebias','light_on','light_off','retention','control'].map(k=>[k,k.replaceAll('_',' ')]))}${researchField('Start (s) '+i,'window_start'+i,'','number','step="any"')}${researchField('End (s) '+i,'window_end'+i,'','number','step="any"')}`).join('')}</details>`;
  if(['pl','xrd'].includes(key))return `${key==='pl'?researchSelect('Text spectrum axis (WDF uses its recorded unit)','axis_unit',[['eV','Photon energy (eV)'],['nm','Wavelength (nm)']]):''}${researchField('Spectrum index in WDF (starts at 0)','dataset_index',0,'number','min="0"')}${researchField('Fit from x','fit_min','','number','step="any"')}${researchField('Fit to x','fit_max','','number','step="any"')}${researchField('Baseline smoothness','baseline_lambda',10000000,'number','min="1"')}${researchField('Baseline asymmetry','baseline_asymmetry',.001,'number','step="any" min="0.000001" max="0.499"')}${researchField('Peak prominence / noise','prominence_sigma',5,'number','step="any" min="0.01"')}${key==='xrd'?`<label>Reference patterns (hold Ctrl to select several)<select multiple name="reference_file_ids">${state.files.filter(f=>f.original_filename.toLowerCase().endsWith('.json')).map(f=>`<option value="${escapeHtml(f.file_id)}">${escapeHtml(f.original_filename)}</option>`).join('')}</select></label>${researchField('Reference line sigma (degrees)','reference_sigma',.08,'number','step="any" min="0.00001"')}`:''}`;
  if(key==='afm')return `${researchSelect('Leveling','level',[['plane','Remove fitted plane'],['none','No leveling'],['line_median','Remove each line median']])}${researchField('Region: left, top, right, bottom pixels','region')}${researchField('Excluded rectangle: left, top, right, bottom','exclude_region')}${researchField('Profile: start x, start y, end x, end y pixels','profile')}${researchField('Feature threshold in calibrated channel units (optional)','threshold','','number','step="any"')}${researchSelect('Matching backward scan','backward_file_id',fileOptions)}${researchSelect('Trace/retrace convention','lfm_convention',[['difference','Forward − backward'],['half_difference','(Forward − backward) / 2']])}${researchField('Shift backward scan: x, y pixels','shift','0,0')}<label class="research-check"><input name="auto_register" type="checkbox"> Estimate integer alignment from scan correlation</label>`;
  if(key==='sem')return `${researchSelect('Instrument TXT sidecar','sidecar_file_id',fileOptions)}${researchField('Confirmed pixel size (µm/pixel), optional','pixel_size_um','','number','step="any" min="0.00000001"')}<label class="research-check"><input name="confirm_sidecar_calibration" type="checkbox"> I verified the sidecar scale against this image</label><p>Click the image below to mark polygon corners, then finish the region. Measurements stay in pixels until calibration is confirmed.</p><button type="button" class="button" id="finish-polygon">Finish region</button><button type="button" class="button" id="clear-polygons">Clear draft regions</button><div id="polygon-count">No draft regions.</div>`;
  return '<p>Stored workbook values will be read. Growth records keep their worksheet and source row for explicit linking.</p>';
}

async function renderResearchAnalysis(key) {
  if (key === 'raman') return renderRamanAnalysis();
  const files=state.files.filter(f=>techniqueKey(f.technique)===key && researchFileIsRelevant(f,key));
  const selected=new URLSearchParams(location.search).get('dataset');
  const selectionKey = `transport-selection:${selected || 'default'}`;
  const savedSelection = key === 'transport' ? PlotPreferences.read(selectionKey)?.fileIds : null;
  const selectedIds = new Set(selected && files.some(file => file.file_id === selected) ? [selected] : files.slice(0, 1).map(file => file.file_id));
  if (Array.isArray(savedSelection)) {
    selectedIds.clear();
    savedSelection.filter(id => files.some(file => file.file_id === id)).forEach(id => selectedIds.add(id));
  }
  view.innerHTML=pageHeading('Measurement analysis',({transport:'Transport',afm:'AFM',pl:'Photoluminescence',xrd:'XRD',sem:'SEM',cvd:'Growth'})[key],
    'Review settings, save a versioned analysis, and export its exact parameters, source checksums and derived measurements.', '<a class="button" href="/app/research">Research library</a>')+
    `<div id="research-status" role="status" hidden></div><div class="research-grid"><section class="panel"><h2>Analysis choices</h2><form id="research-analyze" class="research-form"><label>Group imported measurements<select id="imported-measurement-grouping" name="measurement_grouping"><option value="sample">Sample</option><option value="project">Project</option><option value="technique">Technique</option><option value="date">Measurement date</option><option value="instrument">Instrument</option><option value="folder">Source folder</option></select></label><div id="imported-measurements" class="imported-measurements" aria-label="Imported measurements">${importedMeasurementGroups(files, 'sample', selectedIds)}</div>${researchTechniqueOptions(key)}<button class="button primary" type="submit" ${!files.length?'disabled':''}>Analyze and save selected</button></form>${!files.length?'<p>Import a compatible file to begin.</p>':''}<h3>Saved analyses</h3><div id="research-history"></div><details><summary>Compare saved results</summary><p>Choose saved results from files with comparable settings and controls.</p><div id="comparison-selections"></div><button id="research-compare" class="button" type="button">Compare selected</button></details></section><section class="panel"><h2>Measurement and result</h2><div id="research-source"></div><div id="research-result"><p>Select a file or open a saved analysis.</p></div></section></div>`;
  if (key === 'transport') {
    const guide = document.createElement('section'); guide.id = 'photodetector-guide';
    document.querySelector('#research-status').before(guide);
    initPhotodetectorWorkflow(files, guide);
  }
  const form=document.querySelector('#research-analyze');
  const selectedFileIds=()=>[...form.querySelectorAll('input[name="file_id"]:checked')].map(input=>input.value);
  const renderMeasurementGroups=mode=>{
    document.querySelector('#imported-measurements').innerHTML=importedMeasurementGroups(files,mode,new Set(selectedFileIds()));
    document.querySelectorAll('[data-group-toggle]').forEach(toggle=>toggle.addEventListener('change',()=>{
      const group=toggle.closest('[data-group]');
      group.querySelectorAll('[data-member-toggle]').forEach(member=>{member.checked=toggle.checked;});
      updateGroupCounts();
      displaySource();
    }));
    document.querySelectorAll('[data-member-toggle]').forEach(member=>member.addEventListener('change',()=>{updateGroupCounts();displaySource();}));
  };
  const updateGroupCounts=()=>{
    document.querySelectorAll('[data-group]').forEach(group=>{
      const members=[...group.querySelectorAll('[data-member-toggle]')];
      const checked=members.filter(member=>member.checked).length;
      const toggle=group.querySelector('[data-group-toggle]');
      toggle.checked=checked===members.length;
      toggle.indeterminate=checked>0&&checked<members.length;
      group.querySelector('small').textContent=`${checked}/${members.length} selected`;
    });
  };
  document.querySelector('#imported-measurement-grouping').addEventListener('change',event=>renderMeasurementGroups(event.target.value));
  let polygons=[],pending=[],canvas=null,image=null;
  let displayRevision = 0;
  const displaySource=async()=>{
    const revision = ++displayRevision;
    const ids = selectedFileIds();
    if (key === 'transport') PlotPreferences.write(selectionKey, {fileIds: ids});
    const id=ids[0];
    if(!id){
      document.querySelector('#research-result').innerHTML='<p>Select files to plot their saved analyses together.</p>';
      document.querySelector('#research-history').innerHTML='';
      document.querySelector('#comparison-selections').innerHTML='';
      return;
    }
    if (key === 'transport') {
      const selectedFiles = ids.map(fileId => files.find(file => file.file_id === fileId));
      const loaded = await loadTransportSelection(selectedFiles, researchRequest);
      if (revision !== displayRevision || !form.isConnected) return;
      showTransportResults(loaded.filter(item => item.result).map(item => item.result), loaded.filter(item => !item.result));
      const history = loaded.flatMap(item => item.history.map(run => ({...run, filename: item.file.original_filename})));
      document.querySelector('#research-history').innerHTML=history.map(run=>`<button class="button" type="button" data-result="${escapeHtml(run.processing_id)}">${escapeHtml(run.filename)} · ${escapeHtml(run.processed_at)}</button>`).join('') || '<p>No saved analyses for the selected files.</p>';
      document.querySelectorAll('[data-result]').forEach(button=>button.addEventListener('click',async()=>{
        const historyRevision = ++displayRevision;
        try {
          const result = await researchRequest(`/research/analyses/${encodeURIComponent(button.dataset.result)}`);
          if (historyRevision === displayRevision && form.isConnected) showTransportResults([result]);
        } catch(error) { if (historyRevision === displayRevision) researchMessage(error.message,true); }
      }));
      document.querySelector('#comparison-selections').innerHTML=history.map(run=>`<label class="research-check"><input type="checkbox" value="${escapeHtml(run.processing_id)}"> ${escapeHtml(run.filename)} · ${escapeHtml(run.processed_at)}</label>`).join('');
      return;
    }
    polygons=[];pending=[];
    if(key==='sem'){
      document.querySelector('#research-source').innerHTML='<canvas id="sem-annotation" class="research-image" width="800" height="600" aria-label="Click SEM image to place region vertices"></canvas>';
      canvas=document.querySelector('#sem-annotation');image=new Image();
      image.onload=()=>{canvas.width=image.naturalWidth;canvas.height=image.naturalHeight;canvas.getContext('2d').drawImage(image,0,0);};
      image.onerror=()=>researchMessage('Image preview unavailable; inspect the imported file before annotating.',true);
      image.src=`/research/files/${encodeURIComponent(id)}/image`;
      canvas.addEventListener('click',event=>{
        if(!image.complete||!image.naturalWidth)return;
        const box=canvas.getBoundingClientRect();pending.push([(event.clientX-box.left)*canvas.width/box.width,(event.clientY-box.top)*canvas.height/box.height]);
        const ctx=canvas.getContext('2d');ctx.strokeStyle='#ffed45';ctx.lineWidth=Math.max(1,canvas.width/400);ctx.beginPath();pending.forEach((p,i)=>i?ctx.lineTo(...p):ctx.moveTo(...p));ctx.stroke();
        document.querySelector('#polygon-count').textContent=`${polygons.length} finished regions; ${pending.length} vertices in current region.`;
      });
    }
    try{
      const history=await researchRequest(`/research/files/${id}/analyses`);
      document.querySelector('#research-history').innerHTML=history.map(r=>`<button class="button" type="button" data-result="${r.processing_id}">${escapeHtml(r.processed_at)} · ${escapeHtml(r.model_name)}</button>`).join('')||'<p>No saved result for this file.</p>';
      document.querySelectorAll('[data-result]').forEach(b=>b.addEventListener('click',async()=>{try{showResearchResult(await researchRequest(`/research/analyses/${b.dataset.result}`),key);}catch(e){researchMessage(e.message,true);}}));
      if(history.length)showResearchResult(await researchRequest(`/research/analyses/${history[0].processing_id}`),key);
      else document.querySelector('#research-result').innerHTML='<p>No saved analysis. Review choices and analyze this file.</p>';
      const all=await Promise.all(files.map(f=>researchRequest(`/research/files/${f.file_id}/analyses`).then(r=>r.map(run=>({...run,filename:f.original_filename})))));
      document.querySelector('#comparison-selections').innerHTML=all.flat().filter(r=>r.model_name==='research_'+key).map(r=>`<label class="research-check"><input type="checkbox" value="${r.processing_id}"> ${escapeHtml(r.filename)} · ${escapeHtml(r.processed_at)}</label>`).join('');
    }catch(error){researchMessage(error.message,true);}
  };
  renderMeasurementGroups('sample');
  document.querySelector('#finish-polygon')?.addEventListener('click',()=>{if(pending.length<3){researchMessage('Place at least three corners.',true);return;}polygons.push(pending);pending=[];document.querySelector('#polygon-count').textContent=`${polygons.length} regions ready for the next saved analysis.`;});
  document.querySelector('#clear-polygons')?.addEventListener('click',()=>{polygons=[];pending=[];if(image?.complete)canvas.getContext('2d').drawImage(image,0,0);document.querySelector('#polygon-count').textContent='No draft regions.';});
  bindResearchForm('#research-analyze',async data=>{
    const options={};
    const numeric=['dataset_index','baseline_lambda','baseline_asymmetry','prominence_sigma','reference_sigma','threshold','pixel_size_um','time_scale','temperature_k','length_m','width_m','thickness_m','current_relative_standard_uncertainty','voltage_relative_standard_uncertainty','length_standard_uncertainty_m','width_standard_uncertainty_m','thickness_standard_uncertainty_m'];
    for(const name of numeric)if(data.has(name)&&data.get(name)!=='')options[name]=Number(data.get(name));
    for(const name of ['sheet','axis_unit','level','backward_file_id','lfm_convention','sidecar_file_id','time_column','gate_connection','light_condition','biased_terminal','configuration_notes','sweep_direction'])if(data.get(name))options[name]=data.get(name);
    if(key==='transport') {
      options.units_confirmed=data.has('units_confirmed');
      options.wiring_confirmed=data.has('wiring_confirmed');
      options.measurement_configuration=data.get('measurement_configuration');
      options.geometry_model=data.get('geometry_model');
    }
    if(data.getAll('terminal_roles').length)options.terminal_roles=data.getAll('terminal_roles');
    options.terminal_mapping={};
    for(const channel of ['AI','BI','DrainI','GateI','SourceI']){
      const role=data.get(`terminal_mapping_${channel}`);
      if(role)options.terminal_mapping[channel]=role;
    }
    if(data.get('fit_min')!==null&&data.get('fit_min')!=='' || data.get('fit_max')!==null&&data.get('fit_max')!==''){
      if(data.get('fit_min')===''||data.get('fit_max')==='')throw new Error('Specify both ends of the fit window.');options.fit_window=[Number(data.get('fit_min')),Number(data.get('fit_max'))];
    }
    const tuple=(name,length)=>{const values=data.get(name).split(',').map(v=>Number(v.trim()));if(values.length!==length||values.some(v=>!Number.isFinite(v)))throw new Error(`${name} needs ${length} numbers separated by commas.`);return values;};
    if(data.get('region'))options.region=tuple('region',4);
    if(data.get('exclude_region'))options.exclude_regions=[tuple('exclude_region',4)];
    if(data.get('profile'))options.profiles=[tuple('profile',4)];
    if(data.get('shift'))options.shift_pixels=tuple('shift',2);
    if(key==='sem'){if(pending.length)throw new Error('Finish or clear the current region before saving.');options.polygons=polygons;options.confirm_sidecar_calibration=data.has('confirm_sidecar_calibration');}
    if(key==='afm')options.auto_register=data.has('auto_register');
    if(key==='xrd')options.reference_file_ids=data.getAll('reference_file_ids');
    if(data.get('current'))options.channels=[{current:data.get('current'),voltage:data.get('voltage'),terminal:data.get('terminal'),current_scale:Number(data.get('current_scale')),voltage_scale:Number(data.get('voltage_scale')),polarity:Number(data.get('polarity')),voltage_status:data.get('voltage_status')}];
    for(const i of [2,3])if(data.get('current'+i)){
      options.channels??=[];options.channels.push({current:data.get('current'+i),voltage:data.get('voltage'+i),terminal:data.get('terminal'+i),current_scale:Number(data.get('current_scale')),voltage_scale:Number(data.get('voltage_scale')),polarity:Number(data.get('polarity'+i)),voltage_status:data.get('voltage_status')});
    }
    for(const i of [1,2,3,4])if(data.get('window_label'+i)){
      if(data.get('window_start'+i)===''||data.get('window_end'+i)==='')throw new Error('Specify both times for each named window.');
      options.windows??=[];options.windows.push({label:data.get('window_label'+i),kind:data.get('window_kind'+i),start:Number(data.get('window_start'+i)),end:Number(data.get('window_end'+i))});
    }
    const fileIds=selectedFileIds();
    if(!fileIds.length)throw new Error('Select at least one imported measurement.');
    researchMessage(`Reading ${fileIds.length} verified source${fileIds.length===1?'':'s'} and calculating the saved analyses…`);
    let result;
    for(const fileId of fileIds)result=await researchRequest(`/research/files/${fileId}/analyze`,{technique:key,options});
    await displaySource();
    if (key !== 'transport') showResearchResult(result,key);
    researchMessage(`${fileIds.length} analysis${fileIds.length===1?'':'es'} saved with source hashes and exact choices.`);
  });
  document.querySelector('#research-compare').addEventListener('click',async()=>{
    const comparisonRevision = ++displayRevision;
    try{const ids=[...document.querySelectorAll('#comparison-selections input:checked')].map(e=>e.value);const result=await researchRequest('/research/compare',{processing_ids:ids});
      if (key === 'transport') {
        if (comparisonRevision === displayRevision && form.isConnected) showTransportResults(result.results);
        return;
      }
      document.querySelector('#research-result').innerHTML=`<h3>Control and repeat comparison</h3><p>${escapeHtml(result.warnings.join(' '))}</p>`+result.results.map(r=>`<article><h4>${escapeHtml(r.original_filename)}</h4>${researchJson({summary:r.summary,windows:r.sheets?.flatMap(s=>s.windows),parameters:r.parameters.options,source_sha256:r.source_sha256})}</article>`).join('');
    }catch(error){researchMessage(error.message,true);}
  });
  await displaySource();
}

function renderRamanAnalysis() {
  const query = new URLSearchParams({workspace: 'raman', layout: '20260916-4'});
  const dataset = new URLSearchParams(location.search).get('dataset');
  if (dataset) query.set('dataset', dataset);
  view.innerHTML = pageHeading('Measurement analysis', 'Raman',
    'Review spectra, choose a Raman recipe, inspect peak fits and quality checks, and export a reproducible analysis.',
    '<a class="button" href="/app/research">Research library</a>') +
    `<iframe id="raman-studio" class="raman-studio" title="Raman analysis choices and plot" width="100%" height="900" style="display:block;width:100%;border:0" src="/upload?${escapeHtml(query.toString())}"></iframe>`;
}

window.addEventListener('message', event => {
  const frame = document.querySelector('#raman-studio');
  if (!frame || event.origin !== location.origin || event.source !== frame.contentWindow ||
      event.data?.type !== 'raman-studio-size') return;
  if (Number.isFinite(event.data.height)) frame.style.height = `${Math.max(600, Math.min(30000, event.data.height))}px`;
  frame.classList.toggle('raman-studio-expanded', event.data.expanded === true);
});

function showResearchResult(result,key) {
  const target=document.querySelector('#research-result');
  target.innerHTML=`<h3>${escapeHtml(result.original_filename)}</h3><a class="button primary" href="/research/analyses/${encodeURIComponent(result.processing_id)}/export">Export report and measurements</a><p>${escapeHtml((result.warnings||[]).join(' '))}</p><div class="research-metrics">${Object.entries(result.summary).map(([k,v])=>`<div><small>${escapeHtml(k.replaceAll('_',' '))}</small><strong>${typeof v==='number'?v.toPrecision(6):escapeHtml(v??'Unconfirmed')}</strong></div>`).join('')}</div><div id="research-plot"></div>
    ${result.peaks?`<h4>Peak fits</h4>${researchJson(result.peaks)}${result.reference_matches?.length?'<h4>Reference similarities (not phase fractions)</h4>'+researchJson(result.reference_matches):''}`:''}
    ${result.records?`<h4>Reviewed growth rows</h4>${researchJson(result.records)}`:''}
    ${result.features?`<h4>Region measurements</h4>${researchJson(result.features)}`:''}
    ${result.sheets?`<details><summary>Fit windows and signed diagnostics</summary>${researchJson(result.sheets.map(s=>({sheet:s.name,branches:s.branches.map(b=>({channel:b.channel,direction:b.direction,first_row:b.first_row,last_row:b.last_row,fit:b.fit})),windows:s.windows,diagnostics:s.diagnostics})))}</details><details><summary>Acquisition settings</summary>${researchJson(result.settings)}</details>`:''}
    <details><summary>Calibration, parameters and provenance</summary>${researchJson({calibration:result.calibration,metadata:result.metadata,parameters:result.parameters,source_sha256:result.source_sha256,result_sha256:result.result_sha256})}</details>`;
  if (key === 'transport' && result.sheets) {
    target.querySelector('a.button.primary')?.remove();
    target.insertAdjacentHTML('afterbegin', transportExportControls(result));
    bindTransportExports(target);
    renderTransportPlot(result, target.querySelector('#research-plot'));
    return;
  }
  if ((result.sheets || []).some(sheet => sheet.measurement_type === 'current_time' && sheet.branches.some(branch => !branch.x_unit))) {
    target.insertAdjacentHTML('afterbegin', '<p role="status">This saved analysis predates time-axis support. Click <strong>Analyze and save selected</strong> to generate the I-t plot.</p>');
  }
  if(result.map){
    const width=result.map[0].length,height=result.map.length;
    document.querySelector('#research-plot').innerHTML=`<p>Calibrated map · ${escapeHtml(result.calibration.unit)}. Missing/masked pixels are transparent.</p><canvas id="research-map" class="research-image" width="${width}" height="${height}"></canvas>${result.profiles?.length?researchJson(result.profiles):''}`;
    const values=result.map.flat().filter(v=>v!==null);let min=Infinity,max=-Infinity;for(const v of values){min=Math.min(min,v);max=Math.max(max,v);}
    const ctx=document.querySelector('#research-map').getContext('2d'),image=ctx.createImageData(width,height);
    result.map.flat().forEach((v,i)=>{if(v===null)return;const t=(v-min)/(max-min||1);image.data.set([Math.round(40+215*t),Math.round(80+150*t),Math.round(120-85*t),255],i*4);});ctx.putImageData(image,0,0);
    return;
  }
  const series=result.points?[{label:`Raw · ${result.axis_unit}`,points:result.points.map(p=>[p[0],p[1]])},{label:'Baseline corrected',points:result.points.map(p=>[p[0],p[3]])}]:
    (result.sheets||[]).flatMap(s=>s.branches.map((b,i)=>({
      label:`${s.name} · ${b.channel} · ${b.terminal || 'terminal'}${b.terminal_role ? ` (${b.terminal_role})` : ' (role unknown)'} · ${b.direction} ${i+1}`,
      points:b.points,
      xUnit:b.x_unit || 'V',
      worksheet:s.name,
      channel:b.channel,
      terminal:b.terminal || 'terminal',
      role:b.terminal_role || 'Unknown',
      direction:b.direction,
    })));
  if(!series.length)return;
  const axisUnits = [...new Set(series.map(item => item.xUnit || 'V'))];
  const axisLabel = unit => unit === 's' ? 'Time (s)' : 'Voltage (V)';
  const groupedSeries = new Map();
  series.forEach((item,index) => {
    const group = item.role === 'Unknown' ? item.channel : `${item.channel} (${item.role})`;
    if (!groupedSeries.has(group)) groupedSeries.set(group, []);
    groupedSeries.get(group).push({item,index});
  });
  const seriesMenuHtml = [...groupedSeries.entries()].map(([group,items]) => `<div class="plot-series-group"><label class="plot-series-group-header"><input type="checkbox" data-series-group="${items.map(({index})=>index).join(',')}" checked><strong>${escapeHtml(group)}</strong><small>${items.length}/${items.length} selected</small></label>${items.map(({item,index})=>`<label><input type="checkbox" name="plot_series" value="${index}" checked>${escapeHtml(item.direction)}</label>`).join('')}</div>`).join('');
  const channelActions = [...new Set(series.map(item => item.channel))].map(channel => `<button type="button" class="button compact" data-series-action="channel:${escapeHtml(channel)}">${escapeHtml(channel)}</button>`).join('');
  document.querySelector('#research-plot').innerHTML=`<div class="research-form research-plot-controls">${researchSelect('Horizontal axis','plot_axis',axisUnits.map(unit=>[unit,axisLabel(unit)]))}<div class="plot-series-dropdown"><button type="button" class="button" id="plot-series-toggle" aria-expanded="false">Plot series</button><div id="plot-series-menu" class="plot-series-menu" hidden><div class="plot-series-actions"><button type="button" class="button compact" data-series-action="all">All</button><button type="button" class="button compact" data-series-action="none">Clear</button><button type="button" class="button compact" data-series-action="increasing">Increasing</button><button type="button" class="button compact" data-series-action="decreasing">Decreasing</button>${channelActions}</div>${seriesMenuHtml}</div></div>${researchSelect('Display','plot_mode',[['signed','Signed values'],['absolute','Absolute values'],['log','Absolute values, logarithmic scale']])}</div><div class="research-chart-shell"><canvas id="research-chart" width="900" height="460" class="research-chart"></canvas></div><ul id="research-plot-legend" class="research-plot-legend"></ul><div id="research-chart-range" class="research-chart-properties"></div>`;
  const draw=()=>{
    const selectedIndexes=[...target.querySelectorAll('[name=plot_series]:checked')].map(option=>Number(option.value));
    const mode=target.querySelector('[name=plot_mode]').value;
    const axisUnit=target.querySelector('[name=plot_axis]').value;
    const xLabel=axisLabel(axisUnit);
    const selections=selectedIndexes.map(index=>series[index]).filter(item=>item && (item.xUnit || 'V') === axisUnit);
    const transformed=selections.map(selection=>selection.points.map(([x,y])=>[x,y===null?null:mode==='signed'?y:mode==='absolute'?Math.abs(y):y===0?null:Math.log10(Math.abs(y))]));
    const finite=transformed.flat().filter(p=>p.every(Number.isFinite));
    if(!finite.length){
      const emptyCanvas=document.querySelector('#research-chart');
      emptyCanvas.getContext('2d').clearRect(0,0,emptyCanvas.width,emptyCanvas.height);
      document.querySelector('#research-plot-legend').innerHTML='';
      document.querySelector('#research-chart-range').textContent='No selected data for this axis and display mode.';
      return;
    }
    let xmin=Infinity,xmax=-Infinity,ymin=Infinity,ymax=-Infinity;for(const [x,y]of finite){xmin=Math.min(xmin,x);xmax=Math.max(xmax,x);ymin=Math.min(ymin,y);ymax=Math.max(ymax,y);}
    const canvas=document.querySelector('#research-chart');
    const ctx=canvas.getContext('2d');
    const width=canvas.clientWidth||900,height=460,ratio=window.devicePixelRatio||1;
    canvas.width=width*ratio;canvas.height=height*ratio;ctx.setTransform(ratio,0,0,ratio,0,0);ctx.clearRect(0,0,width,height);
    const frame={left:86,right:width-28,top:28,bottom:height-70};
    const xSpan=xmax-xmin||1,ySpan=ymax-ymin||1;
    const x=value=>frame.left+(value-xmin)/xSpan*(frame.right-frame.left);
    const y=value=>frame.bottom-(value-ymin)/ySpan*(frame.bottom-frame.top);
    ctx.fillStyle='#fff';ctx.fillRect(0,0,width,height);ctx.font='12px system-ui';
    ctx.strokeStyle='#e1e8e4';ctx.fillStyle='#66746f';ctx.lineWidth=1;
    const tickValue=(min,max,index)=>min+(max-min)*index/5;
    for(let index=0;index<=5;index+=1){
      const xv=x(tickValue(xmin,xmax,index)),yv=y(tickValue(ymin,ymax,index));
      ctx.beginPath();ctx.moveTo(xv,frame.top);ctx.lineTo(xv,frame.bottom);ctx.stroke();
      ctx.beginPath();ctx.moveTo(frame.left,yv);ctx.lineTo(frame.right,yv);ctx.stroke();
      ctx.fillText(tickValue(xmin,xmax,index).toPrecision(4),xv-18,frame.bottom+20);
      ctx.fillText(tickValue(ymin,ymax,index).toPrecision(4),8,yv+4);
    }
    ctx.strokeStyle='#71837c';ctx.strokeRect(frame.left,frame.top,frame.right-frame.left,frame.bottom-frame.top);
    ctx.fillStyle='#192522';ctx.textAlign='center';ctx.fillText(xLabel,(frame.left+frame.right)/2,height-20);
    ctx.save();ctx.translate(18,(frame.top+frame.bottom)/2);ctx.rotate(-Math.PI/2);ctx.fillText(mode==='log'?'log10 |Current| (A)':'Current (A)',0,0);ctx.restore();
    const terminalColors=new Map([['Source','#d47534'],['Drain','#3f8f84'],['Gate','#8465a5'],['Unknown','#226e61']]);
    transformed.forEach((points,index)=>{
      const selected=selections[index];
      ctx.strokeStyle=terminalColors.get(selected.role) || terminalColors.get('Unknown');
      ctx.lineWidth=2;ctx.setLineDash(selected.direction === 'decreasing' ? [7, 5] : []);ctx.beginPath();let active=false;
      for(const [valueX,valueY]of points){if(!Number.isFinite(valueX)||!Number.isFinite(valueY)){active=false;continue;}active?ctx.lineTo(x(valueX),y(valueY)):ctx.moveTo(x(valueX),y(valueY));active=true;}ctx.stroke();
    });
    ctx.setLineDash([]);
    document.querySelector('#research-plot-legend').innerHTML=selections.map(selection=>`<li><span class="research-legend-swatch ${selection.direction === 'decreasing' ? 'dashed' : ''}" style="background:${terminalColors.get(selection.role) || terminalColors.get('Unknown')}"></span>${escapeHtml(selection.label)}</li>`).join('');
    document.querySelector('#research-chart-range').innerHTML=`<strong>Axis properties</strong><span>${selections.length} series plotted</span><span>X: ${escapeHtml(xLabel)}, ${xmin.toPrecision(5)} to ${xmax.toPrecision(5)}</span><span>Y: ${mode==='log'?'log10 |Current| (A)':'Current (A)'}, ${ymin.toPrecision(5)} to ${ymax.toPrecision(5)}</span>${mode==='log'?'<span>Zero-current points are omitted in logarithmic mode.</span>':''}<span>Display transforms do not change saved signed measurements.</span>`;
  };
  const seriesToggle=target.querySelector('#plot-series-toggle');
  const seriesMenu=target.querySelector('#plot-series-menu');
  const updateSeriesLabel=()=>{
    const count=target.querySelectorAll('[name=plot_series]:checked').length;
    seriesToggle.textContent=count ? `Plot series (${count} selected)` : 'Plot series (none selected)';
  };
  const updateSeriesGroupStates=()=>{
    target.querySelectorAll('[data-series-group]').forEach(groupToggle=>{
      const indexes=groupToggle.dataset.seriesGroup.split(',').map(Number);
      const members=indexes.map(index=>target.querySelector(`[name="plot_series"][value="${index}"]`));
      const checked=members.filter(member=>member?.checked).length;
      groupToggle.checked=checked===members.length;
      groupToggle.indeterminate=checked>0&&checked<members.length;
      groupToggle.closest('.plot-series-group').querySelector('small').textContent=`${checked}/${members.length} selected`;
    });
  };
  seriesToggle.addEventListener('click',()=>{
    seriesMenu.hidden=!seriesMenu.hidden;
    seriesToggle.setAttribute('aria-expanded',String(!seriesMenu.hidden));
  });
  let closeSeriesMenuTimer;
  const closeSeriesMenu=()=>{
    if(seriesMenu.hidden)return;
    seriesMenu.hidden=true;
    seriesToggle.setAttribute('aria-expanded','false');
  };
  const scheduleSeriesMenuClose=()=>{
    clearTimeout(closeSeriesMenuTimer);
    closeSeriesMenuTimer=setTimeout(closeSeriesMenu,250);
  };
  const keepSeriesMenuOpen=()=>{
    clearTimeout(closeSeriesMenuTimer);
  };
  const seriesDropdown=target.querySelector('.plot-series-dropdown');
  seriesDropdown.addEventListener('pointerleave',scheduleSeriesMenuClose);
  seriesDropdown.addEventListener('pointerenter',keepSeriesMenuOpen);
  seriesToggle.addEventListener('keydown',event=>{
    if(event.key==='Escape')closeSeriesMenu();
  });
  target.querySelectorAll('[data-series-group]').forEach(groupToggle=>groupToggle.addEventListener('change',()=>{
    const checked=groupToggle.checked;
    groupToggle.dataset.seriesGroup.split(',').map(Number).forEach(index=>{
      const member=target.querySelector(`[name="plot_series"][value="${index}"]`);
      if(member)member.checked=checked;
    });
    updateSeriesGroupStates();updateSeriesLabel();draw();
  }));
  target.querySelectorAll('[data-series-action]').forEach(button=>button.addEventListener('click',()=>{
    const action=button.dataset.seriesAction;
    target.querySelectorAll('[name=plot_series]').forEach(input=>{
      const item=series[Number(input.value)];
      const direction=String(item.direction || '').trim().toLowerCase();
      const matchesChannel=action.startsWith('channel:') && String(item.channel)===action.slice(8);
      const matchesDirection=['increasing','decreasing'].includes(action) && direction===action;
      input.checked=action==='all'
        || (action!=='none' && (matchesDirection || matchesChannel));
    });
    updateSeriesGroupStates();updateSeriesLabel();draw();
  }));
  target.querySelectorAll('[name=plot_series]').forEach(input=>input.addEventListener('change',()=>{updateSeriesGroupStates();updateSeriesLabel();draw();}));
  target.querySelector('[name=plot_mode]').addEventListener('change',draw);
  target.querySelector('[name=plot_axis]').addEventListener('change',draw);
  updateSeriesGroupStates();updateSeriesLabel();draw();
}
