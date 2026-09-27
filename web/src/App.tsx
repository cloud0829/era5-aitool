import { useEffect } from "react";
import { HashRouter, Navigate, Route, Routes } from "react-router-dom";
import Layout from "./components/common/Layout";
import OverviewPage from "./pages/OverviewPage";
import ChatPage from "./pages/ChatPage";
import PlotPage from "./pages/PlotPage";
import ConfigPage from "./pages/ConfigPage";
import TasksPage from "./pages/TasksPage";
import DataPage from "./pages/DataPage";
import WizardPage from "./pages/WizardPage";
import api from "./api/client";
import { wsClient } from "./api/ws";
import { useAppStore } from "./store/appStore";

export default function App() {
  const setAccount = useAppStore((s) => s.setAccount);
  const setConfig = useAppStore((s) => s.setConfig);
  const setLlm = useAppStore((s) => s.setLlm);
  const setVariableMap = useAppStore((s) => s.setVariableMap);
  const setProfiles = useAppStore((s) => s.setProfiles);
  const upsertTask = useAppStore((s) => s.upsertTask);
  const setWsConnected = useAppStore((s) => s.setWsConnected);
  const setLastEvent = useAppStore((s) => s.setLastEvent);

  useEffect(() => {
    // 启动拉取基础状态
    api
      .accountStatus()
      .then(setAccount)
      .catch(() => setAccount(null));
    api
      .configGet()
      .then((c) => {
        setConfig(c);
        setLlm(c.llm);
      })
      .catch(() => setConfig(null));
    api
      .variableMap()
      .then((v) => setVariableMap(v.variable_map))
      .catch(() => setVariableMap(null));
    api
      .plotProfiles()
      .then((p) => setProfiles(p.profiles))
      .catch(() => setProfiles([]));

    // WS 实时事件 → 任务状态更新
    wsClient.connect();
    const off = wsClient.on((ev) => {
      setLastEvent(ev);
      if (ev.type === "subscribed" && ev.message === "connected") {
        setWsConnected(true);
      }
      if (ev.task_id) {
        api
          .downloadGet(ev.task_id)
          .then(upsertTask)
          .catch(() => undefined);
      }
    });
    return () => {
      off();
      wsClient.close();
    };
  }, [setAccount, setConfig, setLlm, setVariableMap, setProfiles, upsertTask, setWsConnected, setLastEvent]);

  return (
    <HashRouter>
      <Layout>
        <Routes>
          <Route path="/" element={<OverviewPage />} />
          <Route path="/chat" element={<ChatPage />} />
          <Route path="/plot" element={<PlotPage />} />
          <Route path="/config" element={<ConfigPage />} />
          <Route path="/tasks" element={<TasksPage />} />
          <Route path="/data" element={<DataPage />} />
          <Route path="/wizard" element={<WizardPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Layout>
    </HashRouter>
  );
}
