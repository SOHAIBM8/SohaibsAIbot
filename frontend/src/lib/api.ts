/**
 * Typed fetch wrapper for the dashboard API. Every mutating request
 * echoes the `dashboard_csrf` cookie in an X-CSRF-Token header (the
 * double-submit pattern api/auth/csrf.py implements) — this is the
 * ONE place that happens, so no call site can forget it.
 *
 * Error handling follows spec section 21 ("every API error surfaces
 * the backend's actual structured reason... the UI must not discard
 * that precision"): ApiError carries the parsed response body, not
 * just a status code, so callers can render the real `detail` string
 * FastAPI's HTTPException produced.
 */

const CSRF_COOKIE_NAME = "dashboard_csrf";

export class ApiError extends Error {
  status: number;
  detail: unknown;

  constructor(status: number, detail: unknown) {
    super(typeof detail === "string" ? detail : `Request failed with status ${status}`);
    this.status = status;
    this.detail = detail;
  }
}

function readCookie(name: string): string | null {
  const match = document.cookie.match(new RegExp(`(?:^|; )${name}=([^;]*)`));
  return match ? decodeURIComponent(match[1]) : null;
}

const MUTATING_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

interface RequestOptions {
  method?: string;
  body?: unknown;
  params?: Record<string, string | number | boolean | undefined | null | string[]>;
}

function buildQueryString(params?: RequestOptions["params"]): string {
  if (!params) return "";
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null) continue;
    if (Array.isArray(value)) {
      for (const item of value) search.append(key, String(item));
    } else {
      search.append(key, String(value));
    }
  }
  const qs = search.toString();
  return qs ? `?${qs}` : "";
}

export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = options.method ?? "GET";
  const headers: Record<string, string> = {};

  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
  }
  if (MUTATING_METHODS.has(method)) {
    const csrfToken = readCookie(CSRF_COOKIE_NAME);
    if (csrfToken) headers["X-CSRF-Token"] = csrfToken;
  }

  const response = await fetch(`${path}${buildQueryString(options.params)}`, {
    method,
    headers,
    credentials: "include",
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
  });

  if (!response.ok) {
    let detail: unknown = null;
    try {
      const parsed = await response.json();
      detail = parsed.detail ?? parsed;
    } catch {
      detail = response.statusText;
    }
    throw new ApiError(response.status, detail);
  }

  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

export const api = {
  get: <T>(path: string, params?: RequestOptions["params"]) =>
    apiRequest<T>(path, { method: "GET", params }),
  post: <T>(path: string, body?: unknown) => apiRequest<T>(path, { method: "POST", body }),
  put: <T>(path: string, body?: unknown) => apiRequest<T>(path, { method: "PUT", body }),
  delete: <T>(path: string) => apiRequest<T>(path, { method: "DELETE" }),
};
