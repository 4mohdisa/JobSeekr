// The 90-second page.
//
// Every design choice here is about the stopwatch. A manual application means
// the user reading an ad, filling a form and pasting a letter; the dashboard's
// only job is to make sure they never have to go looking for anything.
//
//   - one card at a time, so there is nothing to scan past
//   - the job link, both PDFs and the letter are one click each
//   - every answer is a copy chip with a visible copied-state
//   - Done and Skip advance automatically to the next job
//   - a live timer, because a target you cannot see is not a target
//
// The whole card arrives in a single API call for the same reason.

import { useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";
import { useAsync, useClipboard, useElapsedSeconds } from "../lib/hooks";
import { ScoreBadge, StatusBadge } from "../components/StatusBadge";
import { Button, Card, Empty, ErrorNote, Spinner, cx } from "../components/ui";
import type { QueueCard } from "../lib/types";

const TARGET_SECONDS = 90;

function Timer({ running }: { running: boolean }) {
  const seconds = useElapsedSeconds(running);
  const over = seconds > TARGET_SECONDS;
  return (
    <span
      className={cx(
        "tnum rounded border px-2 py-0.5 text-xs",
        over
          ? "border-warn/40 bg-warn/10 text-warn"
          : "border-ink-700 bg-ink-850 text-ink-400",
      )}
      title={`Target is ${TARGET_SECONDS} seconds per manual application`}
    >
      {String(Math.floor(seconds / 60)).padStart(2, "0")}:
      {String(seconds % 60).padStart(2, "0")} / 1:30
    </span>
  );
}

function CopyChip({
  label,
  value,
  copiedKey,
  onCopy,
}: {
  label: string;
  value: string;
  copiedKey: string | null;
  onCopy: (value: string, key: string) => void;
}) {
  const key = `${label}:${value}`;
  const isCopied = copiedKey === key;
  return (
    <button
      type="button"
      onClick={() => onCopy(value, key)}
      className={cx(
        "group flex w-full items-start gap-2 rounded border px-2 py-1.5 text-left text-xs transition-colors",
        isCopied
          ? "border-good/50 bg-good/10 text-good"
          : "border-ink-700 bg-ink-850 hover:border-accent hover:bg-ink-800",
      )}
      title="Click to copy"
    >
      <span className="min-w-0 flex-1">
        <span className="block truncate text-ink-400">{label}</span>
        <span className="block font-medium text-ink-100">{value}</span>
      </span>
      <span className={cx("shrink-0 pt-3", isCopied ? "text-good" : "text-ink-600")}>
        {isCopied ? "copied" : "copy"}
      </span>
    </button>
  );
}

export function Queue() {
  const { data, error, loading, reload } = useAsync(() => api.getQueue({ limit: 50 }), []);
  const [index, setIndex] = useState(0);
  const [busy, setBusy] = useState(false);
  const { copied, copy } = useClipboard();

  const cards = useMemo(() => data ?? [], [data]);
  const card: QueueCard | undefined = cards[index];

  const advance = () => setIndex((i) => Math.min(i + 1, Math.max(cards.length - 1, 0)));

  const act = async (action: "done" | "skip") => {
    if (!card) return;
    setBusy(true);
    try {
      if (action === "done") await api.queueDone(card.job.id);
      else await api.queueSkip(card.job.id);
      advance();
    } finally {
      setBusy(false);
    }
  };

  // Keyboard first: the point is not to reach for the mouse between jobs.
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (event.target instanceof HTMLInputElement) return;
      if (event.target instanceof HTMLTextAreaElement) return;
      if (event.key === "j" || event.key === "ArrowRight") advance();
      if (event.key === "k" || event.key === "ArrowLeft") setIndex((i) => Math.max(0, i - 1));
      if (event.key === "Enter" && card) void act("done");
      if (event.key === "s" && card) void act("skip");
      if (event.key === "o" && card) window.open(card.apply_url, "_blank", "noopener");
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [card, cards.length]);

  if (loading) return <Spinner label="Loading the queue" />;

  return (
    <div className="mx-auto max-w-5xl">
      <ErrorNote error={error} />

      <div className="mb-3 flex items-center gap-3">
        <h1 className="text-lg font-semibold">Manual queue</h1>
        <span className="tnum text-sm text-ink-400">
          {cards.length === 0 ? "0" : `${index + 1} / ${cards.length}`}
        </span>
        {card && <Timer running key={card.job.id} />}
        <div className="ml-auto flex items-center gap-2">
          <Button variant="quiet" onClick={reload}>
            Refresh
          </Button>
        </div>
      </div>

      <p className="mb-4 text-xs text-ink-600">
        Keys: <kbd className="text-ink-400">o</kbd> open ad ·{" "}
        <kbd className="text-ink-400">Enter</kbd> done ·{" "}
        <kbd className="text-ink-400">s</kbd> skip · <kbd className="text-ink-400">j/k</kbd>{" "}
        next / previous
      </p>

      {!card ? (
        <Empty>
          Nothing waiting. Jobs land here when a campaign&rsquo;s gray-zone action is
          &ldquo;queue&rdquo;, when a screening question could not be answered, or when a
          listing turns out to be manual-only.
        </Empty>
      ) : (
        <div className="grid gap-4 lg:grid-cols-[1.3fr_1fr]">
          <div className="space-y-4">
            <Card
              title={
                <span className="flex items-center gap-2">
                  <ScoreBadge score={card.score} />
                  <span className="truncate">{card.job.title}</span>
                </span>
              }
              actions={
                <>
                  <StatusBadge status={card.job.status} />
                  <StatusBadge status={card.job.apply_type} kind="apply_type" />
                </>
              }
            >
              <div className="mb-3 text-sm text-ink-300">
                <div className="font-medium text-ink-100">{card.job.company}</div>
                <div className="text-ink-400">{card.job.location ?? "location not stated"}</div>
              </div>

              <div className="flex flex-wrap gap-2">
                <a href={card.apply_url} target="_blank" rel="noopener noreferrer">
                  <Button variant="primary">Open the ad ↗</Button>
                </a>
                {card.resume_document_id && (
                  <a
                    href={api.documentUrl(card.resume_document_id)}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    <Button>Resume PDF</Button>
                  </a>
                )}
                {card.cover_letter_document_id && (
                  <a
                    href={api.documentUrl(card.cover_letter_document_id)}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    <Button>Cover letter PDF</Button>
                  </a>
                )}
                {card.combined_document_id && (
                  <a
                    href={api.documentUrl(card.combined_document_id)}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    <Button>Combined PDF</Button>
                  </a>
                )}
              </div>

              {card.reasoning && (
                <p className="mt-3 border-t border-ink-800 pt-3 text-xs text-ink-400">
                  {card.reasoning}
                </p>
              )}
            </Card>

            <Card
              title="Cover letter"
              actions={
                <Button
                  onClick={() => copy(card.cover_letter_text, `letter:${card.job.id}`)}
                  variant={copied === `letter:${card.job.id}` ? "primary" : "ghost"}
                >
                  {copied === `letter:${card.job.id}` ? "Copied" : "Copy all"}
                </Button>
              }
            >
              {card.cover_letter_text ? (
                <pre className="max-h-72 overflow-auto text-xs whitespace-pre-wrap text-ink-300">
                  {card.cover_letter_text}
                </pre>
              ) : (
                <p className="text-xs text-ink-600">
                  No cover letter text yet — build the documents for this job first.
                </p>
              )}
            </Card>
          </div>

          <div className="space-y-4">
            <Card title="Answers — tap to copy">
              <div className="space-y-1.5">
                {card.answers.length === 0 && (
                  <p className="text-xs text-ink-600">No answers saved yet.</p>
                )}
                {card.answers.map((answer) => (
                  <CopyChip
                    key={answer.question}
                    label={answer.question}
                    value={answer.value}
                    copiedKey={copied}
                    onCopy={copy}
                  />
                ))}
              </div>

              {card.unanswered_questions.length > 0 && (
                <div className="mt-3 rounded border border-warn/40 bg-warn/10 p-2">
                  <div className="mb-1 text-xs font-semibold text-warn">
                    Unanswered — fill these in on the Answer bank page
                  </div>
                  <ul className="space-y-0.5 text-xs text-warn/90">
                    {card.unanswered_questions.map((question) => (
                      <li key={question}>· {question}</li>
                    ))}
                  </ul>
                </div>
              )}
            </Card>

            <div className="flex gap-2">
              <Button
                variant="primary"
                className="flex-1 py-2"
                disabled={busy}
                onClick={() => act("done")}
              >
                Done — applied
              </Button>
              <Button className="flex-1 py-2" disabled={busy} onClick={() => act("skip")}>
                Skip
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
