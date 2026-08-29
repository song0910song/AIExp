"""Local DIALux handoff through the registered ``dial://`` protocol (Windows).

Replicates the Luminaire Finder "Send to DIALux" button: the OS dispatches a
validated ``dial://…uld`` URL to the installed DIAL Data Dispatcher, which
downloads the .uld file and opens the DIALux luminaire import dialog.
"""

from __future__ import annotations

import os
import re

# Validated again before any OS handoff so only luminaire protocol links,
# never arbitrary URLs or file paths, can be opened via ShellExecute.
DIALUX_PROTOCOL_URL_PATTERN = re.compile(
    r"^dial://[A-Za-z0-9._~:/?#@!$&()*+,;=%\[\]'-]+\.uld$", re.I
)


class DialuxProtocolError(RuntimeError):
    """A structured local-handoff failure safe to expose through the API."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": str(self)}


def validate_protocol_url(value: str) -> str:
    """Return the URL only when it is a strict ``dial://…uld`` handoff link."""

    url = value.strip()
    if not DIALUX_PROTOCOL_URL_PATTERN.match(url):
        raise DialuxProtocolError(
            "拒绝打开非 dial://…uld 灯具链接，本接口只能唤起 DIALux 导入",
            code="invalid_protocol_url",
        )
    return url


def find_protocol_handler() -> str | None:
    """Return the registered dial:// handler command, or None when absent."""

    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"dial\shell\open\command") as key:
            handler = winreg.QueryValue(key, None)
    except (ImportError, OSError):
        return None
    return str(handler) if handler else None


def open_in_dialux(dial_url: str) -> str:
    """Validate the link, check the local handler and hand off via ShellExecute."""

    url = validate_protocol_url(dial_url)
    handler = find_protocol_handler()
    if handler is None:
        raise DialuxProtocolError(
            "本机未注册 dial:// 协议，请先安装 DIALux evo（DIAL Data Dispatcher）",
            code="protocol_not_registered",
        )
    os.startfile(url)
    return handler
