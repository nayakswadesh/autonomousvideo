"""Shorts must not require long-form-only provider imports during live setup."""

import builtins

from autonomous_studio.config import Settings
from autonomous_studio.providers.longform_visuals import OpenAILongFormVisualPlanner
from autonomous_studio.workflow import Studio


def test_live_short_does_not_import_longform_visual_planner(tmp_path, monkeypatch):
    studio = Studio(tmp_path, settings=Settings(api_key="test"))
    project = studio.create_topic("A surprising tree", duration=55, production_mode="full")
    original_import = builtins.__import__

    def reject_longform_visual_import(name, *args, **kwargs):
        if name == "autonomous_studio.providers.longform_visuals":
            raise ImportError("long-form visual planner is unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_longform_visual_import)
    studio.configure_live(project)

    assert studio.live_client is not None
    assert studio.research is not None
    assert studio.director is not None
    assert studio.longform_visual_planner is None
    assert studio.authentic_media_discovery is None


def test_live_longform_still_configures_visual_and_discovery_providers(tmp_path):
    studio = Studio(tmp_path, settings=Settings(api_key="test"))
    project = studio.create_longform_topic("How stars shine")

    studio.configure_live(project)

    assert isinstance(studio.longform_visual_planner, OpenAILongFormVisualPlanner)
    assert studio.authentic_media_discovery is not None
