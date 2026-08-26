// The single source of truth for what a status looks like.
//
// Every page that shows a job, application or response status uses this. When
// colour lives in each page instead, "failed" ends up red on one screen and
// grey on another, and the operator learns to distrust the colour.

import type {
  ApplicationOutcome,
  ApplyType,
  JobStatus,
  ResponseStatus,
} from "../lib/types";
import { cx } from "./ui";

type Tone = "neutral" | "info" | "good" | "warn" | "bad" | "muted";

const TONE_CLASS: Record<Tone, string> = {
  neutral: "bg-ink-800 text-ink-300 border-ink-700",
  info: "bg-accent/15 text-accent border-accent/30",
  good: "bg-good/15 text-good border-good/30",
  warn: "bg-warn/15 text-warn border-warn/30",
  bad: "bg-bad/15 text-bad border-bad/30",
  muted: "bg-ink-850 text-ink-600 border-ink-800",
};

const JOB_STATUS: Record<JobStatus, { label: string; tone: Tone }> = {
  discovered: { label: "Discovered", tone: "neutral" },
  scored: { label: "Scored", tone: "info" },
  rejected: { label: "Rejected", tone: "muted" },
  queued: { label: "Queued", tone: "info" },
  documents_ready: { label: "Docs ready", tone: "info" },
  // Loud on purpose: a parked job is waiting on the user, and it stays parked
  // until they answer.
  needs_answer: { label: "Needs answer", tone: "warn" },
  applying: { label: "Applying", tone: "info" },
  applied: { label: "Applied", tone: "good" },
  failed: { label: "Failed", tone: "bad" },
  manual_queue: { label: "Manual queue", tone: "warn" },
  skipped: { label: "Skipped", tone: "muted" },
  ghosted: { label: "Ghosted", tone: "muted" },
};

const RESPONSE_STATUS: Record<ResponseStatus, { label: string; tone: Tone }> = {
  none: { label: "No reply", tone: "muted" },
  acknowledged: { label: "Acknowledged", tone: "neutral" },
  rejected: { label: "Rejected", tone: "bad" },
  interview_request: { label: "Interview", tone: "good" },
  recruiter_outreach: { label: "Recruiter", tone: "info" },
  ghosted: { label: "Ghosted", tone: "muted" },
};

const OUTCOME: Record<ApplicationOutcome, { label: string; tone: Tone }> = {
  submitted: { label: "Submitted", tone: "good" },
  failed: { label: "Failed", tone: "bad" },
  aborted: { label: "Aborted", tone: "warn" },
};

const APPLY_TYPE: Record<ApplyType, { label: string; tone: Tone }> = {
  quick_apply: { label: "Quick Apply", tone: "info" },
  easy_apply: { label: "Easy Apply", tone: "info" },
  external: { label: "External", tone: "neutral" },
  unknown: { label: "Unknown", tone: "muted" },
  manual_only: { label: "Manual only", tone: "warn" },
};

export function StatusBadge({
  status,
  kind = "job",
  className,
}: {
  status: string;
  kind?: "job" | "response" | "outcome" | "apply_type";
  className?: string;
}) {
  const table =
    kind === "response"
      ? RESPONSE_STATUS
      : kind === "outcome"
        ? OUTCOME
        : kind === "apply_type"
          ? APPLY_TYPE
          : JOB_STATUS;

  const entry = (table as Record<string, { label: string; tone: Tone }>)[status] ?? {
    label: status,
    tone: "neutral" as Tone,
  };

  return (
    <span
      className={cx(
        "inline-flex items-center rounded border px-1.5 py-0.5 text-xs whitespace-nowrap",
        TONE_CLASS[entry.tone],
        className,
      )}
    >
      {entry.label}
    </span>
  );
}

/** Score colouring, shared so a 91 is the same green everywhere. */
export function ScoreBadge({ score }: { score: number | null | undefined }) {
  if (score === null || score === undefined) {
    return <span className="text-ink-600">—</span>;
  }
  const tone: Tone = score >= 80 ? "good" : score >= 60 ? "info" : score >= 40 ? "warn" : "muted";
  return (
    <span
      className={cx(
        "tnum inline-flex min-w-[2.5rem] items-center justify-center rounded border px-1.5 py-0.5 text-xs font-semibold",
        TONE_CLASS[tone],
      )}
    >
      {score.toFixed(0)}
    </span>
  );
}
