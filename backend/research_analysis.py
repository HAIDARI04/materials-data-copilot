"""Immutable multi-technique results and reproducible, source-cited exports."""
import csv
import hashlib
import io
import json
import os
import platform
from importlib.metadata import version
import zipfile
from pathlib import Path
from uuid import uuid4
from xml.sax.saxutils import escape

import database
import preview
import research_microscopy
import research_spectra
import research_store as store
import research_transport
import transport
import photodetector
import transport_provenance
import numpy as np

VERSION = '1.5.0'
ELECTRICAL_VERSION = '2.0.0'


def growth(path):
    sheets,warnings = transport.read_workbook(path)
    records = []
    for sheet in sheets:
        for index,row in enumerate(sheet['rows'][:20]):
            headers = [str(v).strip() if v is not None else '' for v in row]
            if any(h.casefold()=='sample' for h in headers) and any('temperature' in h.casefold() for h in headers):
                for row_index,values in enumerate(sheet['rows'][index+1:],start=index+2):
                    if any(v not in {None,''} for v in values):
                        records.append({'sheet':sheet['name'],'row':row_index,'values':dict(zip(headers,values))})
                break
    if not records:
        raise ValueError('No CVD header with Sample and Temperature columns was found.')
    return {'records':records,'summary':{'growth_record_count':len(records)},'warnings':warnings+[
        'Growth rows are reviewed source records. Link selected rows to growth entities; sample names alone do not establish device identity.']}


def load(run, processed):
    path = Path(run['result_path']).resolve(strict=True)
    expected = processed.resolve()/run['file_id']/run['processing_id']
    if path.parent != expected:
        raise ValueError('Result is outside its managed processing directory.')
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest()!=run['result_sha256']:
        raise ValueError('Saved result checksum does not match.')
    result = json.loads(data)
    if result.get('processing_id')!=run['processing_id'] or result.get('source_sha256')!=run['source_sha256']:
        raise ValueError('Saved result identity does not match its record.')
    return {**result,'result_sha256':run['result_sha256']}


def process(file_id, technique, options, raw, processed):
    metadata = database.get_imported_file(file_id)
    if not metadata:
        raise ValueError('Imported source not found.')
    source,digest = preview.verify_stored_file(metadata,raw)
    dependencies = []
    for identity in list(dict.fromkeys(options.get('reference_file_ids',[])+[i for i in [options.get('backward_file_id'),options.get('sidecar_file_id')] if i])):
        record = database.get_imported_file(identity)
        if not record:
            raise ValueError('Related imported source not found.')
        path,_ = preview.verify_stored_file(record,raw)
        dependencies.append((record,path))
    params = {'options':options,'source_context':{k:metadata.get(k) for k in ['sample_id','technique','substrate','measurement_role','updated_at']},
              'dependencies':[{'file_id':r['file_id'],'sha256':r['sha256']} for r,_ in dependencies]}
    electrical = technique in {'transport', 'photodetector'}
    model_version = ELECTRICAL_VERSION if electrical else VERSION
    implementation = transport_provenance.implementation(technique) if electrical else None
    if electrical:
        params['implementation_sha256'] = implementation['sha256']
    encoded = store.encode(params)
    model = 'research_'+technique
    with database.connect_database() as connection:
        cached = database.find_processing_run(connection,file_id,digest,model,model_version,encoded)
    if cached:
        return {**load(cached,processed),'cached':True}
    paths = {r['file_id']:p for r,p in dependencies}
    if technique=='transport':
        result = research_transport.analyze(source,options)
    elif technique == 'photodetector':
        if len(dependencies) != 1:
            raise ValueError('A photodetector recipe requires one confirmed dark reference.')
        result = photodetector.analyze(source, dependencies[0][1], options)
    elif technique in {'pl','xrd'}:
        result = research_spectra.analyze(source,technique,options,[(r,p) for r,p in dependencies if r['file_id'] in options.get('reference_file_ids',[])])
    elif technique=='afm':
        result = research_microscopy.analyze_afm(source,options,paths.get(options.get('backward_file_id')))
    elif technique=='sem':
        result = research_microscopy.analyze_sem(source,options,paths.get(options.get('sidecar_file_id')),metadata.get('relative_path') or metadata['original_filename'])
    elif technique=='cvd':
        result = growth(source)
    elif technique=='spatial':
        if len(dependencies)!=1 or len(options.get('landmarks',[]))<3:
            raise ValueError('Registration requires one reference file and at least three paired landmarks.')
        points = np.asarray(options['landmarks'],dtype=float)
        design = np.column_stack([points[:,:2],np.ones(len(points))])
        matrix,_,rank,_ = np.linalg.lstsq(design,points[:,2:],rcond=None)
        if rank<3 or np.linalg.matrix_rank(matrix[:2])<2:
            raise ValueError('Landmarks must span a two-dimensional region in both measurements.')
        errors = np.linalg.norm(design@matrix-points[:,2:],axis=1)
        result = {'affine_matrix':matrix.T.tolist(),'landmarks':points.tolist(),'residuals_target_units':errors.tolist(),
                  'summary':{'landmark_count':len(points),'rms_target_units':float(np.sqrt(np.mean(errors**2)))},
                  'warnings':['Coordinates retain the units supplied by the reviewer. A three-landmark fit has no independent validation; use additional landmarks and inspect alignment before assigning material regions.']}
    else:
        raise ValueError('Unknown research analysis technique.')
    if electrical:
        result['calculation_keys'] = sorted(result)
        result['implementation'] = implementation
        result['replay_environment'] = transport_provenance.environment(technique)
        result['sources'] = [transport_provenance.source_record(metadata, 'light' if technique == 'photodetector' else 'measurement', 1)]
        result['sources'].extend(transport_provenance.source_record(record, 'dark' if technique == 'photodetector' else f'reference-{index}', index)
                                 for index, (record, _) in enumerate(dependencies, 2))
    # Verify again before retaining a result, including every reference/companion.
    for record,_ in [(metadata,source),*dependencies]:
        preview.verify_stored_file(record,raw)
    identity = str(uuid4())
    timestamp = store.now()
    result.update(processing_id=identity,file_id=file_id,source_sha256=digest,model_name=model,model_version=model_version,
                  parameters=params,processed_at=timestamp,original_filename=metadata['original_filename'])
    result['environment'] = {'python':platform.python_version(),**{name:version(name) for name in ['numpy','scipy','Pillow','xlrd']}}
    data = store.encode(result).encode('utf-8')
    checksum = hashlib.sha256(data).hexdigest()
    destination = processed.resolve()/file_id/identity/'research-result.json'
    run = {'processing_id':identity,'file_id':file_id,'model_name':model,'model_version':model_version,'parameters_json':encoded,
           'source_sha256':digest,'result_path':str(destination),'result_sha256':checksum,'processed_at':timestamp,
           'summary_json':store.encode(result['summary'])}
    staging = processed.resolve()/'.staging'/f'{identity}.json'
    staging.parent.mkdir(parents=True,exist_ok=True)
    try:
        with staging.open('xb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        with database.connect_database() as connection:
            connection.execute('BEGIN IMMEDIATE')
            cached = database.find_processing_run(connection,file_id,digest,model,model_version,encoded)
            if cached:
                return {**load(cached,processed),'cached':True}
            destination.parent.mkdir(parents=True,exist_ok=False)
            os.replace(staging,destination)
            try:
                database.insert_processing_run(connection,run)
            except Exception:
                destination.unlink(missing_ok=True)
                destination.parent.rmdir()
                raise
    finally:
        staging.unlink(missing_ok=True)
    store.add_evidence(f'{technique.upper()} result: {metadata["original_filename"]}',store.encode(result['summary']),
                       'verified_result',{'processing_id':identity,'file_id':file_id,'source_sha256':digest,'result_sha256':checksum})
    return {**result,'result_sha256':checksum,'cached':False}


def export_result(run, processed, raw):
    result = load(run,processed)
    if result.get('model_name') in {'research_transport', 'research_photodetector'}:
        import transport_exports
        return transport_exports.export_package(run, result, raw)
    for source in [{'file_id':run['file_id'],'sha256':run['source_sha256']},*result['parameters']['dependencies']]:
        record = database.get_imported_file(source['file_id'])
        if not record or record['sha256']!=source['sha256']:
            raise ValueError('Export source provenance mismatch.')
        preview.verify_stored_file(record,raw)
    files = {'result.json':Path(run['result_path']).read_bytes()}
    def table(name, rows):
        output = io.StringIO(newline='')
        writer = csv.writer(output)
        writer.writerows(rows)
        files[name] = output.getvalue().encode('utf-8')
    if 'points' in result:
        table('spectrum.csv',[result['columns'],*result['points']])
    for index,sheet in enumerate(result.get('sheets',[])):
        table(f'worksheet-{index+1}.csv',[sheet['columns'],*[[row.get(k) for k in sheet['columns']] for row in sheet.get('rows',[])]])
    if 'map' in result:
        table('calibrated-map.csv',result['map'])
    for index,profile in enumerate(result.get('profiles',[])):
        table(f'profile-{index+1}.csv',[['distance_um',result['calibration']['unit']],*profile['points']])
    if result.get('records'):
        columns = list(dict.fromkeys(k for r in result['records'] for k in r['values']))
        table('growth.csv',[['sheet','row',*columns],*[[r['sheet'],r['row'],*[r['values'].get(k) for k in columns]] for r in result['records']]])
    series = result.get('points')
    transport_unit = 'V'
    if not series and result.get('sheets'):
        branch = next((b for s in result['sheets'] for b in s['branches'] if len(b['points'])>1),None)
        if branch:
            series = branch['points']
            transport_unit = branch.get('x_unit', 'V')
    if series:
        x = [p[0] for p in series]; y = [p[1] for p in series]
        xmin,xmax,ymin,ymax = min(x),max(x),min(y),max(y)
        coords = ' '.join(f'{60+700*(a-xmin)/(xmax-xmin or 1):.3f},{350-280*(b-ymin)/(ymax-ymin or 1):.3f}' for a,b in zip(x,y))
        unit = result.get('axis_unit',f'{transport_unit}; selected transport channel in A')
        svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="840" height="440" viewBox="0 0 840 440"><rect width="840" height="440" fill="white"/><text x="60" y="30">{escape(result["original_filename"])}</text><path d="M60 60V350H770" fill="none" stroke="black"/><polyline points="{coords}" fill="none" stroke="#27675e"/><text x="60" y="375">x: {xmin:.5g} to {xmax:.5g} ({escape(unit)})</text><text x="60" y="395">y: {ymin:.5g} to {ymax:.5g}; raw spectrum / signed transport branch</text><text x="60" y="420" font-size="10">Source SHA-256: {run["source_sha256"]}</text></svg>'
        files['figure.svg'] = svg.encode()
    files['report.md'] = ('# Saved research result\n\n'+result['original_filename']+'\n\n'+
        '\n'.join(f'- {k}: {v}' for k,v in result['summary'].items())+'\n\n'+
        '\n'.join('- '+w for w in result.get('warnings',[]))+'\n\nSource SHA-256: '+run['source_sha256']+
        '\n\nResult SHA-256: '+run['result_sha256']+'\n\nExact parameters and source dependencies are in result.json.').encode()
    manifest = {'processing_id':run['processing_id'],'files':{name:hashlib.sha256(data).hexdigest() for name,data in files.items()}}
    files['manifest.json'] = store.encode(manifest).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,data in files.items():
            archive.writestr(name,data)
    return buffer.getvalue()
