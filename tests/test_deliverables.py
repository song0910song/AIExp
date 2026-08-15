import json

from lighting_agent import main as cli
from lighting_agent.deliverables import build_design_report, build_dialux_task_package
from lighting_agent.project_store import ProjectStore
from lighting_agent.rag import LocalEvidenceStore
from lighting_agent.schemas import DesignBrief, Evidence, LuminaireCandidate, ProjectState


def test_report_contains_evidence_and_review_boundary() -> None:
    state = ProjectState(
        brief=DesignBrief(project_name="会议室", area_m2=30),
        evidence=[Evidence(source_name="标准摘录", source_type="standard", excerpt="会议室照度条文", locator="第 1 页")],
    )
    report = build_design_report(state)

    assert "标准摘录，第 1 页" in report
    assert "人工复核声明" in report
    assert "不得将本报告视作规范符合性结论" not in report
    package = build_dialux_task_package(state)
    assert package["project_id"] == state.project_id
    assert "UGR" in package["pending_simulation_metrics"]


def test_cli_creates_project_and_report_in_store(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "ProjectStore", lambda: ProjectStore(tmp_path))
    monkeypatch.setattr(cli, "create_evidence_store", lambda: LocalEvidenceStore(tmp_path / "index.json"))

    cli.main(["init-project", "CLI 会议室", "--area-m2", "30"])
    project_id = json.loads(capsys.readouterr().out)["project_id"]
    cli.main(["generate-report", project_id, "--revision", "0"])
    response = json.loads(capsys.readouterr().out)

    report = tmp_path / f"{project_id}.design-report.md"
    assert response["report"] == str(report)
    assert report.exists()


def test_chat_parser_accepts_interactive_without_message() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["chat", "--interactive"])

    assert args.interactive is True
    assert args.message is None


def test_dialux_task_includes_published_photometry_curve_reference() -> None:
    curve_url = "https://luminaires.example.test/fixtures/curve.png"
    state = ProjectState(
        brief=DesignBrief(project_name="Photometry export"),
        luminaires=[
            LuminaireCandidate(
                luminaire_id="fixture-1",
                article_name="Fixture 1",
                detail_url="https://luminaires.example.test/fixtures/fixture-1",
                photometry_image_url=curve_url,
                has_uld=True,
                has_photometry_download=True,
            )
        ],
        selected_luminaire_ids=["fixture-1"],
    )

    package = build_dialux_task_package(state)
    photometry = package["candidates"][0]["photometry"]

    assert photometry == {
        "curve_preview_url": curve_url,
        "curve_preview_available": True,
        "uld_available": True,
        "photometry_file_download_available": True,
        "source_detail_url": "https://luminaires.example.test/fixtures/fixture-1",
    }
