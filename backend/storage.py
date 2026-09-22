import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from dotenv import load_dotenv


BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BACKEND_DIR.parent
DEFAULT_LOCAL_DATA_DIR = BACKEND_DIR / "data"

# Keep machine-specific paths out of Git while allowing the same checkout to be
# configured independently on every computer. Shared payloads and the active
# SQLite catalog intentionally have separate roots.
load_dotenv(PROJECT_ROOT / ".env", override=False)


@dataclass(frozen=True)
class StoragePaths:
    local_data_dir: Path
    shared_data_dir: Path
    database_path: Path
    raw_data_dir: Path
    processed_data_dir: Path
    reference_data_dir: Path


def _resolve_path(value: str | None, default: Path) -> Path:
    path = Path(value).expanduser() if value else default
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def resolve_storage_paths(
    environment: Mapping[str, str] | None = None,
) -> StoragePaths:
    environment = os.environ if environment is None else environment
    local_data_dir = _resolve_path(
        environment.get("MDC_LOCAL_DATA_DIR"),
        DEFAULT_LOCAL_DATA_DIR,
    )
    shared_data_dir = _resolve_path(
        environment.get("MDC_SHARED_DATA_DIR"),
        local_data_dir,
    )
    database_path = _resolve_path(
        environment.get("MDC_DATABASE_PATH"),
        local_data_dir / "materials_data_copilot.db",
    )
    return StoragePaths(
        local_data_dir=local_data_dir,
        shared_data_dir=shared_data_dir,
        database_path=database_path,
        raw_data_dir=shared_data_dir / "raw",
        processed_data_dir=shared_data_dir / "processed",
        reference_data_dir=shared_data_dir / "references",
    )


PATHS = resolve_storage_paths()
DATABASE_PATH = PATHS.database_path
RAW_DATA_DIR = PATHS.raw_data_dir
PROCESSED_DATA_DIR = PATHS.processed_data_dir
REFERENCE_DATA_DIR = PATHS.reference_data_dir
