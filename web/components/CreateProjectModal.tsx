"use client";

import { useState } from "react";
import { X } from "lucide-react";
import { api } from "@/lib/api";
import type { Project } from "@/lib/types";

export function CreateProjectModal({ onClose, onCreated }: {
  onClose: () => void; onCreated: (project: Project) => void;
}) {
  const [name, setName] = useState("");
  const [spaceType, setSpaceType] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      onCreated(await api.createProject(name.trim(), spaceType.trim()));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "创建项目失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <section className="modal-dialog" role="dialog" aria-modal="true" aria-label="新建项目" onMouseDown={(event) => event.stopPropagation()}>
        <header className="modal-heading"><h2>新建项目</h2><button className="icon-button" onClick={onClose} aria-label="关闭"><X size={18} /></button></header>
        <form onSubmit={(event) => void submit(event)} className="field-stack">
          <label>项目名称<input value={name} onChange={(event) => setName(event.target.value)} required autoFocus placeholder="例如：会议室照明" /></label>
          <label>空间用途<input value={spaceType} onChange={(event) => setSpaceType(event.target.value)} placeholder="例如：会议室" /></label>
          {error ? <p className="error-text" role="alert">{error}</p> : null}
          <footer className="modal-actions"><button type="button" className="button secondary" onClick={onClose}>取消</button><button className="button primary" disabled={busy || !name.trim()}>{busy ? "创建中…" : "创建"}</button></footer>
        </form>
      </section>
    </div>
  );
}
