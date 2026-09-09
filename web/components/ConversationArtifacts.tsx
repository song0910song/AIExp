"use client";

import { useEffect, useMemo, useState } from "react";
import {
  Box,
  CircleAlert,
  Download,
  ExternalLink,
  FileImage,
  FileText,
  Image as ImageIcon,
  Lightbulb,
  LoaderCircle,
  Power,
  X,
} from "lucide-react";
import { api } from "@/lib/api";
import type { BlenderWorkflowNode, Project } from "@/lib/types";

type Artifact = {
  id: string;
  title: string;
  detail: string;
  kind: "model" | "illuminance" | "source" | "luminaire";
  src: string;
};

const kindIcon = {
  model: Box,
  illuminance: Lightbulb,
  source: FileImage,
  luminaire: ImageIcon,
};

function nodeStatusLabel(status: BlenderWorkflowNode["status"]) {
  return {
    pending: "待执行",
    running: "执行中",
    succeeded: "已完成",
    blocked: "待补充",
    failed: "失败",
  }[status];
}

function collectArtifacts(project: Project): Artifact[] {
  const artifacts: Artifact[] = [];
  const workflow = project.blender_workflow;
  if (workflow.estimate?.status === "succeeded" && workflow.estimate.heatmap_path) {
    artifacts.push({
      id: `illuminance:${workflow.estimate.heatmap_path}`,
      title: "工作面照度热力图",
      detail: `${workflow.estimate.grid_rows} x ${workflow.estimate.grid_columns} 网格`,
      kind: "illuminance",
      src: api.blenderWorkflowAssetUrl(project.project_id, workflow.estimate.heatmap_path),
    });
  }
  if (workflow.model?.status === "ready") {
    workflow.model.render_paths.forEach((path, index) => artifacts.push({
      id: `model:${path}`,
      title: index === 0 ? "Blender 三维方案" : "Blender 工作面视图",
      detail: workflow.model?.reused ? "复用项目模型" : "当前项目模型",
      kind: "model",
      src: api.blenderWorkflowAssetUrl(project.project_id, path),
    }));
  }
  [...workflow.source_assets].reverse().forEach((source) => {
    source.preview_paths.forEach((path, index) => artifacts.push({
      id: `source:${path}`,
      title: `${source.source_name}${source.preview_paths.length > 1 ? ` · ${index + 1}` : ""}`,
      detail: `${source.source_type.toUpperCase()} 源资料`,
      kind: "source",
      src: api.blenderWorkflowAssetUrl(project.project_id, path),
    }));
  });
  const selected = new Set(project.selected_luminaire_ids);
  project.luminaires
    .filter((item) => selected.has(item.luminaire_id))
    .forEach((luminaire) => {
      const src = luminaire.image_url ?? luminaire.photometry_image_url;
      if (!src) return;
      artifacts.push({
        id: `luminaire:${luminaire.luminaire_id}`,
        title: luminaire.article_name,
        detail: [luminaire.brand_name, luminaire.luminous_flux_lm ? `${luminaire.luminous_flux_lm} lm` : null]
          .filter(Boolean)
          .join(" · "),
        kind: "luminaire",
        src,
      });
    });
  return artifacts.filter((artifact, index) => artifacts.findIndex((item) => item.src === artifact.src) === index);
}

export function ConversationArtifacts({ project, onClose }: { project: Project; onClose: () => void }) {
  const artifacts = useMemo(() => collectArtifacts(project), [project]);
  const [selectedId, setSelectedId] = useState<string | null>(artifacts[0]?.id ?? null);
  const [opening, setOpening] = useState(false);
  const [openError, setOpenError] = useState<string | null>(null);

  useEffect(() => {
    if (!artifacts.some((artifact) => artifact.id === selectedId)) setSelectedId(artifacts[0]?.id ?? null);
  }, [artifacts, selectedId]);

  const selected = artifacts.find((artifact) => artifact.id === selectedId) ?? artifacts[0] ?? null;
  const workflow = project.blender_workflow;
  const estimate = workflow.estimate;

  async function openBlender() {
    setOpening(true);
    setOpenError(null);
    try {
      await api.openBlenderWorkflow(project.project_id);
    } catch (reason) {
      setOpenError(reason instanceof Error ? reason.message : "无法启动 Blender");
    } finally {
      setOpening(false);
    }
  }

  return (
    <aside className="conversation-artifacts" aria-label="项目结果预览">
      <header className="artifact-panel-heading">
        <div><ImageIcon size={15} aria-hidden="true" /><strong>结果预览</strong></div>
        <div><span>{artifacts.length} 项</span><button onClick={onClose} title="关闭结果预览" aria-label="关闭结果预览"><X size={14} /></button></div>
      </header>

      {selected ? (
        <div className="artifact-stage">
          <div className="artifact-stage-toolbar">
            <span className={`artifact-kind artifact-kind-${selected.kind}`}>{selected.kind === "illuminance" ? "照度" : selected.kind === "model" ? "三维" : selected.kind === "source" ? "资料" : "灯具"}</span>
            <a href={selected.src} target="_blank" rel="noreferrer" title="打开原图" aria-label="打开原图"><ExternalLink size={14} /></a>
          </div>
          <a href={selected.src} target="_blank" rel="noreferrer" className="artifact-main-image">
            <img src={selected.src} alt={selected.title} />
          </a>
          <div className="artifact-caption"><strong>{selected.title}</strong><small>{selected.detail}</small></div>
        </div>
      ) : (
        <div className="artifact-empty">
          <ImageIcon size={24} aria-hidden="true" />
          <strong>暂无可视结果</strong>
          <span>上传设计报告或平面图后，这里会显示资料、模型和照度图像。</span>
        </div>
      )}

      {artifacts.length > 1 ? (
        <div className="artifact-filmstrip" aria-label="可视结果列表">
          {artifacts.map((artifact) => {
            const Icon = kindIcon[artifact.kind];
            return (
              <button
                key={artifact.id}
                className={artifact.id === selected?.id ? "active" : ""}
                onClick={() => setSelectedId(artifact.id)}
                title={artifact.title}
              >
                <img src={artifact.src} alt="" />
                <span><Icon size={11} />{artifact.title}</span>
              </button>
            );
          })}
        </div>
      ) : null}

      {estimate?.status === "succeeded" ? (
        <dl className="artifact-metrics">
          <div><dt>平均照度</dt><dd>{Math.round(estimate.average_illuminance_lx ?? 0)} lx</dd></div>
          <div><dt>最小照度</dt><dd>{Math.round(estimate.minimum_illuminance_lx ?? 0)} lx</dd></div>
          <div><dt>均匀度 U0</dt><dd>{estimate.uniformity_u0?.toFixed(3) ?? "-"}</dd></div>
        </dl>
      ) : null}

      <section className="artifact-workflow" aria-label="方案执行状态">
        <div className="artifact-section-title"><span>方案流程</span><small>{workflow.nodes.filter((node) => node.status === "succeeded").length}/{workflow.nodes.length}</small></div>
        <ol>
          {workflow.nodes.map((node) => (
            <li key={node.node_id} className={`artifact-node artifact-node-${node.status}`} title={node.message ?? node.description}>
              <i aria-hidden="true" />
              <span><strong>{node.title}</strong><small>{nodeStatusLabel(node.status)}</small></span>
            </li>
          ))}
        </ol>
      </section>

      <div className="artifact-actions">
        <button className="button button-secondary" onClick={() => void openBlender()} disabled={opening}>
          {opening ? <LoaderCircle size={14} className="spin" /> : <Power size={14} />}<span>{opening ? "正在连接" : "打开 Blender"}</span>
        </button>
        {workflow.report_pdf_path ? (
          <a className="button button-secondary" href={api.blenderWorkflowReportUrl(project.project_id)}>
            <Download size={14} /><span>方案报告</span>
          </a>
        ) : null}
      </div>
      {openError ? <p className="artifact-panel-error"><CircleAlert size={13} />{openError}</p> : null}
      {workflow.report_markdown_path ? <span className="artifact-report-state"><FileText size={12} />优化方案已保存到项目</span> : null}
    </aside>
  );
}
