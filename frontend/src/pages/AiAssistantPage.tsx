import { useState, type FormEvent } from "react";
import { api, ApiError } from "@/lib/api";
import { Card, CardHeader, CardTitle } from "@/components/ui/Card";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import type { ChatResponseOut } from "@/types/api";

interface ChatMessage {
  role: "user" | "assistant";
  text: string;
}

/**
 * Must visually distinguish "the deterministic system decided X" from
 * "the AI is describing X" (spec section 16) — every assistant turn
 * carries the same "AI-generated narration" badge OrderDetailPage's
 * explanation viewer uses, so no user ever mistakes a chat answer for
 * a structured system fact.
 */
export function AiAssistantPage() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [question, setQuestion] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (!question.trim()) return;
    const userMessage: ChatMessage = { role: "user", text: question };
    setMessages((prev) => [...prev, userMessage]);
    setQuestion("");
    setSending(true);
    setError(null);
    try {
      const response = await api.post<ChatResponseOut>("/api/ai/chat", { question: userMessage.text });
      setMessages((prev) => [...prev, { role: "assistant", text: response.answer }]);
    } catch (err) {
      const detail =
        err instanceof ApiError
          ? typeof err.detail === "string"
            ? err.detail
            : "The AI assistant is unavailable."
          : "Request failed.";
      setError(detail);
    } finally {
      setSending(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>AI Assistant</CardTitle>
      </CardHeader>
      <div className="mb-3 max-h-96 space-y-3 overflow-y-auto">
        {messages.length === 0 && (
          <p className="text-sm text-gray-500">
            Ask about your own trading data — trades, risk decisions, regime history.
          </p>
        )}
        {messages.map((m, i) => (
          <div key={i} className={m.role === "user" ? "text-right" : "text-left"}>
            {m.role === "assistant" && (
              <div className="mb-1">
                <Badge tone="info">AI-generated narration — not a system decision</Badge>
              </div>
            )}
            <div
              className={
                m.role === "user"
                  ? "inline-block rounded-lg bg-blue-600 px-3 py-2 text-sm text-white"
                  : "inline-block rounded-lg bg-gray-100 px-3 py-2 text-sm text-gray-800 dark:bg-gray-800 dark:text-gray-200"
              }
            >
              {m.text}
            </div>
          </div>
        ))}
        {sending && <p className="text-sm text-gray-400">Thinking…</p>}
      </div>
      {error && (
        <div className="mb-3 rounded border border-red-200 bg-red-50 p-2 text-xs text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300">
          {error}
        </div>
      )}
      <form onSubmit={(e) => void handleSubmit(e)} className="flex gap-2">
        <input
          className="flex-1 rounded border border-gray-300 px-3 py-1.5 text-sm dark:border-gray-700 dark:bg-gray-800"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="What happened with my last trade?"
          disabled={sending}
        />
        <Button type="submit" variant="primary" disabled={sending}>
          Send
        </Button>
      </form>
    </Card>
  );
}
