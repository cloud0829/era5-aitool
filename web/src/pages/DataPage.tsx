// 数据管理页（design-data-manager.md §3.3 / §4）
//
// 容器职责：持有 files/loading/query/sortDir/selected/detail/dialog/snackbar；
// 派生 filtered = query 过滤 + size 排序（默认降序）；详情与删除回调在此实现。
import { useEffect, useMemo, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  IconButton,
  InputAdornment,
  Snackbar,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import DeleteSweepIcon from "@mui/icons-material/DeleteSweep";
import RefreshIcon from "@mui/icons-material/Refresh";
import SearchIcon from "@mui/icons-material/Search";
import StorageIcon from "@mui/icons-material/Storage";
import api from "../api/client";
import type { CacheFile } from "../types";
import FileTable, { type SortDir } from "../components/data/FileTable";
import FileDetailDrawer from "../components/data/FileDetailDrawer";
import DeleteFileDialog, { type DeleteMode } from "../components/data/DeleteFileDialog";
import { formatBytes, isBusy } from "../utils/format";

interface SnackState {
  severity: "success" | "error" | "warning" | "info";
  message: string;
}

export default function DataPage() {
  const [files, setFiles] = useState<CacheFile[]>([]);
  const [loading, setLoading] = useState(false);
  const [query, setQuery] = useState("");
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  const [selected, setSelected] = useState<string[]>([]);
  const [detail, setDetail] = useState<CacheFile | null>(null);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleteMode, setDeleteMode] = useState<DeleteMode>("single");
  const [deleteEntries, setDeleteEntries] = useState<CacheFile[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [snack, setSnack] = useState<SnackState | null>(null);
  const [snackKey, setSnackKey] = useState(0);

  const refresh = () => {
    setLoading(true);
    api
      .dataFiles()
      .then((r) => {
        setFiles(r.files);
        // 刷新后清除已消失的勾选（文件可能已被外部删除）
        setSelected((prev) => {
          const alive = new Set(r.files.map((f) => f.rel_path));
          return prev.filter((p) => alive.has(p));
        });
      })
      .catch((e) => showSnack("error", (e as Error).message))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    refresh();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // 本地过滤（数据集/变量/rel_path 包含，不区分大小写）+ size 排序（默认降序）
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const rows = q
      ? files.filter(
          (f) =>
            f.dataset.toLowerCase().includes(q) ||
            f.variable.toLowerCase().includes(q) ||
            f.rel_path.toLowerCase().includes(q)
        )
      : [...files];
    rows.sort((a, b) => {
      if (a.size !== b.size) {
        return sortDir === "desc" ? b.size - a.size : a.size - b.size;
      }
      return a.rel_path.localeCompare(b.rel_path);
    });
    return rows;
  }, [files, query, sortDir]);

  const selectableVisible = useMemo(
    () => filtered.filter((f) => !isBusy(f)).map((f) => f.rel_path),
    [filtered]
  );
  const allSelectableSelected =
    selectableVisible.length > 0 &&
    selectableVisible.every((p) => selected.includes(p));

  const showSnack = (severity: SnackState["severity"], message: string) => {
    setSnackKey((k) => k + 1);
    setSnack({ severity, message });
  };

  const handleToggle = (relPath: string) => {
    setSelected((prev) =>
      prev.includes(relPath)
        ? prev.filter((p) => p !== relPath)
        : [...prev, relPath]
    );
  };

  const handleToggleAll = () => {
    if (allSelectableSelected) {
      setSelected((prev) => prev.filter((p) => !selectableVisible.includes(p)));
    } else {
      setSelected((prev) => Array.from(new Set([...prev, ...selectableVisible])));
    }
  };

  const openSingleDelete = (row: CacheFile) => {
    setDeleteMode("single");
    setDeleteEntries([row]);
    setDeleteOpen(true);
  };

  const openBatchDelete = () => {
    const rows = files.filter((f) => selected.includes(f.rel_path) && !isBusy(f));
    if (rows.length === 0) {
      showSnack("info", "请先勾选要删除的文件");
      return;
    }
    setDeleteMode("batch");
    setDeleteEntries(rows);
    setDeleteOpen(true);
  };

  const handleConfirmDelete = async () => {
    if (submitting) {
      return;
    }
    setSubmitting(true);
    const paths = deleteEntries.map((e) => e.rel_path);
    try {
      if (deleteMode === "single") {
        const r = await api.dataDeleteFile(paths[0]);
        showSnack("success", `已删除，释放 ${formatBytes(r.released_bytes)}`);
      } else {
        const r = await api.dataDeleteFiles(paths);
        const parts = [`删除 ${r.deleted} 个成功`];
        if (r.busy > 0) {
          parts.push(`${r.busy} 个被占用`);
        }
        if (r.failed > 0) {
          parts.push(`${r.failed} 个失败`);
        }
        if (r.released_bytes > 0) {
          parts.push(`释放约 ${formatBytes(r.released_bytes)}`);
        }
        showSnack(r.deleted > 0 ? "success" : "warning", parts.join("，"));
      }
    } catch (e) {
      // 单删后端 6001/6002/6003 由拦截器转 Error(message)，明确提示 + 刷新
      showSnack("error", (e as Error).message || "删除失败");
    } finally {
      setSubmitting(false);
      setDeleteOpen(false);
      setSelected([]);
      refresh();
    }
  };

  const selectedReadyCount = files.filter(
    (f) => selected.includes(f.rel_path) && !isBusy(f)
  ).length;

  return (
    <Box>
      <Box sx={{ display: "flex", alignItems: "center", gap: 1, mb: 2, flexWrap: "wrap" }}>
        <StorageIcon color="primary" />
        <Typography variant="h5" sx={{ fontWeight: 700 }}>
          数据
        </Typography>
        <Chip label={`共 ${files.length} 个文件`} size="small" variant="outlined" />
        <Tooltip title="刷新列表">
          <span>
            <IconButton onClick={refresh} disabled={loading}>
              {loading ? <CircularProgress size={20} /> : <RefreshIcon />}
            </IconButton>
          </span>
        </Tooltip>
        <Box sx={{ flexGrow: 1 }} />
        <TextField
          size="small"
          placeholder="搜索数据集 / 变量 / 路径…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          sx={{ width: 300 }}
          InputProps={{
            startAdornment: (
              <InputAdornment position="start">
                <SearchIcon fontSize="small" />
              </InputAdornment>
            ),
          }}
        />
        <Tooltip title={selectedReadyCount === 0 ? "请先勾选要删除的文件" : `删除选中的 ${selectedReadyCount} 个文件`}>
          <span>
            <Button
              variant="contained"
              color="error"
              startIcon={<DeleteSweepIcon />}
              disabled={selectedReadyCount === 0}
              onClick={openBatchDelete}
            >
              批量删除{selectedReadyCount > 0 ? `(${selectedReadyCount})` : ""}
            </Button>
          </span>
        </Tooltip>
      </Box>

      <FileTable
        rows={filtered}
        loading={loading}
        sortDir={sortDir}
        selected={selected}
        onSort={setSortDir}
        onToggle={handleToggle}
        onToggleAll={handleToggleAll}
        onDetail={setDetail}
        onDelete={openSingleDelete}
        emptyKind={files.length === 0 ? "none" : "search"}
      />

      <FileDetailDrawer
        open={detail !== null}
        entry={detail}
        onClose={() => setDetail(null)}
      />

      <DeleteFileDialog
        open={deleteOpen}
        mode={deleteMode}
        entries={deleteEntries}
        submitting={submitting}
        onCancel={() => setDeleteOpen(false)}
        onConfirm={handleConfirmDelete}
      />

      <Snackbar
        key={snackKey}
        open={snack !== null}
        autoHideDuration={4000}
        anchorOrigin={{ vertical: "bottom", horizontal: "center" }}
        onClose={() => setSnack(null)}
      >
        <Alert
          severity={snack?.severity ?? "info"}
          variant="filled"
          onClose={() => setSnack(null)}
          sx={{ width: "100%" }}
        >
          {snack?.message}
        </Alert>
      </Snackbar>
    </Box>
  );
}
