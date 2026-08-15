"use client";

import { useEffect, useRef, useState } from "react";
import {
  Bot,
  Bug,
  Check,
  CircleCheck,
  CircleDot,
  CircleX,
  LoaderCircle,
  RotateCcw,
  UserRound,
  Wrench,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api } from "@/lib/api";
import { unavailableContextUsage } from "@/lib/context-usage";
import type {
  AgentPlanStep,
  AgentStepStatus,
  AgentToolRun,
  ClarificationField,
  ClarificationRequest,
  ContextUsage,
  Health,
  Project,
} from "@/lib/types";
import { ChatComposer } from "./ChatComposer";
import { Notice } from "./ui";

type Message = {
  id: string;
  role: "user" | "assistant";
  content: string;
  streaming?: boolean;
  tools?: AgentToolRun[];
  retrying?: { attempt: number; max: number; detail?: string } | null;
};

const toolLabels: Record<string, string> = {
  get_project: "读取项目",
  create_project: "创建项目",
  ask_user: "请求补充条件",
  update_project_brief: "更新任务书",
  search_evidence: "检索规范证据",
  adopt_evidence: "采纳规范证据",
  add_document: "载入资料",
  calculate_preliminary_lighting: "照明初算",
  check_design_rules: "规则校核",
  search_luminaires: "检索 DIALux 灯具",
  select_luminaires: "确认最终选定灯具",
  generate_design_report: "生成设计报告",
  create_dialux_task_package: "生成 DIALux 任务包",
};

function statusLabel(status: AgentStepStatus | AgentToolRun["status"]) {
  return { pending: "待执行", active: "执行中", done: "已完成", failed: "失败", skipped: "本轮未触发" }[status];
}

function formatDuration(value: number | undefined) {
  if (value === undefined) return null;
  return value >= 1000 ? `${(value / 1000).toFixed(1)} s` : `${value} ms`;
}

function DebugRunPanel({
  project,
  steps,
  tools,
  busy,
  activity,
}: {
  project: Project;
  steps: AgentPlanStep[];
  tools: AgentToolRun[];
  busy: boolean;
  activity: string | null;
}) {
  const stepIcon = (status: AgentStepStatus) => {
    if (status === "done") return <CircleCheck size={14} />;
    if (status === "failed") return <CircleX size={14} />;
    if (status === "active") return <LoaderCircle size={14} className="spin" />;
    return <CircleDot size={14} />;
  };

  return (
    <aside className="debug-run-panel" aria-label="执行调试">
      <header className="debug-run-heading">
        <div><Bug size={15} aria-hidden="true" /><strong>执行调试</strong></div>
        <span className={busy ? "is-running" : ""}>{busy ? activity ?? "运行中" : "空闲"}</span>
      </header>
      <dl className="debug-project-meta">
        <div><dt>项目</dt><dd title={project.project_id}>{project.project_id.slice(0, 12)}</dd></div>
        <div><dt>版本</dt><dd>r{project.revision}</dd></div>
      </dl>
      <section className="debug-run-section">
        <h2>流程</h2>
        {steps.length ? <ol className="debug-step-list">
          {steps.map((step) => <li key={step.id} className={`debug-step debug-step-${step.status}`}>
            <span>{stepIcon(step.status)}</span>
            <div><strong>{step.title}</strong><small>{step.description}</small></div>
            <em>{statusLabel(step.status)}</em>
          </li>)}
        </ol> : <p className="debug-empty">本轮尚未触发项目工具。</p>}
      </section>
      <section className="debug-run-section">
        <h2>工具</h2>
        {tools.length ? <ol className="debug-tool-list">
          {tools.map((tool) => <li key={tool.call_id} className={`debug-tool debug-tool-${tool.status}`}>
            <div className="debug-tool-row">
              <Wrench size={14} aria-hidden="true" />
              <strong>{toolLabels[tool.name] ?? tool.name}</strong>
              <em>{statusLabel(tool.status)}</em>
              {formatDuration(tool.duration_ms) ? <small>{formatDuration(tool.duration_ms)}</small> : null}
            </div>
            <code>{tool.name} · {tool.call_id}</code>
            {tool.input || tool.output ? <details>
              <summary>调试摘要</summary>
              {tool.input ? <><span>输入</span><pre>{JSON.stringify(tool.input, null, 2)}</pre></> : null}
              {tool.output ? <><span>结果</span><pre>{JSON.stringify(tool.output, null, 2)}</pre></> : null}
            </details> : null}
          </li>)}
        </ol> : <p className="debug-empty">工具调用会按实际执行顺序出现在这里。</p>}
      </section>
    </aside>
  );
}

function AssistantMarkdown({ content, streaming, activity }: Pick<Message, "content" | "streaming"> & { activity?: string }) {
  return (
    <div className={`chat-markdown ${streaming ? "chat-markdown-streaming" : ""}`}>
      {content ? <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown> : streaming ? <p className="chat-activity">{activity ?? "正在准备回答…"}</p> : null}
      {streaming ? <span className="streaming-cursor" aria-label="正在生成" /> : null}
    </div>
  );
}

function mergeToolRuns(current: AgentToolRun[], next: AgentToolRun) {
  const index = current.findIndex((tool) => tool.call_id === next.call_id);
  if (!next.name || next.name === "unknown_tool") {
    if (index === -1) return current;
    return current.map((tool, itemIndex) => itemIndex === index ? { ...tool, ...next, name: tool.name } : tool);
  }
  return index === -1
    ? [...current, next]
    : current.map((tool, itemIndex) => itemIndex === index ? { ...tool, ...next } : tool);
}

function failUnfinishedTools(tools: AgentToolRun[] | undefined) {
  return tools?.map((tool) => tool.status === "active" ? { ...tool, status: "failed" as const } : tool);
}

function AgentRunTrace({
  tools,
  streaming,
  activity,
}: {
  tools?: AgentToolRun[];
  streaming?: boolean;
  activity?: string | null;
}) {
  if (!tools?.length) return null;

  const isRunning = Boolean(streaming);
  const summary = isRunning ? activity ?? "正在调用工具" : "工具调用完成";

  return (
    <details className={`agent-run-trace ${isRunning ? "agent-run-trace-running" : ""}`} open={Boolean(streaming)}>
      <summary>
        <span className="agent-run-trace-state">{isRunning ? <LoaderCircle size={14} className="spin" aria-hidden="true" /> : <Check size={14} aria-hidden="true" />}</span>
        <span>{summary}</span>
        <small>{tools.length} 次工具调用</small>
      </summary>
      <div className="agent-run-trace-body">
        <section className="agent-run-trace-section" aria-label="工具调用">
          <ul className="agent-run-tool-list">
            {tools.map((tool) => <li key={tool.call_id} className={`agent-run-tool agent-run-tool-${tool.status}`}>
              {tool.status === "active" ? <LoaderCircle size={14} className="spin" aria-hidden="true" /> : <Wrench size={14} aria-hidden="true" />}
              <span>{toolLabels[tool.name] ?? tool.name}</span>
              <em>{statusLabel(tool.status)}</em>
            </li>)}
          </ul>
        </section>
      </div>
    </details>
  );
}

function fieldValue(values: Record<string, string | string[]>, field: ClarificationField) {
  const value = values[field.field_id];
  return Array.isArray(value) ? value.join("、") : value ?? "";
}

function ClarificationCard({
  request,
  busy,
  onSubmit,
}: {
  request: ClarificationRequest;
  busy: boolean;
  onSubmit: (content: string) => void;
}) {
  const [values, setValues] = useState<Record<string, string | string[]>>(() => (
    Object.fromEntries(request.fields.map((field) => [field.field_id, field.input_type === "multiselect" ? [] : ""]))
  ));
  const [error, setError] = useState<string | null>(null);

  function updateValue(fieldId: string, value: string | string[]) {
    setValues((current) => ({ ...current, [fieldId]: value }));
  }

  function toggleOption(field: ClarificationField, option: string) {
    const current = values[field.field_id];
    const selected = Array.isArray(current) ? current : [];
    updateValue(field.field_id, selected.includes(option) ? selected.filter((item) => item !== option) : [...selected, option]);
  }

  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const missing = request.fields.filter((field) => field.required && !fieldValue(values, field).trim());
    if (missing.length) {
      setError(`请填写：${missing.map((field) => field.label).join("、")}`);
      return;
    }
    const answers = request.fields
      .filter((field) => fieldValue(values, field).trim())
      .map((field) => `- ${field.label}：${fieldValue(values, field).trim()}`)
      .join("\n");
    onSubmit(`已填写“${request.title}”：\n${answers}`);
  }

  return (
    <section className="clarification-card" aria-label={request.title}>
      <div className="clarification-card-heading">
        <p className="eyebrow">NEED YOUR INPUT</p>
        <h3>{request.title}</h3>
        <p>{request.question}</p>
      </div>
      <form onSubmit={submit} className="clarification-form">
        {request.fields.map((field, index) => (
          <label key={field.field_id} className="clarification-field">
            <span className="clarification-field-label"><b>{index + 1}</b>{field.label}{field.required ? <em>必填</em> : null}</span>
            {field.description ? <small>{field.description}</small> : null}
            {field.input_type === "select" && field.options.length ? (
              <select value={fieldValue(values, field)} onChange={(event) => updateValue(field.field_id, event.target.value)} disabled={busy}>
                <option value="">请选择</option>
                {field.options.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
              </select>
            ) : field.input_type === "multiselect" && field.options.length ? (
              <span className="clarification-options">
                {field.options.map((option) => {
                  const selected = Array.isArray(values[field.field_id]) && values[field.field_id].includes(option.value);
                  return <button className={selected ? "selected" : ""} type="button" key={option.value} disabled={busy} onClick={() => toggleOption(field, option.value)}>{option.label}</button>;
                })}
              </span>
            ) : (
              <input
                type={field.input_type === "number" ? "number" : "text"}
                value={fieldValue(values, field)}
                placeholder={field.placeholder ?? "请输入"}
                onChange={(event) => updateValue(field.field_id, event.target.value)}
                disabled={busy}
              />
            )}
          </label>
        ))}
        {error ? <p className="clarification-error">{error}</p> : null}
        <div className="clarification-actions"><span>提交后，智能体会基于这些确认信息继续执行。</span><button className="button button-primary" type="submit" disabled={busy}>确认并继续</button></div>
      </form>
    </section>
  );
}

export function SmartConversation({ project, health, onProject }: { project: Project; health: Health | null; onProject: (project: Project) => void }) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [sessionId, setSessionId] = useState<string | undefined>();
  const [draft, setDraft] = useState("");
  const [attachments, setAttachments] = useState<File[]>([]);
  const [steps, setSteps] = useState<AgentPlanStep[]>([]);
  const [tools, setTools] = useState<AgentToolRun[]>([]);
  const [clarification, setClarification] = useState<ClarificationRequest | null>(null);
  const [activity, setActivity] = useState<string | null>(null);
  const [contextUsage, setContextUsage] = useState<ContextUsage>(() => unavailableContextUsage(health?.llm_context_window_tokens));
  const [busy, setBusy] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [restoring, setRestoring] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [debugOpen, setDebugOpen] = useState(true);
  const transcriptRef = useRef<HTMLDivElement>(null);
  const sessionStorageKey = `lighting-smart-session:${project.project_id}`;
  const clarificationStorageKey = `lighting-clarification:${project.project_id}`;

  useEffect(() => {
    let active = true;
    const storedSessionId = window.localStorage.getItem(sessionStorageKey);
    const storedClarification = window.localStorage.getItem(clarificationStorageKey);
    setMessages([]);
    setSessionId(storedSessionId ?? undefined);
    setDraft("");
    setAttachments([]);
    setSteps([]);
    setTools([]);
    setActivity(null);
    setError(null);
    setContextUsage(unavailableContextUsage(health?.llm_context_window_tokens));
    setDebugOpen(window.localStorage.getItem("lighting-debug-open") !== "false");
    try {
      setClarification(storedClarification ? JSON.parse(storedClarification) as ClarificationRequest : null);
    } catch {
      window.localStorage.removeItem(clarificationStorageKey);
      setClarification(null);
    }
    if (!storedSessionId) {
      setRestoring(false);
      return () => { active = false; };
    }
    setRestoring(true);
    void api.chatHistory(storedSessionId)
      .then((response) => {
        if (!active) return;
        if (!response.messages.length) window.localStorage.removeItem(sessionStorageKey);
        setSessionId(response.messages.length ? response.session_id : undefined);
        setMessages(response.messages.map((message, index) => ({ id: `history-${message.role}-${index}`, role: message.role, content: message.content })));
      })
      .catch(() => {
        if (!active) return;
        window.localStorage.removeItem(sessionStorageKey);
        setSessionId(undefined);
      })
      .finally(() => { if (active) setRestoring(false); });
    return () => { active = false; };
  }, [clarificationStorageKey, health?.llm_context_window_tokens, sessionStorageKey]);

  useEffect(() => {
    const transcript = transcriptRef.current;
    if (transcript) transcript.scrollTo({ top: transcript.scrollHeight, behavior: busy ? "auto" : "smooth" });
  }, [busy, clarification, messages]);

  function updateStep(stepId: string, status: AgentStepStatus) {
    setSteps((current) => current.map((step) => step.id === stepId ? { ...step, status } : step));
  }

  function updateTool(next: AgentToolRun) {
    setTools((current) => mergeToolRuns(current, next));
  }

  function toggleDebug() {
    setDebugOpen((current) => {
      const next = !current;
      window.localStorage.setItem("lighting-debug-open", String(next));
      return next;
    });
  }

  async function send(providedContent?: string) {
    const instruction = providedContent ?? draft.trim();
    if ((!instruction && !attachments.length) || busy || uploading || restoring || (clarification && !providedContent)) return;

    setUploading(true);
    setError(null);
    let uploadedNames: string[] = [];
    let floorPlans: import("@/lib/types").FloorPlan[] = [];
    let projectRevision = project.revision;
    try {
      const uploads: Array<{ floorPlan?: import("@/lib/types").FloorPlan; name: string }> = [];
      for (const file of attachments) {
        if ([".dxf", ".dwg"].includes(file.name.slice(file.name.lastIndexOf(".")).toLowerCase())) {
          const imported = await api.importFloorPlan(project.project_id, projectRevision, file);
          const floorPlan = imported.floor_plan;
          projectRevision = imported.project.revision;
          onProject(imported.project);
          uploads.push({ floorPlan, name: floorPlan.asset.source_name });
          continue;
        }
        const document = await api.uploadDocument(file, "project_document");
        uploads.push({ name: document.source_name });
      }
      uploadedNames = uploads.map((upload) => upload.name);
      floorPlans = uploads.flatMap((upload) => upload.floorPlan ? [upload.floorPlan] : []);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "资料上传失败");
      return;
    } finally {
      setUploading(false);
    }

    const floorPlanContext = floorPlans.map((plan) => {
      const candidateSummary = plan.area_candidates
        .map((candidate, index) => `${index + 1}: ${candidate.area_m2 ?? "未知"} m²`)
        .join("；");
      const applied = plan.selected_area_candidate_index !== null
        ? `系统已采用第 ${plan.selected_area_candidate_index + 1} 个边界更新任务书面积与长宽`
        : "未识别出可换算边界，未改写任务书几何数据";
      return `已解析并写入项目的 CAD 平面图：${plan.asset.source_name}。单位：${plan.drawing_units}；候选闭合边界：${candidateSummary || "未识别"}。${applied}；请提示用户确认或在后续对话中修正。`;
    });
    const content = [
      instruction || "我已上传项目资料，请读取并用于本轮分析。",
      uploadedNames.length ? `已上传文件：${uploadedNames.join("、")}。` : "",
      floorPlanContext.join("\n"),
      uploadedNames.length > floorPlans.length ? "非 CAD 资料已入库，可在需要时检索其内容。" : "",
    ].filter(Boolean).join("\n\n");
    const timestamp = Date.now();
    const assistantId = `assistant-${timestamp}-${Math.random().toString(36).slice(2)}`;
    let receivedDelta = false;
    setMessages((current) => [
      ...current,
      { id: `user-${timestamp}`, role: "user", content },
      { id: assistantId, role: "assistant", content: "", streaming: true, tools: [], retrying: null },
    ]);
    setDraft("");
    setAttachments([]);
    setSteps([]);
    setTools([]);
    setClarification(null);
    window.localStorage.removeItem(clarificationStorageKey);
    setBusy(true);
    setActivity("正在分析请求…");

    try {
      const response = await api.chatStream({
        message: content,
        session_id: sessionId,
        project_id: project.project_id,
        debug: debugOpen,
      }, {
        onStart: (newSessionId) => { setSessionId(newSessionId); window.localStorage.setItem(sessionStorageKey, newSessionId); },
        onPlan: (nextSteps) => {
          setSteps(nextSteps);
        },
        onStep: (stepId, status) => {
          updateStep(stepId, status);
        },
        onToolStart: (tool) => {
          updateTool(tool);
          setMessages((current) => current.map((message) => message.id === assistantId
            ? { ...message, tools: mergeToolRuns(message.tools ?? [], tool) }
            : message));
        },
        onToolEnd: (tool) => {
          updateTool(tool);
          setMessages((current) => current.map((message) => message.id === assistantId
            ? { ...message, tools: mergeToolRuns(message.tools ?? [], tool) }
            : message));
        },
        onStatus: setActivity,
        onProject,
        onContext: setContextUsage,
        onClarification: (request) => { setClarification(request); window.localStorage.setItem(clarificationStorageKey, JSON.stringify(request)); },
        onDelta: (delta) => {
          receivedDelta = true;
          setActivity("正在整理本轮结果…");
          setMessages((current) => current.map((message) => message.id === assistantId ? { ...message, content: message.content + delta } : message));
        },
        onRetry: (info) => setMessages((current) => current.map((message) => message.id === assistantId ? { ...message, retrying: info } : message)),
      });
      setSessionId(response.session_id);
      window.localStorage.setItem(sessionStorageKey, response.session_id);
      if (response.project) onProject(response.project);
      setMessages((current) => current.map((message) => message.id === assistantId ? { ...message, content: message.content || response.answer, streaming: false } : message));
    } catch (reason) {
      const latestProject = await api.project(project.project_id).catch(() => null);
      if (latestProject) onProject(latestProject);
      setMessages((current) => current.filter((message) => message.id !== assistantId || receivedDelta).map((message) => message.id === assistantId ? { ...message, streaming: false } : message));
      setError(reason instanceof Error ? reason.message : "智能对话请求失败");
    } finally {
      setTools((current) => failUnfinishedTools(current) ?? []);
      setMessages((current) => current.map((message) => message.id === assistantId
        ? { ...message, tools: failUnfinishedTools(message.tools) }
        : message));
      setActivity(null);
      setBusy(false);
    }
  }

  async function clear() {
    if (busy) return;
    if (sessionId) await api.clearChat(sessionId).catch(() => undefined);
    window.localStorage.removeItem(sessionStorageKey);
    window.localStorage.removeItem(clarificationStorageKey);
    setSessionId(undefined);
    setMessages([]);
    setAttachments([]);
    setSteps([]);
    setTools([]);
    setClarification(null);
    setError(null);
    setActivity(null);
  }

  const showObservability = steps.length > 0 || tools.length > 0;
  const statusText = busy ? activity ?? "正在处理请求" : showObservability ? "本轮执行已完成" : "准备就绪";

  return (
    <div className={`conversation-app ${debugOpen ? "conversation-debug-open" : ""}`}>
      <header className="conversation-header">
        <div className="conversation-header-title">
          <span className="agent-mark" aria-hidden="true"><Bot size={16} /></span>
          <div><h1>照明设计助手</h1><p>{project.brief.project_name} · 项目会话会自动保存</p></div>
        </div>
        <div className="conversation-header-actions">
          <span className={`conversation-run-status ${busy ? "is-running" : ""}`}><i />{statusText}</span>
          <button className={`conversation-debug-toggle ${debugOpen ? "active" : ""}`} onClick={toggleDebug} aria-pressed={debugOpen} title={debugOpen ? "关闭执行调试" : "打开执行调试"}>
            <Bug size={16} /><span>调试</span>
          </button>
          <button className="button button-quiet conversation-reset" onClick={clear} disabled={busy} title="新会话"><RotateCcw size={16} /><span>新会话</span></button>
        </div>
      </header>

      {!health?.llm_configured ? <Notice tone="danger">尚未配置 LIGHTING_LLM_API_KEY，智能对话暂不可用；项目数据不会受影响。</Notice> : null}

      <div className="conversation-layout">
        <section className="conversation-thread">
          <div ref={transcriptRef} className="chat-transcript conversation-transcript" aria-live="polite">
            {restoring ? <div className="chat-welcome"><p className="eyebrow">RESTORING SESSION</p><h3>正在恢复本项目的会话</h3></div> : messages.length ? messages.map((message) => (
              <article key={message.id} className={`chat-message chat-${message.role}`}>
                <span className="message-avatar" aria-label={message.role === "user" ? "用户" : "照明设计助手"}>{message.role === "user" ? <UserRound size={15} /> : <Bot size={15} />}</span>
                <div>{message.role === "assistant" ? <>
                  <AgentRunTrace tools={message.tools} streaming={message.streaming} activity={message.streaming ? activity : null} />
                  {message.retrying ? <div className={`chat-retry-notice ${message.streaming ? "is-retrying" : ""}`} title={message.retrying.detail}>
                    {message.streaming ? <LoaderCircle size={12} className="spin" aria-hidden="true" /> : <RotateCcw size={12} aria-hidden="true" />}
                    <span>{message.streaming
                      ? `模型请求失败，正在自动重试（第 ${message.retrying.attempt}/${message.retrying.max} 次）…`
                      : `本轮模型请求自动重试了 ${message.retrying.attempt} 次后恢复`}</span>
                  </div> : null}
                  <AssistantMarkdown content={message.content} streaming={message.streaming} activity={activity ?? undefined} />
                </> : message.content}</div>
              </article>
            )) : <div className="chat-welcome smart-welcome"><p className="eyebrow">LIGHTING DESIGN AGENT</p><h3>从一个问题开始</h3><p>可直接上传 DXF/DWG 平面图；系统会提取可审计的空间几何并用于后续照明设计。项目分析、计算、灯具与交付会自动调度工具并显示执行过程。</p><div>{["上传平面图并检查空间几何", "检查当前任务书还缺什么", "根据已确认条件推荐灯具"].map((text) => <button key={text} onClick={() => setDraft(text)}>{text}</button>)}</div></div>}
            {clarification ? <ClarificationCard request={clarification} busy={busy} onSubmit={(content) => void send(content)} /> : null}
          </div>
          {error ? <div className="conversation-error"><Notice tone="danger">{error}</Notice></div> : null}
          <ChatComposer
            draft={draft}
            attachments={attachments}
            busy={busy || uploading || restoring}
            disabled={!health?.llm_configured || restoring || busy || uploading || Boolean(clarification)}
            usage={contextUsage}
            model={health?.llm_model}
            placeholder={clarification ? "请先完成上方问询，再继续。" : "给照明设计助手发送消息"}
            onDraftChange={setDraft}
            onAttachmentsChange={setAttachments}
            onRemoveAttachment={(file) => setAttachments((current) => current.filter((item) => item !== file))}
            onSubmit={() => void send()}
          />
        </section>
        {debugOpen ? <DebugRunPanel project={project} steps={steps} tools={tools} busy={busy} activity={activity} /> : null}
      </div>
    </div>
  );
}
