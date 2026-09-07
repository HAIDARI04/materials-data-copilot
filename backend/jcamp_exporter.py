import hashlib
from datetime import datetime, timezone
from pathlib import Path

import database
import preview

RAW_DATA_DIR = Path(__file__).parent / "data" / "raw"


class JCAMPExportError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def export_jcamp_dx(file_id: str) -> tuple[str, str]:
    """Export processed/raw Raman spectrum as standard IUPAC JCAMP-DX (v4.24) text format."""
    with database.connect_database() as connection:
        cursor = connection.execute(
            f"SELECT {database.IMPORTED_FILE_COLUMNS} FROM imported_files WHERE file_id = ?",
            (file_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise JCAMPExportError(
                404,
                "file_not_found",
                f"Imported file with file_id '{file_id}' was not found.",
            )
        metadata = dict(row)

    try:
        preview_data = preview.build_file_preview(metadata, RAW_DATA_DIR)
    except preview.PreviewError as error:
        raise JCAMPExportError(
            error.status_code,
            error.code,
            error.message,
        ) from error

    raw_points = preview_data["points"]
    if not raw_points:
        raise JCAMPExportError(
            422,
            "no_data_points",
            f"File '{file_id}' contains no valid XY data points.",
        )

    points = [
        {"x": float(pt[0]), "y": float(pt[1])} if isinstance(pt, (tuple, list)) else {"x": float(pt["x"]), "y": float(pt["y"])}
        for pt in raw_points
    ]

    x_vals = [float(pt["x"]) for pt in points]
    y_vals = [float(pt["y"]) for pt in points]

    first_x, last_x = x_vals[0], x_vals[-1]
    min_y, max_y = min(y_vals), max(y_vals)
    n_points = len(points)

    file_sha256 = metadata.get("sha256") or hashlib.sha256(file_bytes).hexdigest()
    now_utc = datetime.now(timezone.utc).strftime("%Y/%m/%d %H:%M:%S")

    jcamp_lines = [
        f"##TITLE={metadata['original_filename']}",
        "##JCAMP-DX=4.24",
        "##DATA TYPE=RAMAN SPECTRUM",
        "##DATA CLASS=XYDATA",
        "##ORIGIN=Materials Data Copilot",
        f"##DATE={now_utc.split()[0]}",
        f"##TIME={now_utc.split()[1]}",
        f"##FILE ID={metadata['file_id']}",
        f"##SHA256={file_sha256}",
        f"##MATERIAL SYSTEM={metadata.get('material_system') or 'Unknown'}",
        f"##SAMPLE ID={metadata.get('sample_id') or 'N/A'}",
        f"##INSTRUMENT={metadata.get('instrument') or 'N/A'}",
        f"##OPERATOR={metadata.get('operator') or 'N/A'}",
        "##XUNITS=1/CM",
        "##YUNITS=ARBITRARY UNITS",
        f"##FIRSTX={first_x:.4f}",
        f"##LASTX={last_x:.4f}",
        f"##MINY={min_y:.4f}",
        f"##MAXY={max_y:.4f}",
        f"##NPOINTS={n_points}",
        "##XYDATA=(X++(Y..Y))",
    ]

    for pt in points:
        jcamp_lines.append(f"{pt['x']:.4f} {pt['y']:.6f}")

    jcamp_lines.append("##END=")

    content = "\n".join(jcamp_lines)
    filename = f"{Path(metadata['original_filename']).stem}_jcamp.dx"
    return content, filename
