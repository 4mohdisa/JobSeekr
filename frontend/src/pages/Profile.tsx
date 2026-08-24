// The profile: the only facts the system may state about the user.
//
// Every section uses the same DynamicFieldList. Saving creates a NEW version
// rather than overwriting, because scores record which profile version they
// were computed against.

import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { useAsync } from "../lib/hooks";
import { DynamicFieldList, StringList } from "../components/DynamicFieldList";
import { Button, Card, ErrorNote, Field, Input, Spinner, Textarea } from "../components/ui";
import type { Profile } from "../lib/types";

type Row = Record<string, unknown>;

const text = (row: Row, key: string) => String(row[key] ?? "");

export function ProfilePage() {
  const { data, error, loading, reload } = useAsync(() => api.getProfile(), []);
  const [draft, setDraft] = useState<Partial<Profile> | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState<number | null>(null);

  useEffect(() => {
    if (data) setDraft(structuredClone(data));
  }, [data]);

  if (loading || !draft) return <Spinner />;

  const identity = (draft.identity ?? {}) as Row;
  const workRights = (draft.work_rights ?? {}) as Row;

  const set = <K extends keyof Profile>(key: K, value: Profile[K]) =>
    setDraft((current) => ({ ...(current ?? {}), [key]: value }));

  const setIdentity = (key: string, value: string) =>
    set("identity", { ...identity, [key]: value } as Profile["identity"]);

  const save = async () => {
    setSaving(true);
    try {
      const next = await api.saveProfile(draft);
      setSaved(next.version);
      reload();
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="mx-auto max-w-4xl">
      <div className="mb-3 flex items-center gap-3">
        <h1 className="text-lg font-semibold">Profile</h1>
        <span className="text-xs text-ink-400">version {data?.version}</span>
        <div className="ml-auto flex items-center gap-2">
          {saved && <span className="text-xs text-good">Saved as version {saved}</span>}
          <Button variant="primary" onClick={save} disabled={saving}>
            {saving ? "Saving…" : "Save as new version"}
          </Button>
        </div>
      </div>

      <ErrorNote error={error} />

      <div className="mb-4 rounded border border-ink-700 bg-ink-850 px-3 py-2 text-xs text-ink-300">
        These are the <strong>only</strong> facts the system may state about you. Generated
        cover letters are validated against this profile and a build fails outright if it
        asserts an employer, date, metric or credential that is not here. Saving creates a
        new version; old scores stay attributed to the version they were computed under.
      </div>

      <Card title="Identity" className="mb-4">
        <div className="grid gap-3 md:grid-cols-2">
          <Field label="Full name">
            <Input
              value={text(identity, "name")}
              onChange={(event) => setIdentity("name", event.target.value)}
            />
          </Field>
          <Field label="Headline">
            <Input
              value={text(identity, "headline")}
              onChange={(event) => setIdentity("headline", event.target.value)}
            />
          </Field>
          <Field label="Email">
            <Input
              value={text(identity, "email")}
              onChange={(event) => setIdentity("email", event.target.value)}
            />
          </Field>
          <Field label="Phone" hint="Australian format, e.g. +61 412 345 678">
            <Input
              value={text(identity, "phone")}
              onChange={(event) => setIdentity("phone", event.target.value)}
            />
          </Field>
          <Field label="Location">
            <Input
              value={text(identity, "location")}
              onChange={(event) => setIdentity("location", event.target.value)}
            />
          </Field>
          <Field label="LinkedIn">
            <Input
              value={text(identity, "linkedin")}
              onChange={(event) => setIdentity("linkedin", event.target.value)}
            />
          </Field>
        </div>
        <div className="mt-3">
          <Field label="Summary">
            <Textarea
              rows={3}
              value={text(identity, "summary")}
              onChange={(event) => setIdentity("summary", event.target.value)}
            />
          </Field>
        </div>
      </Card>

      <Card title="Work rights" className="mb-4">
        <Field
          label="Statement"
          hint="Stated verbatim on documents. Never inferred, never embellished."
        >
          <Input
            value={text(workRights, "statement")}
            onChange={(event) =>
              set("work_rights", {
                ...workRights,
                statement: event.target.value,
              } as Profile["work_rights"])
            }
          />
        </Field>
      </Card>

      <Card className="mb-4">
        <StringList
          label="Skills"
          hint="Used for scoring and asserted on the resume — list only what you can evidence."
          values={(draft.skills ?? []) as string[]}
          onChange={(next) => set("skills", next as Profile["skills"])}
          placeholder="Add a skill and press Enter"
        />
      </Card>

      <Card className="mb-4">
        <DynamicFieldList<Row>
          label="Experience"
          items={(draft.experience ?? []) as Row[]}
          onChange={(next) => set("experience", next as Profile["experience"])}
          blank={() => ({ title: "", company: "", start: "", end: "", highlights: [] })}
          summary={(row) => `${text(row, "title") || "New role"} — ${text(row, "company")}`}
          addLabel="role"
          renderRow={(row, update) => (
            <div className="grid gap-2 md:grid-cols-2">
              <Field label="Title">
                <Input
                  value={text(row, "title")}
                  onChange={(event) => update({ ...row, title: event.target.value })}
                />
              </Field>
              <Field label="Company">
                <Input
                  value={text(row, "company")}
                  onChange={(event) => update({ ...row, company: event.target.value })}
                />
              </Field>
              <Field label="Start">
                <Input
                  value={text(row, "start")}
                  onChange={(event) => update({ ...row, start: event.target.value })}
                />
              </Field>
              <Field label="End">
                <Input
                  value={text(row, "end")}
                  onChange={(event) => update({ ...row, end: event.target.value })}
                />
              </Field>
              <div className="md:col-span-2">
                <Field label="Highlights" hint="One per line">
                  <Textarea
                    rows={3}
                    value={((row.highlights as string[]) ?? []).join("\n")}
                    onChange={(event) =>
                      update({
                        ...row,
                        highlights: event.target.value.split("\n").filter(Boolean),
                      })
                    }
                  />
                </Field>
              </div>
            </div>
          )}
        />

        <DynamicFieldList<Row>
          label="Projects"
          items={(draft.projects ?? []) as Row[]}
          onChange={(next) => set("projects", next as Profile["projects"])}
          blank={() => ({ name: "", stack: "", description: "" })}
          summary={(row) => text(row, "name") || "New project"}
          addLabel="project"
          renderRow={(row, update) => (
            <div className="grid gap-2 md:grid-cols-2">
              <Field label="Name">
                <Input
                  value={text(row, "name")}
                  onChange={(event) => update({ ...row, name: event.target.value })}
                />
              </Field>
              <Field label="Stack">
                <Input
                  value={text(row, "stack")}
                  onChange={(event) => update({ ...row, stack: event.target.value })}
                />
              </Field>
              <div className="md:col-span-2">
                <Field label="Description">
                  <Textarea
                    rows={2}
                    value={text(row, "description")}
                    onChange={(event) => update({ ...row, description: event.target.value })}
                  />
                </Field>
              </div>
            </div>
          )}
        />

        <DynamicFieldList<Row>
          label="Education"
          items={(draft.education ?? []) as Row[]}
          onChange={(next) => set("education", next as Profile["education"])}
          blank={() => ({ qualification: "", institution: "", year: "" })}
          summary={(row) => text(row, "qualification") || "New qualification"}
          addLabel="qualification"
          renderRow={(row, update) => (
            <div className="grid gap-2 md:grid-cols-3">
              <Field label="Qualification">
                <Input
                  value={text(row, "qualification")}
                  onChange={(event) => update({ ...row, qualification: event.target.value })}
                />
              </Field>
              <Field label="Institution">
                <Input
                  value={text(row, "institution")}
                  onChange={(event) => update({ ...row, institution: event.target.value })}
                />
              </Field>
              <Field label="Year">
                <Input
                  value={text(row, "year")}
                  onChange={(event) => update({ ...row, year: event.target.value })}
                />
              </Field>
            </div>
          )}
        />

        <DynamicFieldList<Row>
          label="Certifications"
          hint="Only certifications you actually hold — these are checked before a document is built."
          items={(draft.certifications ?? []) as Row[]}
          onChange={(next) => set("certifications", next as Profile["certifications"])}
          blank={() => ({ name: "", issuer: "", year: "" })}
          summary={(row) => text(row, "name") || "New certification"}
          addLabel="certification"
          renderRow={(row, update) => (
            <div className="grid gap-2 md:grid-cols-3">
              <Field label="Name">
                <Input
                  value={text(row, "name")}
                  onChange={(event) => update({ ...row, name: event.target.value })}
                />
              </Field>
              <Field label="Issuer">
                <Input
                  value={text(row, "issuer")}
                  onChange={(event) => update({ ...row, issuer: event.target.value })}
                />
              </Field>
              <Field label="Year">
                <Input
                  value={text(row, "year")}
                  onChange={(event) => update({ ...row, year: event.target.value })}
                />
              </Field>
            </div>
          )}
        />
      </Card>
    </div>
  );
}
