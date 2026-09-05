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
import type {
  AnalyticsBucket,
  CacheRate,
  CampaignFunnel,
  CostPoint,
  CoveragePoint,
  FactLeverage,
  PerformanceTelemetry,
  QuestionCluster,
  RunProfile,
} from "../lib/types";

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

// A campaign's whole pipeline. Every stage is drawn against `discovered`, so
// the bar that collapses is the stage that is losing people — which is the
// question a final interview rate cannot answer.
function CampaignFunnels({
  funnels,
  minimum,
}: {
  funnels: CampaignFunnel[];
  minimum: number;
}) {
  if (funnels.length === 0) {
    return (
      <Card title="Per-campaign funnel">
        <Empty>No campaigns have discovered anything yet.</Empty>
      </Card>
    );
  }

  return (
    <Card title="Per-campaign funnel">
      <div className="space-y-5">
        {funnels.map((funnel) => {
          const stages = [
            { label: "discovered", count: funnel.discovered },
            { label: "scored", count: funnel.scored },
            { label: "applied", count: funnel.applied },
            { label: "heard back", count: funnel.acknowledged },
            { label: "replied", count: funnel.replied },
            { label: "interview", count: funnel.interviews },
          ];
          const top = Math.max(1, funnel.discovered);
          return (
            <div key={funnel.campaign_id ?? "none"}>
              <div className="mb-1.5 flex items-baseline justify-between">
                <span className="text-sm font-medium text-ink-100">{funnel.name}</span>
                <span className="tnum text-xs text-ink-400">
                  {funnel.sufficient_data ? (
                    <>interview rate {formatPercent(funnel.interview_rate)}</>
                  ) : (
                    <span
                      className="text-ink-600"
                      title={`Needs at least ${minimum} applications to report a rate`}
                    >
                      n={funnel.applied}, need {minimum}
                    </span>
                  )}
                </span>
              </div>
              <div className="space-y-1">
                {stages.map((stage) => (
                  <div key={stage.label}>
                    <div className="mb-0.5 flex justify-between text-xs">
                      <span className="text-ink-300">{stage.label}</span>
                      <span className="tnum text-ink-400">
                        {stage.count}
                        {stage.label !== "discovered" && (
                          <span className="ml-1 text-ink-600">
                            ({((stage.count / top) * 100).toFixed(0)}%)
                          </span>
                        )}
                      </span>
                    </div>
                    <div className="h-1.5 overflow-hidden rounded bg-ink-850">
                      <div
                        className="h-full rounded bg-accent"
                        style={{ width: `${Math.max(1, (stage.count / top) * 100)}%` }}
                      />
                    </div>
                  </div>
                ))}
              </div>
            </div>
          );
        })}
      </div>
      <p className="mt-4 text-xs text-ink-600">
        Discovered counts ads stored for this campaign — an ad matching two campaigns is
        attributed to whichever searched first. Applied counts submitted applications
        only; an aborted attempt never reached an employer.
      </p>
    </Card>
  );
}

function QuestionTable({
  title,
  clusters,
  countLabel,
  count,
  note,
}: {
  title: string;
  clusters: QuestionCluster[];
  countLabel: string;
  count: (cluster: QuestionCluster) => number;
  note: string;
}) {
  if (clusters.length === 0) {
    return (
      <Card title={title}>
        <Empty>No screening questions recorded yet.</Empty>
      </Card>
    );
  }

  return (
    <Card title={title}>
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-ink-800 text-left text-xs tracking-wide text-ink-400 uppercase">
            <th className="py-1.5">Question</th>
            <th className="py-1.5 text-right">{countLabel}</th>
            <th className="py-1.5 text-right">Employers</th>
            <th className="py-1.5 text-right">Asked</th>
          </tr>
        </thead>
        <tbody>
          {clusters.map((cluster) => (
            <tr key={cluster.question} className="border-b border-ink-800 last:border-0">
              <td className="py-1.5 pr-3">
                {cluster.question}
                {cluster.variants > 1 && (
                  <span
                    className="ml-1 text-xs text-ink-600"
                    title={`${cluster.variants} wordings of this question folded together`}
                  >
                    +{cluster.variants - 1} wording
                    {cluster.variants > 2 ? "s" : ""}
                  </span>
                )}
              </td>
              <td className="tnum py-1.5 text-right">
                <span className={count(cluster) > 0 ? "text-warn" : undefined}>
                  {count(cluster)}
                </span>
              </td>
              <td className="tnum py-1.5 text-right">{cluster.employers}</td>
              <td className="tnum py-1.5 text-right">{cluster.asked}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="mt-3 text-xs text-ink-600">{note}</p>
    </Card>
  );
}

// The one trend that is allowed to fall. A week below the reporting minimum
// shows its raw counts and no rate — the same rule the bucket tables follow,
// and it matters more here: one wrong point makes a trend line lie.
function Coverage({ points, minimum }: { points: CoveragePoint[]; minimum: number }) {
  if (points.length === 0) {
    return (
      <Card title="Coverage">
        <Empty>No screening questions recorded yet.</Empty>
      </Card>
    );
  }

  return (
    <Card title="Coverage — answered without you">
      <div className="space-y-2">
        {points.map((point) => (
          <div key={point.week}>
            <div className="mb-0.5 flex justify-between text-xs">
              <span className="text-ink-300">week of {point.week}</span>
              <span className="tnum text-ink-400">
                {point.sufficient_data ? (
                  formatPercent(point.rate)
                ) : (
                  <span
                    className="text-ink-600"
                    title={`Needs at least ${minimum} questions to report a rate`}
                  >
                    {point.resolved}/{point.asked}, need {minimum}
                  </span>
                )}
              </span>
            </div>
            <div className="h-2 overflow-hidden rounded bg-ink-850">
              <div
                className={cx(
                  "h-full rounded",
                  point.sufficient_data ? "bg-good" : "bg-ink-700",
                )}
                style={{ width: `${Math.max(2, (point.rate ?? 0) * 100)}%` }}
              />
            </div>
          </div>
        ))}
      </div>
      <p className="mt-3 text-xs text-ink-600">
        The share of screening questions resolved from the answer bank, a fact or a
        cached form map — with no Telegram round trip. Identity fields the profile fills
        are not counted; they cannot fail, and counting them would push this to 100%.
      </p>
    </Card>
  );
}

function FactLeverageTable({ rows }: { rows: FactLeverage[] }) {
  if (rows.length === 0) {
    return (
      <Card title="Fact leverage">
        <Empty>No facts stated yet.</Empty>
      </Card>
    );
  }

  return (
    <Card title="Fact leverage">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-ink-800 text-left text-xs tracking-wide text-ink-400 uppercase">
            <th className="py-1.5">Fact</th>
            <th className="py-1.5">Category</th>
            <th className="py-1.5 text-right">Answers</th>
            <th className="py-1.5 text-right">Awaiting you</th>
            <th className="py-1.5 text-right">Stale</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={row.fact_id}
              className={cx(
                "border-b border-ink-800 last:border-0",
                row.derived === 0 && "text-ink-600",
              )}
            >
              <td className="py-1.5">{row.key}</td>
              <td className="py-1.5 text-ink-400">{row.category.replace("_", " ")}</td>
              <td className="tnum py-1.5 text-right">
                <span className={row.confirmed > 0 ? "text-good" : undefined}>
                  {row.confirmed}
                </span>
              </td>
              <td className="tnum py-1.5 text-right">{row.derived - row.confirmed}</td>
              <td className="tnum py-1.5 text-right">
                <span className={row.stale > 0 ? "text-warn" : undefined}>
                  {row.stale}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="mt-3 text-xs text-ink-600">
        Only confirmed derivations ever answer a form. A fact answering nothing is dimmed
        — it is either a question nobody asks or one the derivation step cannot read.
      </p>
    </Card>
  );
}

function seconds(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${(ms / 60_000).toFixed(1)}m`;
}

// Work stages only. `pacing` arrives as its own field and is rendered below the
// table, outside the total — the wait between submissions protects the account
// and must never read as latency someone could shave.
function StageTable({ stages }: { stages: PerformanceTelemetry["stages"] }) {
  if (stages.work.length === 0) {
    return (
      <Card title="Where the time goes">
        <Empty>Nothing timed yet — run an apply pass.</Empty>
      </Card>
    );
  }

  return (
    <Card title="Where the time goes">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-ink-800 text-left text-xs tracking-wide text-ink-400 uppercase">
            <th className="py-1.5">Stage</th>
            <th className="py-1.5 text-right">Median</th>
            <th className="py-1.5 text-right">Slowest</th>
            <th className="py-1.5 text-right">Total</th>
            <th className="py-1.5 text-right">n</th>
          </tr>
        </thead>
        <tbody>
          {stages.work.map((stat) => (
            <tr key={stat.stage} className="border-b border-ink-800 last:border-0">
              <td className="py-1.5">
                {stat.stage.replace(/_/g, " ")}
                {stat.stage === stages.slowest_stage && (
                  <span className="ml-2 rounded bg-warn/15 px-1.5 py-0.5 text-xs text-warn">
                    slowest
                  </span>
                )}
              </td>
              <td className="tnum py-1.5 text-right">{seconds(stat.median_ms)}</td>
              <td className="tnum py-1.5 text-right">{seconds(stat.slowest_ms)}</td>
              <td className="tnum py-1.5 text-right">{seconds(stat.total_ms)}</td>
              <td className="tnum py-1.5 text-right text-ink-400">
                {stat.observations}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="mt-3 border-t border-ink-800 pt-3 text-xs">
        <div className="flex justify-between">
          <span className="text-ink-300">work total</span>
          <span className="tnum text-ink-100">{seconds(stages.work_total_ms)}</span>
        </div>
        {stages.pacing && (
          <div className="mt-1 flex justify-between text-ink-400">
            <span>pacing — deliberate, not work</span>
            <span className="tnum">
              {seconds(stages.pacing.total_ms)} over {stages.pacing.observations} waits
            </span>
          </div>
        )}
      </div>
      <p className="mt-3 text-xs text-ink-600">
        Pacing is the randomised delay between submissions. It protects the LinkedIn
        account, so it is measured separately and never added to the work total —
        it is not latency to optimise.
      </p>
    </Card>
  );
}

function RunTable({ runs }: { runs: RunProfile[] }) {
  if (runs.length === 0) {
    return (
      <Card title="Per run">
        <Empty>No apply passes recorded yet.</Empty>
      </Card>
    );
  }

  return (
    <Card title="Per run — slowest stage">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-ink-800 text-left text-xs tracking-wide text-ink-400 uppercase">
            <th className="py-1.5">Run</th>
            <th className="py-1.5 text-right">Jobs</th>
            <th className="py-1.5">Slowest stage</th>
            <th className="py-1.5 text-right">Work</th>
            <th className="py-1.5 text-right">Pacing</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr key={run.run_id} className="border-b border-ink-800 last:border-0">
              <td className="py-1.5 text-ink-400">
                {new Date(run.started_at).toLocaleString()}
              </td>
              <td className="tnum py-1.5 text-right">{run.applications}</td>
              <td className="py-1.5">
                {run.slowest_stage ? (
                  <>
                    {run.slowest_stage.replace(/_/g, " ")}
                    <span className="ml-1 text-ink-600">
                      ({seconds(run.slowest_stage_ms)})
                    </span>
                  </>
                ) : (
                  <span className="text-ink-600">nothing timed</span>
                )}
              </td>
              <td className="tnum py-1.5 text-right">{seconds(run.work_ms)}</td>
              <td className="tnum py-1.5 text-right text-ink-600">
                {seconds(run.pacing_ms)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  );
}

function CacheRates({ rates }: { rates: CacheRate[] }) {
  if (rates.length === 0) {
    return (
      <Card title="Cache hit rates">
        <Empty>No cache lookups recorded yet.</Empty>
      </Card>
    );
  }

  const byCache = new Map<string, CacheRate[]>();
  for (const rate of rates) {
    const existing = byCache.get(rate.cache);
    if (existing) existing.push(rate);
    else byCache.set(rate.cache, [rate]);
  }

  return (
    <Card title="Cache hit rates — each should climb">
      <div className="space-y-4">
        {[...byCache.entries()].map(([cache, points]) => (
          <div key={cache}>
            <div className="mb-1 flex items-baseline justify-between">
              <span className="text-sm text-ink-100">{cache.replace(/_/g, " ")}</span>
              <span className="text-xs text-ink-600">{points[0].unit}</span>
            </div>
            <div className="flex items-end gap-1">
              {points.map((point) => (
                <div
                  key={point.week}
                  className="flex-1"
                  title={`week of ${point.week}: ${point.hits}/${point.lookups}`}
                >
                  <div className="flex h-12 items-end">
                    <div
                      className="w-full rounded-t bg-accent"
                      style={{ height: `${Math.max(3, point.rate * 100)}%` }}
                    />
                  </div>
                  <div className="tnum mt-0.5 text-center text-xs text-ink-600">
                    {(point.rate * 100).toFixed(0)}
                  </div>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
      <p className="mt-3 text-xs text-ink-600">
        The denominators differ per cache and are labelled: the answer bank is asked
        every screening question, the facts layer only the ones the bank missed, so a
        falling facts rate can mean the bank got better.
      </p>
    </Card>
  );
}

function CostTrend({ points }: { points: CostPoint[] }) {
  if (points.length === 0) {
    return (
      <Card title="Cost per application">
        <Empty>No submitted applications yet.</Empty>
      </Card>
    );
  }
  const peak = Math.max(...points.map((p) => p.per_application_usd), 0.0001);

  return (
    <Card title="Cost per application — should fall">
      <div className="space-y-2">
        {points.map((point) => (
          <div key={point.week}>
            <div className="mb-0.5 flex justify-between text-xs">
              <span className="text-ink-300">week of {point.week}</span>
              <span className="tnum text-ink-400">
                ${point.per_application_usd.toFixed(3)}
                <span className="ml-1 text-ink-600">({point.applications})</span>
              </span>
            </div>
            <div className="h-2 overflow-hidden rounded bg-ink-850">
              <div
                className="h-full rounded bg-accent"
                style={{ width: `${Math.max(2, (point.per_application_usd / peak) * 100)}%` }}
              />
            </div>
          </div>
        ))}
      </div>
      <p className="mt-3 text-xs text-ink-600">
        Model spend attributable to each submitted application. Spend with no job —
        the per-campaign summary embedding — is real and is not in this number.
      </p>
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

      <h2 className="mt-6 mb-3 text-base font-semibold">Where campaigns lose people</h2>
      <CampaignFunnels funnels={data.campaign_funnels} minimum={data.minimum_sample} />

      <h2 className="mt-6 mb-3 text-base font-semibold">Screening questions</h2>
      <div className="grid gap-4 lg:grid-cols-2">
        <QuestionTable
          title="Friction — what to pre-answer next"
          clusters={data.questions.friction}
          countLabel="Jobs parked"
          count={(cluster) => cluster.jobs_parked}
          note="Ranked by applications stopped, not by how often the question was asked. Answer the top row and those jobs go out on the next pass."
        />
        <QuestionTable
          title="Frequency — what employers ask"
          clusters={data.questions.frequency}
          countLabel="Parked"
          count={(cluster) => cluster.jobs_parked}
          note="Ranked by how many distinct employers ask. Near-identical wordings are folded into one question — hover a wording count to see how many."
        />
        <Coverage points={data.questions.coverage} minimum={data.minimum_sample} />
        <FactLeverageTable rows={data.questions.fact_leverage} />
      </div>

      <h2 className="mt-6 mb-3 text-base font-semibold">Performance</h2>
      <div className="grid gap-4 lg:grid-cols-2">
        <StageTable stages={data.performance.stages} />
        <RunTable runs={data.performance.runs} />
        <CacheRates rates={data.performance.caches} />
        <CostTrend points={data.performance.cost} />
      </div>
    </div>
  );
}
