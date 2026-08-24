// The primitives everything else is built from. Small on purpose.

import type { ReactNode, Ref, TextareaHTMLAttributes } from "react";
import type { ButtonHTMLAttributes, InputHTMLAttributes, SelectHTMLAttributes } from "react";

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

const BUTTON_VARIANTS = {
  primary: "bg-accent text-ink-950 hover:brightness-110 font-medium",
  ghost: "bg-ink-800 text-ink-100 hover:bg-ink-700",
  danger: "bg-bad text-ink-950 hover:brightness-110 font-semibold",
  quiet: "bg-transparent text-ink-300 hover:bg-ink-800 hover:text-ink-100",
} as const;

export function Button({
  variant = "ghost",
  className,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: keyof typeof BUTTON_VARIANTS }) {
  return (
    <button
      className={cx(
        "inline-flex items-center justify-center gap-1.5 rounded px-3 py-1.5 text-sm",
        "transition-colors disabled:cursor-not-allowed disabled:opacity-40",
        BUTTON_VARIANTS[variant],
        className,
      )}
      {...props}
    />
  );
}

export function Input({ className, ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      className={cx(
        "w-full rounded border border-ink-700 bg-ink-900 px-2 py-1.5 text-sm text-ink-100",
        "placeholder:text-ink-600 focus:border-accent focus:outline-none",
        className,
      )}
      {...props}
    />
  );
}

export function Textarea({
  className,
  ref,
  ...props
}: TextareaHTMLAttributes<HTMLTextAreaElement> & {
  // React 19 passes `ref` as an ordinary prop to function components, so no
  // forwardRef wrapper is needed — but it still has to be declared.
  ref?: Ref<HTMLTextAreaElement>;
}) {
  return (
    <textarea
      ref={ref}
      className={cx(
        "w-full rounded border border-ink-700 bg-ink-900 px-2 py-1.5 text-sm text-ink-100",
        "placeholder:text-ink-600 focus:border-accent focus:outline-none",
        className,
      )}
      {...props}
    />
  );
}

export function Select({ className, ...props }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select
      className={cx(
        "rounded border border-ink-700 bg-ink-900 px-2 py-1.5 text-sm text-ink-100",
        "focus:border-accent focus:outline-none",
        className,
      )}
      {...props}
    />
  );
}

export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs font-medium tracking-wide text-ink-400 uppercase">
        {label}
      </span>
      {children}
      {hint && <span className="mt-1 block text-xs text-ink-600">{hint}</span>}
    </label>
  );
}

export function Card({
  title,
  actions,
  children,
  className,
}: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section
      className={cx("rounded-lg border border-ink-800 bg-ink-900", className)}
    >
      {(title || actions) && (
        <header className="flex items-center justify-between gap-3 border-b border-ink-800 px-4 py-2.5">
          <h2 className="text-sm font-semibold text-ink-100">{title}</h2>
          <div className="flex items-center gap-2">{actions}</div>
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="rounded border border-dashed border-ink-700 px-4 py-10 text-center text-sm text-ink-400">
      {children}
    </div>
  );
}

export function ErrorNote({ error }: { error: Error | undefined }) {
  if (!error) return null;
  return (
    <div className="mb-3 rounded border border-bad/40 bg-bad/10 px-3 py-2 text-sm text-bad">
      {error.message}
    </div>
  );
}

export function Spinner({ label = "Loading" }: { label?: string }) {
  return <div className="px-2 py-6 text-sm text-ink-400">{label}…</div>;
}

/** A labelled number, for the header strip and the settings page. */
export function Stat({
  label,
  value,
  tone = "normal",
}: {
  label: string;
  value: ReactNode;
  tone?: "normal" | "good" | "warn" | "bad";
}) {
  const tones = {
    normal: "text-ink-100",
    good: "text-good",
    warn: "text-warn",
    bad: "text-bad",
  } as const;
  return (
    <div>
      <div className="text-xs tracking-wide text-ink-400 uppercase">{label}</div>
      <div className={cx("tnum text-lg font-semibold", tones[tone])}>{value}</div>
    </div>
  );
}
