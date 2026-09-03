"use client";

import { useEffect, useState } from "react";
import { Save } from "lucide-react";
import { api } from "@/lib/api";
import type { DesignBrief, LightingGroup, Project } from "@/lib/types";
import { BusyButton, Field, Notice, Panel, toNullableNumber } from "./ui";

const numericFields = [
  ["area_m2", "面积 / m²", "0.1"],
  ["length_m", "长度 / m", "0.1"],
  ["width_m", "宽度 / m", "0.1"],
  ["room_height_m", "房间高度 / m", "0.1"],
  ["workplane_height_m", "工作面高度 / m", "0.05"],
  ["target_illuminance_lx", "目标照度 / lx", "1"],
  ["target_cct_k", "目标色温 / K", "100"],
  ["min_cri", "最低显色指数 / Ra", "1"],
  ["target_ugr", "目标 UGR", "1"],
  ["max_power_w", "功率上限 / W", "0.1"],
] as const;

type NumericBriefKey = (typeof numericFields)[number][0];

export function BriefPanel({ project, onProject }: { project: Project; onProject: (project: Project) => void }) {
  const [draft, setDraft] = useState<DesignBrief>(project.brief);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: "danger" | "success"; text: string } | null>(null);

  useEffect(() => setDraft(project.brief), [project]);

  function setValue<K extends keyof DesignBrief>(key: K, value: DesignBrief[K]) {
    setDraft((current) => ({ ...current, [key]: value }));
  }

  function setNumericValue(key: NumericBriefKey, value: string) {
    setValue(key, toNullableNumber(value));
  }

  function updateGroup(index: number, patch: Partial<LightingGroup>) {
    setValue("lighting_groups", draft.lighting_groups.map((group, itemIndex) => itemIndex === index ? { ...group, ...patch, confirmed: false } : group));
  }

  function addGroup() {
    const group: LightingGroup = {
      group_id: crypto.randomUUID(), region_name: "", group_name: "", purpose: null,
      area_m2: draft.area_m2 ?? 1, mounting_height_m: 0.1, target_illuminance_lx: draft.target_illuminance_lx ?? 1,
      target_cct_k: draft.target_cct_k, min_cri: draft.min_cri, target_ugr: draft.target_ugr,
      utilization_factor: null, maintenance_factor: null, luminaire_ids: [], source_evidence_ids: [], source_references: [], confirmed: false,
    };
    setValue("lighting_groups", [...draft.lighting_groups, group]);
  }

  async function save(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setMessage(null);
    try {
      const confirmed = Object.entries(draft)
        .filter(([key, value]) => !["confirmed_fields", "lighting_parameter_sources", "template_origin"].includes(key) && value !== null && value !== "" && (!Array.isArray(value) || value.length > 0))
        .map(([key]) => key);
      const updated = await api.updateBrief(project.project_id, project.revision, {
        ...draft,
        lighting_groups: draft.lighting_groups.map((group) => ({ ...group, confirmed: true })),
        confirmed_fields: confirmed,
      });
      onProject(updated);
      setMessage({ tone: "success", text: `任务书已保存，项目版本更新为 r${updated.revision}。` });
    } catch (reason) {
      setMessage({ tone: "danger", text: reason instanceof Error ? reason.message : "保存失败" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="content-stack">
      <div className="section-heading">
        <div><p className="eyebrow">DESIGN BRIEF</p><h1>设计任务书</h1><p>只有已确认的条件才进入计算、规则校核与灯具筛选。</p></div>
      </div>
      <form onSubmit={save}>
        <Panel title="空间与设计目标" eyebrow={`REVISION ${project.revision}`} action={<BusyButton busy={busy} type="submit"><Save size={16} />保存任务书</BusyButton>}>
          <div className="form-grid form-grid-three">
            <Field label="项目名称" wide><input value={draft.project_name} onChange={(event) => setValue("project_name", event.target.value)} required /></Field>
            <Field label="空间类型"><input value={draft.space_type ?? ""} onChange={(event) => setValue("space_type", event.target.value || null)} placeholder="会议室、办公室、走廊…" /></Field>
            <Field label="安装方式"><input value={draft.mounting ?? ""} onChange={(event) => setValue("mounting", event.target.value || null)} placeholder="嵌入式 / 吸顶 / 吊装" /></Field>
            {numericFields.map(([key, label, step]) => (
              <Field key={key} label={label}>
                <input type="number" step={step} value={draft[key] ?? ""} onChange={(event) => setNumericValue(key, event.target.value)} />
              </Field>
            ))}
            <Field label="最低防护等级"><input value={draft.min_ip_rating ?? ""} onChange={(event) => setValue("min_ip_rating", event.target.value.toUpperCase() || null)} placeholder="IP20" pattern="IP[0-9]{2}[A-Za-z]?" /></Field>
            <Field label="偏好品牌" wide hint="多个品牌请用逗号分隔；偏好不是合规条件。"><input value={draft.preferred_brands.join("，")} onChange={(event) => setValue("preferred_brands", event.target.value.split(/[，,]/).map((item) => item.trim()).filter(Boolean))} /></Field>
            <Field label="项目备注" wide><textarea rows={4} value={draft.notes ?? ""} onChange={(event) => setValue("notes", event.target.value || null)} placeholder="记录甲方要求、现场限制或尚未结构化的条件。" /></Field>
          </div>
          <div className="boundary-note">
            <strong>Lighting regions and groups</strong>
            {draft.lighting_groups.map((group, index) => (
              <div className="form-grid form-grid-three" key={group.group_id}>
                <Field label="Region"><input value={group.region_name} onChange={(event) => updateGroup(index, { region_name: event.target.value })} required /></Field>
                <Field label="Group"><input value={group.group_name} onChange={(event) => updateGroup(index, { group_name: event.target.value })} required /></Field>
                <Field label="Area / m2"><input type="number" min="0.1" step="0.1" value={group.area_m2} onChange={(event) => updateGroup(index, { area_m2: Number(event.target.value) })} required /></Field>
                <Field label="Mounting point / m"><input type="number" min="0.1" step="0.1" value={group.mounting_height_m} onChange={(event) => updateGroup(index, { mounting_height_m: Number(event.target.value) })} required /></Field>
                <Field label="Target illuminance / lx"><input type="number" min="1" step="1" value={group.target_illuminance_lx} onChange={(event) => updateGroup(index, { target_illuminance_lx: Number(event.target.value) })} required /></Field>
              </div>
            ))}
            <button type="button" className="button button-quiet" onClick={addGroup}>Add lighting group</button>
          </div>
          {draft.template_origin ? <div className="boundary-note"><strong>模板初始值</strong><p>{draft.template_origin.template_name} · {draft.template_origin.standard_reference}</p><p>模板值仅作为可编辑起点；请结合项目资料、RAG 证据和工程师确认修正。</p></div> : null}
          {message ? <Notice tone={message.tone}>{message.text}</Notice> : null}
        </Panel>
      </form>
    </div>
  );
}
