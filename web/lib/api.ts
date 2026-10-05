import type { ChatMessage, ChatStreamEvent, DocumentContent, DocumentRecord, Evidence, Health, Luminaire, Project, SpatialModel } from "./types";

const ROOT = "/backend";

async function assertOk(response: Response): Promise<void> {
  if (response.ok) return;
  let detail = `请求失败（${response.status}）`;
  try {
    const error = await response.json();
    detail = typeof error.detail === "string" ? error.detail : Array.isArray(error.detail)
      ? error.detail.map((item: { loc?: string[]; msg?: string }) => `${item.loc?.slice(1).join(".") ?? "字段"}: ${item.msg ?? "无效"}`).join("；")
      : error.detail?.message ?? detail;
  } catch { /* The server did not return JSON. */ }
  throw new Error(detail);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${ROOT}${path}`, {
    ...init,
    headers: init?.body instanceof FormData ? init.headers : { "Content-Type": "application/json", ...init?.headers },
  });
  await assertOk(response);
  return response.status === 204 ? undefined as T : response.json() as Promise<T>;
}

async function streamChat(
  projectId: string, message: string, reasoningEffort: string, sessionId: string | undefined,
  onEvent: (event: ChatStreamEvent) => void, signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(`${ROOT}/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ project_id: projectId, message, session_id: sessionId, reasoning_effort: reasoningEffort }),
    signal,
  });
  await assertOk(response);
  if (!response.body) throw new Error("浏览器未提供流式响应");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let completed = false;
  function dispatch(block: string) {
    const eventName = block.match(/^event: (.+)$/m)?.[1];
    const data = block.match(/^data: (.+)$/m)?.[1];
    if (!eventName || !data) return;
    const event = { type: eventName, ...JSON.parse(data) } as ChatStreamEvent;
    if (event.type === "error") throw new Error(event.message);
    if (event.type === "done") completed = true;
    onEvent(event);
  }
  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      buffer = buffer.replace(/\r\n/g, "\n");
      let boundary = buffer.indexOf("\n\n");
      while (boundary !== -1) {
        dispatch(buffer.slice(0, boundary));
        buffer = buffer.slice(boundary + 2);
        boundary = buffer.indexOf("\n\n");
      }
      if (done) break;
    }
    if (!completed) throw new Error("响应意外中断，请重试");
  } finally {
    reader.releaseLock();
  }
}

export const api = {
  reviewModel: (project: Project, model: SpatialModel, note: string) => request<Project>(`/projects/${project.project_id}/spatial-model`, {
    method: "PUT", body: JSON.stringify({ expected_revision: project.revision, meters_per_unit: model.meters_per_unit, rooms: model.rooms,
      elements: model.elements, coverage_confirmed: model.coverage_confirmed, elements_reviewed: model.elements_reviewed, note }),
  }),
  health: () => request<Health>("/health"),
  projects: () => request<Project[]>("/projects"),
  project: (id: string) => request<Project>(`/projects/${id}`),
  deleteProject: (id: string) => request<void>(`/projects/${id}`, { method: "DELETE" }),
  createProject: (name: string, spaceType: string) =>
    request<Project>("/projects", {
      method: "POST",
      body: JSON.stringify({ project_name: name, space_type: spaceType || null }),
    }),
  updateProject: (project: Project, name: string, spaceType: string) =>
    request<Project>(`/projects/${project.project_id}/brief`, {
      method: "PUT",
      body: JSON.stringify({ expected_revision: project.revision, project_name: name, space_type: spaceType || null }),
    }),
  importCad: (project: Project, file: File) => {
    const data = new FormData();
    data.append("file", file);
    data.append("expected_revision", String(project.revision));
    return request<{ project: Project }>(`/projects/${project.project_id}/floor-plan`, { method: "POST", body: data });
  },
  selectRoom: (project: Project, candidateIndex: number) =>
    request<Project>(
      `/projects/${project.project_id}/floor-plan/selection?expected_revision=${project.revision}&candidate_index=${candidateIndex}`,
      { method: "PUT" },
    ),
  documents: (projectId?: string) =>
    request<DocumentRecord[]>(projectId ? `/projects/${projectId}/documents` : "/documents"),
  deleteDocument: (sourceHash: string) =>
    request<void>(`/documents/${encodeURIComponent(sourceHash)}`, { method: "DELETE" }),
  deleteDocuments: (sourceHashes: string[]) =>
    request<void>("/documents/delete", { method: "POST", body: JSON.stringify({ source_hashes: sourceHashes }) }),
  document: (sourceHash: string, projectId?: string) =>
    request<DocumentContent>(
      projectId ? `/projects/${projectId}/documents/${encodeURIComponent(sourceHash)}` : `/documents/${encodeURIComponent(sourceHash)}`,
    ),
  uploadDocument: (file: File, projectId?: string) => {
    const data = new FormData();
    data.append("file", file);
    return request<{ indexed_chunks: number; sha256: string; source_name: string }>(
      projectId ? `/projects/${projectId}/documents` : "/documents",
      { method: "POST", body: data },
    );
  },
  searchEvidence: (query: string, projectId?: string) =>
    request<{ evidence: Evidence[] }>("/evidence/search", {
      method: "POST", body: JSON.stringify({ query, project_id: projectId ?? null, top_k: 6 }),
    }),
  searchLuminaires: (project: Project, filters: Record<string, string | number>) =>
    request<{ project: Project; candidates: Luminaire[]; warnings: string[] }>(
      `/projects/${project.project_id}/luminaires`,
      { method: "POST", body: JSON.stringify({ ...filters, expected_revision: project.revision, max_results: 5 }) },
    ),
  sendToDialux: (projectId: string, luminaireId: string) =>
    request<{ status: "launched" }>(
      `/projects/${projectId}/luminaires/${encodeURIComponent(luminaireId)}/send-to-dialux`,
      { method: "POST" },
    ),
  chat: (projectId: string, message: string, reasoningEffort: string, sessionId?: string) =>
    request<{ session_id: string; answer: string; project: Project | null }>("/chat", {
      method: "POST",
      body: JSON.stringify({ project_id: projectId, message, session_id: sessionId, reasoning_effort: reasoningEffort }),
    }),
  streamChat,
  chatHistory: (projectId: string, sessionId: string) =>
    request<{ session_id: string; messages: ChatMessage[] }>(
      `/chat/${sessionId}?project_id=${encodeURIComponent(projectId)}`,
    ),
  clearChat: (projectId: string, sessionId: string) =>
    request<void>(`/chat/${sessionId}?project_id=${encodeURIComponent(projectId)}`, { method: "DELETE" }),
};
