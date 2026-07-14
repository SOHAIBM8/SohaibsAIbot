import { Navigate } from "react-router-dom";
import { useAuth } from "@/auth/AuthContext";
import { AppShell } from "@/components/layout/AppShell";

export function RequireAuth() {
  const { session, isLoading } = useAuth();

  if (isLoading) {
    return <div className="p-6 text-sm text-gray-500">Loading…</div>;
  }
  if (!session) {
    return <Navigate to="/login" replace />;
  }
  return <AppShell />;
}
