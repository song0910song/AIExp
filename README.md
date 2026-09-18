# LuxBeyond · 室内照明设计智能体

一个**可审计、可追溯**的室内照明设计工作台。

它把需求理解、资料检索、照度初算、DIALux 验证和灯具选型串成一条工作流；项目事实、证据、计算结果和版本快照由程序保存，LLM 只负责对话与工具编排。

> 定位：辅助设计与 DIALux 交接，不替代专业照明软件的最终仿真和人工签发。

## 核心能力

| 模块 | 能做什么 |
| --- | --- |
| 项目管理 | 创建项目、保存项目级设计任务书、revision 版本和待确认事项 |
| 资料检索 | 将 Markdown、TXT、Word、PDF 建库，保留证据片段和来源位置 |
| 平面图解析 | 读取 DXF；安装 ODA File Converter 后可转换并读取 DWG，提取二维房间边界与标注 |
| 确定性初算 | 使用流明法计算所需光通量、灯具数量、估算照度和装机功率 |
| 联合检验 | 仅以照度为标准，流明法与当前版本的 DIALux 维持照度均达标才通过 |
| 灯具选型 | 查询 DIALux Luminaire Finder，补全型号、品牌、功率、IP、ULD 和配光信息 |
| 交付输出 | 生成 Markdown 报告草稿和 DIALux evo 交接包（ZIP） |
| 智能对话 | 通过 CLI 或 Web 工作台上传资料、确认条件并编排上述工具 |

## 当前验收口径

- 现阶段仅以照度作为计算、验证与迭代停止标准；UGR、显色指数、色温、功率等不参与本阶段通过/不通过判定。
- 流明法用于方案前置估算，DIALux evo 用于仿真复核。两者结果都不低于目标照度时，联合检验才通过。
- DIALux 证据可上传 PDF 设计报告或 PNG/JPG/WEBP 仿真图片。原文件、哈希、任务包和项目 revision 一并保存；图片必须经视觉模型识别 DIALux 身份、主要计算面和维持照度，PDF 仅从明确标注的结果字段提取。用户可填写人工校正值，但不能绕过图片视觉解析。
- 未达标时智能体继续调整可控方案并生成最新任务包；需要重新运行 DIALux 时暂停等待用户回传结果。达到目标，或用户明确说停止、结束、取消迭代时，终止循环。

## 快速开始

### 1. 安装后端依赖

要求：Python `>=3.14`、[uv](https://docs.astral.sh/uv/)。

```powershell
uv sync --group dev
```

### 2. 启动 Web 工作台（推荐）

要求：Node.js `>=20`。

```powershell
cd web
npm install       # 首次运行
npm run dev
```

浏览器打开 <http://localhost:3000>。`npm run dev` 会自动启动后端并等待健康检查；API 文档位于 <http://127.0.0.1:8000/docs>。

### 3. 使用 CLI

在项目根目录执行：

```powershell
# 创建项目
uv run python main.py init-project "会议室改造" `
  --space-type "会议室" --area-m2 30 `
  --target-lx 500 --target-cct-k 4000 --min-cri 80

# 查看项目
uv run python main.py show-project <project_id>

# 建库并检索资料
uv run python main.py add-document .\src\data\user_docs\GB-50034-2024.md --source-type standard
uv run python main.py search-evidence "会议室 照度 显色指数"

# 初步计算、查询灯具、生成交付物
uv run python main.py calculate <project_id> --revision 0 `
  --area-m2 30 --target-lx 500 `
  --lumens 3200 --power-w 24 --utilization-factor 0.6 --maintenance-factor 0.8
uv run python main.py search-luminaires "嵌入式 LED 筒灯" --target-cct-k 4000 --min-cri 80
uv run python main.py generate-report <project_id> --revision <revision>

# 需要 LLM 时使用
uv run python main.py chat --interactive
```

所有命令的完整参数可用以下命令查看：

```powershell
uv run python main.py --help
```

## 配置

在项目根目录创建 `.env`。离线命令不需要 LLM 密钥；`chat` 和 Web 智能对话需要配置网关。

```dotenv
LIGHTING_LLM_API_KEY=你的密钥
LIGHTING_LLM_MODEL=模型名称
LIGHTING_LLM_BASE_URL=https://你的网关地址/v1
# ODA 安装在非默认目录时配置完整可执行文件路径
ODA_FILE_CONVERTER_PATH=F:\oda\ODAFileConverter.exe
```

常用变量：

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `LIGHTING_LLM_API_KEY` | 无 | 智能对话密钥 |
| `LIGHTING_LLM_MODEL` | 无 | 对话模型 |
| `LIGHTING_LLM_BASE_URL` | 无 | OpenAI 兼容网关地址 |
| `LIGHTING_VISION_MODEL` | `LIGHTING_LLM_MODEL` | 解析 DIALux 仿真图片的多模态模型 |
| `LIGHTING_VISION_MIN_CONFIDENCE` | `0.7` | 自动采用图片照度读数的最低置信度 |
| `LIGHTING_VISION_TIMEOUT_SECONDS` | `60` | 单次视觉模型请求超时 |
| `LIGHTING_VISION_MAX_RETRIES` | `0` | 视觉模型失败后的最大重试次数 |
| `ODA_FILE_CONVERTER_PATH` | 自动探测 | ODA File Converter 的完整路径；用于自定义安装目录下的 DWG 转换 |
| `LIGHTING_RAG_BACKEND` | `chroma` | `local` 切换为 SQLite 关键词检索 |
| `LIGHTING_EMBEDDING_MODEL` | `BAAI/bge-small-zh-v1.5` | Chroma 嵌入模型 |
| `LIGHTING_EMBEDDING_CACHE_FOLDER` | `.model-cache` | 嵌入模型缓存目录 |
| `DIALUX_BASE_URL` | `https://luminaires.dialux.com` | DIALux 灯具目录地址 |
| `DIALUX_TIMEOUT_SECONDS` | `15` | DIALux 请求超时 |
| `PADDLEOCR_API_URL` | 见 `config.py` | PDF OCR 作业端点 |

首次使用 Chroma 前需要下载嵌入模型。网络受限时可使用镜像：

```powershell
$env:HF_ENDPOINT = "https://hf-mirror.com"
$env:LIGHTING_EMBEDDING_LOCAL_FILES_ONLY = "false"
uv run python -c "from lighting_agent.rag import create_evidence_store; create_evidence_store()"
```

不需要语义检索时，可临时使用本地关键词检索：

```powershell
$env:LIGHTING_RAG_BACKEND = "local"
```

完整配置清单见 [`src/lighting_agent/config.py`](src/lighting_agent/config.py)。

## 工作流边界

- CAD 仅解析**二维平面图**，不提供三维建模。
- DIALux Luminaire Finder 是灯具产品目录，不是仿真服务；搜索结果只是候选。
- 只有明确确认的最终灯具才会进入 DIALux 交接包并尝试下载配光文件。
- 本阶段的照度必须在 DIALux evo 或等效专业软件中复核；其他指标不参与当前验收。
- 仿真结果会与交接包的 `handoff_id` 和输入快照校验；输入发生变化时，旧结果会标记为过期。

## 数据位置

| 路径 | 内容 |
| --- | --- |
| `data/lighting_design.sqlite3` | CLI 使用的项目与全局资料库 |
| `data/projects/` | CLI 项目数据（运行时生成） |
| 工作区目录下的 `projects/<project_id>/` | Web 项目状态、聊天记录、资料和交付文件（每个项目独立存放） |
| `.model-cache/` | 嵌入模型缓存（运行时生成） |
| `src/data/user_docs/` | 可加入资料库的示例文档 |

项目状态以版本化 `ProjectState` 为准，更新使用 revision 乐观锁，避免旧会话覆盖新数据。计算和选型均基于项目级条件，不再维护照明分组。

## 验证

```powershell
uv run pytest tests/ -q --basetemp .pytest-basetemp
uv run python main.py --help
```

## 项目结构

```text
src/lighting_agent/
  agent.py              # LLM 对话与工具编排
  tools.py              # 项目、检索、计算、选型和交付工具
  project_store.py      # ProjectState 版本化持久化
  rag.py                # Chroma / SQLite 证据检索
  floor_plan.py         # DXF / DWG 二维平面图解析
  dialux_api.py         # DIALux Luminaire Finder 客户端
  deliverables.py       # 报告与 DIALux 交接包
  web_api.py            # FastAPI 接口
  calculations/         # 流明法与规则校核
web/                    # Next.js 工作台
tests/                  # pytest 测试
main.py                 # CLI 入口
```

## 文档

- [从零启动教程](docs/从零启动教程.md)：环境准备、首次安装和常见问题
- [照明设计智能体方案](docs/照明设计智能体方案.md)：系统设计与数据模型
- [DIALux Luminaire Finder API](docs/DIALux-Luminaire-Finder-API.md)：目录接口与字段说明
- [灯具坐标与照明布局分析方案](docs/灯具坐标与照明布局分析方案.md)：平面图分析方案

## License

见 [LICENSE](LICENSE)。
