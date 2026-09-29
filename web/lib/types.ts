export type Health = {
  status: string;
  llm_configured: boolean;
  llm_model: string | null;
  rag_backend: string;
  project_count: number;
  llm_reasoning_efforts: string[];
  llm_reasoning_effort_default: string;
  llm_reasoning_effort_options: Array<{ value: string; label: string; description: string }>;
  llm_prompt_cache_enabled: boolean;
};

export type AreaCandidate = {
  entity_type: string;
  layer: string;
  area_m2: number | null;
  length_m: number | null;
  width_m: number | null;
  points: Array<{ x: number; y: number }>;
};

export type FloorPlan = {
  asset: { source_name: string; source_type: "dxf" | "dwg"; converted_from_dwg: boolean };
  drawing_units: string;
  meters_per_drawing_unit: number | null;
  entity_counts: Record<string, number>;
  text_items: string[];
  room_name: string | null;
  area_candidates: AreaCandidate[];
  selected_area_candidate_index: number | null;
  warnings: string[];
};

export type Luminaire = {
  luminaire_id: string;
  article_name: string;
  brand_name: string | null;
  summary: string | null;
  technical_summary: string | null;
  power_w: number | null;
  luminous_flux_lm: number | null;
  cct_k: number | null;
  cri: number | null;
  ip_rating: string | null;
  ugr: number | null;
  image_url: string | null;
  detail_url: string;
  detail_fields: Record<string, string>;
  matching_status: "matches" | "incomplete" | "rejected";
};

export type Project = {
  project_id: string;
  revision: number;
  brief: { project_name: string; space_type: string | null };
  floor_plan: FloorPlan | null;
  luminaires: Luminaire[];
  created_at: string;
  updated_at: string;
};

export type Evidence = {
  evidence_id: string;
  source_name: string;
  source_type: "standard" | "project_document" | "user_note";
  excerpt: string;
  locator: string | null;
  score: number | null;
};

export type DocumentRecord = {
  source_hash: string;
  source_name: string;
  source_type: Evidence["source_type"];
  page_count: number | null;
  indexed_at: string;
  indexed_chunks: number;
  project_id: string | null;
};

export type DocumentContent = DocumentRecord & { content: string };
export type ChatMessage = { role: "user" | "assistant"; content: string };
