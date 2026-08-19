# 室内照明设计智能体

一个可审计的室内照明设计助手：LLM 只负责理解需求、追问与编排工具；项目事实、初步计算、规则校核和候选灯具均保存为可追溯数据。

## 已实现的能力

- 版本化 `ProjectState`：设计任务书、证据、计算、规则校核、候选灯具与待确认事项存放在 `data/projects/`，写入时进行 revision 冲突检查。
- 资料入库与检索：使用 Chroma 向量数据库管理 `.md`、`.txt`、`.docx`、`.pdf` 的语义检索；扫描 PDF 会提交给已配置的 PaddleOCR 作业端点。保留原文片段及定位信息。
- 确定性预计算：流明法 `N = E × A / (Φ × UF × MF)`，输出输入条件、假设与局限；规则校核只比较显式、带证据来源的阈值。
- CAD 平面图：支持上传 `.dxf`，并在本机安装 ODA File Converter 时支持 `.dwg`；提取单位、边界、文字标注与闭合房间边界。经确认的边界会将面积、长宽写入版本化任务书，供后续计算与选型使用。
- DIALux Luminaire Finder：搜索 JSON 列表、补取产品详情页、标准化型号/品牌/功率/IP/详情链接/ULD 与配光下载标记，并声明字段缺失。
- 交付草稿：可生成包含证据、输入、计算、规则状态、候选灯具、待确认事项与人工复核声明的 Markdown 报告，以及 DIALux evo 仿真交接包；任务包和配光文件只使用项目中明确确认的最终灯具。
- `create_agent`：提供项目、检索、计算、校核、灯具查询和 DIALux 交接包工具。没有 `LIGHTING_LLM_API_KEY` 时，离线命令仍可正常使用。

## 安装与配置

```powershell
uv sync --group dev
$env:LIGHTING_LLM_API_KEY = "..."       # 仅 chat 命令需要
$env:LIGHTING_LLM_MODEL = "deepseek-v4-flash"  # 可选
```

默认安装包含 Chroma + `BAAI/bge-small-zh-v1.5` 语义检索依赖。首次安装后运行 `uv sync --group dev` 即可使用默认的 Chroma 后端。

如需临时使用 SQLite 关键词检索，可显式设置 `$env:LIGHTING_RAG_BACKEND = "local"`。

嵌入模型默认仅从已下载的本地缓存加载，避免运行时依赖外网；首次下载模型时可临时设置
`$env:LIGHTING_EMBEDDING_LOCAL_FILES_ONLY = "false"`。默认缓存目录为项目根目录的
`.model-cache`，可用 `LIGHTING_EMBEDDING_CACHE_FOLDER` 覆盖。

可选配置项：`LIGHTING_LLM_BASE_URL`、`LIGHTING_LLM_TEMPERATURE`、`LIGHTING_LLM_CONTEXT_WINDOW_TOKENS`（默认 `1000000`，用于模型实际返回 token 的窗口占用比例）、`DIALUX_BASE_URL`、`DIALUX_TIMEOUT_SECONDS`、`PADDLEOCR_API_URL`、`PADDLEOCR_MODEL`、`PADDLEOCR_TIMEOUT_SECONDS`。聊天流会请求模型返回 usage；若所用网关不支持该字段，界面会明确显示“模型未返回用量”，不会显示估算值。

## 快速开始

```powershell
uv run python main.py init-project "会议室改造" --space-type "会议室" --area-m2 30 --mounting-height-m 2.7 --target-lx 500 --target-cct-k 4000 --min-cri 80
uv run python main.py show-project <project_id>
uv run python main.py add-document .\src\data\user_docs\GB-50034-2024.md --source-type standard
uv run python main.py search-evidence "会议室 照度 显色指数"
uv run python main.py calculate <project_id> --revision 0 --area-m2 30 --target-lx 500 --lumens 3200 --power-w 24 --utilization-factor 0.6 --maintenance-factor 0.8
uv run python main.py search-luminaires "嵌入式 LED 筒灯" --target-cct-k 4000 --min-cri 80 --max-power-w 25
uv run python main.py create-dialux-task <project_id> --revision 1
uv run python main.py import-dialux-result <project_id> --revision 1 --handoff-id <handoff_id> --maintained-lx 750 --uniformity-u0 0.6
uv run python main.py generate-report <project_id> --revision 1
uv run python main.py chat "为刚才的会议室建立选灯条件"
uv run python main.py chat --interactive
```

Web 工作台：

```powershell
cd web
npm install
npm run dev        # 自动拉起后端并等待 /api/health 就绪后再启动前端
```

也可手动先启动后端（脚本检测到后端已就绪会直接复用）：

```powershell
uv run uvicorn lighting_agent.web_api:app --reload
cd web
npm run dev
```


连续聊天会在一个进程内保留消息历史；输入 `exit`、`quit` 或 `退出` 结束。项目事实仍以 `ProjectState` 为准，长期项目请在会话中让智能体读取对应 `project_id`。

## DIALux 调用边界

接口使用两阶段查询：先请求 `/{lang}/{skip}/{count}/search/query/a231?ft={keyword}` 的 JSON 列表，再请求 `/{lang}/article/{luminaireId}` 获取详情页补充字段。品牌筛选使用 DIALux 返回的 `brandId` 作为 `bf` 参数。该形式已于 2026-07-27 完成只读在线验证。

基本搜索接口稳定支持关键词、分页、品牌筛选；搜索条件以目标照度、色温、显色指数（Ra）和 UGR 为主（从已确认任务书自动采用），功率、IP、品牌等其它条件仅在用户明确说明时加入。色温、显色指数、UGR 等条件会在详情数据可用时参与匹配，缺少字段的候选会标记为 `incomplete`，不会被描述为“符合要求”。应避免空关键词，因为 DIALux 会返回随机结果。

Luminaire Finder 是产品目录，不是项目仿真服务。搜索得到的型号只是候选；在 Chat 或 Agent 中确认最终型号后（房间通常由多款灯具组合，最终选定不限于单款），智能体会调用 `select_luminaires` 一次性写入全部最终型号。只有这些最终选定灯具的 ULD/配光文件和详情链接会被装入 DIALux 交接包，未选定的搜索候选不会下载。系统不会直接向本机 DIALux 导入灯具；维持照度、均匀度、UGR、反射比和布灯方案仍须在 DIALux evo 或等效软件中手动完成核验。

## DIALux 结果回灌（第一阶段）

交接包会生成不可变 `handoff_id` 与 `input_snapshot_sha256`（写入 `dialux-task.json` / `manifest.json`）。用户在 DIALux evo 完成仿真后，可导入结构化结果：

```text
POST /api/projects/{project_id}/dialux-results
{
  "expected_revision": 1,
  "handoff_id": "handoff-...",
  "input_snapshot_sha256": "...",
  "source_kind": "manual_form",
  "metrics": { "maintained_illuminance_lx": 750, "minimum_illuminance_lx": 450,
               "uniformity_u0": 0.6, "ugr": 19, "installed_power_density_w_m2": 9 }
}
```

系统将结果与当前交接包校验，返回 `matched` / `mismatch` 并写入可验证 `SimulationRun`。只有 `matched` 结果才可称为本项目结论；任务书、最终灯具或图纸变化会使旧结果自动标记为 `stale`，并派生项目工作流状态（`simulation_pending` / `simulation_verified` / `needs_revision` 等）。查询端点：

```text
GET /api/projects/{project_id}/dialux-results
GET /api/projects/{project_id}/dialux-results/{run_id}
```

有关系统设计与 API 字段，见 [照明设计智能体方案](照明设计智能体方案.md) 和 [DIALux API 文档](DIALux-Luminaire-Finder-API.md)。

## 验证

```powershell
uv run pytest
uv run python main.py --help
```

## SQLite 持久化

项目当前状态、不可变 revision 快照、RAG 证据块和聊天会话均保存到
`data/lighting_design.sqlite3`。首次启动时会自动导入旧的
`data/projects/*.json` 与旧版 `data/rag/index.json`（若存在）；原始 JSON 不会被删除，
新安装不会创建 `data/rag/` 目录。

检索结果的 `evidence_id` 是稳定证据块 ID。需要将其写入项目、并使其
进入设计报告时，调用：

```text
POST /api/projects/{project_id}/evidence
{
  "expected_revision": 3,
  "evidence_ids": ["..."]
}
```

使用 `GET /api/projects/{project_id}/revisions` 可读取全部不可变版本快照。

聊天会话会在 SQLite 中保存，并由 `LIGHTING_CHAT_SESSION_TTL_HOURS`
（默认 168）和 `LIGHTING_CHAT_SESSION_MAX_MESSAGES`（默认 80）控制保留期限与历史长度。
