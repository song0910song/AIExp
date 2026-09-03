"use client";

import { useState } from "react";
import { FolderOpen } from "lucide-react";
import { LIGHTING_TEMPLATES } from "@/lib/lighting-templates";
import { api, type WorkspaceDirectorySelection } from "@/lib/api";
import type { Project } from "@/lib/types";
import { BusyButton, Field, Modal, Notice, toNullableNumber } from "./ui";

type ProjectDraft = {
  project_name: string;
  space_type: string;
  area_m2: string;
  workplane_height_m: string;
  target_illuminance_lx: string;
  target_cct_k: string;
  min_cri: string;
  target_ugr: string;
};

const emptyDraft: ProjectDraft = {
  project_name: "",
  space_type: "",
  area_m2: "",
  workplane_height_m: "0.75",
  target_illuminance_lx: "",
  target_cct_k: "",
  min_cri: "",
  target_ugr: "",
};

const templateValueFields = [
  "space_type",
  "workplane_height_m",
  "target_illuminance_lx",
  "target_cct_k",
  "min_cri",
  "target_ugr",
] as const;

export function CreateProjectModal({ onClose, onCreated }: { onClose: () => void; onCreated: (project: Project) => void }) {
  const [draft, setDraft] = useState<ProjectDraft>(emptyDraft);
  const [templateId, setTemplateId] = useState("");
  const [busy, setBusy] = useState(false);
  const [selectingDirectory, setSelectingDirectory] = useState(false);
  const [workspaceDirectory, setWorkspaceDirectory] = useState<WorkspaceDirectorySelection | null>(null);
  const [error, setError] = useState<string | null>(null);
  const selectedTemplate = LIGHTING_TEMPLATES.find((template) => template.id === templateId) ?? null;

  function updateField(key: keyof ProjectDraft, value: string) {
    setDraft((current) => ({ ...current, [key]: value }));
  }

  function selectTemplate(id: string) {
    setTemplateId(id);
    const template = LIGHTING_TEMPLATES.find((item) => item.id === id);
    if (!template) return;

    setDraft((current) => ({
      ...current,
      ...Object.fromEntries(templateValueFields.map((field) => [field, String(template.values[field] ?? "")])),
    }));
  }

  async function selectDirectory() {
    setSelectingDirectory(true);
    setError(null);
    try {
      const selected = await api.selectWorkspaceDirectory();
      if (selected.selected && selected.selection_id && selected.directory) {
        setWorkspaceDirectory(selected);
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法打开项目文件夹选择器");
    } finally {
      setSelectingDirectory(false);
    }
  }

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (!workspaceDirectory?.selection_id) {
        throw new Error("请先选择项目文件夹");
      }
      const values = {
        project_name: draft.project_name.trim(),
        space_type: draft.space_type || null,
        area_m2: toNullableNumber(draft.area_m2),
        workplane_height_m: toNullableNumber(draft.workplane_height_m),
        target_illuminance_lx: toNullableNumber(draft.target_illuminance_lx),
        target_cct_k: toNullableNumber(draft.target_cct_k),
        min_cri: toNullableNumber(draft.min_cri),
        target_ugr: toNullableNumber(draft.target_ugr),
      };
      const confirmed_fields = Object.entries(values)
        .filter(([key, value]) => key !== "project_name" && value !== null && value !== "")
        .map(([key]) => key);

      const project = await api.createProject({
        ...values,
        workspace_selection_id: workspaceDirectory.selection_id,
        confirmed_fields,
        template_origin: selectedTemplate
          ? {
              template_id: selectedTemplate.id,
              template_name: selectedTemplate.name,
              standard_reference: selectedTemplate.standardReference,
              applied_at: new Date().toISOString(),
            }
          : null,
      });
      onCreated(project);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "创建失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title="建立照明项目" onClose={onClose}>
      <form onSubmit={submit}>
        <div className="form-grid">
          <Field label="常用室内照明模板" wide>
            <select value={templateId} onChange={(event) => selectTemplate(event.target.value)}>
              <option value="">手动填写</option>
              {LIGHTING_TEMPLATES.map((template) => <option key={template.id} value={template.id}>{template.name}</option>)}
            </select>
          </Field>
          {selectedTemplate ? (
            <div className="template-reference" aria-live="polite">
              <span>规范初始值</span>
              <strong>{selectedTemplate.values.target_illuminance_lx} lx · UGR {selectedTemplate.values.target_ugr} · Ra {selectedTemplate.values.min_cri}</strong>
              <small>{selectedTemplate.standardReference}</small>
            </div>
          ) : null}
          <Field label="项目名称" wide>
            <input value={draft.project_name} onChange={(event) => updateField("project_name", event.target.value)} required placeholder="例如：总部三层会议室改造" autoFocus />
          </Field>
          <Field label="项目文件夹" hint="项目状态、资料、图纸、配光和交付文件均保存于此目录" wide>
            <div className="directory-picker">
              <input value={workspaceDirectory?.directory ?? ""} readOnly placeholder="请选择本机项目文件夹" aria-label="项目文件夹" />
              <BusyButton className="button button-secondary" busy={selectingDirectory} type="button" onClick={() => void selectDirectory()} disabled={busy}>
                <FolderOpen size={16} />选择文件夹
              </BusyButton>
            </div>
          </Field>
          <Field label="空间类型">
            <input value={draft.space_type} onChange={(event) => updateField("space_type", event.target.value)} placeholder="会议室" />
          </Field>
          <Field label="面积 / m²">
            <input value={draft.area_m2} onChange={(event) => updateField("area_m2", event.target.value)} type="number" min="0.1" step="0.1" placeholder="30" />
          </Field>
          <Field label="工作面高度 / m">
            <input value={draft.workplane_height_m} onChange={(event) => updateField("workplane_height_m", event.target.value)} type="number" min="0" step="0.05" />
          </Field>
          <Field label="目标照度 / lx">
            <input value={draft.target_illuminance_lx} onChange={(event) => updateField("target_illuminance_lx", event.target.value)} type="number" min="1" step="1" placeholder="500" />
          </Field>
          <Field label="目标色温 / K">
            <input value={draft.target_cct_k} onChange={(event) => updateField("target_cct_k", event.target.value)} type="number" min="1000" step="100" placeholder="4000" />
          </Field>
          <Field label="最低显色指数 / Ra">
            <input value={draft.min_cri} onChange={(event) => updateField("min_cri", event.target.value)} type="number" min="0" max="100" step="1" placeholder="80" />
          </Field>
          <Field label="目标 UGR">
            <input value={draft.target_ugr} onChange={(event) => updateField("target_ugr", event.target.value)} type="number" min="0" max="40" step="1" placeholder="19" />
          </Field>
        </div>
        {error ? <Notice tone="danger">{error}</Notice> : null}
        <div className="form-actions">
          <button type="button" className="button button-quiet" onClick={onClose}>取消</button>
          <BusyButton busy={busy} type="submit">创建并进入项目</BusyButton>
        </div>
      </form>
    </Modal>
  );
}
