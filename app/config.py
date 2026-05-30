from functools import lru_cache
from pathlib import Path
import os


@lru_cache
def data_dir() -> Path:
    path = Path(os.getenv("TRANSFERV_DATA_DIR", "./data")).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def database_path() -> Path:
    return data_dir() / "transferv.db"

