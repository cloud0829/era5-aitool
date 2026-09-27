# -*- coding: utf-8 -*-
"""任务编排与调度（design-final.md §3.3/§8.1/§8.2，E5 状态机模式落地）。

- submit → 归一化 → 切块 → 后台线程跑 CDS 通道（并发≤4 + 退避 + 断点续传）。
- 状态机：pending → running → success/failed/paused；paused/failed → running(resume)。
- 进度/状态事件经 TaskEventBus → WS 广播 + events.jsonl 持久化。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

from era5tool.acquisition.cds_channel import (RETRYABLE_CATEGORIES,
                                             THROTTLE_CATEGORIES, CdsChannel,
                                             classify_error)
from era5tool.config.schema import (ERR_NO_CDS, ERR_PARAM, ERR_TASK_NOT_FOUND,
                                    ERR_TASK_STATE, ApiError, RequestSchema)
from era5tool.config.settings import Settings
from era5tool.core.events import EventBroker, TaskEventBus
from era5tool.core.normalizer import CdsRequest, Normalizer, interleave_blocks
from era5tool.core.resumable import ResumableStore, ensure_dir
from era5tool.core.task_store import TaskStore
from era5tool.models.task import Task, TaskStatus, TaskType

# task.error["failed_blocks"] 最多保留多少个块 key（day 粒度可达数千块，
# 全量塞进 task.json 会让响应体/落盘膨胀，补漏只需要知道"有哪些"的规模与样本）。
FAILED_BLOCKS_CAP = 200

# 任务结局（task.error["outcome"] / task.result["outcome"]）：
#   全部成功 / 部分成功（有失败但已有部分数据）/ 全部失败
OUTCOME_ALL_SUCCESS = "all_success"
OUTCOME_PARTIAL_SUCCESS = "partial_success"
OUTCOME_ALL_FAILED = "all_failed"


def summarize_failures(failed: List[Dict[str, Any]],
                       sample_cap: int = 5) -> Dict[str, Any]:
    """失败原因分类汇总（可观测：不再只留一句"任务失败"）。

    返回：
      total      失败块总数
      categories {类别: 数量}（类别人 → queue_limited / rate_limit / server_error /
                             network / no_data / auth / bad_request / not_found / ...）
      throttled  因撞 CDS 限流/队列墙而失败的块数（可重试、有望补漏）
      retryable  分类上属于瞬时、重试有机会成功的块数
      permanent  永久性失败（MarsNoData / 401 / 403 / 404 / 本地 IO）块数
      samples    各类别的代表块（block + category + error，最多 sample_cap 条）
    """
    cats: Dict[str, int] = {}
    for r in failed:
        cat = r.get("error_category") or classify_error(
            Exception(str(r.get("error") or "")))
        cats[cat] = cats.get(cat, 0) + 1
    samples: List[Dict[str, Any]] = []
    seen: set = set()
    for r in failed:
        cat = r.get("error_category") or classify_error(
            Exception(str(r.get("error") or "")))
        if cat in seen:
            continue
        seen.add(cat)
        samples.append({"block": r.get("block"), "category": cat,
                        "error": str(r.get("error") or "")[:200]})
        if len(samples) >= sample_cap:
            break
    throttled = sum(n for c, n in cats.items() if c in THROTTLE_CATEGORIES)
    retryable = sum(n for c, n in cats.items() if c in RETRYABLE_CATEGORIES)
    return {
        "total": len(failed),
        "categories": cats,
        "throttled": throttled,
        "retryable": retryable,
        "permanent": len(failed) - retryable,
        "samples": samples,
    }


def failed_task_error(failed: List[Dict[str, Any]]) -> Dict[str, str]:
    """按失败块的 retried/error 生成任务失败错误信息（供 _run_download 汇总）。

    - 存在 retried=True 的失败块 → 重试耗尽（BUSY_AFTER_RETRIES）。
    - 全部失败块都 retried=False → 不可重试失败（DOWNLOAD_FAILED），附首个原因。
    - 首个失败块若带 `user_message`（用户友好的中文诊断，如 MarsNoDataError 提示），
      优先采用它拼接进 message，否则沿用原始 error 文本（降级）。不改变默认文案形态。
    """
    if any(r.get("retried") for r in failed):
        return {
            "code": "BUSY_AFTER_RETRIES",
            "message": "存在块重试耗尽",
            "event_message": f"任务失败（{len(failed)} 块重试耗尽）",
        }
    first = failed[0] if failed else {}
    first_user = first.get("user_message")
    first_reason = first_user if first_user else first.get("error") or "未知错误"
    return {
        "code": "DOWNLOAD_FAILED",
        "message": f"存在块下载失败（不可重试：{first_reason}）",
        "event_message": f"任务失败（{len(failed)} 块下载失败）",
    }


class Orchestrator:
    """任务编排器。"""

    def __init__(self, settings: Settings, broker: EventBroker):
        self.settings = settings
        self.broker = broker
        self.store = TaskStore(settings)
        self.normalizer = Normalizer(settings)
        self.channel = CdsChannel(settings)
        self._cancel_events: Dict[str, threading.Event] = {}
        # 每个任务最近一次下载会话的“进程池真正退出”信号（Event）。resume 会先等待
        # 上一会话（暂停后仍在跑完当前块的旧 worker）收尾，再重建 pending 并发起新
        # 一轮下载 —— 避免新旧会话并发下载同一块（重复请求 / 并发写同一 .nc）。
        self._settle_events: Dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        # 落盘节流：记录每个任务上次写盘时间戳（§3.5 / P1）
        self._persist_ts: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # 提交 / 查询
    # ------------------------------------------------------------------
    def submit(self, schema: RequestSchema,
               task_type: TaskType = TaskType.DOWNLOAD) -> Task:
        try:
            cds_req = self.normalizer.normalize(schema)
        except ValueError as exc:
            raise ApiError(ERR_PARAM, str(exc))
        if not self.channel.mock and not self.channel.has_cds_credentials():
            raise ApiError(ERR_NO_CDS, "未配置 CDS 凭据（~/.cdsapirc），请先完成账号向导")
        prepared = self.channel.prepare_blocks(cds_req)
        task = self.store.create(task_type, {
            "request_schema": schema.model_dump(mode="json"),
            "channel": "cds",
            "dataset": cds_req.dataset,
            "family": cds_req.family,
        })
        # §3.5 / §7-③：params 落盘瘦身——只存可重建的块元数据（key/variable/year/
        # month/day/dataset/freq/rel_target），**不存 request 字典**（避免 31 块任务
        # 响应体膨胀到 ~15KB/块）；运行期由 prepare_blocks 补 request。
        task.params["chunk_granularity"] = cds_req.granularity
        task.params["warnings"] = cds_req.warnings
        task.params["blocks"] = self._slim_blocks(prepared)
        task.block_stats.total = len(prepared)
        self.store.save(task)
        threading.Thread(target=self._run_download, args=(task.id, prepared),
                         daemon=True, name=f"task-{task.id}").start()
        return task

    def get(self, task_id: str) -> Task:
        task = self.store.get(task_id)
        if task is None:
            raise ApiError(ERR_TASK_NOT_FOUND, f"任务不存在: {task_id}")
        return task

    def list(self, status: Optional[str] = None,
             page: int = 1, size: int = 20) -> Dict:
        return self.store.list(status=status, page=page, size=size)

    # ------------------------------------------------------------------
    # 取消 / 续传 / 删除
    # ------------------------------------------------------------------
    def cancel(self, task_id: str) -> Task:
        task = self.get(task_id)
        if task.status not in (TaskStatus.PENDING, TaskStatus.RUNNING):
            raise ApiError(ERR_TASK_STATE, f"任务状态不允许取消: {task.status.value}")
        # 标记取消：cancel_event（主进程）+ cancel.flag（worker 可见）
        # 锁内读取本进程“活动会话”标记：cancel_event（run_blocks 50ms 轮询信号）或
        # settle_event（resume 等待上一会话收尾 / 正在下载的会话注册）。两者皆无 ⇒
        # 磁盘 RUNNING 但没有本进程任何活动线程/worker（如后端重启后的残留 running、
        # 或任务由已退出进程启动）——此时没有任何轮询方会感知 cancel.flag。
        with self._lock:
            ev = self._cancel_events.get(task_id)
            settle = self._settle_events.get(task_id)
            live_session = ev is not None or settle is not None
        if ev is not None:
            ev.set()
        ensure_dir(self.store.task_dir(task_id))
        flag = self.store.task_dir(task_id) / "cancel.flag"
        flag.write_text("cancel", encoding="utf-8")
        if task.status == TaskStatus.PENDING:
            task.transition(TaskStatus.PAUSED)
            task.progress = 0.0
            self.store.save(task)
        elif task.status == TaskStatus.RUNNING and not live_session:
            # —— 暂停无效根因修复（无活动会话的 RUNNING 任务）——
            # 旧逻辑：RUNNING 一律只写 flag + 广播“取消已请求”，等 worker/run_blocks
            # 感知后自行转 paused。若任务实际没有任何活动会话（无 event 可 set、
            # 无 worker 会读 flag），状态将**永远卡 running**：暂停无效、任务不结束、
            # 且 resume（需 PAUSED/FAILED）与 delete（需非 RUNNING）都被拒 → 砖化。
            # 此处无在跑 worker、无待收尾会话，RUNNING→PAUSED 为状态机合法转移，
            # 立即转 paused（后续可正常 resume/delete），绝不静默只写 flag。
            task.transition(TaskStatus.PAUSED)
            task.error = {"code": "CANCELLED",
                          "message": "任务被用户取消（无活动下载会话）",
                          "paused_blocks": 0}
            self.store.save(task)
        self.broker.schedule_broadcast({
            "type": "status", "task_id": task_id, "status": task.status.value,
            "message": "取消已请求" if task.status == TaskStatus.RUNNING else "已取消",
        })
        return task

    def retry_failed(self, task_id: str) -> Task:
        """一键补漏：只重下失败块（已 done 的块自动跳过，不重跑全量）。

        与 resume 的区别：resume 语义上是"继续整任务"，而补漏会先**显式清除失败
        标记**并计数，让"重下 N 个失败块"这一动作可观测、可确认。底层复用
        resume 的路径（重建 blocks → pending_blocks 跳过已 done → 只跑失败/缺失块），
        因此不存在"重跑全量"的重复下载/配额浪费。

        状态要求与 resume 一致（PAUSED / FAILED）。
        """
        task = self.get(task_id)
        if task.status not in (TaskStatus.PAUSED, TaskStatus.FAILED):
            raise ApiError(ERR_TASK_STATE,
                           f"任务状态不允许补漏: {task.status.value}")
        store = ResumableStore(self.store.task_dir(task_id), self.settings)
        cleared = store.clear_failed()
        resumed = self.resume(task_id)
        self.broker.schedule_broadcast({
            "type": "status", "task_id": task_id, "status": resumed.status.value,
            "message": f"补漏已启动：清除 {cleared} 个失败标记，仅重下失败块",
        })
        return resumed

    def resume(self, task_id: str) -> Task:
        task = self.get(task_id)
        if task.status not in (TaskStatus.PAUSED, TaskStatus.FAILED):
            raise ApiError(ERR_TASK_STATE,
                           f"任务状态不允许续传: {task.status.value}")
        # 用 request_schema 重新归一化 + prepare_blocks（P1-2：params 中旧块可能无 request/rel_target）
        schema_dict = (task.params or {}).get("request_schema")
        if not schema_dict:
            raise ApiError(ERR_TASK_STATE, "任务缺少 request_schema，无法续传")
        # §3.5：粒度锁定——用提交时持久化的 chunk_granularity 重建，即使用户此刻把
        # 配置改成 month，本任务仍按提交时的 day 粒度重建 → 块 key 不漂移 → 已完成块
        # 继续被 skip（续传不失效，不重复下载）。
        granularity = (task.params or {}).get("chunk_granularity") or \
            getattr(getattr(self.settings, "download", None), "chunk_granularity", None)
        try:
            schema = RequestSchema(**schema_dict)
            cds_req = self.normalizer.normalize(schema, granularity=granularity)
            prepared = self.channel.prepare_blocks(cds_req)
        except ValueError as exc:
            raise ApiError(ERR_PARAM, str(exc))
        if not prepared:
            raise ApiError(ERR_TASK_STATE, "任务缺少块定义，无法续传")
        # 清理取消标记
        flag = self.store.task_dir(task_id) / "cancel.flag"
        if flag.exists():
            flag.unlink(missing_ok=True)
        # N2：锁内同步置 RUNNING 再起线程，防止连续两次 resume 双线程重复下载
        with self._lock:
            task = self.get(task_id)
            if task.status not in (TaskStatus.PAUSED, TaskStatus.FAILED):
                raise ApiError(ERR_TASK_STATE,
                               f"任务状态不允许续传: {task.status.value}")
            task.transition(TaskStatus.RUNNING)
            task.error = None
            # §7-③：同样落瘦身块（运行期 prepare_blocks 补 request）
            task.params["blocks"] = self._slim_blocks(prepared)
            self.store.save(task)
            threading.Thread(target=self._run_download, args=(task_id, prepared),
                             daemon=True, name=f"task-{task_id}-resume").start()
        return task

    # ------------------------------------------------------------------
    # 块瘦身 / 落盘节流 / files 重建（design-speedup-download.md §3.5）
    # ------------------------------------------------------------------
    @staticmethod
    def _slim_blocks(prepared: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """块瘦身（§7-③）：去掉 request 字典，保留可重建字段。

        保留：key / variable / year / month / day / dataset / freq / rel_target。
        运行期由 Normalizer + prepare_blocks 依 schema 与块字段确定性重建 request。
        """
        keep = ("key", "variable", "year", "month", "day", "dataset", "freq",
                "rel_target")
        return [{k: b[k] for k in keep if k in b} for b in prepared]

    def _persist_throttled(self, task: Task, force: bool = False) -> None:
        """落盘节流（§3.5 / P1）：避免 day 粒度 365~3000 块 × 每块全量 task.json
        写盘造成数百 MB 级无效 IO。

        仅满足以下任一条件才 store.save(task)：
        - force（末尾汇总强制落盘）；或
        - 距上次落盘尚无比对基线（首块建立基线）；或
        - 已完成块数 % progress_persist_every == 0；或
        - 距上次落盘 >= progress_persist_interval_s。
        WS/进度事件在 _on_block_done 内不节流（前端体验不变）。
        """
        import time
        every = self.settings.download.progress_persist_every
        interval = self.settings.download.progress_persist_interval_s
        now = time.time()
        last = self._persist_ts.get(task.id)
        if (force or last is None or task.block_stats.done % every == 0
                or (now - (last or 0.0)) >= interval):
            task.touch()
            self.store.save(task)
            self._persist_ts[task.id] = now

    def _rebuild_result_files(self, blocks: List[Dict[str, Any]],
                              store: "ResumableStore") -> List[str]:
        """result.files 全量重建（§3.5 / P1）。

        按 blocks 的 rel_target + manifest（status==done）收集所有已完成文件，
        而非仅本轮 results 中的 done。day 粒度下 resume 场景若只收集本轮，会丢掉
        上一轮已完成块 → files 不全 → plot_routes / open_mfdataset 出图缺数据。
        """
        manifest = store.load()
        cache_dir = self.settings.cache_dir
        files: List[str] = []
        for b in blocks:
            key = b.get("key")
            status = (manifest.get(key) or {}).get("status")
            if status == "done":
                rel = b.get("rel_target")
                if rel:
                    files.append(str(cache_dir / rel))
        return files

    def delete(self, task_id: str, delete_files: bool = False) -> bool:
        task = self.get(task_id)
        if task.status == TaskStatus.RUNNING:
            raise ApiError(ERR_TASK_STATE, "任务运行中，禁止删除")
        ok = self.store.delete(task_id, delete_files=delete_files)
        if not ok:
            raise ApiError(ERR_TASK_NOT_FOUND, f"任务不存在: {task_id}")
        # 清理会话收尾跟踪（paused 任务可能留有未置位的 settle Event）
        with self._lock:
            self._settle_events.pop(task_id, None)
            self._cancel_events.pop(task_id, None)
        return ok

    def delete_all(self, delete_files: bool) -> Dict[str, int]:
        """一键删除全部任务：running 跳过，其余复用 delete() 删除。

        - 全量枚举（store.list_all 不分页，避免 list 单页 size 上限漏删）。
        - running 任务跳过（计数 skipped_running），**不抛错**；pending/成功/失败/
          暂停任务全部删除。
        - 复用 delete()：内部已含 store.delete 的 task 目录 + 缓存文件清理，以及
          _cancel_events/_settle_events 注册表 pop（防止内存残留）。
        - 返回 {"deleted": N, "skipped_running": M}；无任务 → {0, 0}。
        """
        deleted = 0
        skipped_running = 0
        for task in self.store.list_all():
            if task.status == TaskStatus.RUNNING:
                skipped_running += 1
                continue
            try:
                self.delete(task.id, delete_files=delete_files)
                deleted += 1
            except ApiError as exc:
                if exc.code == ERR_TASK_STATE:
                    # 枚举与删除间的竞态：任务刚进入 running → 跳过不删
                    skipped_running += 1
                elif exc.code == ERR_TASK_NOT_FOUND:
                    # 目录已被并发删除/不存在 → 视为已删（目标状态已达成）
                    deleted += 1
                else:
                    raise
        return {"deleted": deleted, "skipped_running": skipped_running}

    # ------------------------------------------------------------------
    # 内部：下载执行线程
    # ------------------------------------------------------------------
    def _wait_previous_drain(self, task_id: str,
                             cancel_event: threading.Event,
                             timeout_s: Optional[float] = None) -> bool:
        """等待上一下载会话（暂停后仍在跑完当前块的旧 worker）真正退出。

        返回 True = 上一会话已收尾可安全开始新下载；返回 False = 等待被中断
        （收到新一轮取消，或超过 timeout_s 上限仍未见收尾）。中断返回 False 后由
        调用方区分原因：cancel_event.is_set() → 用户取消；否则 → 超时（DRAIN_TIMEOUT）。

        timeout_s：None（默认/直接调用）→ 永不超时，保持既有语义；
        非 None → 超过该秒数视为放弃等待，并顺手清理当前等待的 settle 残留，防止
        任务无限卡 running（旧 worker 若因真实网络挂起等永不退出，旧逻辑会永久空等：
        resume 后 running 但无进度、不结束——用户可见“卡在最后一个块”）。
        """
        prev: Optional[threading.Event] = None
        with self._lock:
            prev = self._settle_events.get(task_id)
        if prev is None:
            return True
        deadline: Optional[float] = None
        if timeout_s is not None:
            deadline = time.monotonic() + max(timeout_s, 0.0)
        while not prev.is_set():
            if cancel_event.is_set():
                return False
            if deadline is not None and time.monotonic() >= deadline:
                # 超时：把仍在 _settle_events 里的陈旧事件清掉（若已被新会话替换
                # 则不动），避免下一次 resume 再次空等同一永不置位的事件。
                with self._lock:
                    if self._settle_events.get(task_id) is prev:
                        self._settle_events.pop(task_id, None)
                return False
            prev.wait(0.1)
        return True

    def _run_download(self, task_id: str, blocks: List[Dict[str, Any]]) -> None:
        task = self.get(task_id)
        cancel_event = threading.Event()
        # 本会话的“进程池真正退出”信号：run_blocks 正常路径同步置位 / 取消路径由
        # run_blocks 起的守护线程在旧 worker 全部退出后置位。
        settle_event: Optional[threading.Event] = None
        settle_handed = False   # settle 是否已交给 run_blocks（含取消 watcher）
        with self._lock:
            self._cancel_events[task_id] = cancel_event
        task_dir = self.store.task_dir(task_id)
        store = ResumableStore(task_dir, self.settings)
        bus = TaskEventBus(task_id, task_dir, self.broker)
        try:
            # N1：取消可能在线程启动前已发生（cancel.flag）→ 一律收束为 paused，
            # 不再进入 running。能走到本分支且 flag 存在 ⇒ 本会话尚未启动任何下载
            # （L275 在会话入口只执行一次；flag 由 cancel() 于本会话启动前落盘）。
            # 可能的状态：
            #   - PENDING：submit 后、本线程执行到此处前被 cancel（cancel() 可能已
            #     同步转 paused，也可能只写 flag——取决于与 cancel() 的读写交错）；
            #   - RUNNING：resume() 在锁内已同步置 RUNNING（L162）后才起本线程，
            #     cancel 恰落在「置 RUNNING 之后、本线程检查 flag 之前」的窗口——
            #     此时 cancel() 见 status==RUNNING 不会转 paused，只写 flag + 广播
            #     “取消已请求”。若本分支只放行 PENDING→PAUSED 而 RUNNING 直接
            #     save+return，任务将永久卡 running（无 worker/无 pool；resume 需
            #     PAUSED/FAILED、delete 需非 RUNNING）→ 彻底砖化。
            # 故凡 flag 存在一律 transition(PAUSED)（RUNNING→PAUSED 为状态机合法
            # 转移），保证任务可再次 resume/delete；emit 固定发 paused（勿沿用
            # 旧代码在 RUNNING 分支下误发 running）。
            if (task_dir / "cancel.flag").exists():
                if task.status != TaskStatus.PAUSED:
                    task.transition(TaskStatus.PAUSED)
                self.store.save(task)
                bus.emit({"type": "status", "status": "paused",
                          "message": "任务已取消（未启动下载）"})
                return
            if task.status == TaskStatus.PENDING:
                task.transition(TaskStatus.RUNNING)
            elif task.status in (TaskStatus.PAUSED, TaskStatus.FAILED):
                task.transition(TaskStatus.RUNNING)
            task.error = None
            self.store.save(task)
            bus.emit({"type": "status", "status": "running",
                      "message": "任务开始运行"})

            # —— Bug 修复（cancel → resume 无法正常继续下载的根因）——
            # 暂停只取消“未开始的块”；在跑块的真实下载（cdsapi.retrieve 阻塞分钟级）
            # 无法被 kill，旧 worker 会继续跑完当前块并写 .done。若 resume 不等待这些
            # 旧 worker 真正退出就立即发起新下载，新一轮会话会把旧在跑块重新判为
            # pending → 新旧两会话并发下载同一块（重复 CDS 请求；并发写同一 .nc，
            # Windows 下相互截断/覆盖导致产物损坏甚至任务失败）。
            # 这里先等上一会话进程池全部退出：期间旧块写完 .done → resume 的
            # pending_blocks 能正确跳过 → 不重复下载。
            drain_ok = self._wait_previous_drain(
                task_id, cancel_event,
                timeout_s=getattr(self.settings.download, "drain_wait_timeout_s", None))
            if not drain_ok:
                # 等待上一会话收尾期间被中断：用户再次取消 → 直接转 paused（不触碰
                # 文件）；或超过 drain_wait_timeout_s 仍未见收尾（旧 worker 挂起）→
                # 同样转 paused（DRAIN_TIMEOUT），绝不无限卡 running。
                timed_out = not cancel_event.is_set()
                task.transition(TaskStatus.PAUSED)
                task.error = {
                    "code": "DRAIN_TIMEOUT" if timed_out else "CANCELLED",
                    "message": ("上一会话迟迟未收尾，等待超时" if timed_out
                                else "任务被用户取消"),
                    "paused_blocks": 0}
                self.store.save(task)
                bus.emit({"type": "status", "status": "paused",
                          "message": ("任务已暂停（等待上一会话收尾超时，可重试续传）"
                                      if timed_out
                                      else "任务已暂停（等待上一会话收尾期间取消）")})
                return
            # 上一会话已退出 → 注册本会话 settle（供可能的下一会话等待）
            settle_event = threading.Event()
            with self._lock:
                self._settle_events[task_id] = settle_event

            # 断点续传：跳过 done 块
            pending = store.pending_blocks(blocks)
            # 提交顺序去偏（bugfix download-gaps）：_split_blocks 是变量外层循环，
            # 不交错的话"变量 A 的块全排在最前"，在 CDS 排队/限流墙下会把
            # 排在后面的变量饿死（线上实证：10m_u 拿到 52 个文件、10m_v 0 个）。
            # 交错是纯排序，不改变块集合/数量，故不影响断点续传的 key 匹配。
            if getattr(self.settings.download, "block_interleave", True):
                pending = interleave_blocks(pending)
            skipped = len(blocks) - len(pending)
            task.block_stats.total = len(blocks)
            task.block_stats.skipped = skipped
            task.block_stats.done = skipped
            self.store.save(task)

            total_blocks = len(blocks)

            def _on_block_done(result: Dict[str, Any], completed: int,
                               total: int) -> None:
                """每完成一个块：实时累加 block_stats、更新 progress、落盘并 WS 推送。

                - task.block_stats.done 初值 = skipped（断点续传已计入），
                  每完成一个 done 块 +1 → done_so_far = skipped + 新完成 done 数；
                - progress = done_so_far / 总块数（skipped 也计入进度）；
                - 末尾统一汇总仍会重算（幂等），不破坏最终状态判定 SUCCESS/FAILED/PAUSED。
                """
                try:
                    status = result.get("status")
                    if status == "done":
                        task.block_stats.done += 1
                    elif status == "failed":
                        task.block_stats.failed += 1
                    task.progress = round(
                        task.block_stats.done / max(total_blocks, 1), 4)
                    # 落盘节流：避免 day 粒度 365~3000 块 × 每块全量 task.json 写盘
                    # 造成数百 MB 级无效 IO（§3.5 / P1）。WS/进度事件仍每块发送。
                    self._persist_throttled(task)
                    bus.emit({
                        "type": "task",
                        "task_id": task.id,
                        "status": task.status.value,
                        "progress": task.progress,
                        "block_stats": task.block_stats.model_dump(mode="json"),
                    })
                except Exception:
                    # 进度回调异常不应中断下载主流程（落盘/推送尽力而为）
                    pass

            if pending:
                bus.start()
                try:
                    results = self.channel.run_blocks(task, pending, store, bus,
                                                      cancel_event,
                                                      on_block_done=_on_block_done,
                                                      settle_event=settle_event)
                finally:
                    bus.stop()
                # run_blocks 已接手 settle：正常路径同步置位，取消路径由 watcher 置位
                settle_handed = True
            else:
                results = []

            # 汇总
            cancelled = [r for r in results if r["status"] == "cancelled"]
            failed = [r for r in results if r["status"] == "failed"]
            done = [r for r in results if r["status"] == "done"]
            manifest = store.load()
            for r in results:
                if r["status"] == "done":
                    manifest[r["block"]] = {"status": "done"}
                elif r["status"] == "failed":
                    manifest[r["block"]] = {"status": "failed",
                                            "error": r.get("error", "BUSY_AFTER_RETRIES")}
            store.save(manifest)

            task.block_stats.done = skipped + len(done)
            task.block_stats.failed = len(failed)
            done_total = skipped + len(done)

            if cancel_event.is_set() or cancelled:
                task.transition(TaskStatus.PAUSED)
                task.error = {"code": "CANCELLED", "message": "任务被用户取消",
                              "outcome": (OUTCOME_PARTIAL_SUCCESS if done_total
                                          else OUTCOME_ALL_FAILED),
                              "paused_blocks": len(cancelled)}
                bus.emit({"type": "status", "status": "paused",
                          "message": f"任务已暂停（未完成 {len(cancelled)} 块）"})
            elif failed:
                task.transition(TaskStatus.FAILED)
                err = failed_task_error(failed)
                partial = done_total > 0
                summary = summarize_failures(failed)
                task.error = {
                    "code": err["code"],
                    "message": err["message"],
                    # 明确区分「部分成功 / 全部失败」：部分成功时已有数据可用，
                    # 只需补漏，不应让用户以为整单报废。
                    "outcome": (OUTCOME_PARTIAL_SUCCESS if partial
                                else OUTCOME_ALL_FAILED),
                    "failed_blocks": [r["block"] for r in failed[:FAILED_BLOCKS_CAP]],
                    "failed_blocks_total": len(failed),
                    "failed_blocks_truncated": len(failed) > FAILED_BLOCKS_CAP,
                    "failure_summary": summary,
                    "done_blocks": done_total,
                    "retryable_failed": summary["retryable"],
                    "hint": ("已有部分块下载成功，可点击『续传/补漏』只重下失败块"
                             if partial else
                             "全部块下载失败，请检查失败原因后『补漏』重试"),
                }
                if partial:
                    # 部分成功：把已到手的文件写进 result，用户可直接出图/查看，
                    # 不必等补漏完成（plot_routes 只读 result["files"]，兼容）。
                    task.result = {"partial": True,
                                   "outcome": OUTCOME_PARTIAL_SUCCESS,
                                   "cache_hit": skipped > 0,
                                   "files": self._rebuild_result_files(blocks, store)}
                bus.emit({"type": "status", "status": "failed",
                          "message": err["event_message"],
                          "outcome": task.error["outcome"],
                          "failure_summary": summary})
            else:
                task.transition(TaskStatus.SUCCESS)
                task.progress = 1.0
                # result.files 全量重建（§3.5 / P1）：按 blocks 的 rel_target +
                # manifest(status==done) 收集所有已完成文件，而非仅本轮 results
                # 中的 done。修复 day 粒度 resume 场景 files 丢掉上一轮已完成块
                # （plot_routes 拿到的文件不全 → open_mfdataset 出图缺数据）。
                task.result = {"cache_hit": skipped > 0,
                               "outcome": OUTCOME_ALL_SUCCESS,
                               "files": self._rebuild_result_files(blocks, store)}
                bus.emit({"type": "status", "status": "success",
                          "message": "任务完成"})
                bus.emit({"type": "done", "status": "success",
                          "message": "全部块下载完成",
                          "block_stats": task.block_stats.model_dump(mode="json")})
        except Exception as exc:  # 兜底：任务进入 failed
            try:
                task = self.get(task_id)
                task.transition(TaskStatus.FAILED)
                task.error = {"code": "INTERNAL", "message": str(exc)}
                self.store.save(task)
                bus.emit({"type": "status", "status": "failed",
                          "message": f"任务异常终止: {exc}"})
            except Exception:
                pass
        finally:
            # 持久化最终状态：直接保存本地 task（各分支已更新状态），
            # 不能重新从 store 读取（磁盘上仍是 running，会覆盖新状态）。
            try:
                task.touch()
                self.store.save(task)
            except Exception:
                pass
            # settle 收尾：本会话未把 settle 交给 run_blocks（pending 为空 / 未进入
            # run_blocks）→ 无进程池可等，立即置位，避免后续 resume 死等。
            # 已交给 run_blocks 的正常路径：run_blocks 已同步置位；取消路径：等
            # run_blocks 起的 watcher 在旧 worker 全部退出后置位。
            # 兜底也覆盖 run_blocks 在自身 finally **之前**抛异常（如 pool 构造失败）：
            # 此时 settle_handed 仍为 False（L364 只在正常返回后置 True）→ 此处置位并
            # 清理，下一次 resume/_wait_previous_drain 不空等（QA R1 缺陷2 实证结论）。
            if settle_event is not None and not settle_handed:
                settle_event.set()
            if settle_event is not None and settle_event.is_set():
                with self._lock:
                    if self._settle_events.get(task_id) is settle_event:
                        self._settle_events.pop(task_id, None)
            # cancel_event 收尾：仅当仍指向本会话的 event 才 pop（防止旧线程误删
            # 快速 resume 新会话刚注册的 cancel_event → 下一次 cancel 失效）。
            with self._lock:
                if self._cancel_events.get(task_id) is cancel_event:
                    self._cancel_events.pop(task_id, None)
