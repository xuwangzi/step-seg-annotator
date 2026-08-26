import subprocess
import sys
from pathlib import Path

from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
from OCP.IFSelect import IFSelect_RetDone
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

from stepseg.storage import save_document
from stepseg.topology import load_step, new_document


def write_step(path: Path) -> None:
    writer = STEPControl_Writer()
    assert writer.Transfer(BRepPrimAPI_MakeCylinder(10, 5).Shape(), STEPControl_AsIs) == IFSelect_RetDone
    assert writer.Write(str(path)) == IFSelect_RetDone


def run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", "from stepseg.cli import main; main()", *arguments],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_cli_inspect_validate_and_export(tmp_path: Path) -> None:
    source = tmp_path / "part.step"
    write_step(source)
    entities = load_step(source)
    annotation = tmp_path / "part.stepseg.json"
    save_document(annotation, new_document(source, entities))

    inspected = run_cli("inspect", str(source))
    assert inspected.returncode == 0, inspected.stderr
    assert "entity_0001" in inspected.stdout

    validated = run_cli("validate", str(annotation))
    assert validated.returncode == 0, validated.stderr
    assert "valid: 1 entities" in validated.stdout

    output = tmp_path / "exports"
    exported = run_cli(
        "export-solids",
        str(annotation),
        "--output",
        str(output),
    )
    assert exported.returncode == 0, exported.stderr
    assert (output / "combined.step").is_file()
    assert (output / "manifest.json").is_file()
    assert (output / "entities" / "entity_0001.step").is_file()
