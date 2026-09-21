import hashlib
import io
import json
import sqlite3
import struct
import zipfile
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image, TiffImagePlugin

import database
import integrity
import main
import research_analysis
import research_microscopy
import research_spectra
import research_store
import research_transport
import transport


@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(database,'DATABASE_PATH',tmp_path/'catalog.db')
    monkeypatch.setattr(main,'RAW_DATA_DIR',tmp_path/'raw')
    monkeypatch.setattr(main,'PROCESSED_DATA_DIR',tmp_path/'processed')
    monkeypatch.setattr(main,'REFERENCE_DATA_DIR',tmp_path/'references')
    database.initialize_database()
    return TestClient(main.app),tmp_path


def assert_raw(record, data, filename, mime):
    # Fresh connection proves the API response and persistent provenance agree.
    with sqlite3.connect(database.DATABASE_PATH) as connection:
        connection.row_factory=sqlite3.Row
        saved=dict(connection.execute('SELECT * FROM imported_files WHERE file_id=?',(record['file_id'],)).fetchone())
    assert saved['original_filename']==filename
    assert saved['content_type']==mime
    assert saved['size_bytes']==len(data)
    assert saved['sha256']==hashlib.sha256(data).hexdigest()
    assert Path(saved['storage_path']).read_bytes()==data
    assert Path(saved['storage_path']).parent==main.RAW_DATA_DIR/record['file_id']
    assert saved['storage_path']==record['storage_path']
    assert saved['imported_at']==record['imported_at']
    assert datetime.fromisoformat(saved['imported_at']).tzinfo is not None


def upload(client, data, name='spectrum.txt', technique='PL', mime='text/plain'):
    response=client.post('/files/import',data={'sample_id':'test-device','technique':technique},files={'file':(name,data,mime)})
    assert response.status_code==201,response.text
    record=response.json()
    assert_raw(record,data,name,mime)
    return record


def workbook(sheets):
    buffer=io.BytesIO()
    ns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    rel='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as archive:
        names=''.join(f'<sheet name="{escape(name)}" sheetId="{i}" r:id="r{i}"/>' for i,name in enumerate(sheets,1))
        archive.writestr('xl/workbook.xml',f'<workbook xmlns="{ns}" xmlns:r="{rel}"><sheets>{names}</sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'+''.join(f'<Relationship Id="r{i}" Type="{rel}/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1,len(sheets)+1))+'</Relationships>')
        for sheet_index,rows in enumerate(sheets.values(),1):
            content=[]
            for row_index,row in enumerate(rows,1):
                cells=[]
                for column,value in enumerate(row):
                    if value is None: continue
                    reference=f'{chr(65+column)}{row_index}'
                    cells.append(f'<c r="{reference}" t="str"><v>{escape(value)}</v></c>' if isinstance(value,str) else f'<c r="{reference}"><v>{value}</v></c>')
                content.append(f'<row r="{row_index}">{"".join(cells)}</row>')
            archive.writestr(f'xl/worksheets/sheet{sheet_index}.xml',f'<worksheet xmlns="{ns}"><sheetData>{"".join(content)}</sheetData></worksheet>')
    return buffer.getvalue()


def park_bytes(z, forward=True, gain=1., unit='nm'):
    header=bytearray(580)
    for offset,fmt,value in [(100,'I',z.shape[1]),(104,'I',z.shape[0]),(128,'I',int(forward)),(140,'d',8.),(148,'d',4.),(220,'d',gain),(228,'d',1.),(348,'I',2)]:
        struct.pack_into('<'+fmt,header,offset,value)
    header[4:4+20]='Topography'.encode('utf-16-le')
    header[244:244+len(unit)*2]=unit.encode('utf-16-le')
    tags=TiffImagePlugin.ImageFileDirectory_v2()
    for key,value in {50432:0x0E031301,50433:0x1000002,50434:np.asarray(z,dtype='<f4').tobytes(),50435:bytes(header)}.items():
        tags[key]=value
    output=io.BytesIO()
    Image.fromarray(np.zeros(z.shape,dtype=np.uint8)).save(output,format='TIFF',tiffinfo=tags)
    return output.getvalue()


def test_hierarchy_aliases_states_and_history(storage):
    client,_=storage
    project=client.post('/research/entities',json={'kind':'project','name':'Study'}).json()
    substrate=client.post('/research/entities',json={'kind':'substrate','name':'S01','parent_id':project['entity_id']}).json()
    device=client.post('/research/entities',json={'kind':'device','name':'D01-01','parent_id':substrate['entity_id'],'aliases':['S01-D01','D-01-01']}).json()
    assert client.post('/research/entities',json={'kind':'device','name':'d-01-01','parent_id':substrate['entity_id']}).status_code==409
    assert client.post('/research/entities',json={'kind':'run','name':'Bad parent','parent_id':project['entity_id']}).status_code==422
    state=client.post('/research/entities',json={'kind':'state','name':'MoS2 only','parent_id':device['entity_id'],'metadata':{'transfer':'planned'}}).json()
    assert client.post(f'/research/entities/{state["entity_id"]}/revisions',json={'metadata':{'transfer':'done'},'reason':'changed'}).status_code==422
    update=client.post(f'/research/entities/{device["entity_id"]}/revisions',json={'metadata':{'illumination':'white light'},'reason':'User correction scoped to this device'})
    assert update.status_code==200
    details=client.get('/research/entities/'+device['entity_id']).json()
    assert len(details['history'])==1
    assert details['metadata']['illumination']=='white light'
    with database.connect_database() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute('DELETE FROM research_entities WHERE entity_id=?',(device['entity_id'],))


def test_reviewed_import_dedup_metadata_and_signature(storage):
    client,root=storage
    folder=root/'sources';folder.mkdir()
    image=io.BytesIO();Image.new('RGB',(3,2)).save(image,format='PNG');data=image.getvalue()
    (folder/'one.jpg').write_bytes(data);(folder/'two.jpg').write_bytes(data)
    entity=client.post('/research/entities',json={'kind':'project','name':'Research'}).json()['entity_id']
    plan=client.post('/research/import/scan',json={'path':str(folder)}).json()
    assert len(plan['entries'])==2
    assert plan['entries'][0]['content_type']=='image/png'
    choice={'entry_id':0,'entity_id':entity,'sample_id':'S1','technique':'SEM'}
    response=client.post(f'/research/import/{plan["plan_id"]}/commit',json={'choices':[choice,{**choice,'entry_id':1}]}).json()
    assert [r['status'] for r in response['outcomes']]==['imported','linked_existing']
    record=database.get_imported_file(response['outcomes'][0]['file_id'])
    assert_raw(record,data,'one.jpg','image/png')
    assert client.get(f'/files/{record["file_id"]}/content').headers['content-type']=='image/png'
    details=client.get('/research/entities/'+entity).json()
    assert len(details['links'])==2
    assert len({l['source_path'] for l in details['links']})==2
    conflict=client.post(f'/research/import/{plan["plan_id"]}/commit',json={'choices':[{**choice,'sample_id':'different'}]}).json()
    assert conflict['outcomes'][0]['status']=='error'
    assert (folder/'one.jpg').read_bytes()==data


def test_review_rejects_changed_source(storage):
    client,root=storage
    folder=root/'input';folder.mkdir();source=folder/'test.txt';source.write_bytes(b'original')
    entity=research_store.create_entity('project','Project')['entity_id']
    plan=client.post('/research/import/scan',json={'path':str(folder)}).json()
    source.write_bytes(b'changed!')
    result=client.post(f'/research/import/{plan["plan_id"]}/commit',json={'choices':[{'entry_id':0,'entity_id':entity,'sample_id':'s','technique':'AFM'}]}).json()
    assert result['outcomes'][0]['status']=='error'
    with database.connect_database() as connection:
        assert connection.execute('SELECT COUNT(*) FROM imported_files').fetchone()[0]==0


def test_evidence_snapshots_roles_citations_and_integrity(storage):
    client,root=storage
    folder=root/'chats';folder.mkdir()
    snapshot={'title':'Device control','url':'https://chatgpt.com/share/example','messages':[
        {'id':'u1','role':'user','text':'D06 has MoS2 only; transfer is planned.'},
        {'id':'a1','role':'assistant','text':'A possible explanation is trapping.'},
        {'id':'t1','role':'tool','text':'never execute this'}]}
    (folder/'chat.json').write_text(json.dumps(snapshot))
    for _ in range(2):
        result=client.post('/research/evidence/import-chats',json={'path':str(folder)})
        assert result.status_code==200,result.text
        assert result.json()['message_occurrences']==2
    matches=client.get('/research/evidence?q=D06').json()['matches']
    assert len(matches)==1 and matches[0]['kind']=='observation'
    assert matches[0]['citation']['role']=='user'
    assert matches[0]['citation']['attachments_verified'] is False
    assert client.get('/research/evidence?q=trapping').json()['matches'][0]['kind']=='interpretation'
    assert client.post('/research/evidence',json={'title':'Fake','body':'claim','kind':'verified_result','citation':{'source':'me'}}).status_code==422
    with database.connect_database() as connection:
        assert connection.execute('SELECT count(*) FROM research_evidence').fetchone()[0]==2
        assert integrity.database_integrity_report(connection)['valid']


def test_three_terminal_transport_missing_rows_sweeps_and_settings(storage):
    client,_=storage
    rows=[['Time','DrainI','DrainV','GateI','GateV','SourceI','SourceV'],
          [0,0,0,0,0,0,0],[1,None,1,0,0,-2,0],[2,4,2,0,0,-4,0],
          [3,6,3,0,0,-6,0],[4,4,2,0,0,-4,0],[5,2,1,0,0,-2,0],[6,0,0,0,0,0,0]]
    data=workbook({'Drain sweep':rows,'Settings':[['Test','Drain sweep'],['Gate','disconnected'],['Hold',10]]})
    record=upload(client,data,'device.xlsx','Transport','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response=client.post(f'/research/files/{record["file_id"]}/analyze',json={'technique':'transport','options':{'fit_window':[0,3],
        'windows':[{'label':'return','kind':'retention','start':3,'end':6}]}})
    assert response.status_code==200,response.text
    result=response.json();sheet=result['sheets'][0]
    fits=[b for b in sheet['branches'] if b['channel']=='DrainI' and b['fit']]
    assert len(fits)==1
    assert fits[0]['direction']=='decreasing'
    assert fits[0]['fit']['resistance_ohm']==pytest.approx(.5)
    assert result['settings'][0]['rows'][1]==['Gate','disconnected']
    assert sheet['windows'][2]['channel']=='DrainI'
    assert_raw(record,data,'device.xlsx','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


def test_transport_uses_active_sweep_for_constant_biased_channel(tmp_path):
    data = workbook({
        'Sweep': [
            ['BI', 'BV', 'AI', 'AV'],
            [3, 0, 1, -1],
            [2, 0, 2, 0],
            [1, 0, 3, 1],
            [2, 0, 4, 0],
            [3, 0, 5, -1],
        ],
    })
    path = tmp_path / 'constant_bias.xlsx'
    path.write_bytes(data)

    result = research_transport.analyze(path, {})

    bi_branches = [
        branch for branch in result['sheets'][0]['branches']
        if branch['channel'] == 'BI'
    ]
    assert [branch['direction'] for branch in bi_branches] == [
        'increasing',
        'decreasing',
    ]
    assert all(branch['voltage'] == 'AV' for branch in bi_branches)


def test_transport_filters_sweep_direction_and_terminal_roles(tmp_path):
    data = workbook({
        'Sweep': [
            ['DrainI', 'DrainV', 'GateI', 'GateV', 'SourceI', 'SourceV'],
            [1, -1, 10, -1, 100, -1],
            [2, 0, 20, 0, 200, 0],
            [3, 1, 30, 1, 300, 1],
            [2, 0, 20, 0, 200, 0],
            [1, -1, 10, -1, 100, -1],
        ],
    })
    path = tmp_path / 'roles.xlsx'
    path.write_bytes(data)

    result = research_transport.analyze(
        path,
        {'sweep_direction': 'decreasing', 'terminal_roles': ['Source', 'Gate']},
    )

    assert {(branch['channel'], branch['direction']) for branch in result['sheets'][0]['branches']} == {
        ('GateI', 'decreasing'),
        ('SourceI', 'decreasing'),
    }


def test_transport_applies_ambiguous_terminal_mapping(tmp_path):
    data = workbook({
        'Sweep': [
            ['BI', 'BV', 'AI', 'AV'],
            [1, 0, 10, -1],
            [2, 0, 20, 1],
        ],
    })
    path = tmp_path / 'mapped.xlsx'
    path.write_bytes(data)

    result = research_transport.analyze(
        path,
        {
            'terminal_mapping': {'AI': 'Drain', 'BI': 'Source'},
            'terminal_roles': ['Source'],
        },
    )

    assert {branch['channel'] for branch in result['sheets'][0]['branches']} == {'BI'}
    assert result['sheets'][0]['branches'][0]['terminal_role'] == 'Source'


def test_drift_is_row_aligned():
    sheet={'name':'Time trace','rows':[['Time','AI'],[0,0],[1,None],[2,4],[3,6]]}
    result=transport._analyze_sheet(sheet)
    assert result['current_drift']['AI']['slope']==pytest.approx(2)
    assert result['current_drift']['AI']['intercept']==pytest.approx(0)


def test_park_gain_orientation_level_mask_profile_and_source_immutability(storage):
    client,_=storage
    yy,xx=np.indices((8,8));z=2*xx+3*yy+4.
    data=park_bytes(z,gain=-2)
    record=upload(client,data,'height.tiff','AFM','image/tiff')
    array,calibration=research_microscopy.park(Path(record['storage_path']))
    assert np.allclose(array,np.flipud(z*-2))
    assert calibration['scan_width_um']==8
    response=client.post(f'/research/files/{record["file_id"]}/analyze',json={'technique':'afm','options':{'level':'plane','exclude_regions':[[0,0,2,2]],'profiles':[[0,4,7,4]]}})
    assert response.status_code==200,response.text
    result=response.json()
    assert result['summary']['sq']<1e-10
    assert result['summary']['point_count']==60
    assert result['map'][0][0] is None
    assert result['profiles'][0]['points'][-1][0]==7
    assert_raw(record,data,'height.tiff','image/tiff')


def test_afm_rejects_preview_and_bad_regions(storage):
    client,_=storage
    out=io.BytesIO();Image.new('RGB',(8,8)).save(out,format='TIFF')
    record=upload(client,out.getvalue(),'preview.tiff','AFM','image/tiff')
    assert client.post(f'/research/files/{record["file_id"]}/analyze',json={'technique':'afm'}).status_code==422
    with pytest.raises(ValueError):research_microscopy.rectangle([0,0,9,9],8,8)


def test_lfm_half_difference_has_correct_factor_and_dependency_hash(storage):
    client,_=storage
    z=np.arange(64,dtype=float).reshape(8,8)
    forward=upload(client,park_bytes(z),'forward.tiff','AFM','image/tiff')
    backward=upload(client,park_bytes(z-4,forward=False),'backward.tiff','AFM','image/tiff')
    result=client.post(f'/research/files/{forward["file_id"]}/analyze',json={'technique':'afm','options':{'level':'none','backward_file_id':backward['file_id'],'lfm_convention':'half_difference'}})
    assert result.status_code==200,result.text
    assert result.json()['summary']['mean']==2
    assert result.json()['parameters']['dependencies'][0]['sha256']==backward['sha256']


def test_spectrum_fit_export_cache_tamper_and_parameter_validation(storage):
    client,_=storage
    x=np.linspace(1.5,2.5,301);y=10+2*x+100*np.exp(-.5*((x-2)/.05)**2)
    data='\n'.join(f'{a:.9f},{b:.9f}' for a,b in zip(x,y)).encode()
    record=upload(client,data)
    url=f'/research/files/{record["file_id"]}/analyze'
    request={'technique':'pl','options':{'axis_unit':'eV'}}
    response=client.post(url,json=request)
    assert response.status_code==200,response.text
    result=response.json()
    assert result['peaks'][0]['center']==pytest.approx(2,abs=.003)
    assert result['peaks'][0]['fwhm']==pytest.approx(.1177,abs=.01)
    assert client.post(url,json=request).json()['cached'] is True
    export=client.get(f'/research/analyses/{result["processing_id"]}/export')
    assert export.status_code==200,export.text
    with zipfile.ZipFile(io.BytesIO(export.content)) as archive:
        manifest=json.loads(archive.read('manifest.json'))
        for name,digest in manifest['files'].items():assert hashlib.sha256(archive.read(name)).hexdigest()==digest
        assert b'Source SHA-256' in archive.read('figure.svg')
    assert client.post(url,json={'technique':'pl','options':{'baseline_asymmetry':1}}).status_code==422
    assert client.post(url,json={'technique':'pl','options':{'fit_window':[2,1]}}).status_code==422
    run=database.get_processing_run(result['processing_id'])
    Path(run['result_path']).write_bytes(b'{}')
    assert client.get(f'/research/analyses/{result["processing_id"]}').status_code==422
    assert client.get(f'/research/analyses/{result["processing_id"]}/export').status_code==422
    assert_raw(record,data,'spectrum.txt','text/plain')


def test_xrd_ras_reference_coefficients_are_not_phase_fractions(storage):
    client,_=storage
    x=np.linspace(10,30,501);y=5+100*np.exp(-.5*((x-20)/.08)**2)
    ras=b'*RAS_HEADER_START\nComment \xff\n*RAS_INT_START\n'+('\n'.join(f'{a} {b} 1' for a,b in zip(x,y))).encode()+b'\n*RAS_INT_END\n'
    record=upload(client,ras,'pattern.ras','XRD','application/octet-stream')
    reference=upload(client,json.dumps({'chemical_formula':'Example','pattern':[[100,[1,0,0],20,4.4]]}).encode(),'reference.json','XRD','application/json')
    response=client.post(f'/research/files/{record["file_id"]}/analyze',json={'technique':'xrd','options':{'reference_file_ids':[reference['file_id']]}})
    assert response.status_code==200,response.text
    result=response.json()
    assert result['axis_unit']=='degree_2theta'
    assert result['reference_matches'][0]['relative_coefficient']==1
    assert any('not quantitative phase fractions' in w for w in result['warnings'])
    assert any('Latin-1' in w for w in result['warnings'])


def test_sem_calibration_polygon_and_date_conflict(storage):
    client,_=storage
    output=io.BytesIO();Image.new('RGB',(100,80)).save(output,format='TIFF')
    image=upload(client,output.getvalue(),'sem.tif','SEM','image/tiff')
    text='$CM_DATE 3/17/2032\n$CM_IMAGE_RES 100x80\n$$SM_MICRON_BAR 20\n$$SM_MICRON_MARKER 10µm\n'
    sidecar=upload(client,text.encode(),'sem.txt','SEM','text/plain')
    response=client.post(f'/research/files/{image["file_id"]}/analyze',json={'technique':'sem','options':{'sidecar_file_id':sidecar['file_id'],'confirm_sidecar_calibration':True,'polygons':[[[0,0],[10,0],[10,10],[0,10]]]}})
    assert response.status_code==200,response.text
    assert response.json()['features'][0]['area_um2']==25
    assert client.get(f'/research/files/{image["file_id"]}/image').headers['content-type']=='image/png'
    direct=research_microscopy.analyze_sem(Path(image['storage_path']),{},Path(sidecar['storage_path']),'SEM/25.12.24/sem.tif')
    assert any('Date conflict' in w for w in direct['warnings'])
    assert direct['summary']['pixel_size_um'] is None


def test_cvd_header_rows_retained(storage):
    client,_=storage
    data=workbook({'Sheet1':[['Growth log'],['Date','Sample','Temperature-C','Note'],['2026-09-01','T13',750,'No transfer yet']]})
    record=upload(client,data,'growth.xlsx','CVD','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response=client.post(f'/research/files/{record["file_id"]}/analyze',json={'technique':'cvd'})
    assert response.status_code==200,response.text
    assert response.json()['records'][0]['row']==3
    assert response.json()['records'][0]['values']['Temperature-C']==750


def test_raw_corruption_blocks_analysis(storage):
    client,_=storage
    data=b'0,1\n1,2\n2,3\n3,4\n4,5\n5,6\n6,7\n7,8'
    record=upload(client,data)
    Path(record['storage_path']).write_bytes(b'changed')
    response=client.post(f'/research/files/{record["file_id"]}/analyze',json={'technique':'pl','options':{'axis_unit':'eV'}})
    assert response.status_code==409
    with database.connect_database() as connection:
        assert connection.execute('SELECT count(*) FROM processing_runs').fetchone()[0]==0


@pytest.mark.parametrize("voltage_columns", [True, False])
def test_transport_time_trace_axis_gaps_scales_and_no_iv_fit(tmp_path, voltage_columns):
    headers = ['Elapsed', 'AI'] + (['AV'] if voltage_columns else [])
    rows = [[0, 1], [1000, 2], [2000, None], [3000, 4], [4000, 5]]
    if voltage_columns:
        rows = [row + [5] for row in rows]
    path = tmp_path / 'time.xlsx'
    path.write_bytes(workbook({'Run2109': [headers, *rows]}))
    result = research_transport.analyze(path, {
        'time_column': 'Elapsed', 'time_scale': .001,
        'sweep_direction': 'decreasing', 'fit_window': [100, 200],
        'channels': [{'current': 'AI', 'voltage': 'AV', 'current_scale': 2,
                      'voltage_scale': 1, 'polarity': -1, 'terminal': 'A'}],
    })
    sheet = result['sheets'][0]
    assert sheet['measurement_type'] == 'current_time'
    assert [b['points'] for b in sheet['branches']] == [
        [[0, -2], [1, -4]], [[3, -8], [4, -10]],
    ]
    assert all(b['x_unit'] == 's' and b['x_column'] == 'Elapsed'
               and b['fit'] is None and b['voltage'] is None
               for b in sheet['branches'])


def test_transport_time_trace_api_export_and_provenance(storage):
    client, _ = storage
    data = workbook({'Run2109': [['Time', 'BI', 'BV', 'AI', 'AV'],
        [0, -1, 0, 1, 5], [1, -2, 0, 2, 5], [2, -3, 0, 3, 5]]})
    mime = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    record = upload(client, data, 'D00-01_5V-5s.xlsx', 'Transport', mime)
    response = client.post(f'/research/files/{record["file_id"]}/analyze',
                           json={'technique': 'transport', 'options': {}})
    assert response.status_code == 200, response.text
    result = response.json()
    assert {b['channel'] for b in result['sheets'][0]['branches']} == {'AI', 'BI'}
    assert all(b['x_unit'] == 's' for b in result['sheets'][0]['branches'])
    exported = client.get(f'/research/analyses/{result["processing_id"]}/export')
    assert exported.status_code == 200
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        svg = archive.read('figure.svg').decode()
        assert 'Time (s)' in svg
        assert 'AI (A)' in svg
        assert 'report.pdf' in archive.namelist()
    assert_raw(record, data, 'D00-01_5V-5s.xlsx', mime)
