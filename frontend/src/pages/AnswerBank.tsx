// The answer bank. Blanks are the most important thing on this page.
//
// An unanswered question is why a job gets parked mid-application, so the
// blanks are pulled to the top, counted in a banner, and highlighted — the
// page is a to-do list first and a reference table second.

import { useMemo, useState } from "react";
import { api } from "../lib/api";
import { formatDate, useAsync } from "../lib/hooks";
import { DataTable, type Column } from "../components/DataTable";
import { Button, Card, ErrorNote, Input, Select, Spinner, cx } from "../components/ui";
import type { Answer, AnswerType, MatchType } from "../lib/types";

const MATCH_TYPES: MatchType[] = ["exact", "regex", "fuzzy"];
const ANSWER_TYPES: AnswerType[] = ["text", "boolean", "choice", "number", "date"];

export function AnswerBank() {
  const { data, error, loading, reload } = useAsync(() => api.listAnswers(), []);
  const [edits, setEdits] = useState<Record<number, string>>({});
  const [saving, setSaving] = useState(false);
  const [onlyBlank, setOnlyBlank] = useState(false);

  const rows = useMemo(() => {
    const all = data ?? [];
    const visible = onlyBlank ? all.filter((row) => !row.answer_value.trim()) : all;
    // Blanks first: they are the ones costing applications.
    return [...visible].sort((a, b) => {
      const left = a.answer_value.trim() ? 1 : 0;
      const right = b.answer_value.trim() ? 1 : 0;
      return left - right || a.question_pattern.localeCompare(b.question_pattern);
    });
  }, [data, onlyBlank]);

  const blanks = (data ?? []).filter((row) => !row.answer_value.trim()).length;
  const dirty = Object.keys(edits).length;

  const saveAll = async () => {
    setSaving(true);
    try {
      await api.bulkAnswers(edits);
      setEdits({});
      reload();
    } finally {
      setSaving(false);
    }
  };

  const patch = async (row: Answer, changes: Partial<Answer>) => {
    await api.updateAnswer(row.id, {
      question_pattern: row.question_pattern,
      match_type: row.match_type,
      answer_value: row.answer_value,
      answer_type: row.answer_type,
      campaign_id: row.campaign_id,
      choices: row.choices,
      notes: row.notes,
      ...changes,
    });
    reload();
  };

  const columns: Column<Answer>[] = [
    {
      key: "question_pattern",
      header: "Question",
      render: (row) => (
        <div>
          <div className={cx("text-sm", row.answer_value.trim() ? "text-ink-100" : "text-warn")}>
            {row.question_pattern}
          </div>
          {row.notes && <div className="text-xs text-ink-600">{row.notes}</div>}
        </div>
      ),
      sortValue: (row) => row.question_pattern,
    },
    {
      key: "answer_value",
      header: "Answer",
      width: "26%",
      // A row that came from a dropdown carries the site's own option set, so
      // it is edited as a dropdown. Typing free text into one of these is the
      // exact failure the option capture exists to prevent: the answer looks
      // right, matches no option, and fails silently at submit.
      render: (row) =>
        row.choices && row.choices.length > 0 ? (
          <Select
            value={edits[row.id] ?? row.answer_value}
            onChange={(event) =>
              setEdits((current) => ({ ...current, [row.id]: event.target.value }))
            }
            className={cx(
              !(edits[row.id] ?? row.answer_value).trim() && "border-warn/50 bg-warn/5",
            )}
          >
            <option value="">— unanswered —</option>
            {row.choices.map((choice) => (
              <option key={choice.value} value={choice.value}>
                {choice.label}
              </option>
            ))}
          </Select>
        ) : (
          <Input
            value={edits[row.id] ?? row.answer_value}
            placeholder="— unanswered —"
            onChange={(event) =>
              setEdits((current) => ({ ...current, [row.id]: event.target.value }))
            }
            className={cx(
              !(edits[row.id] ?? row.answer_value).trim() && "border-warn/50 bg-warn/5",
            )}
          />
        ),
      sortValue: (row) => (row.answer_value.trim() ? 1 : 0),
    },
    {
      key: "match_type",
      header: "Match",
      width: "7rem",
      render: (row) => (
        <Select
          value={row.match_type}
          onChange={(event) => patch(row, { match_type: event.target.value as MatchType })}
        >
          {MATCH_TYPES.map((value) => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
        </Select>
      ),
    },
    {
      key: "answer_type",
      header: "Type",
      width: "7rem",
      render: (row) => (
        <Select
          value={row.answer_type}
          onChange={(event) => patch(row, { answer_type: event.target.value as AnswerType })}
        >
          {ANSWER_TYPES.map((value) => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
        </Select>
      ),
    },
    {
      key: "scope",
      header: "Scope",
      render: (row) => (row.campaign_id ? `campaign ${row.campaign_id}` : "global"),
      sortValue: (row) => row.campaign_id ?? 0,
    },
    {
      key: "verified_at",
      header: "Verified",
      render: (row) =>
        row.verified_at ? (
          <span className="text-good">{formatDate(row.verified_at)}</span>
        ) : (
          <button
            className="text-xs text-ink-400 underline hover:text-ink-100"
            onClick={() => patch(row, { verified: true } as Partial<Answer>)}
          >
            mark verified
          </button>
        ),
      sortValue: (row) => row.verified_at,
    },
  ];

  return (
    <div>
      <h1 className="mb-3 text-lg font-semibold">Answer bank</h1>
      <ErrorNote error={error} />

      {blanks > 0 && (
        <div className="mb-3 rounded border border-warn/40 bg-warn/10 px-3 py-2 text-sm text-warn">
          <strong>{blanks}</strong> unanswered {blanks === 1 ? "question" : "questions"}. An
          application that hits one of these is parked rather than guessed at — the system
          never invents an answer on your behalf.
        </div>
      )}

      <Card
        actions={
          <>
            <Button variant={onlyBlank ? "primary" : "ghost"} onClick={() => setOnlyBlank((v) => !v)}>
              {onlyBlank ? "Showing blanks" : "Show blanks only"}
            </Button>
            <Button variant="primary" onClick={saveAll} disabled={!dirty || saving}>
              {saving ? "Saving…" : dirty ? `Save ${dirty}` : "Save"}
            </Button>
          </>
        }
      >
        {loading ? (
          <Spinner />
        ) : (
          <DataTable
            rows={rows}
            columns={columns}
            rowKey={(row) => row.id}
            searchable={(row) => `${row.question_pattern} ${row.answer_value}`}
            searchPlaceholder="Search questions…"
            pageSize={100}
            empty="No answers yet. Run `uv run python -m backend.seed` to load the standard set."
          />
        )}
      </Card>
    </div>
  );
}
