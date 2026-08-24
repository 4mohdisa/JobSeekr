import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { useAsync } from "../lib/hooks";
import { StringList } from "../components/DynamicFieldList";
import { Button, Card, ErrorNote, Field, Input, Select, Spinner, cx } from "../components/ui";
import type { Campaign, GrayZoneAction } from "../lib/types";

const GRAY_ZONE: { value: GrayZoneAction; label: string; hint: string }[] = [
  { value: "apply", label: "Apply", hint: "Treat gray-zone jobs like any other." },
  { value: "skip", label: "Skip", hint: "Discard them." },
  { value: "ask", label: "Ask", hint: "Queue and notify over Telegram." },
  { value: "queue", label: "Queue", hint: "Send to the manual queue silently." },
];

const BLANK: Partial<Campaign> = {
  name: "",
  active: true,
  search_terms: [],
  locations: [],
  work_types: [],
  score_floor: 60,
  score_auto_apply: 80,
  gray_zone_action: "queue",
  daily_caps: { seek: 10, linkedin: 5 },
  exclusions: {},
  template_ids: {},
  rubric: {},
};

function Editor({
  campaign,
  onSaved,
  onCancel,
}: {
  campaign: Partial<Campaign>;
  onSaved: () => void;
  onCancel: () => void;
}) {
  const [draft, setDraft] = useState(campaign);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<Error | undefined>();

  useEffect(() => setDraft(campaign), [campaign]);

  const set = <K extends keyof Campaign>(key: K, value: Campaign[K]) =>
    setDraft((current) => ({ ...current, [key]: value }));

  const exclusions = (draft.exclusions ?? {}) as Record<string, unknown>;
  const caps = (draft.daily_caps ?? {}) as Record<string, number>;

  const save = async () => {
    setSaving(true);
    setError(undefined);
    try {
      if (draft.id) await api.updateCampaign(draft.id, draft);
      else await api.createCampaign(draft);
      onSaved();
    } catch (caught) {
      setError(caught as Error);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card
      title={draft.id ? `Edit “${campaign.name}”` : "New campaign"}
      actions={
        <>
          <Button variant="quiet" onClick={onCancel}>
            Cancel
          </Button>
          <Button variant="primary" onClick={save} disabled={saving || !draft.name}>
            {saving ? "Saving…" : "Save"}
          </Button>
        </>
      }
      className="mb-4"
    >
      <ErrorNote error={error} />

      <div className="mb-4 grid gap-3 md:grid-cols-2">
        <Field label="Name">
          <Input value={draft.name ?? ""} onChange={(e) => set("name", e.target.value)} />
        </Field>
        <Field label="Salary floor (annual)" hint="Ads with no stated salary are kept.">
          <Input
            type="number"
            value={draft.salary_floor ?? ""}
            onChange={(e) =>
              set("salary_floor", e.target.value ? Number(e.target.value) : null)
            }
          />
        </Field>
      </div>

      <StringList
        label="Search terms"
        values={draft.search_terms ?? []}
        onChange={(next) => set("search_terms", next)}
      />
      <StringList
        label="Locations"
        values={draft.locations ?? []}
        onChange={(next) => set("locations", next)}
      />
      <StringList
        label="Work types"
        values={draft.work_types ?? []}
        onChange={(next) => set("work_types", next)}
      />
      <StringList
        label="Excluded companies"
        hint="Matched on a canonical form, so “Acme Pty Ltd” also blocks “Acme”."
        values={(exclusions.companies as string[]) ?? []}
        onChange={(next) => set("exclusions", { ...exclusions, companies: next })}
      />
      <StringList
        label="Excluded title keywords"
        values={(exclusions.title_keywords as string[]) ?? []}
        onChange={(next) => set("exclusions", { ...exclusions, title_keywords: next })}
      />

      <div className="mb-4 grid gap-3 md:grid-cols-3">
        <Field label="Score floor" hint="Below this, a job is rejected.">
          <Input
            type="number"
            value={draft.score_floor ?? 60}
            onChange={(e) => set("score_floor", Number(e.target.value))}
          />
        </Field>
        <Field label="Auto-apply at" hint="At or above this, apply without asking.">
          <Input
            type="number"
            value={draft.score_auto_apply ?? 80}
            onChange={(e) => set("score_auto_apply", Number(e.target.value))}
          />
        </Field>
        <Field
          label="Gray zone action"
          hint={GRAY_ZONE.find((g) => g.value === draft.gray_zone_action)?.hint}
        >
          <Select
            className="w-full"
            value={draft.gray_zone_action ?? "queue"}
            onChange={(e) => set("gray_zone_action", e.target.value as GrayZoneAction)}
          >
            {GRAY_ZONE.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </Select>
        </Field>
      </div>

      <div className="mb-4 grid gap-3 md:grid-cols-3">
        {["seek", "linkedin", "indeed"].map((platform) => (
          <Field key={platform} label={`${platform} daily cap`}>
            <Input
              type="number"
              value={caps[platform] ?? ""}
              onChange={(e) =>
                set("daily_caps", {
                  ...caps,
                  [platform]: Number(e.target.value || 0),
                })
              }
            />
          </Field>
        ))}
      </div>

      <div className="grid gap-3 md:grid-cols-2">
        <Field label="Target goal type" hint="e.g. interviews, applications">
          <Input
            value={draft.target_goal_type ?? ""}
            onChange={(e) => set("target_goal_type", e.target.value || null)}
          />
        </Field>
        <Field label="Target goal count">
          <Input
            type="number"
            value={draft.target_goal_count ?? ""}
            onChange={(e) =>
              set("target_goal_count", e.target.value ? Number(e.target.value) : null)
            }
          />
        </Field>
      </div>
    </Card>
  );
}

export function Campaigns() {
  const { data, error, loading, reload } = useAsync(() => api.listCampaigns(), []);
  const [editing, setEditing] = useState<Partial<Campaign> | null>(null);

  const toggle = async (campaign: Campaign) => {
    if (campaign.active) await api.pauseCampaign(campaign.id);
    else await api.resumeCampaign(campaign.id);
    reload();
  };

  if (loading) return <Spinner />;

  return (
    <div className="mx-auto max-w-4xl">
      <div className="mb-3 flex items-center gap-3">
        <h1 className="text-lg font-semibold">Campaigns</h1>
        <Button className="ml-auto" variant="primary" onClick={() => setEditing({ ...BLANK })}>
          + New campaign
        </Button>
      </div>

      <ErrorNote error={error} />

      {editing && (
        <Editor
          campaign={editing}
          onCancel={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            reload();
          }}
        />
      )}

      <div className="space-y-3">
        {(data ?? []).map((campaign) => {
          const cap = campaign.daily_caps?.seek ?? 0;
          return (
            <Card
              key={campaign.id}
              title={
                <span className="flex items-center gap-2">
                  <span
                    className={cx(
                      "inline-block h-2 w-2 rounded-full",
                      campaign.active ? "bg-good" : "bg-ink-600",
                    )}
                  />
                  {campaign.name}
                  <span className="text-xs font-normal text-ink-600">
                    rubric v{campaign.rubric_version}
                  </span>
                </span>
              }
              actions={
                <>
                  <Button onClick={() => setEditing(campaign)}>Edit</Button>
                  <Button
                    variant={campaign.active ? "danger" : "primary"}
                    onClick={() => toggle(campaign)}
                  >
                    {campaign.active ? "Stop this campaign" : "Resume"}
                  </Button>
                </>
              }
            >
              <div className="grid gap-3 text-xs md:grid-cols-4">
                <div>
                  <div className="text-ink-400">Terms</div>
                  <div className="text-ink-100">
                    {campaign.search_terms.join(", ") || "—"}
                  </div>
                </div>
                <div>
                  <div className="text-ink-400">Locations</div>
                  <div className="text-ink-100">{campaign.locations.join(", ") || "—"}</div>
                </div>
                <div>
                  <div className="text-ink-400">Thresholds</div>
                  <div className="tnum text-ink-100">
                    floor {campaign.score_floor} · auto {campaign.score_auto_apply} ·{" "}
                    {campaign.gray_zone_action}
                  </div>
                </div>
                <div>
                  <div className="text-ink-400">Today</div>
                  <div className="tnum text-ink-100">
                    {campaign.applied_today}
                    {cap ? ` / ${cap}` : ""} applied
                  </div>
                </div>
              </div>
            </Card>
          );
        })}
      </div>
    </div>
  );
}
