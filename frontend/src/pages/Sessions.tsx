// Which sites are signed in, and when that was last actually confirmed.
//
// This page exists because session expiry is the silent failure: the adapter
// lands on a login page, cannot find the form, parks the job, and the symptom
// is a pile of parked jobs several days later with nothing naming the cause.
//
// Two timestamps, not one. "Checked a minute ago, last confirmed good four days
// ago" is a session that has been dead for four days, and a single "last
// checked" column cannot say that.

import { api } from "../lib/api";
import { formatDateTime, formatRelative, useAsync } from "../lib/hooks";
import { DataTable, type Column } from "../components/DataTable";
import { Card, ErrorNote, Spinner, cx } from "../components/ui";
import type { SessionHealth, SessionStatus } from "../lib/types";

const LABEL: Record<SessionStatus, string> = {
  live: "signed in",
  dead: "signed out",
  unknown: "cannot tell",
  no_session: "never signed in",
  unreachable: "could not check",
};

const TONE: Record<SessionStatus, string> = {
  live: "bg-emerald-100 text-emerald-900",
  dead: "bg-red-100 text-red-900",
  unknown: "bg-amber-100 text-amber-900",
  no_session: "bg-slate-100 text-slate-600",
  unreachable: "bg-amber-100 text-amber-900",
};

export function Sessions() {
  const { data, error, loading } = useAsync(() => api.listSessions(), []);

  const columns: Column<SessionHealth>[] = [
    {
      key: "site",
      header: "Site",
      render: (row) => (
        <div>
          <div className="font-medium">{row.site}</div>
          {row.detail ? (
            <div className="text-xs text-slate-500">{row.detail}</div>
          ) : null}
        </div>
      ),
    },
    {
      key: "status",
      header: "Status",
      render: (row) => (
        <span
          className={cx("rounded px-2 py-0.5 text-xs font-medium", TONE[row.status])}
        >
          {LABEL[row.status]}
        </span>
      ),
    },
    {
      key: "last_verified_at",
      header: "Last confirmed good",
      render: (row) =>
        row.last_verified_at ? (
          <span title={formatDateTime(row.last_verified_at)}>
            {formatRelative(row.last_verified_at)}
          </span>
        ) : (
          <span className="text-slate-500">never</span>
        ),
    },
    {
      key: "last_checked_at",
      header: "Last checked",
      render: (row) =>
        row.last_checked_at ? (
          <span className="text-sm">{formatRelative(row.last_checked_at)}</span>
        ) : (
          <span className="text-slate-500">—</span>
        ),
    },
  ];

  if (loading) return <Spinner label="Loading sessions" />;

  const rows = data ?? [];
  const dead = rows.filter((row) => row.status === "dead");

  return (
    <div className="space-y-4">
      <ErrorNote error={error} />

      {dead.length > 0 ? (
        <Card>
          <p className="text-sm">
            <strong>
              {dead.length} {dead.length === 1 ? "site is" : "sites are"} signed out.
            </strong>{" "}
            Nothing will be submitted to {dead.map((row) => row.site).join(", ")} until you
            sign in. Nothing signs in automatically.
          </p>
          <pre className="mt-2 overflow-x-auto rounded bg-slate-100 p-2 text-xs">
            {dead
              .map(
                (row) =>
                  `uv run python -m backend.apply.session login --platform ${row.site}`,
              )
              .join("\n")}
          </pre>
        </Card>
      ) : null}

      <DataTable
        rows={rows}
        columns={columns}
        empty="No sessions checked yet — the check runs at 09:00 and before each apply pass."
      />
    </div>
  );
}
