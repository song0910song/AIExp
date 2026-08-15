"use client";

import { useState } from "react";
import { api } from "@/lib/api";
import type { Project } from "@/lib/types";
import { BusyButton, Field, Modal, Notice, toNullableNumber } from "./ui";

export function CreateProjectModal({ onClose, onCreated }: { onClose: () => void; onCreated: (project: Project) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    const data = new FormData(event.currentTarget);
    try {
      const project = await api.createProject({
        project_name: String(data.get("project_name") ?? ""),
        space_type: String(data.get("space_type") ?? "") || null,
        area_m2: toNullableNumber(String(data.get("area_m2") ?? "")),
        mounting_height_m: toNullableNumber(String(data.get("mounting_height_m") ?? "")),
        target_illuminance_lx: toNullableNumber(String(data.get("target_illuminance_lx") ?? "")),
        target_cct_k: toNullableNumber(String(data.get("target_cct_k") ?? "")),
        min_cri: toNullableNumber(String(data.get("min_cri") ?? "")),
        confirmed_fields: ["space_type", "area_m2", "mounting_height_m", "target_illuminance_lx"].filter(
          (key) => String(data.get(key) ?? "").trim() !== "",
        ),
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
          <Field label="项目名称" wide>
            <input name="project_name" required placeholder="例如：总部三层会议室改造" autoFocus />
          </Field>
          <Field label="空间类型">
            <input name="space_type" placeholder="会议室" />
          </Field>
          <Field label="面积 / m²">
            <input name="area_m2" type="number" min="0.1" step="0.1" placeholder="30" />
          </Field>
          <Field label="安装高度 / m">
            <input name="mounting_height_m" type="number" min="0.1" step="0.1" placeholder="2.7" />
          </Field>
          <Field label="目标照度 / lx">
            <input name="target_illuminance_lx" type="number" min="1" step="1" placeholder="500" />
          </Field>
          <Field label="目标色温 / K">
            <input name="target_cct_k" type="number" min="1000" step="100" placeholder="4000" />
          </Field>
          <Field label="最低显色指数 / Ra">
            <input name="min_cri" type="number" min="0" max="100" step="1" placeholder="80" />
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
