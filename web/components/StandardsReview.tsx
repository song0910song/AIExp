"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { DesignRule, DesignSettings, DocumentContent, DocumentRecord, Project, StandardRecord } from "@/lib/types";

const metrics = { illuminance: "维持平均照度", uniformity: "均匀度", ugr: "UGR", cri: "显色指数", cct: "色温", lpd: "功率密度" };
const number = (value: string) => value.trim() === "" ? null : Number(value);

export function DocumentPages({ document }: { document: DocumentContent }) {
  return <div className="document-pages">
    <p className={document.extraction_complete ? "muted" : "error-text"}>{document.extraction_complete ? "正文抽取完成" : "正文存在缺失/旧资料无定位，请核对或重新导入"}{document.review_required ? " · 含待核实项" : ""}</p>
    {document.pages?.length ? document.pages.map((page, i) => <details key={i}><summary>{page.locator} · {({ extracted: "原生文本", ocr_review: "外部 OCR 待复核", needs_review: "识别未完成", empty: "空白待核对" } as Record<string, string>)[page.status] ?? page.status}</summary>
      {page.warnings.map((warning, j) => <p className="error-text" key={j}>{warning}</p>)}
      <pre>{page.text || "无可用正文，不可作为规则通过依据"}</pre>
      {page.tables.map(table => <div className="review-table-scroll" key={table.table_id}><small>表 {table.table_id} · 位置 {table.bbox?.join(", ") ?? "待核对"}</small><table><tbody>{table.cells.map((row, r) => <tr key={r}>{row.map((cell, c) => <td key={c}>{cell}</td>)}</tr>)}</tbody></table></div>)}
      <details><summary>页面块位置 / 脚注原文</summary>{page.blocks.map(block => <p key={block.block_id}><small>{block.block_id} · {block.bbox?.join(", ") ?? "服务未返回可靠坐标"}</small><br />{block.text}</p>)}</details>
      {page.ocr_layout?.length ? <details><summary>OCR 服务原始布局坐标</summary>{page.ocr_layout.map(block => <p key={block.block_id}><small>{block.coordinate_space} · {block.source_bbox?.join(", ")}</small><br />{block.text}</p>)}</details> : null}
    </details>) : <pre>{document.content}</pre>}
  </div>;
}

export function StandardsReview({ project, onProject }: { project: Project; onProject: (project: Project) => void }) {
  const [open, setOpen] = useState(false);
  const [documents, setDocuments] = useState<DocumentRecord[]>([]);
  const [standards, setStandards] = useState<StandardRecord[]>([]);
  const [hash, setHash] = useState("");
  const [content, setContent] = useState<DocumentContent | null>(null);
  const [standardId, setStandardId] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [metadata, setMetadata] = useState({ number: "", edition: "", title: "", effective_date: "", scope: "", kind: "official" as StandardRecord["kind"], source: "", source_verified: false });
  const [evidence, setEvidence] = useState<Array<{ key: string; locator: string; text: string; status: string }>>([]);
  const [evidenceKey, setEvidenceKey] = useState("");
  const [manualMetric, setManualMetric] = useState<DesignRule["metric"]>("illuminance");
  const [choice, setChoice] = useState("");
  const [draft, setDraft] = useState<DesignRule | null>(null);
  const [reviewer, setReviewer] = useState("");
  const [reviewNote, setReviewNote] = useState("");
  const [settings, setSettings] = useState<DesignSettings | null>(null);
  const [coverageConfirmed, setCoverageConfirmed] = useState(false);
  const rules = project.rule_set?.rules ?? [];
  const document = documents.find(d => d.source_hash === hash);
  const selectedStandard = standards.find(s => s.standard_id === standardId);
  async function refresh() {
    const [global, local, registered, mapping] = await Promise.all([api.documents(), api.documents(project.project_id), api.standards(project.project_id), api.designSettings(project.project_id)]);
    const all = [...global, ...local]; setDocuments(all); setStandards(registered); setSettings(mapping);
    setHash(old => all.some(d => d.source_hash === old) ? old : all[0]?.source_hash ?? "");
    setStandardId(old => registered.some(s => s.standard_id === old) ? old : registered.at(-1)?.standard_id ?? "");
  }
  useEffect(() => { if (open) void refresh().catch(e => setError(String(e))); }, [open, project.revision]);
  useEffect(() => { setCoverageConfirmed(false); }, [project.rule_set?.version, project.floor_plan?.spatial_model?.version]);
  useEffect(() => {
    setContent(null); if (!document || !open) return; let active = true;
    void api.document(document.source_hash, document.project_id ?? undefined).then(c => { if (active) setContent(c); }).catch(e => { if (active) setError(String(e)); });
    return () => { active = false; };
  }, [hash, open, document?.project_id]);
  useEffect(() => {
    setEvidence([]); setEvidenceKey(""); if (!standardId || !open) return; let active = true;
    void api.standardEvidence(standardId, project.project_id).then(result => { if (active) { setEvidence(result.items); setEvidenceKey(result.items[0]?.key ?? ""); } }).catch(e => { if (active) setError(String(e)); });
    return () => { active = false; };
  }, [standardId, open, project.project_id]);
  useEffect(() => { if (!rules.some(r => r.rule_id === choice)) setChoice(rules[0]?.rule_id ?? ""); }, [project.rule_set?.version]);
  useEffect(() => { const r = rules.find(r => r.rule_id === choice); setDraft(r ? structuredClone(r) : null); }, [choice, project.rule_set?.version]);
  async function perform(work: () => Promise<void>) { setBusy(true); setError(""); setNotice(""); try { await work(); } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); } finally { setBusy(false); } }
  return <details className="review-panel" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>规范与规则 · {rules.length} 条 · {project.rule_set ? `v${project.rule_set.version} / ${project.rule_set.status === "bound" ? "已绑定" : project.rule_set.status === "stale" ? "关联已失效" : "待确认"}` : "未绑定版本"}</summary>
    {open ? <div className="review-body"><p>仅以已上传的标准全文生成候选。登记标准类型与适用范围，逐条核对原页和脚注后确认；不会自动取最严值或判定设计通过。</p>
      <button type="button" disabled={busy} onClick={() => void perform(refresh)}>刷新资料与版本</button>
      <details><summary>1. 逐页证据与标准登记</summary>
        <label>选择全局规范或项目文件<select value={hash} onChange={e => setHash(e.target.value)}><option value="">请先上传标准全文</option>{documents.map(d => <option key={d.source_hash} value={d.source_hash}>{d.project_id ? "项目" : "全局"} · {d.source_name}</option>)}</select></label>
        {content ? <DocumentPages document={content} /> : null}
        <fieldset disabled={busy || !document}><legend>新登记不可变版本（修改资料需登记新版本）</legend><div className="review-grid">
          {Object.entries({ number: "标准编号 / 要求编号", edition: "版本", title: "标题", effective_date: "实施日期", scope: "适用范围", source: "发布机构/授权来源或业主来源" }).map(([key, label]) => <label key={key}>{label}<input type={key === "effective_date" ? "date" : "text"} value={metadata[key as "number"]} onChange={e => setMetadata({ ...metadata, [key]: e.target.value })} /></label>)}
          <label>要求类型<select value={metadata.kind} onChange={e => setMetadata({ ...metadata, kind: e.target.value as StandardRecord["kind"] })}><option value="official">正式标准</option><option value="corporate">企业标准</option><option value="owner">业主要求</option></select></label>
        </div><label className="review-checkbox"><input type="checkbox" checked={metadata.source_verified} onChange={e => setMetadata({ ...metadata, source_verified: e.target.checked })} />我已核实此文件的发布机构/授权来源（不是搜索摘要）</label>
          <button type="button" className="button" onClick={() => void perform(async () => { if (!document) return; const record = await api.registerStandard({ ...metadata, source_hash: document.source_hash, project_id: document.project_id }); await refresh(); setStandardId(record.standard_id); setNotice("标准版本已登记并锁定原文件 SHA-256"); })}>登记此标准版本</button>
        </fieldset>
      </details>
      <details open><summary>2. 生成或补充候选规则</summary>
        <label>标准版本<select value={standardId} onChange={e => setStandardId(e.target.value)}><option value="">请先登记标准</option>{standards.map(s => <option value={s.standard_id} key={s.standard_id}>{s.number} · {s.edition} · {s.title} · {s.standard_id.slice(0, 8)}</option>)}</select></label>
        {selectedStandard ? <p className="source-details">{selectedStandard.scope}<br />来源：{selectedStandard.source} · {selectedStandard.source_verified ? "已核实" : "待核实"}<br />SHA-256：{selectedStandard.file_sha256}<br /><a href={`/backend/standards/${selectedStandard.standard_id}/source?project_id=${project.project_id}`} target="_blank" rel="noreferrer">打开归档原文</a></p> : null}
        <button type="button" disabled={busy || !standardId} onClick={() => void perform(async () => { const result = await api.ruleCandidates(project, standardId); onProject(result.project); setNotice(`新增 ${result.generated} 条候选。${result.notice}`); })}>从此版本抽取候选</button>
        <details><summary>从原文位置手动补充未识别规则</summary><select aria-label="原文证据位置" value={evidenceKey} onChange={e => setEvidenceKey(e.target.value)}>{evidence.map(e => <option key={e.key} value={e.key}>{e.locator} · {e.status}</option>)}</select><pre>{evidence.find(e => e.key === evidenceKey)?.text}</pre><select aria-label="手动规则指标" value={manualMetric} onChange={e => setManualMetric(e.target.value as DesignRule["metric"])}>{Object.entries(metrics).map(([key, name]) => <option value={key} key={key}>{name}</option>)}</select><button type="button" disabled={busy || !evidenceKey} onClick={() => void perform(async () => { onProject(await api.manualRule(project, standardId, evidenceKey, manualMetric)); setNotice("已添加待填写候选，未自动确认"); })}>添加此证据的规则</button></details>
      </details>
      <details open><summary>3. 核对适用性与计算条件</summary><label>选择规则<select value={choice} onChange={e => setChoice(e.target.value)}><option value="">请选择规则</option>{rules.map(r => <option value={r.rule_id} key={r.rule_id}>{metrics[r.metric]} · {r.locator} · {r.status === "confirmed" ? "已确认" : r.status === "rejected" ? "不适用" : "待核实"}</option>)}</select></label>
        {draft ? <fieldset disabled={busy}><legend>{draft.locator} · 原文不可修改</legend><pre>{draft.evidence_text}</pre>
          <div className="review-grid"><label>指标<select value={draft.metric} onChange={e => setDraft({ ...draft, metric: e.target.value as DesignRule["metric"] })}>{Object.entries(metrics).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
            <label>适用空间用途（逗号分隔）<input value={draft.applies_to.join(", ")} onChange={e => setDraft({ ...draft, applies_to: e.target.value.split(/[,，]/).map(v => v.trim()) })} /></label>
            <label>评价场景/口径（必填）<input value={draft.evaluation_scope ?? ""} placeholder="如一般照明；应急照明应单独分组" onChange={e => setDraft({ ...draft, evaluation_scope: e.target.value || null })} /></label>
            <label>比较方式<select value={draft.operator ?? ""} onChange={e => setDraft({ ...draft, operator: e.target.value as DesignRule["operator"] || null })}><option value="">待确认</option><option value=">=">不小于 ≥</option><option value="<=">不大于 ≤</option><option value="=">等于</option><option value="range">区间</option></select></label>
            <label>阈值 / 下限<input type="number" step="any" value={draft.threshold ?? ""} onChange={e => setDraft({ ...draft, threshold: number(e.target.value) })} /></label><label>区间上限<input type="number" step="any" value={draft.upper_threshold ?? ""} onChange={e => setDraft({ ...draft, upper_threshold: number(e.target.value) })} /></label><label>单位（lx、1、K、W/m²）<input value={draft.unit ?? ""} onChange={e => setDraft({ ...draft, unit: e.target.value || null })} /></label>
            {Object.entries({ plane: "计算面定义", workplane_height_m: "工作面高度 m", grid_x_m: "网格 X m", grid_y_m: "网格 Y m", maintenance_factor: "维护系数 0–1", glare_method: "眩光评价方法", glare_observers: "观察对象、位置与方向", additional: "附加条件/脚注/参数依据" }).map(([key, label]) => { const numeric = ["workplane_height_m", "grid_x_m", "grid_y_m", "maintenance_factor"].includes(key); return <label key={key}>{label}<input type={numeric ? "number" : "text"} step="any" value={draft.conditions[key as "plane"] ?? ""} onChange={e => setDraft({ ...draft, conditions: { ...draft.conditions, [key]: numeric ? number(e.target.value) : e.target.value || (key === "additional" ? "" : null) } })} /></label>; })}
          </div><label>排除/编辑说明<input value={draft.review_note} onChange={e => setDraft({ ...draft, review_note: e.target.value })} /></label>
          <div className="review-actions"><button type="button" onClick={() => void perform(async () => { onProject(await api.editRule(project, { ...draft, status: "candidate" })); setNotice("修改已保存为候选，请重新确认"); })}>保存候选修改</button><button type="button" onClick={() => void perform(async () => { onProject(await api.editRule(project, { ...draft, status: "rejected" })); setNotice("已记录不适用原因"); })}>标记不适用</button></div>
        </fieldset> : null}
        <div className="review-grid"><label>设计复核人（必填）<input value={reviewer} onChange={e => setReviewer(e.target.value)} /></label><label>适用性/原页脚注复核说明（必填）<input value={reviewNote} onChange={e => setReviewNote(e.target.value)} /></label></div>
        <button className="button" type="button" disabled={busy || !draft || !reviewer.trim() || !reviewNote.trim()} onClick={() => void perform(async () => { if (!draft) return; const saved = await api.editRule(project, { ...draft, status: "candidate" }); onProject(saved); onProject(await api.confirmRule(saved, draft.rule_id, reviewer, reviewNote)); setNotice("规则已由设计人员确认；不代表仿真合规通过"); })}>保存并确认当前规则</button>
      </details>
      <details open><summary>4. 绑定版本与 DIALux 设置映射</summary>
        <p>{project.rule_set?.invalidation_reason ?? "修改 CAD、空间模型或规则后，旧绑定不会自动沿用。"}</p>
        <label className="review-checkbox"><input type="checkbox" checked={coverageConfirmed} onChange={e => setCoverageConfirmed(e.target.checked)} />我已对照完整原文核对所有适用条款、表格和脚注，未遗漏要求</label>
        <button type="button" className="button primary" disabled={busy || !rules.length || !reviewer.trim() || !reviewNote.trim() || !coverageConfirmed} onClick={() => void perform(async () => { onProject(await api.bindRules(project, reviewer, reviewNote, coverageConfirmed)); setNotice("已绑定当前模型与规则版本；尚未执行 DIALux 计算"); })}>绑定已确认规则版本</button>
        {settings ? <><p>设置状态：{settings.status === "ready_for_dialux_setup" ? "可用于后续 DIALux 设置" : "待核实"} · 尚未执行仿真/合规评估</p><ul>{settings.issues.map((issue, i) => <li key={i}>{issue}</li>)}</ul><details><summary>按房间查看工作面、网格、维护系数与眩光对象</summary><pre>{JSON.stringify(settings.rooms, null, 2)}</pre></details></> : null}
      </details>
      {error ? <p className="error-text" role="alert">{error}</p> : null}{notice ? <p role="status">{notice}</p> : null}{busy ? <p role="status">正在保存并校验…</p> : null}
    </div> : null}
  </details>;
}
