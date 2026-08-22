# AGENTS.md

可审计的室内照明设计智能体：LLM 只负责理解需求与编排工具，项目事实、计算、规则校核、候选灯具均保存为可追溯数据。

- 项目能力与命令：见 [README.md](README.md)
- 系统设计：见 [照明设计智能体方案](照明设计智能体方案.md)
- DIALux 搜索接口：见 [DIALux-Luminaire-Finder-API.md](DIALux-Luminaire-Finder-API.md)
- 开发历史与图纸解析陷阱：见 [docs/512会议室-开发记录.md](docs/512会议室-开发记录.md)

## 常用命令

```powershell
uv sync --group dev                        # 安装依赖（Python >= 3.14，含 torch/chromadb，较重）
uv run pytest tests/ -q --basetemp .pytest-basetemp   # Windows 必须加 --basetemp，否则系统临时目录权限报错
uv run python main.py --help               # CLI 入口（离线命令不需要 LLM key）
uv run uvicorn lighting_agent.web_api:app --reload    # 后端（也可省略，由 npm run dev 自动拉起）
cd web; npm install; npm run dev           # 前端（代理 /backend/* → 后端 /api/*；dev 脚本先保证后端就绪再启动，见 web/scripts/dev.ps1）
```

下载/更新嵌入模型时，本机直连 huggingface.co 会超时（WinError 10060），须走镜像：

```powershell
$env:HF_ENDPOINT = "https://hf-mirror.com"; $env:LIGHTING_EMBEDDING_LOCAL_FILES_ONLY = "false"
uv run python -c "from lighting_agent.rag import create_evidence_store; create_evidence_store()"
```

模型缓存于项目根目录 `.model-cache`；默认 `local_files_only=true`，缓存存在后离线可用。

## 架构分层

```
web_api.py (FastAPI 适配层，无业务逻辑) → tools.py (16 个 LangChain 工具，薄封装)
  ├─ project_store.py    ProjectState 版本化持久化 + 不可变 revision 快照
  ├─ rag.py              SQLite/Chroma 双后端证据检索
  ├─ dialux_api.py       DIALux 目录客户端（缓存/节流/熔断）
  ├─ deliverables.py     Markdown 报告 + DIALux 交接包 (ZIP)
  ├─ photometry_assets.py 配光文件存储
  └─ calculations/       确定性计算（流明法、规则校核），无 LLM 决策
agent.py (ReAct 编排) 只调用上述工具，不做计算
```

## 核心不变量（改代码前必读）

- **乐观锁纪律**：几乎所有写操作必须携带 `expected_revision`，不匹配抛 `RevisionConflictError` → API 409。仅 `append_luminaires` 是追加式（陈旧 revision 自动 rebase）。新增写工具先明确是"替换式"（严格锁）还是"追加式"（`tools.py` 中 `_update_at_latest_revision` 重试）。
- **`ProjectState` 是领域事实源**：所有字段经 `StrictModel`（`extra="forbid"`）校验。新增字段必须同步 [web/lib/types.ts](web/lib/types.ts)（snake_case）；删除/废弃字段要在 `model_validator(mode="before")` 加 drop（参考 `drop_legacy_scene` 先例），保证旧快照可加载。
- **不变量**：`selected_luminaire_ids ⊆ luminaires`；`set_selected_luminaires` 要求 `brief_validation == "matches"`；brief/最终灯具/图纸/候选变化 → 旧 `SimulationRun` 自动标 `stale`。
- **LLM 只编排不计算**：规则阈值必须来自显式 `RuleRequirement`（带 `evidence_id`）。修改 `agent.py` 的 `SYSTEM_PROMPT` 时保持 12 条硬约束，且不要重命名工具（前端按工具名映射步骤显示）。
- **证据纪律**：`evidence_id` 是 sha256 稳定块 ID，不要发明 ID；`apply_rag_lighting_parameters` 禁止覆盖人工/文档已确认的参数。
- **RAG 双后端都要测**：`local`（SQLite 打分）与 `chroma`（向量）行为不同。`_CHROMA_OPERATION_LOCK` 串行化 Chroma 操作（BGE tokenizer 非可重入，并发会 `RuntimeError: Already borrowed`）——不要移除。
- **NDJSON 流契约**：`/api/chat/stream` 的 `tool_start`/`tool_end` 必须成对（按 `call_id`）；空 name 的部分 tool-call 块必须丢弃；事件类型改动需同步 [web/lib/api.ts](web/lib/api.ts) 的 `ChatStreamEvent` 联合类型。

## 领域陷阱

- **DIALux 两阶段查询**：① `GET /{lang}/{skip}/{count}/search/query/a231?ft={keyword}[&bf={brandId}]`（`ft` 必须在 query 参数，放路径会 500）；② `GET /{lang}/article/{luminaireId}` 详情页。空关键词拒绝（返回随机结果）；`isRandom: true` 时触发中文→英文关键词回退。配光 ZIP 下载必须同源校验 + zip-bomb 防护。搜索候选只是候选，只有 `select_luminaires` 确认的型号才进交接包。
- **图纸单位**：DIALux 导出 DXF 的 `$INSUNITS` 不可信，`DLX_*` 专属图层**强制按米解释**（`floor_plan.py`）；墙线是分段网络，需 shapely `polygonize` 重构。解析结果是候选，须经 `set_floor_plan` 确认才写入 brief。
- **交接包校验**：`handoff_id`/`input_snapshot_sha256` 与 revision、最终灯具逐项比对，任一不符即 `mismatch`；只有 `matched` 结果才可称为项目结论。

## 测试约定

- 全部网络/LLM 用 fake 注入：`DialuxAPI(session=FakeSession)`、`TestClient(create_app(..., dialux_api=FakeDialux()))`、`monkeypatch.setattr(web_api, "build_agent", ...)`。**绝不触发真实网络或真实 Chroma 模型**（用 `object.__new__(ChromaEvidenceStore)` 挂假 vector store）。
- 测试一律 `tmp_path` 隔离，按特性命名 `test_<feature>.py`。

## 安全

- [config.py](src/lighting_agent/config.py) 中 `LIGHTING_LLM_API_KEY` 有硬编码默认密钥：**不要打印、不要写入日志或测试，不要提交到仓库**。除非明确要求，不要改成占位符（现有部署依赖该默认值）。
- 前端只认 `/backend/*` 代理路径；CORS 白名单仅 localhost:3000。新增端点记得在 `api.ts` 加方法、`types.ts` 加类型。
- 删除项目会级联删除 revision 快照、聊天会话、`.photometry/`、`.plans/` 与生成文件——改动删除逻辑时注意副作用。

## 约定

- 界面文案与错误消息全中文；代码注释中英混合。
- `web_api.py` 只是适配层，不要放入业务逻辑。
- 工具 `layout_luminaires`（布灯方案生成）**尚未实现**，不要假定存在。
