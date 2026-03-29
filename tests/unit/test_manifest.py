"""Unit tests for config manifest loader."""

import asyncio
import textwrap

import pytest

from ao.config.manifest import AgentConfig, AppManifest, ToolConfig


class TestAppManifest:
    def test_load_yaml(self, tmp_path):
        manifest_yaml = textwrap.dedent("""\
            app_id: email-assistant
            display_name: Email Assistant
            identity_mode: service
            agents:
              - name: classifier
                model: gpt-4o-mini
              - name: responder
                model: gpt-4o
            tools:
              - name: knowledge_base
                type: builtin
            policies:
              - name: pii_filter
                stage: pre_execution
                action: redact
        """)
        f = tmp_path / "ao-manifest.yaml"
        f.write_text(manifest_yaml)

        manifest = AppManifest.from_yaml(str(f))
        assert manifest.app_id == "email-assistant"
        assert manifest.identity_mode.value == "service"
        assert len(manifest.agents) == 2
        assert manifest.agents[0].name == "classifier"
        assert len(manifest.tools) == 1
        assert manifest.tools[0].name == "knowledge_base"

    def test_missing_app_id(self, tmp_path):
        f = tmp_path / "bad.yaml"
        f.write_text("display_name: Test\n")
        with pytest.raises((KeyError, TypeError)):
            AppManifest.from_yaml(str(f))
