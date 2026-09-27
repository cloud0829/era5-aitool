// Zustand 全局状态：账号 / 任务 / 配置 / WS 事件
import { create } from "zustand";
import type {
  AccountStatus,
  AppConfig,
  LlmStatus,
  PlotConfig,
  Task,
  VariableMapData,
  WsEvent,
} from "../types";

interface AppState {
  account: AccountStatus | null;
  config: AppConfig | null;
  llm: LlmStatus | null;
  variableMap: VariableMapData | null;
  profiles: PlotConfig[];
  tasks: Task[];
  tasksTotal: number;
  wsConnected: boolean;
  lastEvent: WsEvent | null;

  setAccount: (a: AccountStatus | null) => void;
  setConfig: (c: AppConfig | null) => void;
  setLlm: (l: LlmStatus | null) => void;
  setVariableMap: (v: VariableMapData | null) => void;
  setProfiles: (p: PlotConfig[]) => void;
  setTasks: (tasks: Task[], total: number) => void;
  upsertTask: (t: Task) => void;
  removeTask: (id: string) => void;
  setWsConnected: (b: boolean) => void;
  setLastEvent: (e: WsEvent | null) => void;
}

export const useAppStore = create<AppState>((set) => ({
  account: null,
  config: null,
  llm: null,
  variableMap: null,
  profiles: [],
  tasks: [],
  tasksTotal: 0,
  wsConnected: false,
  lastEvent: null,

  setAccount: (a) => set({ account: a }),
  setConfig: (c) => set({ config: c }),
  setLlm: (l) => set({ llm: l }),
  setVariableMap: (v) => set({ variableMap: v }),
  setProfiles: (p) => set({ profiles: p }),
  setTasks: (tasks, tasksTotal) => set({ tasks, tasksTotal }),
  upsertTask: (t) =>
    set((s) => {
      const idx = s.tasks.findIndex((x) => x.id === t.id);
      const tasks = idx >= 0 ? s.tasks.map((x, i) => (i === idx ? t : x)) : [t, ...s.tasks];
      return { tasks };
    }),
  removeTask: (id) =>
    set((s) => ({ tasks: s.tasks.filter((x) => x.id !== id) })),
  setWsConnected: (b) => set({ wsConnected: b }),
  setLastEvent: (e) => set({ lastEvent: e }),
}));
