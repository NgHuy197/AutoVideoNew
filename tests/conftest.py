"""Keep the suite away from the real Video Auto data under %LOCALAPPDATA%.

``backend.app.config`` resolves its roots once at import time, so these
variables must be set before any test module imports the backend.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_root = Path(tempfile.mkdtemp(prefix="videoauto-tests-"))
for _name, _folder in (("VIDEOAUTO_DATA_ROOT", "data"), ("VIDEOAUTO_INPUT_ROOT", "input"),
                       ("VIDEOAUTO_OUTPUT_ROOT", "output")):
    os.environ[_name] = str(_root / _folder)
# Never reach a real local LLM from the suite.
os.environ["VIDEOAUTO_TRANSLATION_MODEL"] = ""
