# -*- coding: utf-8 -*-
"""aria2c 探测 / 命令构造 / 下载执行 / 结果校验（design-speedup-download.md §3.4）。

纯函数 + dataclass，零项目内依赖，可独立单测（进程内直测，不触真实 CDS）。
- 探测：explicit_path(settings.aria2_path) → env ERA5_ARIA2_CMD → shutil.which("aria2c")；
  全不可用 → Aria2Info(available=False)，全程不抛异常。
- 下载：subprocess.Popen 调 aria2c（外部二进制），Popen+轮询而非 subprocess.run，
  以便取消能秒级 kill；Windows 加 CREATE_NO_WINDOW 不弹黑窗。
- 命令支持 "python stub.py" 形式（env 注入含参数），用 shlex 拆分（posix=False 保护反斜杠）。
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

# aria2c 固定参数（design-speedup-download.md §3.4 ARIA2_BASE_ARGS）
ARIA2_BASE_ARGS = (
    "--allow-overwrite=true", "--auto-file-renaming=false",
    "--continue=true", "--max-tries=2", "--retry-wait=3",
    "--connect-timeout=15", "--timeout=60",
    "--console-log-level=warn", "--summary-interval=0",
)


@dataclass
class Aria2Info:
    """aria2c 探测结果。"""
    available: bool = False
    path: str = ""           # 命中的可执行（可能是 "python stub.py"）
    version: str = ""
    source: str = ""         # explicit | env | which | （空=未找到）


@dataclass
class Aria2Run:
    """单次 aria2c 调用结果。"""
    ok: bool = False
    returncode: int = 0
    elapsed: float = 0.0
    bytes: int = 0
    argv: List[str] = None  # type: ignore
    stderr_tail: str = ""
    cancelled: bool = False


def _split_cmd(s: str) -> List[str]:
    """拆分可能含参数的命令串（如 `"python" "C:/x/stub.py"`）。

    posix=False：保护 Windows 路径反斜杠（不被当作转义符）。
    但 posix=False 不会剥离引号，故手动剥除首尾引号，否则
    `"C:\\..\\python.exe"` 会被当成带引号字面量传给 subprocess（找不到可执行）。
    """
    if not s:
        return []
    toks = shlex.split(s, posix=False)
    out: List[str] = []
    for t in toks:
        if len(t) >= 2 and t[0] in ('"', "'") and t[-1] == t[0]:
            t = t[1:-1]
        out.append(t)
    return out


def _parse_aria2_version(text: str) -> str:
    """从 `aria2 --version` 输出提取版本号（如 "1.37.0"）。"""
    import re
    m = re.search(r"(\d+\.\d+\.\d+)", text or "")
    return m.group(1) if m else ""


def probe_aria2(explicit_path: str = "") -> Aria2Info:
    """解析顺序：explicit_path → env ERA5_ARIA2_CMD → shutil.which("aria2c")。

    命中后执行 `<bin> --version` 取版本号（失败即 available=False）。全程不抛异常。
    env 注入是测试关键：ProcessPoolExecutor(spawn) 子进程继承环境变量，
    而 monkeypatch 无法穿透进程边界。
    """
    candidates: List[Tuple[str, str]] = []
    if explicit_path:
        candidates.append(("explicit", explicit_path))
    env = os.environ.get("ERA5_ARIA2_CMD", "").strip()
    if env:
        candidates.append(("env", env))
    which = shutil.which("aria2c")
    if which:
        candidates.append(("which", which))

    for source, cmd in candidates:
        try:
            argv = _split_cmd(cmd) + ["--version"]
            proc = subprocess.run(argv, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=10)
            if proc.returncode != 0:
                continue
            out = (proc.stdout or b"").decode("utf-8", "ignore")
            return Aria2Info(available=True, path=cmd,
                             version=_parse_aria2_version(out), source=source)
        except Exception:
            # 该候选不可用（非 aria2c / 参数错误 / 超时）→ 尝试下一个
            continue
    return Aria2Info(available=False, path="", version="", source="")


def build_aria2_argv(bin_path: str, url: str, target: str,
                     connections: int = 8, timeout_s: int = 1800) -> List[str]:
    """构造 aria2c 命令行（design-speedup-download.md §3.4）。

    [bin, -x{n}, -s{n}, -k1M, --dir=<abspath(parent)>, -o <basename>,
     *ARIA2_BASE_ARGS, url]
    注意：aria2c 的 -o 是相对 --dir 的【文件名】，不能传路径（否则产物落错位置）。
    bin_path 可能含参数（"python stub.py"）→ 整体前置。
    """
    parent = os.path.dirname(os.path.abspath(target))
    name = os.path.basename(target)
    head = _split_cmd(bin_path)
    return [*head, f"-x{connections}", f"-s{connections}", "-k1M",
            f"--dir={parent}", "-o", name, *ARIA2_BASE_ARGS, url]


def _target_from_argv(argv: List[str]) -> Optional[str]:
    """从 argv 还原 aria2c 落盘的目标文件路径（--dir + -o）。"""
    parent = "."
    name = "out"
    for i, a in enumerate(argv):
        if a.startswith("--dir="):
            parent = a.split("=", 1)[1]
        elif a == "-o" and i + 1 < len(argv):
            name = argv[i + 1]
    return os.path.join(parent, name)


def run_aria2(argv: List[str], timeout_s: int,
              cancel_check: Optional[Any] = None,
              poll_interval: float = 0.5) -> Aria2Run:
    """Popen + 轮询执行 aria2c（不用 subprocess.run，否则取消会被阻塞）。

    - 每 poll_interval 检查 cancel_check() → True 则 kill，Aria2Run.cancelled=True；
    - 超 timeout_s → kill，ok=False；
    - Windows 加 creationflags=CREATE_NO_WINDOW（打包应用不弹黑窗）；
    - stderr 仅保留尾部 500 字符（与现有 error 截断风格一致，防事件撑爆）。
    """
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    start = time.time()
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, creationflags=creationflags)
    except Exception as exc:  # 启动失败（如二进制不存在）→ 直接判失败，不抛
        return Aria2Run(ok=False, returncode=-1, elapsed=0.0, bytes=0,
                        argv=argv, stderr_tail=str(exc)[:500], cancelled=False)

    cancelled = False
    while True:
        rc = proc.poll()
        if rc is not None:
            break
        if cancel_check is not None and cancel_check():
            try:
                proc.kill()
            except Exception:
                pass
            cancelled = True
            break
        if time.time() - start > timeout_s:
            try:
                proc.kill()
            except Exception:
                pass
            break
        time.sleep(poll_interval)

    # 读取 stderr（进程可能已被 kill，设短超时避免 communicate 阻塞）
    try:
        _out, err = proc.communicate(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        _out, err = b"", b""
    stderr_tail = (err or b"").decode("utf-8", "ignore")[-500:]
    elapsed = round(time.time() - start, 3)

    target = _target_from_argv(argv)
    size = 0
    if target and os.path.isfile(target):
        try:
            size = os.path.getsize(target)
        except OSError:
            size = 0
    return Aria2Run(ok=(proc.returncode == 0 and not cancelled),
                    returncode=proc.returncode or 0,
                    elapsed=elapsed, bytes=size,
                    argv=argv, stderr_tail=stderr_tail, cancelled=cancelled)


def aria2_download(url: str, target: str, bin_path: str, connections: int,
                   timeout_s: int, expected_size: Optional[int],
                   cancel_check: Optional[Any] = None) -> Aria2Run:
    """两阶段下载的 aria2 执行封装：构造 argv → run_aria2。"""
    argv = build_aria2_argv(bin_path, url, target, connections, timeout_s)
    return run_aria2(argv, timeout_s, cancel_check=cancel_check, poll_interval=0.5)


def verify_size(target: str, expected_size: Optional[int]) -> bool:
    """校验落盘文件大小（design-speedup-download.md §3.4）。

    expected_size 已知 → 必须精确相等（与 cdsapi/_check_size 同规则，
    防半截/损坏文件被误判为成功）；未知 → 仅要求文件存在且大小 > 0。
    """
    if not os.path.isfile(target):
        return False
    try:
        sz = os.path.getsize(target)
    except OSError:
        return False
    if expected_size is None:
        return sz > 0
    return sz == expected_size


def cleanup_partial(target: str) -> None:
    """删除 target 与 target + ".aria2" 控制文件（降级前清场，
    避免半截文件冒充产物被 mark_done）。"""
    for p in (target, target + ".aria2"):
        try:
            os.remove(p)
        except OSError:
            pass
