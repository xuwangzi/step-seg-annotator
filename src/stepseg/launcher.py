"""GUI launcher that prepares the macOS Qt plugin before importing Qt."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from hashlib import sha256
from pathlib import Path


def _prepare_macos_qt_platform_plugin() -> list[Path] | None:
    """Create a clean Cocoa plugin mirror before Qt scans platform plugins."""
    if sys.platform != "darwin":
        return None

    import PyQt5

    qt_root = Path(PyQt5.__file__).resolve().parent / "Qt5"
    original_plugin_root = qt_root / "plugins"
    original_plugin = original_plugin_root / "platforms" / "libqcocoa.dylib"
    if not original_plugin.is_file():
        return [original_plugin_root]

    runtime_id = sha256(str(qt_root).encode()).hexdigest()[:12]
    mirror_qt_root = Path(tempfile.gettempdir()) / f"stepseg-qt-{runtime_id}" / "Qt5"
    mirror_plugin_root = mirror_qt_root / "plugins"
    mirror_platform_root = mirror_plugin_root / "platforms"
    mirror_platform_root.mkdir(parents=True, exist_ok=True)
    library_link = mirror_qt_root / "lib"
    if not library_link.exists():
        try:
            library_link.symlink_to(qt_root / "lib", target_is_directory=True)
        except FileExistsError:
            pass

    mirror_plugin = mirror_platform_root / original_plugin.name
    temporary = mirror_plugin.with_name(f".{mirror_plugin.name}.{os.getpid()}.tmp")
    try:
        shutil.copyfile(original_plugin, temporary)
        os.chmod(temporary, original_plugin.stat().st_mode)
        os.replace(temporary, mirror_plugin)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError(
            f"无法准备 Qt macOS 平台插件：{mirror_plugin}。请检查临时目录写权限。"
        ) from exc

    os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = str(mirror_platform_root)
    os.environ["QT_PLUGIN_PATH"] = os.pathsep.join(
        (str(mirror_plugin_root), str(original_plugin_root))
    )
    return [mirror_plugin_root, original_plugin_root]


def main() -> None:
    plugin_roots = _prepare_macos_qt_platform_plugin()
    if plugin_roots is not None:
        from PyQt5.QtCore import QCoreApplication

        QCoreApplication.setLibraryPaths([str(path) for path in plugin_roots])

    from .app import main as run_app

    run_app()
