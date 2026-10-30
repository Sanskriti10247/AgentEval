"use client";

import type { ReactNode } from "react";

// Shared chart chrome. Colours come from CSS variables (globals.css) so both themes apply;
// marks use --chart-1 / --chart-2 or a status tone, text always uses the ink tokens.

export const AXIS_TICK = { fontSize: 10.5, fill: "var(--faint)", fontFamily: "var(--font-mono)" } as const;
export const AXIS_LABEL = { fontSize: 11.5, fill: "var(--faint)", fontFamily: "var(--font-sans)" } as const;
export const GRID_STROKE = "var(--rule)";
export const AXIS_STROKE = "var(--rule-strong)";

/** The tooltip box: the values lead, each keyed by a short stroke of its mark colour. */
export function TooltipBox({ title, rows, footer }: { title: ReactNode; rows: { color?: string; label: string; value: ReactNode }[]; footer?: ReactNode }) {
  return (
    <div className="min-w-[11rem] rounded-[5px] border border-rule-strong bg-surface px-2.5 py-2 font-mono text-[11.5px] text-ink shadow-[0_2px_8px_rgb(0_0_0/0.08)]">
      <div className="mb-1 font-sans text-[12px] font-medium text-ink-2">{title}</div>
      <table className="w-full border-collapse">
        <tbody>
          {rows.map((r) => (
            <tr key={r.label}>
              <td className="py-px pr-2 align-middle">
                {r.color ? <span aria-hidden="true" className="inline-block h-[2px] w-2.5 align-middle" style={{ background: r.color }} /> : null}
              </td>
              <td className="py-px pr-3 text-right font-medium tabular-nums">{r.value}</td>
              <td className="py-px text-muted">{r.label}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {footer ? <div className="mt-1 border-t border-rule pt-1 text-[11px] text-faint">{footer}</div> : null}
    </div>
  );
}

/** A legend row above a chart: a swatch shaped like the mark, then plain text. */
export function ChartLegend({ items }: { items: { color: string; label: string; hollow?: boolean }[] }) {
  return (
    <ul aria-label="Legend" className="mb-2 flex flex-wrap gap-x-4 gap-y-1 text-[12px] text-muted">
      {items.map((i) => (
        <li key={i.label} className="inline-flex items-center gap-1.5">
          <span
            aria-hidden="true"
            className="inline-block size-[9px] rounded-[2px]"
            style={i.hollow ? { border: `1.5px solid ${i.color}` } : { background: i.color }}
          />
          {i.label}
        </li>
      ))}
    </ul>
  );
}

/** The chart's numbers as a table, folded away; the dependable path for screen readers and copying. */
export function TableView({ children }: { children: ReactNode }) {
  return (
    <details className="mt-2">
      <summary className="cursor-pointer text-[12px] text-faint hover:text-ink">Show as table</summary>
      <div className="mt-2 max-h-72 overflow-auto">{children}</div>
    </details>
  );
}
