"""真实导入 DIALux Luminaire Finder 中的灯具（“送到 DIALux”功能)

    产品页 URL
        → 抓取页面，提取“送到 DIALux”按钮背后的 dial:// 链接
        → 校验系统已注册 dial:// 协议（DIAL Data Dispatcher）
        → ShellExecute 打开链接，DIALux 自行下载 .uld 并导入灯具

运行:  python dialux_sim.py [产品页URL 或 dial://...uld 链接]
"""

from __future__ import annotations

import os
import re
import sys
import winreg
from urllib.request import urlopen

# BEGHELLI 产品页（默认示例）
DEFAULT_ARTICLE = "https://luminaires.dialux.com/zh/article/sTi-PgoaSvSgeNvDtL3gPA"


def find_uld_url(article_url: str) -> str:
    """从产品页 HTML 中提取“送到 DIALux”按钮背后的 dial:// 链接"""
    try:
        with urlopen(article_url, timeout=15) as resp:
            html = resp.read().decode("utf-8", "ignore")
        match = re.search(r"dial://[^\"'\s<>]+\.uld", html)
        if match:
            return match.group(0)
        print("[真实] 产品页中未找到 dial:// 链接（页面可能由 JS 渲染）")
    except OSError as exc:
        print(f"[真实] 抓取产品页失败: {exc}")
    return ""


def real_send_to_dialux(uld_url: str) -> int:
    """通过系统注册的 dial:// 协议唤起本机 DIALux（DIAL Data Dispatcher）"""
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"dial\shell\open\command") as key:
            handler = winreg.QueryValue(key, None)
        print(f"[真实] 系统 dial:// 处理程序: {handler}")
    except FileNotFoundError:
        print("[真实] 本机未注册 dial:// 协议，请先安装 DIALux evo")
        return 1

    print(f"[真实] 调用 ShellExecute 打开: {uld_url}")
    print("[真实] DIALux 将自行下载 .uld 并弹出灯具导入窗口，请在 DIALux 中确认")
    os.startfile(uld_url)  # 与浏览器点击“送到 DIALux”完全等价
    return 0


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ARTICLE
    if target.startswith("dial://"):
        uld = target
    else:
        print(f"[真实] 解析产品页: {target}")
        uld = find_uld_url(target)
        if not uld:
            print("[真实] 未能获取 ULD 链接")
            return 1
    return real_send_to_dialux(uld)


if __name__ == "__main__":
    sys.exit(main())
