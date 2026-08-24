// One TemplateEditor, three kinds, selected by a tab.
//
// Instantiating three different editors would mean three autocomplete lists and
// three validators drifting away from the one the backend actually enforces.

import { useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";
import { useAsync } from "../lib/hooks";
import { TemplateEditor } from "../components/TemplateEditor";
import { Button, Card, ErrorNote, Input, Select, Spinner, cx } from "../components/ui";
import type { Template, TemplateKind, TemplatePreview } from "../lib/types";

const KINDS: { value: TemplateKind; label: string }[] = [
  { value: "resume", label: "Resume" },
  { value: "cover_letter", label: "Cover letter" },
  { value: "email", label: "Outbound email" },
];

export function Templates() {
  const { data, error, loading, reload } = useAsync(() => api.listTemplates(), []);
  const jobs = useAsync(() => api.listJobs({ limit: 50 }), []);

  const [kind, setKind] = useState<TemplateKind>("cover_letter");
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [body, setBody] = useState("");
  const [name, setName] = useState("");
  const [jobId, setJobId] = useState<number | undefined>();
  const [preview, setPreview] = useState<TemplatePreview | undefined>();
  const [previewing, setPreviewing] = useState(false);
  const [saving, setSaving] = useState(false);

  const ofKind = useMemo(
    () => (data ?? []).filter((template) => template.kind === kind),
    [data, kind],
  );

  useEffect(() => {
    const first = ofKind[0];
    setSelectedId(first?.id ?? null);
    setBody(first?.body ?? "");
    setName(first?.name ?? "");
  }, [ofKind]);

  const select = (template: Template) => {
    setSelectedId(template.id);
    setBody(template.body);
    setName(template.name);
  };

  const runPreview = async () => {
    if (!body.trim()) return;
    setPreviewing(true);
    try {
      setPreview(await api.previewTemplate(body, jobId));
    } finally {
      setPreviewing(false);
    }
  };

  const save = async () => {
    setSaving(true);
    try {
      if (selectedId) await api.updateTemplate(selectedId, { kind, name, body });
      else await api.createTemplate({ kind, name: name || "untitled", body });
      reload();
    } finally {
      setSaving(false);
    }
  };

  const compile = async () => {
    // Compiling a real PDF needs a job to render against and the document
    // pipeline to run; the preview endpoint reports what it can without one.
    if (!preview?.job_id) {
      window.alert("Preview against a real job first — there is nothing to compile yet.");
      return;
    }
    setSaving(true);
    try {
      const result = await api.buildDocuments(preview.job_id, true);
      const documents = (result.documents ?? {}) as Record<string, number>;
      const id = documents.cover_letter ?? documents.resume ?? documents.combined;
      if (id) window.open(api.documentUrl(id), "_blank", "noopener");
      else window.alert(String(result.failure_reason ?? "The build produced no document."));
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <Spinner />;

  return (
    <div>
      <h1 className="mb-3 text-lg font-semibold">Templates</h1>
      <ErrorNote error={error} />

      <div className="mb-3 flex flex-wrap items-center gap-2">
        <div className="flex rounded border border-ink-800 p-0.5">
          {KINDS.map((option) => (
            <button
              key={option.value}
              onClick={() => setKind(option.value)}
              className={cx(
                "rounded px-3 py-1 text-sm transition-colors",
                kind === option.value
                  ? "bg-accent/15 font-medium text-accent"
                  : "text-ink-400 hover:text-ink-100",
              )}
            >
              {option.label}
            </button>
          ))}
        </div>

        <Select
          value={selectedId ?? ""}
          onChange={(event) => {
            const found = ofKind.find((t) => t.id === Number(event.target.value));
            if (found) select(found);
          }}
        >
          {ofKind.length === 0 && <option value="">no saved templates</option>}
          {ofKind.map((template) => (
            <option key={template.id} value={template.id}>
              {template.name} (v{template.version})
            </option>
          ))}
        </Select>

        <Input
          value={name}
          placeholder="Template name"
          onChange={(event) => setName(event.target.value)}
          className="max-w-xs"
        />

        <Select
          value={jobId ?? ""}
          onChange={(event) =>
            setJobId(event.target.value ? Number(event.target.value) : undefined)
          }
        >
          <option value="">Preview against most recent job</option>
          {(jobs.data?.items ?? []).map((job) => (
            <option key={job.id} value={job.id}>
              {job.title} — {job.company}
            </option>
          ))}
        </Select>

        <Button
          variant="quiet"
          onClick={() => {
            setSelectedId(null);
            setBody("");
            setName("");
          }}
        >
          New
        </Button>
        <Button variant="primary" onClick={save} disabled={saving || !body.trim()}>
          {saving ? "Saving…" : selectedId ? "Save new version" : "Create"}
        </Button>
      </div>

      <Card>
        <TemplateEditor
          value={body}
          onChange={setBody}
          preview={preview}
          previewing={previewing}
          onPreview={runPreview}
          onCompile={compile}
          compiling={saving}
        />
      </Card>
    </div>
  );
}
