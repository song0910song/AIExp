# LuxBeyond · 照明设计知识工作台

LuxBeyond 是**照明设计编排工作台**：中心对话逐步推进任务，项目文件、图纸范围和设计资产在随时可开的项目工作区中查看。

1. 上传 DXF 或 DWG，视觉模型分析图面文字、房间用途、尺寸和门窗/家具。系统在项目工作区叠加显示原图与空间范围，用户只需确认本次涉及的房间和明显边界修正；CAD 内部图层/实体代号不会要求业主填写。无法从图面判断的必要事项会在聊天中以可填写表格逐项确认。DWG 解析依赖本机 ODA File Converter。
2. 上传项目资料或全局规范，在项目工作区逐页查看 OCR 文本、表格和来源位置。标准版本、规则范围、设计假设与待确认决定由智能体分阶段整理；标准数值及适用性需依原文证据核对。扫描 PDF 继续使用外部 OCR，失败页明确保留待核实状态。
3. 询问照明设计知识、检索 DIALux Luminaire Finder；候选保存在当前项目，在会话中的候选列表里点击发送，通过本机 `dial://` 协议打开灯具导入。
4. 助手逐步执行设计编排并展示易懂的进度；有真正影响方案的歧义时，提供预填分析、推荐项和用户选择，不把 CAD 坐标、反射率或网格等专业字段推给业主。

发送灯具只是唤起导入对话框；**不会驱动 DIALux 建模、计算或生成仿真结果**。已移除流明法、配光预览、重设计、自算报告和 DIALux 结果导入。历史项目中的原有记录仍保存在数据库，当前工作台不展示或修改这些数据。

目标 1、2 的详细操作、版本行为和验收边界见 [CAD 与规范规则使用说明](docs/goals-1-2-guide.md)。文件读取完整不等于设计信息齐备；规则绑定不等于仿真合规。更换 CAD 或修改空间模型/规则后，旧绑定与结果关联会失效，不覆盖历史修订记录。

图纸视觉分析默认使用配置好的 `LIGHTING_LLM_MODEL`（如不支持图片，可设置 `LIGHTING_VISION_MODEL`）；发送给视觉服务的是本机生成的图纸 PNG 预览与必要图面文字，不是原始 DXF 文件。请按项目资料安全要求选择已获授权的模型服务。

## 启动

要求 Python 3.14+、Node.js 20+、uv。初次安装：

```powershell
uv sync --group dev
cd web
npm install
```

在项目根目录配置 `.env`：

```dotenv
LIGHTING_LLM_API_KEY=your-key
LIGHTING_LLM_MODEL=your-model
LIGHTING_LLM_BASE_URL=https://your-compatible-endpoint/v1
# 可选：图纸视觉分析模型；不设置时复用对话模型（须支持图片输入）
LIGHTING_VISION_MODEL=your-vision-model
LIGHTING_LLM_REASONING_EFFORTS=none,low,medium,high
LIGHTING_LLM_REASONING_EFFORT_DEFAULT=medium
# 默认启用隐式缓存；若当前兼容网关不接受缓存参数，可设为 false
LIGHTING_LLM_PROMPT_CACHE_ENABLED=true
LIGHTING_LLM_PROMPT_CACHE_KEY=lighting-design-agent-v1
LIGHTING_LLM_PROMPT_CACHE_TTL=30m
# 可选：填写当前模型的真实上下文容量；未配置时按 128000 token 估算占比
LIGHTING_LLM_CONTEXT_WINDOW_TOKENS=128000
# 可选：本机 DWG 转换
ODA_FILE_CONVERTER_PATH=C:\path\to\ODAFileConverter.exe
# 可选：无需嵌入模型时使用本地关键词检索
LIGHTING_RAG_BACKEND=local
# 可选：沿用外部 PaddleOCR 兼容服务（仅上传需要识别的单页 PDF）
PADDLEOCR_API_URL=https://your-ocr-service/ocr/jobs
```

工作台：

Next 上传代理默认等待 15 分钟。需要调整时，在前端进程的环境变量或 `web/.env.local` 中设置 `LIGHTING_UPLOAD_PROXY_TIMEOUT_MS`（毫秒），不要仅写在后端根目录的 `.env`。

```powershell
cd web
npm run dev
```

打开 http://localhost:3000。启动脚本会自动拉起 FastAPI；API 文档为 http://127.0.0.1:8000/docs。项目数据库保存在 `data/lighting_design.sqlite3`，项目图纸和资料统一保存在 `data/projects`。灯具发送到 DIALux 需要后端与装有 DIALux 的 Windows 桌面运行在同一台机器上。兼容网关是否接受缓存扩展参数需按网关协议配置。

CLI 可使用 `uv run python main.py --help` 查看 `analyze-cad`、`add-document`、`search-evidence` 和 `search-luminaires` 等命令。

## 验证

```powershell
uv run pytest tests/ -q
cd web
npm run typecheck
npm run build
```

外部 DIALux 网站和本机软件的真实连接需要在部署环境中单独验证；单元测试只验证 API 和协议调用。
