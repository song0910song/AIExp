# LuxBeyond · 照明设计知识工作台

当前版本的主界面是**一个项目问答窗口**。在同一会话中：

1. 上传 DXF 或 DWG，在原图几何叠加视图中逐房间核对、校准尺度、拖动/编辑边界、补充门窗家具及缺失参数。保存带来源、确认状态和版本的多房间空间模型。DWG 解析依赖本机 ODA File Converter。
2. 上传项目资料或全局规范，逐页查看文本、OCR、表格和位置。登记不可变标准版本，从原文生成/补充候选规则，人工核对适用性与计算条件后绑定项目。扫描 PDF 继续使用外部 OCR，失败页明确保留待核实状态。
3. 询问照明设计知识、检索 DIALux Luminaire Finder；候选保存在当前项目，在会话中的候选列表里点击发送，通过本机 `dial://` 协议打开灯具导入。
4. 问答逐段输出并展示工具调用过程；输入区可选思考强度，显示最近一次请求的上下文窗口占比。模型提示词缓存默认开启，不占用对话界面空间。

发送灯具只是唤起导入对话框；**不会驱动 DIALux 建模、计算或生成仿真结果**。已移除流明法、配光预览、重设计、自算报告和 DIALux 结果导入。历史项目中的原有记录仍保存在数据库，当前工作台不展示或修改这些数据。

目标 1、2 的详细操作、版本行为和验收边界见 [CAD 与规范规则使用说明](docs/goals-1-2-guide.md)。文件读取完整不等于设计信息齐备；规则绑定不等于仿真合规。更换 CAD 或修改空间模型/规则后，旧绑定与结果关联会失效，不覆盖历史修订记录。

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

打开 http://localhost:3000。启动脚本会自动拉起 FastAPI；API 文档为 http://127.0.0.1:8000/docs。创建项目时会在选定文件夹下保存项目数据库与上传资料。灯具发送到 DIALux 需要后端与装有 DIALux 的 Windows 桌面运行在同一台机器上。兼容网关是否接受缓存扩展参数需按网关协议配置。

CLI 可使用 `uv run python main.py --help` 查看 `analyze-cad`、`add-document`、`search-evidence` 和 `search-luminaires` 等命令。

## 验证

```powershell
uv run pytest tests/ -q
cd web
npm run typecheck
npm run build
```

外部 DIALux 网站和本机软件的真实连接需要在部署环境中单独验证；单元测试只验证 API 和协议调用。
