"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronLeft,
  ChevronRight,
  Database,
  FolderOpen,
  LayoutDashboard,
  Menu,
  MessageSquareText,
  Plus,
  RefreshCw,
  Trash2,
  X,
} from "lucide-react";
import { api } from "@/lib/api";
import type { Health, Project, Section } from "@/lib/types";
  import { ConversationPanel } from "./ConversationPanel";
  import { CreateProjectModal } from "./CreateProjectModal";
  import { OverviewPanel } from "./OverviewPanel";
import { BusyButton, Modal, Notice } from "./ui";

const navigation: Array<{ section: Section; label: string; icon: typeof LayoutDashboard }> = [
  { section: "chat", label: "智能对话", icon: MessageSquareText },
  { section: "overview", label: "项目概览", icon: LayoutDashboard },
];

function formatProjectTime(value: string) {
  return new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric" }).format(new Date(value));
}

const initialLoadRetries = 5;
const retryDelayMs = 750;

export function LightingWorkbench() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [project, setProject] = useState<Project | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [section, setSection] = useState<Section>("chat");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const retryTimer = useRef<number | null>(null);

  const load = useCallback(async (preferredProjectId?: string, retriesRemaining = initialLoadRetries) => {
    if (retryTimer.current !== null) {
      window.clearTimeout(retryTimer.current);
      retryTimer.current = null;
    }
    setError(null);
    try {
      const [healthResult, projectResults] = await Promise.all([api.health(), api.projects()]);
      setHealth(healthResult);
      setProjects(projectResults);
      const storedId = preferredProjectId ?? window.localStorage.getItem("lighting-active-project") ?? undefined;
      const selected = projectResults.find((item) => item.project_id === storedId) ?? projectResults[0] ?? null;
      setProject(selected);
      if (selected) window.localStorage.setItem("lighting-active-project", selected.project_id);
      setLoading(false);
      setRefreshing(false);
    } catch (reason) {
      if (retriesRemaining > 0) {
        retryTimer.current = window.setTimeout(() => {
          retryTimer.current = null;
          void load(preferredProjectId, retriesRemaining - 1);
        }, retryDelayMs * (initialLoadRetries - retriesRemaining + 1));
        return;
      }
      setError(reason instanceof Error ? reason.message : "无法连接照明设计 API");
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    const storedSection = window.localStorage.getItem("lighting-active-section") as Section | null;
    setSidebarCollapsed(window.localStorage.getItem("lighting-sidebar-collapsed") === "true");
    if (storedSection && navigation.some((item) => item.section === storedSection)) setSection(storedSection);
    void load();
    return () => {
      if (retryTimer.current !== null) window.clearTimeout(retryTimer.current);
    };
  }, [load]);

  function navigate(next: Section) {
    setSection(next);
    setMobileOpen(false);
    window.localStorage.setItem("lighting-active-section", next);
  }

  function toggleSidebar() {
    setSidebarCollapsed((current) => {
      const next = !current;
      window.localStorage.setItem("lighting-sidebar-collapsed", String(next));
      return next;
    });
  }

  async function selectProject(projectId: string) {
    try {
      const selected = await api.project(projectId);
      setProject(selected);
      window.localStorage.setItem("lighting-active-project", projectId);
      setMobileOpen(false);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "项目读取失败");
    }
  }

  function acceptProject(updated: Project) {
    setProject((current) => {
      if (current?.project_id === updated.project_id && current.revision > updated.revision) return current;
      return updated;
    });
    setProjects((current) => {
      const existing = current.find((item) => item.project_id === updated.project_id);
      if (existing && existing.revision > updated.revision) return current;
      return [updated, ...current.filter((item) => item.project_id !== updated.project_id)];
    });
  }

  async function deleteCurrentProject() {
    if (!project) return;
    const projectId = project.project_id;
    setDeleting(true);
    setDeleteError(null);
    try {
      await api.deleteProject(projectId);
      window.localStorage.removeItem("lighting-active-project");
      window.localStorage.removeItem(`lighting-smart-session:${projectId}`);
      window.localStorage.removeItem(`lighting-clarification:${projectId}`);
      setDeleteOpen(false);
      await load();
    } catch (reason) {
      setDeleteError(reason instanceof Error ? reason.message : "删除项目失败");
    } finally {
      setDeleting(false);
    }
  }

  const activeLabel = useMemo(() => navigation.find((item) => item.section === section)?.label ?? "智能对话", [section]);

  if (loading) {
    return (
      <main className="app-loading" aria-live="polite">
        <div className="app-loading-mark" aria-hidden="true"><span /><span /><span /></div>
        <p>正在载入工作区</p>
      </main>
    );
  }

  return (
    <div className={`chatbot-app ${sidebarCollapsed ? "chatbot-sidebar-collapsed" : ""}`}>
      <aside className={`chatbot-sidebar ${mobileOpen ? "chatbot-sidebar-open" : ""} ${sidebarCollapsed ? "chatbot-sidebar-is-collapsed" : ""}`}>
        <div className="chatbot-sidebar-brand">
          <div className="app-logo" aria-hidden="true"><span /><span /><span /></div>
          <div className="chatbot-sidebar-copy"><strong>光序</strong><small>Lighting design agent</small></div>
          <button className="sidebar-close" onClick={() => setMobileOpen(false)} aria-label="关闭导航"><X size={18} /></button>
          <button className="sidebar-toggle" onClick={toggleSidebar} aria-label={sidebarCollapsed ? "展开侧边栏" : "收起侧边栏"} title={sidebarCollapsed ? "展开侧边栏" : "收起侧边栏"}>
            {sidebarCollapsed ? <ChevronRight size={16} /> : <ChevronLeft size={16} />}
          </button>
        </div>

        <div className="chatbot-sidebar-actions">
          <button className="chatbot-new-chat" onClick={() => setCreateOpen(true)} title="新建项目"><Plus size={17} /><span>新建照明项目</span></button>
        </div>

        <div className="chatbot-sidebar-scroll">
          <div className="sidebar-section-heading"><span>项目</span><small>{projects.length}</small></div>
          <div className="chatbot-project-list" aria-label="项目列表">
            {projects.length ? projects.map((item, index) => (
              <button
                key={item.project_id}
                className={`chatbot-project-item ${project?.project_id === item.project_id ? "active" : ""}`}
                onClick={() => void selectProject(item.project_id)}
                title={item.brief.project_name}
              >
                <span className="project-list-icon"><FolderOpen size={15} /></span>
                <span className="project-list-copy"><strong>{item.brief.project_name || `未命名项目 ${index + 1}`}</strong><small>r{item.revision} · {formatProjectTime(item.updated_at)}</small></span>
              </button>
            )) : <p className="sidebar-empty">还没有项目</p>}
          </div>
        </div>

        <div className="chatbot-sidebar-bottom">
          <nav className="chatbot-workspace-nav" aria-label="当前项目">
            {navigation.map((item) => (
              <button key={item.section} className={section === item.section ? "active" : ""} onClick={() => navigate(item.section)} title={item.label}>
                <item.icon size={16} /><span>{item.label}</span>
              </button>
            ))}
          </nav>
          <div className="chatbot-services">
            <div title={`知识库：${health?.rag_backend ?? "unknown"}`}><Database size={14} /><span><strong>知识库</strong><small>{health?.rag_backend ?? "unknown"}</small></span><i className={health?.status === "ok" ? "online" : ""} /></div>
            <div title={health?.llm_configured ? "模型已配置" : "模型未配置"}><MessageSquareText size={14} /><span><strong>模型服务</strong><small>{health?.llm_configured ? "已连接" : "未配置"}</small></span><i className={health?.llm_configured ? "online" : ""} /></div>
          </div>
        </div>
      </aside>

      {mobileOpen ? <button className="chatbot-sidebar-scrim" onClick={() => setMobileOpen(false)} aria-label="关闭导航遮罩" /> : null}

      <div className="chatbot-main">
        <header className="chatbot-header">
          <div className="chatbot-header-title">
            <button className="mobile-menu" onClick={() => setMobileOpen(true)} aria-label="打开导航"><Menu size={19} /></button>
            <span>{activeLabel}</span>
            {project ? <><i>/</i><strong>{project.brief.project_name}</strong></> : null}
          </div>
          <div className="chatbot-header-actions">
            {project ? <span className="project-revision">r{project.revision}</span> : null}
            {project ? <button className="header-action" onClick={() => { setDeleteError(null); setDeleteOpen(true); }} aria-label="删除当前项目" title="删除当前项目"><Trash2 size={16} /></button> : null}
            <button className="header-action" onClick={() => { setRefreshing(true); void load(project?.project_id); }} aria-label="刷新项目" title="刷新项目"><RefreshCw size={16} className={refreshing ? "spin" : ""} /></button>
          </div>
        </header>

        <main className={`chatbot-workspace chatbot-workspace-${section}`}>
          {error ? (
            <div className="app-error">
              <Notice tone="danger"><strong>无法载入工作区。</strong> {error}</Notice>
              <p>请确认 Python API 已启动：<code>uv run uvicorn lighting_agent.web_api:app --reload</code></p>
              <button className="button button-primary" onClick={() => { setLoading(true); void load(); }}>重新连接</button>
            </div>
          ) : !project ? (
            <section className="chatbot-no-project">
              <div className="app-logo app-logo-large" aria-hidden="true"><span /><span /><span /></div>
              <p className="eyebrow">LIGHTING DESIGN AGENT</p>
              <h1>开始一个照明设计项目</h1>
              <p>创建项目后，可在会话中导入 CAD 平面图、检索规范、完成初步计算、灯具筛选和 DIALux 任务包交付。</p>
              <button className="button button-primary" onClick={() => setCreateOpen(true)}><Plus size={16} />新建项目</button>
            </section>
          ) : section === "overview" ? (
            <OverviewPanel project={project} onStartAgent={() => navigate("chat")} />
          ) : (
            <ConversationPanel key={project.project_id} project={project} health={health} onProject={acceptProject} />
          )}
        </main>
      </div>

      {createOpen ? <CreateProjectModal onClose={() => setCreateOpen(false)} onCreated={(created) => { acceptProject(created); setCreateOpen(false); navigate("chat"); }} /> : null}
      {deleteOpen && project ? (
        <Modal title="删除项目" eyebrow="DELETE PROJECT" onClose={() => { if (!deleting) setDeleteOpen(false); }}>
          <p>将永久删除项目“{project.brief.project_name}”、全部版本、该项目聊天会话和已生成的交付文件。</p>
          <Notice tone="danger">此操作不可撤销；规范资料库和其他项目不会受到影响。</Notice>
          {deleteError ? <Notice tone="danger">{deleteError}</Notice> : null}
          <div className="form-actions">
            <button className="button button-quiet" type="button" onClick={() => setDeleteOpen(false)} disabled={deleting}>取消</button>
            <BusyButton className="button button-danger" busy={deleting} type="button" onClick={() => void deleteCurrentProject()}><Trash2 size={16} />确认删除</BusyButton>
          </div>
        </Modal>
      ) : null}
    </div>
  );
}
