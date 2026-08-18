"""Connect to a running DaVinci Resolve, or report why not.

Used by dump / place spike scripts. Never raises on a missing host —
callers treat ``None`` as "Resolve is not running" and exit 0.
"""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path
from typing import Any, Optional, Tuple

MAC_SCRIPT_API = Path(
    "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
)
MAC_SCRIPT_LIB = Path(
    "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so"
)
MAC_APP = Path("/Applications/DaVinci Resolve/DaVinci Resolve.app")


def default_env() -> Tuple[Path, Path]:
    if sys.platform == "darwin":
        return MAC_SCRIPT_API, MAC_SCRIPT_LIB
    if sys.platform.startswith("win"):
        programdata = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
        api = programdata / "Blackmagic Design" / "DaVinci Resolve" / "Support" / "Developer" / "Scripting"
        lib = Path(r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll")
        return api, lib
    return Path("/opt/resolve/Developer/Scripting"), Path("/opt/resolve/libs/Fusion/fusionscript.so")


def apply_env() -> Tuple[Path, Path]:
    api, lib = default_env()
    os.environ.setdefault("RESOLVE_SCRIPT_API", str(api))
    os.environ.setdefault("RESOLVE_SCRIPT_LIB", str(lib))
    modules = api / "Modules"
    if modules.is_dir() and str(modules) not in sys.path:
        sys.path.insert(0, str(modules))
    return api, lib


def installed_app_version() -> Optional[str]:
    if sys.platform != "darwin" or not MAC_APP.is_dir():
        return None
    plist = MAC_APP / "Contents" / "Info.plist"
    if not plist.is_file():
        return None
    try:
        import plistlib

        data = plistlib.loads(plist.read_bytes())
    except (OSError, ValueError, ImportError):
        return None
    short = data.get("CFBundleShortVersionString")
    build = data.get("CFBundleVersion")
    if short and build:
        return f"{short} ({build})"
    return short or build


def connect() -> Tuple[Optional[Any], str]:
    """Return ``(resolve, note)``. ``resolve`` is None if the host is down."""
    apply_env()
    try:
        import DaVinciResolveScript as dvr
    except ImportError as exc:
        return None, f"DaVinciResolveScript import failed: {exc}"
    try:
        resolve = dvr.scriptapp("Resolve")
    except Exception as exc:  # fusionscript can raise if the IPC socket is down
        return None, f"scriptapp('Resolve') raised: {exc}"
    if resolve is None:
        return None, "scriptapp('Resolve') returned None (Resolve is not running)"
    return resolve, "connected"


def json_safe(value: Any, *, _depth: int = 0) -> Any:
    """Best-effort conversion of Resolve objects to JSON types."""
    if _depth > 8:
        return "<max-depth>"
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {str(k): json_safe(v, _depth=_depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v, _depth=_depth + 1) for v in value]
    try:
        return json_safe(dict(value), _depth=_depth + 1)
    except Exception:
        pass
    return repr(value)


def host_facts() -> dict:
    api, lib = default_env()
    return {
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "installed_app_version": installed_app_version(),
        "resolve_app": str(MAC_APP) if sys.platform == "darwin" else None,
        "script_api": str(api),
        "script_lib": str(lib),
        "script_api_exists": api.is_dir(),
        "script_lib_exists": lib.is_file(),
        "readme": str(api / "README.txt"),
        "changelog": str(api / "CHANGELOG.txt"),
    }
