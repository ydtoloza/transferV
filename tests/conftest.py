import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Every test gets its own empty SQLite database."""
    monkeypatch.setenv("TRANSFERV_DATA_DIR", str(tmp_path))
    from app import config

    config.data_dir.cache_clear()
    from app import db

    db.init_db()
    yield
    config.data_dir.cache_clear()
    os.environ.pop("TRANSFERV_DATA_DIR", None)
