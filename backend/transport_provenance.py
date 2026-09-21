"""Freeze the implementation and environment used by electrical analysis runs."""
import hashlib
from importlib.metadata import requires, version
from pathlib import Path
import platform

from packaging.requirements import Requirement


MODULES = ['transport.py', 'research_transport.py', 'transport_methods.py', 'photodetector.py']
# Capture at module import, alongside the imported analyzers. Later exports never
# substitute the current source tree for the implementation retained by a run.
SOURCES = {name: (Path(__file__).parent / name).read_text(encoding='utf-8') for name in MODULES}
REPLAY = (Path(__file__).parent / 'transport_replay.py').read_text(encoding='utf-8')


def implementation(technique):
    names = MODULES if technique == 'photodetector' else MODULES[:-1]
    files = {name: SOURCES[name] for name in names}
    digests = {name: hashlib.sha256(source.encode('utf-8')).hexdigest() for name, source in files.items()}
    digest = hashlib.sha256(''.join(f'{name}:{sha}\n' for name, sha in sorted(digests.items())).encode()).hexdigest()
    return {'sha256': digest, 'files': files, 'file_sha256': digests, 'replay': REPLAY,
            'replay_sha256': hashlib.sha256(REPLAY.encode()).hexdigest()}


def environment(technique):
    pending = ['numpy', 'scipy', 'xlrd'] + (['pydantic'] if technique == 'photodetector' else [])
    packages = {}
    while pending:
        name = pending.pop()
        if name in packages:
            continue
        packages[name] = version(name)
        for spec in requires(name) or []:
            dependency = Requirement(spec)
            if dependency.marker is None or dependency.marker.evaluate({'extra': ''}):
                pending.append(dependency.name)
    return {'python': platform.python_version(), 'platform': platform.platform(),
            'packages': dict(sorted(packages.items()))}


def source_record(record, role, index):
    suffix = Path(record['original_filename']).suffix.lower()
    return {**{key: record.get(key) for key in (
        'file_id', 'original_filename', 'sha256', 'size_bytes', 'content_type',
        'sample_id', 'technique', 'imported_at', 'measurement_date', 'instrument',
        'substrate', 'material_system', 'measurement_role')},
        'role': role, 'package_path': f'sources/source-{index}{suffix}'}
