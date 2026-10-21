from pathlib import Path

import pytest

from autonomous_studio.workflow.engine import load_brief


@pytest.fixture
def brief():
    return load_brief(Path(__file__).resolve().parents[1] / "examples/earthquake-countdown.yaml")
