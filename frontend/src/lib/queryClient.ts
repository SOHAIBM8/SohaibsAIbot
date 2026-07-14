import { QueryClient } from "@tanstack/react-query";

/**
 * Server state lives exclusively in TanStack Query's cache (spec
 * section 20) — no parallel Zustand/context copy of anything this
 * client fetches from the API, which is exactly how "the UI shows
 * something different from what the backend actually says" bugs
 * happen.
 */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      staleTime: 10_000,
      refetchOnWindowFocus: false,
    },
  },
});
