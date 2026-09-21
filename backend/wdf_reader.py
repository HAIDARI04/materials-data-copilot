import math
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path

MAX_WDF_SPECTRA = 100_000
MAX_WDF_POINTS_PER_SPECTRUM = 10_000_000

DATA_TYPES = {
    0: "Arbitrary",
    1: "Spectral",
    2: "Intensity",
    3: "Spatial X",
    4: "Spatial Y",
    5: "Spatial Z",
    9: "Temperature",
    10: "Pressure",
    11: "Time",
    16: "Checksum",
    17: "Flags",
    18: "Elapsed time",
    19: "Frequency",
}

UNITS = {
    0: ("Arbitrary", None),
    1: ("Raman shift", "cm⁻¹"),
    2: ("Wavenumber", "cm⁻¹"),
    3: ("Wavelength", "nm"),
    4: ("Photon energy", "eV"),
    5: ("Position", "µm"),
    6: ("Intensity", "counts"),
    7: ("Intensity", "electrons"),
    8: ("Position", "mm"),
    9: ("Position", "m"),
    10: ("Temperature", "K"),
    11: ("Pressure", "Pa"),
    12: ("Time", "s"),
    13: ("Time", "ms"),
    14: ("Time", "h"),
    15: ("Time", "days"),
    16: ("Position", "pixels"),
    17: ("Intensity", None),
    18: ("Relative intensity", None),
    19: ("Angle", "°"),
    20: ("Angle", "rad"),
    21: ("Temperature", "°C"),
    22: ("Temperature", "°F"),
    24: ("Time", "UTC"),
    25: ("Time", "µs"),
}

SCAN_TYPES = {
    0: "unspecified",
    1: "static",
    2: "continuous",
    3: "step_repeat",
    4: "filter_scan",
}

MEASUREMENT_TYPES = {
    0: "unspecified",
    1: "single",
    2: "series",
    3: "mapping",
}


class WdfError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _text(value: bytes) -> str | None:
    decoded = value.split(b"\0", 1)[0].decode("utf-8", errors="replace").strip()
    return decoded or None


def _axis_label(data_type: int, unit: int) -> str:
    unit_name, symbol = UNITS.get(unit, (f"Unit {unit}", None))
    type_name = DATA_TYPES.get(data_type, f"Data type {data_type}")
    name = unit_name if unit_name != "Arbitrary" else type_name
    return f"{name} ({symbol})" if symbol else name


def _filetime(value: int) -> str | None:
    if value <= 0:
        return None
    try:
        timestamp = datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(
            microseconds=value / 10
        )
    except (OverflowError, ValueError):
        return None
    return timestamp.isoformat()


def _blocks(raw_bytes: bytes) -> list[dict]:
    blocks = []
    offset = 0
    while offset < len(raw_bytes):
        if len(raw_bytes) - offset < 16:
            raise WdfError(422, "truncated_wdf_block", "The WDF ends inside a block header.")
        name_bytes, block_id, size = struct.unpack_from("<4sIQ", raw_bytes, offset)
        try:
            name = name_bytes.decode("ascii")
        except UnicodeDecodeError as error:
            raise WdfError(
                422,
                "invalid_wdf_block",
                "The WDF contains a non-ASCII block name.",
            ) from error
        if size < 16 or size > len(raw_bytes) - offset:
            raise WdfError(
                422,
                "invalid_wdf_block_size",
                f"WDF block {name!r} has an invalid size.",
            )
        blocks.append(
            {
                "name": name,
                "id": block_id,
                "offset": offset,
                "size": size,
                "payload": memoryview(raw_bytes)[offset + 16 : offset + size],
            }
        )
        offset += size
    if not blocks or blocks[0]["name"] != "WDF1":
        raise WdfError(415, "not_wdf", "The file does not begin with a Renishaw WDF1 header.")
    return blocks


def _first(blocks: list[dict], name: str) -> dict:
    block = next((item for item in blocks if item["name"] == name), None)
    if block is None:
        raise WdfError(
            422,
            "missing_wdf_block",
            f"The WDF does not contain its required {name} block.",
        )
    return block


def _float_values(payload: memoryview, offset: int = 0) -> list[float]:
    byte_count = len(payload) - offset
    if byte_count < 0 or byte_count % 4:
        raise WdfError(
            422,
            "invalid_wdf_numeric_block",
            "A WDF numeric block has an invalid length.",
        )
    values = list(struct.unpack_from(f"<{byte_count // 4}f", payload, offset))
    if any(not math.isfinite(value) for value in values):
        raise WdfError(422, "nonfinite_wdf_data", "The WDF contains non-finite spectral values.")
    return values


def _origins(blocks: list[dict], spectrum_count: int) -> list[dict]:
    block = next((item for item in blocks if item["name"] == "ORGN"), None)
    if block is None:
        return []
    payload = block["payload"]
    if len(payload) < 4:
        raise WdfError(422, "invalid_wdf_origins", "The WDF origin block is truncated.")
    origin_count = struct.unpack_from("<I", payload, 0)[0]
    offset = 4
    origins = []
    for _index in range(origin_count):
        value_bytes = spectrum_count * 8
        if offset + 24 + value_bytes > len(payload):
            raise WdfError(422, "invalid_wdf_origins", "The WDF origin values are truncated.")
        data_type, unit = struct.unpack_from("<II", payload, offset)
        label = _text(bytes(payload[offset + 8 : offset + 24])) or DATA_TYPES.get(
            data_type,
            "Origin",
        )
        offset += 24
        raw_values = struct.unpack_from(f"<{spectrum_count}Q", payload, offset)
        offset += value_bytes
        if unit == 24:
            values = [_filetime(value) for value in raw_values]
        elif data_type in {16, 17}:
            values = list(raw_values)
        else:
            values = [struct.unpack("<d", struct.pack("<Q", value))[0] for value in raw_values]
        origins.append(
            {
                "label": label,
                "data_type": DATA_TYPES.get(data_type, f"Data type {data_type}"),
                "unit": UNITS.get(unit, (f"Unit {unit}", None))[1],
                "values": values,
            }
        )
    return origins


def parse_wdf(raw_bytes: bytes, include_points: bool = False) -> dict:
    blocks = _blocks(raw_bytes)
    header = bytes(blocks[0]["payload"])
    if len(header) < 384:
        raise WdfError(422, "truncated_wdf_header", "The WDF1 metadata header is truncated.")

    point_count = struct.unpack_from("<I", raw_bytes, 60)[0]
    declared_spectra = struct.unpack_from("<Q", raw_bytes, 64)[0]
    collected_spectra = struct.unpack_from("<Q", raw_bytes, 72)[0]
    accumulation_count = struct.unpack_from("<I", raw_bytes, 80)[0]
    application = _text(raw_bytes[96:120]) or "WiRE"
    application_version = ".".join(
        str(value) for value in struct.unpack_from("<4H", raw_bytes, 120) if value
    )
    scan_type_code = struct.unpack_from("<I", raw_bytes, 128)[0]
    measurement_type_code = struct.unpack_from("<I", raw_bytes, 132)[0]
    started_at = _filetime(struct.unpack_from("<Q", raw_bytes, 136)[0])
    ended_at = _filetime(struct.unpack_from("<Q", raw_bytes, 144)[0])
    laser_wavenumber = struct.unpack_from("<f", raw_bytes, 156)[0]
    laser_name = _text(raw_bytes[208:232])
    measurement_title = _text(raw_bytes[240:400])

    if point_count < 2 or point_count > MAX_WDF_POINTS_PER_SPECTRUM:
        raise WdfError(
            422,
            "invalid_wdf_point_count",
            "The WDF point count is outside the supported range.",
        )

    x_block = _first(blocks, "XLST")
    if len(x_block["payload"]) < 8:
        raise WdfError(422, "invalid_wdf_axis", "The WDF X-axis block is truncated.")
    x_data_type, x_unit = struct.unpack_from("<II", x_block["payload"], 0)
    x_values = _float_values(x_block["payload"], 8)
    if len(x_values) < point_count:
        raise WdfError(
            422,
            "invalid_wdf_axis",
            "The WDF X axis has fewer values than each spectrum.",
        )
    x_values = x_values[:point_count]

    data_values = []
    for data_block in (item for item in blocks if item["name"] == "DATA"):
        data_values.extend(_float_values(data_block["payload"]))
    if len(data_values) < point_count or len(data_values) % point_count:
        raise WdfError(
            422,
            "invalid_wdf_data",
            "The WDF spectral data cannot be divided into complete spectra.",
        )
    spectrum_count = len(data_values) // point_count
    if spectrum_count > MAX_WDF_SPECTRA:
        raise WdfError(
            413,
            "too_many_wdf_spectra",
            f"The WDF contains more than {MAX_WDF_SPECTRA:,} spectra.",
        )
    if collected_spectra and spectrum_count != collected_spectra:
        raise WdfError(
            422,
            "wdf_spectrum_count_mismatch",
            "The WDF data count does not match its collected-spectrum metadata.",
        )

    origins = _origins(blocks, spectrum_count)
    x_label = _axis_label(x_data_type, x_unit)
    technique = (
        "Raman spectroscopy"
        if x_unit == 1
        else "Photoluminescence"
        if x_unit in {3, 4}
        else "Spectroscopy"
    )
    datasets = []
    for index in range(spectrum_count):
        coordinates = {
            origin["label"]: origin["values"][index]
            for origin in origins
            if origin["values"][index] is not None
        }
        dataset = {
            "dataset_id": f"spectrum-{index + 1}",
            "dataset_index": index,
            "name": (
                measurement_title
                if spectrum_count == 1 and measurement_title
                else f"{measurement_title or 'Spectrum'} · spectrum {index + 1}"
            ),
            "kind": "spectrum",
            "point_count": point_count,
            "x_label": x_label,
            "y_label": "Intensity (counts)",
            "x_min": min(x_values),
            "x_max": max(x_values),
            "coordinates": coordinates,
        }
        if include_points:
            start = index * point_count
            intensities = data_values[start : start + point_count]
            dataset["points"] = [
                [float(x_value), float(intensity)]
                for x_value, intensity in zip(x_values, intensities)
            ]
        datasets.append(dataset)

    wavelength_nm = 10_000_000 / laser_wavenumber if laser_wavenumber > 0 else None
    return {
        "format": "Renishaw WiRE WDF",
        "application": application,
        "application_version": application_version or None,
        "scan_type": SCAN_TYPES.get(scan_type_code, f"scan_type_{scan_type_code}"),
        "measurement_type": MEASUREMENT_TYPES.get(
            measurement_type_code,
            f"measurement_type_{measurement_type_code}",
        ),
        "measurement_title": measurement_title,
        "laser_name": laser_name,
        "laser_wavelength_nm": wavelength_nm,
        "started_at": started_at,
        "ended_at": ended_at,
        "accumulation_count": accumulation_count,
        "declared_spectrum_count": declared_spectra,
        "spectrum_count": spectrum_count,
        "point_count_per_spectrum": point_count,
        "technique": technique,
        "x_axis": {
            "data_type": DATA_TYPES.get(x_data_type, f"Data type {x_data_type}"),
            "unit": UNITS.get(x_unit, (f"Unit {x_unit}", None))[1],
            "label": x_label,
        },
        "origins": [
            {key: value for key, value in origin.items() if key != "values"}
            for origin in origins
        ],
        "block_inventory": [
            {"name": item["name"], "id": item["id"], "size_bytes": item["size"]}
            for item in blocks
        ],
        "datasets": datasets,
    }


def read_wdf(path: Path, include_points: bool = False) -> dict:
    try:
        raw_bytes = path.read_bytes()
    except OSError as error:
        raise WdfError(409, "wdf_file_unavailable", "The WDF file could not be read.") from error
    return parse_wdf(raw_bytes, include_points)
