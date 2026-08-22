$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$FILE = "f:\AIExp\docs\周报\项目周报_2026年8月第3周.docx"
$p = 0
$q1 = [string][char]0x201C
$q2 = [string][char]0x201D

function Add-Para {
    param([string]$Text, [string[]]$Props = @())
    $a = @("add", $FILE, "/body", "--type", "paragraph", "--prop", "text=$Text") + $Props
    & officecli @a
    $script:p++
    if ($LASTEXITCODE -ne 0) { throw "officecli add paragraph failed" }
}

# ---- 文档默认字体 ----
& officecli set $FILE / --prop docDefaults.font=微软雅黑 --prop docDefaults.fontSize=11pt
if ($LASTEXITCODE -ne 0) { throw "set defaults failed" }

# ---- 标题 ----
Add-Para "项目周报" @("--prop", "size=24pt", "--prop", "bold=true", "--prop", "align=center", "--prop", "spaceAfter=6pt")
Add-Para "汇报周期：2026年8月17日—8月23日" @("--prop", "align=center", "--prop", "spaceAfter=8pt")
Add-Para "本周重点：完成阶段0离线基线测试与脱敏fixture，新增室内照明常用模板与RAG补参证据修复；发布《照明设计智能体可行性评估与实施方案》和《从零启动教程》，明确$q1可审计室内照明设计工作台$q2的产品定位与分阶段实施路线。" @("--prop", "bold=true", "--prop", "spaceAfter=10pt")

# ---- 本周工作及产出 ----
Add-Para "本周工作及产出" @("--prop", "size=18pt", "--prop", "bold=true", "--prop", "spaceBefore=10pt", "--prop", "spaceAfter=6pt")
Add-Para "阶段0离线基线（回归保障）：新增 tests/fixtures/phase0 脱敏基线，含 10 个脱敏项目输入（覆盖普通办公室、会议室、视频会议室三类模板）、脱敏 DXF 平面图、GB 50034-2024 标准资料快照，以及固定的灯具候选与仿真结果快照；基线测试完全离线运行，不访问真实 LLM、Chroma、DIALux 与外部网络。" @("--prop", "listStyle=bullet", "--prop", "spaceAfter=2pt", "--prop", "lineSpacing=1.15x")
Add-Para "基线回归用例：校验智能体工具清单与文档基线一致（16 个工具，确认 import_selected_luminaires_to_dialux 不在工具列表）、fixture 完整性与脱敏性（外部链接全部指向 example.invalid）、相同输入产出稳定可复现的 revision、DIALux 交接包与设计报告。" @("--prop", "listStyle=bullet", "--prop", "spaceAfter=2pt", "--prop", "lineSpacing=1.15x")
Add-Para "一键回归脚本：新增 scripts/verify-phase0.ps1，固定使用 local RAG 后端、本地嵌入模型缓存与独立 pytest 临时目录，规避 Windows 系统临时目录权限问题；当前全套 95 项测试通过。" @("--prop", "listStyle=bullet", "--prop", "spaceAfter=2pt", "--prop", "lineSpacing=1.15x")
Add-Para "室内照明常用模板：工作台新建项目支持按《建筑照明设计标准》（GB 50034-2024）预填任务书，内置普通办公室、会议室、视频会议室三类模板（照度 300/300/750 lx、色温 4000 K、显色指数 80、UGR 19、均匀度 0.6、LPD 6.5 W/m²），标注规范表号来源（表 5.3.2、4.5.1/4.5.2、6.3.5）；视频会议室 LPD 按会议室对照值预填并单独注明。" @("--prop", "listStyle=bullet", "--prop", "spaceAfter=2pt", "--prop", "lineSpacing=1.15x")
Add-Para "RAG 补参证据修复：当检索到的照明参数与任务书现有值相同时，也记录证据来源（lighting_parameter_sources），保证参数可追溯到出处；新增回归测试覆盖参数持久化与溯源、不覆盖手工确认值、同值也记录来源，以及提示词$q1先检索证据、后追问用户$q2的编排顺序。" @("--prop", "listStyle=bullet", "--prop", "spaceAfter=2pt", "--prop", "lineSpacing=1.15x")
Add-Para "嵌入模型本地化（衔接8月10—16日）：RAG 默认使用本地 Chroma 与 bge-small-zh-v1.5 嵌入模型，默认仅从本地缓存加载（LIGHTING_EMBEDDING_LOCAL_FILES_ONLY），避免运行时依赖外网；模型缓存目录可配置，首次下载支持国内镜像。" @("--prop", "listStyle=bullet", "--prop", "spaceAfter=2pt", "--prop", "lineSpacing=1.15x")
Add-Para "可行性评估与实施方案：发布《照明设计智能体可行性评估与实施方案》，内容包括行业形态判断、技术生态与接入建议、当前能力盘点（已具备/不能承诺）、产品定位$q1可审计室内照明设计工作台$q2、人机协作责任边界、推荐状态机与分层架构、阶段0—5分步实施计划、风险闸门与首个试点建议；并按评估结论修正 README 中的工具能力描述。" @("--prop", "listStyle=bullet", "--prop", "spaceAfter=2pt", "--prop", "lineSpacing=1.15x")
Add-Para "从零启动教程与工程整理：新增《从零启动教程》（uv/Node 安装、国内镜像下载模型、CLI 与 Web 启动验证、环境变量、常见问题排查、项目结构速览、Git 使用），修复教程中的合并冲突标记并补充离线安装提示；更新 README 与方案文档链接、整理文件结构、移除 diag_ssl.py、更新 .gitignore（pytest 临时目录与 uv 缓存）、添加 LICENSE。" @("--prop", "listStyle=bullet", "--prop", "spaceAfter=2pt", "--prop", "lineSpacing=1.15x")

# ---- 已实现能力总览 ----
Add-Para "已实现能力总览" @("--prop", "size=18pt", "--prop", "bold=true", "--prop", "spaceBefore=10pt", "--prop", "spaceAfter=6pt")
& officecli add $FILE /body --type table --prop rows=13 --prop cols=2 --prop width=100%
if ($LASTEXITCODE -ne 0) { throw "table add failed" }
& officecli set $FILE "/body/tbl[1]/tr[1]" --prop header=true --prop c1=能力模块 --prop c2=当前实现
& officecli set $FILE "/body/tbl[1]/tr[1]/tc[1]" --prop fill=1F4E79
& officecli set $FILE "/body/tbl[1]/tr[1]/tc[2]" --prop fill=1F4E79
& officecli set $FILE "/body/tbl[1]/tr[1]/tc[1]/p[1]/r[1]" --prop bold=true --prop color=FFFFFF --prop size=10.5pt
& officecli set $FILE "/body/tbl[1]/tr[1]/tc[2]/p[1]/r[1]" --prop bold=true --prop color=FFFFFF --prop size=10.5pt

$rows = @(
    @("项目事实与版本", "ProjectState + SQLite 持久化，不可变 revision 快照与写入冲突检查；聊天不是事实源，$q1谁在什么版本确认了什么$q2可追查"),
    @("资料入库与检索", "Chroma 向量检索（可切换 SQLite 关键词），.md/.txt/.docx/.pdf 语义检索，PDF 可对接 PaddleOCR，保留原文片段与定位信息"),
    @("照明参数自动补全", "检索照度、色温、Ra、UGR、U0、LPD 证据并写入任务书，记录证据来源（含同值场景），不覆盖手工确认值"),
    @("确定性初算与规则校核", "流明法初算（记录输入、假设与局限）；规则校核只用显式带证据来源的阈值，缺证据或观测值不虚报合格"),
    @("灯具检索与选型", "DIALux Luminaire Finder 两阶段查询（搜索列表+详情页），功率/IP/品牌条件过滤，字段缺失标注 incomplete；最终型号一次性确认"),
    @("配光资产归档", "ULD/IES/LDT 安全下载（防压缩炸弹、跨主机重定向）、解压归档、SHA-256 校验、按项目持久化，失败可重试"),
    @("CAD 平面图辅助", "DXF 上传（安装 ODA File Converter 后可扩展 DWG），提取单位、边界、文字与闭合房间边界，面积候选经确认后写入任务书"),
    @("DIALux 交接与回灌", "不可变 handoff_id 与输入快照 SHA-256 交接包；仿真结果结构化回灌，matched/mismatch 校验，条件变化自动标记 stale 并派生工作流状态"),
    @("交付物", "Markdown 设计报告（证据/输入/计算/规则/候选灯具/待确认事项/人工复核声明）与 DIALux evo 仿真交接包"),
    @("Web 工作台", "Next.js + FastAPI：智能对话（工具轨迹、LLM 重试提示、上下文占用）、项目概览、灯具选型、计算与交付面板"),
    @("离线与回归保障", "本地嵌入模型缓存、双检索后端；阶段0脱敏基线不访问外部服务，95 项测试通过，verify-phase0.ps1 一键回归"),
    @("常用模板", "按 GB 50034-2024 预填普通办公室、会议室、视频会议室任务书（照度、4000K、Ra 80、UGR 19、U0 0.6、LPD 6.5 W/m²）")
)
for ($r = 0; $r -lt $rows.Count; $r++) {
    $row = $r + 2
    & officecli set $FILE "/body/tbl[1]/tr[$row]" --prop c1=$($rows[$r][0]) --prop c2=$($rows[$r][1])
    & officecli set $FILE "/body/tbl[1]/tr[$row]/tc[1]/p[1]/r[1]" --prop size=10pt
    & officecli set $FILE "/body/tbl[1]/tr[$row]/tc[2]/p[1]/r[1]" --prop size=10pt
}

# ---- UI概览 ----
Add-Para "UI概览" @("--prop", "size=11pt", "--prop", "bold=true", "--prop", "spaceBefore=10pt", "--prop", "spaceAfter=4pt")
Add-Para "" @("--prop", "align=center", "--prop", "spaceAfter=2pt")
& officecli add $FILE "/body/p[$p]" --type picture --prop src="f:\AIExp\docs\周报\assets\overview.png" --prop width=15.5cm
if ($LASTEXITCODE -ne 0) { throw "picture 1 failed" }
Add-Para "图1 项目概览：设计链路、设计状态与待确认事项一屏展示" @("--prop", "align=center", "--prop", "size=9pt", "--prop", "color=595959", "--prop", "spaceAfter=10pt")
Add-Para "" @("--prop", "align=center", "--prop", "spaceAfter=2pt")
& officecli add $FILE "/body/p[$p]" --type picture --prop src="f:\AIExp\docs\周报\assets\chat.png" --prop width=15.5cm
if ($LASTEXITCODE -ne 0) { throw "picture 2 failed" }
Add-Para "图2 智能对话：会话消息、快速指令与工具执行调试面板" @("--prop", "align=center", "--prop", "size=9pt", "--prop", "color=595959", "--prop", "spaceAfter=10pt")

# ---- 需关注事项 ----
Add-Para "需关注事项" @("--prop", "size=11pt", "--prop", "bold=true", "--prop", "spaceBefore=10pt", "--prop", "spaceAfter=4pt")
Add-Para "阶段0基线是离线确定性回归，不覆盖真实 LLM 编排、Chroma 在线索引与 DIALux 在线接口，端到端表现仍需在试点项目中验证。" @("--prop", "listStyle=bullet", "--prop", "spaceAfter=2pt", "--prop", "lineSpacing=1.15x")
Add-Para "平面图上传仅支持 DXF（安装 ODA File Converter 后可扩展 DWG），输出面积/边界候选，几何与面积须经工程师确认后才写入任务书；不做比例校准与三维场景。" @("--prop", "listStyle=bullet", "--prop", "spaceAfter=2pt", "--prop", "lineSpacing=1.15x")
Add-Para "嵌入模型首次使用需先下载（默认仅读本地缓存），新机器离线安装需提前准备模型缓存或配置国内镜像。" @("--prop", "listStyle=bullet", "--prop", "spaceAfter=2pt", "--prop", "lineSpacing=1.15x")
Add-Para "DIALux 仿真回灌目前仅支持手动表单指标；PDF/CSV 自动解析、规则自动重跑与修订循环尚未实现，且只有 matched 结果才能作为项目结论。" @("--prop", "listStyle=bullet", "--prop", "spaceAfter=2pt", "--prop", "lineSpacing=1.15x")
Add-Para "dial:// 派发只代表文件已交给 DIALux Data Dispatcher，不等于 DIALux 工程导入成功，界面仍标注待人工确认。" @("--prop", "listStyle=bullet", "--prop", "spaceAfter=2pt", "--prop", "lineSpacing=1.15x")
Add-Para "在线资料抓取、审批与许可证治理尚未实现，纳入知识库的资料需人工筛选把关。" @("--prop", "listStyle=bullet", "--prop", "spaceAfter=2pt", "--prop", "lineSpacing=1.15x")

# ---- 下周计划 ----
Add-Para "下周计划" @("--prop", "size=18pt", "--prop", "bold=true", "--prop", "spaceBefore=10pt", "--prop", "spaceAfter=6pt")
Add-Para "启动阶段1$q1仿真结果回灌闭环$q2：完善 DIALux evo 结果导入与输入版本核验，研究仿真输出 PDF/CSV 的结构化解析，推进$q1仿真—校核—修订$q2循环落地。" @("--prop", "listStyle=bullet", "--prop", "spaceAfter=2pt", "--prop", "lineSpacing=1.15x")
Add-Para "开展阶段2预研：IES/LDT 配光解析与照度快速预览、热力图，辅助灯具比选。" @("--prop", "listStyle=bullet", "--prop", "spaceAfter=2pt", "--prop", "lineSpacing=1.15x")
Add-Para "继续打磨前端交互：重点优化对话与灯具选型体验，完善常用模板落单流程。" @("--prop", "listStyle=bullet", "--prop", "spaceAfter=2pt", "--prop", "lineSpacing=1.15x")
Add-Para "以517会议室等项目跑通$q1任务书→初算→校核→选型→交接→仿真→回灌$q2完整链路，收集试点问题并扩充脱敏基线用例。" @("--prop", "listStyle=bullet", "--prop", "spaceAfter=2pt", "--prop", "lineSpacing=1.15x")

# ---- 页脚 ----
& officecli add $FILE / --type footer --prop type=default --prop text="项目周报  |  第 " --prop align=center --prop size=9pt
& officecli add $FILE "/footer[1]/p[1]" --type field --prop fieldType=page
& officecli add $FILE "/footer[1]/p[1]" --type run --prop text=" 页"
& officecli set $FILE /settings --prop updateFields=true

Write-Host "DONE pCount=$p"