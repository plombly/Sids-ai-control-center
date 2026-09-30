import importlib.util
from pathlib import Path

import pytest


SCRIPTS = [
    ("submit_job", "submit-job.py", ["task"]),
    ("submit_review", "submit-review.py", ["builder-job-id"]),
]


def load_script(name, filename):
    path = Path(__file__).resolve().parents[1] / "scripts" / filename
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name, filename, args", SCRIPTS)
def test_default_model(name, filename, args, monkeypatch):
    module = load_script(name, filename)
    monkeypatch.delenv("DEFAULT_MODEL", raising=False)
    assert module.build_parser().parse_args(args).model == "gpt-5.6-luna"


@pytest.mark.parametrize("name, filename, args", SCRIPTS)
def test_environment_model(name, filename, args, monkeypatch):
    module = load_script(name, filename)
    monkeypatch.setenv("DEFAULT_MODEL", "test-model-x")
    assert module.build_parser().parse_args(args).model == "test-model-x"


@pytest.mark.parametrize("name, filename, args", SCRIPTS)
def test_explicit_model_wins(name, filename, args):
    module = load_script(name, filename)
    assert module.build_parser().parse_args(args + ["--model", "other-model"]).model == "other-model"


@pytest.mark.parametrize("name, filename, _args", SCRIPTS)
def test_help_mentions_default_model(name, filename, _args):
    module = load_script(name, filename)
    assert "DEFAULT_MODEL" in module.build_parser().format_help()
