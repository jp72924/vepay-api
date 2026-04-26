import shutil
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def workspace_temp_root(monkeypatch):
    root = Path.cwd() / ".test-tmp"
    root.mkdir(exist_ok=True)
    monkeypatch.setenv("VEPAY_API_TEMP_DIR", str(root))
    yield


@pytest.fixture
def workspace_tmp(request):
    root = Path.cwd() / ".test-tmp" / request.node.name
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)

