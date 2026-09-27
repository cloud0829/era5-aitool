import { Alert, Box, Button, Card, CardContent, Chip, Grid, Typography } from "@mui/material";
import { useNavigate } from "react-router-dom";
import { useEffect, useState } from "react";
import api from "../api/client";
import { useAppStore } from "../store/appStore";
import type { Task } from "../types";

export default function OverviewPage() {
  const navigate = useNavigate();
  const account = useAppStore((s) => s.account);
  const config = useAppStore((s) => s.config);
  const llm = useAppStore((s) => s.llm);
  const tasks = useAppStore((s) => s.tasks);
  const setTasks = useAppStore((s) => s.setTasks);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api
      .downloadList("", 1, 5)
      .then((r) => setTasks(r.tasks, r.total))
      .catch(() => undefined)
      .finally(() => setLoading(false));
  }, [setTasks]);

  const running = tasks.filter((t) => t.status === "running" || t.status === "pending");
  const done = tasks.filter((t) => t.status === "success").length;

  const quickLinks: { title: string; desc: string; to: string; action: string }[] = [
    { title: "说一句话下载数据", desc: "用中文或英文描述数据需求，自动转 CDS 请求", to: "/chat", action: "开始对话" },
    { title: "出图", desc: "对已下载数据渲染空间图 / 时序图 / 动画", to: "/plot", action: "去出图" },
    { title: "配置账号", desc: "CDS 账号向导 + DeepSeek Key 管理", to: "/wizard", action: "去配置" },
  ];

  return (
    <Box>
      <Typography variant="h5" sx={{ mb: 2, fontWeight: 700 }}>
        总览
      </Typography>

      {account?.state !== "READY" && (
        <Alert severity="warning" sx={{ mb: 2 }} action={
          <Button color="inherit" size="small" onClick={() => navigate("/wizard")}>
            去配置
          </Button>
        }>
          尚未配置 CDS 凭据。未配置时下载任务将使用本地模拟（mock）模式。
        </Alert>
      )}
      {llm && !llm.has_key && (
        <Alert severity="info" sx={{ mb: 2 }} action={
          <Button color="inherit" size="small" onClick={() => navigate("/config")}>
            去配置
          </Button>
        }>
          未配置 DEEPSEEK_API_KEY，自然语言将使用规则兜底解析。
        </Alert>
      )}

      <Grid container spacing={2} sx={{ mb: 3 }}>
        <Grid item xs={12} md={4}>
          <Card variant="outlined"><CardContent>
            <Typography variant="subtitle2" color="text.secondary">账号状态</Typography>
            <Typography variant="h6">{account?.state ?? "加载中…"}</Typography>
          </CardContent></Card>
        </Grid>
        <Grid item xs={12} md={4}>
          <Card variant="outlined"><CardContent>
            <Typography variant="subtitle2" color="text.secondary">进行中任务</Typography>
            <Typography variant="h6">{running.length}</Typography>
          </CardContent></Card>
        </Grid>
        <Grid item xs={12} md={4}>
          <Card variant="outlined"><CardContent>
            <Typography variant="subtitle2" color="text.secondary">已完成任务</Typography>
            <Typography variant="h6">{done}{config ? ` / ${config.download.cds_max_workers} 并发` : ""}</Typography>
          </CardContent></Card>
        </Grid>
      </Grid>

      <Grid container spacing={2}>
        {quickLinks.map((q) => (
          <Grid item xs={12} md={4} key={q.to}>
            <Card variant="outlined" sx={{ height: "100%" }}>
              <CardContent>
                <Typography variant="h6">{q.title}</Typography>
                <Typography variant="body2" color="text.secondary" sx={{ my: 1, minHeight: 40 }}>
                  {q.desc}
                </Typography>
                <Button variant="contained" onClick={() => navigate(q.to)}>
                  {q.action}
                </Button>
              </CardContent>
            </Card>
          </Grid>
        ))}
      </Grid>

      <Typography variant="h6" sx={{ mt: 4, mb: 1 }}>最近任务</Typography>
      {loading ? (
        <Typography color="text.secondary">加载中…</Typography>
      ) : tasks.length === 0 ? (
        <Typography color="text.secondary">暂无任务</Typography>
      ) : (
        tasks.slice(0, 5).map((t: Task) => (
          <Chip
            key={t.id}
            label={`${t.id} · ${t.status} · ${Math.round(t.progress * 100)}%`}
            color={t.status === "success" ? "success" : t.status === "failed" ? "error" : "default"}
            sx={{ mr: 1, mb: 1 }}
          />
        ))
      )}
    </Box>
  );
}
