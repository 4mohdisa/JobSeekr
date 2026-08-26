import { useState } from "react";
import { api } from "../lib/api";
import { formatDateTime, useAsync } from "../lib/hooks";
import { DataTable, type Column } from "../components/DataTable";
import { StatusBadge } from "../components/StatusBadge";
import { Button, Card, ErrorNote, Input, Select, Spinner } from "../components/ui";
import type { Application, ResponseStatus } from "../lib/types";

const RESPONSES: ResponseStatus[] = [
  "none",
  "acknowledged",
  "rejected",
  "interview_request",
  "recruiter_outreach",
  "ghosted",
];

function Detail({ application, onSaved }: { application: Application; onSaved: () => void }) {
  const [notes, setNotes] = useState(application.user_notes ?? "");
  const [status, setStatus] = useState<ResponseStatus>(application.response_status);
  const [saving, setSaving] = useState(false);

  const save = async () => {
    setSaving(true);
    try {
      await api.patchApplication(application.id, {
        user_notes: notes,
        response_status: status,
      });
      onSaved();
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="grid gap-4 md:grid-cols-2">
      <div className="space-y-2 text-xs">
        {application.failure_reason && (
          <div className="rounded border border-bad/40 bg-bad/10 px-2 py-1.5 text-bad">
            {application.failure_reason}
          </div>
        )}
        <div>
          <span className="text-ink-400">Attachment read back: </span>
          <span className="text-ink-100">{application.attachment_readback ?? "—"}</span>
        </div>
        <div className="flex flex-wrap gap-2 pt-1">
          {application.job_url && (
            <a href={application.job_url} target="_blank" rel="noopener noreferrer">
              <Button>Open ad ↗</Button>
            </a>
          )}
          {/* Links to the EXACT documents sent, by id — never a rebuild. */}
          {application.resume_doc_id && (
            <a
              href={api.documentUrl(application.resume_doc_id)}
              target="_blank"
              rel="noopener noreferrer"
            >
              <Button>Resume sent</Button>
            </a>
          )}
          {application.cover_letter_doc_id && (
            <a
              href={api.documentUrl(application.cover_letter_doc_id)}
              target="_blank"
              rel="noopener noreferrer"
            >
              <Button>Cover letter sent</Button>
            </a>
          )}
        </div>

        {Object.keys(application.answers_given).length > 0 && (
          <details className="pt-2">
            <summary className="cursor-pointer text-ink-400">Answers given</summary>
            <ul className="mt-1 space-y-0.5">
              {Object.entries(application.answers_given).map(([question, answer]) => (
                <li key={question}>
                  <span className="text-ink-400">{question}: </span>
                  <span className="text-ink-100">{String(answer)}</span>
                </li>
              ))}
            </ul>
          </details>
        )}
      </div>

      <div className="space-y-2">
        <Select
          value={status}
          onChange={(event) => setStatus(event.target.value as ResponseStatus)}
          className="w-full"
        >
          {RESPONSES.map((value) => (
            <option key={value} value={value}>
              {value.replace(/_/g, " ")}
            </option>
          ))}
        </Select>
        <Input
          value={notes}
          placeholder="Notes"
          onChange={(event) => setNotes(event.target.value)}
        />
        <Button variant="primary" onClick={save} disabled={saving}>
          {saving ? "Saving…" : "Save"}
        </Button>
      </div>
    </div>
  );
}

export function Applications() {
  const [platform, setPlatform] = useState("");
  const [response, setResponse] = useState("");
  const { data, error, loading, reload } = useAsync(
    () =>
      api.listApplications({
        limit: 500,
        platform: platform || undefined,
        response_status: response || undefined,
      }),
    [platform, response],
  );

  const columns: Column<Application>[] = [
    {
      key: "applied_at",
      header: "Applied",
      render: (row) => formatDateTime(row.applied_at),
      sortValue: (row) => row.applied_at,
    },
    {
      key: "job_company",
      header: "Company",
      render: (row) => row.job_company ?? "—",
      sortValue: (row) => row.job_company,
    },
    {
      key: "job_title",
      header: "Title",
      render: (row) => <span className="text-ink-100">{row.job_title ?? "—"}</span>,
      sortValue: (row) => row.job_title,
    },
    { key: "platform", header: "Platform", sortValue: (row) => row.platform },
    {
      key: "outcome",
      header: "Outcome",
      render: (row) => <StatusBadge status={row.outcome} kind="outcome" />,
      sortValue: (row) => row.outcome,
    },
    {
      key: "response_status",
      header: "Response",
      render: (row) => <StatusBadge status={row.response_status} kind="response" />,
      sortValue: (row) => row.response_status,
    },
  ];

  return (
    <div>
      <h1 className="mb-3 text-lg font-semibold">Applications</h1>
      <ErrorNote error={error} />

      <Card>
        {loading ? (
          <Spinner />
        ) : (
          <DataTable
            rows={data?.items ?? []}
            columns={columns}
            rowKey={(row) => row.id}
            searchable={(row) => `${row.job_company ?? ""} ${row.job_title ?? ""}`}
            searchPlaceholder="Search company or title…"
            expanded={(row) => <Detail application={row} onSaved={reload} />}
            empty="No applications yet."
            toolbar={
              <>
                <Select value={platform} onChange={(event) => setPlatform(event.target.value)}>
                  <option value="">All platforms</option>
                  <option value="seek">seek</option>
                  <option value="linkedin">linkedin</option>
                  <option value="indeed">indeed</option>
                </Select>
                <Select value={response} onChange={(event) => setResponse(event.target.value)}>
                  <option value="">All responses</option>
                  {RESPONSES.map((value) => (
                    <option key={value} value={value}>
                      {value.replace(/_/g, " ")}
                    </option>
                  ))}
                </Select>
                <a href={api.exportUrl()} download>
                  <Button>Export CSV</Button>
                </a>
              </>
            }
          />
        )}
      </Card>
    </div>
  );
}
