// Add / remove / reorder rows of an arbitrary sub-form.
//
// The Profile page uses this five times over — experience, projects, education,
// certifications, skills. Generic over the row type so each section supplies
// only its own fields.

import type { ReactNode } from "react";
import { Button } from "./ui";

export interface DynamicFieldListProps<T> {
  label: string;
  hint?: string;
  items: T[];
  onChange: (next: T[]) => void;
  /** A fresh, empty row. Called when the user adds one. */
  blank: () => T;
  /** Renders one row's fields. `update` replaces this row. */
  renderRow: (item: T, update: (next: T) => void, index: number) => ReactNode;
  /** Optional short summary shown next to the row controls. */
  summary?: (item: T, index: number) => string;
  addLabel?: string;
}

export function DynamicFieldList<T>({
  label,
  hint,
  items,
  onChange,
  blank,
  renderRow,
  summary,
  addLabel = "Add",
}: DynamicFieldListProps<T>) {
  const replace = (index: number, next: T) =>
    onChange(items.map((item, i) => (i === index ? next : item)));

  const remove = (index: number) => onChange(items.filter((_, i) => i !== index));

  const move = (index: number, delta: number) => {
    const target = index + delta;
    if (target < 0 || target >= items.length) return;
    const next = [...items];
    [next[index], next[target]] = [next[target], next[index]];
    onChange(next);
  };

  return (
    <section className="mb-6">
      <header className="mb-2 flex items-center justify-between">
        <div>
          <h3 className="text-sm font-semibold text-ink-100">{label}</h3>
          {hint && <p className="text-xs text-ink-400">{hint}</p>}
        </div>
        <Button type="button" onClick={() => onChange([...items, blank()])}>
          + {addLabel}
        </Button>
      </header>

      {items.length === 0 ? (
        <p className="rounded border border-dashed border-ink-700 px-3 py-4 text-center text-xs text-ink-600">
          No {label.toLowerCase()} yet.
        </p>
      ) : (
        <ol className="space-y-3">
          {items.map((item, index) => (
            <li key={index} className="rounded border border-ink-800 bg-ink-850 p-3">
              <div className="mb-2 flex items-center justify-between gap-2">
                <span className="text-xs text-ink-400">
                  {summary ? summary(item, index) : `#${index + 1}`}
                </span>
                <div className="flex items-center gap-1">
                  <Button
                    type="button"
                    variant="quiet"
                    onClick={() => move(index, -1)}
                    disabled={index === 0}
                    aria-label="Move up"
                  >
                    ↑
                  </Button>
                  <Button
                    type="button"
                    variant="quiet"
                    onClick={() => move(index, 1)}
                    disabled={index === items.length - 1}
                    aria-label="Move down"
                  >
                    ↓
                  </Button>
                  <Button
                    type="button"
                    variant="quiet"
                    className="text-bad"
                    onClick={() => remove(index)}
                    aria-label="Remove"
                  >
                    ✕
                  </Button>
                </div>
              </div>
              {renderRow(item, (next) => replace(index, next), index)}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

/** The common case: a plain list of strings (skills, search terms, locations). */
export function StringList({
  label,
  hint,
  values,
  onChange,
  placeholder,
}: {
  label: string;
  hint?: string;
  values: string[];
  onChange: (next: string[]) => void;
  placeholder?: string;
}) {
  return (
    <div className="mb-4">
      <label className="mb-1 block text-xs font-medium tracking-wide text-ink-400 uppercase">
        {label}
      </label>
      {hint && <p className="mb-1 text-xs text-ink-600">{hint}</p>}
      <div className="flex flex-wrap gap-1.5">
        {values.map((value, index) => (
          <span
            key={index}
            className="inline-flex items-center gap-1 rounded border border-ink-700 bg-ink-850 px-2 py-1 text-xs"
          >
            {value}
            <button
              type="button"
              className="text-ink-600 hover:text-bad"
              onClick={() => onChange(values.filter((_, i) => i !== index))}
              aria-label={`Remove ${value}`}
            >
              ✕
            </button>
          </span>
        ))}
      </div>
      <input
        className="mt-2 w-full rounded border border-ink-700 bg-ink-900 px-2 py-1.5 text-sm placeholder:text-ink-600 focus:border-accent focus:outline-none"
        placeholder={placeholder ?? "Type and press Enter"}
        onKeyDown={(event) => {
          if (event.key !== "Enter") return;
          event.preventDefault();
          const value = event.currentTarget.value.trim();
          if (!value) return;
          onChange([...values, value]);
          event.currentTarget.value = "";
        }}
      />
    </div>
  );
}
