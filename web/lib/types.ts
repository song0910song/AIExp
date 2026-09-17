export type ReasoningEffort = "none" | "low" | "medium" | "high";

export type ReasoningEffortOption = {
  value: ReasoningEffort;
  label: string;
  description: string;
};

export type Health = {
  status: string;
  llm_configured: boolean;
  llm_model: string;
  rag_backend: string;
  llm_context_window_tokens: number;
  llm_reasoning_efforts?: ReasoningEffort[];
  llm_reasoning_effort_options?: ReasoningEffortOption[];
  llm_reasoning_effort_default?: ReasoningEffort;
  llm_reasoning_effort_source?: "configured" | "fallback";
  project_count: number;
};

export type ContextUsage = {
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
  cached_input_tokens?: number;
  cache_creation_input_tokens?: number;
  cache_hit_ratio?: number;
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
  workplane_height_m: number | null;
  target_illuminance_lx: number | null;
  target_cct_k: number | null;
  min_cri: number | null;
  target_ugr: number | null;
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
  template_origin: BriefTemplateOrigin | null;
};

export type BriefTemplateOrigin = {
  template_id: string;
  template_name: string;
  standard_reference: string;
  applied_at: string;
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
  estimated_illuminance_lx: number;
  installed_power_w: number;
  assumptions: string[];
  limitations: string[];
  calculated_at: string;
};

export type SimulationArtifact = {
  artifact_id: string;
  file_name: string;
  media_type: string;
  storage_path: string;
  sha256: string;
  size_bytes: number;
  uploaded_at: string;
};

export type DialuxVisionAnalysis = {
  is_dialux_result: boolean;
  maintained_illuminance_lx: number | null;
  confidence: number;
  metric_label: string | null;
  calculation_surface: string | null;
  explanation: string;
  model: string;
};

export type SimulationRun = {
  run_id: string;
  status: "pending" | "running" | "succeeded" | "failed" | "stale" | "unverified" | "cancelled";
  input_project_revision: number;
  solver_version: string | null;
  handoff_id: string | null;
  source_file: string | null;
  source_kind: "dialux_pdf" | "dialux_image" | "dialux_csv" | "dialux_json" | "manual_form" | null;
  artifacts: SimulationArtifact[];
  metrics: {
    maintained_illuminance_lx: number | null;
    minimum_illuminance_lx: number | null;
  } | null;
  metric_source: "manual" | "pdf_text" | "vision" | null;
  vision_analysis: DialuxVisionAnalysis | null;
  verification_status: "matched" | "mismatch" | "incomplete" | "unverified" | "stale";
  verification_messages: string[];
  parser_version: string | null;
  stale_reason: string | null;
  created_at: string;
  completed_at: string | null;
};

export type IlluminanceMethodCheck = {
  method: "lumen_method" | "dialux";
  status: "pass" | "fail" | "missing" | "unverified" | "stale";
  observed_illuminance_lx: number | null;
  target_illuminance_lx: number | null;
  source_id: string | null;
  explanation: string;
};

export type IlluminanceVerification = {
  overall_status: "pass" | "fail" | "pending";
  target_illuminance_lx: number | null;
  lumen_method: IlluminanceMethodCheck;
  dialux: IlluminanceMethodCheck;
  difference_percent: number | null;
  iteration: number;
  action: "target_reached" | "revise_design" | "confirm_target" | "run_lumen_method" | "await_dialux_result" | "rerun_dialux";
  message: string;
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

export type PhotometryParseSummary = {
  luminaire_id: string;
  source_file: string;
  summary: {
    format: "ies" | "ldt";
    photometric_system: string;
    lamp_count: number;
    power_w: number | null;
    declared_flux_lm: number | null;
    absolute_flux_declared: boolean;
    integrated_flux_lm: number;
    max_candela_cd: number;
    c_plane_count: number;
    gamma_angle_count: number;
    full_circle_coverage: boolean;
    symmetric_about_vertical_axis: boolean;
    warnings: string[];
  };
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
  simulation_runs: SimulationRun[];
  workflow_status: "draft" | "brief_confirmed" | "preliminary_calculated" | "luminaires_selected" | "simulation_pending" | "simulation_verified" | "needs_revision" | "accepted" | "delivered";
  open_questions: string[];
  created_at: string;
  updated_at: string;
};

export type Section = "overview" | "chat" | "knowledge";

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
