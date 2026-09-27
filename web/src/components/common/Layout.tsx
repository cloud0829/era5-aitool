import { Box } from "@mui/material";
import {
  AppBar,
  Chip,
  Drawer,
  List,
  ListItemButton,
  ListItemIcon,
  ListItemText,
  Toolbar,
  Typography,
} from "@mui/material";
import CloudQueueIcon from "@mui/icons-material/CloudQueue";
import ChatIcon from "@mui/icons-material/Chat";
import ImageIcon from "@mui/icons-material/Image";
import SettingsIcon from "@mui/icons-material/Settings";
import StorageIcon from "@mui/icons-material/Storage";
import TaskIcon from "@mui/icons-material/Task";
import VerifiedUserIcon from "@mui/icons-material/VerifiedUser";
import { ReactNode } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useAppStore } from "../../store/appStore";

const NAV = [
  { path: "/", label: "总览", icon: <CloudQueueIcon /> },
  { path: "/chat", label: "自然语言对话", icon: <ChatIcon /> },
  { path: "/plot", label: "出图", icon: <ImageIcon /> },
  { path: "/tasks", label: "任务", icon: <TaskIcon /> },
  { path: "/data", label: "数据", icon: <StorageIcon /> },
  { path: "/config", label: "配置", icon: <SettingsIcon /> },
  { path: "/wizard", label: "账号向导", icon: <VerifiedUserIcon /> },
];

export default function Layout({ children }: { children: ReactNode }) {
  const navigate = useNavigate();
  const location = useLocation();
  const account = useAppStore((s) => s.account);
  const wsConnected = useAppStore((s) => s.wsConnected);

  return (
    <Box sx={{ display: "flex", height: "100vh" }}>
      <Drawer
        variant="permanent"
        sx={{
          width: 220,
          flexShrink: 0,
          "& .MuiDrawer-paper": { width: 220, boxSizing: "border-box", bgcolor: "#f8fafc" },
        }}
      >
        <Toolbar sx={{ px: 2 }}>
          <Typography variant="h6" sx={{ fontWeight: 700, color: "#173ea8" }}>
            ERA5-AItool
          </Typography>
        </Toolbar>
        <List>
          {NAV.map((item) => (
            <ListItemButton
              key={item.path}
              selected={location.pathname === item.path}
              onClick={() => navigate(item.path)}
            >
              <ListItemIcon>{item.icon}</ListItemIcon>
              <ListItemText primary={item.label} />
            </ListItemButton>
          ))}
        </List>
      </Drawer>
      <Box sx={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0 }}>
        <AppBar position="static" color="inherit" elevation={0} sx={{ borderBottom: 1, borderColor: "divider" }}>
          <Toolbar sx={{ gap: 1 }}>
            <Typography sx={{ flex: 1, fontWeight: 600 }}>
              ERA5 / ERA5-Land 自然语言下载 + 出图工具
            </Typography>
            <Chip
              size="small"
              label={wsConnected ? "WS 已连接" : "WS 重连中…"}
              color={wsConnected ? "success" : "warning"}
              variant="outlined"
            />
            <Chip
              size="small"
              label={account?.state === "READY" ? "CDS 已配置" : "CDS 未配置"}
              color={account?.state === "READY" ? "success" : "error"}
              variant="outlined"
            />
          </Toolbar>
        </AppBar>
        <Box component="main" sx={{ flex: 1, overflow: "auto", p: 3 }}>
          {children}
        </Box>
      </Box>
    </Box>
  );
}
