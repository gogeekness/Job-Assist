"""Every core module must import cleanly with no active profile and no
API keys configured -- catches syntax errors, missing dependencies, and
accidental module-level side effects (network calls, file I/O outside a
function) before they reach a profile with real data."""
import importlib

import pytest

CORE_MODULES = [
    "profile",
    "db",
    "cv_bank",
    "llm_plugin",
    "generate_cv",
    "FindJobs",
]


@pytest.mark.parametrize("module_name", CORE_MODULES)
def test_module_imports(module_name):
    importlib.import_module(module_name)


def test_findjobs_exposes_flask_app():
    import FindJobs
    assert FindJobs.app.name == "FindJobs"
