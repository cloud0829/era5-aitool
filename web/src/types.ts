// 前后端契约类型（与 backend/era5tool/config/schema.py / models/task.py 对应）

export interface ApiResponse<T = unknown> {
  code: number;
  data: T;
  message: string;
}

export interface Area {
  west: number;
  south: number;
  east: number;
  north: number;
}

export interface Timerange {
  start: string;
  end: string;
}

export interface RequestSchema {
  dataset: string;
  dataset_family: string;
  variables: string[];
  pressure_levels: number[] | null;
  timerange: Timerange;
  area: Area;
  frequency: "hourly" | "daily" | "monthly";
  aggregation: "raw" | "mean" | "sum" | "max" | "min";
  confidence: number;
}

export interface NeedInfo {
  missing: string[];
  questions: string[];
}

export interface NlParseResult {
  request_schema?: RequestSchema;
  need_info?: NeedInfo;
  session_id: string;
  engine: "deepseek" | "rule";
  confirm: boolean;
}

export interface BlockStats {
  total: number;
  done: number;
  failed: number;
  skipped: number;
}

export interface Task {
  id: string;
  type: string;
  status: "pending" | "running" | "success" | "failed" | "paused";
  progress: number;
  params: Record<string, unknown>;
  block_stats: BlockStats;
  result: Record<string, unknown>;
  error: { code: string; message: string } | null;
  created_at: string;
  updated_at: string;
}

export interface Artifact {
  url: string;
  format: string;
  size: number;
}

export interface PlotConfig {
  name: string;
  plot_type: "map" | "timeseries" | "animation";
  projection: Record<string, unknown>;
  basemap: "cartopy" | "plain";
  colormap: string;
  aggregation: string;
  output_format: string;
  title: string;
  grid_step: number | null;
  animation_frames: number;
  region: Partial<Area>;
}

export interface AccountStatus {
  state: "INIT" | "GUIDE_REGISTER" | "WAIT_USER" | "VALIDATING" | "READY" | "ERROR";
  has_key: boolean;
  error?: string;
}

export interface LlmStatus {
  has_key: boolean;
  base_url: string;
  model: string;
  provider: string;
}

export interface AppConfig {
  version: number;
  llm: LlmStatus;
  download: {
    cds_max_workers: number;
    retry_max: number;
    backoff_base: number;
    backoff_factor: number;
    backoff_max: number;
    backoff_jitter: number;
    mock: boolean;
    cache_ttl_days: number;
    cache_max_gb: number;
  };
  plot: { default_profile: string; offline_geo_dir: string };
  paths: { config_dir: string; data_dir: string };
}

export interface VariableMapData {
  version: number;
  synonyms: Record<string, { names: string[]; datasets: string[] }>;
}

// WS 事件（design-final.md §7.4）
export interface WsEvent {
  type: "progress" | "status" | "log" | "done" | "subscribed";
  task_id: string;
  status?: string;
  phase?: string;
  block_key?: string;
  block_index?: number;
  block_total?: number;
  progress?: number;
  message?: string;
  level?: string;
  ts: string;
}

// ========== 数据管理（design-data-manager.md §3.3） ==========

export type CacheFileStatus = "ready" | "busy";

export interface CacheFile {
  rel_path: string;
  dataset: string;
  variable: string;
  freq: string; // "hourly" | "monthly" | ""（反解析失败）
  period: string; // "2020" | "2020-01" | "2020-01-05" | ""
  size: number;
  human_size: string;
  status: CacheFileStatus;
  busy_by: string[];
  mtime: string;
  parsed: boolean;
}

export interface NetcdfDimMeta {
  name: string;
  length: number;
  is_unlimited: boolean;
}

export interface NetcdfVarMeta {
  name: string;
  long_name?: string | null;
  units?: string | null;
  dims: string[];
  shape: number[];
  is_coord: boolean;
}

export interface FileMetaData {
  ok: boolean;
  error?: string;
  format?: string;
  variables?: NetcdfVarMeta[];
  dimensions?: NetcdfDimMeta[];
  coords?: string[];
  time_range?: {
    start: string;
    end: string;
    units: string;
    calendar: string;
  } | null;
  global_attrs?: Record<string, unknown>;
}

export type DeleteStatus = "ok" | "busy" | "not_found" | "failed";

export interface DeleteFileResult {
  path: string;
  status: DeleteStatus;
  error?: string;
  busy_by?: string[];
  released_bytes: number;
}

export interface SingleDeleteResponse {
  path: string;
  status: "ok";
  released_bytes: number;
}

export interface BatchDeleteResponse {
  requested: number;
  deleted: number;
  busy: number;
  failed: number;
  released_bytes: number;
  results: DeleteFileResult[];
}
