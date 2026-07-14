import { useEffect, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { Card, CardHeader, CardTitle } from "@/components/ui/Card";
import { Table, Thead, Tbody, Th, Td } from "@/components/ui/Table";
import { SkeletonRows } from "@/components/ui/Skeleton";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import type {
  CredentialCreateIn,
  CredentialSummaryOut,
  NotificationPreferencesIn,
  NotificationPreferencesOut,
} from "@/types/api";

/**
 * Credential masking shows metadata only (credential_id/exchange/
 * mainnet/state) — no last-4-characters display exists, since the
 * backend has no fingerprint column to source it from (a real gap,
 * documented in api/routes/settings.py and CLAUDE.md, not a frontend
 * shortcut). "Add new credential" only ever accepts testnet/paper
 * (mainnet is forced off client-side too, matching the backend's own
 * 400 rejection — belt and suspenders, not a substitute for it).
 */
export function SettingsPage() {
  const queryClient = useQueryClient();
  const credentialsQuery = useQuery({
    queryKey: ["settings", "credentials"],
    queryFn: () => api.get<CredentialSummaryOut[]>("/api/settings/credentials"),
  });

  const [addDialogOpen, setAddDialogOpen] = useState(false);
  const [form, setForm] = useState<CredentialCreateIn>({
    exchange: "binance",
    api_key: "",
    api_secret: "",
    mainnet: false,
  });

  async function submitCredential() {
    await api.post<CredentialSummaryOut>("/api/settings/credentials", form);
    setForm({ exchange: "binance", api_key: "", api_secret: "", mainnet: false });
    await queryClient.invalidateQueries({ queryKey: ["settings", "credentials"] });
  }

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle>Exchange credentials</CardTitle>
          <Button
            variant="primary"
            onClick={() => setAddDialogOpen(true)}
            data-testid="add-credential-open"
          >
            Add credential
          </Button>
        </CardHeader>
        {credentialsQuery.isLoading && <SkeletonRows rows={2} />}
        {credentialsQuery.data && credentialsQuery.data.length === 0 && (
          <p className="text-sm text-gray-500" data-testid="credentials-empty">
            No credentials registered yet.
          </p>
        )}
        {credentialsQuery.data && credentialsQuery.data.length > 0 && (
          <Table>
            <Thead>
              <tr>
                <Th>Exchange</Th>
                <Th>Network</Th>
                <Th>State</Th>
                <Th>Last validated</Th>
              </tr>
            </Thead>
            <Tbody>
              {credentialsQuery.data.map((c) => (
                <tr key={c.credential_id} data-testid="credential-row">
                  <Td>{c.exchange}</Td>
                  <Td>{c.mainnet ? "Mainnet" : "Testnet"}</Td>
                  <Td>
                    <Badge
                      tone={
                        c.state === "active"
                          ? "success"
                          : c.state === "validation_failed"
                            ? "critical"
                            : "neutral"
                      }
                    >
                      {c.state}
                    </Badge>
                  </Td>
                  <Td>
                    {c.last_validated_at ? new Date(c.last_validated_at).toLocaleString() : "—"}
                  </Td>
                </tr>
              ))}
            </Tbody>
          </Table>
        )}
      </Card>

      <NotificationPreferencesCard />

      <ConfirmDialog
        open={addDialogOpen}
        title="Add exchange credential"
        confirmLabel="Add credential"
        description={
          <div className="space-y-2">
            <p>
              Testnet/paper only — this deployment has no real cloud KMS configured, so mainnet
              credentials are rejected.
            </p>
            <input
              data-testid="credential-exchange"
              className="w-full rounded border border-gray-300 px-2 py-1 dark:border-gray-700 dark:bg-gray-800"
              placeholder="exchange (e.g. binance)"
              value={form.exchange}
              onChange={(e) => setForm({ ...form, exchange: e.target.value })}
            />
            <input
              data-testid="credential-api-key"
              className="w-full rounded border border-gray-300 px-2 py-1 dark:border-gray-700 dark:bg-gray-800"
              placeholder="API key"
              value={form.api_key}
              onChange={(e) => setForm({ ...form, api_key: e.target.value })}
            />
            <input
              data-testid="credential-api-secret"
              type="password"
              className="w-full rounded border border-gray-300 px-2 py-1 dark:border-gray-700 dark:bg-gray-800"
              placeholder="API secret"
              value={form.api_secret}
              onChange={(e) => setForm({ ...form, api_secret: e.target.value })}
            />
          </div>
        }
        onConfirm={submitCredential}
        onClose={() => setAddDialogOpen(false)}
      />
    </div>
  );
}

function NotificationPreferencesCard() {
  const queryClient = useQueryClient();
  const prefsQuery = useQuery({
    queryKey: ["settings", "notifications"],
    queryFn: () => api.get<NotificationPreferencesOut>("/api/settings/notifications"),
  });
  const [form, setForm] = useState<NotificationPreferencesIn | null>(null);

  useEffect(() => {
    if (prefsQuery.data && !form) {
      const { account_id: _accountId, updated_at: _updatedAt, ...rest } = prefsQuery.data;
      setForm(rest);
    }
  }, [prefsQuery.data, form]);

  const mutation = useMutation({
    mutationFn: (body: NotificationPreferencesIn) =>
      api.put<NotificationPreferencesOut>("/api/settings/notifications", body),
    onSuccess: (data) => {
      queryClient.setQueryData(["settings", "notifications"], data);
    },
  });

  if (!form) return <SkeletonRows rows={3} />;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Notification preferences</CardTitle>
      </CardHeader>
      <div className="space-y-2 text-sm">
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={form.email_enabled}
            onChange={(e) => setForm({ ...form, email_enabled: e.target.checked })}
          />
          Email notifications
        </label>
        {form.email_enabled && (
          <input
            className="w-full rounded border border-gray-300 px-2 py-1 dark:border-gray-700 dark:bg-gray-800"
            placeholder="email address"
            value={form.email_address ?? ""}
            onChange={(e) => setForm({ ...form, email_address: e.target.value })}
          />
        )}
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={form.webhook_enabled}
            onChange={(e) => setForm({ ...form, webhook_enabled: e.target.checked })}
          />
          Webhook notifications
        </label>
        {form.webhook_enabled && (
          <input
            className="w-full rounded border border-gray-300 px-2 py-1 dark:border-gray-700 dark:bg-gray-800"
            placeholder="webhook URL"
            value={form.webhook_url ?? ""}
            onChange={(e) => setForm({ ...form, webhook_url: e.target.value })}
          />
        )}
        <hr className="border-gray-200 dark:border-gray-800" />
        {(
          [
            ["notify_on_kill_switch", "Kill switch events"],
            ["notify_on_credential_validation_failed", "Credential validation failures"],
            ["notify_on_drawdown_breach", "Drawdown breaches"],
          ] as const
        ).map(([key, label]) => (
          <label key={key} className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={form[key]}
              onChange={(e) => setForm({ ...form, [key]: e.target.checked })}
            />
            {label}
          </label>
        ))}
        <Button
          variant="primary"
          onClick={() => mutation.mutate(form)}
          disabled={mutation.isPending}
        >
          {mutation.isPending ? "Saving…" : "Save preferences"}
        </Button>
        {mutation.isSuccess && <p className="text-xs text-green-600">Saved.</p>}
      </div>
    </Card>
  );
}
