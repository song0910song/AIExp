"use client";

import { useRef, useState } from "react";
import { CheckCircle2, FileText, Search, Upload, X } from "lucide-react";
import { api } from "@/lib/api";
import type { Evidence } from "@/lib/types";
import { BusyButton, Field, Notice, Panel, StatusPill } from "./ui";

type UploadRecord = {
  name: string;
  sourceType: string;
  chunks: number;
  sha256: string;
};

const sourceTypes = [
  { value: "standard", label: "规范标准", hint: "国家标准、行业标准、企业规范" },
  { value: "project_document", label: "设计资料", hint: "通用设计说明、案例和技术资料" },
  { value: "user_note", label: "个人笔记", hint: "团队沉淀的经验与备注" },
] as const;

function formatSourceType(value: string) {
  return sourceTypes.find((item) => item.value === value)?.label ?? value;
}

export function KnowledgeBasePanel() {
  const inputRef = useRef<HTMLInputElement>(null);
  const [sourceType, setSourceType] = useState<(typeof sourceTypes)[number]["value"]>("standard");
  const [files, setFiles] = useState<File[]>([]);
  const [uploads, setUploads] = useState<UploadRecord[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<Evidence[]>([]);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);

  function addFiles(next: FileList | null) {
    if (!next) return;
    const accepted = Array.from(next).filter((file) => /\.(pdf|docx|md|txt)$/i.test(file.name));
    setFiles((current) => [...current, ...accepted.filter((file) => !current.some((item) => item.name === file.name && item.size === file.size))]);
    setUploadError(accepted.length === 0 ? "请选择 PDF、DOCX、Markdown 或 TXT 文件" : null);
  }

  function removeFile(index: number) {
    setFiles((current) => current.filter((_, itemIndex) => itemIndex !== index));
  }

  async function uploadFiles() {
    if (!files.length) return;
    setUploading(true);
    setUploadError(null);
    try {
      const records: UploadRecord[] = [];
      for (const file of files) {
        const uploaded = await api.uploadDocument(file, sourceType);
        records.push({ name: uploaded.source_name, sourceType: uploaded.source_type, chunks: uploaded.indexed_chunks, sha256: uploaded.sha256 });
      }
      setUploads((current) => [...records, ...current]);
      setFiles([]);
      if (inputRef.current) inputRef.current.value = "";
    } catch (reason) {
      setUploadError(reason instanceof Error ? reason.message : "资料入库失败");
    } finally {
      setUploading(false);
    }
  }

  async function search() {
    const value = query.trim();
    if (!value) return;
    setSearching(true);
    setSearchError(null);
    try {
      const response = await api.searchEvidence(value, 6);
      setResults(response.evidence);
    } catch (reason) {
      setSearchError(reason instanceof Error ? reason.message : "资料检索失败");
    } finally {
      setSearching(false);
    }
  }

  return (
    <div className="knowledge-page">
      <div className="knowledge-intro">
        <div>
          <p className="eyebrow">GLOBAL KNOWLEDGE BASE</p>
          <h1>资料入库</h1>
          <p>统一维护规范、设计资料与团队笔记，所有项目都可检索使用。</p>
        </div>
        <StatusPill status="success"><CheckCircle2 size={13} />全局资料库</StatusPill>
      </div>

      <div className="knowledge-grid">
        <Panel title="导入资料" eyebrow="INGEST DOCUMENTS" className="knowledge-upload-panel">
          <div className="knowledge-form-grid">
            <Field label="资料分类" hint={sourceTypes.find((item) => item.value === sourceType)?.hint}>
              <select value={sourceType} onChange={(event) => setSourceType(event.target.value as typeof sourceType)}>
                {sourceTypes.map((item) => <option value={item.value} key={item.value}>{item.label}</option>)}
              </select>
            </Field>
          </div>
          <button className="knowledge-dropzone" type="button" onClick={() => inputRef.current?.click()} onDragOver={(event) => event.preventDefault()} onDrop={(event) => { event.preventDefault(); addFiles(event.dataTransfer.files); }}>
            <span className="knowledge-dropzone-icon"><Upload size={20} /></span>
            <strong>选择或拖入资料文件</strong>
            <small>支持 PDF、DOCX、Markdown、TXT，单个文件不超过 50 MB</small>
            <input ref={inputRef} type="file" accept=".pdf,.docx,.md,.txt" multiple hidden onChange={(event) => addFiles(event.target.files)} />
          </button>
          {files.length ? <div className="knowledge-file-list">
            {files.map((file, index) => <div className="knowledge-file" key={`${file.name}-${file.size}`}><FileText size={16} /><span>{file.name}<small>{Math.ceil(file.size / 1024)} KB</small></span><button type="button" onClick={() => removeFile(index)} aria-label={`移除 ${file.name}`} title="移除"><X size={15} /></button></div>)}
          </div> : null}
          {uploadError ? <Notice tone="danger">{uploadError}</Notice> : null}
          <div className="form-actions knowledge-actions"><small>入库后资料会作为全局证据参与项目检索。</small><BusyButton busy={uploading} className="button button-primary" type="button" disabled={!files.length} onClick={() => void uploadFiles()}><Upload size={15} />开始入库</BusyButton></div>
        </Panel>

        <Panel title="资料检索" eyebrow="EVIDENCE SEARCH" className="knowledge-search-panel">
          <div className="knowledge-searchbar"><input value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") void search(); }} placeholder="检索照度、UGR、会议室等关键词" /><button className="icon-button" type="button" onClick={() => void search()} disabled={searching || !query.trim()} aria-label="检索资料" title="检索资料"><Search size={17} /></button></div>
          {searchError ? <Notice tone="danger">{searchError}</Notice> : null}
          {results.length ? <div className="knowledge-results">{results.map((item) => <article className="knowledge-result" key={item.evidence_id}><div className="knowledge-result-heading"><strong>{item.source_name}</strong><span>{formatSourceType(item.source_type)}</span></div><p>{item.excerpt}</p><small>{item.locator ?? "资料片段"} · {item.evidence_id.slice(0, 12)}</small></article>)}</div> : <div className="knowledge-search-empty"><Search size={20} /><p>输入关键词查看全局资料证据</p></div>}
        </Panel>
      </div>

      <Panel title="最近入库" eyebrow="RECENT INGESTIONS" className="knowledge-history-panel">
        {uploads.length ? <div className="knowledge-history-list">{uploads.map((item, index) => <div className="knowledge-history-row" key={`${item.sha256}-${index}`}><span className="knowledge-history-file"><FileText size={16} /><strong>{item.name}</strong></span><span>{formatSourceType(item.sourceType)}</span><span>{item.chunks} 个资料片段</span><code>{item.sha256.slice(0, 16)}…</code><StatusPill status="success">已入库</StatusPill></div>)}</div> : <div className="knowledge-history-empty">本次会话尚未新增资料</div>}
      </Panel>
    </div>
  );
}
