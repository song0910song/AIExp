# 阶段 0 脱敏基线

该目录是脱敏回归基线。常规自动化测试不访问真实 LLM、Chroma、DIALux 或供应商网络。

- `projects/` 包含 10 个脱敏项目输入，覆盖普通办公室、会议室和视频会议室三类模板。
- `sample-room.dxf` 是共享的脱敏 DXF 平面图输入。
- `standard-gb50034-2024.md` 是标准资料快照。
- `luminaire-candidates.json` 是固定的候选灯具快照。
- `dialux-result.json` 是固定的结构化仿真结果快照。
- `synthetic-bridge-luminaire.ies` 仅用于人工 DIALux 桥接验证；不是商用灯具或设计数据。
- `bridge-spatial-model.json` 是桥接测试共享的合成单房间模型；其中确认状态仅用于测试，`a` 重复构成的 CAD 哈希是占位符。`scripts/prepare_dialux_bridge.py` 生成对应 DXF 并替换为实际哈希，输出独立 IFC/IES 输入包。
- `room-summary-5.14-zh.txt` 保留阶段 0 实际两页 PDF 的关键行与分页，压缩空行用于固定模板解析回归；原始 PDF SHA256 为 `effb5c21808d571ee262864558f823813bbfd16634b4813031e9e22d1bc27f12`。测试此文本不构成新的 DIALux 实机运行。

项目输入只作为测试数据，不代表任何项目的合规结论。
合成光度文件只验证导入/计算/导出链路，不得用于照明方案、产品选型或合规结论。

2026-10-05 已用 DIALux `5.14.0.3` 完成一次真实单房间桥接，记录见 `docs/dialux-phase0-bridge-validation.md`。旧的 `dialux-result.json` 仍是离线快照，不能冒充本次真实输出。
