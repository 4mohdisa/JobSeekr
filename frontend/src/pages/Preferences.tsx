// What the system learned about you, and your veto over all of it.
//
// The page exists because inference without visibility is just the search
// quietly narrowing for reasons nobody can see. Every row shows where it came
// from and whether it is actually in effect, and every row can be undone.
//
// Proposals sit at the top: they are the only rows that need a decision.

import { useMemo, useState } from "react";
import { api } from "../lib/api";
import { formatDate, useAsync } from "../lib/hooks";
import { DataTable, type Column } from "../components/DataTable";
import { Button, Card, ErrorNote, Input, Spinner, cx } from "../components/ui";
import type { Preference, PreferenceSource, PreferenceStatus } from "../lib/types";

const SOURCE_LABEL: Record<PreferenceSource, string> = {
  user_set: "you set this",
  asked: "you confirmed this",
  inferred: "guessed from behaviour",
};

const STATUS_LABEL: Record<PreferenceStatus, string> = {
  active: "in effect",
  proposed: "waiting on you",
  rejected: "declined",
  retired: "stopped asking",
};

function SourceBadge({ row }: { row: Preference }) {
  // Colour tracks trustworthiness, not status: a guess the system made is the
  // thing worth spotting at a glance.
  const tone =
    row.source === "inferred"
      ? "bg-amber-100 text-amber-900"
      : "bg-slate-100 text-slate-700";
  return (
    <span className={cx("rounded px-2 py-0.5 text-xs font-medium", tone)}>
      {SOURCE_LABEL[row.source]}
    </span>
  );
}

export function Preferences() {
  const { data, error, loading, reload } = useAsync(() => api.listPreferences(), []);
  const [busy, setBusy] = useState<number | null>(null);
  const [newKey, setNewKey] = useState("");
  const [newValue, setNewValue] = useState("");

  const rows = useMemo(() => {
    const order: Record<PreferenceStatus, number> = {
      proposed: 0,
      active: 1,
      rejected: 2,
      retired: 3,
    };
    return [...(data ?? [])].sort(
      (a, b) => order[a.status] - order[b.status] || a.key.localeCompare(b.key),
    );
  }, [data]);

  const proposals = rows.filter((row) => row.status === "proposed");

  const act = async (id: number, action: "confirm" | "reject" | "delete") => {
    setBusy(id);
    try {
      if (action === "confirm") await api.confirmPreference(id);
      else if (action === "reject") await api.rejectPreference(id);
      else await api.deletePreference(id);
      reload();
    } finally {
      setBusy(null);
    }
  };

  const addPreference = async () => {
    if (!newKey.trim()) return;
    await api.createPreference({ key: newKey.trim(), value: newValue.trim() });
    setNewKey("");
    setNewValue("");
    reload();
  };

  const columns: Column<Preference>[] = [
    {
      key: "key",
      header: "Preference",
      render: (row) => (
        <div>
          <div className="font-medium">{row.key}</div>
          <div className="text-sm text-slate-600">{row.value}</div>
          {row.evidence ? (
            <div className="mt-0.5 text-xs italic text-slate-500">{row.evidence}</div>
          ) : null}
        </div>
      ),
    },
    { key: "source", header: "Source", render: (row) => <SourceBadge row={row} /> },
    {
      key: "status",
      header: "Status",
      render: (row) => (
        <span className={cx(row.status === "active" ? "text-slate-900" : "text-slate-500")}>
          {STATUS_LABEL[row.status]}
        </span>
      ),
    },
    {
      key: "learned_at",
      header: "Learned",
      render: (row) => <span className="text-sm">{formatDate(row.learned_at)}</span>,
    },
    {
      key: "actions",
      header: "",
      render: (row) => (
        <div className="flex gap-2">
          {row.status === "proposed" ? (
            <>
              <Button
                onClick={() => act(row.id, "confirm")}
                disabled={busy === row.id}
              >
                Confirm
              </Button>
              <Button
                variant="secondary"
                onClick={() => act(row.id, "reject")}
                disabled={busy === row.id}
              >
                No
              </Button>
            </>
          ) : (
            <Button
              variant="secondary"
              onClick={() => act(row.id, "delete")}
              disabled={busy === row.id}
            >
              Delete
            </Button>
          )}
        </div>
      ),
    },
  ];

  if (loading) return <Spinner label="Loading preferences" />;

  return (
    <div className="space-y-4">
      <ErrorNote error={error} />

      {proposals.length > 0 ? (
        <Card>
          <p className="text-sm">
            <strong>{proposals.length}</strong>{" "}
            {proposals.length === 1 ? "pattern" : "patterns"} noticed and waiting on
            you. Nothing below changes what gets applied to until you confirm it.
          </p>
        </Card>
      ) : null}

      <Card title="Add a preference">
        {/* The only route by which a fact — work rights, a licence, a start
            date — ever gets a value. The system refuses to infer those. */}
        <div className="flex flex-wrap items-end gap-2">
          <Input
            placeholder="key, e.g. referral_source"
            value={newKey}
            onChange={(e) => setNewKey(e.target.value)}
          />
          <Input
            placeholder="value"
            value={newValue}
            onChange={(e) => setNewValue(e.target.value)}
          />
          <Button onClick={addPreference} disabled={!newKey.trim()}>
            Add
          </Button>
        </div>
      </Card>

      <DataTable rows={rows} columns={columns} empty="Nothing learned yet." />
    </div>
  );
}
