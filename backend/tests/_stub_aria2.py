# -*- coding: utf-8 -*-
"""测试用假 aria2c（design-speedup-download.md §6.3）。

通过环境变量 `ERA5_ARIA2_CMD="python <path>/_stub_aria2.py"` 注入；
ProcessPoolExecutor(spawn) 子进程**继承**父进程 env，故能穿透进程边界
（monkeypatch 无法穿透，这是测试 aria2 分支的唯一可靠注入方式）。

职责：
- 支持 `--version`：probe_aria2 探测阶段需要返回版本号（命中后 available=True）。
- 解析 `--dir=` 与 `-o`，在目标路径写出**确定大小**的文件，模拟多连接下载落盘。
- 环境变量模拟异常（进程内读取，stub 子进程继承 env）：
    STUB_ARIA2_FAIL=1        → returncode != 0（aria2 自身失败 → 应降级）
    STUB_ARIA2_WRONG_SIZE=1  → 写出与 content_length 不符的文件（校验失败 → 应降级）

STUB_FILE_SIZE 必须与 `backend/era5tool/acquisition/mock_client.py` 中
`FakeResult.STUB_CONTENT_LENGTH` 默认值保持一致，否则 `verify_size` 在 success
用例里判失败。
"""
import os
import sys


# 必须与 mock_client.FakeResult.STUB_CONTENT_LENGTH 保持一致
STUB_FILE_SIZE = 1024


def _parse(argv):
    parent = "."
    name = "out"
    for i, a in enumerate(argv):
        if a.startswith("--dir="):
            parent = a.split("=", 1)[1]
        elif a == "-o" and i + 1 < len(argv):
            name = argv[i + 1]
    return parent, name


def main(argv):
    # 探测阶段：probe_aria2 执行 `<bin> --version`，必须返回 0 + 含版本号。
    if "--version" in argv:
        sys.stdout.write("aria2 version 1.37.0-stub\n")
        return 0
    parent, name = _parse(argv)
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError:
        pass
    target = os.path.join(parent, name)
    # 模拟 aria2 自身失败（returncode != 0）→ 调用方应降级而非判成功
    if os.environ.get("STUB_ARIA2_FAIL") == "1":
        return 1
    # 模拟落盘大小与期望不符（校验失败）→ 调用方应降级并 cleanup
    if os.environ.get("STUB_ARIA2_WRONG_SIZE") == "1":
        size = STUB_FILE_SIZE + 1
    else:
        size = STUB_FILE_SIZE
    with open(target, "wb") as f:
        f.write(b"\x00" * size)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
