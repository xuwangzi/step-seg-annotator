import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS native window test")
def test_macos_native_viewport_starts() -> None:
    project_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(project_root / "src"), environment.get("PYTHONPATH", "")]
    )
    result = subprocess.run(
        [sys.executable, str(project_root / "tests" / "gui_smoke_runner.py")],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
