# Graph Report - AIExp  (2026-09-18)

## Corpus Check
- 101 files · ~105,648 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 413 file(s) not represented in the graph (top: (none) 407, .dxf 2, .dwg 1)

## Summary
- 1448 nodes · 3673 edges · 85 communities (74 shown, 11 thin omitted)
- Extraction: 88% EXTRACTED · 12% INFERRED · 0% AMBIGUOUS · INFERRED: 431 edges (avg confidence: 0.94)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- SQLite Database Recovery
- Model Runtime Configuration
- DXF Geometry Dependencies
- Project Document API
- Frontend Runtime Dependencies
- DIALux Workflow Tests
- DIALux Protocol Handling
- Luminaire Constraint Matching
- FastAPI Domain Models
- Document Loading Pipeline
- Chat Composition UI
- Chat Session Persistence
- Preliminary Lighting Calculations
- DIALux API Client
- Photometry Asset Storage
- Project Revision Persistence
- Workbench Panel Components
- Frontend API Contracts
- Agent Runtime Orchestration
- Photometry Preview Pipeline
- Agent Tool Schemas
- Project Store Operations
- Brief Editing UI
- Chat API Lifecycle
- Application Launcher Utilities
- TypeScript Compiler Configuration
- Knowledge Base UI
- Deterministic Deliverables
- Schema Migration Cleanup
- Design Brief Validation
- Core Agent Tools
- DIALux Result Analysis
- Retry Error Handling
- Persistence Integration Tests
- Evidence Provenance Validation
- Artifact Serialization Utilities
- Joint Illuminance Verification
- Photometry Preview Tests
- Photometry Archive Tests
- Main Workbench UI
- Photometry Parsing Core
- Photometry Format Parsers
- Chroma Evidence Store
- Domain Schema Models
- Application Configuration
- Photometry Distribution Math
- Local Evidence Store
- DIALux HTML Parsing
- HTTP Retry Transport
- Calculation Input Preparation
- Document Chunk Indexing
- DIALux Handoff Tools
- Workspace Evidence Store
- HTTP Response Test Doubles
- Luminaire Search Ranking
- Luminaire Search Tools
- Floor Plan Layout Analysis
- DIALux Luminaire Finder
- Meeting Room Standards
- DIALux Validation Workflow
- Evidence Repair Utilities
- RAG Parameter Tests
- Workbench Architecture
- Product Field Normalization
- Document Catalog Operations
- Audit Chunk Persistence
- Provider Value Normalization
- Lighting Design Dataflow
- Standards Regression Baseline
- Candela Interpolation
- Calculation Tool Tests
- Command Line Interface
- Evidence Index Synchronization
- Workspace Directory Selection
- Evidence Panel UI
- Clarification Card UI
- Development Server Script
- Auditable Revision Workflow
- Retrying HTTP Sessions
- Reasoning Effort Tests
- Calculation Panel UI
- Next.js Generated Types
- Workflow State Derivation
- Shared Test Package
- Project Package Root

## God Nodes (most connected - your core abstractions)
1. `create_app()` - 119 edges
2. `ProjectStore` - 100 edges
3. `DesignBrief` - 73 edges
4. `ProjectState` - 72 edges
5. `StrictModel` - 60 edges
6. `Settings` - 53 edges
7. `LuminaireCandidate` - 51 edges
8. `DialuxAPI` - 47 edges
9. `LocalEvidenceStore` - 47 edges
10. `PhotometryAssetStore` - 42 edges

## Surprising Connections (you probably didn't know these)
- `GB 50034-2024 标准测试快照` --semantically_similar_to--> `《建筑照明设计标准》2024`  [INFERRED] [semantically similar]
  tests/fixtures/phase0/standard-gb50034-2024.md → src/data/user_docs/《建筑照明设计标准》2024.md
- `test_settings_normalize_reasoning_efforts()` --uses--> `Settings`  [INFERRED]
  tests/test_reasoning_effort.py → src/lighting_agent/config.py
- `test_removed_brief_metrics_are_dropped_from_legacy_payloads()` --uses--> `DesignBrief`  [INFERRED]
  tests/test_calculations.py → src/lighting_agent/schemas.py
- `test_calculation_input_ignores_removed_group_fields()` --uses--> `CalculationInput`  [INFERRED]
  tests/test_calculation_tool_inputs.py → src/lighting_agent/schemas.py
- `test_project_state_ignores_removed_layout_fields_in_legacy_payload()` --uses--> `ProjectState`  [INFERRED]
  tests/test_sqlite_migration.py → src/lighting_agent/schemas.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **可审计项目工作流** — readme_auditable_workflow, readme_projectstate_revision, docs__agent_projectstate, docs__agent_evidence_rag, docs__agent_handoff_identity [EXTRACTED 1.00]
- **照度计算与 DIALux 验证闭环** — readme_joint_illuminance_gate, docs__agent_lumen_method, docs__agent_simulationrun, docs__agent_human_dialux_loop, docs__simulation_revision_loop [EXTRACTED 1.00]
- **会议室照明证据链** — src_data_user_docs_2024_office_meeting_requirements, src_data_user_docs_2024_video_meeting_requirements, src_data_user_docs__video_meeting_guidance, docs__meeting_room_512_case [INFERRED 0.85]

## Communities (85 total, 11 thin omitted)

### Community 0 - "SQLite Database Recovery"
Cohesion: 0.06
Nodes (31): Connection, Path, Logically rebuild readable application records and preserve the damaged file., Open short-lived SQLite connections with WAL and transactional writes., SQLiteDatabase, select_workspace_directory(), Path, ValueError (+23 more)

### Community 1 - "Model Runtime Configuration"
Cohesion: 0.06
Nodes (42): langchain_core_messages, _model_supports_prompt_cache(), Return normalized reasoning efforts exposed by this deployment., Return a valid default even when an environment override is stale., Create request-scoped settings without mutating frozen global config., Return provider cache options, normalizing unsupported TTL values., Return whether the configured model is in the GPT-5.6+ family., Runtime settings, read once so secrets are never printed or persisted. (+34 more)

### Community 2 - "DXF Geometry Dependencies"
Cohesion: 0.07
Nodes (54): collections, Drawing, ezdxf, ezdxf_addons, ezdxf_document, ezdxf_filemanagement, ezdxf_lldxf_const, ezdxf_math (+46 more)

### Community 3 - "Project Document API"
Cohesion: 0.06
Nodes (36): Return an effective request and missing deterministic prerequisites. Any…, validate_luminaire_search(), format_evidence(), ProjectUpdate, create_app(), add_document(), add_project_document(), adopt_evidence() (+28 more)

### Community 4 - "Frontend Runtime Dependencies"
Cohesion: 0.05
Nodes (33): next, @next/swc-wasm-nodejs, react-dom, @types/node, @types/react, @types/react-dom, typescript, web_app_globals (+25 more)

### Community 5 - "DIALux Workflow Tests"
Cohesion: 0.13
Nodes (34): DialuxVisionAnalysis, Structured, auditable reading returned by the image-capable model., _add_selected_luminaire(), FakeDialux, _generate_handoff(), make_client(), _matching_run(), Any (+26 more)

### Community 6 - "DIALux Protocol Handling"
Cohesion: 0.12
Nodes (30): MonkeyPatch, DialuxProtocolError, find_protocol_handler(), open_in_dialux(), RuntimeError, A structured local-handoff failure safe to expose through the API., Return the URL only when it is a strict ``dial://…uld`` handoff link., Return the registered dial:// handler command, or None when absent. (+22 more)

### Community 7 - "Luminaire Constraint Matching"
Cohesion: 0.11
Nodes (31): apply_brief_constraints(), match_luminaire_candidate(), add_check(), Fill available product-selection conditions from the confirmed brief. These…, Classify requested attributes, keeping missing supplier data distinct from…, _brief_constraints(), LuminaireCandidate, LuminaireCriterionCheck (+23 more)

### Community 8 - "FastAPI Domain Models"
Cohesion: 0.08
Nodes (32): BaseModel, FastAPI, fastapi_middleware_cors, fastapi_responses, queue, DialuxHandoff, A preserved DIALux result file used as simulation evidence., Immutable identity and input snapshot for one DIALux handoff. (+24 more)

### Community 9 - "Document Loading Pipeline"
Cohesion: 0.10
Nodes (26): concurrent_futures, requests, _checked_path(), DocumentLoadError, _extract_text(), load_document(), _nested_value(), PaddleOCRClient (+18 more)

### Community 10 - "Chat Composition UI"
Cohesion: 0.10
Nodes (26): ChatComposer(), clipboardImage(), isImage(), ContextWindowUsage(), formatTokens(), AgentRunTrace(), failUnfinishedTools(), isDialuxImage() (+18 more)

### Community 11 - "Chat Session Persistence"
Cohesion: 0.10
Nodes (27): ChatSessionStore, _clarification_from_tool_chunk(), _context_usage_from_chunk(), _debug_tool_result(), _debug_value(), _first_token_count(), Any, SQLite-backed LangChain message history with expiry and a bounded size. (+19 more)

### Community 12 - "Preliminary Lighting Calculations"
Cohesion: 0.10
Nodes (29): Namespace, pytest, calculate_lumen_method(), check_design_rules(), Reproducible preliminary calculations; no LLM decisions are made here., Estimate quantity via N = E × A / (Φ × UF × MF)., Compare provided evidence-derived rules with explicit observed values., _brief_from_args() (+21 more)

### Community 13 - "DIALux API Client"
Cohesion: 0.15
Nodes (10): DialuxAPI, DialuxAPIError, Any, RuntimeError, Bounded, cached DIALux catalogue client. The external directory is unversioned…, A structured external-service failure safe to expose through the API., Download a same-origin photometric ZIP with a hard streamed size limit., Extract the product page's Send-to-DIALux ``dial://`` link. (+2 more)

### Community 14 - "Photometry Asset Storage"
Cohesion: 0.16
Nodes (10): Protocol, PhotometryAssetStore, PhotometryDownloader, Path, Download every advertised photometry file required by a task package. A task…, Store source ZIPs, extracted photometry files and their manifest per project., Copy one trusted member with actual-byte limits and no in-memory expansion., PhotometryAsset (+2 more)

### Community 15 - "Project Revision Persistence"
Cohesion: 0.17
Nodes (14): FileNotFoundError, ProjectNotFoundError, RuntimeError, Invalidate prior simulation evidence without deleting its audit trail., Append one imported or planned simulation run atomically., Atomically add newly found luminaires without discarding project changes.…, Persist final luminaires, separately from the searchable candidate history., Attach current-brief checks without replacing the search snapshot. (+6 more)

### Community 16 - "Workbench Panel Components"
Cohesion: 0.19
Nodes (20): lucide-react, react, NumericBriefKey, numericFields, DeliverablesPanel(), Kind, DialuxVerificationPanel(), methodStatus (+12 more)

### Community 17 - "Frontend API Contracts"
Cohesion: 0.08
Nodes (26): ChatHistoryMessage, ChatPayload, ChatStreamEvent, ChatStreamHandlers, AgentPlanStep, AgentStepStatus, BriefTemplateOrigin, CadPoint (+18 more)

### Community 18 - "Agent Runtime Orchestration"
Cohesion: 0.13
Nodes (21): collections_abc, httpx, build_agent(), interactive_chat(), invoke_agent(), _prompt_cache_model_params(), Any, Construction and invocation helpers for the constrained ReAct agent. (+13 more)

### Community 19 - "Photometry Preview Pipeline"
Cohesion: 0.15
Nodes (19): PhotometryFormat, parse_photometry(), parse_photometry_file(), Path, Parse LM-63 ``.ies`` or EULUMDAT ``.ldt`` content into a candela table., compute_illuminance_preview(), IlluminancePreviewRequest, IlluminancePreviewResult (+11 more)

### Community 20 - "Agent Tool Schemas"
Cohesion: 0.15
Nodes (20): langchain_core_tools, AddDocumentInput, AskUserInput, BriefUpdateInput, ClarificationOption, CreateProjectInput, DialuxTaskInput, EvidenceAdoptionInput (+12 more)

### Community 21 - "Project Store Operations"
Cohesion: 0.12
Nodes (12): ProjectStore, Path, Delete one project, its revision snapshots, chat sessions and generated files., Persist project state and every accepted revision in SQLite. ``directory``…, Count persisted projects without deserializing their state., Return the legacy JSON path for compatibility; state is no longer written there., test_agent_append_tools_rebase_stale_revision_within_one_chat_turn(), test_chat_sessions_survive_store_recreation() (+4 more)

### Community 22 - "Brief Editing UI"
Cohesion: 0.12
Nodes (15): BriefPanel(), setNumericValue(), setValue(), CreateProjectModal(), submit(), emptyDraft, ProjectDraft, templateValueFields (+7 more)

### Community 23 - "Chat API Lifecycle"
Cohesion: 0.14
Nodes (19): _agent_plan(), _chat_history(), chat(), chat_agent(), clear_chat(), get_chat_history(), project_sessions(), stream_chat() (+11 more)

### Community 24 - "Application Launcher Utilities"
Cohesion: 0.16
Nodes (14): argparse, contextlib, dataclasses, logging, Convenience launcher for the lighting-design-agent package., pathlib, shutil, sqlite3 (+6 more)

### Community 25 - "TypeScript Compiler Configuration"
Cohesion: 0.11
Nodes (18): compilerOptions, allowJs, esModuleInterop, incremental, isolatedModules, jsx, lib, module (+10 more)

### Community 26 - "Knowledge Base UI"
Cohesion: 0.14
Nodes (13): react-markdown, remark-gfm, formatIndexedAt(), formatSourceType(), KnowledgeBasePanel(), closeViewer(), onKeyDown(), refreshDocuments() (+5 more)

### Community 27 - "Deterministic Deliverables"
Cohesion: 0.15
Nodes (18): build_design_report(), build_dialux_task_archive(), build_dialux_task_package(), _canonical_json(), handoff_id_for(), handoff_id_for_snapshot(), handoff_snapshot(), _markdown_value() (+10 more)

### Community 28 - "Schema Migration Cleanup"
Cohesion: 0.20
Nodes (7): _drop_removed_metric_fields(), Any, model_validator, Discard fields removed from the public model while reading old payloads., Drop the removed mounting filter so stored search runs stay loadable., Drop removed supplier fields so stored candidates stay loadable., Keep projects saved before layout phase removal readable.

### Community 29 - "Design Brief Validation"
Cohesion: 0.18
Nodes (12): DesignBrief, Confirmed input for an indoor lighting design task. The assistant may fill…, FakeDialux, test_luminaire_detail_requests_refresh_for_unsaved_historical_candidate(), test_search_persists_candidates_rejected_by_the_current_project_brief(), test_search_tool_blocks_vendor_call_until_conditions_are_confirmed(), test_search_tool_returns_summary_and_defers_supplier_detail(), test_search_snapshot_is_preserved_when_brief_changes() (+4 more)

### Community 30 - "Core Agent Tools"
Cohesion: 0.14
Nodes (18): add_document(), ask_user(), check_design_rules(), ClarificationField, create_project(), _data(), get_project(), Request missing user input as a structured, fillable form and pause the… (+10 more)

### Community 31 - "DIALux Result Analysis"
Cohesion: 0.15
Nodes (16): base64, analyze_dialux_result_image(), DialuxResultError, DialuxVisionError, extract_maintained_illuminance_from_pdf(), _message_text(), Any, Path (+8 more)

### Community 32 - "Retry Error Handling"
Cohesion: 0.12
Nodes (15): Exception, Bind the callback fired before each SDK-level model retry., set_retry_notifier(), _chat_error_detail(), _claims_structured_clarification(), run_agent_stream(), _fallback_clarification(), Extract displayable text from a LangChain streaming message chunk. (+7 more)

### Community 33 - "Persistence Integration Tests"
Cohesion: 0.17
Nodes (7): fastapi_testclient, SQLite project persistence with immutable revision snapshots., FakeDialux, Any, test_photometry_asset_endpoints_download_list_file_and_remove(), test_settings_normalize_reasoning_efforts(), typing

### Community 34 - "Evidence Provenance Validation"
Cohesion: 0.16
Nodes (11): field_validator, LightingParameterSource, Evidence provenance for a lighting parameter populated from RAG., adopt_evidence(), apply_rag_lighting_parameters(), build_update(), _get_scoped_evidence(), Apply an Agent mutation to the newest snapshot, retrying a racing write. Agent… (+3 more)

### Community 35 - "Artifact Serialization Utilities"
Cohesion: 0.19
Nodes (7): hashlib, io, json, Deterministic, reviewable handoff artifacts., Auditable indoor-lighting design assistant., Durable, project-scoped storage for DIALux photometry downloads., ZipFile

### Community 36 - "Joint Illuminance Verification"
Cohesion: 0.18
Nodes (15): _dialux_check(), evaluate_illuminance(), IlluminanceMethodCheck, IlluminanceVerification, _lumen_check(), Joint illuminance verification for lumen-method and DIALux results., Require both methods to reach the target before accepting the design., One auditable decision using illuminance as the only acceptance metric. (+7 more)

### Community 37 - "Photometry Preview Tests"
Cohesion: 0.19
Nodes (13): build_rotationally_symmetric_ies(), build_simple_ldt(), FakeDialux, FixedPayloadDownloader, make_client(), TestClient, A single C plane with a cosine vertical spread (per-1000-lm basis)., test_api_generates_and_invalidates_photometry_preview() (+5 more)

### Community 38 - "Photometry Archive Tests"
Cohesion: 0.27
Nodes (11): _compressed_bomb_zip(), DIALuxDownloadSession, Downloader, test_dialux_downloads_the_zip_linked_by_the_product_page(), test_failed_download_is_persisted_and_can_be_retried(), test_photometry_store_rejects_compression_bombs(), test_task_archive_downloads_missing_photometry_before_export(), test_task_archive_downloads_only_final_selected_luminaires() (+3 more)

### Community 39 - "Main Workbench UI"
Cohesion: 0.17
Nodes (6): ConversationPanel(), formatProjectTime(), LightingWorkbench(), navigation, Health, Section

### Community 40 - "Photometry Parsing Core"
Cohesion: 0.16
Nodes (11): math, pydantic, _extract_power_hint(), _numeric_tokens(), Deterministic IES LM-63 and EULUMDAT (LDT) photometry parsing. The parsed…, Pull a plausible wattage hint such as ``2x36W`` from a lamp label., _evaluation_axis(), _fixture_positions() (+3 more)

### Community 41 - "Photometry Format Parsers"
Cohesion: 0.24
Nodes (11): _NumberStream, _parse_ies(), _parse_ldt(), PhotometryParseError, ValueError, Whitespace-token cursor shared by both parsers., Raised when a photometric file cannot be parsed deterministically., Read angles plus candela rows; try with and without trailing header extras.… (+3 more)

### Community 42 - "Chroma Evidence Store"
Cohesion: 0.29
Nodes (6): ChromaEvidenceStore, EvidenceNotFoundError, ValueError, Optional semantic backend using Chroma and BGE embeddings., Delete project vectors and their durable audit rows., Evidence

### Community 43 - "Domain Schema Models"
Cohesion: 0.18
Nodes (11): datetime, enum, BriefTemplateOrigin, FloorPlan, LuminaireBriefValidation, Versioned, serialisable domain models. Facts used for calculations live here…, A re-check of a saved candidate against one immutable project revision., Parsed drawing facts. They are never used for design until applied. (+3 more)

### Community 44 - "Application Configuration"
Cohesion: 0.17
Nodes (10): dotenv, langchain_openai, os, re, ensure_data_directories(), _env_optional_bool(), Configuration and filesystem locations for the lighting assistant., Read an optional, forgiving boolean environment setting. (+2 more)

### Community 45 - "Photometry Distribution Math"
Cohesion: 0.21
Nodes (8): _covers_circle(), _integrate_sphere_flux(), PhotometryDistribution, model_validator, Solid-angle integration with midpoint candela per azimuth sector., One parsed luminous-intensity table in absolute candelas., True when the C angles describe a closed revolution around the axis., Integrate the table over the sphere (Type C orientation assumed).

### Community 46 - "Local Evidence Store"
Cohesion: 0.21
Nodes (6): LocalEvidenceStore, Path, 基于 SQLite 的确定性检索，用于离线与测试部署。, Remove all project-scoped evidence and leave global knowledge intact., tokenize(), test_cli_creates_project_and_report_in_store()

### Community 47 - "DIALux HTML Parsing"
Cohesion: 0.21
Nodes (11): bs4, requests_adapters, _field_float(), _field_int(), _field_value(), _ip_components(), _ip_meets_minimum(), Defensive, auditable client for DIALux Luminaire Finder. (+3 more)

### Community 48 - "HTTP Retry Transport"
Cohesion: 0.20
Nodes (6): Request, Notify the active request each time the SDK is about to retry., _RetryNotifyingTransport, Any, RedirectSession, Response

### Community 49 - "Calculation Input Preparation"
Cohesion: 0.20
Nodes (11): calculate_preliminary_lighting(), CalculationInputIncompleteError, CalculationToolInput, _candidate_values_for_calculation(), _prepare_calculation_input(), ValueError, Provider-facing calculation input with backwards-compatible aliases. The…, A recoverable calculation request that needs user/project data. (+3 more)

### Community 50 - "Document Chunk Indexing"
Cohesion: 0.25
Nodes (7): ParsedDocument, chunk_text(), Return the identifier shared by the audit database and Chroma index., Keep identical files in different project scopes as separate records., scoped_source_hash(), stable_chunk_id(), test_audit_store_upserts_chroma_compatible_chunk_ids()

### Community 51 - "DIALux Handoff Tools"
Cohesion: 0.22
Nodes (11): _artifact_path(), create_dialux_task_package(), _dialux_client(), generate_design_report(), _project_directory(), Path, Hand one saved candidate to the local DIALux evo via the dial:// protocol…, Create a ZIP handoff with the task manifest and named photometry ZIP files. (+3 more)

### Community 53 - "HTTP Response Test Doubles"
Cohesion: 0.35
Nodes (4): _detail_html(), FakeResponse, Any, _search_payload()

### Community 54 - "Luminaire Search Ranking"
Cohesion: 0.24
Nodes (5): _brand_key(), DialuxSearchResult, Compatibility wrapper returning only candidates., LuminaireSearchRun, Reproducible provenance for one vendor-directory lookup.

### Community 55 - "Luminaire Search Tools"
Cohesion: 0.22
Nodes (10): candidate_summary(), Return bounded supplier facts suitable for an LLM tool result., get_luminaire_detail(), _luminaire_request(), _prepare_luminaire_request(), prepare_luminaire_search(), Validate deterministic search prerequisites before any DIALux request., Find traceable DIALux candidates after deterministic input validation.… (+2 more)

### Community 56 - "Floor Plan Layout Analysis"
Cohesion: 0.22
Nodes (9): 二维 DXF/DWG 平面图分析, LayoutCandidate 候选布灯方案, DXF 与 PDF 坐标统一, 确定性几何布局检查, 灯具坐标与照明布局分析方案, 照明类别证据优先级, LuminairePlacement, 512 会议室布局案例 (+1 more)

### Community 57 - "DIALux Luminaire Finder"
Cohesion: 0.28
Nodes (9): DIALux Luminaire Finder 集成, 灯具详情页端点, DIALux Luminaire Finder API, 灯具图片与配光资产, Luminaire Finder 搜索端点, 搜索建议端点, Contract Panel 4000K 详情页样本, 18W 4000K CRI90 产品参数 (+1 more)

### Community 58 - "Meeting Room Standards"
Cohesion: 0.31
Nodes (9): 维持平均照度标准, 普通办公室与会议室照明要求, 统一眩光值 UGR, 照度均匀度 U0, 视频会议室照明要求, 会议室照明设计网络资料摘录, 二次网络资料引用限制, 面光顶光逆光三层布光 (+1 more)

### Community 59 - "DIALux Validation Workflow"
Cohesion: 0.25
Nodes (8): 正式交付质量闸门, handoff_id 与输入快照哈希, 人机 DIALux 迭代闭环, SimulationRun, 旧仿真结果失效机制, 设计条件补充循环, 室内照明设计智能体工作流程, 仿真复核与方案修订循环

### Community 60 - "Evidence Repair Utilities"
Cohesion: 0.32
Nodes (8): backfill_audit_from_chroma(), main(), Path, Copy existing Chroma records into the SQLite audit tables by stable chunk ID., Synchronize evidence and re-evaluate every saved project against its brief., Quarantine the current Chroma files and rebuild vectors from SQLite evidence., rebuild_chroma_index(), repair_database()

### Community 61 - "RAG Parameter Tests"
Cohesion: 0.36
Nodes (4): FakeEvidenceStore, test_rag_tool_does_not_overwrite_existing_manual_parameter(), test_rag_tool_persists_lighting_parameters_and_field_provenance(), test_rag_tool_records_provenance_when_value_matches_existing()

### Community 62 - "Workbench Architecture"
Cohesion: 0.29
Nodes (7): 版本化 ProjectState, 室内照明设计智能体技术方案, Agent 与确定性工具边界, 日光实验室视觉系统, 七个等权工作区, 工作台优先界面, 照明设计工作台界面设计

### Community 63 - "Product Field Normalization"
Cohesion: 0.38
Nodes (6): Pattern, _first_float(), _first_int(), _first_text(), _optional_text(), _safe_detail_fields()

### Community 64 - "Document Catalog Operations"
Cohesion: 0.29
Nodes (3): List indexed documents in one scope, including their chunk counts., Delete one indexed document and its cascaded evidence chunks., StoredDocument

### Community 65 - "Audit Chunk Persistence"
Cohesion: 0.29
Nodes (3): Mirror externally indexed chunks into the durable audit tables., Return one document's chunks in index order for full-content preview., StoredChunk

### Community 66 - "Provider Value Normalization"
Cohesion: 0.29
Nodes (5): _calculation_number(), _merge_nested_calculation_fields(), model_validator, Accept provider values such as ``"1200 lm"`` without weakening bounds., Extract calculation fields from provider objects and ignore metadata.

### Community 67 - "Lighting Design Dataflow"
Cohesion: 0.33
Nodes (6): DesignBrief, 可追溯 Evidence 与 RAG, 流明法初算, DIALux evo 交接包, 流明法与 DIALux 联合照度闸门, 确定性交付物生成

### Community 68 - "Standards Regression Baseline"
Cohesion: 0.33
Nodes (6): 色温与显色质量, 照明功率密度 LPD, 《建筑照明设计标准》2024, 阶段 0 脱敏离线回归基线, 脱敏固定快照回归策略, GB 50034-2024 标准测试快照

### Community 70 - "Calculation Tool Tests"
Cohesion: 0.47
Nodes (5): _store_with_selected_luminaire(), test_agent_calculation_accepts_nested_luminaire_object(), test_agent_calculation_fills_project_and_selected_luminaire_values(), test_agent_calculation_returns_recoverable_missing_luminaire_inputs(), test_calculation_input_ignores_removed_group_fields()

### Community 71 - "Command Line Interface"
Cohesion: 0.60
Nodes (4): ArgumentParser, _add_brief_arguments(), build_parser(), Command-line interface for local, testable lighting-design workflows.

### Community 73 - "Workspace Directory Selection"
Cohesion: 0.40
Nodes (4): choose_workspace_directory(), Path, Open the native Windows directory picker for the local desktop user., _unique_upload_target()

### Community 75 - "Clarification Card UI"
Cohesion: 0.60
Nodes (5): ClarificationCard(), submit(), toggleOption(), updateValue(), fieldValue()

### Community 77 - "Auditable Revision Workflow"
Cohesion: 0.50
Nodes (4): 可审计照明设计工作流, LuxBeyond 室内照明设计智能体, ProjectState Revision 乐观锁, expected_revision 冲突保护

### Community 80 - "Calculation Panel UI"
Cohesion: 0.67
Nodes (3): CalculationPanel(), calculate(), checkRule()

## Knowledge Gaps
- **95 isolated node(s):** `lighting-design-agent`, `metadata`, `numericFields`, `NumericBriefKey`, `ProjectDraft` (+90 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 467 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **11 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `create_app()` connect `Project Document API` to `SQLite Database Recovery`, `Model Runtime Configuration`, `DXF Geometry Dependencies`, `DIALux Workflow Tests`, `DIALux Protocol Handling`, `Luminaire Constraint Matching`, `FastAPI Domain Models`, `Document Loading Pipeline`, `Chat Session Persistence`, `Preliminary Lighting Calculations`, `DIALux API Client`, `Photometry Asset Storage`, `Project Revision Persistence`, `Agent Runtime Orchestration`, `Photometry Preview Pipeline`, `Project Store Operations`, `Chat API Lifecycle`, `Deterministic Deliverables`, `Design Brief Validation`, `DIALux Result Analysis`, `Retry Error Handling`, `Persistence Integration Tests`, `Joint Illuminance Verification`, `Photometry Preview Tests`, `Photometry Format Parsers`, `Chroma Evidence Store`, `Application Configuration`, `Workspace Evidence Store`, `Workspace Directory Selection`, `Reasoning Effort Tests`?**
  _High betweenness centrality (0.093) - this node is a cross-community bridge._
- **Why does `DialuxAPI` connect `DIALux API Client` to `Model Runtime Configuration`, `Project Document API`, `Photometry Archive Tests`, `Luminaire Constraint Matching`, `Command Line Interface`, `FastAPI Domain Models`, `Preliminary Lighting Calculations`, `Retrying HTTP Sessions`, `DIALux HTML Parsing`, `DIALux Handoff Tools`, `Agent Tool Schemas`, `Luminaire Search Ranking`, `Product Field Normalization`?**
  _High betweenness centrality (0.049) - this node is a cross-community bridge._
- **Why does `ProjectStore` connect `Project Store Operations` to `SQLite Database Recovery`, `Model Runtime Configuration`, `DXF Geometry Dependencies`, `Project Document API`, `DIALux Workflow Tests`, `DIALux Protocol Handling`, `Luminaire Constraint Matching`, `FastAPI Domain Models`, `Preliminary Lighting Calculations`, `Project Revision Persistence`, `Agent Tool Schemas`, `Application Launcher Utilities`, `Deterministic Deliverables`, `Design Brief Validation`, `Persistence Integration Tests`, `Photometry Preview Tests`, `Domain Schema Models`, `Local Evidence Store`, `Luminaire Search Ranking`, `Evidence Repair Utilities`, `RAG Parameter Tests`, `Calculation Tool Tests`, `Command Line Interface`, `Reasoning Effort Tests`?**
  _High betweenness centrality (0.041) - this node is a cross-community bridge._
- **Are the 16 inferred relationships involving `create_app()` (e.g. with `PhotometryParseError` and `PreviewGeometryError`) actually correct?**
  _`create_app()` has 16 INFERRED edges - model-reasoned connections that need verification._
- **Are the 67 inferred relationships involving `ProjectStore` (e.g. with `DesignBrief` and `FloorPlan`) actually correct?**
  _`ProjectStore` has 67 INFERRED edges - model-reasoned connections that need verification._
- **Are the 54 inferred relationships involving `DesignBrief` (e.g. with `apply_brief_constraints()` and `validate_luminaire_search()`) actually correct?**
  _`DesignBrief` has 54 INFERRED edges - model-reasoned connections that need verification._
- **Are the 35 inferred relationships involving `ProjectState` (e.g. with `_dialux_check()` and `evaluate_illuminance()`) actually correct?**
  _`ProjectState` has 35 INFERRED edges - model-reasoned connections that need verification._