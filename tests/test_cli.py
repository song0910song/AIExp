import pytest

from lighting_agent.main import build_parser


def test_cli_has_no_chat_command() -> None:
    parser = build_parser()

    assert "chat" not in parser.format_help()
    with pytest.raises(SystemExit) as error:
        parser.parse_args(["chat"])
    assert error.value.code == 2
    assert parser.parse_args(["show-project", "project-id"]).project_id == "project-id"
