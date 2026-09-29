"use client";

import { useState } from "react";
import { FolderOpen, X } from "lucide-react";
import { api } from "@/lib/api";
import type { Project } from "@/lib/types";

export function CreateProjectModal({ onClose, onCreated }: {
  onClose: () => void; onCreated: (project: Project) => void;
}) {
  const [name, setName] = useState("");
  const [spaceType, setSpaceType] = useState("");
  const [directory, setDirectory] = useState<{ selection_id: string; directory: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function chooseDirectory() {
    setError("");
    try {
      const choice = await api.chooseDirectory();
      if (choice.selected && choice.selection_id && choice.directory) {
        setDirectory({ selection_id: choice.selection_id, directory: choice.directory });
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "选择文件夹失败");
    }
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!directory) return;
    setBusy(true);
    setError("");
    try {
      onCreated(await api.createProject(name.trim(), spaceType.trim(), directory.selection_id));
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
          <label>工作区文件夹
            <div className="directory-row"><input value={directory?.directory ?? ""} readOnly placeholder="选择本机文件夹" /><button type="button" className="button secondary" onClick={() => void chooseDirectory()}><FolderOpen size={16} />选择</button></div>
          </label>
          {error ? <p className="error-text" role="alert">{error}</p> : null}
          <footer className="modal-actions"><button type="button" className="button secondary" onClick={onClose}>取消</button><button className="button primary" disabled={busy || !name.trim() || !directory}>{busy ? "创建中…" : "创建"}</button></footer>
        </form>
      </section>
    </div>
  );
}
