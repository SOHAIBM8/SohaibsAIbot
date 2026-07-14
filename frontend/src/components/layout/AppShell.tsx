import { useEffect, type ReactNode } from "react";
import { Outlet } from "react-router-dom";
import { useAuth } from "@/auth/AuthContext";
import { Nav } from "@/components/layout/Nav";
import { ModeBanner } from "@/components/layout/ModeBanner";
import { Button } from "@/components/ui/Button";
import { connectWebSocket, disconnectWebSocket, useWebSocketStore } from "@/ws/websocketStore";

function ConnectionDot() {
  const status = useWebSocketStore((s) => s.status);
  const color =
    status === "open" ? "bg-green-500" : status === "connecting" ? "bg-amber-500" : "bg-gray-400";
  return (
    <span className="flex items-center gap-1.5 text-xs text-gray-500 dark:text-gray-400">
      <span className={`h-2 w-2 rounded-full ${color}`} />
      {status}
    </span>
  );
}

export function AppShell({ children }: { children?: ReactNode }) {
  const { session, logout } = useAuth();

  useEffect(() => {
    connectWebSocket();
    return () => disconnectWebSocket();
  }, []);

  return (
    <div className="min-h-screen bg-gray-50 dark:bg-gray-950">
      <header className="flex items-center justify-between border-b border-gray-200 px-4 py-3 dark:border-gray-800">
        <div className="flex items-center gap-3">
          <span className="text-sm font-semibold text-gray-900 dark:text-gray-100">
            Trading Dashboard
          </span>
          <ModeBanner />
        </div>
        <div className="flex items-center gap-4">
          <ConnectionDot />
          {session && (
            <>
              <span className="text-xs text-gray-500 dark:text-gray-400">{session.account_id}</span>
              <Button variant="secondary" onClick={() => void logout()}>
                Log out
              </Button>
            </>
          )}
        </div>
      </header>
      <Nav />
      <main className="mx-auto max-w-6xl p-4">{children ?? <Outlet />}</main>
    </div>
  );
}
