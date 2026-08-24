// Data fetching, formatting and clipboard — the small things every page needs.
//
// One `useAsync` rather than a query library: this dashboard talks to a local
// FastAPI process, so there is no cache-invalidation problem worth a dependency
// and no network latency to paper over. Everything here is shared so no page
// grows its own date formatter.

import { useCallback, useEffect, useRef, useState } from "react";

export interface AsyncState<T> {
  data: T | undefined;
  error: Error | undefined;
  loading: boolean;
  reload: () => void;
}

export function useAsync<T>(loader: () => Promise<T>, deps: unknown[] = []): AsyncState<T> {
  const [data, setData] = useState<T | undefined>(undefined);
  const [error, setError] = useState<Error | undefined>(undefined);
  const [loading, setLoading] = useState(true);
  const [nonce, setNonce] = useState(0);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  useEffect(() => {
    setLoading(true);
    loader()
      .then((result) => {
        if (!alive.current) return;
        setData(result);
        setError(undefined);
      })
      .catch((caught: Error) => {
        if (!alive.current) return;
        setError(caught);
      })
      .finally(() => {
        if (alive.current) setLoading(false);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  return { data, error, loading, reload };
}

/** Copy to clipboard, reporting which key was copied so the UI can confirm it.
 *
 *  The visible confirmation matters on the Queue page: without it there is no
 *  way to tell a successful copy from a missed tap, and re-checking costs more
 *  time than the copy saved. */
export function useClipboard(resetMs = 1400) {
  const [copied, setCopied] = useState<string | null>(null);

  const copy = useCallback(
    async (text: string, key: string) => {
      try {
        await navigator.clipboard.writeText(text);
      } catch {
        // Older/insecure contexts have no clipboard API.
        const area = document.createElement("textarea");
        area.value = text;
        area.style.position = "fixed";
        area.style.opacity = "0";
        document.body.appendChild(area);
        area.select();
        document.execCommand("copy");
        document.body.removeChild(area);
      }
      setCopied(key);
      window.setTimeout(() => setCopied((current) => (current === key ? null : current)), resetMs);
    },
    [resetMs],
  );

  return { copied, copy };
}

/** Elapsed seconds since mount, for the Queue page's 90-second target. */
export function useElapsedSeconds(running: boolean): number {
  const [seconds, setSeconds] = useState(0);
  const start = useRef(Date.now());

  useEffect(() => {
    if (!running) return;
    start.current = Date.now();
    setSeconds(0);
    const timer = window.setInterval(
      () => setSeconds(Math.floor((Date.now() - start.current) / 1000)),
      1000,
    );
    return () => window.clearInterval(timer);
  }, [running]);

  return seconds;
}

// ---------------------------------------------------------------- formatting
// UTC in the database, local time in the UI (Claude.md).

export function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleDateString(undefined, {
    day: "2-digit",
    month: "short",
    year: "numeric",
  });
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString(undefined, {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatRelative(value: string | null | undefined): string {
  if (!value) return "—";
  const then = new Date(value).getTime();
  if (Number.isNaN(then)) return "—";
  const seconds = Math.round((Date.now() - then) / 1000);
  if (seconds < 60) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

export function formatMoney(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined) return "—";
  return `$${value.toFixed(digits)}`;
}

export function formatSalary(
  min: number | null,
  max: number | null,
  basis: string | null,
  estimated: boolean,
): string {
  if (min === null && max === null) return "not stated";
  const short = (value: number) => `$${Math.round(value / 1000)}k`;
  const range =
    min !== null && max !== null
      ? `${short(min)}–${short(max)}`
      : short((min ?? max) as number);
  // Never present a derived figure as though the employer stated it.
  return estimated ? `${range} (est. from ${basis})` : range;
}

export function formatScore(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : value.toFixed(0);
}

export function formatPercent(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : `${(value * 100).toFixed(0)}%`;
}
