import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  Grid,
  List,
  ListItem,
  ListItemText,
  TextField,
  Typography,
} from "@mui/material";
import SaveIcon from "@mui/icons-material/Save";
import { useEffect, useState } from "react";
import api from "../api/client";
import { useAppStore } from "../store/appStore";
import type { AppConfig } from "../types";

export default function ConfigPage() {
  const config = useAppStore((s) => s.config);
  const llm = useAppStore((s) => s.llm);
  const setConfig = useAppStore((s) => s.setConfig);
  const setLlm = useAppStore((s) => s.setLlm);
  const variableMap = useAppStore((s) => s.variableMap);
  const setVariableMap = useAppStore((s) => s.setVariableMap);
  const profiles = useAppStore((s) => s.profiles);
  const setProfiles = useAppStore((s) => s.setProfiles);

  const [workers, setWorkers] = useState(4);
  const [retry, setRetry] = useState(3);
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("https://api.deepseek.com");
  const [model, setModel] = useState("deepseek-chat");
  const [msg, setMsg] = useState("");

  useEffect(() => {
    if (config) {
      setWorkers(config.download.cds_max_workers);
      setRetry(config.download.retry_max);
    }
    if (llm) {
      setBaseUrl(llm.base_url);
      setModel(llm.model);
    }
    if (!variableMap) {
      api.variableMap().then((v) => setVariableMap(v.variable_map)).catch(() => undefined);
    }
    if (profiles.length === 0) {
      api.plotProfiles().then((p) => setProfiles(p.profiles)).catch(() => undefined);
    }
  }, [config, llm, variableMap, profiles, setVariableMap, setProfiles]);

  const handleSaveConfig = async () => {
    try {
      const c = await api.configPut({
        download: {
          ...(config?.download ?? ({} as AppConfig["download"])),
          cds_max_workers: workers,
          retry_max: retry,
        },
      });
      setConfig(c);
      setMsg("全局配置已保存");
    } catch (e) {
      setMsg((e as Error).message);
    }
  };

  const handleSaveLlm = async () => {
    try {
      const r = await api.llmPut({ api_key: apiKey || undefined, base_url: baseUrl, model });
      setLlm({ has_key: r.has_key, base_url: baseUrl, model, provider: llm?.provider ?? "deepseek" });
      setApiKey("");
      setMsg(`LLM 配置已保存（has_key=${r.has_key}）`);
    } catch (e) {
      setMsg((e as Error).message);
    }
  };

  return (
    <Box>
      <Typography variant="h5" sx={{ mb: 2, fontWeight: 700 }}>配置</Typography>
      {msg && <Alert severity="info" sx={{ mb: 2 }} onClose={() => setMsg("")}>{msg}</Alert>}

      <Grid container spacing={2}>
        <Grid item xs={12} md={6}>
          <Card variant="outlined" sx={{ mb: 2 }}>
            <CardContent>
              <Typography variant="h6" sx={{ mb: 1 }}>下载并发与重试</Typography>
              <Grid container spacing={2}>
                <Grid item xs={6}>
                  <TextField fullWidth type="number" label="并发上限 (≤16)" value={workers}
                    onChange={(e) => setWorkers(Math.max(1, Math.min(16, Number(e.target.value))))} />
                </Grid>
                <Grid item xs={6}>
                  <TextField fullWidth type="number" label="重试次数" value={retry}
                    onChange={(e) => setRetry(Math.max(0, Number(e.target.value)))} />
                </Grid>
              </Grid>
              <Box sx={{ mt: 2, display: "flex", gap: 1, alignItems: "center" }}>
                <Chip
                  label={config?.download.mock ? "mock 模式（无真实凭据）" : "real 模式"}
                  color={config?.download.mock ? "warning" : "success"}
                  size="small"
                />
                <Button variant="contained" onClick={handleSaveConfig} startIcon={<SaveIcon />}>
                  保存
                </Button>
              </Box>
            </CardContent>
          </Card>

          <Card variant="outlined">
            <CardContent>
              <Typography variant="h6" sx={{ mb: 1 }}>DeepSeek LLM（自然语言解析）</Typography>
              <Chip
                label={llm?.has_key ? "已配置 Key" : "未配置 Key（规则兜底）"}
                color={llm?.has_key ? "success" : "warning"}
                size="small"
                sx={{ mb: 2 }}
              />
              <Grid container spacing={2}>
                <Grid item xs={12}>
                  <TextField fullWidth type="password" label="DEEPSEEK_API_KEY（留空不修改）"
                    value={apiKey} onChange={(e) => setApiKey(e.target.value)} />
                </Grid>
                <Grid item xs={12} md={6}>
                  <TextField fullWidth label="Base URL" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} />
                </Grid>
                <Grid item xs={12} md={6}>
                  <TextField fullWidth label="模型" value={model} onChange={(e) => setModel(e.target.value)} />
                </Grid>
              </Grid>
              <Button variant="contained" onClick={handleSaveLlm} startIcon={<SaveIcon />} sx={{ mt: 2 }}>
                保存 LLM 配置
              </Button>
            </CardContent>
          </Card>
        </Grid>

        <Grid item xs={12} md={6}>
          <Card variant="outlined" sx={{ mb: 2 }}>
            <CardContent>
              <Typography variant="h6" sx={{ mb: 1 }}>出图 Profile</Typography>
              <List dense>
                {profiles.map((p) => (
                  <ListItem key={p.name} divider>
                    <ListItemText primary={p.name} secondary={`${p.plot_type} · ${p.colormap} · ${p.basemap}`} />
                  </ListItem>
                ))}
              </List>
            </CardContent>
          </Card>

          <Card variant="outlined">
            <CardContent>
              <Typography variant="h6" sx={{ mb: 1 }}>
                变量映射词典（{variableMap ? Object.keys(variableMap.synonyms).length : 0} 词条）
              </Typography>
              <Typography variant="caption" color="text.secondary">
                config/variable_map.json · 支持 ERA5 / ERA5-Land 双家族过滤
              </Typography>
              <List dense sx={{ maxHeight: 320, overflow: "auto" }}>
                {variableMap &&
                  Object.entries(variableMap.synonyms).slice(0, 40).map(([varName, meta]) => (
                    <ListItem key={varName} divider>
                      <ListItemText
                        primary={varName}
                        secondary={meta.names.join(" / ") + `  [${meta.datasets.join(", ")}]`}
                      />
                    </ListItem>
                  ))}
              </List>
            </CardContent>
          </Card>
        </Grid>
      </Grid>
    </Box>
  );
}
