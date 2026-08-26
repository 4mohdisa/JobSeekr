// ONE editor for all three template kinds.
//
// Resume, cover letter and outbound email share a placeholder vocabulary and a
// validator, so they share an editor. Three editors would mean three
// autocomplete lists drifting apart from the one the backend actually knows.

import { useEffect, useMemo, useRef, useState } from "react";
import type { PlaceholderIssue, TemplatePreview } from "../lib/types";
import { Button, Textarea, cx } from "./ui";

export interface TemplateEditorProps {
  value: string;
  onChange: (next: string) => void;
  preview: TemplatePreview | undefined;
  previewing: boolean;
  onPreview: () => void;
  onCompile?: () => void;
  compiling?: boolean;
  actions?: React.ReactNode;
}

function issueTone(kind: string): string {
  // A wrong-delimiter mistake renders as literal text in a real application;
  // an unknown field just fails to substitute. Both are errors, but the first
  // is the one that reaches an employer looking broken.
  return kind === "wrong_delimiters" || kind === "syntax_error"
    ? "border-bad/40 bg-bad/10 text-bad"
    : "border-warn/40 bg-warn/10 text-warn";
}

export function TemplateEditor({
  value,
  onChange,
  preview,
  previewing,
  onPreview,
  onCompile,
  compiling = false,
  actions,
}: TemplateEditorProps) {
  const textarea = useRef<HTMLTextAreaElement>(null);
  const [completion, setCompletion] = useState<string[]>([]);

  const placeholders = useMemo(() => {
    const known = preview?.known_placeholders ?? {};
    return Object.entries(known).flatMap(([root, fields]) =>
      fields.map((field) => `${root}.${field}`),
    );
  }, [preview?.known_placeholders]);

  // Re-preview shortly after typing stops: validation is the whole point of
  // this editor, and a button nobody presses validates nothing.
  useEffect(() => {
    const timer = window.setTimeout(onPreview, 700);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);

  const insert = (placeholder: string) => {
    const element = textarea.current;
    const snippet = `\\VAR{${placeholder}}`;
    if (!element) {
      onChange(value + snippet);
      return;
    }
    const start = element.selectionStart ?? value.length;
    const end = element.selectionEnd ?? start;
    onChange(value.slice(0, start) + snippet + value.slice(end));
    setCompletion([]);
    window.setTimeout(() => {
      element.focus();
      element.selectionStart = element.selectionEnd = start + snippet.length;
    }, 0);
  };

  const onKeyUp = () => {
    const element = textarea.current;
    if (!element) return;
    const upto = value.slice(0, element.selectionStart ?? 0);
    const match = /\\VAR\{([A-Za-z_.]*)$/.exec(upto);
    if (!match) {
      setCompletion([]);
      return;
    }
    const prefix = match[1].toLowerCase();
    setCompletion(placeholders.filter((p) => p.toLowerCase().startsWith(prefix)).slice(0, 8));
  };

  const issues: PlaceholderIssue[] = preview?.issues ?? [];

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <div>
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <Button type="button" onClick={onPreview} disabled={previewing}>
            {previewing ? "Rendering…" : "Refresh preview"}
          </Button>
          {onCompile && (
            <Button type="button" variant="primary" onClick={onCompile} disabled={compiling}>
              {compiling ? "Compiling…" : "Compile preview PDF"}
            </Button>
          )}
          {actions}
        </div>

        <Textarea
          ref={textarea}
          value={value}
          rows={26}
          spellCheck={false}
          onChange={(event) => onChange(event.target.value)}
          onKeyUp={onKeyUp}
          className="font-mono text-xs leading-relaxed"
        />

        {completion.length > 0 && (
          <div className="mt-1 flex flex-wrap gap-1">
            {completion.map((placeholder) => (
              <button
                key={placeholder}
                type="button"
                onClick={() => insert(placeholder)}
                className="rounded border border-accent/40 bg-accent/10 px-1.5 py-0.5 font-mono text-xs text-accent hover:bg-accent/20"
              >
                {placeholder}
              </button>
            ))}
          </div>
        )}

        <details className="mt-3">
          <summary className="cursor-pointer text-xs text-ink-400">
            Available placeholders ({placeholders.length})
          </summary>
          <div className="mt-2 flex flex-wrap gap-1">
            {placeholders.map((placeholder) => (
              <button
                key={placeholder}
                type="button"
                onClick={() => insert(placeholder)}
                className="rounded border border-ink-700 bg-ink-850 px-1.5 py-0.5 font-mono text-xs text-ink-300 hover:border-accent hover:text-accent"
              >
                {placeholder}
              </button>
            ))}
          </div>
          <p className="mt-2 text-xs text-ink-600">
            LaTeX owns <code>{"{ }"}</code> and <code>%</code>, so this project uses{" "}
            <code>{"\\VAR{...}"}</code>, <code>{"\\BLOCK{...}"}</code> and{" "}
            <code>{"\\#{...}"}</code>. Writing <code>{"{{job.company}}"}</code> renders as
            literal text in a real application.
          </p>
        </details>
      </div>

      <div>
        {issues.length > 0 && (
          <div className="mb-3 space-y-1">
            {issues.map((issue, index) => (
              <div
                key={index}
                className={cx("rounded border px-2 py-1.5 text-xs", issueTone(issue.kind))}
              >
                <code className="font-mono">{issue.placeholder}</code> — {issue.detail}
              </div>
            ))}
          </div>
        )}

        {preview?.error && (
          <div className="mb-3 rounded border border-bad/40 bg-bad/10 px-2 py-1.5 text-xs text-bad">
            {preview.error}
          </div>
        )}

        {preview?.ai_slots && preview.ai_slots.length > 0 && (
          <p className="mb-2 text-xs text-ink-400">
            AI slots in use: {preview.ai_slots.join(", ")} — generated per job and validated
            against the profile before any document is built.
          </p>
        )}

        <div className="rounded border border-ink-800 bg-ink-950">
          <div className="border-b border-ink-800 px-3 py-1.5 text-xs text-ink-400">
            Rendered against{" "}
            {preview?.job_id ? `job #${preview.job_id}` : "no job yet"}
          </div>
          <pre className="max-h-[32rem] overflow-auto p-3 font-mono text-xs whitespace-pre-wrap text-ink-300">
            {preview?.rendered || "—"}
          </pre>
        </div>
      </div>
    </div>
  );
}
