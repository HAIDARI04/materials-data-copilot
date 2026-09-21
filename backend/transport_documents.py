"""Render one report model as offline HTML, PDF and a readable Excel snapshot."""
import base64
from html import escape
import io
import json
import re

from transport_report_model import number


def html_report(model, figures):
    def cited_text(value):
        text = escape(str(value))
        for ref in model['references']:
            identity = ref.get('id')
            if identity:
                safe = escape(identity, quote=True)
                text = text.replace('[' + safe + ']', f'<a href="#ref-{safe}">[{safe}]</a>')
        return text
    def table(headers, rows):
        return '<div class="table-wrap"><table><thead><tr>' + ''.join('<th>' + escape(str(h)) + '</th>' for h in headers) + '</tr></thead><tbody>' + ''.join('<tr>' + ''.join('<td>' + escape(number(c)) + '</td>' for c in row) + '</tr>' for row in rows) + '</tbody></table></div>'
    parts = ['<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">',
             '<title>' + escape(model['title']) + '</title>',
             '<style>body{font:15px/1.6 system-ui,sans-serif;color:#233d36;background:#f4f7f5;margin:0}main{max-width:1000px;margin:32px auto;background:white;padding:40px}h1,h2{color:#155d4e;line-height:1.25}h1{font-size:32px}h2{font-size:22px;margin-top:38px}p,li,td{overflow-wrap:anywhere}nav{padding:16px;background:#eaf3ef}nav a{display:inline-block;margin-right:18px;color:#155d4e}table{width:100%;border-collapse:collapse;font-size:13px;margin:16px 0}th,td{text-align:left;vertical-align:top;padding:10px;border-bottom:1px solid #d4e1da}th{background:#eaf3ef}tbody tr:nth-child(even){background:#f8faf9}.table-wrap{overflow:auto}figure{margin:24px 0;break-inside:avoid}figure img{width:100%;height:auto}figcaption{font-size:13px;color:#4c6259}a{color:#17654f}code{overflow-wrap:anywhere}.subtitle{font-size:18px;color:#4c6259}li{margin:9px 0}@media(max-width:650px){main{margin:0;padding:20px}h1{font-size:26px}}@media print{@page{size:A4;margin:16mm}body{background:white;font-size:10pt}main{margin:0;padding:0;max-width:none}nav{display:none}h2{break-after:avoid}thead{display:table-header-group}tr{break-inside:avoid}.table-wrap{overflow:visible}a{color:inherit}}</style></head><body><main>',
             '<h1>' + escape(model['title']) + '</h1><p class="subtitle">' + escape(model['subtitle']) + '</p>',
             '<nav aria-label="Report sections"><a href="#results">Results</a><a href="#methods">Methods</a><a href="#figures">Figures</a><a href="#references">References</a><a href="#reproduce">Reproduce</a></nav>']
    for index, section in enumerate(model['sections']):
        identity = 'results' if index == 0 else 'methods' if section['title'] == 'How the data were prepared' else 'reproduce' if section['title'] == 'Reproduce this analysis' else f'section-{index}'
        parts.append(f'<section id="{identity}"><h2>{escape(section["title"])}</h2>')
        parts.extend('<p>' + cited_text(paragraph) + '</p>' for paragraph in section['paragraphs'])
        if 'rows' in section:
            parts.append(table(section['headers'], section['rows']))
        if section.get('steps'):
            parts.append('<ol>' + ''.join('<li>' + cited_text(step) + '</li>' for step in section['steps']) + '</ol>')
        parts.append('</section>')
    parts.append('<section id="figures"><h2>Figures and diagnostics</h2>')
    for spec, content in figures:
        encoded = base64.b64encode(content).decode('ascii')
        parts.append(f'<figure id="figure-{escape(spec["id"])}"><img src="data:image/svg+xml;base64,{encoded}" alt="{escape(spec["title"])}"><figcaption>{escape(spec["id"])}. {escape(spec["caption"])}</figcaption></figure>')
    parts.append('</section><section id="references"><h2>Method references</h2><ul>')
    for ref in model['references']:
        parts.append(f'<li id="ref-{escape(ref.get("id", "reference"))}">[{escape(ref.get("id", "reference"))}] {escape(ref.get("author", ""))}. <a href="{escape(ref["url"], quote=True)}">{escape(ref["title"])}</a>. {escape(ref.get("section", ""))}</li>')
    parts.append('</ul></section></main></body></html>')
    return ''.join(parts).encode('utf-8')


def pdf_report(model, figures):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.platypus import Image, KeepTogether, LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle

    output = io.BytesIO()
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle('ReportBody', fontName='Helvetica', fontSize=9.5, leading=14, spaceAfter=7, splitLongWords=True))
    styles.add(ParagraphStyle('ReportCell', fontName='Helvetica', fontSize=8, leading=11, splitLongWords=True))
    styles.add(ParagraphStyle('ReportTitle', fontName='Helvetica-Bold', fontSize=24, leading=29, textColor=colors.HexColor('#155d4e'), spaceAfter=12))
    styles.add(ParagraphStyle('ReportHeading', fontName='Helvetica-Bold', fontSize=14, leading=18, textColor=colors.HexColor('#155d4e'), spaceBefore=16, spaceAfter=8, keepWithNext=True))
    styles.add(ParagraphStyle('ReportCaption', parent=styles['ReportBody'], fontSize=8, leading=11, textColor=colors.HexColor('#4c6259')))
    width = A4[0] - 88
    document = SimpleDocTemplate(output, pagesize=A4, rightMargin=44, leftMargin=44, topMargin=46, bottomMargin=42,
                                 title=model['title'], author='Materials Data Copilot', pageCompression=1)
    def clean(value):
        # Use a Unicode TrueType font below when available; core-font fallback
        # transliterates mathematical punctuation while preserving scientific units.
        return escape(number(value)).replace('\n', '<br/>')
    # Matplotlib ships DejaVu Sans with broad Unicode coverage alongside figures.
    from matplotlib import get_data_path
    from pathlib import Path
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    for name, filename in [('ReportSans', 'DejaVuSans.ttf'), ('ReportSansBold', 'DejaVuSans-Bold.ttf')]:
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(Path(get_data_path()) / 'fonts' / 'ttf' / filename)))
    for style in ['ReportBody', 'ReportCell', 'ReportCaption']:
        styles[style].fontName = 'ReportSans'
    for style in ['ReportTitle', 'ReportHeading']:
        styles[style].fontName = 'ReportSansBold'
    def p(value, style='ReportBody'):
        return Paragraph(clean(value), styles[style])
    story = [p(model['title'], 'ReportTitle'), p(model['subtitle']), Spacer(1, 8)]
    for section in model['sections']:
        story.append(p(section['title'], 'ReportHeading'))
        story.extend(p(paragraph) for paragraph in section['paragraphs'])
        if 'rows' in section:
            rows = [[p(cell, 'ReportCell') for cell in section['headers']]] + [[p(cell, 'ReportCell') for cell in row] for row in section['rows']]
            count = len(section['headers'])
            proportions = [0.34, .66] if count == 2 else [1 / count] * count
            table = LongTable(rows, colWidths=[width * fraction for fraction in proportions], repeatRows=1, hAlign='LEFT',
                              splitByRow=1, splitInRow=1)
            table.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#eaf3ef')),
                                       ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f7faf8')]),
                                       ('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (0, 0), (-1, -1), 7),
                                       ('RIGHTPADDING', (0, 0), (-1, -1), 7), ('TOPPADDING', (0, 0), (-1, -1), 6),
                                       ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                                       ('LINEBELOW', (0, 0), (-1, 0), .5, colors.HexColor('#b8cec2'))]))
            story.extend([table, Spacer(1, 8)])
        for index, step in enumerate(section.get('steps', []), 1):
            story.append(p(f'{index}. {step}'))
    story.append(p('Figures and diagnostics', 'ReportHeading'))
    for spec, png in figures:
        story.append(KeepTogether([
            p(spec['title'], 'ReportHeading'),
            Image(io.BytesIO(png), width=width, height=width * 5.1 / 8),
            p(spec['id'] + '. ' + spec['caption'], 'ReportCaption'),
        ]))
    story.append(p('Method references', 'ReportHeading'))
    for ref in model['references']:
        story.append(p(f'[{ref.get("id", "reference")}] {ref.get("author", "")}. {ref["title"]}. {ref.get("section", "")}'))
        story.append(Paragraph(f'<link href="{escape(ref["url"], quote=True)}" color="#155d4e">{escape(ref["url"])}</link>', styles['ReportCaption']))
    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont('ReportSans', 7)
        canvas.setFillColor(colors.HexColor('#4c6259'))
        canvas.drawString(44, 23, 'Materials Data Copilot | Saved scientific analysis')
        canvas.drawRightString(A4[0] - 44, 23, f'Page {doc.page}')
        canvas.restoreState()
    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()


def workbook_bytes(result, tables):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    workbook = Workbook()
    overview = workbook.active
    overview.title = 'Results'
    rows = [['Transport analysis - saved results'], [result['original_filename']],
            ['Analysis ID', result['processing_id']], ['Generated from saved analysis', result['processed_at']],
            ['Snapshot', 'Full precision is in CSV/JSON. Edit analysis settings in the app and save a new run to recalculate.'],
            ['Uncertainty', 'Fit-only unless a combined standard uncertainty is explicitly reported. Blank values mean unavailable.'],
            ['Quantity', 'Value']]
    rows.extend([key.replace('_', ' '), value] for key, value in result['summary'].items())
    def populate(sheet, values, header_row=1):
        for row in values:
            sheet.append([json.dumps(cell, ensure_ascii=False) if isinstance(cell, (dict, list)) else cell for cell in row])
        for row in sheet:
            for cell in row:
                if isinstance(cell.value, str):
                    # Original strings are literal cells, never executable formulas.
                    cell.data_type = 's'
                cell.font = Font(name='Calibri', size=11, color='233D36')
                cell.alignment = Alignment(vertical='top', wrap_text=True)
                if isinstance(cell.value, float):
                    cell.number_format = '0.000000E+00'
            sheet.row_dimensions[row[0].row].height = 32
        for cell in sheet[header_row]:
            cell.font = Font(name='Calibri', size=11, bold=True, color='FFFFFF')
            cell.fill = PatternFill('solid', fgColor='18685B')
        for col in range(1, sheet.max_column + 1):
            samples = [str(sheet.cell(row, col).value or '') for row in range(1, min(sheet.max_row, 100) + 1)]
            sheet.column_dimensions[get_column_letter(col)].width = min(48, max(18, max(map(len, samples), default=18) + 2))
        for row in sheet:
            lines = max(sum(max(1, (len(line) + int(sheet.column_dimensions[cell.column_letter].width) - 3) //
                                (int(sheet.column_dimensions[cell.column_letter].width) - 2))
                            for line in str(cell.value or '').split('\n')) for cell in row)
            sheet.row_dimensions[row[0].row].height = min(409, max(32, lines * 16 + 8))
        sheet.freeze_panes = f'A{header_row + 1}'
        sheet.auto_filter.ref = f'A{header_row}:{get_column_letter(sheet.max_column)}{sheet.max_row}'
        sheet.sheet_view.showGridLines = False
        sheet.sheet_properties.pageSetUpPr.fitToPage = True
        sheet.page_setup.orientation = 'landscape'
        sheet.page_setup.fitToWidth = 1
        sheet.page_setup.fitToHeight = 0
        sheet.print_title_rows = f'1:{header_row}'
    populate(overview, rows, 7)
    overview.column_dimensions['A'].width = 38
    overview.column_dimensions['B'].width = 80
    overview.row_dimensions[5].height = 48
    for name, values in sorted(tables.items(), key=lambda item: (item[0] != 'parameters', item[0])):
        if len(values) > 1_048_576 or len(values[0]) > 16_384:
            raise ValueError('This table exceeds Excel limits. Download CSV tables instead.')
        title = re.sub(r'[\\/*?:\[\]]', '-', name)[:31]
        populate(workbook.create_sheet(title), values)
    sources = [['Role', 'Original filename', 'Package path', 'SHA-256', 'Bytes', 'Imported at']]
    sources.extend([s['role'], s['original_filename'], s['package_path'], s['sha256'], s['size_bytes'], s['imported_at']] for s in result['sources'])
    populate(workbook.create_sheet('Sources'), sources)
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()
