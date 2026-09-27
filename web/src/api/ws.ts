// WebSocket 客户端：订阅 /ws/tasks，自动重连
import type { WsEvent } from "../types";

const WS_URL =
  (import.meta.env.VITE_WS_BASE as string | undefined) ||
  `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/tasks`;

type Handler = (ev: WsEvent) => void;

class WsClient {
  private ws: WebSocket | null = null;
  private handlers: Set<Handler> = new Set();
  private retry = 0;
  private timer: number | null = null;
  private desired: boolean = false;

  connect() {
    this.desired = true;
    this._open();
  }

  private _open() {
    if (!this.desired) return;
    try {
      this.ws = new WebSocket(WS_URL);
    } catch {
      this._scheduleReconnect();
      return;
    }
    this.ws.onopen = () => {
      this.retry = 0;
      this.emit({ type: "subscribed", task_id: "", ts: new Date().toISOString(), message: "connected" });
    };
    this.ws.onmessage = (e) => {
      try {
        const ev = JSON.parse(e.data as string) as WsEvent;
        this.emit(ev);
      } catch {
        /* 忽略非 JSON 帧 */
      }
    };
    this.ws.onclose = () => {
      this._scheduleReconnect();
    };
    this.ws.onerror = () => {
      this.ws?.close();
    };
  }

  private _scheduleReconnect() {
    if (!this.desired) return;
    const delay = Math.min(1000 * 2 ** this.retry, 15000);
    this.retry += 1;
    if (this.timer) window.clearTimeout(this.timer);
    this.timer = window.setTimeout(() => this._open(), delay);
  }

  subscribe(task_id?: string) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ action: "subscribe", task_id }));
    }
  }

  on(handler: Handler) {
    this.handlers.add(handler);
    return () => this.handlers.delete(handler);
  }

  private emit(ev: WsEvent) {
    this.handlers.forEach((h) => {
      try {
        h(ev);
      } catch {
        /* 忽略 handler 异常 */
      }
    });
  }

  close() {
    this.desired = false;
    if (this.timer) window.clearTimeout(this.timer);
    this.ws?.close();
    this.ws = null;
  }
}

export const wsClient = new WsClient();
