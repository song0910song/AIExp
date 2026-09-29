# LuxBeyond · 照明设计知识工作台

当前版本的主界面是**一个项目问答窗口**。在同一会话中：

1. 上传 DXF 或 DWG，查看二维轮廓与面积候选，确认房间边界后再用于后续提问。DWG 解析依赖本机 ODA File Converter。
2. 上传 PDF、DOCX、MD 或 TXT 到项目资料或公共规范库；直接在对话中提问，助手检索片段并注明来源。扫描 PDF 需要可用的 OCR 服务。
3. 询问照明设计知识、检索 DIALux Luminaire Finder；候选保存在当前项目，在会话中的候选列表里点击发送，通过本机 `dial://` 协议打开灯具导入。
4. 在输入区选择思考强度；模型提示词缓存由环境变量控制，页面显示当前开关状态。

发送灯具只是唤起导入对话框；**不会驱动 DIALux 建模、计算或生成仿真结果**。已移除流明法、配光预览、重设计、自算报告和 DIALux 结果导入。历史项目中的原有记录仍保存在数据库，当前工作台不展示或修改这些数据。

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
# 可选：使用兼容网关的隐式缓存；默认只对受支持的 OpenAI 模型自动启用
LIGHTING_LLM_PROMPT_CACHE_ENABLED=true
LIGHTING_LLM_PROMPT_CACHE_KEY=lighting-design-agent-v1
LIGHTING_LLM_PROMPT_CACHE_TTL=30m
# 可选：本机 DWG 转换
ODA_FILE_CONVERTER_PATH=C:\path\to\ODAFileConverter.exe
# 可选：无需嵌入模型时使用本地关键词检索
LIGHTING_RAG_BACKEND=local
```

工作台：

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
