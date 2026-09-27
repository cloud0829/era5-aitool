// 数据管理展示工具（design-data-manager.md §8 共享约定）
import type { CacheFile } from "../types";

const SIZE_UNITS = ["B", "KB", "MB", "GB", "TB"];

/** 字节 → 易读文本（与后端 human_size 一致：1000 进制，>=1KB 保留 1 位小数）。 */
export function formatBytes(bytes: number): string {
  const n = Math.max(Number.isFinite(bytes) ? bytes : 0, 0);
  if (n < 1000) {
    return `${n} B`;
  }
  let value = n;
  let idx = 0;
  while (value >= 1000 && idx < SIZE_UNITS.length - 1) {
    value /= 1000;
    idx += 1;
  }
  return `${value.toFixed(1)} ${SIZE_UNITS[idx]}`;
}

/** 频率展示文案映射（raw 仍保留于 DTO）。 */
export function freqLabel(freq: string): string {
  switch (freq) {
    case "hourly":
      return "逐小时";
    case "monthly":
      return "月均";
    default:
      return freq || "—";
  }
}

/** 是否被下载任务占用（busy 行不可勾选/删除）。 */
export function isBusy(f: CacheFile): boolean {
  return f.status === "busy";
}

/** 勾选文件合计大小文本（供批删确认框「释放约 XX」）。 */
export function sumSelectedBytes(files: CacheFile[]): number {
  return files.reduce((acc, f) => acc + (Number.isFinite(f.size) ? f.size : 0), 0);
}
