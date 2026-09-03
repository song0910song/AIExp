"use client";

import { useState } from "react";
import { Download, FileJson, FileText } from "lucide-react";
import { api } from "@/lib/api";
import type { Project } from "@/lib/types";
import { BusyButton, Notice, Panel } from "./ui";

type Kind = "report" | "dialux-task";

export function DeliverablesPanel({ project }: { project: Project }) {
  const [busy, setBusy] = useState<Kind | null>(null);
  const [ready, setReady] = useState<Record<Kind, string | null>>({ report: null, "dialux-task": null });
  const [error, setError] = useState<string | null>(null);
  const [photometryProgress, setPhotometryProgress] = useState<{ completed: number; total: number; failed: number } | null>(null);

  async function generate(kind: Kind) {
    setBusy(kind); setError(null);
    try {
      if (kind === "dialux-task") {
        const existing = await api.photometryAssets(project.project_id);
        const byLuminaireId = new Map(existing.assets.map((asset) => [asset.luminaire_id, asset]));
        const pending = project.luminaires.filter((item) => item.has_photometry_download && byLuminaireId.get(item.luminaire_id)?.status !== "downloaded");
        let failed = 0;
        setPhotometryProgress({ completed: 0, total: pending.length, failed: 0 });
        for (const [index, item] of pending.entries()) {
          try {
            const response = await api.downloadLuminairePhotometry(project.project_id, item.luminaire_id);
            if (response.asset.status !== "downloaded") failed += 1;
          } catch {
            failed += 1;
          }
          setPhotometryProgress({ completed: index + 1, total: pending.length, failed });
        }
      }
      const response = await api.generateDeliverable(project.project_id, kind, project.revision);
      setReady((current) => ({ ...current, [kind]: response.download_url.replace(/^\/api/, "/backend") }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "生成失败");
    } finally { setBusy(null); }
  }

  const items: Array<{ kind: Kind; icon: typeof FileText; title: string; description: string; format: string }> = [
    { kind: "report", icon: FileText, title: "设计报告草稿", description: "包含任务书、证据、初算、规则状态、候选灯具、待确认事项和人工复核声明。", format: "MARKDOWN" },
    { kind: "dialux-task", icon: FileJson, title: "DIALux evo 任务包", description: "包含空间条件、候选灯具、ULD/配光标记与待仿真指标，不包含虚构的布灯结果。", format: "JSON" },
  ];

  return (
    <div className="content-stack">
      <div className="section-heading"><div><p className="eyebrow">REVIEWABLE OUTPUTS</p><h1>交付文件</h1><p>交付物只组装已保存的数据，不会由界面补写规范条文或仿真数值。</p></div></div>
      {error ? <Notice tone="danger">{error}</Notice> : null}
      <div className="deliverable-grid">
        {items.map((item) => (
          <Panel key={item.kind} title={item.title} eyebrow={item.kind === "dialux-task" ? "ZIP" : item.format}>
            {item.kind === "dialux-task" && photometryProgress ? <p className="deliverable-progress">配光下载：{photometryProgress.completed}/{photometryProgress.total}；失败：{photometryProgress.failed}。</p> : null}
            <div className="deliverable-content"><item.icon size={30} strokeWidth={1.4} /><p>{item.description}</p><dl><div><dt>项目版本</dt><dd>r{project.revision}</dd></div><div><dt>证据</dt><dd>{project.evidence.length} 条</dd></div><div><dt>灯具</dt><dd>{project.luminaires.length} 款</dd></div></dl></div>
            <div className="deliverable-actions">
              <BusyButton busy={busy === item.kind} onClick={() => generate(item.kind)}>生成最新版本</BusyButton>
              {ready[item.kind] ? <a className="button button-secondary" href={ready[item.kind] ?? undefined}><Download size={16} />下载文件</a> : null}
            </div>
          </Panel>
        ))}
      </div>
      <div className="signoff-note"><strong>签发边界</strong><p>Luminaire Finder 结果与流明法初算均不能替代 DIALux evo 仿真和有资质人员复核。最终交付前必须核验空间几何、反射比、布灯、维持照度、均匀度、UGR 和功率密度。</p></div>
    </div>
  );
}
