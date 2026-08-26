import { useState } from "react";
import { api } from "../lib/api";
import { formatRelative, formatSalary, useAsync } from "../lib/hooks";
import { DataTable, type Column } from "../components/DataTable";
import { ScoreBadge, StatusBadge } from "../components/StatusBadge";
import { Button, Card, ErrorNote, Select, Spinner } from "../components/ui";
import type { Job, JobStatus } from "../lib/types";

const STATUSES: JobStatus[] = [
  "discovered",
  "scored",
  "rejected",
  "queued",
  "documents_ready",
  "needs_answer",
  "applied",
  "failed",
  "manual_queue",
  "skipped",
  "ghosted",
];

function JobExpansion({ jobId }: { jobId: number }) {
  const { data, loading } = useAsync(() => api.getJob(jobId), [jobId]);
  if (loading) return <Spinner label="Loading" />;
  if (!data) return null;

  return (
    <div className="grid gap-4 md:grid-cols-2">
      <div>
        {data.score_detail ? (
          <>
            <p className="mb-2 text-sm text-ink-300">{data.score_detail.reasoning}</p>
            <div className="space-y-1 text-xs">
              <div>
                <span className="text-ink-400">Matched: </span>
                <span className="text-good">
                  {data.score_detail.matched_skills.join(", ") || "—"}
                </span>
              </div>
              <div>
                <span className="text-ink-400">Gaps: </span>
                <span className="text-warn">{data.score_detail.gaps.join(", ") || "—"}</span>
              </div>
              <div>
                <span className="text-ink-400">Red flags: </span>
                <span className="text-bad">
                  {data.score_detail.red_flags.join(", ") || "none"}
                </span>
              </div>
              <div className="text-ink-600">
                rubric v{data.score_detail.rubric_version} · profile v
                {data.score_detail.profile_version}
              </div>
            </div>
          </>
        ) : (
          <p className="text-xs text-ink-600">Not scored yet.</p>
        )}
      </div>

      <div>
        <div className="mb-2 flex flex-wrap gap-2">
          <a href={data.url} target="_blank" rel="noopener noreferrer">
            <Button>Open ad ↗</Button>
          </a>
          {data.documents.map((document) => (
            <a
              key={document.id}
              href={api.documentUrl(document.id)}
              target="_blank"
              rel="noopener noreferrer"
            >
              <Button variant={document.parse_check_passed ? "ghost" : "quiet"}>
                {document.kind}
                {!document.parse_check_passed && " (gate failed)"}
              </Button>
            </a>
          ))}
        </div>
        {data.description && (
          <pre className="max-h-56 overflow-auto rounded border border-ink-800 bg-ink-950 p-2 text-xs whitespace-pre-wrap text-ink-400">
            {data.description}
          </pre>
        )}
      </div>
    </div>
  );
}

export function Jobs() {
  const [status, setStatus] = useState<string>("");
  const [source, setSource] = useState<string>("");
  const [minScore, setMinScore] = useState<string>("");

  const { data, error, loading } = useAsync(
    () =>
      api.listJobs({
        limit: 500,
        status: status || undefined,
        source: source || undefined,
        min_score: minScore || undefined,
      }),
    [status, source, minScore],
  );

  const columns: Column<Job>[] = [
    {
      key: "score",
      header: "Score",
      width: "5rem",
      render: (job) => <ScoreBadge score={job.score} />,
      sortValue: (job) => job.score,
    },
    {
      key: "title",
      header: "Title",
      render: (job) => <span className="font-medium text-ink-100">{job.title}</span>,
      sortValue: (job) => job.title,
    },
    { key: "company", header: "Company", sortValue: (job) => job.company },
    {
      key: "location",
      header: "Location",
      render: (job) => job.location ?? "—",
      sortValue: (job) => job.location,
    },
    {
      key: "salary",
      header: "Salary",
      render: (job) =>
        formatSalary(job.salary_min, job.salary_max, job.salary_basis, job.salary_is_estimated),
      sortValue: (job) => job.salary_max ?? job.salary_min,
    },
    { key: "source", header: "Source", sortValue: (job) => job.source },
    {
      key: "status",
      header: "Status",
      render: (job) => <StatusBadge status={job.status} />,
      sortValue: (job) => job.status,
    },
    {
      key: "discovered_at",
      header: "Found",
      render: (job) => formatRelative(job.discovered_at),
      sortValue: (job) => job.discovered_at,
    },
  ];

  return (
    <div>
      <h1 className="mb-3 text-lg font-semibold">Jobs</h1>
      <ErrorNote error={error} />

      <Card>
        {loading ? (
          <Spinner />
        ) : (
          <DataTable
            rows={data?.items ?? []}
            columns={columns}
            rowKey={(job) => job.id}
            searchable={(job) => `${job.title} ${job.company} ${job.location ?? ""}`}
            searchPlaceholder="Search title, company, location…"
            expanded={(job) => <JobExpansion jobId={job.id} />}
            empty="No jobs match. Run discovery, or widen the filters."
            toolbar={
              <>
                <Select value={status} onChange={(event) => setStatus(event.target.value)}>
                  <option value="">All statuses</option>
                  {STATUSES.map((value) => (
                    <option key={value} value={value}>
                      {value.replace(/_/g, " ")}
                    </option>
                  ))}
                </Select>
                <Select value={source} onChange={(event) => setSource(event.target.value)}>
                  <option value="">All sources</option>
                  <option value="seek">seek</option>
                  <option value="linkedin">linkedin</option>
                  <option value="indeed">indeed</option>
                </Select>
                <Select value={minScore} onChange={(event) => setMinScore(event.target.value)}>
                  <option value="">Any score</option>
                  <option value="80">80+</option>
                  <option value="60">60+</option>
                  <option value="40">40+</option>
                </Select>
              </>
            }
          />
        )}
      </Card>
    </div>
  );
}
