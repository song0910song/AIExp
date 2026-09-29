from importlib.resources import files

from lighting_agent.agent import SYSTEM_PROMPT


def test_system_prompt_is_loaded_from_markdown() -> None:
    prompt_file = files("lighting_agent").joinpath("system_prompt.md")
    assert SYSTEM_PROMPT == prompt_file.read_text(encoding="utf-8")
    assert SYSTEM_PROMPT.startswith("# 角色与目标\n")
    assert "# 回复方式\n" in SYSTEM_PROMPT
