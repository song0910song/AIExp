import pytest

from lighting_agent.project_store import ProjectNotFoundError, ProjectStore, RevisionConflictError
from lighting_agent.schemas import (
    DesignBrief,
    LuminaireCandidate,
    LuminaireSearchRequest,
    LuminaireSearchRun,
    ProjectUpdate,
)


def test_store_uses_revision_for_updates(tmp_path) -> None:
    store = ProjectStore(tmp_path)
    state = store.create(DesignBrief(project_name="会议室", area_m2=30))

    assert state.revision == 0
    assert "space_type" in state.open_questions
    updated = store.update(
        state.project_id,
        ProjectUpdate(expected_revision=0, brief=state.brief.model_copy(update={"space_type": "会议室"})),
    )
    assert updated.revision == 1
    assert store.get(state.project_id).brief.space_type == "会议室"

    with pytest.raises(RevisionConflictError):
        store.update(state.project_id, ProjectUpdate(expected_revision=0))


def test_store_delete_removes_project_revisions_and_artifacts(tmp_path) -> None:
    store = ProjectStore(tmp_path)
    state = store.create(DesignBrief(project_name="待删除项目"))
    report = store.artifact_path(state.project_id, ".design-report.md")
    legacy = store.artifact_path(state.project_id, ".json")
    report.write_text("report", encoding="utf-8")
    legacy.write_text(state.model_dump_json(), encoding="utf-8")

    store.delete(state.project_id)

    with pytest.raises(ProjectNotFoundError):
        store.get(state.project_id)
    with pytest.raises(ProjectNotFoundError):
        store.revisions(state.project_id)
    assert not report.exists()
    assert not legacy.exists()


def test_store_persists_rejected_candidates_but_marks_them_ineligible(tmp_path) -> None:
    store = ProjectStore(tmp_path)
    project = store.create(
        DesignBrief(
            project_name="Meeting room",
            mounting="recessed",
            target_cct_k=3500,
            min_cri=90,
            min_ip_rating="IP20",
        )
    )
    rejected = LuminaireCandidate(
        luminaire_id="bad-fixture",
        article_name="Bad fixture",
        detail_url="https://example.test/bad",
        cct_k=4000,
        cri=80,
        ip_rating="IP44",
    )

    state, saved, _ = store.append_luminaires(project.project_id, project.revision, [rejected])

    assert saved == 1
    assert state.revision == project.revision + 1
    assert state.luminaires[0].brief_validation is not None
    assert state.luminaires[0].brief_validation.matching_status == "rejected"
    with pytest.raises(ValueError, match="requires candidates verified"):
        store.set_selected_luminaires(project.project_id, state.revision, [rejected.luminaire_id])


def test_store_keeps_final_selection_separate_from_search_candidates(tmp_path) -> None:
    store = ProjectStore(tmp_path)
    project = store.create(DesignBrief(project_name="Final selection"))
    first = LuminaireCandidate(
        luminaire_id="candidate-a",
        article_name="Candidate A",
        detail_url="https://example.test/a",
    )
    second = LuminaireCandidate(
        luminaire_id="candidate-b",
        article_name="Candidate B",
        detail_url="https://example.test/b",
    )
    saved, count, _ = store.append_luminaires(project.project_id, project.revision, [first, second])
    assert count == 2

    selected = store.set_selected_luminaires(project.project_id, saved.revision, [second.luminaire_id])

    assert selected.selected_luminaire_ids == [second.luminaire_id]
    assert [item.luminaire_id for item in selected.selected_luminaires()] == [second.luminaire_id]
    assert len(selected.luminaires) == 2


def test_project_state_drops_legacy_plan_and_scene_fields() -> None:
    legacy = {
        "project_id": "a" * 32,
        "revision": 2,
        "brief": {"project_name": "Legacy project"},
        "plan": {"plan_id": "legacy-plan"},
        "scene": {"scene_id": "legacy-scene"},
    }

    state = __import__("lighting_agent.schemas", fromlist=["ProjectState"]).ProjectState.model_validate(legacy)

    payload = state.model_dump(mode="json")
    assert "plan" not in payload
    assert "scene" not in payload


def test_search_snapshot_is_preserved_when_brief_changes(tmp_path) -> None:
    store = ProjectStore(tmp_path)
    project = store.create(
        DesignBrief(
            project_name="Snapshot",
            space_type="会议室",
            mounting="recessed",
            target_cct_k=4000,
        )
    )
    request = LuminaireSearchRequest(
        keyword="downlight",
        target_cct_k=4000,
    )
    run = LuminaireSearchRun(
        request=request,
        original_keyword="downlight",
        resolved_keyword="downlight",
        endpoint="https://luminaires.dialux.com/zh/0/50/search/query/a231",
    )
    candidate = LuminaireCandidate(
        luminaire_id="snapshot-fixture",
        article_name="Snapshot fixture",
        detail_url="https://luminaires.dialux.com/zh/article/snapshot-fixture",
        cct_k=4000,
        detail_status="fetched",
        matching_status="matches",
        search_run_id=run.search_run_id,
    )

    saved, count, _ = store.append_luminaires(project.project_id, project.revision, [candidate], run)
    assert count == 1
    assert saved.luminaire_search_runs[0].search_run_id == run.search_run_id
    assert saved.luminaires[0].matching_status == "matches"
    assert saved.luminaires[0].brief_validation is not None
    assert saved.luminaires[0].brief_validation.matching_status == "matches"

    selected = store.set_selected_luminaires(
        project.project_id,
        saved.revision,
        [candidate.luminaire_id],
    )
    changed = store.update(
        project.project_id,
        ProjectUpdate(
            expected_revision=selected.revision,
            brief=selected.brief.model_copy(update={"target_cct_k": 3500}),
        ),
    )

    assert changed.selected_luminaire_ids == []
    changed_candidate = changed.luminaires[0]
    assert changed_candidate.matching_status == "matches"
    assert changed_candidate.brief_validation is not None
    assert changed_candidate.brief_validation.matching_status == "rejected"
