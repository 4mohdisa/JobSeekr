// Reports what the data supports and refuses to report what it does not.
//
// The greying-out is the feature, not a caveat. Every bucket carries
// `sufficient_data` from the backend, and a bucket below the threshold shows
// its n with NO rate at all. A 100% interview rate from one application is not
// an encouraging number, it is a wrong one, and putting it on screen invites a
// real decision to be made on noise.

import { api } from "../lib/api";
import { formatPercent, useAsync } from "../lib/hooks";
import { Card, Empty, ErrorNote, Spinner, Stat, cx } from "../components/ui";
import type { AnalyticsBucket } from "../lib/types";

function BucketTable({
  title,
  buckets,
  minimum,
  keyLabel,
}: {
  title: string;
  buckets: AnalyticsBucket[];
  minimum: number;
  keyLabel: string;
}) {
  if (buckets.length === 0) {
    return (
      <Card title={title}>
        <Empty>No applications yet.</Empty>
      </Card>
    );
  }

  return (
    <Card title={title}>
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-ink-800 text-left text-xs tracking-wide text-ink-400 uppercase">
            <th className="py-1.5">{keyLabel}</th>
            <th className="py-1.5 text-right">Applied</th>
            <th className="py-1.5 text-right">Replied</th>
            <th className="py-1.5 text-right">Interviews</th>
            <th className="py-1.5 text-right">Any reply</th>
            <th className="py-1.5 text-right">Interview rate</th>
          </tr>
        </thead>
        <tbody>
          {buckets.map((bucket) => (
            <tr
              key={bucket.key}
              className={cx(
                "border-b border-ink-800 last:border-0",
                // Not enough data: the whole row is dimmed so the eye does not
                // land on it as if it meant something.
                !bucket.sufficient_data && "text-ink-600",
              )}
            >
              <td className="py-1.5">{bucket.key}</td>
              <td className="tnum py-1.5 text-right">{bucket.applied}</td>
              <td className="tnum py-1.5 text-right">{bucket.replied}</td>
              <td className="tnum py-1.5 text-right">{bucket.interviews}</td>
              <td className="tnum py-1.5 text-right">
                {bucket.sufficient_data ? (
                  formatPercent(bucket.any_reply_rate)
                ) : (
                  <span title={`Needs at least ${minimum} applications to report a rate`}>
                    n={bucket.applied}
                  </span>
                )}
              </td>
              <td className="tnum py-1.5 text-right">
                {bucket.sufficient_data ? (
                  <span className={bucket.interviews > 0 ? "text-good" : undefined}>
                    {formatPercent(bucket.interview_rate)}
                  </span>
                ) : (
                  <span
                    className="text-ink-600"
                    title={`Needs at least ${minimum} applications to report a rate`}
                  >
                    not enough data
                  </span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  );
}

function Funnel({ stages }: { stages: { stage: string; count: number }[] }) {
  const top = Math.max(1, stages[0]?.count ?? 1);
  return (
    <Card title="Funnel">
      <div className="space-y-2">
        {stages.map((stage) => (
          <div key={stage.stage}>
            <div className="mb-0.5 flex justify-between text-xs">
              <span className="text-ink-300 capitalize">{stage.stage}</span>
              <span className="tnum text-ink-400">
                {stage.count}
                {top > 0 && stage.stage !== "applied" && (
                  <span className="ml-1 text-ink-600">
                    ({((stage.count / top) * 100).toFixed(0)}%)
                  </span>
                )}
              </span>
            </div>
            <div className="h-2 overflow-hidden rounded bg-ink-850">
              <div
                className="h-full rounded bg-accent"
                style={{ width: `${Math.max(2, (stage.count / top) * 100)}%` }}
              />
            </div>
          </div>
        ))}
      </div>
    </Card>
  );
}

export function Analytics() {
  const { data, error, loading } = useAsync(() => api.getAnalytics(), []);

  if (loading) return <Spinner />;
  if (!data) return <ErrorNote error={error} />;

  const interviews = data.funnel.find((s) => s.stage === "interview")?.count ?? 0;
  const replied = data.funnel.find((s) => s.stage === "replied")?.count ?? 0;
  const enough = data.total_applied >= data.minimum_sample;

  return (
    <div>
      <h1 className="mb-3 text-lg font-semibold">Analytics</h1>
      <ErrorNote error={error} />

      <div className="mb-4 grid grid-cols-2 gap-4 md:grid-cols-4">
        <Card>
          <Stat label="Applied" value={data.total_applied} />
        </Card>
        <Card>
          <Stat label="Any reply" value={replied} />
        </Card>
        <Card>
          <Stat label="Interviews" value={interviews} tone={interviews > 0 ? "good" : "normal"} />
        </Card>
        <Card>
          <Stat
            label="Interview rate"
            value={
              enough ? (
                formatPercent(interviews / Math.max(1, data.total_applied))
              ) : (
                <span className="text-sm text-ink-600">
                  n={data.total_applied}, need {data.minimum_sample}
                </span>
              )
            }
          />
        </Card>
      </div>

      {!enough && (
        <div className="mb-4 rounded border border-ink-700 bg-ink-850 px-3 py-2 text-xs text-ink-400">
          Rates are hidden until a bucket has at least {data.minimum_sample} applications.
          Below that, the number tells you about the sample rather than about the strategy.
        </div>
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        <Funnel stages={data.funnel} />
        <BucketTable
          title="By platform"
          buckets={data.by_platform}
          minimum={data.minimum_sample}
          keyLabel="Platform"
        />
        <BucketTable
          title="By campaign"
          buckets={data.by_campaign}
          minimum={data.minimum_sample}
          keyLabel="Campaign"
        />
        <BucketTable
          title="By score decile"
          buckets={data.by_score_decile}
          minimum={data.minimum_sample}
          keyLabel="Score"
        />
        <BucketTable
          title="By rubric version"
          buckets={data.by_rubric_version}
          minimum={data.minimum_sample}
          keyLabel="Rubric"
        />
      </div>

      <p className="mt-4 text-xs text-ink-600">
        Scores computed under different rubric versions are not comparable, which is why
        they are broken out separately rather than pooled.
      </p>
    </div>
  );
}
