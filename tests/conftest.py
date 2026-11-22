import os

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Tests never read or write the real ~/.recallhook (config file, store) or RECALLHOOK_* env."""
    for k in [k for k in os.environ if k.startswith("RECALLHOOK_")]:
        monkeypatch.delenv(k)
    monkeypatch.setenv("RECALLHOOK_HOME", str(tmp_path / "home"))
    return tmp_path / "home"
