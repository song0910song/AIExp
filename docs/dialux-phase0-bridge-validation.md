# 目标 3：IFC/DIALux 桥接验证记录

验证日期：2026-10-05。软件：DIALux evo 14.0 (64-Bit)，文件版本 `5.14.0.3`。

2026-10-05 完成合成单房间的首轮 IFC 导入、IES 导入、布置一盏灯、真实计算、保存并重新打开原生工程、导出原始 PDF 与光线追踪图片。最初桌面操作为交互验证；随后增加独立执行器和固定双房间 envelope profile。下文同时保留历史单房间证据与最新双房间验收，不把固定 profile 外推为任意项目能力。

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

旧的 `src/lighting_agent/ifc_export.py` API 仍明确拒绝多房间及门窗开口；新增的独立 envelope 导出器仅针对固定合成双房间 profile 支持共墙、门洞宿主和门窗材质回读。柱、遮挡物、非矩形房间、任意真实墙体及通用门窗规则尚无同等实机验收，不应标记为已验证。

生成新的合成输入包（目录必须尚不存在）：

```powershell
uv run scripts/prepare_dialux_bridge.py --output-dir data/dialux-runs/new-bridge
```

脚本从共享合成模型生成对应 DXF、IFC、IES 副本和哈希清单，不会操作 DIALux，也不会把准备输入记为计算完成。常规回归测试保持离线；本轮 `uv run pytest -q` 为 87 项通过。

## 后续工作

2026-10-06 范围确认：目标 3 只包含人工照明，不包含自然光计算。后续独立执行器不创建日光场景或输出日光评价指标。

2026-10-06 首次增量：独立原生工程执行器与 PDF 结果回收，使用方式和当前边界见 [独立执行器](dialux-standalone-executor.md)。按用户要求，在目标 3 功能完整验收前不接入项目智能体。首次增量基于已验收原生模板重新计算；后续新增的 IFC/IES 配置验收记录另列如下。

本机自动验收目录为 `data/dialux-runs/executor-20261006/`，成功结果位于 `attempt-011/`。此次尝试从启动到验收全程由命令行执行器完成，用时约 206 秒，过程中未人工补做桌面步骤。前面的开发失败尝试仍保留原状态和证据。

随后用相同代码创建全新任务 `data/dialux-runs/executor-repeat-20261006/`，首次尝试 `attempt-001/` 全程自动通过，用时约 218 秒，平均照度与均匀度仍为 `23.2 lx / 0.46`。两次成功运行均完成本次渲染及工程重开，且已逐页检查各自的 PDF 和原始图片。全新任务的第一次尝试成功与同任务重复调用的幂等检查分别验证，不能混为一项。

- 完成输入快照/哈希检查、桌面互斥、固定进程/主窗口/工程身份、完整计算设置、清除旧结果、观察新计算启动和结束。
- 导出 `project.evo`、两页 `room-summary.pdf`、`dialux-raytrace.jpg`，并输出逐字段来源的 `results.json`。
- PDF 和结果面板均为平均照度 `23.2 lx`、`U₀=0.46`；工作面/房间功率密度分别 `0.67/0.42 W/m²`。切换到另一工程再重开本次工程，结果一致。
- 本次 PDF 两页已用 Poppler 渲染并视觉核对，原始光线追踪图也已检查。渲染出现嵌入字体类型警告，实际页面可读、未见缺字。
- 再次执行同一个已完成任务只校验成果哈希，没有产生新尝试或重复计算。
- `state.json` 记录执行器源码 SHA256、每次尝试、进程启动时间和全部成果哈希；源码在执行期间改变则拒绝标记成功。
- 全量离线回归 `uv run pytest -q` 为 **113 项通过**，4 个已有依赖弃用警告。

此次原生工程 SHA256 为 `a9f0a29f33867331eeda942d9d3f95a6c42a5cbcd23c6996ed5a80113cb5a389`；PDF SHA256 为 `a4e15b5e8e7953d9bd171876f80bb62182dddd34c95e0801e18ff6af509feaa0`。这只验收固定合成单房间的原生工程重算路径，不代表通用 IFC 导入、任意布灯、多房间或规则优化已完成。

### IFC/IES 新建工程增量（2026-10-06）

独立配置 `evo-5.14.0.3-zh-synthetic-ifc-v1` 已完成一次全程自动实机验收，从 IFC/IES 输入创建工程，不读原生模板。成果位于 `data/dialux-runs/import-acceptance-20261006/attempt-002/`，运行 ID `6e9a7324b6894296a0a2bdcfc1324ab9`，用时约 342 秒。该成功尝试未人工补做桌面操作；同目录第一次失败尝试、其他开发任务与分步探针均保留，不计为成功。

- 校验米制 DXF 的房间/家具轮廓、CAD 哈希与模型绑定；重新导出 IFC 比较几何、材质和源信息；IES 固定为合成 1000 lm / 10 W。
- 自动导入 IFC，选择“新方法”、保留模型反射率；实际房间和家具出现在项目树，房间高度 2.8 m，PDF 面积为 24 m²。
- 归档本次专用进程的原始 `ifc-import-report.txt`。内容只有版本信息，没有警告或错误；原始报告不替代几何和结果核对。
- 自动导入 IES、创建单灯、数值设置并回读位置 `(3, 2, 2.5) m` 与零旋转，核对场景一盏灯、100% 调光。
- 工作面高 0.8 m、边缘区 0.5 m、维护系数 0.8；报告树核对工作面为自适应直角照度。明确设置并回读“无日光”。
- 观察真实计算启动/结束，结果为 `23.2 lx / U₀ 0.46`；工作面/房间功率密度 `0.67 / 0.42 W/m²`。
- 保存原生工程，导出两页 PDF 与新 400 × 300 光线追踪图；切换到本次未计算工程，再重开最终工程，结果和文件哈希一致。
- 成功尝试的 PDF 已逐页渲染检查，光线追踪图已视觉核对。PDF 有 Poppler 字体类型警告，页面正常可读。
- 已完成任务再次调用 `run` 只校验输入和输出哈希，尝试次数保持 2，不再次计算。
- 全量回归 `uv run pytest -q` 为 **133 项通过**、4 个已有依赖弃用警告；最后的渲染视图调整后，执行器相关 46 项测试通过。

| 成果 | SHA256 |
|---|---|
| 本次输入 `bridge.ifc` | `f07d49326ef45d92d737777dbf025cc1c0248431a64649534adabec0053d6c3a` |
| `project.evo` | `53b338468d92dec492d8a6b381b8d349ca35e8e6ced74f64579e5548fdc1e234` |
| `room-summary.pdf` | `d3dd4d85728493d64b0ec02aee2e77ac7305ea5399acec517d52b126316dae93` |
| `ifc-import-report.txt` | `98ff61b30881aaafd67a458bd6e60bcc024db6fdebe40ee71469b37bded66992` |
| `dialux-raytrace.jpg` | `ddc8699923cbd4ab729dcc8cb85ffd3978aea0a2152357c7ae9b323ae75dad22` |

源码仍与智能体/API 分离，成果目录被 Git 忽略。该验收只覆盖固定合成单房间，不构成任意项目、真实灯具设计或规范合规结论。

使用完全相同的执行器源码再次创建全新任务 `data/dialux-runs/import-repeat-20261006/`，运行 ID `54e29d9d4efe4e8ca5dc68b6fbc82813`，首次尝试 `attempt-001/` 全程自动通过，用时约 356 秒。导入报告仍无警告，照度/均匀度仍为 `23.2 lx / 0.46`，新 PDF、光线追踪图已视觉核对，原生工程已重开验证。再次调用该已完成任务只校验哈希，尝试数保持 1。原生文件 SHA256 为 `5704152fe2d61fe38571f583c7ddc9fde6f4d62d292aae29594c220dc7ee658d`，PDF SHA256 为 `57965aaea9ac3dce39610e436b9e9f05f97c5416bfe863872472aac7927d02d1`。

### 双房间 envelope profile 验收（2026-10-06）

独立配置 `evo-5.14.0.3-zh-synthetic-envelope-v1` 已完成一次全程自动实机验收。成果位于 `data/dialux-runs/envelope-index-acceptance-20261006/attempt-004/`，运行 ID 为 `cfe745d947ae4ae2acbcfd20136047bf`，状态为 `completed`。该尝试从 IFC/IES 输入创建全新双房间工程，不读取原生模板；前 3 次失败/探针尝试及 UIA 证据均保留，不计为成功。

- IFC4 导入报告归档为零警告、零错误；两个 `IfcSpace`、家具、关闭门和测试玻璃均出现在 DIALux 项目树。
- 两个矩形房间共墙只生成一次。门洞通过 `IfcOpeningElement` 宿主关系表达；默认门态为 `closed_door`，门扇尺寸 `1.000 m × 2.100 m`，实读位置约 `(6.080, 2.000, 1.050) m`。开放通道只作为显式 `open_passage` 控制，不创建门扇。
- 玻璃使用显式厚度 `8 mm`、反射率 `10%`、可见光透射率 `70%`、折射率 `1.520`，在导入、涂刷和重开后均回读一致。
- 两盏合成 IES 均为 `1000 lm / 10 W`，位置分别为房间 A `(3, 2, 2.5) m`、房间 B `(9.12, 2, 2.5) m`；场景为人工照明、两盏灯均 100% 调光，明确选择并回读“无日光”。
- 真实计算和 PDF 结果一致：房间 A 为 `22.7 lx / U₀ 0.47 / WP2`，房间 B 为 `23.3 lx / U₀ 0.48 / WP1`。两个工作面树绑定文件分别记录房间、场景和自适应直角照度计算面。
- 已保存 `project.evo`，导出两份房间摘要 PDF 和光线追踪 JPEG；切换到 `working.evo` 后重新打开归档工程，两个房间结果、门材质和玻璃光学参数仍一致，归档工程哈希未被读回过程改写。
- 两份 PDF 已用 Poppler 渲染并视觉核对，页面可读；渲染过程仅出现已知嵌入字体类型警告，未出现缺页或缺字。
- `results.json`、`state.json` 和全部成果哈希均已写入；该 profile 的结果合规状态仍为 `not_evaluated`，因为未绑定用户确认的规范目标。
- 全量离线回归 `uv run pytest -q` 为 **170 项通过**，4 个已有依赖弃用警告。

| 成果 | SHA256 |
|---|---|
| `project.evo` | `ca9edbb0817826743e835f44f65b1a2711888b44f65886326bd8b70c4abff14e` |
| `room-1-summary.pdf` | `8092e843ed99ec9ee038c918580524b4055b8472922b0bf20d3575234b2c95f8` |
| `room-2-summary.pdf` | `c36706749b06c92461e5ea89b2c867434f2f4d96f0cce917366738a2f76fb05b` |
| `ifc-import-report.txt` | `94c708a2e808e145192b2b48ac9a9567948df986286859e9966995807cb765ad` |
| `dialux-raytrace.jpg` | `934047e76121684e88e67427da4b68de56bdcfa85c20b2ab76f3c01f5fc31e91` |

本次验收证明固定合成双房间 profile 的独立执行链可重复完成；不代表任意真实 CAD、任意 IES、复杂墙体、任意门窗状态或规则合规已完成，也未接入智能体/API。

### 当次合成验收的剩余边界

1. 将固定 IFC/IES 新建配置扩展为已确认真实工程、任意合格光度文件及更多灯具布局；持续核对尺寸、材料和计算设置。
2. 扩展数值导出和结果解析；已验证固定中文两页 PDF 摘要的严格解析。XML/CSV 尚未验证，不能宣称已有稳定结果 API。Excel 导出选项可见，字段结构尚未验收。
3. 当次合成 envelope 记录不覆盖真实 CAD、复杂墙体及更多房间；真实 CAD 单房间受控 profile 的后续验收见下文，双房间真实 CAD、门窗宿主和复杂墙体仍需单独验收。
4. 最后增加受运行次数和时间约束的布局迭代；每次迭代必须独立绑定输入、设置及真实结果。

原阶段 0 交互导入的报告仍未找回；新增配置已分别归档各次**本次导入**的原始报告，不将新报告追记为旧运行证据。

### 通用 DXF/DWG 智能体接入与单房间实机验证（2026-10-07）

通用任务已接入项目智能体工具和 HTTP API，仍采用“准备/展示假设与布局 → 用户确认 → 异步执行 → 状态与结果回收”。不支持的复杂几何会在就绪检查阶段阻断，不近似成矩形；本地 IES/LDT/ULD 或已保存的 DIALux 候选（由官方 `dial://` 链接自动缓存 ULD）均可作为真实计算输入。该接入不改变灯具目录的“发送到 DIALux”只是唤起导入流程这一边界。

`data/dialux-runs/generic-integration-20261006-v7/attempt-001/` 已在 DIALux `5.14.0.3` 完成端到端桌面实机运行。`state.json` 状态为 `completed`，以下步骤均记录为 `verified`：IFC 导入、工作面设置、IES 导入及单灯布置、无日光人工照明场景、真实计算、原生工程保存、PDF 导出、光线追踪导出、原生工程重开和结果复核。IFC4 原始导入报告为 **0 warning / 0 error**。

- 结果为平均照度 `23.2 lx`、均匀度 `U₀=0.46`；DIALux 报告目标为 `500 lx / 0.60`，两项均未达标。该合成 1000 lm 灯具仅用于验证执行链，不是可用设计或合规结论。
- 两页房间摘要 PDF 已用 Poppler 渲染并逐页检查，页面可读、无裁切或缺页；报告带有 DIALux 自身嵌入字体类型警告。光线追踪及计算结果图均已导出。
- `results.json` 确认 `real_calculation_completed`、`native_project_reopened`、`room_results_exported`、`fresh_raytrace_exported`、`daylight_disabled` 和 `photometry_read_back` 均为真。工程文件 SHA256 为 `da49360f59390b19d55bd52b225577edcb008a6acffef452ea86143eb702351f`，PDF SHA256 为 `c97c063c55404a2d7a2675514d6f255b68ccf7ee479e9f2ffd9fe0bca390b2e6`。
- 本轮完整测试 `uv run pytest -q` 为 **177 项通过**，4 条第三方依赖弃用警告；`compileall` 与 `git diff --check` 通过。
- 有界布局候选的生成、独立任务绑定、真实计算编排及硬约束评分已经实现；本次 v7 未启用多候选优化，因此仍需单独完成多候选真实 DIALux 验收。优化结果要求所有参与房间都提供所配置硬约束的指标，缺项会降低候选优先级；未达标时明确返回 `no_feasible_candidate`。
- 当前结果模式没有经验证的总输入功率字段，功率上限及减功率/减灯具数目标会在 readiness 阶段阻断；当前可运行的优化范围仅为满足平均照度和均匀度硬约束的位置候选比较，不声称优化能耗或灯具数量。

此次单房间验收不覆盖任意真实 DWG、任意厂家 IES/LDT/ULD、复杂墙体或通用多房间布局，也不代表结果达到规范目标。

### 真实 CAD 单房间通用 profile 验收（2026-10-07）

使用真实脱敏 DXF `tests/fixtures/phase0/sample-room.dxf` 验证通用 profile。解析器识别 6 个候选房间；人工核对保留候选 0（512 会议室，面积 `143.59453723933817 m²`），其余 5 个候选带排除原因保留在模型中。原始 CAD SHA256 为 `723e2ac6ddc361cad1a7ee4e3ba2d8db2a5495ddc77e0c5787930c7f392ddd02`。图纸中存在 1 个 `ACAD_TABLE` 注释实体，明确记录为不参与建筑几何；没有外部参照、未知几何错误或虚构家具/门窗。该候选是无孔正交阶梯形闭合轮廓，使用标准 IFC4 单房间导出器保留原始顶点，不近似成外包矩形。

任务目录为 `data/dialux-runs/real-cad-sample-20261007-v2/`，最终 `attempt-003` 状态为 `completed`。两次前置尝试分别暴露并修复了 DIALux 坐标显示精度和 PDF 面积显示精度问题；失败 attempt、截图、UI 请求/响应和原因均保留，恢复从不可变输入重新开始。最终全部步骤 `verified`：真实 CAD 派生 IFC4 导入、0 warning / 0 error 导入报告、工作面设置、IES 回读及单灯布置、无日光人工照明场景、真实计算、原生工程保存、PDF 导出、光线追踪导出、工程重开和结果复核。

最终结果为平均照度 `5.09 lx`、均匀度 `U₀=0.13`；DIALux PDF 内置目标为 `500 lx / 0.60`，因此该运行只证明真实 CAD 到 DIALux 的执行、结果绑定和可追溯归档，不构成规范合规或可交付设计结论。`results.json` 的 `compliance.status` 保持 `not_evaluated`，`daylight_disabled=true`。

关键成果哈希如下：

| 成果 | SHA256 |
|---|---|
| `inputs/source.dxf` | `723e2ac6ddc361cad1a7ee4e3ba2d8db2a5495ddc77e0c5787930c7f392ddd02` |
| `inputs/bridge.ifc` | `28f49d111e421bf325e9ae2dd7446218ab46b5bcc9c68a7be4da76f6ab94ce0d` |
| `attempt-003/project.evo` | `57d73cf31171b437573c164a1bede9d0e009db3f9463dc81f74517b87fba62a6` |
| `attempt-003/room-1-summary.pdf` | `2ec48ecca3dee5adcbf7308559538c90431dee1f2e283b9740b6df46280e2fb1` |
| `attempt-003/ifc-import-report.txt` | `2d700bd26c8b5167fb276317a4e7be8f122c25a570d0898156c2f0ce9d4697d3` |
| `attempt-003/dialux-raytrace.jpg` | `98827903392e4f0782f925c41e8334c8fa8a9b5d6fb849e52271a48556dd6094` |
| `attempt-003/results.json` | `a0d3ffd29c1067a76f90d0a2f433091820437deb12591f9279b1701bc841e274` |

这次验收将目标 3 的真实 CAD 单房间 profile 从“待验收”推进为“已验收的受控范围”；任意 DWG、厂家光度文件、门窗宿主、多房间/复杂墙体和多候选优化仍需分别验收。
