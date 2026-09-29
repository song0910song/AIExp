"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowUp, BookOpen, Brain, Check, ChevronDown, Database, FileText,
  FolderOpen, Lightbulb, LoaderCircle, Paperclip, Plus, RotateCcw, Send, Trash2, X,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api } from "@/lib/api";
import type { ChatMessage, DocumentRecord, FloorPlan, Health, Luminaire, Project } from "@/lib/types";
import { CreateProjectModal } from "./CreateProjectModal";

const sessionKey = (id: string) => `lighting-chat:${id}`;
const messageError = (reason: unknown) => reason instanceof Error ? reason.message : "操作失败，请重试";

export function LightingWorkbench() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [project, setProject] = useState<Project | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

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

  useEffect(() => { void refresh(); }, [refresh]);
  function updateProject(next: Project) {
    setProject(next);
    setProjects((old) => old.map((item) => item.project_id === next.project_id ? next : item));
  }
  function selectProject(id: string) {
    window.localStorage.setItem("lighting-active-project", id);
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
    <div className="workbench">
      <aside className="sidebar">
        <div className="brand"><span className="brand-mark"><Lightbulb size={19} /></span><div><strong>LuxBeyond</strong><small>照明设计智能体</small></div></div>
        <div className="sidebar-label">项目 <span>{projects.length}</span></div>
        <button className="button new-project" onClick={() => setCreateOpen(true)}><Plus size={16} />新建项目</button>
        <div className="project-list">
          {projects.map((item) => (
            <button key={item.project_id} className={project?.project_id === item.project_id ? "project-item active" : "project-item"} onClick={() => selectProject(item.project_id)} title={item.brief.project_name}>
              <FolderOpen size={16} /><span>{item.brief.project_name}<small>{item.brief.space_type ?? "未指定用途"}</small></span>
            </button>
          ))}
        </div>
        <div className="sidebar-footer"><span className={health?.llm_configured ? "service-dot online" : "service-dot"} />{health?.llm_model ?? "模型未配置"}</div>
      </aside>
      <main className="workspace-main">
        <header className="topbar"><div><small>当前项目</small><strong>{project?.brief.project_name ?? "未选择项目"}</strong></div><div className="topbar-actions"><select className="mobile-project-select" aria-label="切换项目" value={project?.project_id ?? ""} onChange={(event) => selectProject(event.target.value)}>{projects.map((item) => <option key={item.project_id} value={item.project_id}>{item.brief.project_name}</option>)}</select>{project ? <button className="icon-button" title="删除项目" aria-label="删除项目" onClick={() => void deleteCurrentProject()}><Trash2 size={17} /></button> : null}<button className="icon-button" title="刷新项目" aria-label="刷新项目" onClick={() => void refresh(project?.project_id)}><RotateCcw size={17} /></button></div></header>
        {error ? <div className="banner error-text" role="alert">{error}</div> : null}
        {loading ? <div className="center-state"><LoaderCircle className="spin" size={24} />正在载入工作区</div> : !project ? (
          <div className="center-state"><FolderOpen size={30} /><h2>开始一个照明项目</h2><button className="button primary" onClick={() => setCreateOpen(true)}><Plus size={16} />新建项目</button></div>
        ) : <ChatView key={project.project_id} project={project} health={health} onProject={updateProject} />}
      </main>
      {createOpen ? <CreateProjectModal onClose={() => setCreateOpen(false)} onCreated={(next) => {
        setProjects((old) => [next, ...old]);
        setProject(next);
        setCreateOpen(false);
        window.localStorage.setItem("lighting-active-project", next.project_id);
      }} /> : null}
    </div>
  );
}

function CadReview({ project, onProject }: { project: Project; onProject: (value: Project) => void }) {
  const plan = project.floor_plan;
  const [choice, setChoice] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const selected = plan?.area_candidates[choice];
  const outline = useMemo(() => {
    if (!selected?.points.length) return "";
    const xs = selected.points.map((point) => point.x);
    const ys = selected.points.map((point) => point.y);
    const minX = Math.min(...xs); const minY = Math.min(...ys);
    const scale = Math.min(250 / Math.max(0.01, Math.max(...xs) - minX), 100 / Math.max(0.01, Math.max(...ys) - minY));
    return selected.points.map((point) => `${15 + (point.x - minX) * scale},${120 - (point.y - minY) * scale}`).join(" ");
  }, [selected]);
  if (!plan || plan.selected_area_candidate_index !== null) return null;
  async function confirm() {
    setBusy(true); setError("");
    try { onProject(await api.selectRoom(project, choice)); }
    catch (reason) { setError(messageError(reason)); }
    finally { setBusy(false); }
  }
  return (
    <section className="review-strip" aria-label="CAD 房间边界确认">
      <div className="review-text"><strong><FolderOpen size={15} />{plan.asset.source_name}</strong><small>{plan.drawing_units} · {plan.area_candidates.length} 个边界候选</small>
        {plan.area_candidates.length ? <div className="review-choice"><select aria-label="选择房间边界" value={choice} onChange={(event) => setChoice(Number(event.target.value))}>{plan.area_candidates.map((item, index) => <option value={index} key={index}>候选 {index + 1} · {item.area_m2?.toFixed(2) ?? "未知"} m²</option>)}</select><button className="button primary" disabled={busy || selected?.area_m2 === null} onClick={() => void confirm()}><Check size={15} />确认边界</button></div> : <span className="error-text">未检测到闭合房间轮廓</span>}
        {error ? <span className="error-text">{error}</span> : null}
      </div>
      {outline ? <svg className="review-preview" viewBox="0 0 280 140" role="img" aria-label="房间候选轮廓"><polygon points={outline} fill="#c8e9df" stroke="#087b6c" strokeWidth="2" /></svg> : null}
    </section>
  );
}

function ChatView({ project, health, onProject }: {
  project: Project; health: Health | null; onProject: (value: Project) => void;
}) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [sessionId, setSessionId] = useState<string | undefined>();
  const [draft, setDraft] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [documentScope, setDocumentScope] = useState<"project" | "global">("project");
  const [documents, setDocuments] = useState<DocumentRecord[]>([]);
  const [busy, setBusy] = useState(false);
  const [sending, setSending] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [effort, setEffort] = useState("medium");
  const endRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    let active = true;
    const previous = window.localStorage.getItem(sessionKey(project.project_id));
    setSessionId(previous || undefined);
    if (previous) void api.chatHistory(project.project_id, previous).then((history) => {
      if (active) setMessages(history.messages);
    }).catch((reason) => { if (active) setError(messageError(reason)); });
    void api.documents(project.project_id).then((items) => { if (active) setDocuments(items); }).catch(() => {});
    return () => { active = false; };
  }, [project.project_id]);
  useEffect(() => {
    if (!health?.llm_reasoning_efforts?.length) return;
    const saved = window.localStorage.getItem("lighting-reasoning-effort");
    setEffort(saved && health.llm_reasoning_efforts.includes(saved) ? saved : health.llm_reasoning_effort_default);
  }, [health]);
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages, busy]);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if ((!draft.trim() && !files.length) || busy) return;
    setBusy(true); setError(""); setNotice("");
    let current = project;
    const uploaded: string[] = [];
    try {
      for (const file of files) {
        if (/\.(dxf|dwg)$/i.test(file.name)) {
          current = (await api.importCad(current, file)).project;
          onProject(current);
          uploaded.push(`CAD 图纸「${file.name}」`);
        } else {
          await api.uploadDocument(file, documentScope === "global" ? undefined : current.project_id);
          uploaded.push(`${documentScope === "global" ? "公共资料" : "项目资料"}「${file.name}」`);
        }
      }
      if (files.length) {
        setFiles([]);
        setDocuments(await api.documents(current.project_id));
      }
      const question = [draft.trim(), uploaded.length ? `本轮已上传：${uploaded.join("、")}。请先读取已上传资料，并结合本轮问题分析。` : ""].filter(Boolean).join("\n\n");
      setDraft("");
      setMessages((old) => [...old, { role: "user", content: question }]);
      if (!health?.llm_configured) {
        if (uploaded.length) setNotice("资料已保存；配置问答模型后可继续分析。");
        else setError("尚未配置问答模型，请先设置 LIGHTING_LLM_MODEL 和 LIGHTING_LLM_API_KEY。");
        return;
      }
      const result = await api.chat(project.project_id, question, effort, sessionId);
      setSessionId(result.session_id);
      window.localStorage.setItem(sessionKey(project.project_id), result.session_id);
      if (result.project) onProject(result.project);
      setMessages((old) => [...old, { role: "assistant", content: result.answer }]);
    } catch (reason) { setError(messageError(reason)); }
    finally { setBusy(false); }
  }

  async function resetChat() {
    if (sessionId) await api.clearChat(project.project_id, sessionId).catch(() => {});
    window.localStorage.removeItem(sessionKey(project.project_id));
    setMessages([]); setSessionId(undefined); setError(""); setNotice("");
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
      <header className="section-heading"><div><h1>项目对话</h1><p>{project.brief.space_type ?? project.brief.project_name}</p></div><button className="icon-button" onClick={() => void resetChat()} title="新对话" aria-label="新对话"><Plus size={18} /></button></header>
      <div className="chat-messages">
        <CadReview project={project} onProject={onProject} />
        {project.floor_plan?.selected_area_candidate_index !== null && project.floor_plan ? <div className="context-line"><FolderOpen size={15} /><span>{project.floor_plan.asset.source_name}</span><small>边界已确认</small></div> : null}
        {documents.length ? <details className="context-details"><summary><BookOpen size={15} />项目资料 · {documents.length}<ChevronDown size={14} /></summary><div>{documents.map((document) => <div key={document.source_hash}><FileText size={14} />{document.source_name}</div>)}</div></details> : null}
        {project.luminaires.length ? <details className="context-details product-context"><summary><Lightbulb size={15} />候选灯具 · {project.luminaires.length}<ChevronDown size={14} /></summary><div>{project.luminaires.toReversed().map((item) => <div className="context-product" key={item.luminaire_id}><span><strong>{item.article_name}</strong><small>{item.brand_name ?? "品牌未提供"} · {[item.power_w !== null ? `${item.power_w} W` : null, item.cct_k !== null ? `${item.cct_k} K` : null, item.cri !== null ? `Ra ${item.cri}` : null].filter(Boolean).join(" · ")}</small></span><button className="icon-button" disabled={Boolean(sending)} title="发送到 DIALux" aria-label={`发送 ${item.article_name} 到 DIALux`} onClick={() => void sendLuminaire(item)}>{sending === item.luminaire_id ? <LoaderCircle className="spin" size={16} /> : <Send size={16} />}</button></div>)}</div></details> : null}
        {!messages.length ? <div className="chat-empty"><Lightbulb size={26} /><h2>照明设计智能体</h2><p>询问规范、解读图纸，或按条件寻找灯具。</p></div> : messages.map((item, index) => (
          <article key={index} className={`chat-message ${item.role}`}><div className="message-label">{item.role === "user" ? "你" : "照明助手"}</div><div className="message-content"><ReactMarkdown remarkPlugins={[remarkGfm]}>{item.content}</ReactMarkdown></div></article>
        ))}
        {busy ? <div className="chat-working"><LoaderCircle className="spin" size={16} />正在处理</div> : null}
        <div ref={endRef} />
      </div>
      {error ? <div className="inline-error" role="alert">{error}</div> : null}
      {notice ? <div className="notice" role="status">{notice}</div> : null}
      <form className="chat-compose" onSubmit={(event) => void submit(event)}>
        {files.length ? <div className="compose-files">{files.map((file, index) => <span key={index}><FileText size={13} />{file.name}<button type="button" aria-label={`移除 ${file.name}`} onClick={() => setFiles((old) => old.filter((_, position) => position !== index))}><X size={12} /></button></span>)}</div> : null}
        <div className="compose-input"><textarea value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="询问规范、分析图纸、检索灯具…" rows={2} disabled={busy} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }} /><button className="icon-button send-button" type="submit" disabled={(!draft.trim() && !files.length) || busy} title="发送" aria-label="发送"><ArrowUp size={19} /></button></div>
        <div className="compose-footer"><button className="icon-button" type="button" disabled={busy} title="添加 CAD 或资料文件" aria-label="添加 CAD 或资料文件" onClick={() => fileInputRef.current?.click()}><Paperclip size={18} /></button><input ref={fileInputRef} type="file" hidden multiple accept=".dxf,.dwg,.pdf,.docx,.md,.txt" onChange={(event) => { setFiles((old) => [...old, ...Array.from(event.target.files ?? [])]); event.target.value = ""; }} /><label className="scope-select" title="本轮上传的文档范围"><BookOpen size={14} /><select aria-label="文档范围" value={documentScope} onChange={(event) => setDocumentScope(event.target.value as "project" | "global")}><option value="project">项目资料</option><option value="global">公共规范</option></select></label><span className="compose-spacer" />{health?.llm_reasoning_efforts?.length ? <label className="effort-select" title="思考强度"><Brain size={15} /><select aria-label="思考强度" value={effort} onChange={(event) => { setEffort(event.target.value); window.localStorage.setItem("lighting-reasoning-effort", event.target.value); }}>{health.llm_reasoning_effort_options.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}</select></label> : null}<span className="cache-indicator" title={health?.llm_prompt_cache_enabled ? "模型提示词缓存已启用" : "模型提示词缓存未启用"}><Database size={14} />{health?.llm_prompt_cache_enabled ? "缓存开启" : "缓存关闭"}</span><span className="model-label">{health?.llm_model ?? "模型未配置"}</span></div>
      </form>
    </section>
  );
}
