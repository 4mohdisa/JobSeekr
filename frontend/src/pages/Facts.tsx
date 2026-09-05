// Layer 1: what is true about you, in your own words.
//
// Plain textareas on purpose. A licence is not a boolean — it has a state, a
// class, a date and possibly conditions — and any set of structured fields is
// a set the next form asks a question outside of. Prose holds everything, and
// the derivation layer turns it into a Yes when a form needs one.
//
// Nothing here rewrites what you type. The text is stored verbatim because it
// is the evidence every derived answer is checked against.

import { useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";
import { formatDateTime, useAsync } from "../lib/hooks";
import { Button, Card, ErrorNote, Select, Spinner, Textarea, cx } from "../components/ui";
import type { DerivedAnswer, Fact } from "../lib/types";

// The prompt shown above each textarea. Mirrors backend/seed.py FACT_SHELLS —
// deliberately not stored as the fact text, which would be a fabricated fact
// sitting in the one place the system treats as verbatim truth.
const PROMPTS: Record<string, string> = {
  work_rights:
    "Your right to work: citizenship or visa, any conditions, whether you need sponsorship.",
  licence:
    "Driver's or other licences: which state issued it, what class, how long you have held it, any restrictions.",
  checks:
    "Police checks, working-with-children checks, security clearances — which ones you hold and when they were issued.",
  education: "Your highest qualification, where and when, plus anything else relevant.",
  experience: "Years of experience, and in what. Write it the way you would say it.",
  availability:
    "Notice period, earliest start date, and whether you will relocate, travel, or work weekends and shifts.",
  compensation: "Salary or rate expectations, and whether they are negotiable.",
  transport: "Whether you have your own reliable transport.",
  referees: "Whether you can provide contactable referees.",
  health:
    "Anything about medicals, drug tests or vaccination status you are willing to declare.",
  business: "ABN, company, or contracting arrangements, if you have any.",
};

const JURISDICTIONS = [
  { value: "", label: "Everywhere" },
  { value: "AU", label: "Australia only" },
  { value: "NZ", label: "New Zealand only" },
];

function DerivedList({
  rows,
  onDecide,
}: {
  rows: DerivedAnswer[];
  onDecide: (id: number, confirm: boolean) => void;
}) {
  if (rows.length === 0) return null;
  return (
    <div className="mt-3 space-y-1 border-t border-slate-200 pt-2">
      <p className="text-xs font-medium uppercase tracking-wide text-slate-500">
        Answers derived from this fact
      </p>
      {rows.map((row) => (
        <div key={row.id} className="flex items-start gap-2 text-sm">
          <span
            className={cx(
              "mt-0.5 shrink-0 rounded px-1.5 py-0.5 text-xs font-medium",
              row.stale
                ? "bg-amber-100 text-amber-900"
                : row.confirmed_at
                  ? "bg-emerald-100 text-emerald-900"
                  : "bg-slate-100 text-slate-700",
            )}
          >
            {row.stale ? "needs re-checking" : row.confirmed_at ? "in use" : "waiting"}
          </span>
          <span className="flex-1">
            <span className="text-slate-700">{row.question_text}</span>
            <span className="font-medium"> → {row.answer_value}</span>
            {row.reasoning ? (
              <span className="block text-xs italic text-slate-500">{row.reasoning}</span>
            ) : null}
          </span>
          {!row.confirmed_at || row.stale ? (
            <span className="flex shrink-0 gap-1">
              <Button onClick={() => onDecide(row.id, true)}>Yes</Button>
              <Button variant="ghost" onClick={() => onDecide(row.id, false)}>
                No
              </Button>
            </span>
          ) : null}
        </div>
      ))}
    </div>
  );
}

export function Facts() {
  const { data, error, loading, reload } = useAsync(() => api.listFacts(), []);
  const derived = useAsync(() => api.listDerived(), []);

  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [zones, setZones] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState<string | null>(null);

  // Seed the editors from the server once, then leave them alone — re-syncing
  // on every reload would discard whatever the user is part way through typing.
  useEffect(() => {
    if (!data) return;
    setDrafts((current) =>
      Object.keys(current).length ? current : Object.fromEntries(data.map((f) => [f.key, f.text])),
    );
    setZones((current) =>
      Object.keys(current).length
        ? current
        : Object.fromEntries(data.map((f) => [f.key, f.jurisdiction ?? ""])),
    );
  }, [data]);

  const byFact = useMemo(() => {
    const map: Record<number, DerivedAnswer[]> = {};
    for (const row of derived.data ?? []) {
      if (row.fact_id == null) continue;
      (map[row.fact_id] ??= []).push(row);
    }
    return map;
  }, [derived.data]);

  const save = async (fact: Fact) => {
    setSaving(fact.key);
    try {
      await api.updateFact(fact.key, {
        text: drafts[fact.key] ?? "",
        jurisdiction: zones[fact.key] || null,
      });
      reload();
      derived.reload();
    } finally {
      setSaving(null);
    }
  };

  const decide = async (id: number, confirm: boolean) => {
    if (confirm) await api.confirmDerived(id);
    else await api.rejectDerived(id);
    derived.reload();
  };

  if (loading) return <Spinner label="Loading facts" />;

  const facts = data ?? [];
  const blank = facts.filter((f) => !f.text.trim()).length;
  const waiting = (derived.data ?? []).filter((r) => !r.confirmed_at || r.stale).length;

  return (
    <div className="space-y-4">
      <ErrorNote error={error} />

      <Card>
        <p className="text-sm">
          Write these in prose, the way you would say them out loud. Stored exactly as
          typed — everything the system tells an employer is checked against these words.
          {blank > 0 ? (
            <>
              {" "}
              <strong>{blank}</strong> still blank; a blank fact answers nothing.
            </>
          ) : null}
          {waiting > 0 ? (
            <>
              {" "}
              <strong>{waiting}</strong> derived {waiting === 1 ? "answer" : "answers"} need
              a yes or no.
            </>
          ) : null}
        </p>
      </Card>

      {facts.map((fact) => {
        const dirty = (drafts[fact.key] ?? "") !== fact.text
          || (zones[fact.key] ?? "") !== (fact.jurisdiction ?? "");
        return (
          <Card key={fact.key} title={fact.key.replace(/_/g, " ")}>
            <p className="mb-2 text-sm text-slate-600">{PROMPTS[fact.key] ?? ""}</p>
            <Textarea
              rows={3}
              value={drafts[fact.key] ?? ""}
              placeholder="Nothing recorded yet."
              onChange={(e) =>
                setDrafts((current) => ({ ...current, [fact.key]: e.target.value }))
              }
            />
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <Select
                value={zones[fact.key] ?? ""}
                onChange={(e) =>
                  setZones((current) => ({ ...current, [fact.key]: e.target.value }))
                }
              >
                {JURISDICTIONS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </Select>
              <Button onClick={() => save(fact)} disabled={!dirty || saving === fact.key}>
                {saving === fact.key ? "Saving" : "Save"}
              </Button>
              <span className="text-xs text-slate-500">
                {fact.text.trim() ? `updated ${formatDateTime(fact.updated_at)}` : "blank"}
              </span>
              {dirty ? (
                <span className="text-xs text-amber-700">
                  editing this will re-check anything derived from it
                </span>
              ) : null}
            </div>
            <DerivedList rows={byFact[fact.id] ?? []} onDecide={decide} />
          </Card>
        );
      })}
    </div>
  );
}
