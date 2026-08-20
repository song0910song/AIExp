from __future__ import annotations

import pytest

import lighting_agent.agent as agent
import lighting_agent.tools as agent_tools
from lighting_agent.project_store import ProjectStore
from lighting_agent.schemas import DesignBrief, Evidence


class FakeEvidenceStore:
    def __init__(self) -> None:
        self.evidence = {
            "meeting-room-standard": Evidence(
                evidence_id="meeting-room-standard",
                source_name="GB-50034-2024.md",
                source_type="standard",
                excerpt="普通会议室维持照度 300 lx，统一眩光值 UGR 不应大于 19，Ra 不应低于 80。",
                locator="表 6.3.5",
            )
        }

    def get_evidence(self, evidence_ids: list[str]) -> list[Evidence]:
        return [self.evidence[item] for item in evidence_ids if item in self.evidence]


def test_rag_tool_persists_lighting_parameters_and_field_provenance(tmp_path, monkeypatch) -> None:
    store = ProjectStore(tmp_path / "projects")
    project = store.create(
        DesignBrief(
            project_name="RAG meeting room",
            space_type="普通会议室",
            area_m2=30,
            mounting_height_m=2.7,
        )
    )
    monkeypatch.setattr(agent_tools, "project_store", store)
    monkeypatch.setattr(agent_tools, "evidence_store", FakeEvidenceStore())

    result = agent_tools.apply_rag_lighting_parameters.invoke(
        {
            "project_id": project.project_id,
            "expected_revision": project.revision,
            "evidence_ids": ["meeting-room-standard"],
            "target_illuminance_lx": 300,
            "min_cri": 80,
            "target_ugr": 19,
        }
    )

    saved = store.get(project.project_id)
    assert result["source"] == "rag"
    assert result["applied_fields"] == ["min_cri", "target_illuminance_lx", "target_ugr"]
    assert saved.brief.target_illuminance_lx == 300
    assert saved.brief.min_cri == 80
    assert saved.brief.target_ugr == 19
    assert {"target_illuminance_lx", "min_cri", "target_ugr"} <= saved.brief.confirmed_fields
    assert saved.brief.lighting_parameter_sources["target_illuminance_lx"].evidence_ids == [
        "meeting-room-standard"
    ]
    assert [item.evidence_id for item in saved.evidence] == ["meeting-room-standard"]


def test_rag_tool_does_not_overwrite_existing_manual_parameter(tmp_path, monkeypatch) -> None:
    store = ProjectStore(tmp_path / "projects")
    project = store.create(
        DesignBrief(
            project_name="Protected target",
            target_illuminance_lx=500,
            confirmed_fields={"target_illuminance_lx"},
        )
    )
    monkeypatch.setattr(agent_tools, "project_store", store)
    monkeypatch.setattr(agent_tools, "evidence_store", FakeEvidenceStore())

    with pytest.raises(ValueError, match="cannot overwrite"):
        agent_tools.apply_rag_lighting_parameters.invoke(
            {
                "project_id": project.project_id,
                "expected_revision": project.revision,
                "evidence_ids": ["meeting-room-standard"],
                "target_illuminance_lx": 300,
            }
        )


def test_rag_tool_records_provenance_when_value_matches_existing(tmp_path, monkeypatch) -> None:
    store = ProjectStore(tmp_path / "projects")
    project = store.create(
        DesignBrief(
            project_name="Template-initialised targets",
            space_type="普通办公室",
            area_m2=30,
            mounting_height_m=2.7,
            target_illuminance_lx=300,
            min_cri=80,
            confirmed_fields={"target_illuminance_lx", "min_cri"},
        )
    )
    monkeypatch.setattr(agent_tools, "project_store", store)
    monkeypatch.setattr(agent_tools, "evidence_store", FakeEvidenceStore())

    result = agent_tools.apply_rag_lighting_parameters.invoke(
        {
            "project_id": project.project_id,
            "expected_revision": project.revision,
            "evidence_ids": ["meeting-room-standard"],
            "target_illuminance_lx": 300,
            "min_cri": 80,
        }
    )

    saved = store.get(project.project_id)
    assert result["status"] == "ok"
    assert result["applied_fields"] == ["min_cri", "target_illuminance_lx"]
    assert saved.brief.target_illuminance_lx == 300
    assert saved.brief.min_cri == 80
    assert saved.brief.lighting_parameter_sources["target_illuminance_lx"].evidence_ids == [
        "meeting-room-standard"
    ]
    assert [item.evidence_id for item in saved.evidence] == ["meeting-room-standard"]


def test_agent_prompt_uses_rag_before_user_clarification() -> None:
    assert "apply_rag_lighting_parameters" in agent.SYSTEM_PROMPT
    assert "只有证据明确、适用且不冲突时" in agent.SYSTEM_PROMPT
    assert "仅当 RAG 没有适用明确值" in agent.SYSTEM_PROMPT
