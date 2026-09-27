# -*- coding: utf-8 -*-
"""断点续传：manifest.json + 每块 .done 标记（design-final.md §3.3，E5 验证模式）。

- manifest.json 记录每块状态（done/failed/missing）。
- 每块 .done 硬标记（key 含变量/年/月子路径，写入前确保父目录）。
- resume 时跳过 done 块，重下 failed/missing 块。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

from era5tool.config.settings import Settings


def ensure_dir(path: str | Path) -> str:
    os.makedirs(path, exist_ok=True)
    return str(path)


class ResumableStore:
    """单个任务的断点续传存储。"""

    def __init__(self, task_dir: str | Path, settings: Settings):
        self.task_dir = Path(task_dir)
        self.manifest_path = self.task_dir / "manifest.json"

    # ------------------------------------------------------------------
    def load(self) -> Dict[str, Dict[str, Any]]:
        if self.manifest_path.is_file():
            try:
                with open(self.manifest_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def save(self, manifest: Dict[str, Dict[str, Any]]) -> None:
        ensure_dir(self.task_dir)
        tmp = self.manifest_path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.manifest_path)

    # ------------------------------------------------------------------
    def marker_path(self, key: str) -> Path:
        """块 .done 标记路径（key 含变量/年/月子路径，父目录需确保存在）。"""
        return self.task_dir / f"{key}.done"

    def mark_done(self, key: str) -> None:
        """写 .done 硬标记（worker 进程内调用；manifest 由编排层维护，避免并发覆盖）。"""
        ensure_dir(self.marker_path(key).parent)
        with open(self.marker_path(key), "w", encoding="utf-8") as f:
            f.write("done")

    def is_done(self, key: str) -> bool:
        """仅当 .done 标记内容为 "done" 才算完成。

        mark_failed 写的是同一路径（内容 "failed: ..."），
        若只查文件存在，失败块会被误判为 done 而跳过（P1-1 数据完整性缺陷）。
        """
        p = self.marker_path(key)
        if not p.is_file():
            return False
        try:
            return p.read_text(encoding="utf-8").strip() == "done"
        except OSError:
            return False

    def mark_failed(self, key: str, err: str) -> None:
        ensure_dir(self.marker_path(key).parent)
        with open(self.marker_path(key), "w", encoding="utf-8") as f:
            f.write(f"failed: {err}")

    # ------------------------------------------------------------------
    def clear_failed(self) -> int:
        """清除所有"失败"标记（供「一键补漏」重下失败块），返回清除数量。

        背景（bugfix download-gaps）：`mark_failed` 与 `mark_done` 写的是同一路径
        （内容分别为 "failed: ..." / "done"），`is_done` 只认 "done"，故失败块本来
        就会在 resume 时被重下。但当任务因其它原因（如后端重启）残留失败标记时，
        显式清一遍能让"补漏"语义明确、可计数、可观测。

        只删内容 != "done" 的标记文件（即失败标记），绝不删真正的 .done 标记 →
        已成功下载的块不会被重复下载。
        """
        manifest = self.load()
        for key, info in list(manifest.items()):
            if (info or {}).get("status") == "failed":
                manifest.pop(key, None)
        cleared = 0
        if self.task_dir.is_dir():
            for p in self.task_dir.rglob("*.done"):
                try:
                    if p.read_text(encoding="utf-8").strip() == "done":
                        continue
                    p.unlink()
                    cleared += 1
                except OSError:  # pragma: no cover - 文件被并发删除等
                    continue
        self.save(manifest)
        return cleared

    # ------------------------------------------------------------------
    def pending_blocks(self, blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """过滤出待处理块（未 done），并同步 manifest。"""
        manifest = self.load()
        pending: List[Dict[str, Any]] = []
        for b in blocks:
            key = b["key"]
            if self.is_done(key):
                manifest[key] = {"status": "done"}
            else:
                pending.append(b)
        self.save(manifest)
        return pending
