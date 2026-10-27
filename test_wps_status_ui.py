import shutil
import subprocess
from pathlib import Path

import pytest


def test_actual_status_and_stream_handlers():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable for the UI contract test")
    completed = subprocess.run([node, "test_wps_status_ui.js"], cwd=Path(__file__).parent,
                               capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert completed.returncode == 0, completed.stdout + completed.stderr
