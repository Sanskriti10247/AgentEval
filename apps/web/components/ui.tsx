"use client";

import Link from "next/link";
import type { ButtonHTMLAttributes, ReactNode, SelectHTMLAttributes } from "react";
import { ApiError } from "@/lib/api/client";
import type { EvaluationStatus, RunStatus } from "@/lib/api/types";
import { shortId } from "@/lib/format";
import { categoryTone, EVALUATION_STATUS_TONE, RUN_STATUS_TONE, type Tone } from "@/lib/status";

const TONE_CLASS: Record<Tone, string> = {
  neutral: "text-neutral border-neutral/40 bg-neutral/10",
  info: "text-info border-info/40 bg-info/10",
  active: "text-active border-active/40 bg-active/10",
  warn: "text-warn border-warn/40 bg-warn/10",
  good: "text-good border-good/40 bg-good/10",
  bad: "text-bad border-bad/40 bg-bad/10",
  muted: "text-dim border-dim/40 bg-dim/10",
};

export function Badge({ tone = "neutral", children }: { tone?: Tone; children: ReactNode }) {
  return (
    <span
      className={`inline-block whitespace-nowrap rounded border px-1.5 py-px font-mono text-[11px] leading-4 ${TONE_CLASS[tone]}`}
    >
      {children}
    </span>
  );
}

export const RunStatusBadge = ({ status }: { status: RunStatus }) => (
  <Badge tone={RUN_STATUS_TONE[status]}>{status}</Badge>
);

export const EvaluationStatusBadge = ({ status }: { status: EvaluationStatus }) => (
  <Badge tone={EVALUATION_STATUS_TONE[status]}>{status}</Badge>
);

export const CategoryBadge = ({ category }: { category: string | null | undefined }) => (
  <Badge tone={categoryTone(category)}>{category ?? "none"}</Badge>
);

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: ReactNode; actions?: ReactNode }) {
  return (
    <header className="mb-4 flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0">
        <h1 className="text-lg font-semibold tracking-tight">{title}</h1>
        {subtitle ? <div className="mt-0.5 text-sm text-muted">{subtitle}</div> : null}
      </div>
      {actions ? <div className="flex items-center gap-2">{actions}</div> : null}
    </header>
  );
}

export function Panel({ title, actions, children, className = "" }: { title?: string; actions?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`rounded border border-line bg-panel ${className}`}>
      {title ? (
        <div className="flex items-center justify-between border-b border-line px-3 py-2">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">{title}</h2>
          {actions}
        </div>
      ) : null}
      <div className="p-3">{children}</div>
    </section>
  );
}

export function Stat({ label, value, hint }: { label: string; value: ReactNode; hint?: ReactNode }) {
  return (
    <div className="rounded border border-line bg-panel px-3 py-2.5">
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="mt-1 font-mono text-xl tabular-nums">{value}</dd>
      {hint ? <div className="mt-0.5 text-xs text-muted">{hint}</div> : null}
    </div>
  );
}

export const Mono = ({ children, className = "" }: { children: ReactNode; className?: string }) => (
  <span className={`font-mono tabular-nums ${className}`}>{children}</span>
);

export function IdLink({ href, id, label }: { href?: string; id: string; label?: string }) {
  const content = <span className="font-mono">{label ?? shortId(id)}</span>;
  return href ? (
    <Link href={href} title={id}>
      {content}
    </Link>
  ) : (
    <span title={id}>{content}</span>
  );
}

// ---- states ------------------------------------------------------------------

export function Loading({ label = "Loading" }: { label?: string }) {
  return (
    <div role="status" className="py-8 text-center text-sm text-muted">
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
    <div role="alert" className="rounded border border-bad/40 bg-bad/10 px-3 py-3 text-sm">
      <div className="font-medium text-bad">
        {heading}
        {status}
      </div>
      <div className="mt-1 break-words text-muted">{message}</div>
      {onRetry ? (
        <Button className="mt-3" onClick={onRetry}>
          Retry
        </Button>
      ) : null}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="py-8 text-center text-sm text-muted">{children}</div>;
}

interface GateQuery {
  isLoading: boolean;
  isError: boolean;
  error: unknown;
  refetch: () => unknown;
}

/** Loading / error handling shared by every data view. Renders children only with data. */
export function Gate({ query, children, loadingLabel }: { query: GateQuery; children: ReactNode; loadingLabel?: string }) {
  if (query.isLoading) return <Loading label={loadingLabel} />;
  if (query.isError) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  return <>{children}</>;
}

// ---- controls ----------------------------------------------------------------

export function Button({ variant = "default", className = "", ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "default" | "danger" }) {
  const base =
    "rounded border px-2.5 py-1 text-sm disabled:cursor-not-allowed disabled:opacity-50 hover:enabled:bg-panel2";
  const tone = variant === "danger" ? "border-bad/50 text-bad" : "border-line text-fg";
  return <button type="button" className={`${base} ${tone} ${className}`} {...props} />;
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-1 text-xs text-muted">
      {label}
      {children}
    </label>
  );
}

export function Select({ className = "", ...props }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select
      className={`rounded border border-line bg-panel px-2 py-1 text-sm text-fg ${className}`}
      {...props}
    />
  );
}

export function LoadMore({ hasNext, loading, onClick, shown }: { hasNext: boolean; loading: boolean; onClick: () => void; shown: number }) {
  return (
    <div className="mt-3 flex items-center gap-3 text-xs text-muted">
      <span className="font-mono">{shown} shown</span>
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

// ---- tables ------------------------------------------------------------------

export function Table({ children, caption }: { children: ReactNode; caption: string }) {
  return (
    <div className="overflow-x-auto rounded border border-line">
      <table className="w-full border-collapse text-left text-[13px]">
        <caption className="sr-only">{caption}</caption>
        {children}
      </table>
    </div>
  );
}

export const Th = ({ children, className = "" }: { children?: ReactNode; className?: string }) => (
  <th scope="col" className={`whitespace-nowrap border-b border-line bg-panel2 px-3 py-1.5 text-xs font-medium text-muted ${className}`}>
    {children}
  </th>
);

export const Td = ({ children, className = "" }: { children?: ReactNode; className?: string }) => (
  <td className={`border-b border-line/60 px-3 py-1.5 align-top ${className}`}>{children}</td>
);

export function KeyValue({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1 text-[13px]">
      {items.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-muted">{k}</dt>
          <dd className="min-w-0 break-words">{v}</dd>
        </div>
      ))}
    </dl>
  );
}
