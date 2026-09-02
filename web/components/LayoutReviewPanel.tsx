"use client";

import { useMemo, useState } from "react";
import { AlertTriangle, Crosshair, FileText, MapPinned } from "lucide-react";
import type { LayoutAnalysis, LuminairePlacement, Project } from "@/lib/types";

const modelColors: Record<string, string> = {
  "CEAH-M6311": "#0f766e",
  PAK41101Y: "#c2410c",
};

function pointColor(item: LuminairePlacement): string {
  if (item.matching_status === "coordinate_mismatch") return "#b91c1c";
  return modelColors[item.model ?? ""] ?? "#475569";
}

function extent(analysis: LayoutAnalysis, project: Project): [number, number, number, number] {
  const points = analysis.placements.map((item) => [item.x_m, item.y_m] as const);
  const candidate = project.floor_plan?.area_candidates[project.floor_plan.selected_area_candidate_index ?? 0];
  const unitScale = project.floor_plan?.meters_per_drawing_unit ?? 1;
  if (candidate) points.push(...candidate.points.map((item) => [item.x * unitScale, item.y * unitScale] as const));
  const xs = points.map(([x]) => x);
  const ys = points.map(([, y]) => y);
  const minX = Math.min(...xs, 0);
  const maxX = Math.max(...xs, 1);
  const minY = Math.min(...ys, 0);
  const maxY = Math.max(...ys, 1);
  const padding = Math.max(maxX - minX, maxY - minY) * 0.06;
  return [minX - padding, minY - padding, maxX - minX + padding * 2, maxY - minY + padding * 2];
}

export function LayoutReviewPanel({ project }: { project: Project }) {
  const analysis = project.layout_analysis;
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [modelFilter, setModelFilter] = useState<string>("all");
  const [showUnmatched, setShowUnmatched] = useState(true);

  const visible = useMemo(() => {
    if (!analysis) return [];
    return analysis.placements.filter((item) =>
      (modelFilter === "all" || item.model === modelFilter) &&
      (showUnmatched || item.matching_status === "matched")
    );
  }, [analysis, modelFilter, showUnmatched]);
  const selected = visible.find((item) => item.placement_id === selectedId) ?? null;

  if (!analysis || !project.floor_plan) {
    return (
      <section className="overview-section layout-review">
        <header><div><p className="eyebrow">LAYOUT REVIEW</p><h2>灯具坐标审查</h2></div><Crosshair size={18} /></header>
        <p className="muted">导入 DXF 平面图和 PDF 灯具报告后，这里显示逐灯坐标及来源匹配。</p>
      </section>
    );
  }

  const [minX, minY, width, height] = extent(analysis, project);
  const candidate = project.floor_plan.area_candidates[project.floor_plan.selected_area_candidate_index ?? 0];
  const unitScale = project.floor_plan.meters_per_drawing_unit ?? 1;
  const models = Object.keys(analysis.model_counts);
  return (
    <section className="overview-section layout-review">
      <header>
        <div><p className="eyebrow">LAYOUT REVIEW</p><h2>灯具坐标审查</h2></div>
        <span className="layout-review-count"><MapPinned size={15} />{analysis.placements.length} 套</span>
      </header>
      <div className="layout-toolbar">
        <div className="layout-filter-group" role="group" aria-label="型号筛选">
          <button className={modelFilter === "all" ? "selected" : ""} onClick={() => setModelFilter("all")}>全部</button>
          {models.map((model) => <button key={model} className={modelFilter === model ? "selected" : ""} onClick={() => setModelFilter(model)}>{model}</button>)}
        </div>
        <label className="layout-toggle"><input type="checkbox" checked={showUnmatched} onChange={(event) => setShowUnmatched(event.target.checked)} />显示待复核</label>
      </div>
      <div className="layout-review-grid">
        <div className="layout-canvas-wrap">
          <svg className="layout-canvas" viewBox={`${minX} ${minY} ${width} ${height}`} role="img" aria-label="灯具平面位置叠加图">
            {candidate ? <polygon points={candidate.points.map((item) => `${item.x * unitScale},${item.y * unitScale}`).join(" ")} className="layout-boundary" /> : null}
            {visible.map((item) => <circle key={item.placement_id} cx={item.x_m} cy={item.y_m} r={Math.max(width, height) * 0.012} fill={pointColor(item)} className={selectedId === item.placement_id ? "layout-point selected" : "layout-point"} onClick={() => setSelectedId(item.placement_id)} />)}
          </svg>
          <div className="layout-legend">
            {models.map((model) => <span key={model}><i style={{ background: modelColors[model] ?? "#475569" }} />{model}</span>)}
            <span><i className="legend-warning" />待复核</span>
          </div>
        </div>
        <div className="layout-details">
          {selected ? (
            <div className="layout-selected-detail">
              <div className="layout-detail-heading"><div><p className="eyebrow">{selected.placement_id}</p><h3>{selected.model ?? "型号未识别"}</h3></div><span className={`layout-status ${selected.matching_status}`}>{selected.matching_status}</span></div>
              <dl><div><dt>坐标</dt><dd>{selected.x_m.toFixed(3)}, {selected.y_m.toFixed(3)} m</dd></div><div><dt>安装高度</dt><dd>{selected.z_m === null ? "—" : `${selected.z_m.toFixed(3)} m`}</dd></div><div><dt>报告页</dt><dd>{selected.report_page ?? "—"}</dd></div><div><dt>残差</dt><dd>{selected.coordinate_residual_m === null ? "—" : `${selected.coordinate_residual_m.toFixed(3)} m`}</dd></div></dl>
              <p className="layout-source"><FileText size={14} />{selected.source_refs.find((ref) => ref.includes("page-")) ?? selected.source_refs[0]}</p>
            </div>
          ) : <p className="muted">选择平面图中的灯具查看来源和坐标。</p>}
          <div className="layout-checks">{analysis.checks.map((check) => <div key={String(check.metric)}><span className={`layout-status ${String(check.status)}`}>{String(check.status)}</span><strong>{String(check.metric)}</strong></div>)}</div>
          {analysis.issues.length ? <p className="layout-issue-summary"><AlertTriangle size={14} />{analysis.issues.length} 个问题需要复核</p> : null}
        </div>
      </div>
      <div className="layout-table-wrap"><table className="layout-table"><thead><tr><th>编号</th><th>型号</th><th>X / Y / Z (m)</th><th>匹配</th><th>来源</th></tr></thead><tbody>{visible.map((item) => <tr key={item.placement_id} className={selectedId === item.placement_id ? "selected" : ""} onClick={() => setSelectedId(item.placement_id)}><td>{item.placement_id}</td><td>{item.model ?? "—"}</td><td>{item.x_m.toFixed(3)} / {item.y_m.toFixed(3)} / {item.z_m === null ? "—" : item.z_m.toFixed(3)}</td><td><span className={`layout-status ${item.matching_status}`}>{item.matching_status}</span></td><td>{item.report_page ? `PDF 第 ${item.report_page} 页 · ${item.dxf_entity_handles.length} 个实体` : `${item.dxf_entity_handles.length} 个 DXF 实体`}</td></tr>)}</tbody></table></div>
    </section>
  );
}
