from __future__ import annotations

from lighting_agent.project_store import ProjectStore
from lighting_agent.schemas import BriefTemplateOrigin, DesignBrief, ProjectUpdate


def test_template_origin_is_preserved_with_editable_brief_values(tmp_path) -> None:
    store = ProjectStore(tmp_path / "projects")
    brief = DesignBrief(
        project_name="视频会议室改造",
        space_type="视频会议室",
        target_illuminance_lx=750,
        target_cct_k=4000,
        min_cri=80,
        target_ugr=19,
        target_uniformity_u0=0.6,
        max_lpd_w_m2=6.5,
        template_origin=BriefTemplateOrigin(
            template_id="video-conference-room",
            template_name="视频会议室",
            standard_reference="GB 50034-2024 表 5.3.2、4.5.1/4.5.2、表 6.3.5",
        ),
    )

    project = store.create(brief)
    updated_brief = brief.model_copy(update={"target_illuminance_lx": 500})
    updated = store.update(
        project.project_id,
        ProjectUpdate(
            expected_revision=project.revision,
            brief=updated_brief,
        ),
    )

    assert updated.brief.target_illuminance_lx == 500
    assert updated.brief.template_origin is not None
    assert updated.brief.template_origin.template_id == "video-conference-room"
