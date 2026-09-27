// REST 客户端：统一解包 {code, data, message}
import axios, { AxiosInstance } from "axios";
import type {
  AccountStatus,
  ApiResponse,
  AppConfig,
  Artifact,
  BatchDeleteResponse,
  CacheFile,
  FileMetaData,
  LlmStatus,
  NeedInfo,
  NlParseResult,
  PlotConfig,
  RequestSchema,
  SingleDeleteResponse,
  Task,
  VariableMapData,
} from "../types";

// 基地址：走 Vite 代理 /api（同源免 CORS）；也可用 VITE_API_BASE 直连后端
const BASE = (import.meta.env.VITE_API_BASE as string | undefined) || "/api";

const http: AxiosInstance = axios.create({
  baseURL: BASE,
  timeout: 60000,
});

// 统一响应解包：code!==0 抛业务错误
http.interceptors.response.use(
  (resp) => {
    const body = resp.data as ApiResponse;
    if (body && typeof body.code === "number" && body.code !== 0) {
      return Promise.reject(new Error(`[${body.code}] ${body.message}`));
    }
    return resp;
  },
  (err) => Promise.reject(err)
);

async function unwrap<T>(p: Promise<{ data: ApiResponse<T> }>): Promise<T> {
  const resp = await p;
  return resp.data.data;
}

export const api = {
  // ---- NL ----
  nlParse: (text: string, session_id?: string) =>
    unwrap<NlParseResult>(http.post("/nl/parse", { text, session_id })),
  nlClarify: (session_id: string, answers: Record<string, unknown>) =>
    unwrap<NlParseResult>(http.post("/nl/clarify", { session_id, answers })),

  // ---- Download ----
  downloadSubmit: (request_schema: RequestSchema) =>
    unwrap<{ task_id: string }>(
      http.post("/download/submit", { request_schema })
    ),
  downloadGet: (task_id: string) =>
    unwrap<Task>(http.get(`/download/${task_id}`)),
  downloadList: (status?: string, page = 1, size = 20) =>
    unwrap<{ tasks: Task[]; total: number }>(
      http.get("/download/list", { params: { status, page, size } })
    ),
  downloadCancel: (task_id: string) =>
    unwrap<{ task_id: string; status: string }>(
      http.post(`/download/${task_id}/cancel`)
    ),
  downloadResume: (task_id: string) =>
    unwrap<{ task_id: string }>(http.post(`/download/${task_id}/resume`)),
  // 一键补漏：只重下失败块（已成功块自动跳过，不重跑全量）
  downloadRetryFailed: (task_id: string) =>
    unwrap<{ task_id: string }>(
      http.post(`/download/${task_id}/retry-failed`)
    ),
  downloadDelete: (task_id: string, delete_files = false) =>
    unwrap<{ task_id: string }>(
      http.delete(`/download/${task_id}`, { params: { delete_files } })
    ),
  // 一键清空全部任务（running 任务由后端跳过并返回 skipped_running）
  downloadDeleteAll: (delete_files: boolean) =>
    unwrap<{ deleted: number; skipped_running: number; delete_files: boolean }>(
      http.post("/download/delete-all", { delete_files })
    ),

  // ---- Plot ----
  plotRender: (
    body: {
      task_id?: string;
      request_schema?: RequestSchema;
      profile: string;
      overrides?: Partial<PlotConfig>;
    }
  ) => unwrap<{ artifact: Artifact }>(http.post("/plot/render", body)),
  plotProfiles: () => unwrap<{ profiles: PlotConfig[] }>(http.get("/plot/profiles")),
  plotProfileGet: (name: string) =>
    unwrap<{ profile: PlotConfig }>(http.get(`/plot/profiles/${name}`)),
  plotProfilePut: (name: string, profile: PlotConfig) =>
    unwrap<{ profile: PlotConfig; version: number }>(
      http.put(`/plot/profiles/${name}`, { profile })
    ),
  plotProfileCreate: (profile: PlotConfig) =>
    unwrap<{ profile: PlotConfig }>(http.post("/plot/profiles", { profile })),

  // ---- Account ----
  accountStatus: () => unwrap<AccountStatus>(http.get("/account/status")),
  accountValidate: (api_key: string) =>
    unwrap<{ valid: boolean; error?: string }>(
      http.post("/account/validate", { api_key })
    ),
  accountFinalize: (api_key: string) =>
    unwrap<AccountStatus>(http.post("/account/finalize", { api_key })),
  accountClear: () => unwrap<AccountStatus>(http.delete("/account/credentials")),
  accountTestDownload: () =>
    unwrap<{ task_id: string }>(http.post("/account/test-download")),

  // ---- Config ----
  configGet: () => unwrap<AppConfig>(http.get("/config")),
  configPut: (settings: Partial<AppConfig>) =>
    unwrap<AppConfig>(http.put("/config", { settings })),
  variableMap: () => unwrap<{ variable_map: VariableMapData }>(http.get("/config/variable-map")),
  llmGet: () => unwrap<LlmStatus>(http.get("/config/llm")),
  llmPut: (body: { api_key?: string; base_url?: string; model?: string }) =>
    unwrap<{ has_key: boolean }>(http.put("/config/llm", body)),

  // ---- Data（本地缓存文件管理，design-data-manager.md §4）----
  dataFiles: () =>
    unwrap<{ files: CacheFile[]; total: number }>(http.get("/data/files")),
  dataMetadata: (path: string) =>
    unwrap<FileMetaData>(
      http.get("/data/files/metadata", { params: { path } })
    ),
  dataDeleteFile: (path: string) =>
    unwrap<SingleDeleteResponse>(
      http.delete("/data/files", { params: { path } })
    ),
  // 批删用 `?paths=a&paths=b` 手拼，避免 axios 数组序列化为 paths[]=a 的差异
  dataDeleteFiles: (paths: string[]) => {
    const qs = paths
      .map((p) => `paths=${encodeURIComponent(p)}`)
      .join("&");
    return unwrap<BatchDeleteResponse>(http.delete(`/data/files?${qs}`));
  },
};

export type { NeedInfo };
export default api;
