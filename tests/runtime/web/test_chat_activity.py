"""Execute browser state/reconciliation behavior against the shipped script."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_chat_activity_browser_behavior():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed for the browser behavior checks")
    root = Path(__file__).resolve().parents[3]
    subprocess.run(
        [node, str(Path(__file__).with_name("chat_activity.cjs")),
         str(root / "src/handler/runtime/web/static/app/app.js")],
        check=True, capture_output=True, text=True,
    )
