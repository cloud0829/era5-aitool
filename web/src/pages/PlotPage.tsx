import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  FormControl,
  Grid,
  InputLabel,
  MenuItem,
  Select,
  TextField,
  Typography,
} from "@mui/material";
import ImageIcon from "@mui/icons-material/Image";
import { useEffect, useState } from "react";
import api from "../api/client";
import { useAppStore } from "../store/appStore";
import type { Artifact, PlotConfig, Task } from "../types";

export default function PlotPage() {
  const profiles = useAppStore((s) => s.profiles);
  const tasks = useAppStore((s) => s.tasks);
  const setTasks = useAppStore((s) => s.setTasks);

  const [taskId, setTaskId] = useState<string>("");
  const [profile, setProfile] = useState("default_map");
  const [plotType, setPlotType] = useState<PlotConfig["plot_type"]>("map");
  const [colormap, setColormap] = useState("RdYlBu_r");
  const [aggregation, setAggregation] = useState("raw");
  const [basemap, setBasemap] = useState<PlotConfig["basemap"]>("cartopy");
  const [frames, setFrames] = useState(10);
  const [artifact, setArtifact] = useState<Artifact | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");

  useEffect(() => {
    api
      .downloadList("", 1, 50)
      .then((r) => setTasks(r.tasks, r.total))
      .catch(() => undefined);
  }, [setTasks]);

  const handleRender = async () => {
    setBusy(true);
    setMsg("");
    setArtifact(null);
    try {
      const body: {
        task_id?: string;
        request_schema?: Parameters<typeof api.plotRender>[0]["request_schema"];
        profile: string;
        overrides: Record<string, unknown>;
      } = {
        profile,
        overrides: { plot_type: plotType, colormap, aggregation, basemap, animation_frames: frames },
      };
      if (taskId) {
        body.task_id = taskId;
      } else {
        // 无任务：用一个默认 schema 合成数据出图
        body.request_schema = {
          dataset: "reanalysis-era5-land",
          dataset_family: "land",
          variables: ["2m_temperature"],
          pressure_levels: null,
          timerange: { start: "2020-06-01", end: "2020-06-30" },
          area: { west: 118, south: 29, east: 123, north: 34 },
          frequency: "hourly",
          aggregation: "raw",
          confidence: 0.9,
        };
      }
      const r = await api.plotRender(body);
      setArtifact(r.artifact);
    } catch (e) {
      setMsg(`出图失败: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Box>
      <Typography variant="h5" sx={{ mb: 2, fontWeight: 700 }}>出图</Typography>

      <Grid container spacing={2}>
        <Grid item xs={12} md={4}>
          <Card variant="outlined">
            <CardContent>
              <Typography variant="subtitle2" sx={{ mb: 1 }}>数据来源</Typography>
              <FormControl fullWidth size="small" sx={{ mb: 2 }}>
                <InputLabel>任务（可选）</InputLabel>
                <Select value={taskId} label="任务（可选）" onChange={(e) => setTaskId(e.target.value as string)}>
                  <MenuItem value="">（无任务 · 合成数据）</MenuItem>
                  {tasks
                    .filter((t: Task) => t.status === "success")
                    .map((t: Task) => (
                      <MenuItem key={t.id} value={t.id}>{t.id}</MenuItem>
                    ))}
                </Select>
              </FormControl>

              <Typography variant="subtitle2" sx={{ mb: 1 }}>Profile</Typography>
              <FormControl fullWidth size="small" sx={{ mb: 2 }}>
                <InputLabel>Profile</InputLabel>
                <Select value={profile} label="Profile" onChange={(e) => setProfile(e.target.value as string)}>
                  {profiles.map((p) => (
                    <MenuItem key={p.name} value={p.name}>{p.name}</MenuItem>
                  ))}
                </Select>
              </FormControl>

              <Typography variant="subtitle2" sx={{ mb: 1 }}>面板覆盖（仅本次）</Typography>
              <FormControl fullWidth size="small" sx={{ mb: 1 }}>
                <InputLabel>图类型</InputLabel>
                <Select value={plotType} label="图类型" onChange={(e) => setPlotType(e.target.value as PlotConfig["plot_type"])}>
                  <MenuItem value="map">空间图</MenuItem>
                  <MenuItem value="timeseries">时序图</MenuItem>
                  <MenuItem value="animation">动画</MenuItem>
                </Select>
              </FormControl>
              <FormControl fullWidth size="small" sx={{ mb: 1 }}>
                <InputLabel>配色</InputLabel>
                <Select value={colormap} label="配色" onChange={(e) => setColormap(e.target.value as string)}>
                  {["RdYlBu_r", "RdYlBu", "viridis", "plasma", "magma", "jet", "coolwarm", "terrain", "Blues"].map((c) => (
                    <MenuItem key={c} value={c}>{c}</MenuItem>
                  ))}
                </Select>
              </FormControl>
              <FormControl fullWidth size="small" sx={{ mb: 1 }}>
                <InputLabel>聚合</InputLabel>
                <Select value={aggregation} label="聚合" onChange={(e) => setAggregation(e.target.value as string)}>
                  <MenuItem value="raw">raw</MenuItem>
                  <MenuItem value="mean">mean</MenuItem>
                  <MenuItem value="sum">sum</MenuItem>
                  <MenuItem value="max">max</MenuItem>
                  <MenuItem value="min">min</MenuItem>
                </Select>
              </FormControl>
              <FormControl fullWidth size="small" sx={{ mb: 1 }}>
                <InputLabel>底图</InputLabel>
                <Select value={basemap} label="底图" onChange={(e) => setBasemap(e.target.value as PlotConfig["basemap"])}>
                  <MenuItem value="cartopy">cartopy 海岸线</MenuItem>
                  <MenuItem value="plain">纯网格</MenuItem>
                </Select>
              </FormControl>
              <TextField fullWidth size="small" type="number" label="动画帧数" value={frames}
                onChange={(e) => setFrames(Number(e.target.value))} sx={{ mb: 2 }} />
              <Button fullWidth variant="contained" onClick={handleRender} disabled={busy} startIcon={<ImageIcon />}>
                {busy ? "渲染中…" : "渲染"}
              </Button>
              {msg && <Alert severity="error" sx={{ mt: 1 }}>{msg}</Alert>}
            </CardContent>
          </Card>
        </Grid>
        <Grid item xs={12} md={8}>
          <Card variant="outlined">
            <CardContent>
              {artifact ? (
                <Box>
                  <Box sx={{ display: "flex", gap: 1, alignItems: "center", mb: 1 }}>
                    <Chip label={`${artifact.format} · ${(artifact.size / 1024).toFixed(1)} KB`} color="primary" size="small" />
                    <Typography variant="caption" color="text.secondary">{artifact.url}</Typography>
                  </Box>
                  {artifact.format === "gif" ? (
                    <img src={artifact.url} alt="animation" style={{ maxWidth: "100%", borderRadius: 8 }} />
                  ) : (
                    <img src={artifact.url} alt="plot" style={{ maxWidth: "100%", borderRadius: 8 }} />
                  )}
                </Box>
              ) : (
                <Box sx={{ display: "flex", alignItems: "center", justifyContent: "center", minHeight: 320, color: "text.disabled" }}>
                  <Typography>选择参数后点击「渲染」</Typography>
                </Box>
              )}
            </CardContent>
          </Card>
        </Grid>
      </Grid>
    </Box>
  );
}
