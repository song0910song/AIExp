# 目标 3：单房间桥接验证记录

验证日期：2026-10-05。软件：DIALux evo 14.0 (64-Bit)，文件版本 `5.14.0.3`。

本轮完成合成单房间的 IFC 导入、IES 导入、布置一盏灯、真实计算、保存并重新打开原生工程、导出原始 PDF 与光线追踪图片。目标 3 尚未全部完成；桌面操作目前是交互验证，尚未固化为独立执行器。

## 已解决的导入问题

| 问题 | 修改与验证依据 |
|---|---|
| 米制坐标被作为毫米解释 | 显式设置长度、面积、体积 SI 单位；IFC 几何回读与 DIALux 控件均核对尺寸 |
| 场地没有坐标系、建筑被跳过 | 保留 Project → Site → Building → Storey → Space，并为各级设置 `IfcLocalPlacement`；不对空间容器添加实体或包围盒 |
| `Bridge test room` 的 `IfcSpace` 被跳过 | 显式设置空间层级 `CompositionType=ELEMENT` 后，房间出现在 DIALux 项目树中，尺寸为 6 × 4 × 2.8 m |
| 桌子曾被当作墙处理 | 恢复 `IfcFurniture`，实测可选中并显示 `Test desk` 和正确实体类型；先前家具不受支持的推断撤销 |
| 材质属性有反射率但 DIALux 使用默认值 | 添加 `IfcStyledItem` / `IfcSurfaceStyleShading`；保留自定义属性用于溯源。桌面实读 30%，最终报告的顶/墙/地分别为 70%/50%/20% |
| 只有空间体积，没有物理顶面 | 在已确认净高处添加简化不透明顶板，厚度必须显式提供；DIALux 项目树同时出现地板和顶板 |

实测采用 IFC4、SweptSolid 几何和 DIALux“新方法”导入。导入时不勾选“使用默认反射率覆盖现有反射率”。几何与报告警告须分别核对，警告消失不能单独作为验收依据。

## 测试条件和结果

测试模型为合成房间，面积 24 m²、层高 3 m、净高 2.8 m，含 1.2 × 0.6 × 0.75 m 的简化家具块。墙厚 0.12 m、楼板厚 0.15 m、顶板厚 0.10 m 均是测试假设。灰色漫反射表面用于传递反射率，不代表真实装饰颜色。

灯具使用仓库内明确标注的合成 IES，1000 lm、10 W，位置 `(3, 2, 2.5) m`、旋转角 `(0, 0, 0)`。工作面高 0.8 m、边缘区 0.5 m、维护系数 0.8、自动网格，计算包含间接光与家具。

| 输出 | 本次 DIALux 原始结果 |
|---|---|
| 房间 / 场景 | `101 - Bridge test room` / `灯光场景 1` |
| 计算面 | `工作面 (101 - Bridge test room)`，WP1 |
| 平均照度 / U₀ | 23.2 lx / 0.46 |
| 空间 / 工作面功率密度 | 0.42 / 0.67 W/m²，面积口径不同，不可混用 |
| 原始眩光字段 | `RUG,max = 22`，报告条件为矩形 6 × 4 m、SHR 0.25；未验证观察点法 |
| 重新打开 `.evo` | 相同房间与工作面仍显示 23.2 lx、0.46 |

原始 PDF 的 500 lx、U₀ 0.60、眩光 19 及能耗条件来自 DIALux 默认办公室模板，未绑定用户确认的规范。低照度和红色不合格符号是本合成测试的原始结果，不构成真实设计结论。光线追踪图片为 DIALux 实际输出的 400 × 300 像素验证样图。

本机成果保存在 `data/dialux-runs/phase0-20261005/`（已被 Git 忽略，不随代码提交）：

- `bridge.dxf`、`spatial-model.json`、`bridge.ifc`、`synthetic-bridge-luminaire.ies`：本次输入。
- `bridge.evo`：已重新打开验证的原生工程。
- `room-summary.pdf`、`room-summary.txt`：DIALux 原始两页报告及提取文本。
- `workplane-isolines.jpg`、`dialux-raytrace.jpg`：DIALux 导出的等值线图和光线追踪图片。
- `calculation-results.png`、`reopened-project.png`：本次与重新打开后的结果界面。
- `input-manifest.json`：输入准备时的状态快照；其中未验证标记不会事后改写。
- `run-record.json`：交互验收记录、版本、设置、字段来源、限制及各文件 SHA256。

最终 IFC SHA256：`9ecbc2a6d3cfcdd32b7cc08971d6f2c978fc3e4a4b6006facf613e13f7ae9aab`。

## 代码与复现

`src/lighting_agent/ifc_export.py` 提供单房间导出。`POST /api/projects/{id}/spatial-model/ifc` 要求当前项目修订号、`wall_thickness_m`、`floor_slab_thickness_m`、`ceiling_slab_thickness_m`。API 重新评估模型确认状态，并核对当前 CAD 哈希，拒绝过期或未确认模型。

导出目前明确拒绝多房间及门窗开口。柱、遮挡物和非矩形房间尚无同等实机验收，不应标记为已验证。墙目前使用一个环形拉伸体，后续开口和共墙处理需拆分宿主。

生成新的合成输入包（目录必须尚不存在）：

```powershell
uv run scripts/prepare_dialux_bridge.py --output-dir data/dialux-runs/new-bridge
```

脚本从共享合成模型生成对应 DXF、IFC、IES 副本和哈希清单，不会操作 DIALux，也不会把准备输入记为计算完成。常规回归测试保持离线；本轮 `uv run pytest -q` 为 87 项通过。

## 后续工作

1. 固化独立 Windows 执行器：指定进程和工程、桌面会话互斥、分步后置验证、超时、可恢复状态及人工接管；记录本次发现的控件路径。
2. 验证结构化数值导出和结果解析；XML/CSV 尚未验证，不能宣称已有稳定结果 API。当前仅验证了 PDF 输出。
3. 扩展多房间、门窗开口与宿主关系；用已确认真实 CAD、灯具和规则做验收。
4. 最后增加受运行次数和时间约束的布局迭代；每次迭代必须独立绑定输入、设置及真实结果。

最终成功导入的 IFC 报告尚未单独归档；生产执行器仍需补齐这一证据收集步骤。
