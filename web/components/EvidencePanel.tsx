"use client";

import { useState } from "react";
import { BookmarkPlus, FileUp, Search } from "lucide-react";
import { api } from "@/lib/api";
import type { Evidence, Project } from "@/lib/types";
import { BusyButton, EmptyState, Field, Notice, Panel, StatusPill } from "./ui";

const sourceLabels = { standard: "标准规范", project_document: "项目资料", user_note: "人工备注" };

export function EvidencePanel({ project, onProject }: { project: Project; onProject: (project: Project) => void }) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<Evidence[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [searching, setSearching] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [adopting, setAdopting] = useState(false);
  const [notice, setNotice] = useState<{ tone: "danger" | "success"; text: string } | null>(null);

  async function search(event: React.FormEvent) {
    event.preventDefault();
    setSearching(true);
    setNotice(null);
    try {
      const response = await api.searchEvidence(query, 5);
      setResults(response.evidence);
      setSelected([]);
    } catch (reason) {
      setNotice({ tone: "danger", text: reason instanceof Error ? reason.message : "检索失败" });
    } finally {
      setSearching(false);
    }
  }

  async function adopt() {
    if (!selected.length) return;
    setAdopting(true);
    setNotice(null);
    try {
      const response = await api.adoptEvidence(project.project_id, project.revision, selected);
      onProject(response.project);
      setSelected([]);
      setNotice({ tone: "success", text: `已采纳 ${response.evidence.length} 条证据并保存为 r${response.project.revision}。` });
    } catch (reason) {
      setNotice({ tone: "danger", text: reason instanceof Error ? reason.message : "采纳证据失败" });
    } finally {
      setAdopting(false);
    }
  }

  async function upload(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const file = data.get("file");
    if (!(file instanceof File) || !file.size) return;
    setUploading(true);
    setNotice(null);
    try {
      const response = await api.uploadDocument(file, String(data.get("source_type")));
      setNotice({ tone: "success", text: `${response.source_name} 已入库，共 ${response.indexed_chunks} 个检索片段。` });
      event.currentTarget.reset();
    } catch (reason) {
      setNotice({ tone: "danger", text: reason instanceof Error ? reason.message : "入库失败" });
    } finally {
      setUploading(false);
    }
  }

  function toggleEvidence(evidenceId: string) {
    setSelected((current) => current.includes(evidenceId)
      ? current.filter((item) => item !== evidenceId)
      : [...current, evidenceId]);
  }

  return (
    <div className="content-stack">
      <div className="section-heading">
        <div>
          <p className="eyebrow">EVIDENCE LIBRARY</p>
          <h1>规范与项目资料</h1>
          <p>检索结果始终保留来源、定位和原文；选中后才会采纳到当前项目 revision 并进入交付物。</p>
        </div>
      </div>

      <div className="split-grid split-grid-evidence">
        <Panel title="检索证据" eyebrow="RAG SEARCH">
          <form onSubmit={search} className="search-form">
            <input value={query} onChange={(event) => setQuery(event.target.value)} required placeholder="例如：会议室 照度 显色指数" />
            <BusyButton busy={searching} type="submit"><Search size={16} />检索</BusyButton>
          </form>
          <div className="evidence-results">
            {results.length ? results.map((item) => {
              const isSelected = selected.includes(item.evidence_id);
              return (
                <article key={item.evidence_id} className={`evidence-item ${isSelected ? "evidence-selected" : ""}`}>
                  <div className="evidence-meta"><StatusPill status={item.source_type === "standard" ? "success" : "neutral"}>{sourceLabels[item.source_type]}</StatusPill><span>{item.source_name}</span><span>{item.locator ?? "位置未标注"}</span>{item.score !== null ? <code>{Math.round(item.score * 100)}%</code> : null}</div>
                  <blockquote>{item.excerpt}</blockquote>
                  <button className="button button-secondary evidence-adopt" type="button" onClick={() => toggleEvidence(item.evidence_id)} aria-pressed={isSelected}>
                    <BookmarkPlus size={15} />{isSelected ? "已选中" : "选择采纳"}
                  </button>
                </article>
              );
            }) : <EmptyState title="等待检索">输入空间类型和指标名称，检索已入库资料中的可引用原文。</EmptyState>}
          </div>
          {selected.length ? <div className="evidence-adoption-bar"><span>已选择 {selected.length} 条</span><BusyButton busy={adopting} type="button" onClick={adopt}><BookmarkPlus size={16} />采纳至 r{project.revision + 1}</BusyButton></div> : null}
        </Panel>

        <Panel title="资料入库" eyebrow="DOCUMENT INGEST">
          <form onSubmit={upload} className="upload-form">
            <Field label="资料类别"><select name="source_type" defaultValue="project_document"><option value="standard">标准规范</option><option value="project_document">项目资料</option><option value="user_note">人工备注</option></select></Field>
            <Field label="选择文件" hint="支持 PDF、DOCX、Markdown、TXT；最大 50 MB。"><input name="file" type="file" required accept=".pdf,.docx,.md,.txt" /></Field>
            <BusyButton busy={uploading} type="submit"><FileUp size={16} />上传并入库</BusyButton>
          </form>
          <div className="boundary-note"><strong>资料类别不是可信度认证</strong><p>“标准规范”标签只帮助区分来源；仍应人工确认文件版本、完整性和法律效力。</p></div>
          {notice ? <Notice tone={notice.tone}>{notice.text}</Notice> : null}
        </Panel>
      </div>
    </div>
  );
}
