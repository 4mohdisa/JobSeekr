// The one way this app talks to the backend.
//
// Every page goes through here. No component calls fetch() directly — that is
// how error handling, query-string encoding and JSON parsing end up
// implemented five slightly different ways.

import type {
  Analytics,
  Answer,
  Application,
  AppSettings,
  Campaign,
  ControlState,
  DerivedAnswer,
  DocumentRef,
  Fact,
  Job,
  JobDetail,
  Page,
  Preference,
  Profile,
  QueueCard,
  SessionHealth,
  Template,
  TemplatePreview,
} from "./types";

export class ApiError extends Error {
  // Declared and assigned separately rather than as constructor parameter
  // properties: this project builds with `erasableSyntaxOnly`, which rejects
  // any TypeScript syntax that emits runtime code.
  readonly status: number;
  readonly detail?: unknown;

  constructor(status: number, message: string, detail?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

type Params = Record<string, string | number | boolean | null | undefined>;

function queryString(params?: Params): string {
  if (!params) return "";
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined || value === "") continue;
    search.append(key, String(value));
  }
  const rendered = search.toString();
  return rendered ? `?${rendered}` : "";
}

async function request<T>(
  path: string,
  { params, ...init }: RequestInit & { params?: Params } = {},
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}${queryString(params)}`, {
      headers: { "Content-Type": "application/json", ...(init.headers ?? {}) },
      ...init,
    });
  } catch (cause) {
    // A dead backend is the common case during development; say so plainly
    // rather than surfacing "Failed to fetch".
    throw new ApiError(0, "Cannot reach the backend. Is uvicorn running?", cause);
  }

  if (!response.ok) {
    let detail: unknown;
    let message = `${response.status} ${response.statusText}`;
    try {
      detail = await response.json();
      const parsed = detail as { detail?: unknown };
      if (typeof parsed.detail === "string") message = parsed.detail;
    } catch {
      // A non-JSON error body is fine; the status line is enough.
    }
    throw new ApiError(response.status, message, detail);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

const body = (payload: unknown) => JSON.stringify(payload);

export const api = {
  // ---------------------------------------------------------------- profile
  getProfile: () => request<Profile>("/profile"),
  saveProfile: (payload: Partial<Profile>) =>
    request<Profile>("/profile", { method: "PUT", body: body(payload) }),
  profileVersions: () => request<Profile[]>("/profile/versions"),

  // -------------------------------------------------------------- campaigns
  listCampaigns: () => request<Campaign[]>("/campaigns"),
  getCampaign: (id: number) => request<Campaign>(`/campaigns/${id}`),
  createCampaign: (payload: Partial<Campaign>) =>
    request<Campaign>("/campaigns", { method: "POST", body: body(payload) }),
  updateCampaign: (id: number, payload: Partial<Campaign>) =>
    request<Campaign>(`/campaigns/${id}`, { method: "PUT", body: body(payload) }),
  deleteCampaign: (id: number) =>
    request<void>(`/campaigns/${id}`, { method: "DELETE" }),
  pauseCampaign: (id: number) =>
    request<Campaign>(`/campaigns/${id}/pause`, { method: "POST" }),
  resumeCampaign: (id: number) =>
    request<Campaign>(`/campaigns/${id}/resume`, { method: "POST" }),

  // ---------------------------------------------------------------- control
  controlState: () => request<ControlState>("/control"),
  stopEverything: (reason: string) =>
    request<ControlState>("/control/stop", { method: "POST", params: { reason } }),
  resumeEverything: () =>
    request<ControlState>("/control/resume", { method: "POST" }),

  // ------------------------------------------------------------ answer bank
  listAnswers: (params?: { campaign_id?: number; unanswered_only?: boolean }) =>
    request<Answer[]>("/answers", { params }),
  createAnswer: (payload: Partial<Answer>) =>
    request<Answer>("/answers", { method: "POST", body: body(payload) }),
  updateAnswer: (id: number, payload: Partial<Answer>) =>
    request<Answer>(`/answers/${id}`, { method: "PUT", body: body(payload) }),
  bulkAnswers: (values: Record<number, string>) =>
    request<Answer[]>("/answers/bulk", { method: "POST", body: body(values) }),
  deleteAnswer: (id: number) => request<void>(`/answers/${id}`, { method: "DELETE" }),

  // ------------------------------------------------------------ preferences
  listPreferences: (params?: { status?: string }) =>
    request<Preference[]>("/preferences", { params }),
  createPreference: (payload: Partial<Preference>) =>
    request<Preference>("/preferences", { method: "POST", body: body(payload) }),
  confirmPreference: (id: number) =>
    request<Preference>(`/preferences/${id}/confirm`, { method: "POST" }),
  rejectPreference: (id: number) =>
    request<Preference>(`/preferences/${id}/reject`, { method: "POST" }),
  deletePreference: (id: number) =>
    request<void>(`/preferences/${id}`, { method: "DELETE" }),

  // ------------------------------------------------------------------ facts
  listFacts: () => request<Fact[]>("/facts"),
  updateFact: (key: string, payload: { text: string; jurisdiction?: string | null }) =>
    request<Fact>(`/facts/${key}`, { method: "PUT", body: body(payload) }),
  listDerived: (params?: { fact_id?: number }) =>
    request<DerivedAnswer[]>("/facts/derived", { params }),
  confirmDerived: (id: number) =>
    request<DerivedAnswer>(`/facts/derived/${id}/confirm`, { method: "POST" }),
  rejectDerived: (id: number) =>
    request<void>(`/facts/derived/${id}`, { method: "DELETE" }),

  // --------------------------------------------------------------- sessions
  listSessions: () => request<SessionHealth[]>("/sessions"),

  // -------------------------------------------------------------- templates
  listTemplates: () => request<Template[]>("/templates"),
  createTemplate: (payload: Partial<Template>) =>
    request<Template>("/templates", { method: "POST", body: body(payload) }),
  updateTemplate: (id: number, payload: Partial<Template>) =>
    request<Template>(`/templates/${id}`, { method: "PUT", body: body(payload) }),
  deleteTemplate: (id: number) =>
    request<void>(`/templates/${id}`, { method: "DELETE" }),
  previewTemplate: (templateBody: string, jobId?: number) =>
    request<TemplatePreview>("/templates/preview", {
      method: "POST",
      params: { body: templateBody, job_id: jobId },
    }),

  // ------------------------------------------------------------------- jobs
  listJobs: (params?: Params) => request<Page<Job>>("/jobs", { params }),
  getJob: (id: number) => request<JobDetail>(`/jobs/${id}`),
  setJobStatus: (id: number, status: string) =>
    request<Job>(`/jobs/${id}/status`, { method: "POST", params: { status } }),

  // ------------------------------------------------------------------ queue
  getQueue: (params?: { campaign_id?: number; limit?: number }) =>
    request<QueueCard[]>("/queue", { params }),
  queueDone: (jobId: number) =>
    request<Job>(`/queue/${jobId}/done`, { method: "POST" }),
  queueSkip: (jobId: number) =>
    request<Job>(`/queue/${jobId}/skip`, { method: "POST" }),

  // ----------------------------------------------------------- applications
  listApplications: (params?: Params) =>
    request<Page<Application>>("/applications", { params }),
  patchApplication: (id: number, payload: Partial<Application>) =>
    request<Application>(`/applications/${id}`, {
      method: "PATCH",
      body: body(payload),
    }),
  exportUrl: () => "/api/applications/export.csv",

  // -------------------------------------------------------------- analytics
  getAnalytics: () => request<Analytics>("/analytics"),

  // --------------------------------------------------------------- settings
  getSettings: () => request<AppSettings>("/settings"),
  saveSettings: (payload: Partial<AppSettings>) =>
    request<AppSettings>("/settings", { method: "PUT", body: body(payload) }),
  recentRuns: (limit = 20) =>
    request<Record<string, unknown>[]>("/settings/runs", { params: { limit } }),
  healthSummary: () => request<Record<string, unknown>>("/settings/health-summary"),

  // -------------------------------------------------------------- documents
  jobDocuments: (jobId: number) => request<DocumentRef[]>(`/documents/job/${jobId}`),
  buildDocuments: (jobId: number, force = false) =>
    request<Record<string, unknown>>(`/documents/job/${jobId}/build`, {
      method: "POST",
      params: { force },
    }),
  /** Documents are served as files, not JSON — link to this, do not fetch it. */
  documentUrl: (documentId: number) => `/api/documents/${documentId}/file`,
};
