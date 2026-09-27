import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Checkbox,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogContentText,
  DialogTitle,
  FormControlLabel,
  IconButton,
  LinearProgress,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Typography,
} from "@mui/material";
import RefreshIcon from "@mui/icons-material/Refresh";
import DeleteIcon from "@mui/icons-material/Delete";
import DeleteSweepIcon from "@mui/icons-material/DeleteSweep";
import PlayArrowIcon from "@mui/icons-material/PlayArrow";
import ReplayIcon from "@mui/icons-material/Replay";
import StopIcon from "@mui/icons-material/Stop";
import { useEffect, useState } from "react";
import api from "../api/client";
import { useAppStore } from "../store/appStore";
import type { Task } from "../types";

const STATUS_COLOR: Record<string, "success" | "error" | "warning" | "default" | "info"> = {
  success: "success",
  failed: "error",
  running: "info",
  pending: "warning",
  paused: "warning",
};

export default function TasksPage() {
  const tasks = useAppStore((s) => s.tasks);
  const tasksTotal = useAppStore((s) => s.tasksTotal);
  const setTasks = useAppStore((s) => s.setTasks);
  const upsertTask = useAppStore((s) => s.upsertTask);
  const removeTask = useAppStore((s) => s.removeTask);
  const [loading, setLoading] = useState(false);
  const [msg, setMsg] = useState("");
  // 一键清空二次确认对话框
  const [deleteAllOpen, setDeleteAllOpen] = useState(false);
  const [deleteAllFiles, setDeleteAllFiles] = useState(true);
  const [deletingAll, setDeletingAll] = useState(false);

  // 运行中任务数：清空时由后端保留（Y 仅统计 running；pending 会一并删除）
  const runningCount = tasks.filter((t) => t.status === "running").length;

  const refresh = () => {
    setLoading(true);
    api
      .downloadList("", 1, 50)
      .then((r) => setTasks(r.tasks, r.total))
      .catch((e) => setMsg((e as Error).message))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    refresh();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const handleCancel = async (t: Task) => {
    try {
      const r = await api.downloadCancel(t.id);
      setMsg(`已请求取消 ${r.task_id}`);
      refresh();
    } catch (e) {
      setMsg((e as Error).message);
    }
  };

  const handleResume = async (t: Task) => {
    try {
      await api.downloadResume(t.id);
      setMsg(`已请求续传 ${t.id}`);
      refresh();
    } catch (e) {
      setMsg((e as Error).message);
    }
  };

  // 一键补漏：只重下失败块（部分成功时比"续传"语义更明确）
  const handleRetryFailed = async (t: Task) => {
    try {
      const r = await api.downloadRetryFailed(t.id);
      setMsg(`已启动补漏：${r.task_id}（仅重下失败块）`);
      refresh();
    } catch (e) {
      setMsg((e as Error).message);
    }
  };

  const handleDelete = async (t: Task) => {
    try {
      await api.downloadDelete(t.id, true);
      removeTask(t.id);
      setMsg(`已删除 ${t.id}`);
    } catch (e) {
      setMsg((e as Error).message);
    }
  };

  const handleDeleteAll = async () => {
    setDeletingAll(true);
    try {
      const r = await api.downloadDeleteAll(deleteAllFiles);
      let text = `已删除 ${r.deleted} 个任务`;
      if (r.skipped_running > 0) {
        text += `，保留 ${r.skipped_running} 个运行中任务`;
      }
      setMsg(text);
      setDeleteAllOpen(false);
      refresh();
    } catch (e) {
      setMsg((e as Error).message);
      setDeleteAllOpen(false);
    } finally {
      setDeletingAll(false);
    }
  };

  return (
    <Box>
      <Box sx={{ display: "flex", alignItems: "center", gap: 1, mb: 2 }}>
        <Typography variant="h5" sx={{ fontWeight: 700 }}>任务</Typography>
        <Chip label={`共 ${tasksTotal}`} size="small" />
        <IconButton onClick={refresh} disabled={loading}>
          {loading ? <CircularProgress size={20} /> : <RefreshIcon />}
        </IconButton>
        <Box sx={{ flexGrow: 1 }} />
        <Button
          variant="outlined"
          color="error"
          size="small"
          startIcon={<DeleteSweepIcon />}
          disabled={tasksTotal === 0 || loading}
          onClick={() => setDeleteAllOpen(true)}
        >
          清空全部任务
        </Button>
      </Box>
      {msg && <Alert severity="info" sx={{ mb: 2 }} onClose={() => setMsg("")}>{msg}</Alert>}

      <Card variant="outlined">
        <CardContent sx={{ p: 0 }}>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>任务 ID</TableCell>
                <TableCell>状态</TableCell>
                <TableCell sx={{ width: 240 }}>进度</TableCell>
                <TableCell>块 (done/failed/total)</TableCell>
                <TableCell>错误</TableCell>
                <TableCell align="right">操作</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {tasks.length === 0 && (
                <TableRow>
                  <TableCell colSpan={6} sx={{ textAlign: "center", color: "text.secondary" }}>
                    暂无任务
                  </TableCell>
                </TableRow>
              )}
              {tasks.map((t) => (
                <TableRow key={t.id}>
                  <TableCell>{t.id}</TableCell>
                  <TableCell>
                    <Chip label={t.status} color={STATUS_COLOR[t.status] ?? "default"} size="small" />
                  </TableCell>
                  <TableCell>
                    <LinearProgress
                      variant={t.status === "running" && t.progress <= 0 ? "indeterminate" : "determinate"}
                      value={t.progress * 100}
                      sx={{ height: 8, borderRadius: 2 }}
                    />
                    <Typography variant="caption">{Math.round(t.progress * 100)}%</Typography>
                  </TableCell>
                  <TableCell>
                    {t.block_stats.done}/{t.block_stats.failed}/{t.block_stats.total}
                  </TableCell>
                  <TableCell>
                    {t.error ? <Typography variant="caption" color="error">{t.error.message}</Typography> : "—"}
                  </TableCell>
                  <TableCell align="right">
                    {t.status === "running" || t.status === "pending" ? (
                      <IconButton size="small" title="取消" onClick={() => handleCancel(t)}>
                        <StopIcon fontSize="small" />
                      </IconButton>
                    ) : null}
                    {t.status === "paused" ? (
                      <IconButton size="small" title="续传" onClick={() => handleResume(t)}>
                        <PlayArrowIcon fontSize="small" />
                      </IconButton>
                    ) : null}
                    {t.status === "failed" ? (
                      <IconButton
                        size="small"
                        title="补漏：仅重下失败块"
                        onClick={() => handleRetryFailed(t)}
                      >
                        <ReplayIcon fontSize="small" />
                      </IconButton>
                    ) : null}
                    <IconButton size="small" title="删除" onClick={() => handleDelete(t)}>
                      <DeleteIcon fontSize="small" />
                    </IconButton>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      {tasks.some((t) => t.status === "running") && (
        <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1 }}>
          实时进度经 WebSocket 推送；断线重连后自动从任务列表补状态。
        </Typography>
      )}

      {/* 一键清空全部任务：二次确认（运行中任务保留、不可撤销） */}
      <Dialog
        open={deleteAllOpen}
        onClose={() => { if (!deletingAll) setDeleteAllOpen(false); }}
      >
        <DialogTitle>清空全部任务</DialogTitle>
        <DialogContent>
          <DialogContentText>
            将删除全部任务（当前共 {tasksTotal} 个任务；其中 {runningCount} 个
            运行中任务将被保留，不会删除）。
          </DialogContentText>
          <DialogContentText sx={{ mt: 1, color: "error.main" }}>
            此操作不可撤销。
          </DialogContentText>
          <FormControlLabel
            sx={{ mt: 1 }}
            control={
              <Checkbox
                checked={deleteAllFiles}
                onChange={(e) => setDeleteAllFiles(e.target.checked)}
              />
            }
            label="同时删除已下载的缓存文件（.nc）"
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDeleteAllOpen(false)} disabled={deletingAll}>
            取消
          </Button>
          <Button color="error" onClick={handleDeleteAll} disabled={deletingAll}>
            {deletingAll ? "删除中…" : "确认清空"}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
