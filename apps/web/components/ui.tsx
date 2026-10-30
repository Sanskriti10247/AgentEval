"use client";

import Link from "next/link";
import type {
  ButtonHTMLAttributes,
  InputHTMLAttributes,
  ReactNode,
  SelectHTMLAttributes,
} from "react";
import { ApiError } from "@/lib/api/client";
import type { EvaluationStatus, RunStatus } from "@/lib/api/types";
import { shortId } from "@/lib/format";
import { categoryTone, EVALUATION_STATUS_TONE, RUN_STATUS_TONE, type Tone } from "@/lib/status";

// ---- status marks --------------------------------------------------------------
// Status is a small coloured glyph next to plain text, never a filled pill. Each tone
// also has its own shape, so the marks still read in greyscale or for colour-blind eyes.

const TONE_TEXT: Record<Tone, string> = {
  neutral: "text-neutral",
  info: "text-info",
  active: "text-active",
  warn: "text-warn",
  good: "text-good",
  bad: "text-bad",
  muted: "text-dim",
};

export function Glyph({ tone, className = "" }: { tone: Tone; className?: string }) {
  const cls = `inline-block shrink-0 ${TONE_TEXT[tone]} ${className}`;
  const common = { width: 9, height: 9, viewBox: "0 0 10 10", "aria-hidden": true, className: cls } as const;
  switch (tone) {
    case "good":
      return (
        <svg {...common}>
          <circle cx="5" cy="5" r="4" fill="currentColor" />
        </svg>
      );
    case "bad":
      return (
        <svg {...common}>
          <rect x="1" y="1" width="8" height="8" fill="currentColor" />
        </svg>
      );
    case "active":
      return (
        <svg {...common}>
          <circle cx="5" cy="5" r="3.9" fill="none" stroke="currentColor" strokeWidth="1.3" />
          <circle cx="5" cy="5" r="1.7" fill="currentColor" />
        </svg>
      );
    case "warn":
      return (
        <svg {...common}>
          <path d="M5 0.8 9.4 9H0.6Z" fill="currentColor" />
        </svg>
      );
    case "info":
      return (
        <svg {...common}>
          <path d="M5 0.8 9.2 5 5 9.2 0.8 5Z" fill="none" stroke="currentColor" strokeWidth="1.3" />
        </svg>
      );
    case "muted":
      return (
        <svg {...common}>
          <rect x="1" y="4.2" width="8" height="1.6" fill="currentColor" />
        </svg>
      );
    default:
      return (
        <svg {...common}>
          <circle cx="5" cy="5" r="3.9" fill="none" stroke="currentColor" strokeWidth="1.3" />
        </svg>
      );
  }
}

export function StatusMark({ tone, children, title }: { tone: Tone; children: ReactNode; title?: string }) {
  return (
    <span title={title} className="inline-flex items-center gap-1.5 whitespace-nowrap font-mono text-[11.5px] tracking-[0.02em] text-ink-2">
      <Glyph tone={tone} />
      {children}
    </span>
  );
}

/** Kept for call sites that want a quiet outlined label (tools, task types). */
export function Tag({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <span className={`inline-block whitespace-nowrap rounded-[2px] border border-rule px-1.5 font-mono text-[11px] leading-[18px] text-ink-2 ${className}`}>
      {children}
    </span>
  );
}

export const RunStatusBadge = ({ status }: { status: RunStatus }) => (
  <StatusMark tone={RUN_STATUS_TONE[status]}>{status}</StatusMark>
);

export const EvaluationStatusBadge = ({ status }: { status: EvaluationStatus }) => (
  <StatusMark tone={EVALUATION_STATUS_TONE[status]}>{status}</StatusMark>
);

export const CategoryBadge = ({ category }: { category: string | null | undefined }) => (
  <StatusMark tone={categoryTone(category)}>{category ?? "none"}</StatusMark>
);

/** A small "live" marker shown while a view is polling. */
export function Live({ label = "live" }: { label?: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 font-mono text-[11px] uppercase tracking-[0.08em] text-accent" title="Refreshing automatically">
      <span className="relative inline-flex size-[7px]">
        <span className="absolute inset-0 animate-ping rounded-full bg-accent opacity-40 motion-reduce:hidden" />
        <span className="relative inline-flex size-[7px] rounded-full bg-accent" />
      </span>
      {label}
    </span>
  );
}

// ---- page structure ------------------------------------------------------------

export function PageHeader({
  title,
  eyebrow,
  subtitle,
  actions,
}: {
  title: ReactNode;
  eyebrow?: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header className="mb-7 border-b border-rule pb-4">
      <div className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
        <div className="min-w-0">
          {eyebrow ? (
            <div className="mb-1.5 font-mono text-[11px] uppercase tracking-[0.12em] text-faint">{eyebrow}</div>
          ) : null}
          <h1 className="break-words font-display text-[26px] font-semibold leading-[1.1] tracking-[-0.01em] text-ink">
            {title}
          </h1>
          {subtitle ? <div className="mt-2 max-w-[68ch] text-[13.5px] leading-relaxed text-muted">{subtitle}</div> : null}
        </div>
        {actions ? <div className="flex flex-wrap items-end gap-2">{actions}</div> : null}
      </div>
    </header>
  );
}

/** A titled section: a small caps label over a hairline, no box around the content. */
export function Section({
  title,
  actions,
  note,
  children,
  className = "",
}: {
  title?: string;
  actions?: ReactNode;
  note?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`min-w-0 ${className}`}>
      {title ? (
        <div className="mb-3 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 border-b border-rule pb-1.5">
          <h2 className="font-mono text-[11px] font-medium uppercase tracking-[0.12em] text-muted">{title}</h2>
          {note ? <div className="text-xs text-faint">{note}</div> : null}
          {actions ? <div className="flex items-center gap-2">{actions}</div> : null}
        </div>
      ) : null}
      {children}
    </section>
  );
}

/** Kept as an alias: older call sites name it Panel. */
export const Panel = Section;

/** A strip of instrument readouts separated by hairlines. */
const READOUT_COLS = { 2: "sm:grid-cols-2", 3: "sm:grid-cols-3", 4: "sm:grid-cols-4" } as const;

export function Readouts({ children, className = "", cols = 4 }: { children: ReactNode; className?: string; cols?: 2 | 3 | 4 }) {
  return (
    <dl className={`grid grid-cols-2 gap-px overflow-hidden border-y border-rule bg-rule ${READOUT_COLS[cols]} ${className}`}>
      {children}
    </dl>
  );
}

export function Stat({ label, value, hint, meter }: { label: string; value: ReactNode; hint?: ReactNode; meter?: ReactNode }) {
  return (
    <div className="min-w-0 bg-paper px-4 py-3 [&:nth-child(2n+1)]:pl-0 sm:[&:nth-child(2n+1)]:pl-4 sm:[&:nth-child(4n+1)]:pl-0">
      <dt className="text-[12px] text-muted">{label}</dt>
      <dd className="mt-1 break-words font-mono text-[18px] leading-tight sm:text-[21px] tracking-[-0.02em] text-ink tabular-nums">{value}</dd>
      {meter ? <div className="mt-2">{meter}</div> : null}
      {hint ? <div className="mt-1 text-[12px] leading-snug text-faint">{hint}</div> : null}
    </div>
  );
}

/** A thin horizontal meter for a 0..1 value. Empty track when the value is unknown. */
export function Meter({
  value,
  tone = "ink",
  className = "w-full",
  label,
}: {
  value: number | null | undefined;
  tone?: "ink" | "good" | "bad" | "accent";
  className?: string;
  label?: string;
}) {
  const v = value === null || value === undefined || Number.isNaN(value) ? null : Math.max(0, Math.min(1, value));
  const fill = { ink: "bg-ink-2", good: "bg-good", bad: "bg-bad", accent: "bg-accent" }[tone];
  return (
    <div
      role={label ? "img" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      className={`relative h-[3px] bg-sunken ${className}`}
    >
      {v !== null ? <div className={`absolute inset-y-0 left-0 ${fill}`} style={{ width: `${v * 100}%` }} /> : null}
    </div>
  );
}

/** A rate as text with a short meter after it; for table cells. */
export function RateCell({ rate, text }: { rate: number | null | undefined; text: string }) {
  return (
    <span className="inline-flex items-center justify-end gap-2">
      <span className="font-mono tabular-nums">{text}</span>
      <Meter value={rate} className="w-10" />
    </span>
  );
}

export const Mono = ({ children, className = "" }: { children: ReactNode; className?: string }) => (
  <span className={`font-mono tabular-nums ${className}`}>{children}</span>
);

export function IdLink({ href, id, label }: { href?: string; id: string; label?: string }) {
  const content = <span className="font-mono text-[12.5px]">{label ?? shortId(id)}</span>;
  return href ? (
    <Link href={href} title={id}>
      {content}
    </Link>
  ) : (
    <span title={id} className="text-ink-2">
      {content}
    </span>
  );
}

/** "2026-10-17 12:00:00" with the date dimmed, so a column of times scans by the time. */
export function Timestamp({ value }: { value: string }) {
  if (value.length < 11 || value[10] !== " ") return <span className="font-mono text-[12.5px] text-faint">{value}</span>;
  return (
    <span className="whitespace-nowrap font-mono text-[12.5px] tabular-nums">
      <span className="text-faint">{value.slice(0, 10)}</span> <span className="text-ink-2">{value.slice(11)}</span>
    </span>
  );
}

// ---- states --------------------------------------------------------------------

export function Loading({ label = "Loading" }: { label?: string }) {
  return (
    <div role="status" className="flex items-center gap-2 py-8 font-mono text-[12px] text-faint">
      <span className="inline-block h-3 w-[2px] animate-pulse bg-faint motion-reduce:animate-none" />
      {label}…
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const message = error instanceof Error ? error.message : "Something went wrong.";
  const status = error instanceof ApiError && error.status > 0 ? ` (HTTP ${error.status})` : "";
  // 403 is an authorization problem of the key, not a failed load: say so.
  const heading = error instanceof ApiError && error.status === 403 ? "Not permitted" : "Could not load data";
  return (
    <div role="alert" className="border-l-2 border-bad py-1 pl-3 text-[13px]">
      <div className="font-medium text-ink">
        {heading}
        <span className="font-mono text-[12px] font-normal text-muted">{status}</span>
      </div>
      <div className="mt-0.5 break-words text-muted">{message}</div>
      {onRetry ? (
        <Button className="mt-2.5" onClick={onRetry}>
          Retry
        </Button>
      ) : null}
    </div>
  );
}

/** An inline message for a refused or failed write: plain words, no stack of codes. */
export function Notice({ tone = "bad", title, children }: { tone?: "bad" | "warn" | "info"; title?: ReactNode; children: ReactNode }) {
  const border = { bad: "border-bad", warn: "border-warn", info: "border-rule-strong" }[tone];
  return (
    <div role={tone === "info" ? "note" : "alert"} className={`border-l-2 ${border} py-0.5 pl-3 text-[13px]`}>
      {title ? <div className="font-medium text-ink">{title}</div> : null}
      <div className="break-words text-muted">{children}</div>
    </div>
  );
}

export function Empty({ title, children, action }: { title?: string; children: ReactNode; action?: ReactNode }) {
  return (
    <div className="border-y border-dashed border-rule-strong/70 py-8 pl-1 text-[13px] text-muted">
      {title ? <div className="mb-1 font-medium text-ink-2">{title}</div> : null}
      <div className="max-w-[60ch] leading-relaxed">{children}</div>
      {action ? <div className="mt-3">{action}</div> : null}
    </div>
  );
}

interface GateQuery {
  isLoading: boolean;
  isError: boolean;
  error: unknown;
  refetch: () => unknown;
  data?: unknown;
}

/**
 * Loading / error handling shared by every data view. Renders children only with data.
 * A failed background refetch keeps the last good data on screen; views that poll say
 * so themselves (see StaleBanner) rather than blanking the page.
 */
export function Gate({ query, children, loadingLabel }: { query: GateQuery; children: ReactNode; loadingLabel?: string }) {
  if (query.isLoading) return <Loading label={loadingLabel} />;
  if (query.isError && query.data === undefined) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  return <>{children}</>;
}

// ---- controls ------------------------------------------------------------------

type ButtonVariant = "default" | "primary" | "danger" | "quiet";

const BUTTON_VARIANT: Record<ButtonVariant, string> = {
  default: "border-rule-strong bg-surface text-ink hover:enabled:border-ink-2",
  primary: "border-ink bg-ink text-on-ink hover:enabled:bg-ink-2 hover:enabled:border-ink-2",
  danger: "border-rule-strong bg-surface text-bad hover:enabled:border-bad",
  quiet: "border-transparent text-muted hover:enabled:text-ink",
};

export const buttonClass = (variant: ButtonVariant = "default", className = "") =>
  `inline-flex h-[30px] items-center justify-center gap-1.5 whitespace-nowrap rounded-[3px] border px-3 text-[13px] font-medium no-underline transition-colors disabled:cursor-not-allowed disabled:opacity-45 ${BUTTON_VARIANT[variant]} ${className}`;

export function Button({ variant = "default", className = "", ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: ButtonVariant }) {
  return <button type="button" className={buttonClass(variant, className)} {...props} />;
}

/** A link that looks like a button (for navigation to a form). */
export function ButtonLink({ href, variant = "default", children, className = "" }: { href: string; variant?: ButtonVariant; children: ReactNode; className?: string }) {
  return (
    <Link href={href} className={buttonClass(variant, `hover:no-underline ${className}`)}>
      {children}
    </Link>
  );
}

/** A compact filter control: the label wraps the input, so getByLabelText works on it. */
export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-1 font-mono text-[10.5px] uppercase tracking-[0.1em] text-faint">
      {label}
      {children}
    </label>
  );
}

const CONTROL =
  "h-[30px] rounded-[3px] border border-rule-strong bg-surface px-2 text-[13px] normal-case tracking-normal text-ink hover:border-ink-2 aria-[invalid=true]:border-bad disabled:opacity-50";

export function Select({ className = "", ...props }: SelectHTMLAttributes<HTMLSelectElement>) {
  return <select className={`${CONTROL} pr-7 font-sans ${className}`} {...props} />;
}

export function Input({ className = "", ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return <input className={`${CONTROL} font-sans ${className}`} {...props} />;
}

export function LoadMore({ hasNext, loading, onClick, shown }: { hasNext: boolean; loading: boolean; onClick: () => void; shown: number }) {
  return (
    <div className="mt-3 flex items-center gap-3 font-mono text-[11.5px] text-faint">
      <span className="tabular-nums">{shown} shown</span>
      {hasNext ? (
        <Button onClick={onClick} disabled={loading}>
          {loading ? "Loading…" : "Load more"}
        </Button>
      ) : (
        <span>end of list</span>
      )}
    </div>
  );
}

// ---- tables --------------------------------------------------------------------

/** Ruled table. Scrolls inside itself on narrow screens; the page never does. */
export function Table({ children, caption }: { children: ReactNode; caption: string }) {
  return (
    <div className="-mx-4 overflow-x-auto px-4 sm:mx-0 sm:px-0">
      <table className="w-full border-collapse text-left text-[13px]">
        <caption className="sr-only">{caption}</caption>
        {children}
      </table>
    </div>
  );
}

export const Th = ({ children, className = "" }: { children?: ReactNode; className?: string }) => (
  <th
    scope="col"
    className={`whitespace-nowrap border-b border-rule-strong py-1.5 pr-4 font-mono text-[10.5px] font-normal uppercase tracking-[0.1em] text-faint last:pr-0 ${className}`}
  >
    {children}
  </th>
);

export const Td = ({ children, className = "" }: { children?: ReactNode; className?: string }) => (
  <td className={`border-b border-rule py-2 pr-4 align-baseline last:pr-0 ${className}`}>{children}</td>
);

/** Numeric cell: right-aligned, monospaced, tabular. */
export const Num = ({ children, className = "" }: { children?: ReactNode; className?: string }) => (
  <Td className={`whitespace-nowrap text-right font-mono text-[12.5px] tabular-nums ${className}`}>{children}</Td>
);

export function KeyValue({ items, columns = 1 }: { items: [string, ReactNode][]; columns?: 1 | 2 }) {
  return (
    <dl className={`grid gap-x-10 text-[13px] ${columns === 2 ? "md:grid-cols-2" : ""}`}>
      {items.map(([k, v]) => (
        <div key={k} className="grid grid-cols-[minmax(7.5rem,10rem)_1fr] gap-x-4 border-b border-rule py-1.5">
          <dt className="text-muted">{k}</dt>
          <dd className="min-w-0 break-words text-ink">{v}</dd>
        </div>
      ))}
    </dl>
  );
}

/** Provider "fake" is the built-in simulated model: no tokens are bought and costs are notional. */
export const isSimulated = (provider: string | null | undefined) => provider === "fake";

/** "Simulated" or "Real model", so a reader never mistakes a fake-model number for a real one. */
export function ProviderMark({ provider, simulated }: { provider: string | null | undefined; simulated?: boolean }) {
  const sim = simulated ?? isSimulated(provider);
  return (
    <Tag className={sim ? "text-muted" : "border-rule-strong text-ink"}>
      <span title={sim ? "Fake model provider: deterministic, no real model calls, no spend" : `Calls ${provider ?? "a real provider"}; costs real money`}>
        {sim ? "Simulated" : "Real model"}
      </span>
    </Tag>
  );
}

/** Kept for compatibility with older imports. */
export const Badge = ({ tone, children }: { tone?: Tone; children: ReactNode }) =>
  tone ? <StatusMark tone={tone}>{children}</StatusMark> : <Tag>{children}</Tag>;
