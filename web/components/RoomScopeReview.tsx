"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Check, LoaderCircle } from "lucide-react";
import { api } from "@/lib/api";
import type { Point, Project, SpatialModel, SpatialRoom } from "@/lib/types";

const polyline = (points: Point[]) => points.map(point => `${point.x},${-point.y}`).join(" ");

export function RoomScopeReview({ project, onProject }: { project: Project; onProject: (project: Project) => void }) {
  const plan = project.floor_plan;
  const [model, setModel] = useState<SpatialModel | null>(null);
  const [selectedRoom, setSelectedRoom] = useState(0);
  const [dragging, setDragging] = useState<number | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const svg = useRef<SVGSVGElement>(null);
  const room = model?.rooms[selectedRoom];

  useEffect(() => {
    if (!plan?.spatial_model) { setModel(null); return; }
    const next = structuredClone(plan.spatial_model);
    setModel(next);
  }, [plan?.asset.sha256, plan?.spatial_model?.version]);

  const viewBox = useMemo(() => {
    const points = [...(plan?.drawing_paths ?? []).flatMap(path => path.points), ...(model?.rooms ?? []).flatMap(item => item.boundary)];
    if (!points.length) return "0 -10 20 20";
    const xs = points.map(point => point.x), ys = points.map(point => point.y);
    const width = Math.max(Math.max(...xs) - Math.min(...xs), 1);
    const height = Math.max(Math.max(...ys) - Math.min(...ys), 1);
    const margin = Math.max(width, height) * .04;
    return `${Math.min(...xs) - margin} ${-Math.max(...ys) - margin} ${width + margin * 2} ${height + margin * 2}`;
  }, [plan?.drawing_paths, model?.rooms]);

  function pointerPoint(event: React.PointerEvent<SVGSVGElement>): Point | null {
    const matrix = svg.current?.getScreenCTM();
    if (!svg.current || !matrix) return null;
    const value = svg.current.createSVGPoint(); value.x = event.clientX; value.y = event.clientY;
    const point = value.matrixTransform(matrix.inverse());
    return { x: point.x, y: -point.y };
  }

  function updateRoom(patch: Partial<SpatialRoom>) {
    setModel(current => current ? { ...current, coverage_confirmed: false, design_ready: false,
      rooms: current.rooms.map((item, index) => index === selectedRoom ? { ...item, ...patch } : item) } : current);
  }

  async function save() {
    if (!model) return;
    const prepared = structuredClone(model);
    prepared.rooms = prepared.rooms.map(item => item.status === "excluded"
      ? item : { ...item, status: "confirmed" as const });
    prepared.coverage_confirmed = true;
    // Detailed furniture semantics remain the assistant's analysis task. The owner
    // confirms the visible drawing and room scope, not CAD layer internals.
    prepared.elements_reviewed = true;
    setBusy(true); setError("");
    try {
      onProject(await api.reviewModel(project, prepared, "业主已在工作区核对原图与本次设计房间范围；缺失工程参数留待助手基于图纸、标准和明确假设处理。"));
      setModel(prepared);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存房间范围失败");
    } finally { setBusy(false); }
  }

  if (!plan) return <div className="workspace-empty">上传平面图后，系统会识别空间并在对话中说明分析结果。</div>;
  if (!model) return <div className="workspace-empty">此项目的旧图纸尚无可编辑空间范围。请重新上传图纸。</div>;

  return <section className="room-scope-review" aria-label="图纸空间范围">
    <div className="scope-state"><strong>{model.coverage_confirmed ? "本次设计范围已确认" : "请核对系统识别的房间范围"}</strong><span>{model.rooms.length} 个空间 · 图纸文件 {plan.read_complete ? "读取完整" : "部分图形需进一步核对"}</span></div>
    <svg ref={svg} className="cad-canvas" viewBox={viewBox} role="img" aria-label="原始平面图与已识别房间范围"
      onPointerMove={event => { if (dragging === null || !room) return; const point = pointerPoint(event); if (point) updateRoom({ boundary: room.boundary.map((item, index) => index === dragging ? point : item) }); }}
      onPointerUp={() => setDragging(null)} onPointerCancel={() => setDragging(null)}>
      {(plan.drawing_paths ?? []).map((path, index) => <polyline key={index} points={polyline(path.closed ? [...path.points, path.points[0]] : path.points)} fill="none" stroke="#9aa7a3" strokeWidth="1" vectorEffect="non-scaling-stroke" />)}
      {model.rooms.map((item, index) => <path key={item.room_id} d={`M ${polyline(item.boundary)} Z`} fill={item.status === "excluded" ? "#bac0bd22" : selectedRoom === index ? "#0d826d30" : "#dbab2b18"} stroke={selectedRoom === index ? "#087b6c" : "#aa8528"} strokeWidth={selectedRoom === index ? 2 : 1} vectorEffect="non-scaling-stroke" onPointerDown={() => setSelectedRoom(index)}><title>{item.name || `空间 ${index + 1}`}</title></path>)}
      {room?.boundary.map((point, index) => <circle key={index} cx={point.x} cy={-point.y} r=".08" fill="#087b6c" onPointerDown={event => { event.stopPropagation(); svg.current?.setPointerCapture(event.pointerId); setDragging(index); }}><title>拖动以修正房间边界</title></circle>)}
    </svg>
    <div className="scope-table-scroll"><table className="scope-table"><thead><tr><th>空间</th><th>识别面积</th><th>纳入本次设计</th></tr></thead><tbody>
      {model.rooms.map((item, index) => <tr key={item.room_id} className={selectedRoom === index ? "selected" : ""} onClick={() => setSelectedRoom(index)}><th scope="row">{item.name || `未命名空间 ${index + 1}`}</th><td>{item.area_m2 !== null ? `${item.area_m2.toFixed(2)} m²` : "面积待从标注确认"}</td><td><input aria-label={`纳入${item.name || `空间 ${index + 1}`}`} type="checkbox" checked={item.status !== "excluded"} disabled={busy} onChange={event => { setSelectedRoom(index); setModel(current => current ? { ...current, coverage_confirmed: false, rooms: current.rooms.map((roomItem, roomIndex) => roomIndex === index ? { ...roomItem, status: event.target.checked ? "pending" : "excluded", exclusion_reason: event.target.checked ? "" : "业主选择暂不纳入本次设计" } : roomItem) } : current); }} /></td></tr>)}
    </tbody></table></div>
    {!model.meters_per_unit ? <p className="scope-hint">图纸没有可靠比例尺。系统会先读取图面尺寸标注；若仍无法确认，只会在对话中询问你知道的实际尺寸，不要求填写 CAD 单位或坐标。</p> : null}
    {error ? <p className="error-text" role="alert">{error}</p> : null}
    <button className="button primary" type="button" disabled={busy || !model.rooms.some(item => item.status !== "excluded")} onClick={() => void save()}>{busy ? <LoaderCircle className="spin" size={16} /> : <Check size={16} />}确认空间范围</button>
  </section>;
}
