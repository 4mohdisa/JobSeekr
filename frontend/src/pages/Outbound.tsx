// Follow-up emails, drafted and waiting on you.
//
// Nothing here sends on its own. A draft is written after an application is
// confirmed sent, and only if the ad published an address — addresses are never
// guessed or looked up, which is one of the three properties that make this
// defensible under the Spam Act.
//
// The recipient is shown and is not editable. That is deliberate: the API has
// no field for it, because a recipient parameter is exactly what the outbound
// module refuses to have.

import { useState } from "react";
import { api } from "../lib/api";
import { formatDateTime, useAsync } from "../lib/hooks";
import { Button, Card, ErrorNote, Input, Spinner, Textarea, cx } from "../components/ui";
import type { OutboundMessage, OutboundStatus } from "../lib/types";

const TONE: Record<OutboundStatus, string> = {
  drafted: "bg-amber-100 text-amber-900",
  sent: "bg-emerald-100 text-emerald-900",
  skipped: "bg-slate-100 text-slate-600",
};

export function Outbound() {
  const { data, error, loading, reload } = useAsync(() => api.listOutbound(), []);
  const [edits, setEdits] = useState<Record<number, { subject: string; body: string }>>({});
  const [busy, setBusy] = useState<number | null>(null);
  const [failure, setFailure] = useState<string>("");

  const rows = data ?? [];
  const drafts = rows.filter((row) => row.status === "drafted");

  const draftOf = (row: OutboundMessage) =>
    edits[row.id] ?? { subject: row.subject, body: row.body };

  const act = async (row: OutboundMessage, action: "send" | "skip" | "save") => {
    setBusy(row.id);
    setFailure("");
    try {
      if (action === "save") {
        await api.editOutbound(row.id, draftOf(row));
      } else if (action === "skip") {
        await api.skipOutbound(row.id);
      } else {
        // Save any pending edit first, so Send never sends the stale text the
        // user has just finished rewriting.
        if (edits[row.id]) await api.editOutbound(row.id, draftOf(row));
        await api.sendOutbound(row.id, "dashboard");
      }
      setEdits((current) => {
        const next = { ...current };
        delete next[row.id];
        return next;
      });
      reload();
    } catch (err) {
      // A refusal is the interesting case — OUTBOUND_ENABLED being off, or a
      // job that has already had its one message. Surfacing the reason is the
      // difference between "nothing happened" and knowing why.
      setFailure(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  };

  if (loading) return <Spinner label="Loading drafts" />;

  return (
    <div className="space-y-4">
      <ErrorNote error={error} />
      {failure ? (
        <Card>
          <p className="text-sm text-red-800">{failure}</p>
        </Card>
      ) : null}

      <Card>
        <p className="text-sm">
          {drafts.length > 0 ? (
            <>
              <strong>{drafts.length}</strong> {drafts.length === 1 ? "draft" : "drafts"}{" "}
              waiting. Nothing is sent until you press Send.
            </>
          ) : (
            <>No drafts waiting. A follow-up is written only after an application is
            confirmed sent, and only when the ad published an address.</>
          )}{" "}
          One message per job, ever — no follow-ups, no sequences.
        </p>
      </Card>

      {rows.map((row) => {
        const editing = draftOf(row);
        const dirty =
          editing.subject !== row.subject || editing.body !== row.body;
        const open = row.status === "drafted";

        return (
          <Card key={row.id} title={`Job ${row.job_id}`}>
            <div className="mb-2 flex flex-wrap items-center gap-2 text-sm">
              <span className={cx("rounded px-2 py-0.5 text-xs font-medium", TONE[row.status])}>
                {row.status}
              </span>
              <span className="text-slate-600">
                to <strong>{row.to_address}</strong> (from the ad)
              </span>
              {row.sent_at ? (
                <span className="text-xs text-slate-500">
                  sent {formatDateTime(row.sent_at)}
                  {row.approved_by ? ` · approved by ${row.approved_by}` : ""}
                </span>
              ) : null}
            </div>

            <Input
              value={editing.subject}
              disabled={!open}
              onChange={(e) =>
                setEdits((current) => ({
                  ...current,
                  [row.id]: { ...editing, subject: e.target.value },
                }))
              }
            />
            <Textarea
              className="mt-2"
              rows={8}
              value={editing.body}
              disabled={!open}
              onChange={(e) =>
                setEdits((current) => ({
                  ...current,
                  [row.id]: { ...editing, body: e.target.value },
                }))
              }
            />

            <p className="mt-2 text-xs text-slate-600">
              Attachments:{" "}
              {row.attachments.length ? row.attachments.join(", ") : "none"}
            </p>

            {open ? (
              <div className="mt-3 flex gap-2">
                <Button onClick={() => act(row, "send")} disabled={busy === row.id}>
                  Send
                </Button>
                <Button
                  variant="ghost"
                  onClick={() => act(row, "save")}
                  disabled={busy === row.id || !dirty}
                >
                  Save edit
                </Button>
                <Button
                  variant="ghost"
                  onClick={() => act(row, "skip")}
                  disabled={busy === row.id}
                >
                  Skip
                </Button>
              </div>
            ) : null}
          </Card>
        );
      })}
    </div>
  );
}
