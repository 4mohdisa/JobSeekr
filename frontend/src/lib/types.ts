// Mirrors backend/api/schemas.py. Kept hand-written rather than generated so
// the shapes the UI actually consumes stay obvious at a glance; the API tests
// are what stop the two drifting.

export type JobStatus =
  | "discovered"
  | "scored"
  | "rejected"
  | "queued"
  | "documents_ready"
  | "needs_answer"
  | "applying"
  | "applied"
  | "failed"
  | "manual_queue"
  | "skipped"
  | "ghosted";

export type ApplyType =
  | "quick_apply"
  | "easy_apply"
  | "external"
  | "unknown"
  | "manual_only";

export type ApplicationOutcome = "submitted" | "failed" | "aborted";

export type ResponseStatus =
  | "none"
  | "acknowledged"
  | "rejected"
  | "interview_request"
  | "recruiter_outreach"
  | "ghosted";

export type MatchType = "exact" | "regex" | "fuzzy";
export type AnswerType = "text" | "boolean" | "choice" | "number" | "date";
export type TemplateKind = "resume" | "cover_letter" | "email";
export type GrayZoneAction = "apply" | "skip" | "ask" | "queue";
export type DocumentKind = "resume" | "cover_letter" | "combined";

export interface Page<T> {
  items: T[];
  total: number;
  offset: number;
  limit: number;
}

export interface Profile {
  id: number;
  version: number;
  created_at: string;
  identity: Record<string, unknown>;
  work_rights: Record<string, unknown>;
  experience: Record<string, unknown>[];
  projects: Record<string, unknown>[];
  education: Record<string, unknown>[];
  certifications: Record<string, unknown>[];
  skills: string[];
  preferences: Record<string, unknown>;
}

export interface Campaign {
  id: number;
  name: string;
  active: boolean;
  search_terms: string[];
  locations: string[];
  salary_floor: number | null;
  work_types: string[];
  exclusions: Record<string, unknown>;
  score_floor: number;
  score_auto_apply: number;
  gray_zone_action: GrayZoneAction;
  daily_caps: Record<string, number>;
  target_goal_type: string | null;
  target_goal_count: number | null;
  template_ids: Record<string, number>;
  rubric: Record<string, unknown>;
  rubric_version: number;
  created_at: string;
  updated_at: string;
  applied_today: number;
}

export interface Answer {
  id: number;
  question_pattern: string;
  match_type: MatchType;
  answer_value: string;
  answer_type: AnswerType;
  campaign_id: number | null;
  choices: string[] | null;
  notes: string | null;
  verified_at: string | null;
  updated_at: string;
}

export interface Template {
  id: number;
  kind: TemplateKind;
  name: string;
  body: string;
  campaign_id: number | null;
  is_default: boolean;
  version: number;
  updated_at: string;
}

export interface PlaceholderIssue {
  placeholder: string;
  kind: string;
  detail: string;
}

export interface TemplatePreview {
  job_id: number | null;
  rendered: string;
  issues: PlaceholderIssue[];
  ai_slots: string[];
  known_placeholders: Record<string, string[]>;
  pdf_path: string | null;
  pdf_document_id: number | null;
  error: string | null;
}

export interface Score {
  stage1: number | null;
  stage2: number | null;
  final: number | null;
  reasoning: string | null;
  matched_skills: string[];
  gaps: string[];
  red_flags: string[];
  rubric_version: number;
  profile_version: number;
  scored_at: string;
}

export interface DocumentRef {
  id: number;
  kind: DocumentKind;
  parse_check_passed: boolean;
  built_at: string;
  template_version: number | null;
}

export interface Job {
  id: number;
  source: string;
  url: string;
  title: string;
  company: string;
  location: string | null;
  salary_min: number | null;
  salary_max: number | null;
  salary_basis: string | null;
  salary_is_estimated: boolean;
  posted_at: string | null;
  discovered_at: string;
  apply_type: ApplyType;
  status: JobStatus;
  campaign_id: number | null;
  ad_contact_email: string | null;
  score: number | null;
}

export interface JobDetail extends Job {
  description: string | null;
  score_detail: Score | null;
  documents: DocumentRef[];
}

export interface CopyableAnswer {
  question: string;
  value: string;
  answered: boolean;
}

export interface QueueCard {
  job: Job;
  score: number | null;
  reasoning: string | null;
  apply_url: string;
  resume_document_id: number | null;
  cover_letter_document_id: number | null;
  combined_document_id: number | null;
  cover_letter_text: string;
  answers: CopyableAnswer[];
  unanswered_questions: string[];
}

export interface Application {
  id: number;
  job_id: number;
  applied_at: string;
  outcome: ApplicationOutcome;
  response_status: ResponseStatus;
  response_at: string | null;
  failure_reason: string | null;
  platform: string | null;
  user_notes: string | null;
  attachment_readback: string | null;
  resume_doc_id: number | null;
  cover_letter_doc_id: number | null;
  answers_given: Record<string, unknown>;
  job_title: string | null;
  job_company: string | null;
  job_url: string | null;
}

export interface AnalyticsBucket {
  key: string;
  applied: number;
  acknowledged: number;
  replied: number;
  interviews: number;
  /** False when n is below the reporting minimum. The UI must grey the row
   *  out rather than render a rate — see Analytics.tsx. */
  sufficient_data: boolean;
  interview_rate: number | null;
  any_reply_rate: number | null;
}

export interface Analytics {
  minimum_sample: number;
  total_applied: number;
  funnel: { stage: string; count: number }[];
  by_campaign: AnalyticsBucket[];
  by_platform: AnalyticsBucket[];
  by_score_decile: AnalyticsBucket[];
  by_rubric_version: AnalyticsBucket[];
}

export interface AppSettings {
  llm_monthly_cap_usd: number;
  apply_window_start: string;
  apply_window_end: string;
  apply_min_interval_floor_seconds: number;
  scoring_stage1_top_n: number;
  scoring_cost_target_usd: number;
  discovery_default_hours_old: number;
  timezone: string;
  /** Read-only. Env-only switch; the API refuses to set it. */
  allow_live_submit: boolean;
  spend: Record<string, number | string>;
  circuit_breakers: Record<string, { disabled: boolean; consecutive_failures: number }>;
}

export interface ControlState {
  stopped: boolean;
  stop_file: string;
  reason: string | null;
}


/** Where a preference came from. This is a safety boundary, not metadata:
 *  facts about the user are never "inferred". */
export type PreferenceSource = "user_set" | "asked" | "inferred";

/** Only "active" affects behaviour. "proposed" is waiting on the user. */
export type PreferenceStatus = "active" | "proposed" | "rejected" | "retired";

export interface Preference {
  id: number;
  key: string;
  value: string;
  value_type: AnswerType;
  scope: "global" | "campaign";
  campaign_id: number | null;
  source: PreferenceSource;
  status: PreferenceStatus;
  confidence: number;
  times_confirmed: number;
  times_ignored: number;
  /** Why it was proposed, in the user's terms. */
  evidence: string | null;
  learned_at: string;
  confirmed_at: string | null;
}


export interface Fact {
  id: number;
  key: string;
  /** The user's own words. Never rewritten by the system. */
  text: string;
  category: string;
  /** null means the fact holds everywhere. "AU"/"NZ" scopes it to one country. */
  jurisdiction: string | null;
  updated_at: string;
}

export interface DerivedAnswer {
  id: number;
  question_key: string;
  question_text: string;
  answer_value: string;
  answer_type: AnswerType;
  fact_id: number | null;
  region: string | null;
  reasoning: string | null;
  /** null until confirmed. An unconfirmed derivation answers nothing. */
  confirmed_at: string | null;
  /** The source fact was edited, so this no longer applies. */
  stale: boolean;
}


export type SessionStatus = "live" | "dead" | "unknown" | "no_session" | "unreachable";

export interface SessionHealth {
  id: number;
  site: string;
  status: SessionStatus;
  detail: string | null;
  cookie_count: number;
  last_checked_at: string | null;
  /** Last time the session was confirmed LIVE — not merely checked. */
  last_verified_at: string | null;
  consecutive_failures: number;
}


export type OutboundStatus = "drafted" | "sent" | "skipped";

export interface OutboundMessage {
  id: number;
  job_id: number;
  /** From the ad. Never editable — the API has no field for it. */
  to_address: string;
  subject: string;
  body: string;
  attachments: string[];
  status: OutboundStatus;
  approved_by: string | null;
  created_at: string;
  sent_at: string | null;
}
