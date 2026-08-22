# 阶段 0 脱敏基线

该目录是离线回归基线，不访问真实 LLM、Chroma、DIALux 或供应商网络。

- `projects/` 包含 10 个脱敏项目输入，覆盖普通办公室、会议室和视频会议室三类模板。
- `sample-room.dxf` 是共享的脱敏 DXF 平面图输入。
- `standard-gb50034-2024.md` 是标准资料快照。
- `luminaire-candidates.json` 是固定的候选灯具快照。
- `dialux-result.json` 是固定的结构化仿真结果快照。

项目输入只作为测试数据，不代表任何项目的合规结论。
