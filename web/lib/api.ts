import type { ChatMessage, DocumentContent, DocumentRecord, Evidence, Health, Luminaire, Project } from "./types";

const ROOT = "/backend";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${ROOT}${path}`, {
    ...init,
    headers: init?.body instanceof FormData ? init.headers : { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    let detail = `请求失败（${response.status}）`;
    try {
      const error = await response.json();
      detail = typeof error.detail === "string" ? error.detail : error.detail?.message ?? detail;
    } catch { /* The server did not return JSON. */ }
    throw new Error(detail);
  }
  return response.status === 204 ? undefined as T : response.json() as Promise<T>;
}

export const api = {
  health: () => request<Health>("/health"),
  projects: () => request<Project[]>("/projects"),
  project: (id: string) => request<Project>(`/projects/${id}`),
  deleteProject: (id: string) => request<void>(`/projects/${id}`, { method: "DELETE" }),
  chooseDirectory: () =>
    request<{ selected: boolean; selection_id?: string; directory?: string }>("/workspaces/select-directory", { method: "POST" }),
  createProject: (name: string, spaceType: string, selectionId: string) =>
    request<Project>("/projects", {
      method: "POST",
      body: JSON.stringify({ project_name: name, space_type: spaceType || null, workspace_selection_id: selectionId }),
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
  document: (sourceHash: string, projectId?: string) =>
    request<DocumentContent>(
      projectId ? `/projects/${projectId}/documents/${encodeURIComponent(sourceHash)}` : `/documents/${encodeURIComponent(sourceHash)}`,
    ),
  uploadDocument: (file: File, projectId?: string) => {
    const data = new FormData();
    data.append("file", file);
    data.append("source_type", projectId ? "project_document" : "standard");
    return request<{ indexed_chunks: number }>(
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
  chatHistory: (projectId: string, sessionId: string) =>
    request<{ session_id: string; messages: ChatMessage[] }>(
      `/chat/${sessionId}?project_id=${encodeURIComponent(projectId)}`,
    ),
  clearChat: (projectId: string, sessionId: string) =>
    request<void>(`/chat/${sessionId}?project_id=${encodeURIComponent(projectId)}`, { method: "DELETE" }),
};
