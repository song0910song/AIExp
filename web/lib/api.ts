import type { AgentPlanStep, AgentStepStatus, AgentToolRun, ClarificationRequest, ContextUsage, DesignBrief, FloorPlanImport, Health, Project } from "./types";

const API_ROOT = "/backend";

type ChatPayload = { message: string; session_id?: string; project_id?: string; debug?: boolean };
type ChatStreamEvent =
  | { type: "start"; session_id: string }
  | { type: "plan"; steps: AgentPlanStep[] }
  | { type: "step"; step_id: string; status: AgentStepStatus }
  | { type: "tool_start"; call_id: string; name: string; input?: Record<string, unknown> | unknown[]; started_at?: string }
  | { type: "tool_end"; call_id: string; name: string; status: "done" | "failed"; output?: Record<string, unknown>; duration_ms?: number }
  | { type: "status"; content: string }
  | { type: "delta"; content: string }
  | { type: "context"; usage: ContextUsage }
  | { type: "retry"; attempt: number; max: number; detail?: string }
  | ({ type: "clarification" } & ClarificationRequest)
  | { type: "project"; project: Project }
  | { type: "done"; session_id: string; answer: string; project?: Project }
  | { type: "error"; detail: string };

type ChatStreamHandlers = {
  onStart?: (sessionId: string) => void;
  onPlan?: (steps: AgentPlanStep[]) => void;
  onStep?: (stepId: string, status: AgentStepStatus) => void;
  onToolStart?: (tool: AgentToolRun) => void;
  onToolEnd?: (tool: AgentToolRun) => void;
  onStatus?: (content: string) => void;
  onDelta?: (content: string) => void;
  onContext?: (usage: ContextUsage) => void;
  onRetry?: (info: { attempt: number; max: number; detail?: string }) => void;
  onClarification?: (request: ClarificationRequest) => void;
  onProject?: (project: Project) => void;
};

type ChatHistoryMessage = {
  role: "user" | "assistant";
  content: string;
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_ROOT}${path}`, {
    ...init,
    headers: init?.body instanceof FormData ? init.headers : { "Content-Type": "application/json", ...init?.headers },
    cache: "no-store",
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(payload.detail ?? `请求失败：${response.status}`);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export const api = {
  health: () => request<Health>("/health"),
  projects: () => request<Project[]>("/projects"),
  project: (id: string) => request<Project>(`/projects/${id}`),
  createProject: (brief: Partial<DesignBrief> & { project_name: string }) =>
    request<Project>("/projects", { method: "POST", body: JSON.stringify(brief) }),
  deleteProject: (id: string) => request<void>(`/projects/${id}`, { method: "DELETE" }),
  updateBrief: (id: string, expected_revision: number, brief: DesignBrief) =>
    request<Project>(`/projects/${id}/brief`, {
      method: "PUT",
      body: JSON.stringify({ expected_revision, brief }),
    }),
  searchEvidence: (query: string, top_k = 3) =>
    request<{ evidence: import("./types").Evidence[]; formatted: string }>("/evidence/search", {
      method: "POST",
      body: JSON.stringify({ query, top_k }),
    }),
  adoptEvidence: (id: string, expected_revision: number, evidence_ids: string[]) =>
    request<{ evidence: import("./types").Evidence[]; project: Project }>(`/projects/${id}/evidence`, {
      method: "POST",
      body: JSON.stringify({ expected_revision, evidence_ids }),
    }),
  uploadDocument: (file: File, sourceType: string) => {
    const data = new FormData();
    data.append("file", file);
    data.append("source_type", sourceType);
    return request<{ source_name: string; indexed_chunks: number; sha256: string }>("/documents", {
      method: "POST",
      body: data,
    });
  },
  calculate: (id: string, expected_revision: number, inputs: Record<string, number>) =>
    request<{ calculation: import("./types").Calculation; project: Project }>(`/projects/${id}/calculations`, {
      method: "POST",
      body: JSON.stringify({ expected_revision, inputs }),
    }),
  importFloorPlan: (id: string, expectedRevision: number, file: File) => {
    const data = new FormData();
    data.append("file", file);
    data.append("expected_revision", String(expectedRevision));
    return request<FloorPlanImport>(`/projects/${id}/floor-plan`, { method: "POST", body: data });
  },
  checkRule: (
    id: string,
    expected_revision: number,
    requirements: Array<Record<string, unknown>>,
    observations: Record<string, number | null>,
  ) =>
    request<{ checks: import("./types").RuleCheck[]; project: Project }>(`/projects/${id}/rule-checks`, {
      method: "POST",
      body: JSON.stringify({ expected_revision, requirements, observations }),
    }),
  searchLuminaires: (id: string, payload: Record<string, unknown>) =>
    request<{
      candidates: import("./types").Luminaire[];
      notice: string;
      project?: Project;
      saved_count?: number;
      rebased?: boolean;
    }>(`/projects/${id}/luminaires`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  selectLuminaires: (id: string, expectedRevision: number, luminaireIds: string[]) =>
    request<Project>(`/projects/${id}/selected-luminaires`, {
      method: "PUT",
      body: JSON.stringify({ expected_revision: expectedRevision, luminaire_ids: luminaireIds }),
    }),
  photometryAssets: (id: string) =>
    request<{ assets: import("./types").PhotometryAsset[] }>(`/projects/${id}/photometry`),
  downloadLuminairePhotometry: (id: string, luminaireId: string) =>
    request<{ asset: import("./types").PhotometryAsset }>(
      `/projects/${id}/luminaires/${encodeURIComponent(luminaireId)}/photometry`,
      { method: "POST" },
    ),
  savedLuminairePhotometryUrl: (id: string, luminaireId: string) =>
    `${API_ROOT}/projects/${id}/luminaires/${encodeURIComponent(luminaireId)}/photometry/file`,
  extractedLuminairePhotometryUrl: (id: string, luminaireId: string, relativePath: string) =>
    `${API_ROOT}/projects/${id}/luminaires/${encodeURIComponent(luminaireId)}/photometry/extracted?relative_path=${encodeURIComponent(relativePath)}`,
  removeLuminaire: (id: string, luminaireId: string, expectedRevision: number) =>
    request<Project>(
      `/projects/${id}/luminaires/${encodeURIComponent(luminaireId)}?expected_revision=${expectedRevision}`,
      { method: "DELETE" },
    ),
  generateDeliverable: (id: string, kind: "report" | "dialux-task", revision: number) =>
    request<{ kind: string; filename: string; download_url: string }>(
      `/projects/${id}/deliverables/${kind}?expected_revision=${revision}`,
      { method: "POST" },
    ),
  chat: (payload: ChatPayload) =>
    request<{ session_id: string; answer: string }>("/chat", { method: "POST", body: JSON.stringify(payload) }),
  chatHistory: (sessionId: string) =>
    request<{ session_id: string; messages: ChatHistoryMessage[] }>(`/chat/${sessionId}`),
  chatStream: async (payload: ChatPayload, handlers: ChatStreamHandlers = {}) => {
    const response = await fetch(`${API_ROOT}/chat/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      cache: "no-store",
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({ detail: response.statusText }));
      throw new Error(error.detail ?? `请求失败：${response.status}`);
    }
    if (!response.body) throw new Error("浏览器不支持流式响应");

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    const state: { completed?: { session_id: string; answer: string; project?: Project } } = {};

    const handleLine = (line: string) => {
      if (!line.trim()) return;
      const event = JSON.parse(line) as ChatStreamEvent;
      if (event.type === "start") handlers.onStart?.(event.session_id);
      if (event.type === "plan") handlers.onPlan?.(event.steps);
      if (event.type === "step") handlers.onStep?.(event.step_id, event.status);
      if (event.type === "tool_start") handlers.onToolStart?.({ ...event, status: "active" });
      if (event.type === "tool_end") handlers.onToolEnd?.(event);
      if (event.type === "status") handlers.onStatus?.(event.content);
      if (event.type === "delta") handlers.onDelta?.(event.content);
      if (event.type === "context") handlers.onContext?.(event.usage);
      if (event.type === "retry") handlers.onRetry?.(event);
      if (event.type === "clarification") handlers.onClarification?.(event);
      if (event.type === "project") handlers.onProject?.(event.project);
      if (event.type === "error") throw new Error(event.detail);
      if (event.type === "done") state.completed = { session_id: event.session_id, answer: event.answer, project: event.project };
    };

    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      lines.forEach(handleLine);
      if (done) break;
    }
    if (buffer.trim()) handleLine(buffer);
    if (!state.completed) throw new Error("聊天流意外结束");
    return state.completed;
  },
  clearChat: (sessionId: string) => request<void>(`/chat/${sessionId}`, { method: "DELETE" }),
};
