import {
  Alert,
  Box,
  Button,
  Chip,
  FormControl,
  Grid,
  InputLabel,
  MenuItem,
  Select,
  TextField,
  Typography,
} from "@mui/material";
import SendIcon from "@mui/icons-material/Send";
import DownloadIcon from "@mui/icons-material/Download";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import api from "../api/client";
import { useAppStore } from "../store/appStore";
import type { NlParseResult, RequestSchema } from "../types";

const EMPTY_SCHEMA: RequestSchema = {
  dataset: "reanalysis-era5-single-levels",
  dataset_family: "era5-single",
  variables: ["2m_temperature"],
  pressure_levels: null,
  timerange: { start: "2020-06-01", end: "2020-06-30" },
  area: { west: 118, south: 29, east: 123, north: 34 },
  frequency: "hourly",
  aggregation: "raw",
  confidence: 0.8,
};

export default function ChatPage() {
  const navigate = useNavigate();
  const [text, setText] = useState("下载2020年6月长三角地表温度");
  const [result, setResult] = useState<NlParseResult | null>(null);
  const [schema, setSchema] = useState<RequestSchema>(EMPTY_SCHEMA);
  const [answers, setAnswers] = useState<Record<string, unknown>>({});
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [submittedTask, setSubmittedTask] = useState<string | null>(null);

  const handleParse = async () => {
    if (!text.trim()) return;
    setBusy(true);
    setMsg("");
    setSubmittedTask(null);
    try {
      const r = await api.nlParse(text);
      setResult(r);
      if (r.request_schema) {
        setSchema({ ...EMPTY_SCHEMA, ...r.request_schema });
      }
    } catch (e) {
      setMsg(`解析失败: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const handleClarify = async () => {
    if (!result?.session_id) return;
    setBusy(true);
    setMsg("");
    try {
      const r = await api.nlClarify(result.session_id, answers);
      setResult(r);
      if (r.request_schema) setSchema({ ...EMPTY_SCHEMA, ...r.request_schema });
    } catch (e) {
      setMsg(`补参失败: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const handleSubmit = async () => {
    setBusy(true);
    setMsg("");
    try {
      const r = await api.downloadSubmit(schema);
      setSubmittedTask(r.task_id);
      setMsg(`下载任务已提交: ${r.task_id}`);
    } catch (e) {
      setMsg(`提交失败: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const update = (patch: Partial<RequestSchema>) =>
    setSchema((s) => ({ ...s, ...patch }));

  return (
    <Box>
      <Typography variant="h5" sx={{ mb: 2, fontWeight: 700 }}>
        自然语言对话
      </Typography>
      <Alert severity="info" sx={{ mb: 2 }}>
        示例：下载最近五年长三角五六月地表温度 / 华北平原2023年1月土壤湿度，按月平均 /
        Get 2m temperature for the Yangtze River Delta, June 2020
      </Alert>

      <Box sx={{ display: "flex", gap: 1, mb: 2 }}>
        <TextField
          fullWidth
          multiline
          minRows={2}
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="用中文或英文描述你的数据需求…"
        />
        <Button variant="contained" onClick={handleParse} disabled={busy || !text.trim()} startIcon={<SendIcon />}>
          解析
        </Button>
      </Box>

      {msg && <Alert severity={submittedTask ? "success" : "info"} sx={{ mb: 2 }}>{msg}</Alert>}

      {result?.need_info && (
        <Alert severity="warning" sx={{ mb: 2 }}>
          <Typography variant="subtitle2" sx={{ mb: 1 }}>需要补充信息</Typography>
          {result.need_info.questions.map((q) => (
            <Typography key={q} variant="body2">· {q}</Typography>
          ))}
        </Alert>
      )}

      {result && (
        <Box sx={{ mb: 2 }}>
          <Chip
            label={`引擎: ${result.engine}${result.confirm ? " · 低置信需确认" : ""}`}
            color={result.confirm ? "warning" : "success"}
            size="small"
            sx={{ mb: 1 }}
          />
        </Box>
      )}

      {schema && (
        <Grid container spacing={2}>
          <Grid item xs={12} md={6}>
            <TextField
              fullWidth label="变量（逗号分隔）" value={schema.variables.join(", ")}
              onChange={(e) => update({ variables: e.target.value.split(/[,，]/).map((s) => s.trim()).filter(Boolean) })}
            />
          </Grid>
          <Grid item xs={12} md={6}>
            <FormControl fullWidth>
              <InputLabel>数据集家族</InputLabel>
              <Select
                value={schema.dataset_family}
                label="数据集家族"
                onChange={(e) => {
                  const fam = e.target.value as string;
                  const dataset = {
                    "era5-single": "reanalysis-era5-single-levels",
                    "era5-pressure": "reanalysis-era5-pressure-levels",
                    "era5-monthly": "reanalysis-era5-single-levels-monthly-means",
                    land: "reanalysis-era5-land",
                    "land-monthly": "reanalysis-era5-land-monthly-means",
                  }[fam] ?? "reanalysis-era5-single-levels";
                  update({ dataset_family: fam, dataset });
                }}
              >
                <MenuItem value="era5-single">ERA5 单层 (0.25°)</MenuItem>
                <MenuItem value="era5-pressure">ERA5 气压层</MenuItem>
                <MenuItem value="era5-monthly">ERA5 月均</MenuItem>
                <MenuItem value="land">ERA5-Land (0.1°)</MenuItem>
                <MenuItem value="land-monthly">ERA5-Land 月均</MenuItem>
              </Select>
            </FormControl>
          </Grid>
          <Grid item xs={6} md={3}>
            <TextField fullWidth type="date" label="开始日期" value={schema.timerange.start}
              onChange={(e) => update({ timerange: { ...schema.timerange, start: e.target.value } })} />
          </Grid>
          <Grid item xs={6} md={3}>
            <TextField fullWidth type="date" label="结束日期" value={schema.timerange.end}
              onChange={(e) => update({ timerange: { ...schema.timerange, end: e.target.value } })} />
          </Grid>
          <Grid item xs={3} md={1.5}>
            <TextField fullWidth type="number" label="西" value={schema.area.west}
              onChange={(e) => update({ area: { ...schema.area, west: Number(e.target.value) } })} />
          </Grid>
          <Grid item xs={3} md={1.5}>
            <TextField fullWidth type="number" label="南" value={schema.area.south}
              onChange={(e) => update({ area: { ...schema.area, south: Number(e.target.value) } })} />
          </Grid>
          <Grid item xs={3} md={1.5}>
            <TextField fullWidth type="number" label="东" value={schema.area.east}
              onChange={(e) => update({ area: { ...schema.area, east: Number(e.target.value) } })} />
          </Grid>
          <Grid item xs={3} md={1.5}>
            <TextField fullWidth type="number" label="北" value={schema.area.north}
              onChange={(e) => update({ area: { ...schema.area, north: Number(e.target.value) } })} />
          </Grid>
          <Grid item xs={6} md={3}>
            <FormControl fullWidth>
              <InputLabel>频率</InputLabel>
              <Select value={schema.frequency} label="频率"
                onChange={(e) => update({ frequency: e.target.value as RequestSchema["frequency"] })}>
                <MenuItem value="hourly">hourly</MenuItem>
                <MenuItem value="daily">daily</MenuItem>
                <MenuItem value="monthly">monthly</MenuItem>
              </Select>
            </FormControl>
          </Grid>
          <Grid item xs={6} md={3}>
            <FormControl fullWidth>
              <InputLabel>聚合</InputLabel>
              <Select value={schema.aggregation} label="聚合"
                onChange={(e) => update({ aggregation: e.target.value as RequestSchema["aggregation"] })}>
                <MenuItem value="raw">raw</MenuItem>
                <MenuItem value="mean">mean</MenuItem>
                <MenuItem value="sum">sum</MenuItem>
                <MenuItem value="max">max</MenuItem>
                <MenuItem value="min">min</MenuItem>
              </Select>
            </FormControl>
          </Grid>
        </Grid>
      )}

      <Box sx={{ display: "flex", gap: 1, mt: 2 }}>
        {result?.need_info && (
          <Button variant="contained" onClick={handleClarify} disabled={busy}>
            提交补参
          </Button>
        )}
        {result?.request_schema && (
          <Button variant="contained" color="success" onClick={handleSubmit} disabled={busy}
            startIcon={<DownloadIcon />}>
            提交下载
          </Button>
        )}
        {submittedTask && (
          <Button variant="outlined" onClick={() => navigate("/tasks")}>
            查看任务
          </Button>
        )}
      </Box>
    </Box>
  );
}
