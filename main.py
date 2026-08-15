"""Convenience launcher for the lighting-design-agent package."""

from __future__ import annotations

import sys
from pathlib import Path

# 添加 src 目录到 sys.path，以便在 main.py 中导入 lighting_agent 包
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from lighting_agent.main import main


if __name__ == "__main__":
    main()
