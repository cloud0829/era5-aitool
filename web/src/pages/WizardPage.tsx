import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  Step,
  StepContent,
  StepLabel,
  Stepper,
  TextField,
  Typography,
} from "@mui/material";
import VerifiedUserIcon from "@mui/icons-material/VerifiedUser";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import api from "../api/client";
import { useAppStore } from "../store/appStore";
import type { AccountStatus } from "../types";

// CDS 注册引导页（design-final.md §8.3：点击"申请账号"应打开注册页 + 说明）
const CDS_REGISTER_URL = "https://cds.climate.copernicus.eu/";

export default function WizardPage() {
  const navigate = useNavigate();
  const account = useAppStore((s) => s.account);
  const setAccount = useAppStore((s) => s.setAccount);

  const [apiKey, setApiKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");

  const state = account?.state ?? "INIT";
  const stepIndex =
    state === "INIT" ? 0
    : state === "GUIDE_REGISTER" ? 1
    : state === "WAIT_USER" || state === "VALIDATING" ? 2
    : state === "READY" ? 3
    : 2;

  const handleStart = () => {
    // 必须在用户手势内同步打开新标签页，否则会被浏览器弹窗拦截（导致"无反应"）
    window.open(CDS_REGISTER_URL, "_blank", "noopener,noreferrer");
    // 进入"等待用户注册"步骤，展示注册说明与 API Key 表单（design-final.md §3.5 / §8.3）
    setAccount({ state: "WAIT_USER", has_key: false });
  };

  const handleValidate = async () => {
    setBusy(true);
    setMsg("");
    try {
      const r = await api.accountValidate(apiKey);
      if (r.valid) {
        setMsg("校验通过，正在写入凭据…");
        const s = await api.accountFinalize(apiKey);
        setAccount(s);
        setMsg("已就绪！凭据已写入 ~/.cdsapirc 与系统钥匙串。");
      } else {
        setMsg(`校验失败：${r.error ?? "未知错误"}`);
      }
    } catch (e) {
      setMsg((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const handleClear = async () => {
    try {
      const s = await api.accountClear();
      setAccount(s);
      setApiKey("");
      setMsg("凭据已清除");
    } catch (e) {
      setMsg((e as Error).message);
    }
  };

  const handleTest = async () => {
    try {
      const r = await api.accountTestDownload();
      setMsg(`已提交最小探测下载任务: ${r.task_id}`);
      navigate("/tasks");
    } catch (e) {
      setMsg((e as Error).message);
    }
  };

  return (
    <Box>
      <Typography variant="h5" sx={{ mb: 2, fontWeight: 700 }}>账号向导</Typography>
      <Alert severity="info" sx={{ mb: 2 }}>
        按步骤申请 CDS 账号并配置 API Key。凭据只写入 <code>~/.cdsapirc</code>（权限 600）与系统钥匙串，绝不入库。
      </Alert>

      <Card variant="outlined" sx={{ mb: 2 }}>
        <CardContent>
          <Stepper activeStep={stepIndex} orientation="vertical">
            <Step>
              <StepLabel>开始</StepLabel>
              <StepContent>
                <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
                  当前状态：{state}
                </Typography>
                {state === "INIT" && (
                  <Button variant="contained" onClick={handleStart} startIcon={<VerifiedUserIcon />}>
                    申请账号（打开注册引导）
                  </Button>
                )}
              </StepContent>
            </Step>
            <Step>
              <StepLabel>在 CDS 官网注册</StepLabel>
              <StepContent>
                <Typography variant="body2" color="text.secondary">
                  打开 https://cds.climate.copernicus.eu 完成注册，并在个人页复制 API Key（完整凭据，可整串粘贴）。
                </Typography>
              </StepContent>
            </Step>
            <Step>
              <StepLabel>粘贴 API Key</StepLabel>
              <StepContent>
                <TextField fullWidth label="API Key" type="password" value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)} sx={{ mb: 2 }} />
                <Box sx={{ display: "flex", gap: 1 }}>
                  <Button variant="contained" onClick={handleValidate} disabled={busy || !apiKey}>
                    {busy ? "校验中…" : "校验并保存"}
                  </Button>
                </Box>
              </StepContent>
            </Step>
            <Step>
              <StepLabel>就绪</StepLabel>
              <StepContent>
                <Typography variant="body2" color="text.secondary">
                  账号已就绪。可执行最小探测下载验证。
                </Typography>
                <Box sx={{ display: "flex", gap: 1, mt: 1 }}>
                  <Button variant="contained" color="success" onClick={handleTest}>最小探测下载</Button>
                  <Button variant="outlined" color="error" onClick={handleClear}>清除凭据</Button>
                </Box>
              </StepContent>
            </Step>
          </Stepper>
          {account?.has_key && (
            <Box sx={{ mt: 1 }}>
              <Chip label="已配置 API Key" color="success" size="small" />
            </Box>
          )}
          {msg && <Alert severity="info" sx={{ mt: 2 }} onClose={() => setMsg("")}>{msg}</Alert>}
        </CardContent>
      </Card>
    </Box>
  );
}
