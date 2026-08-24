import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { formatDateTime, formatMoney, useAsync } from "../lib/hooks";
import { Button, Card, ErrorNote, Field, Input, Spinner, Stat, cx } from "../components/ui";
import type { AppSettings } from "../lib/types";

function SpendBar({ spent, cap }: { spent: number; cap: number }) {
  const fraction = cap > 0 ? Math.min(1, spent / cap) : 0;
  const tone = fraction >= 1 ? "bg-bad" : fraction >= 0.8 ? "bg-warn" : "bg-accent";
  return (
    <div>
      <div className="mb-1 flex justify-between text-xs">
        <span className="text-ink-400">This month</span>
        <span className={cx("tnum", fraction >= 0.8 ? "text-warn" : "text-ink-300")}>
          {formatMoney(spent)} / {formatMoney(cap)}
        </span>
      </div>
      <div className="h-2 overflow-hidden rounded bg-ink-850">
        <div
          className={cx("h-full rounded transition-all", tone)}
          style={{ width: `${Math.max(1, fraction * 100)}%` }}
        />
      </div>
      {fraction >= 0.8 && fraction < 1 && (
        <p className="mt-1 text-xs text-warn">
          Past 80% of the cap. LLM work halts at 100%; discovery keeps running.
        </p>
      )}
      {fraction >= 1 && (
        <p className="mt-1 text-xs text-bad">
          Cap reached. Scoring and document generation are halted until next month or
          until you raise the cap. Discovery is unaffected.
        </p>
      )}
    </div>
  );
}

export function SettingsPage() {
  const { data, error, loading, reload } = useAsync(() => api.getSettings(), []);
  const runs = useAsync(() => api.recentRuns(10), []);
  const [draft, setDraft] = useState<Partial<AppSettings>>({});
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (data) setDraft(data);
  }, [data]);

  if (loading || !data) return <Spinner />;

  const set = <K extends keyof AppSettings>(key: K, value: AppSettings[K]) =>
    setDraft((current) => ({ ...current, [key]: value }));

  const save = async () => {
    setSaving(true);
    try {
      await api.saveSettings({
        llm_monthly_cap_usd: draft.llm_monthly_cap_usd,
        apply_window_start: draft.apply_window_start,
        apply_window_end: draft.apply_window_end,
        apply_min_interval_floor_seconds: draft.apply_min_interval_floor_seconds,
        scoring_stage1_top_n: draft.scoring_stage1_top_n,
        scoring_cost_target_usd: draft.scoring_cost_target_usd,
        discovery_default_hours_old: draft.discovery_default_hours_old,
      });
      reload();
    } finally {
      setSaving(false);
    }
  };

  const spent = Number(data.spend?.spent_usd ?? data.spend?.spend_this_month ?? 0);
  const breakers = Object.entries(data.circuit_breakers ?? {});

  return (
    <div className="mx-auto max-w-3xl">
      <div className="mb-3 flex items-center gap-3">
        <h1 className="text-lg font-semibold">Settings</h1>
        <Button className="ml-auto" variant="primary" onClick={save} disabled={saving}>
          {saving ? "Saving…" : "Save"}
        </Button>
      </div>

      <ErrorNote error={error} />

      {/* Read-only on purpose: this is the one control that must require a
          human editing .env on the machine itself. */}
      <Card title="Live submit" className="mb-4">
        <div className="flex items-start gap-4">
          <Stat
            label="ALLOW_LIVE_SUBMIT"
            value={data.allow_live_submit ? "ON" : "OFF"}
            tone={data.allow_live_submit ? "warn" : "normal"}
          />
          <p className="flex-1 text-xs text-ink-400">
            {data.allow_live_submit ? (
              <>
                Applications <strong className="text-warn">can be submitted</strong>. Every
                guardrail still applies — caps, the schedule window, the warm-up ramp, the
                parse gate and the answer bank.
              </>
            ) : (
              <>
                Nothing can be submitted. The whole pipeline runs and reports what it{" "}
                <em>would</em> send. This is deliberately <strong>not</strong> a toggle:
                set <code className="text-ink-300">ALLOW_LIVE_SUBMIT=true</code> in{" "}
                <code className="text-ink-300">.env</code> on the machine and restart.
              </>
            )}
          </p>
        </div>
      </Card>

      <Card title="Spend" className="mb-4">
        <SpendBar spent={spent} cap={draft.llm_monthly_cap_usd ?? data.llm_monthly_cap_usd} />
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          <Field label="Monthly cap (USD)">
            <Input
              type="number"
              step="0.5"
              value={draft.llm_monthly_cap_usd ?? ""}
              onChange={(e) => set("llm_monthly_cap_usd", Number(e.target.value))}
            />
          </Field>
          <Field
            label="Scoring cost target (USD / 200 jobs)"
            hint="A run projecting above this logs a warning before spending."
          >
            <Input
              type="number"
              step="0.01"
              value={draft.scoring_cost_target_usd ?? ""}
              onChange={(e) => set("scoring_cost_target_usd", Number(e.target.value))}
            />
          </Field>
        </div>
      </Card>

      <Card title="Schedule and pacing" className="mb-4">
        <div className="grid gap-3 md:grid-cols-2">
          <Field label="Apply window start" hint={`Local time (${data.timezone})`}>
            <Input
              value={draft.apply_window_start ?? ""}
              onChange={(e) => set("apply_window_start", e.target.value)}
            />
          </Field>
          <Field label="Apply window end">
            <Input
              value={draft.apply_window_end ?? ""}
              onChange={(e) => set("apply_window_end", e.target.value)}
            />
          </Field>
          <Field
            label="Minimum interval floor (seconds)"
            hint="Actual gaps are drawn randomly above this — a fixed cadence is a bot signature."
          >
            <Input
              type="number"
              value={draft.apply_min_interval_floor_seconds ?? ""}
              onChange={(e) =>
                set("apply_min_interval_floor_seconds", Number(e.target.value))
              }
            />
          </Field>
          <Field label="Discovery window (hours)" hint="How far back an incremental run looks.">
            <Input
              type="number"
              value={draft.discovery_default_hours_old ?? ""}
              onChange={(e) => set("discovery_default_hours_old", Number(e.target.value))}
            />
          </Field>
          <Field
            label="Stage 2 fan-out (top N)"
            hint="The main cost lever: only this many jobs reach the LLM."
          >
            <Input
              type="number"
              value={draft.scoring_stage1_top_n ?? ""}
              onChange={(e) => set("scoring_stage1_top_n", Number(e.target.value))}
            />
          </Field>
        </div>
      </Card>

      {breakers.length > 0 && (
        <Card title="Circuit breakers" className="mb-4">
          <div className="space-y-1 text-xs">
            {breakers.map(([platform, state]) => (
              <div key={platform} className="flex items-center gap-2">
                <span
                  className={cx(
                    "inline-block h-2 w-2 rounded-full",
                    state.disabled ? "bg-bad" : "bg-good",
                  )}
                />
                <span className="text-ink-100">{platform}</span>
                <span className="text-ink-400">
                  {state.disabled
                    ? "disabled after consecutive failures"
                    : `${state.consecutive_failures} consecutive failures`}
                </span>
              </div>
            ))}
          </div>
        </Card>
      )}

      <Card title="Recent runs">
        {runs.loading ? (
          <Spinner />
        ) : (
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-ink-800 text-left text-ink-400">
                <th className="py-1">Phase</th>
                <th className="py-1">Started</th>
                <th className="py-1">OK</th>
                <th className="py-1">Counts</th>
              </tr>
            </thead>
            <tbody>
              {(runs.data ?? []).map((run) => (
                <tr key={String(run.id)} className="border-b border-ink-800 last:border-0">
                  <td className="py-1 text-ink-100">{String(run.phase)}</td>
                  <td className="py-1">{formatDateTime(String(run.started_at))}</td>
                  <td className={cx("py-1", run.ok ? "text-good" : "text-bad")}>
                    {run.ok ? "yes" : "no"}
                  </td>
                  <td className="py-1 font-mono text-ink-400">
                    {JSON.stringify(run.counts).slice(0, 90)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );
}
