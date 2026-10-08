# DIALux 独立执行器

此组件通过 `lighting-dialux-runner` 或 `uv run -m lighting_agent.dialux_runner` 独立运行。通用任务已经由项目智能体和 HTTP API 编排；本机执行器仍保持独立，便于单独验证和人工接管。

## 重大变更：CAD 模型自主判断与自动启动

自 2026-10-08 起，真实 CAD 主链不再要求人工选择候选房间、确认空间模型或确认默认假设。视觉/语言模型根据 CAD 预览、解析几何、文字/尺寸证据和项目上下文自动决定纳入与排除的空间及构件，生成结构化判断、证据定位、置信度和假设；确定性几何/IFC 校验通过后，系统自动生成 IFC、准备任务并启动 DIALux 真实计算。

自动运行不得把低置信度或冲突伪装成确定结论。输入无法解析、单位无法换算、模型结论冲突、几何自交/重叠、楼层不一致、门窗宿主不唯一、IFC 导入出现未批准的警告/跳过对象，或桌面步骤无法验证时，系统自动停止并进入 `needs_attention`，保存诊断和证据；不请求执行前人工确认，也不自动近似复杂几何。源模型变化时判定、IFC 和任务全部失效并重新运行分析。

自动主链状态为 `cad_ingested → model_analyzing → model_validated → ifc_generated → dialux_prepared → dialux_running → completed`。每次任务不可变地绑定 CAD/模型/判断/假设/IFC/光度文件哈希及 DIALux 版本；启动采用幂等锁，避免重复创建工程或重复触发计算。人工介入仅用于 `needs_attention` 接管和事后结果复核。

## 当前范围

用户已于 2026-10-06 确认：目标 3 只计算人工照明，不包含自然光计算。

目前限定 Windows、DIALux 文件版本 `5.14.0.3`、中文界面。执行器包含固定合成 envelope profile，以及受控的通用 DXF/DWG profile：真实 CAD 的空间纳入/排除及语义由模型自动判断，单楼层、无孔、正交闭合模型通过校验后自动执行；源 CAD 哈希和模型判定均进入任务绑定。曲线、孔洞、斜墙、多楼层、重叠边界和无法唯一确定门窗宿主的模型自动阻断，不会自动近似。原生重算配置把已验收工程复制到每次尝试的独立目录，重新计算并回收真实输出。

原生重算配置的执行顺序：

1. 校验输入文件哈希、已验收原生工程及版本绑定，获取当前 Windows 会话的互斥锁。
2. 启动专用 DIALux 进程，检查进程身份、软件版本、桌面可访问性和工程路径。
3. 核对房间/场景/工作面，设置完整计算选项，清除副本中的既有结果。
4. 触发真实计算，观察计算启动及结束，读取本次结果。
5. 保存新 `.evo`，导出房间摘要 PDF，逐字段解析并对照计算面、场景、维护系数、反射率等条件。
6. 重新运行 DIALux 光线追踪并导出 JPEG。
7. 切换到另一工程后重新打开本次 `.evo`，核对结果仍一致，最后写入成果哈希。

原生重算配置通过已验收原生模板固定几何、IES、布灯和计算面设置，模板文件哈希固定。IFC/IES 配置限定为固定合成双房间、共墙、关闭不透明门、测试玻璃和两灯布局；通用 profile 的 IFC 由模型自主判断且通过几何校验的 CAD 空间模型生成，单房间正交轮廓使用标准 IFC 导出器。编辑 job 中的布局/条件不能代替实际修改 DIALux 工程；扩展配置须有独立实机验收。

## 运行

### 从 IFC/IES 创建合成双房间 envelope 工程

新增 `prepare-envelope` 独立配置 `evo-5.14.0.3-zh-synthetic-envelope-v1`，使用下列命令准备任务：

```powershell
uv run scripts/prepare_dialux_envelope.py --output-dir data/dialux-runs/new-envelope-inputs
uv run -m lighting_agent.dialux_runner prepare-envelope --source data/dialux-runs/new-envelope-inputs --output data/dialux-runs/new-envelope-job --dialux F:/dlalux/DIALux_x64.exe
uv run -m lighting_agent.dialux_runner run data/dialux-runs/new-envelope-job
```

输入包和任务目录都必须尚不存在。此配置只接受固定双房间合成模型及合成 IES；校验 CAD 哈希、完整模型字段，以及按同一模型和导出假设重新生成的 IFC 实体内容。比较 IFC 时只忽略生成的 GUID、头部时间和无序集合的成员顺序，保留几何、材质及源信息。不会读取 `.evo` 模板。

新工程通过 IFC 导入向导选择“新方法”，关闭默认反射率覆盖，归档本次专用进程生成的原始 `ifc-import-report.txt`。报告必须对应 `bridge.ifc`、IFC4、DIALux `5.14.0.3`，任何跳过对象、其他警告或未知消息都会停止执行。报告本身不代替房间、家具和结果核对。

任务设置固定为 `lighting_mode=artificial_only`、`daylight=false`；执行时选择并回读“无日光”。当前默认门处理为关闭、普通不透明门扇；开放通道必须通过 `--passage` 显式生成 `open_passage`，不表示铰链门扇打开。玻璃必须显式绑定厚度、可见光透射率和折射率。布灯创建两个对象并分别回读位置、旋转、1000 lm、10 W 与 100% 调光。工作面沿用并核验 DIALux 的自适应直角照度计算；每个房间的 `room-N-workplane-grid.json` 绑定实际报告树中的房间、场景和计算面。此配置仍用于固定合成几何，不能通过手工编辑 job 扩大范围。

### 使用真实 DXF/DWG 房间

`prepare-real-cad` 会先重新读取原始 DXF/DWG，再由模型分析全部空间候选、图面文字和渲染图，自动选择本次设计空间并为排除候选写明理由。原始候选均保留以供追溯；只有通过实体白名单与语义判断的注释实体（如 `ACAD_TABLE`）可排除在 IFC 几何之外。未知实体、外部参照、模型冲突和几何错误自动阻断任务并归档诊断，不要求用户预选候选。

```powershell
uv run -m lighting_agent.dialux_runner prepare-real-cad `
  --cad tests/fixtures/phase0/sample-room.dxf `
  --photometry tests/fixtures/phase0/synthetic-bridge-luminaire.ies `
  --output data/dialux-runs/real-cad-sample-20261007-v2 `
  --dialux F:/dlalux/DIALux_x64.exe `
  --project-name "Real CAD sample room" --project-id real-cad-sample `
  --model-analysis auto --analysis-model <configured-vision-model>
uv run -m lighting_agent.dialux_runner run data/dialux-runs/real-cad-sample-20261007-v2
```

任务包中的 `model_decisions`、模型/假设哈希、`source_cad_sha256`、`spatial-model.json` 和 `bridge.ifc` 共同绑定自动判断。任务通过验证后，CLI/API 自动启动 DIALux；IFC 导入、计算、PDF、光线追踪和重开结果仍按同一桌面验收链执行。

### 原生模板重算

需要可用的 Windows 交互桌面、已登录且具备对应功能的 DIALux，以及 PATH 中的 Poppler `pdftotext`。版本检查和实际打开/计算/导出用来验证能力；没有独立许可查询接口。

准备新任务，不启动桌面软件；输出目录必须不存在：

```powershell
uv run -m lighting_agent.dialux_runner prepare --source data/dialux-runs/phase0-20261005 --output data/dialux-runs/my-replay --dialux F:/dlalux/DIALux_x64.exe
```

执行与检查：

```powershell
uv run -m lighting_agent.dialux_runner run data/dialux-runs/my-replay
uv run -m lighting_agent.dialux_runner status data/dialux-runs/my-replay
```

任务运行期间执行器使用鼠标与前台窗口，应让专用实例保持可操作。已完成任务再次执行仅验证成果哈希，不启动新的计算。

## 超时、接管与恢复

桌面操作超时、选择器不唯一、未知弹窗、结果错配或文件缺失均进入 `needs_attention`。不会因为点击超时就重复点击。会保留专用进程、失败截图、UI 控件快照和本次尝试，供人工检查。

```powershell
uv run -m lighting_agent.dialux_runner pause data/dialux-runs/my-replay
```

暂停请求在下一步骤边界生效，不强制终止正在执行的 DIALux 计算。检查后关闭失败尝试对应的专用 DIALux 实例；若存在 `pause.request`，确认接管结束后删除该文件，再执行：

```powershell
uv run -m lighting_agent.dialux_runner run data/dialux-runs/my-replay --resume
```

恢复会创建新 `attempt-NNN`，从不可变输入重新开始，不续接状态不确定的桌面步骤。旧尝试和证据保留。任务或输入发生变化时拒绝恢复，必须准备新任务。

默认上限：启动 120 秒，普通等待 90 秒，计算/渲染各 300 秒，总运行 1200 秒。单条 PowerShell 命令最多 30 秒。恢复是显式操作；不会无限重试。

控件定位优先使用 AutomationId。DIALux 的 `Process.MainWindowHandle` 有时指向辅助窗口，因此根据进程、窗口类、标题及所属关系定位真正的主窗口，再绑定句柄和进程启动时间。弹层内的计算选项在同一 UIA 连接中完成设置与回读。原生文件对话框可能是主窗口的同级窗口，通过绑定进程、窗口标题、原生控件 ID 和 Win32 类定位，写入路径后先回读再提交。原生工程的后台保存可能晚于标题变更，因此最终保存还要等待磁盘文件确实更新并稳定，再核对重开前后的哈希。

## 输出与来源

| 文件 | 内容 |
|---|---|
| `job.json` / `inputs/` | 本次任务和输入快照，绑定 CAD/IFC/模型/IES/布局/条件版本 |
| `state.json` / `events.jsonl` | 步骤状态、失败原因、进程身份、尝试历史、执行器源码及成果 SHA256 |
| `attempt-NNN/commands/` | 每次 UIA 请求与响应 |
| `project.evo` | 本次计算保存的原生工程 |
| `room-N-summary.pdf` / `.txt` | 每个房间的 DIALux 原始 PDF 与 Poppler 文本 |
| `results.json` | 本次结构化数值及来源文件哈希、页码、原文、解析器版本 |
| `ifc-import-report.txt` / `.json` | IFC/IES 配置的本次 DIALux 原始导入报告及严格核验结果 |
| `artificial-scene.json` / `workplane-grid.json` | “无日光”选项回读及实际工作面计算类型 |
| `dialux-raytrace.jpg` | DIALux 本次光线追踪输出，当前模板为 400 × 300 |
| 截图与 UI JSON | 计算启动/完成、渲染启动/完成、工程重开等证据 |

结果解析严格限定已验收的两页中文房间摘要。实际值与目标值分列读取；工作面/房间功率密度分开保存；`RUG,max` 保留矩形房间/SHR 条件，不能冒充观察点 UGR。缺失、重复字段、其他语言/模板或条件不一致会停止回收。

PDF 内置办公室目标不是用户确认的规范，结构化结果的合规状态固定为 `not_evaluated`。不从 PDF 补造 Ra、CCT 或观察点眩光数据。XML/CSV 尚未验证；报告导出菜单可见 Excel 选项，但其字段结构也尚未验收。

## 已验收与边界

- 固定双房间 envelope profile 已完成 IFC 导入、共墙、门窗宿主、关闭门、显式玻璃光学参数、两灯人工照明计算、双 PDF、光线追踪和工程重开验收。
- 通用 profile 已通过一份真实 DXF 的单房间实机验收；它仍不是任意真实项目执行器。复杂墙体、非矩形房间、任意 CAD、任意厂家 IES、更多灯具和更多房间仍需单独验收。
- 真实 CAD 的候选核对、显式排除、源哈希、IFC 生成、确认规则和人工照明计算设置均有任务级绑定。
- 有次数/时间上限的布局优化与每次迭代独立计算。
- 通用 profile 已接入智能体/API 的准备、确认、启动和结果回收；独立 CLI 仍可单独执行同一验证链。

即使不计算自然光，门扇遮挡和玻璃透射仍会影响人工照明；当前 profile 的关闭门和玻璃参数是明确的合成测试假设，不应自动外推到真实项目。

2026-10-06 双房间验收位于 `data/dialux-runs/envelope-index-acceptance-20261006/attempt-004/`，运行 ID `cfe745d947ae4ae2acbcfd20136047bf`，状态 `completed`。结果为房间 A `22.7 lx / U₀ 0.47 / WP2`、房间 B `23.3 lx / U₀ 0.48 / WP1`；IFC 导入报告零警告，重开后结果和材质一致。两份 PDF 已渲染检查，光线追踪图已视觉检查；全量回归为 `170 passed`、4 个已有依赖弃用警告。详细证据与哈希见 [桥接验证记录](dialux-phase0-bridge-validation.md)。

## 智能体接入（通用 DXF/DWG profile）

智能体现在可以在项目上下文中创建并跟踪人工照明任务。接入流程为：

1. CAD 上传后，模型分析自动判定空间范围、语义及缺失参数，确定性校验生成可执行判定。
2. 判定通过后系统自动生成 IFC 与不可变任务包；任何校验失败均进入 `needs_attention`。
3. 任务绑定项目 CAD 哈希、模型判断/假设哈希、空间模型、IFC4、光度文件哈希、布局、计算设置和 DIALux 版本；灯具光度数据可使用本地 IES/LDT/ULD，或从官方 `dial://` 缓存 ULD。
4. 系统自动调用 `start_dialux_run`，执行器在独立 Windows 桌面会话中启动 DIALux；`get_dialux_run` 返回状态和已归档结果。

通用 profile 当前采用模型自动判断和确定性门控：闭合、无孔、正交轮廓（矩形或阶梯形）和单楼层模型由模型自动选入/排除并在校验通过后执行；曲线、孔洞、斜墙、多楼层、重叠边界或门窗无法唯一宿主时自动阻断并返回原因。阻断项不会被自动近似为矩形，也不会等待人工预确认后越过硬校验。

真实 CAD 扩展当前仍仅验收单房间无孔正交闭合轮廓（包括保留原始顶点的阶梯形轮廓），不把它强行变成外包矩形；门窗元素在未完成独立宿主验收前由规则引擎自动阻断该 profile。此限制与模型自主判断不冲突：模型只能决定空间语义和范围，不能授权执行器越过未验收的 IFC 几何能力。

布局优化是有界的真实计算流程。系统最多生成 8 个确定性候选，每个候选使用独立不可变任务包并由 DIALux 重新计算；平均照度和均匀度是硬约束，未找到可行候选时结果标记为 `no_feasible_candidate`。功率和灯具数量只有在 DIALux 返回可验证字段后才能作为次级目标，不能由几何估算冒充。

HTTP 接口当前包括：`/api/projects/{project_id}/dialux/readiness`、`/dialux/runs`、`/dialux/runs/{run_id}/confirm`、`/dialux/runs/{run_id}/start`、`/dialux/runs/{run_id}`，以及用于绑定光度文件的 `/api/projects/{project_id}/dialux/light-files`。自动化改造完成后，CAD 分析通过的主流程将由服务端自动准备并启动，不再经过 `/confirm` 人工闸门；旧接口仅保留兼容/诊断用途。产品目录的“发送到 DIALux”仍只是唤起本机导入，不等于已完成计算。

### 真实 CAD 验收记录（2026-10-07）

源文件为 `tests/fixtures/phase0/sample-room.dxf`，解析得到 6 个候选；旧验收运行由人工保留候选 0（512 会议室，`143.59453723933817 m²`），其余 5 个候选显式排除。DXF SHA256 为 `723e2ac6ddc361cad1a7ee4e3ba2d8db2a5495ddc77e0c5787930c7f392ddd02`；唯一未建模实体为 1 个 `ACAD_TABLE` 注释对象，不参与 IFC 几何。任务目录为 `data/dialux-runs/real-cad-sample-20261007-v2/`，最终 `attempt-003` 状态为 `completed`。该记录证明 CAD→IFC→DIALux 链路可工作，但不证明新模型自主判断和自动启动链已通过验收。

本次 DIALux `5.14.0.3` 实机链路全部 verified：IFC4 导入（0 warning / 0 error）、工作面设置、IES 回读与单灯布置、无日光人工照明场景、真实计算、原生工程保存、房间 PDF、光线追踪、工程重开和结果复核。结果为平均照度 `5.09 lx`、均匀度 `U₀=0.13`，DIALux 报告内置目标为 `500 lx / 0.60`，因此仅证明真实 CAD 到 DIALux 的可追溯执行链，不构成规范合规或可交付设计结论。

关键成果哈希：`bridge.ifc` `28f49d111e421bf325e9ae2dd7446218ab46b5bcc9c68a7be4da76f6ab94ce0d`；`project.evo` `57d73cf31171b437573c164a1bede9d0e009db3f9463dc81f74517b87fba62a6`；`room-1-summary.pdf` `2ec48ecca3dee5adcbf7308559538c90431dee1f2e283b9740b6df46280e2fb1`；`results.json` `a0d3ffd29c1067a76f90d0a2f433091820437deb12591f9279b1701bc841e274`。前两次尝试的失败原因和恢复证据保留在同一 `state.json`，没有复用状态不确定的桌面步骤。
