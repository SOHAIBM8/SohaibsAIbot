import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "@/auth/AuthContext";
import { Button } from "@/components/ui/Button";
import { ApiError } from "@/lib/api";

export function LoginPage() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await login(username, password);
      navigate("/", { replace: true });
    } catch (err) {
      const message =
        err instanceof ApiError && typeof err.detail === "string"
          ? err.detail
          : "Login failed";
      setError(message);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-gray-50 dark:bg-gray-950">
      <form
        onSubmit={(e) => void handleSubmit(e)}
        className="w-full max-w-sm rounded-lg border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-900"
      >
        <h1 className="mb-4 text-lg font-semibold text-gray-900 dark:text-gray-100">
          Trading Dashboard
        </h1>
        <label className="mb-1 block text-xs font-medium text-gray-600 dark:text-gray-400">
          Username
        </label>
        <input
          data-testid="login-username"
          className="mb-3 w-full rounded border border-gray-300 px-2.5 py-1.5 text-sm dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          autoComplete="username"
        />
        <label className="mb-1 block text-xs font-medium text-gray-600 dark:text-gray-400">
          Password
        </label>
        <input
          data-testid="login-password"
          type="password"
          className="mb-4 w-full rounded border border-gray-300 px-2.5 py-1.5 text-sm dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="current-password"
        />
        {error && (
          <div
            data-testid="login-error"
            className="mb-3 rounded border border-red-200 bg-red-50 p-2 text-xs text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300"
          >
            {error}
          </div>
        )}
        <Button
          type="submit"
          variant="primary"
          className="w-full"
          disabled={submitting}
          data-testid="login-submit"
        >
          {submitting ? "Logging in…" : "Log in"}
        </Button>
      </form>
    </div>
  );
}
