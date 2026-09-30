"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/lib/api";
import type { Point, Project, SpatialElement, SpatialModel, SpatialRoom } from "@/lib/types";

const xy = (points: Point[]) => points.map(p => `${p.x},${-p.y}`).join(" ");
const numeric = (value: string) => value.trim() === "" ? null : Number(value);
const roomFields = { floor: "楼层", number: "房间编号", name: "名称", usage: "用途（需与规则一致）", elevation_m: "地坪标高 m", height_m: "层高 m", ceiling_height_m: "吊顶净高 m", wall_reflectance: "墙反射率 0–1", ceiling_reflectance: "顶反射率 0–1", floor_reflectance: "地反射率 0–1" } as const;
const textFields = new Set(["floor", "number", "name", "usage"]);

export function SpatialReview({ project, onProject }: { project: Project; onProject: (project: Project) => void }) {
  const plan = project.floor_plan;
  const [model, setModel] = useState<SpatialModel | null>(plan?.spatial_model ?? null);
  const [choice, setChoice] = useState(0);
  const [elementChoice, setElementChoice] = useState(0);
  const [boundary, setBoundary] = useState("");
  const [holes, setHoles] = useState("");
  const [footprint, setFootprint] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const [drawn, setDrawn] = useState<Point[] | null>(null);
  const [dragging, setDragging] = useState<number | null>(null);
  const [focus, setFocus] = useState<Point | null>(null);
  const [knownDistance, setKnownDistance] = useState("");
  const svgRef = useRef<SVGSVGElement>(null);
  const room = model?.rooms[choice];
  const visibleElements = model?.elements.filter(item => !/\bDLX_(?:APERT|OBJ|LUM|CALC)\b/i.test(item.name)) ?? [];
  const element = visibleElements[elementChoice];
  useEffect(() => {
    if (!plan?.spatial_model) { setModel(null); return; }
    const next = structuredClone(plan.spatial_model);
    for (const item of next.elements) {
      if (/\bDLX_(?:APERT|OBJ|LUM|CALC)\b/i.test(item.name)) item.status = "excluded";
    }
    setModel(next); setError("");
  }, [plan?.asset.sha256, plan?.spatial_model?.version]);
  useEffect(() => { setBoundary(JSON.stringify(room?.boundary ?? [])); setHoles(JSON.stringify(room?.holes ?? [])); }, [room]);
  useEffect(() => { setFootprint(JSON.stringify(element?.footprint ?? [])); }, [element]);
  const viewBox = useMemo(() => {
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const points of [...(plan?.drawing_paths ?? []).map(p => p.points), ...(model?.rooms ?? []).map(r => r.boundary)]) {
      for (const p of points) { minX = Math.min(minX, p.x); maxX = Math.max(maxX, p.x); minY = Math.min(minY, p.y); maxY = Math.max(maxY, p.y); }
    }
    if (!Number.isFinite(minX)) return [0, -10, 10, 10];
    const span = Math.max(maxX - minX, maxY - minY, 1);
    if (focus) return [focus.x - span / 12, -focus.y - span / 12, span / 6, span / 6];
    return [minX - span * .03, -maxY - span * .03, Math.max(maxX - minX, span * .1) + span * .06, Math.max(maxY - minY, span * .1) + span * .06];
  }, [plan, model?.rooms, focus]);
  if (!plan) return null;
  if (!model) return <div className="notice">旧 CAD 仅有摘要，请重新上传以启用多房间核对。</div>;
  function changeRoom(patch: Partial<SpatialRoom>) {
    setModel(old => old ? { ...old, coverage_confirmed: false, design_ready: false, rooms: old.rooms.map((r, i) => i === choice ? { ...r, ...patch } : r) } : old);
    setNotice("有未保存修改；请重新确认覆盖范围后保存");
  }
  function changeElement(patch: Partial<SpatialElement>) {
    setModel(old => old ? { ...old, elements_reviewed: false, design_ready: false, elements: old.elements.map((e, i) => i === elementChoice ? { ...e, ...patch } : e) } : old);
    setNotice("构件已修改，保存前请核对完整性");
  }
  function pointAt(event: React.PointerEvent<SVGSVGElement>): Point | null {
    const svg = svgRef.current, matrix = svg?.getScreenCTM();
    if (!svg || !matrix) return null;
    const p = svg.createSVGPoint(); p.x = event.clientX; p.y = event.clientY;
    const local = p.matrixTransform(matrix.inverse());
    return { x: local.x, y: -local.y };
  }
  async function save() {
    if (!model) return; setBusy(true); setError("");
    try { onProject(await api.reviewModel(project, model, note)); setNotice("空间模型已保存；旧规则绑定与计算关联已重新评估"); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "保存失败"); }
    finally { setBusy(false); }
  }
  function finishRoom() {
    if (!drawn || drawn.length < 3 || !model) return;
    const created: SpatialRoom = { room_id: crypto.randomUUID(), candidate_id: null, floor: null, number: null, name: null, usage: null, boundary: drawn, holes: [], area_m2: null, elevation_m: null, height_m: null, ceiling_height_m: null, wall_reflectance: null, ceiling_reflectance: null, floor_reflectance: null, status: "pending", exclusion_reason: "", provenance: {} };
    setModel({ ...model, rooms: [...model.rooms, created], coverage_confirmed: false }); setChoice(model.rooms.length); setDrawn(null);
  }
  return <details className="review-panel" open>
    <summary>CAD 核对 · {plan.asset.source_name} · 模型 v{model.version} · {model.rooms.length} 个候选</summary>
    <div className="review-body">
      <p className="review-status">文件读取：{plan.read_complete ? "完整" : "有未支持/未读取项"}　设计信息：{model.design_ready ? "已齐备" : "待核实"}</p>
      <div className="review-actions"><a href={`/backend/projects/${project.project_id}/floor-plan/source`} target="_blank" rel="noreferrer">原始 CAD</a><a href={`/backend/projects/${project.project_id}/spatial-model/export`} target="_blank" rel="noreferrer">精确空间模型 JSON</a><button type="button" onClick={() => setFocus(null)}>全图</button><button type="button" onClick={() => setDrawn(drawn ? null : [])}>{drawn ? "取消绘制" : "手绘遗漏房间"}</button>{drawn ? <button type="button" disabled={drawn.length < 3} onClick={finishRoom}>闭合为新房间（{drawn.length} 点）</button> : null}</div>
      <p className="muted">灰线为原图 WCS 几何；点击房间切换，拖动绿色控制点修正边界。圆弧保留源参数，建模采样容差与预览抽稀分离。</p>
      <svg ref={svgRef} className="cad-canvas" viewBox={viewBox.join(" ")} role="img" aria-label="原始 CAD 与多房间边界叠加核对"
        onPointerDown={e => { if (drawn) { const p = pointAt(e); if (p) setDrawn([...drawn, p]); } }}
        onPointerMove={e => { if (dragging === null || !room) return; const p = pointAt(e); if (p) changeRoom({ boundary: room.boundary.map((v, i) => i === dragging ? p : v), status: "pending" }); }}
        onPointerUp={() => setDragging(null)} onPointerCancel={() => setDragging(null)}>
        {plan.drawing_paths.map((p, i) => <polyline key={i} points={xy(p.closed ? [...p.points, p.points[0]] : p.points)} fill="none" stroke="#94a39d" strokeWidth="1" vectorEffect="non-scaling-stroke" />)}
        {model.rooms.map((r, i) => <g key={r.room_id}><path d={[r.boundary, ...r.holes].map(ring => `M ${xy(ring)} Z`).join(" ")} fillRule="evenodd" fill={r.status === "excluded" ? "#b8b8b822" : i === choice ? "#087b6c44" : "#dbab2b22"} stroke={i === choice ? "#087b6c" : "#b18724"} vectorEffect="non-scaling-stroke" strokeWidth={i === choice ? 2 : 1} onClick={() => { if (!drawn) setChoice(i); }}><title>{r.name || `房间 ${i + 1}`} · {r.status}</title></path></g>)}
        {plan.drawing_labels.map((t, i) => <text key={i} x={t.position.x} y={-t.position.y} fontSize={viewBox[2] / 85} fill="#42564d" pointerEvents="none">{t.text}</text>)}
        {room?.boundary.map((p, i) => <circle key={i} cx={p.x} cy={-p.y} r={viewBox[2] / 180} fill="#087b6c" onPointerDown={event => { if (drawn) return; event.stopPropagation(); svgRef.current?.setPointerCapture(event.pointerId); setDragging(i); }}><title>顶点 {i + 1}：{p.x}, {p.y}</title></circle>)}
        {drawn ? <polyline points={xy(drawn)} fill="none" stroke="#c46f11" strokeWidth="2" vectorEffect="non-scaling-stroke" /> : null}
      </svg>
      <div className="review-grid">
        <label>每图纸单位对应米<input aria-label="每图纸单位对应米" type="number" step="any" value={model.meters_per_unit ?? ""} onChange={e => setModel({ ...model, meters_per_unit: numeric(e.target.value), coverage_confirmed: false, design_ready: false })} /></label>
        <label>选中房间前两点实际距离 m<input type="number" step="any" value={knownDistance} onChange={e => setKnownDistance(e.target.value)} /></label>
        <button type="button" onClick={() => { const [a, b] = room?.boundary ?? []; const distance = a && b ? Math.hypot(a.x - b.x, a.y - b.y) : 0; if (distance > 0 && Number(knownDistance) > 0) setModel({ ...model, meters_per_unit: Number(knownDistance) / distance, coverage_confirmed: false, design_ready: false }); else setError("需要两个不同顶点及正的实测距离"); }}>按已知尺寸校准全图</button>
      </div>
      <label>逐房间核对<select value={choice} onChange={e => setChoice(Number(e.target.value))}>{model.rooms.map((r, i) => <option key={r.room_id} value={i}>{i + 1}. {r.name || "未命名"} · {r.status === "confirmed" ? "边界已确认" : r.status === "excluded" ? "已排除" : "待核对"} · {r.area_m2?.toFixed(2) ?? "?"} m²（已保存）</option>)}</select></label>
      {room ? <fieldset disabled={busy}><legend>房间属性 · 缺失参数留空，人工补充按假设记录</legend><div className="review-grid">
        {Object.entries(roomFields).map(([key, label]) => <label key={key}>{label}<input type={textFields.has(key) ? "text" : "number"} step="any" value={room[key as keyof typeof roomFields] ?? ""} onChange={e => changeRoom({ [key]: textFields.has(key) ? e.target.value || null : numeric(e.target.value) })} /></label>)}
        <label>边界确认<select value={room.status} onChange={e => changeRoom({ status: e.target.value as SpatialRoom["status"] })}><option value="pending">待核对</option><option value="confirmed">边界已确认</option><option value="excluded">排除候选</option></select></label>
        <label>排除原因<input value={room.exclusion_reason} onChange={e => changeRoom({ exclusion_reason: e.target.value })} /></label>
      </div><details><summary>精确边界、内洞与来源</summary><label>WCS 边界 JSON（不是预览轮廓）<textarea value={boundary} onChange={e => setBoundary(e.target.value)} /></label><label>内洞 JSON<textarea value={holes} onChange={e => setHoles(e.target.value)} /></label><button type="button" onClick={() => { try { const b = JSON.parse(boundary), h = JSON.parse(holes); if (!Array.isArray(b) || b.length < 3 || !b.every(p => Number.isFinite(p.x) && Number.isFinite(p.y)) || !Array.isArray(h) || !h.every(r => Array.isArray(r) && r.every(p => Number.isFinite(p.x) && Number.isFinite(p.y)))) throw Error("请输入有效的坐标数组"); changeRoom({ boundary: b, holes: h, status: "pending" }); } catch (e) { setError(String(e)); } }}>应用坐标修正</button><pre>{JSON.stringify(room.provenance, null, 2)}</pre></details></fieldset> : null}
      <details><summary>图面识别到的门窗与家具 · {visibleElements.length} 项</summary>
        {visibleElements.length ? <select aria-label="查看图面构件" value={Math.min(elementChoice, visibleElements.length - 1)} onChange={e => setElementChoice(Number(e.target.value))}>{visibleElements.map((e, i) => <option key={e.element_id} value={i}>{({ door: "门洞", window: "窗洞", column: "柱", furniture: "家具", obstruction: "遮挡物" } as Record<string, string>)[e.kind]} · {e.name.replace(/\bDLX_[A-Z_]+\s*\/\s*/ig, "")}</option>)}</select> : <p>当前图纸没有需要单独核对的建筑构件。你可以通过对话补充识别遗漏。</p>}
        {element ? <div><div className="review-grid"><label>类型<select value={element.kind} onChange={e => changeElement({ kind: e.target.value as SpatialElement["kind"] })}>{Object.entries({ door: "门洞", window: "窗洞", column: "柱", furniture: "家具", obstruction: "遮挡物" }).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label><label>名称<input value={element.name} onChange={e => changeElement({ name: e.target.value })} /></label><label>所属房间<select value={element.room_id ?? ""} onChange={e => changeElement({ room_id: e.target.value || null })}><option value="">待确认</option>{model.rooms.map(r => <option key={r.room_id} value={r.room_id}>{r.name || r.room_id}</option>)}</select></label>
          {Object.entries({ elevation_m: "底部标高 m", height_m: "高度 m", rotation_deg: "朝向 °", reflectance: "反射率" }).map(([key, label]) => <label key={key}>{label}<input type="number" step="any" value={element[key as "height_m"] ?? ""} onChange={e => changeElement({ [key]: numeric(e.target.value) })} /></label>)}<label>材质<input value={element.material ?? ""} onChange={e => changeElement({ material: e.target.value || null })} /></label><label>状态<select value={element.status} onChange={e => changeElement({ status: e.target.value as SpatialElement["status"] })}><option value="pending">待确认</option><option value="confirmed">已确认</option><option value="excluded">排除</option></select></label></div><label>位置及尺寸（WCS 占地多边形）<textarea value={footprint} onChange={e => setFootprint(e.target.value)} /></label><button type="button" onClick={() => { try { const points = JSON.parse(footprint); if (!Array.isArray(points) || points.length < 3 || !points.every(p => Number.isFinite(p.x) && Number.isFinite(p.y))) throw Error("构件占地至少需要三个坐标点"); changeElement({ footprint: points, status: "pending" }); } catch (e) { setError(String(e)); } }}>应用构件轮廓</button></div> : null}
      </details>
      <details><summary>文件读取与异常 · {plan.issues.length} 项</summary><p>原图：{plan.asset.source_name} · {plan.read_complete ? "几何读取完整" : "存在未读取区域"}</p>{plan.warnings.map((warning, i) => <p key={i}>{warning}</p>)}{plan.issues.filter(issue => issue.code !== "unsupported_entity" && issue.code !== "incomplete_read").map((issue, i) => <p key={i}><button type="button" onClick={() => { if (issue.position) setFocus(issue.position); }}>{issue.message}{issue.position ? " · 在图纸中定位" : ""}</button></p>)}</details>
      <details><summary>已保存模型的待确认清单 · {model.outstanding.length} 项</summary><ul>{model.outstanding.map((item, i) => <li key={i}>{item}</li>)}</ul><pre>{model.audit_log.join("\n")}</pre></details>
      <label className="review-checkbox"><input type="checkbox" checked={model.coverage_confirmed} onChange={e => setModel({ ...model, coverage_confirmed: e.target.checked })} />已对照原图核对所有待设计房间、遗漏、重叠和排除范围</label>
      <label className="review-checkbox"><input type="checkbox" checked={model.elements_reviewed} onChange={e => setModel({ ...model, elements_reviewed: e.target.checked })} />已核对门窗、柱、家具、遮挡物完整性（无构件时确认确实不存在）</label>
      <label>核对/假设说明（必填）<input value={note} onChange={e => setNote(e.target.value)} placeholder="说明尺寸依据、人工假设或修正原因" /></label>
      {error ? <p className="error-text" role="alert">{error}</p> : null}{notice ? <p role="status">{notice}</p> : null}
      <button className="button primary" type="button" disabled={busy || !note.trim() || !model.meters_per_unit} onClick={() => void save()}>{busy ? "保存中…" : "保存空间模型与确认记录"}</button>
    </div>
  </details>;
}
