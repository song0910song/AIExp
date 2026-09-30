"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowUp, BookOpen, Brain, Check, ChevronDown, FileText, FolderOpen, Gauge,
  Lightbulb, LoaderCircle, PanelLeftClose, PanelLeftOpen, Paperclip, Plus, RotateCcw,
  Send, Trash2, Upload, Wrench, X,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import { api } from "@/lib/api";
import type { ChatMessage, ContextUsage, DocumentContent, DocumentRecord, Health, Luminaire, Project, ToolCall } from "@/lib/types";
import { CreateProjectModal } from "./CreateProjectModal";
import { SpatialReview } from "./SpatialReview";
import { DocumentPages, StandardsReview } from "./StandardsReview";

const sessionKey = (id: string) => `lighting-chat:${id}`;
const messageError = (reason: unknown) => reason instanceof Error ? reason.message : "操作失败，请重试";
const supportedProjectFile = /\.(dxf|dwg|pdf|docx|md|txt)$/i;
const maxProjectFileBytes = 50 * 1024 * 1024;
const protectedMarkdownPattern = /(```[\s\S]*?```|`[^`\n]*`|\$\$[\s\S]*?\$\$|\$[^$\n]+\$|\\\([\s\S]*?\\\)|\\\[[\s\S]*?\\\])/g;
const bareMathTokenPattern = /(?<![$\\\w])((?:[A-Z](?:[A-Za-z])?|UGR|CCT|LPD)(?:\\?_\{[^{}\n]+\}|\\?_[A-Za-z0-9]+)(?:\\?\^\{[^{}\n]+\}|\\?\^[A-Za-z0-9]+)?)(?!\w)/g;
const fileSizeLabel = (bytes: number) => bytes < 1024
  ? `${bytes} B`
  : bytes < 1024 * 1024
    ? `${(bytes / 1024).toFixed(2)} KB`
    : `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
const usageLabel = (usage: ContextUsage | null) => usage
  ? `${usage.estimated ? "约" : ""}${usage.percentage === 0 && usage.input_tokens > 0 ? "<0.01" : usage.percentage}%`
  : "--";

function assistantMarkdown(content: string): string {
  const withoutInternalLocators = content.replace(
    /\s*[（(]?\s*chunks?\s+\d+(?:\s*(?:[-–—,]\s*\d+))*\s*[）)]?/gi,
    "",
  );
  const protectedParts: string[] = [];
  const protectedContent = withoutInternalLocators.replace(protectedMarkdownPattern, (part) => {
    const marker = `\uE000${protectedParts.length}\uE001`;
    protectedParts.push(/^\$\$[\s\S]*\$\$$/.test(part)
      ? `$$\n${part.slice(2, -2).trim()}\n$$`
      : part);
    return marker;
  });
  const normalized = protectedContent.replace(
    bareMathTokenPattern,
    (token) => `$${token.replace(/\\([_^])/g, "$1")}$`,
  );
  return normalized.replace(/\uE000(\d+)\uE001/g, (_marker, index: string) => protectedParts[Number(index)] ?? "");
}

export function LightingWorkbench() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [project, setProject] = useState<Project | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [globalDocuments, setGlobalDocuments] = useState<DocumentRecord[]>([]);
  const [globalBusy, setGlobalBusy] = useState(false);
  const [globalError, setGlobalError] = useState("");
  const [globalNotice, setGlobalNotice] = useState("");
  const [globalPanelOpen, setGlobalPanelOpen] = useState(false);
  const [selectedGlobalHash, setSelectedGlobalHash] = useState<string | null>(null);
  const [globalContent, setGlobalContent] = useState<DocumentContent | null>(null);
  const [globalDetailBusy, setGlobalDetailBusy] = useState(false);
  const [globalDetailError, setGlobalDetailError] = useState("");
  const globalInputRef = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async (preferred?: string) => {
    try {
      const [items, state] = await Promise.all([api.projects(), api.health()]);
      setProjects(items);
      setHealth(state);
      const id = preferred ?? window.localStorage.getItem("lighting-active-project");
      const selected = items.find((item) => item.project_id === id) ?? items[0] ?? null;
      setProject(selected);
      if (selected) window.localStorage.setItem("lighting-active-project", selected.project_id);
      setError("");
    } catch (reason) { setError(messageError(reason)); }
    finally { setLoading(false); }
  }, []);

  useEffect(() => {
    void refresh();
    void api.documents().then(setGlobalDocuments).catch((reason) => setGlobalError(messageError(reason)));
    setSidebarCollapsed(window.localStorage.getItem("lighting-sidebar-collapsed") === "true");
  }, [refresh]);
  useEffect(() => {
    if (!globalPanelOpen) return;
    setSelectedGlobalHash((old) => globalDocuments.some((item) => item.source_hash === old)
      ? old : globalDocuments[0]?.source_hash ?? null);
  }, [globalPanelOpen, globalDocuments]);
  useEffect(() => {
    if (!globalPanelOpen || !selectedGlobalHash) return;
    let active = true;
    setGlobalContent(null); setGlobalDetailError(""); setGlobalDetailBusy(true);
    void api.document(selectedGlobalHash).then((item) => {
      if (active) setGlobalContent(item);
    }).catch((reason) => {
      if (active) setGlobalDetailError(messageError(reason));
    }).finally(() => { if (active) setGlobalDetailBusy(false); });
    return () => { active = false; };
  }, [globalPanelOpen, selectedGlobalHash]);
  function toggleSidebar() {
    if (window.matchMedia("(max-width: 680px)").matches) setMobileOpen((old) => !old);
    else setSidebarCollapsed((old) => {
      window.localStorage.setItem("lighting-sidebar-collapsed", String(!old));
      return !old;
    });
  }
  async function uploadGlobalDocuments(files: FileList | null) {
    if (!files?.length || globalBusy) return;
    const pending = Array.from(files);
    setGlobalBusy(true); setGlobalError(""); setGlobalNotice("");
    let uploaded = 0;
    let latestHash: string | null = null;
    try {
      for (const file of pending) {
        const result = await api.uploadDocument(file);
        latestHash = result.sha256;
        uploaded += 1;
      }
      setGlobalNotice(`已上传 ${uploaded} 份全局资料，所有项目均可检索。`);
    } catch (reason) { setGlobalError(`已上传 ${uploaded} 份；${messageError(reason)}`); }
    finally {
      try {
        const current = await api.documents();
        setGlobalDocuments(current);
        if (uploaded) setSelectedGlobalHash(current.find((item) => item.source_hash === latestHash)?.source_hash ?? current[0]?.source_hash ?? null);
      }
      catch (reason) { setGlobalError(messageError(reason)); }
      setGlobalBusy(false);
    }
  }
  function updateProject(next: Project) {
    setProject(next);
    setProjects((old) => old.map((item) => item.project_id === next.project_id ? next : item));
  }
  function selectProject(id: string) {
    window.localStorage.setItem("lighting-active-project", id);
    setMobileOpen(false);
    void refresh(id);
  }
  async function deleteCurrentProject() {
    if (!project || !window.confirm(`删除项目「${project.brief.project_name}」及其项目文件、资料和会话？此操作不可恢复。`)) return;
    try {
      await api.deleteProject(project.project_id);
      window.localStorage.removeItem(sessionKey(project.project_id));
      window.localStorage.removeItem("lighting-active-project");
      await refresh("");
    } catch (reason) { setError(messageError(reason)); }
  }

  return (
    <div className={`workbench${sidebarCollapsed ? " sidebar-collapsed" : ""}${mobileOpen ? " sidebar-open" : ""}`}>
      {mobileOpen ? <button className="sidebar-backdrop" aria-label="关闭侧边栏" onClick={() => setMobileOpen(false)} /> : null}
      <aside className="sidebar" aria-label="项目与全局资料">
        <div className="sidebar-heading"><div className="brand"><span className="brand-mark"><Lightbulb size={19} /></span><div><strong>LuxBeyond</strong><small>照明设计智能体</small></div></div><button className="sidebar-toggle icon-button" title={sidebarCollapsed ? "展开侧边栏" : "折叠侧边栏"} aria-label={sidebarCollapsed ? "展开侧边栏" : "折叠侧边栏"} aria-expanded={!sidebarCollapsed} onClick={toggleSidebar}>{sidebarCollapsed ? <PanelLeftOpen size={18} /> : <PanelLeftClose size={18} />}</button><button className="mobile-close icon-button" title="关闭侧边栏" aria-label="关闭侧边栏" onClick={() => setMobileOpen(false)}><X size={18} /></button></div>
        <div className="sidebar-label">项目 <span>{projects.length}</span></div>
        <button className="button new-project" title="新建项目" onClick={() => { setMobileOpen(false); setCreateOpen(true); }}><Plus size={16} /><span>新建项目</span></button>
        <div className="project-list">
          {projects.map((item) => (
            <button key={item.project_id} className={project?.project_id === item.project_id ? "project-item active" : "project-item"} onClick={() => selectProject(item.project_id)} title={item.brief.project_name}>
              <FolderOpen size={16} /><span>{item.brief.project_name}<small>{item.brief.space_type ?? "未指定用途"}</small></span>
            </button>
          ))}
        </div>
        <section className="global-documents" aria-label="全局资料">
          <div className="sidebar-label">全局资料 <span>{globalDocuments.length}</span></div>
          <button className="button global-upload" title="打开全局资料（所有项目可用）" aria-expanded={globalPanelOpen} onClick={() => { setMobileOpen(false); setGlobalPanelOpen(true); }}><Upload size={16} /><span>上传全局资料</span></button>
        </section>
        <div className="sidebar-footer"><span className={health?.llm_configured ? "service-dot online" : "service-dot"} />{health?.llm_model ?? "模型未配置"}</div>
      </aside>
      <main className="workspace-main">
        <header className="topbar"><button className="mobile-sidebar-toggle icon-button" title="打开侧边栏" aria-label="打开侧边栏" aria-expanded={mobileOpen} onClick={toggleSidebar}><PanelLeftOpen size={19} /></button><div className="topbar-project"><small>当前项目</small><strong>{project?.brief.project_name ?? "未选择项目"}</strong></div><div className="topbar-actions">{project ? <button className="icon-button" title="删除项目" aria-label="删除项目" onClick={() => void deleteCurrentProject()}><Trash2 size={17} /></button> : null}<button className="icon-button" title="刷新项目" aria-label="刷新项目" onClick={() => void refresh(project?.project_id)}><RotateCcw size={17} /></button></div></header>
        {error ? <div className="banner error-text" role="alert">{error}</div> : null}
        {loading ? <div className="center-state"><LoaderCircle className="spin" size={24} />正在载入工作区</div> : !project ? (
          <div className="center-state"><FolderOpen size={30} /><h2>开始一个照明项目</h2><button className="button primary" onClick={() => setCreateOpen(true)}><Plus size={16} />新建项目</button></div>
        ) : <ChatView key={project.project_id} project={project} health={health} onProject={updateProject} />}
      </main>
      {globalPanelOpen ? <>
        <button className="global-panel-backdrop" aria-label="关闭全局资料" onClick={() => setGlobalPanelOpen(false)} />
        <aside className="global-panel" aria-label="全局资料详情">
          <header className="global-panel-heading"><div><h2>全局资料</h2><small>所有项目可检索 · {globalDocuments.length} 份</small></div><button className="icon-button" title="关闭资料面板" aria-label="关闭资料面板" onClick={() => setGlobalPanelOpen(false)}><X size={18} /></button></header>
          <div className="global-panel-upload"><button className="button primary" disabled={globalBusy} onClick={() => globalInputRef.current?.click()}>{globalBusy ? <LoaderCircle className="spin" size={16} /> : <Upload size={16} />}上传资料</button><input ref={globalInputRef} type="file" hidden multiple accept=".pdf,.docx,.md,.txt" onChange={(event) => { void uploadGlobalDocuments(event.target.files); event.target.value = ""; }} /><small>PDF、DOCX、MD、TXT</small></div>
          {globalError ? <div className="inline-error" role="alert">{globalError}</div> : null}
          {globalNotice ? <div className="notice" role="status">{globalNotice}</div> : null}
          <div className="global-panel-list" aria-label="已上传的全局资料">
            {globalDocuments.length ? globalDocuments.map((item) => <button className={selectedGlobalHash === item.source_hash ? "global-panel-item active" : "global-panel-item"} key={item.source_hash} title={item.source_name} onClick={() => setSelectedGlobalHash(item.source_hash)}><FileText size={16} /><span><strong>{item.source_name}</strong><small>{item.page_count ? `${item.page_count} 页 · ` : ""}{item.indexed_chunks} 个索引片段</small></span></button>) : <p className="global-panel-empty">暂无全局资料</p>}
          </div>
          {selectedGlobalHash ? <section className="global-panel-detail">
            {globalDetailBusy ? <div className="global-panel-empty"><LoaderCircle className="spin" size={16} />正在读取资料</div> : globalDetailError ? <div className="inline-error" role="alert">{globalDetailError}</div> : globalContent ? <>
              <h3>{globalContent.source_name}</h3>
              <div className="global-detail-meta"><span>{globalContent.page_count ? `${globalContent.page_count} 页` : "文本文档"}</span><span>{globalContent.indexed_chunks} 个索引片段</span><span>{new Date(globalContent.indexed_at).toLocaleString("zh-CN")}</span></div>
              <DocumentPages document={globalContent} />
            </> : null}
          </section> : null}
        </aside>
      </> : null}
      {createOpen ? <CreateProjectModal onClose={() => setCreateOpen(false)} onCreated={(next) => {
        setProjects((old) => [next, ...old]);
        setProject(next);
        setCreateOpen(false);
        window.localStorage.setItem("lighting-active-project", next.project_id);
      }} /> : null}
    </div>
  );
}

function ChatView({ project, health, onProject }: {
  project: Project; health: Health | null; onProject: (value: Project) => void;
}) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [sessionId, setSessionId] = useState<string | undefined>();
  const [draft, setDraft] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [documents, setDocuments] = useState<DocumentRecord[]>([]);
  const [busy, setBusy] = useState(false);
  const [workingStatus, setWorkingStatus] = useState("");
  const [sending, setSending] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [effort, setEffort] = useState("medium");
  const [contextUsage, setContextUsage] = useState<ContextUsage | null>(null);
  const messagesRef = useRef<HTMLDivElement>(null);
  const followRef = useRef(true);
  const streamRef = useRef<AbortController | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    let active = true;
    const previous = window.localStorage.getItem(sessionKey(project.project_id));
    setSessionId(previous || undefined);
    if (previous) void api.chatHistory(project.project_id, previous).then((history) => {
      if (active) {
        setMessages(history.messages);
        setContextUsage(history.messages.toReversed().find((item) => item.context_usage)?.context_usage ?? null);
      }
    }).catch((reason) => { if (active) setError(messageError(reason)); });
    void api.documents(project.project_id).then((items) => { if (active) setDocuments(items); }).catch(() => {});
    return () => { active = false; streamRef.current?.abort(); };
  }, [project.project_id]);
  useEffect(() => {
    if (!health?.llm_reasoning_efforts?.length) return;
    const saved = window.localStorage.getItem("lighting-reasoning-effort");
    setEffort(saved && health.llm_reasoning_efforts.includes(saved) ? saved : health.llm_reasoning_effort_default);
  }, [health]);
  useEffect(() => {
    const list = messagesRef.current;
    if (list && followRef.current) list.scrollTop = list.scrollHeight;
  }, [messages, busy, documents, project.floor_plan]);

  function addFiles(selected: FileList | null) {
    const choices = Array.from(selected ?? []);
    if (!choices.length) return;
    const accepted: File[] = [];
    const rejected: string[] = [];
    for (const file of choices) {
      if (!supportedProjectFile.test(file.name)) rejected.push(`${file.name}：格式不支持`);
      else if (!file.size) rejected.push(`${file.name}：文件为空`);
      else if (file.size > maxProjectFileBytes) rejected.push(`${file.name}：超过 50 MB`);
      else accepted.push(file);
    }
    if (accepted.length) setFiles((old) => [...old, ...accepted]);
    setError(rejected.length ? `${rejected.join("；")}。支持 DXF、DWG、PDF、DOCX、MD、TXT。` : "");
    setNotice("");
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if ((!draft.trim() && !files.length) || busy) return;
    setBusy(true); setError(""); setNotice("");
    setWorkingStatus(files.length ? "正在上传文件" : "正在生成回答");
    let current = project;
    const uploaded: string[] = [];
    let uploading = files.length > 0;
    try {
      for (const file of files) {
        if (/\.(dxf|dwg)$/i.test(file.name)) {
          current = (await api.importCad(current, file)).project;
          onProject(current);
          uploaded.push(`CAD 图纸「${file.name}」`);
        } else {
          await api.uploadDocument(file, current.project_id);
          uploaded.push(`项目资料「${file.name}」`);
        }
        setFiles((old) => old.slice(1));
      }
      if (files.length) {
        uploading = false;
        setWorkingStatus("正在刷新项目资料");
        try { setDocuments(await api.documents(current.project_id)); }
        catch (reason) { setNotice(`文件已保存，但资料列表刷新失败：${messageError(reason)}`); }
      }
      const question = [draft.trim(), uploaded.length ? `本轮已上传：${uploaded.join("、")}。请先读取已上传资料，并结合本轮问题分析。` : ""].filter(Boolean).join("\n\n");
      setDraft("");
      followRef.current = true;
      setMessages((old) => [...old, { role: "user", content: question }]);
      if (!health?.llm_configured) {
        if (uploaded.length) setNotice("资料已保存；配置问答模型后可继续分析。");
        else setError("尚未配置问答模型，请先设置 LIGHTING_LLM_MODEL 和 LIGHTING_LLM_API_KEY。");
        return;
      }
      setWorkingStatus("正在生成回答");
      setMessages((old) => [...old, { role: "assistant", content: "", tool_calls: [] }]);
      const controller = new AbortController();
      streamRef.current = controller;
      await api.streamChat(project.project_id, question, effort, sessionId, (event) => {
        if (event.type === "delta" || event.type === "tool") {
          setMessages((old) => {
            const last = old.at(-1);
            if (!last || last.role !== "assistant") return old;
            if (event.type === "delta") return [...old.slice(0, -1), { ...last, content: last.content + event.text }];
            const calls = last.tool_calls ?? [];
            const exists = calls.some((call) => call.id === event.id);
            const updated: ToolCall = {
              id: event.id, name: event.name, input: event.input, status: event.status, summary: event.summary,
            };
            return [...old.slice(0, -1), {
              ...last, tool_calls: exists ? calls.map((call) => call.id === event.id ? updated : call) : [...calls, updated],
            }];
          });
        } else if (event.type === "done") {
          setSessionId(event.session_id);
          setContextUsage(event.context_usage);
          window.localStorage.setItem(sessionKey(project.project_id), event.session_id);
          if (event.project) onProject(event.project);
        }
      }, controller.signal);
    } catch (reason) {
      setError(uploading ? `文件上传失败：${messageError(reason)}` : messageError(reason));
      if (uploading && uploaded.length) {
        setNotice(`已保存 ${uploaded.join("、")}；未完成的文件仍在输入框中。`);
        void api.documents(current.project_id).then(setDocuments).catch(() => {});
      }
    }
    finally { streamRef.current = null; setBusy(false); setWorkingStatus(""); }
  }

  async function resetChat() {
    if (busy) return;
    if (sessionId) await api.clearChat(project.project_id, sessionId).catch(() => {});
    window.localStorage.removeItem(sessionKey(project.project_id));
    setMessages([]); setSessionId(undefined); setContextUsage(null); setError(""); setNotice("");
  }
  async function sendLuminaire(item: Luminaire) {
    setSending(item.luminaire_id); setError(""); setNotice("");
    try {
      await api.sendToDialux(project.project_id, item.luminaire_id);
      setNotice(`已请求本机 DIALux 导入「${item.article_name}」，请在软件内确认。`);
    } catch (reason) { setError(messageError(reason)); }
    finally { setSending(""); }
  }

  return (
    <section className="chat-view">
      <header className="section-heading"><div><h1>项目对话</h1><p>{project.brief.space_type ?? project.brief.project_name}</p></div><button className="icon-button" disabled={busy} onClick={() => void resetChat()} title="新对话" aria-label="新对话"><Plus size={18} /></button></header>
      <div className="chat-messages" ref={messagesRef} onScroll={(event) => { const list = event.currentTarget; followRef.current = list.scrollHeight - list.scrollTop - list.clientHeight < 120; }}>
        <SpatialReview project={project} onProject={onProject} />
        <StandardsReview project={project} onProject={onProject} />
        {project.floor_plan?.selected_area_candidate_index !== null && project.floor_plan ? <div className="context-line"><FolderOpen size={15} /><span>{project.floor_plan.asset.source_name}</span><small>边界已确认</small></div> : null}
        {documents.length ? <details className="context-details"><summary><BookOpen size={15} />项目资料 · {documents.length}<ChevronDown size={14} /></summary><div>{documents.map((document) => <div key={document.source_hash}><FileText size={14} />{document.source_name}</div>)}</div></details> : null}
        {project.luminaires.length ? <details className="context-details product-context"><summary><Lightbulb size={15} />候选灯具 · {project.luminaires.length}<ChevronDown size={14} /></summary><div>{project.luminaires.toReversed().map((item) => <div className="context-product" key={item.luminaire_id}><span><strong>{item.article_name}</strong><small>{item.brand_name ?? "品牌未提供"} · {[item.power_w !== null ? `${item.power_w} W` : null, item.cct_k !== null ? `${item.cct_k} K` : null, item.cri !== null ? `Ra ${item.cri}` : null].filter(Boolean).join(" · ")}</small></span><button className="icon-button" disabled={Boolean(sending)} title="发送到 DIALux" aria-label={`发送 ${item.article_name} 到 DIALux`} onClick={() => void sendLuminaire(item)}>{sending === item.luminaire_id ? <LoaderCircle className="spin" size={16} /> : <Send size={16} />}</button></div>)}</div></details> : null}
        {!messages.length ? <div className="chat-empty"><Lightbulb size={26} /><h2>照明设计智能体</h2><p>询问规范、解读图纸，或按条件寻找灯具。</p></div> : messages.map((item, index) => (
          <article key={index} className={`chat-message ${item.role}`}><div className="message-label">{item.role === "user" ? "你" : "照明助手"}</div>{item.tool_calls?.length ? <details className="tool-process" open={index === messages.length - 1 && busy}><summary><Wrench size={14} />工具调用 · {item.tool_calls.length} 项<ChevronDown size={14} /></summary><div>{item.tool_calls.map((call) => <div className="tool-step" key={call.id}><span className={call.status === "running" ? "tool-status running" : call.status === "error" ? "tool-status failed" : "tool-status completed"}>{call.status === "running" ? <LoaderCircle className="spin" size={13} /> : call.status === "error" ? <X size={13} /> : <Check size={13} />}</span><span><strong>{({ get_project: "读取项目与 CAD", search_evidence: "检索资料", search_luminaires: "检索 DIALux 灯具" } as Record<string, string>)[call.name] ?? call.name}</strong>{call.input ? <small title={call.input}>{call.input}</small> : null}{call.summary ? <small>{call.summary}</small> : null}</span></div>)}</div></details> : null}{item.content ? <div className="message-content"><ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeKatex]} components={{ table: ({ node: _node, ...props }) => <div className="markdown-table-scroll"><table {...props} /></div> }}>{item.role === "assistant" ? assistantMarkdown(item.content) : item.content}</ReactMarkdown></div> : null}</article>
        ))}
        {busy ? <div className="chat-working" role="status"><LoaderCircle className="spin" size={16} />{workingStatus}</div> : null}
      </div>
      {error ? <div className="inline-error" role="alert">{error}</div> : null}
      {notice ? <div className="notice" role="status">{notice}</div> : null}
      <form className="chat-compose" onSubmit={(event) => void submit(event)}>
        {files.length ? <div className="compose-files" aria-label="待发送的项目附件" aria-live="polite">{files.map((file, index) => <div className="compose-file" key={`${file.name}-${file.lastModified}-${index}`}><span className="compose-file-icon"><FileText size={22} /></span><span className="compose-file-info"><strong title={file.name}>{file.name}</strong><small>{file.name.split(".").at(-1)?.toUpperCase()} · {fileSizeLabel(file.size)} · {workingStatus === "正在上传文件" ? "上传中" : "待发送"}</small></span><button className="icon-button" type="button" disabled={busy} title={`移除 ${file.name}`} aria-label={`移除 ${file.name}`} onClick={() => setFiles((old) => old.filter((_, position) => position !== index))}><X size={15} /></button></div>)}</div> : null}
        <div className="compose-input"><textarea value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="询问规范、分析图纸、检索灯具…" rows={2} disabled={busy} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }} /><button className="icon-button send-button" type="submit" disabled={(!draft.trim() && !files.length) || busy} title="发送" aria-label="发送"><ArrowUp size={19} /></button></div>
        <div className="compose-footer"><button className="icon-button" type="button" disabled={busy} title="添加项目 CAD 或资料文件" aria-label="添加项目 CAD 或资料文件" onClick={() => fileInputRef.current?.click()}><Paperclip size={18} /></button><input ref={fileInputRef} type="file" hidden multiple accept=".dxf,.dwg,.pdf,.docx,.md,.txt" onChange={(event) => { addFiles(event.target.files); event.target.value = ""; }} /><span className="compose-scope">项目资料</span><span className="compose-spacer" />{health?.llm_reasoning_efforts?.length ? <label className="effort-select" title="思考强度"><Brain size={15} /><select aria-label="思考强度" value={effort} onChange={(event) => { setEffort(event.target.value); window.localStorage.setItem("lighting-reasoning-effort", event.target.value); }}>{health.llm_reasoning_effort_options.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}</select></label> : null}<span className="context-meter" title={contextUsage ? `上次请求输入 ${contextUsage.input_tokens.toLocaleString()} / ${contextUsage.window_tokens.toLocaleString()} token${contextUsage.estimated ? "（按默认窗口或估算用量）" : ""}` : "尚无上下文用量"}><Gauge size={14} />上下文 {usageLabel(contextUsage)}<span className="context-meter-track"><span style={{ width: `${Math.min(100, contextUsage?.percentage ?? 0)}%` }} /></span></span><span className="model-label">{health?.llm_model ?? "模型未配置"}</span></div>
      </form>
    </section>
  );
}
