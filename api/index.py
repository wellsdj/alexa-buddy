"""Vercel entry point when the project builds from the repository's top folder.
The website itself lives in server/api/index.py (also used when Vercel builds from server/)."""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "study_cloud", Path(__file__).resolve().parent.parent / "server" / "api" / "index.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
app = _mod.app
