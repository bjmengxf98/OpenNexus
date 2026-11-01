import shutil
import subprocess
from pathlib import Path

import pytest


def test_actual_conversation_navigation_and_background_stream_handlers():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable for the UI contract test")
    result = subprocess.run([node, "test_conversation_browsing.js"], cwd=Path(__file__).parent,
                            capture_output=True, text=True, encoding="utf-8", timeout=25)
    assert result.returncode == 0, result.stdout + result.stderr
