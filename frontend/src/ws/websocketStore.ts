import { create } from "zustand";

export interface WsEvent {
  event_type?: string;
  [key: string]: unknown;
}

type ConnectionStatus = "connecting" | "open" | "closed";

interface WebSocketState {
  status: ConnectionStatus;
  events: WsEvent[];
}

const MAX_BUFFERED_EVENTS = 100;

export const useWebSocketStore = create<WebSocketState>(() => ({
  status: "closed",
  events: [],
}));

let socket: WebSocket | null = null;
let reconnectAttempt = 0;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let manuallyClosed = false;

/**
 * Client-side reconnect/backoff mirrors the same pattern already built
 * server-side for exchange WebSocket connections (spec section 19;
 * core/marketdata/websocket_connection.py's exponential-backoff-with-
 * jitter reconnect) — a proven pattern, not reinvented for the
 * frontend.
 */
function scheduleReconnect() {
  if (manuallyClosed) return;
  const baseMs = Math.min(1000 * 2 ** reconnectAttempt, 30_000);
  const jitterMs = Math.random() * 500;
  reconnectAttempt += 1;
  reconnectTimer = setTimeout(connectWebSocket, baseMs + jitterMs);
}

export function connectWebSocket() {
  manuallyClosed = false;
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
    return;
  }
  useWebSocketStore.setState({ status: "connecting" });

  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  socket = new WebSocket(`${protocol}//${window.location.host}/api/ws`);

  socket.onopen = () => {
    reconnectAttempt = 0;
    useWebSocketStore.setState({ status: "open" });
  };

  socket.onmessage = (event) => {
    try {
      const parsed = JSON.parse(event.data) as WsEvent;
      useWebSocketStore.setState((state) => ({
        events: [parsed, ...state.events].slice(0, MAX_BUFFERED_EVENTS),
      }));
    } catch {
      // Malformed frame — dropped, never crashes the connection.
    }
  };

  socket.onclose = () => {
    useWebSocketStore.setState({ status: "closed" });
    scheduleReconnect();
  };

  socket.onerror = () => {
    socket?.close();
  };
}

export function disconnectWebSocket() {
  manuallyClosed = true;
  if (reconnectTimer) clearTimeout(reconnectTimer);
  socket?.close();
  socket = null;
  useWebSocketStore.setState({ status: "closed" });
}
