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
  candidate_id: string;
  boundary: Point[];
};

export type Point = { x: number; y: number };
export type Provenance = { source: string; locator: string; confidence: number; confirmed: boolean; note: string };
export type SpatialRoom = {
  room_id: string; candidate_id: string | null; floor: string | null; number: string | null;
  name: string | null; usage: string | null; boundary: Point[]; holes: Point[][]; area_m2: number | null;
  elevation_m: number | null; height_m: number | null; ceiling_height_m: number | null;
  wall_reflectance: number | null; ceiling_reflectance: number | null; floor_reflectance: number | null;
  status: "pending" | "confirmed" | "excluded"; exclusion_reason: string; provenance: Record<string, Provenance>;
};
export type SpatialElement = {
  element_id: string; kind: "door" | "window" | "column" | "furniture" | "obstruction";
  name: string; room_id: string | null; footprint: Point[]; elevation_m: number | null;
  height_m: number | null; rotation_deg: number | null; material: string | null; reflectance: number | null;
  status: "pending" | "confirmed" | "excluded"; provenance: Record<string, Provenance>;
};
export type SpatialModel = {
  version: number; source_sha256: string; meters_per_unit: number | null; rooms: SpatialRoom[];
  elements: SpatialElement[]; coverage_confirmed: boolean; elements_reviewed: boolean;
  design_ready: boolean; outstanding: string[]; audit_log: string[]; geometry_tolerance_m: number | null;
};
export type EvidencePage = {
  page_number: number | null; locator: string; text: string; status: string; warnings: string[];
  blocks: Array<{ block_id: string; text: string; bbox: number[] | null; kind: string }>;
  ocr_layout?: Array<{ block_id: string; text: string; bbox: number[] | null; source_bbox: number[] | null; coordinate_space: string }>;
  tables: Array<{ table_id: string; cells: Array<Array<string | null>>; bbox: number[] | null }>;
};
export type StandardRecord = {
  standard_id: string; source_hash: string; file_sha256: string; project_id: string | null;
  number: string; edition: string; title: string; effective_date: string; scope: string;
  kind: "official" | "corporate" | "owner"; source: string; source_verified: boolean; registered_at: string;
};
export type CalculationConditions = {
  plane: string | null; workplane_height_m: number | null; grid_x_m: number | null; grid_y_m: number | null;
  maintenance_factor: number | null; glare_method: string | null; glare_observers: string | null; additional: string;
};
export type DesignRule = {
  rule_id: string; standard_id: string; locator: string; page_number: number | null; evidence_text: string; evidence_key: string;
  metric: "illuminance" | "uniformity" | "ugr" | "cri" | "cct" | "lpd";
  operator: ">=" | "<=" | "=" | "range" | null; threshold: number | null; upper_threshold: number | null; unit: string | null;
  applies_to: string[]; evaluation_scope: string | null; conditions: CalculationConditions; status: "candidate" | "confirmed" | "rejected";
  reviewer: string | null; review_note: string; reviewed_at: string | null;
};
export type RuleSet = {
  version: number; standards: StandardRecord[]; rules: DesignRule[]; bound_version: number | null;
  bound_model_version: number | null; status: "draft" | "bound" | "stale"; invalidation_reason: string | null;
};
export type DesignSettings = {
  status: string; rule_version: number | null; model_version: number | null; issues: string[];
  rooms: Array<{ room_id: string; usage: string; conditions: CalculationConditions; requirements: DesignRule[] }>;
  simulation_performed: false; compliance_status: "not_evaluated";
};

export type FloorPlan = {
  asset: { source_name: string; source_type: "dxf" | "dwg"; converted_from_dwg: boolean; sha256: string };
  drawing_units: string;
  meters_per_drawing_unit: number | null;
  entity_counts: Record<string, number>;
  text_items: string[];
  room_name: string | null;
  area_candidates: AreaCandidate[];
  selected_area_candidate_index: number | null;
  warnings: string[];
  read_complete: boolean; spatial_model: SpatialModel | null; repairs: string[]; conversion_log: string[];
  layers: string[]; external_references: string[]; unsupported_entities: Record<string, number>;
  drawing_paths: Array<{ layer: string; source_handle: string; points: Point[]; closed: boolean }>;
  drawing_labels: Array<{ text: string; position: Point; layer: string }>;
  issues: Array<{ code: string; message: string; severity: string; source_handle: string | null; position: Point | null }>;
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
  dialux_protocol_url?: string | null;
  has_uld?: boolean;
  has_photometry_download?: boolean;
  detail_fields: Record<string, string>;
  matching_status: "matches" | "incomplete" | "rejected";
};

export type Project = {
  project_id: string;
  revision: number;
  brief: { project_name: string; space_type: string | null };
  floor_plan: FloorPlan | null;
  rule_set: RuleSet | null;
  invalidated_dependencies: string[];
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

export type DocumentContent = DocumentRecord & { content: string; pages: EvidencePage[]; source_sha256?: string; extraction_complete: boolean; review_required: boolean };
export type ContextUsage = {
  input_tokens: number;
  window_tokens: number;
  percentage: number;
  estimated: boolean;
};
export type ToolCall = {
  id: string;
  name: string;
  input: string;
  status: "running" | "completed" | "error";
  summary: string;
};
export type ChatMessage = {
  role: "user" | "assistant";
  content: string;
  tool_calls?: ToolCall[];
  context_usage?: ContextUsage;
};
export type ChatStreamEvent =
  | { type: "session"; session_id: string }
  | { type: "delta"; text: string }
  | ({ type: "tool" } & ToolCall)
  | { type: "done"; session_id: string; project: Project | null; context_usage: ContextUsage }
  | { type: "error"; message: string };
