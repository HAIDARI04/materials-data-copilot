"""Research routes. State changes retain provenance and explicit review choices."""
import hashlib
import io
import json
import sqlite3
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field, model_validator
from starlette.datastructures import Headers

import database
import preview
import research_analysis as analysis
import research_import as importer
import research_store as store
import transport
import photodetector
import wdf_reader


class ResearchRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def guarded(request):
            try:
                return await handler(request)
            except (preview.PreviewError, transport.TransportError, wdf_reader.WdfError) as error:
                raise HTTPException(error.status_code, detail={'message':error.message,'code':error.code}) from error
            except sqlite3.IntegrityError as error:
                raise HTTPException(409, detail='The alias, relationship or history constraint conflicts with an existing record.') from error
            except (ValueError, OSError, KeyError) as error:
                raise HTTPException(422, detail=str(error)) from error
        return guarded


router = APIRouter(route_class=ResearchRoute)


class StrictModel(BaseModel):
    model_config = {'extra':'forbid','allow_inf_nan':False}


class Entity(StrictModel):
    kind: Literal['project','substrate','device','state','run','region','growth']
    name: str = Field(min_length=1,max_length=200)
    parent_id: str | None = None
    aliases: list[str] = Field(default_factory=list,max_length=50)
    metadata: dict = Field(default_factory=dict)


class Revision(StrictModel):
    metadata: dict
    reason: str = Field(min_length=1,max_length=2000)


class Link(StrictModel):
    entity_id: str
    file_id: str
    selector: str = Field(default='',max_length=1000)
    role: Literal['raw','derived','reference','preview','control','protocol'] = 'raw'
    source_path: str = Field(default='',max_length=2000)


class Evidence(StrictModel):
    title: str = Field(min_length=1,max_length=500)
    body: str = Field(min_length=1,max_length=2_000_000)
    kind: Literal['observation','correction','hypothesis','plan','interpretation','reference']
    citation: dict
    entity_id: str | None = None


class Folder(StrictModel):
    path: str = Field(min_length=1,max_length=2000)


class DocumentEvidence(StrictModel):
    entity_id: str | None = None
    kind: Literal['observation','correction','hypothesis','plan','interpretation','reference'] = 'reference'


class ImportChoice(StrictModel):
    entry_id: int = Field(ge=0)
    entity_id: str
    sample_id: str = Field(min_length=1,max_length=100)
    technique: str = Field(min_length=1,max_length=100)
    material_system: str = 'Unknown'
    role: Literal['raw','derived','reference','preview','control','protocol'] = 'raw'
    selector: str = Field(default='',max_length=1000)
    acknowledge_existing_metadata: bool = False


class Commit(StrictModel):
    choices: list[ImportChoice] = Field(min_length=1,max_length=200)


class Channel(StrictModel):
    current: str
    voltage: str
    terminal: str = 'unknown'
    current_scale: float = Field(default=1,gt=0)
    voltage_scale: float = Field(default=1,gt=0)
    polarity: Literal[-1,1] = 1
    voltage_status: Literal['measured','programmed','unknown'] = 'unknown'


class Window(StrictModel):
    label: str = Field(min_length=1,max_length=100)
    kind: Literal['prebias','light_on','light_off','retention','control','baseline']
    start: float
    end: float

    @model_validator(mode='after')
    def ordered(self):
        if self.end <= self.start:
            raise ValueError('Window end must exceed start.')
        return self


class Options(StrictModel):
    sheet: str | None = None
    channels: list[Channel] = Field(default_factory=list,max_length=8)
    sweep_direction: Literal['increasing','decreasing','dual'] = 'dual'
    terminal_roles: list[Literal['Source','Drain','Gate','instrument']] = Field(
        default_factory=list,
        max_length=4,
    )
    terminal_mapping: dict[str, Literal['Source','Drain','Gate','Unknown']] = Field(
        default_factory=dict,
        max_length=8,
    )
    time_column: str = 'Time'
    time_scale: float = Field(default=1,gt=0)
    windows: list[Window] = Field(default_factory=list,max_length=100)
    fit_window: tuple[float,float] | None = None
    baseline_lambda: float = Field(default=1e7,ge=1,le=1e12)
    baseline_asymmetry: float = Field(default=.001,gt=0,lt=.5)
    prominence_sigma: float = Field(default=5,gt=0,le=100)
    axis_unit: Literal['eV','nm'] | None = None
    dataset_index: int = Field(default=0,ge=0)
    reference_file_ids: list[str] = Field(default_factory=list,max_length=40)
    reference_sigma: float = Field(default=.08,gt=0,le=5)
    level: Literal['none','plane','line_median'] = 'plane'
    region: tuple[int,int,int,int] | None = None
    exclude_regions: list[tuple[int,int,int,int]] = Field(default_factory=list,max_length=200)
    profiles: list[tuple[float,float,float,float]] = Field(default_factory=list,max_length=50)
    threshold: float | None = None
    minimum_feature_pixels: int = Field(default=4,ge=1)
    backward_file_id: str | None = None
    shift_pixels: tuple[int,int] = (0,0)
    auto_register: bool = False
    lfm_convention: Literal['difference','half_difference'] = 'difference'
    sidecar_file_id: str | None = None
    pixel_size_um: float | None = Field(default=None,gt=0)
    confirm_sidecar_calibration: bool = False
    polygons: list[list[tuple[float,float]]] = Field(default_factory=list,max_length=200)
    landmarks: list[tuple[float,float,float,float]] = Field(default_factory=list,max_length=100)
    gate_connection: Literal['unknown','zero_bias','floating','disconnected','previously_connected'] = 'unknown'
    light_condition: Literal['unknown','dark','white_light','controlled_pulses'] = 'unknown'
    biased_terminal: str = Field(default='unknown',max_length=100)
    configuration_notes: str = Field(default='',max_length=4000)
    units_confirmed: bool = False
    wiring_confirmed: bool = False
    measurement_configuration: Literal['unknown', 'two_terminal', 'four_terminal'] = 'unknown'
    geometry_model: Literal['none', 'rectangular_bar'] = 'none'
    temperature_k: float | None = Field(default=None, gt=0, le=5000)
    length_m: float | None = Field(default=None, gt=0)
    width_m: float | None = Field(default=None, gt=0)
    thickness_m: float | None = Field(default=None, gt=0)
    current_relative_standard_uncertainty: float | None = Field(default=None, ge=0, le=1)
    voltage_relative_standard_uncertainty: float | None = Field(default=None, ge=0, le=1)
    length_standard_uncertainty_m: float | None = Field(default=None, ge=0)
    width_standard_uncertainty_m: float | None = Field(default=None, ge=0)
    thickness_standard_uncertainty_m: float | None = Field(default=None, ge=0)

    @model_validator(mode='after')
    def valid_window(self):
        if self.fit_window and self.fit_window[1]<=self.fit_window[0]:
            raise ValueError('Fit window must increase.')
        if sum(len(p) for p in self.polygons)>10000:
            raise ValueError('At most 10,000 annotation vertices are supported.')
        return self


class Analyze(StrictModel):
    technique: Literal['transport','pl','xrd','afm','sem','cvd','spatial']
    options: Options = Field(default_factory=Options)


@router.get('/assets/research.js')
def script():
    return FileResponse(Path(__file__).parent/'static'/'research.js',media_type='text/javascript')


@router.get('/assets/raman_workspace.js')
def raman_workspace_script():
    return FileResponse(Path(__file__).parent / 'static' / 'raman_workspace.js', media_type='text/javascript')


@router.get('/assets/raman_workspace.css')
def raman_workspace_style():
    return FileResponse(Path(__file__).parent / 'static' / 'raman_workspace.css', media_type='text/css')


@router.get('/assets/transport_plot.js')
def transport_plot_script():
    return FileResponse(
        Path(__file__).parent / 'static' / 'transport_plot.js',
        media_type='text/javascript',
    )


@router.get('/assets/plot_preferences.js')
def plot_preferences_script():
    return FileResponse(
        Path(__file__).parent / 'static' / 'plot_preferences.js',
        media_type='text/javascript',
    )


@router.get('/assets/transport_exports.js')
def transport_export_script():
    return FileResponse(Path(__file__).parent / 'static' / 'transport_exports.js', media_type='text/javascript')


@router.get('/assets/photodetector.js')
def photodetector_script():
    return FileResponse(Path(__file__).parent / 'static' / 'photodetector.js', media_type='text/javascript')


@router.get('/research/photodetector/suggestions')
def photodetector_suggestions():
    with database.connect_database() as connection:
        records = [dict(row) for row in connection.execute('SELECT * FROM imported_files')]
    return {'pairs': photodetector.suggest_pairs(records)}


@router.get('/research/photodetector/files/{file_id}/traces')
def photodetector_traces(file_id: str):
    import main
    record = database.get_imported_file(file_id)
    if not record or 'transport' not in str(record.get('technique', '')).lower():
        raise ValueError('Choose an imported Transport workbook.')
    path, digest = preview.verify_stored_file(record, main.RAW_DATA_DIR)
    traces, warnings = photodetector.read_traces(path)
    return {'file_id': file_id, 'sha256': digest, 'warnings': warnings,
            'traces': [{key: value for key, value in trace.items() if key != 'points'} for trace in traces]}


@router.post('/research/photodetector/analyze')
def photodetector_recipe(request: photodetector.Recipe):
    import main
    for identity in [request.dark_file_id, request.light_file_id]:
        record = database.get_imported_file(identity)
        if not record or 'transport' not in str(record.get('technique', '')).lower():
            raise ValueError('Both sources must be imported Transport workbooks.')
    options = request.model_dump()
    options['reference_file_ids'] = [request.dark_file_id]
    return analysis.process(request.light_file_id, 'photodetector', options, main.RAW_DATA_DIR, main.PROCESSED_DATA_DIR)


@router.get('/research/photodetector/history')
def photodetector_history():
    with database.connect_database() as connection:
        return [dict(row) for row in connection.execute(
            "SELECT p.processing_id, p.processed_at, f.original_filename FROM processing_runs p "
            "JOIN imported_files f ON p.file_id=f.file_id WHERE p.model_name='research_photodetector' "
            "ORDER BY p.processed_at DESC LIMIT 50")]


def verified_photodetector_result(processing_id):
    import main
    run = database.get_processing_run(processing_id)
    if not run or run['model_name'] != 'research_photodetector':
        raise ValueError('Saved photodetector analysis not found.')
    result = analysis.load(run, main.PROCESSED_DATA_DIR)
    for source in result['sources']:
        record = database.get_imported_file(source['file_id'])
        if not record or record['sha256'] != source['sha256']:
            raise ValueError('Source provenance no longer matches this analysis.')
        preview.verify_stored_file(record, main.RAW_DATA_DIR)
    return result


@router.get('/research/photodetector/{processing_id}/report')
def photodetector_report(processing_id: str):
    import photodetector_reports
    result = verified_photodetector_result(processing_id)
    if result.get('implementation'):
        import transport_exports
        return Response(transport_exports.asset_bytes(result, 'report.html'), media_type='text/html',
                        headers=export_headers('report.html', inline=True))
    return Response(photodetector_reports.report_html(result), media_type='text/html')


@router.get('/research/photodetector/{processing_id}/figures')
def photodetector_figures(processing_id: str):
    import photodetector_reports
    return photodetector_reports.figures(verified_photodetector_result(processing_id))


class PhotoQuestion(StrictModel):
    processing_id: str
    question: str = Field(min_length=1, max_length=2000)


@router.post('/research/photodetector/explain')
def photodetector_explain(request: PhotoQuestion):
    result = verified_photodetector_result(request.processing_id)
    return {'answer': photodetector.explain(result, request.question),
            'processing_id': request.processing_id, 'references': result['references'],
            'mode': 'source-grounded local guide'}


@router.get('/research/entities')
def entities():
    return store.list_entities()


@router.post('/research/entities',status_code=201)
def create_entity(request: Entity):
    return store.create_entity(**request.model_dump())


@router.get('/research/entities/{identity}')
def entity(identity: str):
    return store.detail(identity)


@router.post('/research/entities/{identity}/revisions')
def revision(identity: str,request: Revision):
    return store.revise(identity,**request.model_dump())


@router.post('/research/links',status_code=201)
def link(request: Link):
    store.add_link(**request.model_dump())
    return {'linked':True}


@router.post('/research/evidence',status_code=201)
def evidence(request: Evidence):
    return {'evidence_id':store.add_evidence(**request.model_dump())}


@router.get('/research/evidence')
def search(q: str = Query(min_length=1,max_length=500),entity_id: str | None = None):
    return {'matches':store.search(q,entity_id),'method':'Local source retrieval; excerpts are evidence, not generated conclusions.'}


@router.post('/research/evidence/import-chats')
def import_chats(request: Folder):
    return importer.import_chats(request.path)


@router.post('/research/files/{file_id}/evidence')
def index_document(file_id: str,request: DocumentEvidence):
    import main
    record = database.get_imported_file(file_id)
    if not record:
        raise HTTPException(404,'Imported source not found.')
    path,_ = preview.verify_stored_file(record,main.RAW_DATA_DIR)
    return importer.index_document(record,path,request.kind,request.entity_id)


@router.get('/research/sources/{source_id}')
def source_snapshot(source_id: str):
    with database.connect_database() as connection:
        source = connection.execute('SELECT * FROM research_sources WHERE source_id=?',(source_id,)).fetchone()
    if not source:
        raise HTTPException(404,'Source snapshot not found.')
    if hashlib.sha256(source['payload']).hexdigest()!=source_id:
        raise ValueError('Source snapshot checksum does not match.')
    return Response(source['payload'],media_type=source['content_type'],headers={'Content-Disposition':f'attachment; filename="source-{source_id}.json"','ETag':f'"{source_id}"'})


@router.post('/research/import/scan')
def scan(request: Folder):
    return importer.scan(request.path)


@router.post('/research/import/{plan_id}/commit')
async def commit(plan_id: str,request: Commit):
    import main
    outcomes = []
    for choice in request.choices:
        try:
            store.detail(choice.entity_id)
            path,entry = importer.plan_source(plan_id,choice.entry_id)
            with path.open('rb') as source:
                data = source.read(main.MAX_FILE_SIZE_BYTES+1)
            if len(data)!=entry['size_bytes'] or hashlib.sha256(data).hexdigest()!=entry['sha256']:
                raise ValueError('Source changed since review. Scan the folder again.')
            with database.connect_database() as connection:
                existing = database.find_imported_file_by_sha256(connection,entry['sha256'])
            if existing:
                preview.verify_stored_file(existing,main.RAW_DATA_DIR)
                if any(existing[k]!=getattr(choice,k) for k in ['sample_id','technique','material_system']) and not choice.acknowledge_existing_metadata:
                    raise ValueError('Identical stored bytes have different metadata. Explicitly acknowledge keeping that original metadata and adding this association.')
                record = existing
            else:
                upload = UploadFile(io.BytesIO(data),filename=path.name,headers=Headers({'content-type':entry['content_type']}))
                try:
                    record = await main.import_file(file=upload,relative_path=entry['relative_path'],technique=choice.technique,
                        material_system=choice.material_system,sample_id=choice.sample_id,measurement_date=None,instrument=None,
                        operator=None,notes=None,substrate='unknown',measurement_role='unspecified',data_category='raw_measurement')
                finally:
                    await upload.close()
            store.add_link(choice.entity_id,record['file_id'],choice.selector,choice.role,str(path))
            outcomes.append({'entry_id':choice.entry_id,'file_id':record['file_id'],'status':'linked_existing' if existing else 'imported'})
        except (ValueError,OSError,sqlite3.IntegrityError,HTTPException,preview.PreviewError) as error:
            outcomes.append({'entry_id':choice.entry_id,'status':'error','message':str(getattr(error,'detail',error))})
    return {'outcomes':outcomes}


@router.post('/research/files/{file_id}/analyze')
def analyze(file_id: str,request: Analyze):
    import main
    return analysis.process(file_id,request.technique,request.options.model_dump(exclude_none=True),main.RAW_DATA_DIR,main.PROCESSED_DATA_DIR)


@router.get('/research/files/{file_id}/analyses')
def history(file_id: str):
    from research_analysis import VERSION
    with database.connect_database() as connection:
        return [dict(r) for r in connection.execute(
            'SELECT processing_id,model_name,model_version,processed_at,source_sha256,result_sha256,summary_json '
            "FROM processing_runs WHERE file_id=? AND model_name LIKE ? AND (model_version=? OR model_name IN ('research_transport','research_photodetector')) ORDER BY processed_at DESC",
            (file_id, 'research_%', VERSION),
        )]


@router.get('/research/files/{file_id}/image')
def image_preview(file_id: str):
    from PIL import Image
    import main
    record = database.get_imported_file(file_id)
    if not record:
        raise HTTPException(404,'Imported image not found.')
    path,_ = preview.verify_stored_file(record,main.RAW_DATA_DIR)
    with Image.open(path) as image:
        if image.width*image.height>40_000_000:
            raise ValueError('Image exceeds 40 million pixels.')
        output = io.BytesIO()
        image.convert('RGB').save(output,format='PNG')
    return Response(output.getvalue(),media_type='image/png',headers={'Cache-Control':'no-store'})


@router.get('/research/analyses/{identity}')
def result(identity: str):
    import main
    run = database.get_processing_run(identity)
    if not run or not run['model_name'].startswith('research_'):
        raise HTTPException(404,'Research result not found.')
    return analysis.load(run,main.PROCESSED_DATA_DIR)


@router.get('/research/analyses/{identity}/export')
def export(identity: str, format: Literal['complete', 'report', 'figures'] = 'complete'):
    import main
    run = database.get_processing_run(identity)
    if not run or not run['model_name'].startswith('research_'):
        raise HTTPException(404,'Research result not found.')
    if run['model_name'] in {'research_transport', 'research_photodetector'}:
        import transport_exports
        result = analysis.load(run, main.PROCESSED_DATA_DIR)
        transport_exports.verify_result_sources(result, main.RAW_DATA_DIR)
        if format == 'report':
            return Response(transport_exports.asset_bytes(result, 'report.pdf'), media_type='application/pdf',
                            headers=export_headers(f'transport-{identity}.pdf'))
        data = transport_exports.export_package(run, result, main.RAW_DATA_DIR, format)
    else:
        if format != 'complete':
            raise ValueError('This export choice is available for Transport analyses.')
        data = analysis.export_result(run, main.PROCESSED_DATA_DIR, main.RAW_DATA_DIR)
    return Response(data, media_type='application/zip', headers=export_headers(f'research-{identity}.zip'))


def export_headers(filename, inline=False):
    return {'Content-Disposition': f'{"inline" if inline else "attachment"}; filename="{filename}"',
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'Content-Security-Policy': "default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'self'"}


def electrical_export_result(identity):
    import main
    import transport_exports
    run = database.get_processing_run(identity)
    if not run or run['model_name'] not in {'research_transport', 'research_photodetector'}:
        raise HTTPException(404, 'Saved Transport analysis not found.')
    result = analysis.load(run, main.PROCESSED_DATA_DIR)
    transport_exports.verify_result_sources(result, main.RAW_DATA_DIR)
    return result


@router.get('/research/analyses/{identity}/export/files')
def export_files(identity: str):
    import transport_exports
    result = electrical_export_result(identity)
    return {'processing_id': identity, 'files': transport_exports.catalog(result)}


@router.get('/research/analyses/{identity}/export/file')
def export_file(identity: str, name: str = Query(max_length=180), inline: bool = False):
    import transport_exports
    result = electrical_export_result(identity)
    assets = {asset['name']: asset for asset in transport_exports.catalog(result)}
    if name not in assets:
        raise HTTPException(404, 'Export file not found.')
    return Response(transport_exports.asset_bytes(result, name), media_type=assets[name]['media_type'],
                    headers=export_headers(Path(name).name, inline=inline and name in {'report.html', 'report.pdf'}))


class Compare(StrictModel):
    processing_ids: list[str] = Field(min_length=2,max_length=20)


@router.post('/research/compare')
def compare(request: Compare):
    import main
    results = []
    for identity in request.processing_ids:
        run = database.get_processing_run(identity)
        if not run or not run['model_name'].startswith('research_'):
            raise ValueError('Research result not found.')
        results.append(analysis.load(run,main.PROCESSED_DATA_DIR))
    kinds = {r['model_name'] for r in results}
    if len(kinds)!=1:
        raise ValueError('Compare results from the same measurement technique.')
    return {'results':results,'warnings':['Controls and acquisition conditions require review. Side-by-side values do not establish causation.']}
