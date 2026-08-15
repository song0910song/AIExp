"use client";

import { ArrowUpRight, Bot, CheckCircle2, ExternalLink, FileText, Lightbulb, Map as MapIcon, Ruler, ShieldCheck } from "lucide-react";
import type { Project } from "@/lib/types";
import { BusyButton, EmptyState, Notice, StatusPill, formatNumber } from "./ui";

const questionLabels: Record<string, string> = {
  space_type: "空间类型",
  area_m2: "面积",
  target_illuminance_lx: "目标照度",
  mounting_height_m: "安装高度",
};

export function OverviewPanel({ project, onStartAgent }: { project: Project; onStartAgent: () => void }) {
  const latest = project.calculations.at(-1);
  const passed = project.rule_checks.filter((item) => item.status === "pass").length;
  const failed = project.rule_checks.filter((item) => item.status === "fail").length;
  const byLuminaireId = new Map(project.luminaires.map((item) => [item.luminaire_id, item]));
  const finalLuminaires = project.selected_luminaire_ids
    .map((luminaireId) => byLuminaireId.get(luminaireId))
    .filter((item): item is NonNullable<typeof item> => Boolean(item));
  const workflow = [
    { icon: Ruler, label: "设计任务书", state: project.open_questions.length ? `${project.open_questions.length} 项待补充` : "核心条件已具备" },
    { icon: FileText, label: "规范证据", state: `${project.evidence.length} 条已采纳` },
    { icon: ShieldCheck, label: "初算与校核", state: `${project.calculations.length} 次计算 · ${project.rule_checks.length} 项校核` },
    { icon: Lightbulb, label: "灯具选型", state: `${project.selected_luminaire_ids.length} 款已确定 · ${project.luminaires.length} 款候选` },
  ];

  return (
    <div className="overview-page">
      <header className="overview-header">
        <div>
          <p className="eyebrow">PROJECT OVERVIEW</p>
          <h1>{project.brief.project_name}</h1>
          <p>当前项目事实、规范依据与设计推进情况。所有更新都可在智能对话中完成。</p>
        </div>
        <button className="button button-primary" onClick={onStartAgent}><Bot size={16} />继续设计<ArrowUpRight size={16} /></button>
      </header>

      <section className="overview-brief">
        <div className="overview-brief-copy">
          <span>设计任务</span>
          <strong>{project.brief.space_type ?? "待补充空间类型"}</strong>
          <p>最近更新于 {new Date(project.updated_at).toLocaleString("zh-CN", { month: "long", day: "numeric", hour: "2-digit", minute: "2-digit" })}</p>
        </div>
        <dl className="overview-facts">
          <div><dt>面积</dt><dd>{formatNumber(project.brief.area_m2)} <small>m2</small></dd></div>
          <div><dt>目标照度</dt><dd>{formatNumber(project.brief.target_illuminance_lx, 0)} <small>lx</small></dd></div>
          <div><dt>安装高度</dt><dd>{formatNumber(project.brief.mounting_height_m)} <small>m</small></dd></div>
          <div><dt>目标色温</dt><dd>{formatNumber(project.brief.target_cct_k, 0)} <small>K</small></dd></div>
        </dl>
      </section>

      <section className="overview-metrics" aria-label="设计状态">
        <article><span>设计进度</span><strong>{project.open_questions.length ? "待补充" : "可推进"}</strong><p>{project.open_questions.length ? `${project.open_questions.length} 个输入需要确认` : "核心输入已满足下一步工作"}</p></article>
        <article><span>初算灯具数量</span><strong>{latest?.luminaire_count ?? "--"}<small> 套</small></strong><p>{latest ? `LPD ${formatNumber(latest.installed_power_density_w_m2)} W/m2` : "尚未执行流明法初算"}</p></article>
        <article><span>最终选定灯具</span><strong>{project.selected_luminaire_ids.length}<small> 款</small></strong><p>{project.luminaires.length} 款灯具候选已保存在项目中</p></article>
        <article><span>规则校核</span><strong>{passed}<small> 通过</small></strong><p>{failed ? `${failed} 项未通过` : "当前没有未通过项"}</p></article>
      </section>

      <div className="overview-content-grid">
        <section className="overview-section overview-workflow">
          <header><div><p className="eyebrow">WORKFLOW</p><h2>设计链路</h2></div><button className="text-action" onClick={onStartAgent}>交给助手<ArrowUpRight size={15} /></button></header>
          <ol>
            {workflow.map((item, index) => (
              <li key={item.label}>
                <span className="workflow-number">{String(index + 1).padStart(2, "0")}</span>
                <item.icon size={18} />
                <div><strong>{item.label}</strong><p>{item.state}</p></div>
                {index < workflow.length - 1 ? <i aria-hidden="true" /> : null}
              </li>
            ))}
          </ol>
        </section>

        <section className="overview-section overview-plan">
          <header><div><p className="eyebrow">FLOOR PLAN</p><h2>平面图依据</h2></div><MapIcon size={18} /></header>
          {project.floor_plan ? (
            <div className="overview-plan-summary">
              <strong>{project.floor_plan.asset.source_name}</strong>
              <p>{project.floor_plan.asset.source_type.toUpperCase()} · {project.floor_plan.drawing_units} · {project.floor_plan.area_candidates.length} 个候选房间边界</p>
              <small>{project.floor_plan.selected_area_candidate_index !== null ? `已采用候选边界 ${project.floor_plan.selected_area_candidate_index + 1}` : project.floor_plan.room_name ? `识别标注：${project.floor_plan.room_name}` : "未识别出明确的空间名称"}</small>
            </div>
          ) : <EmptyState title="尚未确认平面图">在智能对话中上传 DXF 或 DWG 文件，确认边界后将自动更新设计面积与尺寸。</EmptyState>}
        </section>

        <section className="overview-section overview-questions">
          <header><div><p className="eyebrow">INPUTS</p><h2>待确认事项</h2></div>{project.open_questions.length ? <StatusPill status="warning">{project.open_questions.length} 项</StatusPill> : <StatusPill status="success">已齐备</StatusPill>}</header>
          {project.open_questions.length ? (
            <div className="overview-question-list">
              {project.open_questions.map((question) => (
                <button key={question} onClick={onStartAgent} title="在智能对话中补充">
                  <CheckCircle2 size={16} /><span><strong>{questionLabels[question] ?? question}</strong><small>在对话中补充或确认</small></span><ArrowUpRight size={15} />
                </button>
              ))}
            </div>
          ) : <EmptyState title="核心输入已经齐备">仍需在 DIALux evo 中确认房间反射比、布灯位置和计算网格。</EmptyState>}
        </section>
      </div>

      <section className="overview-luminaires" aria-label="最终选定灯具">
        <header>
          <div>
            <p className="eyebrow">FINAL LUMINAIRES</p>
            <h2>最终选定灯具</h2>
          </div>
        </header>
        {finalLuminaires.length ? (
          <div className="final-luminaire-grid">
            {finalLuminaires.map((item) => (
              <article className="final-luminaire-card" key={item.luminaire_id}>
                <div className="final-luminaire-image">
                  {item.image_url ? <img src={item.image_url} alt={`${item.brand_name ?? ""} ${item.article_name}`} /> : <span>产品图未提供</span>}
                </div>
                <div className="final-luminaire-body">
                  <p className="eyebrow">{item.brand_name ?? "品牌未提供"}</p>
                  <h3>{item.article_name}</h3>
                  <p>{item.summary ?? item.technical_summary ?? "技术摘要未提供"}</p>
                  <dl className="final-luminaire-specs">
                    <div><dt>功率</dt><dd>{formatNumber(item.power_w)} W</dd></div>
                    <div><dt>光通量</dt><dd>{formatNumber(item.luminous_flux_lm, 0)} lm</dd></div>
                    <div><dt>CCT</dt><dd>{formatNumber(item.cct_k, 0)} K</dd></div>
                    <div><dt>CRI</dt><dd>{formatNumber(item.cri, 0)}</dd></div>
                    <div><dt>UGR</dt><dd>{formatNumber(item.ugr, 0)}</dd></div>
                    <div><dt>防护</dt><dd>{item.ip_rating ?? "—"}</dd></div>
                  </dl>
                  <div className="final-luminaire-flags">
                    <span className={item.has_uld ? "available" : ""}>ULD {item.has_uld ? "可用" : "缺失"}</span>
                    <span className={item.has_photometry_download ? "available" : ""}>配光 {item.has_photometry_download ? "可用" : "缺失"}</span>
                  </div>
                  <a className="text-link" href={item.detail_url} target="_blank" rel="noreferrer">查看 DIALux 详情 <ExternalLink size={14} /></a>
                </div>
              </article>
            ))}
          </div>
        ) : (
          <EmptyState title="尚无最终选定灯具">在智能对话中完成灯具选型并确认最终型号后，将在此以卡片展示；配光文件可通过 DIALux 任务包手动使用。</EmptyState>
        )}
      </section>
    </div>
  );
}
