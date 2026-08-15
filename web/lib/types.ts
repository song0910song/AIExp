export type Health = {
  status: string;
  llm_configured: boolean;
  llm_model: string;
  rag_backend: string;
  llm_context_window_tokens: number;
  project_count: number;
};

export type ContextUsage = {
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
  context_window_tokens: number;
  source: "reported" | "unavailable";
};

export type DesignBrief = {
  project_name: string;
  space_type: string | null;
  area_m2: number | null;
  length_m: number | null;
  width_m: number | null;
  room_height_m: number | null;
  mounting_height_m: number | null;
  workplane_height_m: number | null;
  target_illuminance_lx: number | null;
  target_cct_k: number | null;
  min_cri: number | null;
  target_ugr: number | null;
  target_uniformity_u0: number | null;
  max_lpd_w_m2: number | null;
  max_power_w: number | null;
  mounting: string | null;
  min_ip_rating: string | null;
  preferred_brands: string[];
  notes: string | null;
  confirmed_fields: string[];
  lighting_parameter_sources: Record<string, {
    source: "rag";
    evidence_ids: string[];
    applied_at: string;
  }>;
};

export type Evidence = {
  evidence_id: string;
  source_name: string;
  source_type: "standard" | "project_document" | "user_note";
  excerpt: string;
  locator: string | null;
  score: number | null;
};

export type Calculation = {
  method: "lumen_method";
  inputs: {
    area_m2: number;
    target_illuminance_lx: number;
    luminaire_luminous_flux_lm: number;
    luminaire_power_w: number;
    utilization_factor: number;
    maintenance_factor: number;
  };
  required_luminous_flux_lm: number;
  luminaire_count: number;
  installed_power_w: number;
  installed_power_density_w_m2: number;
  assumptions: string[];
  limitations: string[];
  calculated_at: string;
};

export type RuleCheck = {
  metric: string;
  status: "pass" | "fail" | "not_applicable" | "insufficient_data";
  observed: number | null;
  threshold: number | null;
  explanation: string;
  evidence_id: string | null;
};

export type Luminaire = {
  luminaire_id: string;
  article_name: string;
  brand_name: string | null;
  summary: string | null;
  technical_summary: string | null;
  power_w: number | null;
  luminous_flux_lm: number | null;
  ip_rating: string | null;
  cct_k: number | null;
  cri: number | null;
  ugr: number | null;
  detail_url: string;
  image_url: string | null;
  photometry_image_url: string | null;
  has_uld: boolean;
  has_photometry_download: boolean;
  matching_status: "matches" | "incomplete" | "rejected";
  missing_requested_fields: string[];
  failed_requested_fields: string[];
  criteria_checks: LuminaireCriterionCheck[];
  brief_validation: LuminaireBriefValidation | null;
};

export type LuminaireCriterionCheck = {
  field: "brand" | "max_power_w" | "target_cct_k" | "min_cri" | "max_ugr" | "min_ip_rating" | "mounting" | "detail";
  status: "pass" | "fail" | "unknown";
  expected: string;
  observed: string | null;
  priority: "required" | "preference";
};

export type LuminaireBriefValidation = {
  project_revision: number;
  matching_status: "matches" | "incomplete" | "rejected";
  missing_requested_fields: string[];
  failed_requested_fields: string[];
  criteria_checks: LuminaireCriterionCheck[];
};

export type PhotometryExtractedFile = {
  relative_path: string;
  sha256: string;
  size_bytes: number;
  file_type: "ies" | "ldt" | "uld";
};

export type PhotometryAsset = {
  luminaire_id: string;
  article_name: string;
  status: "pending" | "downloaded" | "failed" | "not_available";
  source_url: string | null;
  downloaded_at: string | null;
  sha256: string | null;
  zip_file: string | null;
  zip_size_bytes: number | null;
  extracted_files: PhotometryExtractedFile[];
  error: string | null;
};

export type CadPoint = { x: number; y: number };

export type FloorPlanAreaCandidate = {
  entity_type: "LWPOLYLINE" | "POLYLINE";
  layer: string;
  raw_area: number;
  area_m2: number | null;
  length_m: number | null;
  width_m: number | null;
  points: CadPoint[];
};

export type FloorPlan = {
  asset: {
    source_name: string;
    source_type: "dxf" | "dwg";
    storage_path: string;
    sha256: string;
    size_bytes: number;
    converted_from_dwg: boolean;
    imported_at: string;
  };
  drawing_units: string;
  meters_per_drawing_unit: number | null;
  bounds: [CadPoint, CadPoint] | null;
  entity_counts: Record<string, number>;
  text_items: string[];
  room_name: string | null;
  area_candidates: FloorPlanAreaCandidate[];
  selected_area_candidate_index: number | null;
  warnings: string[];
};

export type FloorPlanImport = {
  floor_plan: FloorPlan;
  project: Project;
  applied_area_candidate_index: number | null;
};

export type Project = {
  project_id: string;
  revision: number;
  brief: DesignBrief;
  evidence: Evidence[];
  calculations: Calculation[];
  rule_checks: RuleCheck[];
  luminaires: Luminaire[];
  selected_luminaire_ids: string[];
  floor_plan: FloorPlan | null;
  open_questions: string[];
  created_at: string;
  updated_at: string;
};

export type Section = "overview" | "chat";

export type AgentStepStatus = "pending" | "active" | "done" | "failed" | "skipped";

export type AgentPlanStep = {
  id: string;
  title: string;
  description: string;
  tools: string[];
  status: AgentStepStatus;
};

export type AgentToolRun = {
  call_id: string;
  name: string;
  status: "active" | "done" | "failed";
  input?: Record<string, unknown> | unknown[];
  output?: Record<string, unknown>;
  started_at?: string;
  duration_ms?: number;
};

export type ClarificationOption = {
  label: string;
  value: string;
};

export type ClarificationField = {
  field_id: string;
  label: string;
  description: string | null;
  input_type: "text" | "number" | "select" | "multiselect";
  required: boolean;
  placeholder: string | null;
  options: ClarificationOption[];
};

export type ClarificationRequest = {
  title: string;
  question: string;
  fields: ClarificationField[];
};
