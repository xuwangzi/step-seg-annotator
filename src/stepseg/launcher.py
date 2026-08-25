"""GUI launcher that prepares the macOS Qt plugin before importing Qt."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def _prepare_macos_qt_platform_plugin() -> Path | None:
    """Refresh the Cocoa plugin before Qt scans the platform directory."""
    if sys.platform != "darwin":
        return None

    import PyQt5

    plugin_root = Path(PyQt5.__file__).resolve().parent / "Qt5" / "plugins"
    platform_root = plugin_root / "platforms"
    os.environ["QT_PLUGIN_PATH"] = str(plugin_root)
    os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = str(platform_root)
    plugin = platform_root / "libqcocoa.dylib"
    if not plugin.is_file():
        return plugin_root

    temporary = plugin.with_name(f".{plugin.name}.stepseg.tmp")
    try:
        shutil.copyfile(plugin, temporary)
        os.chmod(temporary, plugin.stat().st_mode)
        os.replace(temporary, plugin)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError(
            f"无法准备 Qt macOS 平台插件：{plugin}。请检查虚拟环境写权限。"
        ) from exc
    return plugin_root


def main() -> None:
    plugin_root = _prepare_macos_qt_platform_plugin()
    if plugin_root is not None:
        from PyQt5.QtCore import QCoreApplication

        QCoreApplication.setLibraryPaths([str(plugin_root)])

    from .app import main as run_app

    run_app()
