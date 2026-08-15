"use client";

import { useState } from "react";
import { Calculator, ShieldCheck } from "lucide-react";
import { api } from "@/lib/api";
import type { Project } from "@/lib/types";
import { BusyButton, EmptyState, Field, Notice, Panel, StatusPill, formatNumber } from "./ui";

export function CalculationPanel({ project, onProject }: { project: Project; onProject: (project: Project) => void }) {
  const [busy, setBusy] = useState<"calculation" | "rule" | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function calculate(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    setBusy("calculation"); setError(null);
    try {
      const result = await api.calculate(project.project_id, project.revision, {
        area_m2: Number(data.get("area_m2")), target_illuminance_lx: Number(data.get("target_illuminance_lx")),
        luminaire_luminous_flux_lm: Number(data.get("lumens")), luminaire_power_w: Number(data.get("power")),
        utilization_factor: Number(data.get("uf")), maintenance_factor: Number(data.get("mf")),
      });
      onProject(result.project);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "计算失败"); } finally { setBusy(null); }
  }

  async function checkRule(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const metric = String(data.get("metric"));
    setBusy("rule"); setError(null);
    try {
      const result = await api.checkRule(project.project_id, project.revision, [{ metric, operator: data.get("operator"), threshold: Number(data.get("threshold")), description: data.get("description") || null }], { [metric]: Number(data.get("observed")) });
      onProject(result.project);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "校核失败"); } finally { setBusy(null); }
  }

  const latest = project.calculations.at(-1);
  return (
    <div className="content-stack">
      <div className="section-heading"><div><p className="eyebrow">DETERMINISTIC SERVICES</p><h1>预计算与规则校核</h1><p>所有结果由确定性函数生成；流明法不替代逐点仿真。</p></div></div>
      {error ? <Notice tone="danger">{error}</Notice> : null}
      <div className="split-grid">
        <Panel title="流明法初算" eyebrow="LUMEN METHOD">
          <form onSubmit={calculate} className="form-grid">
            <Field label="面积 / m²"><input name="area_m2" type="number" required min="0.1" step="0.1" defaultValue={project.brief.area_m2 ?? ""} /></Field>
            <Field label="目标照度 / lx"><input name="target_illuminance_lx" type="number" required min="1" defaultValue={project.brief.target_illuminance_lx ?? ""} /></Field>
            <Field label="单灯光通量 / lm"><input name="lumens" type="number" required min="1" placeholder="3200" /></Field>
            <Field label="单灯功率 / W"><input name="power" type="number" required min="0.1" step="0.1" placeholder="24" /></Field>
            <Field label="利用系数 UF"><input name="uf" type="number" required min="0.01" max="1" step="0.01" defaultValue="0.6" /></Field>
            <Field label="维护系数 MF"><input name="mf" type="number" required min="0.01" max="1" step="0.01" defaultValue="0.8" /></Field>
            <div className="field-wide"><BusyButton busy={busy === "calculation"} type="submit"><Calculator size={16} />执行并保存计算</BusyButton></div>
          </form>
        </Panel>
        <Panel title="最新计算结果" eyebrow={latest ? new Date(latest.calculated_at).toLocaleString("zh-CN") : "NO RESULT"}>
          {latest ? <><div className="result-grid"><div><span>灯具数量</span><strong>{latest.luminaire_count}<small> 套</small></strong></div><div><span>装机功率</span><strong>{formatNumber(latest.installed_power_w)}<small> W</small></strong></div><div><span>功率密度</span><strong>{formatNumber(latest.installed_power_density_w_m2, 2)}<small> W/m²</small></strong></div><div><span>目标总光通量</span><strong>{formatNumber(latest.required_luminous_flux_lm, 0)}<small> lm</small></strong></div></div><div className="boundary-note"><strong>适用限制</strong>{latest.limitations.map((item) => <p key={item}>{item}</p>)}</div></> : <EmptyState title="尚未计算">填写灯具光通量、功率和两个设计系数后执行流明法。</EmptyState>}
        </Panel>
      </div>
      <div className="split-grid">
        <Panel title="规则校核器" eyebrow="EXPLICIT THRESHOLD">
          <form onSubmit={checkRule} className="form-grid">
            <Field label="指标"><select name="metric"><option value="illuminance_lx">照度 / lx</option><option value="cri">显色指数 / Ra</option><option value="lpd_w_m2">LPD / W·m⁻²</option><option value="ugr">UGR</option></select></Field>
            <Field label="判定方向"><select name="operator"><option value="min">不低于</option><option value="max">不高于</option></select></Field>
            <Field label="阈值"><input name="threshold" type="number" step="0.1" required /></Field>
            <Field label="观测值"><input name="observed" type="number" step="0.1" required /></Field>
            <Field label="规则说明" wide hint="阈值应来自已检索并人工确认的证据。"><input name="description" placeholder="例如：会议室一般照明标准值" /></Field>
            <div className="field-wide"><BusyButton busy={busy === "rule"} type="submit"><ShieldCheck size={16} />执行并保存校核</BusyButton></div>
          </form>
        </Panel>
        <Panel title="校核记录" eyebrow={`${project.rule_checks.length} CHECKS`}>
          {project.rule_checks.length ? <div className="check-table">{project.rule_checks.toReversed().map((item, index) => <div key={`${item.metric}-${index}`}><StatusPill status={item.status === "pass" ? "success" : item.status === "fail" ? "danger" : "warning"}>{item.status}</StatusPill><p><strong>{item.metric}</strong><small>{item.explanation}</small></p></div>)}</div> : <EmptyState title="尚无校核记录">输入证据阈值与观测值后进行确定性比较。</EmptyState>}
        </Panel>
      </div>
    </div>
  );
}
