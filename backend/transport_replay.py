"""Run from an extracted Transport package: python reproduce.py [--verify-only]."""
import argparse
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import platform
import sys


def verify_files(root):
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    for name, digest in manifest['files'].items():
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError(f'Missing or unsafe package file: {name}')
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError(f'Checksum mismatch: {name}')
    return manifest


def compare(expected, actual, relative=1e-10, absolute=1e-30, path='result'):
    """Compare every field; identifiers and counts are exact, floats use stated tolerances."""
    if isinstance(expected, dict):
        if not isinstance(actual, dict) or set(expected) != set(actual):
            raise ValueError(f'{path}: result fields differ')
        for key in expected:
            compare(expected[key], actual[key], relative, absolute, f'{path}.{key}')
    elif isinstance(expected, list):
        if not isinstance(actual, (list, tuple)) or len(expected) != len(actual):
            raise ValueError(f'{path}: sequence lengths differ')
        for index, (left, right) in enumerate(zip(expected, actual)):
            compare(left, right, relative, absolute, f'{path}[{index}]')
    elif isinstance(expected, float):
        if not isinstance(actual, (float, int)) or isinstance(actual, bool) or not math.isfinite(actual) or not math.isclose(expected, actual, rel_tol=relative, abs_tol=absolute):
            raise ValueError(f'{path}: expected {expected!r}, got {actual!r}')
    elif type(expected) is not type(actual) or expected != actual:
        raise ValueError(f'{path}: expected {expected!r}, got {actual!r}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify-only', action='store_true', help='Verify every packaged checksum without importing scientific dependencies.')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    manifest = verify_files(root)
    if args.verify_only:
        print(f'Verified {len(manifest["files"])} package files.')
        return
    recipe = json.loads((root / 'recipe.json').read_text(encoding='utf-8'))
    for name, expected_version in recipe['environment']['packages'].items():
        try:
            installed = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError as error:
            raise ValueError(f'Missing {name}. Install requirements.txt in a virtual environment.') from error
        if installed != expected_version:
            raise ValueError(f'{name}: expected {expected_version}, found {installed}. Use requirements.txt.')
    if platform.python_version() != recipe['environment']['python']:
        print(f'Python differs: saved {recipe["environment"]["python"]}, running {platform.python_version()}. Numerical comparison remains required.')
    sys.path.insert(0, str(root / 'code'))
    source_paths = {source['role']: root / source['package_path'] for source in recipe['sources']}
    if recipe['technique'] == 'transport':
        import research_transport
        actual = research_transport.analyze(source_paths['measurement'], recipe['options'])
    elif recipe['technique'] == 'photodetector':
        import photodetector
        actual = photodetector.analyze(source_paths['light'], source_paths['dark'], recipe['options'])
    else:
        raise ValueError('Unsupported replay technique.')
    expected = json.loads((root / 'calculation.json').read_text(encoding='utf-8'))
    compare(expected, actual, **recipe['tolerances'])
    destination = root / 'reproduced'
    destination.mkdir(exist_ok=True)
    (destination / 'calculation.json').write_text(json.dumps(actual, indent=2, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    (destination / 'verification.json').write_text(json.dumps({
        'status': 'passed', 'processing_id': recipe['processing_id'],
        'tolerances': recipe['tolerances'], 'verified_files': len(manifest['files']),
        'python': platform.python_version(),
    }, indent=2), encoding='utf-8')
    print('Reproduction passed: all calculation fields match within the declared tolerances.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, ImportError) as error:
        print(f'Reproduction failed: {error}', file=sys.stderr)
        sys.exit(1)
