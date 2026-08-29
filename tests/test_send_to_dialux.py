"""Tests for the local Send-to-DIALux handoff (dial:// protocol launcher)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import lighting_agent.dialux_protocol as dialux_protocol
import lighting_agent.tools as tools_module
from lighting_agent.dialux_api import DialuxAPIError
from lighting_agent.dialux_protocol import DialuxProtocolError
from lighting_agent.project_store import ProjectStore
from lighting_agent.rag import LocalEvidenceStore
from lighting_agent.schemas import DesignBrief, LuminaireCandidate
from lighting_agent.web_api import create_app

DIAL_URL = "dial://downloads.dialux.com/products/fixture-1.uld"
HANDLER = "DIAL Data Dispatcher"


class FakeDialuxAPI:
    def __init__(self, *, no_link: bool = False) -> None:
        self.no_link = no_link

    def resolve_send_to_dialux_url(self, detail_url: str) -> str:
        if self.no_link:
            raise DialuxAPIError(
                "产品页未提供“送到 DIALux”协议链接（页面可能由脚本渲染）",
                code="send_to_dialux_link_not_found",
            )
        return DIAL_URL


def _make_app(tmp_path: Any, dialux: FakeDialuxAPI) -> tuple[TestClient, ProjectStore]:
    store = ProjectStore(tmp_path / "projects")
    app = create_app(
        project_store=store,
        evidence_store=LocalEvidenceStore(tmp_path / "rag.json"),
        dialux_api=dialux,
    )
    return TestClient(app), store


def _append_candidate(store: ProjectStore, project_id: str, revision: int) -> None:
    store.append_luminaires(
        project_id,
        revision,
        [
            LuminaireCandidate(
                luminaire_id="send-fixture",
                article_name="Send fixture",
                detail_url="https://luminaires.dialux.com/zh/article/send-fixture",
            )
        ],
    )


def test_validate_protocol_url_accepts_only_dial_uld_links() -> None:
    assert dialux_protocol.validate_protocol_url(DIAL_URL) == DIAL_URL
    assert dialux_protocol.validate_protocol_url("DIAL://downloads.dialux.com/a.ULD") == (
        "DIAL://downloads.dialux.com/a.ULD"
    )
    for bad in ("https://example.com/file.uld", "dial://downloads.dialux.com/a.zip", "file.uld", ""):
        with pytest.raises(DialuxProtocolError) as excinfo:
            dialux_protocol.validate_protocol_url(bad)
        assert excinfo.value.code == "invalid_protocol_url"


def test_open_in_dialux_requires_registered_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dialux_protocol, "find_protocol_handler", lambda: None)
    with pytest.raises(DialuxProtocolError) as excinfo:
        dialux_protocol.open_in_dialux(DIAL_URL)
    assert excinfo.value.code == "protocol_not_registered"


def test_open_in_dialux_hands_off_via_startfile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dialux_protocol, "find_protocol_handler", lambda: HANDLER)
    opened: list[str] = []

    def fake_startfile(url: str) -> None:
        opened.append(url)

    monkeypatch.setattr(dialux_protocol.os, "startfile", fake_startfile, raising=False)

    assert dialux_protocol.open_in_dialux(DIAL_URL) == HANDLER
    assert opened == [DIAL_URL]


def test_send_to_dialux_endpoint_launches(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    client, store = _make_app(tmp_path, FakeDialuxAPI())
    project = client.post("/api/projects", json={"project_name": "送 DIALux"}).json()
    _append_candidate(store, project["project_id"], project["revision"])

    captured: dict[str, str] = {}

    def fake_open(url: str) -> str:
        captured["url"] = url
        return HANDLER

    monkeypatch.setattr(dialux_protocol, "open_in_dialux", fake_open)

    response = client.post(
        f"/api/projects/{project['project_id']}/luminaires/send-fixture/send-to-dialux"
    )
    assert response.status_code == 200
    assert response.json() == {
        "status": "launched",
        "luminaire_id": "send-fixture",
        "dialux_protocol_url": DIAL_URL,
        "handler": HANDLER,
    }
    assert captured["url"] == DIAL_URL


def test_send_to_dialux_endpoint_missing_luminaire(tmp_path: Any) -> None:
    client, _store = _make_app(tmp_path, FakeDialuxAPI())
    project = client.post("/api/projects", json={"project_name": "送 DIALux 404"}).json()

    missing = client.post(
        f"/api/projects/{project['project_id']}/luminaires/unknown-fixture/send-to-dialux"
    )
    assert missing.status_code == 404


def test_send_to_dialux_endpoint_vendor_error(tmp_path: Any) -> None:
    client, store = _make_app(tmp_path, FakeDialuxAPI(no_link=True))
    project = client.post("/api/projects", json={"project_name": "送 DIALux 502"}).json()
    _append_candidate(store, project["project_id"], project["revision"])

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(dialux_protocol, "open_in_dialux", lambda _url: HANDLER)
        response = client.post(
            f"/api/projects/{project['project_id']}/luminaires/send-fixture/send-to-dialux"
        )

    assert response.status_code == 502
    assert "送到 DIALux" in response.json()["detail"]


def test_send_to_dialux_endpoint_protocol_not_registered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    client, store = _make_app(tmp_path, FakeDialuxAPI())
    project = client.post("/api/projects", json={"project_name": "送 DIALux 422"}).json()
    _append_candidate(store, project["project_id"], project["revision"])

    def fake_open(_url: str) -> str:
        raise DialuxProtocolError("本机未注册 dial:// 协议", code="protocol_not_registered")

    monkeypatch.setattr(dialux_protocol, "open_in_dialux", fake_open)

    response = client.post(
        f"/api/projects/{project['project_id']}/luminaires/send-fixture/send-to-dialux"
    )
    assert response.status_code == 422
    assert "dial://" in response.json()["detail"]


def test_send_to_dialux_tool_launches(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    store = ProjectStore(tmp_path / "projects")
    tools_module.configure_runtime_services(
        projects=store, evidence=LocalEvidenceStore(tmp_path / "rag.json")
    )
    project = store.create(DesignBrief(project_name="工具送 DIALux"))
    _append_candidate(store, project.project_id, project.revision)

    monkeypatch.setattr(tools_module, "DialuxAPI", FakeDialuxAPI)
    monkeypatch.setattr(dialux_protocol, "find_protocol_handler", lambda: HANDLER)
    opened: list[str] = []
    monkeypatch.setattr(
        dialux_protocol.os, "startfile", lambda url: opened.append(url), raising=False
    )

    result = tools_module.send_luminaire_to_dialux.invoke(
        {"project_id": project.project_id, "luminaire_id": "send-fixture"}
    )
    assert result["status"] == "launched"
    assert result["dialux_protocol_url"] == DIAL_URL
    assert result["handler"] == HANDLER
    assert opened == [DIAL_URL]


def test_send_to_dialux_tool_reports_failures(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    store = ProjectStore(tmp_path / "projects")
    tools_module.configure_runtime_services(
        projects=store, evidence=LocalEvidenceStore(tmp_path / "rag.json")
    )
    project = store.create(DesignBrief(project_name="工具送 DIALux 失败"))
    _append_candidate(store, project.project_id, project.revision)

    monkeypatch.setattr(tools_module, "DialuxAPI", FakeDialuxAPI)
    monkeypatch.setattr(dialux_protocol, "find_protocol_handler", lambda: None)

    result = tools_module.send_luminaire_to_dialux.invoke(
        {"project_id": project.project_id, "luminaire_id": "send-fixture"}
    )
    assert result["status"] == "local_handoff_failed"
    assert result["error"]["code"] == "protocol_not_registered"

    missing = tools_module.send_luminaire_to_dialux.invoke(
        {"project_id": project.project_id, "luminaire_id": "unknown-fixture"}
    )
    assert missing["status"] == "not_found"


def test_send_to_dialux_tool_reports_vendor_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    store = ProjectStore(tmp_path / "projects")
    tools_module.configure_runtime_services(
        projects=store, evidence=LocalEvidenceStore(tmp_path / "rag.json")
    )
    project = store.create(DesignBrief(project_name="工具送 DIALux 供应商错误"))
    _append_candidate(store, project.project_id, project.revision)

    monkeypatch.setattr(tools_module, "DialuxAPI", lambda: FakeDialuxAPI(no_link=True))

    result = tools_module.send_luminaire_to_dialux.invoke(
        {"project_id": project.project_id, "luminaire_id": "send-fixture"}
    )
    assert result["status"] == "vendor_error"
    assert result["vendor_error"]["code"] == "send_to_dialux_link_not_found"
