"use client";

import { useId, type ReactNode } from "react";

export interface ControlProps {
  id: string;
  "aria-describedby"?: string;
  "aria-invalid"?: boolean;
}

/**
 * One labelled row of a launch form: label on the left on wide screens, the control and
 * its helper text on the right. The control gets the id, describedby and invalid state.
 */
export function FormRow({
  label,
  hint,
  error,
  children,
}: {
  label: string;
  hint?: ReactNode;
  error?: string | null;
  children: (props: ControlProps) => ReactNode;
}) {
  const id = useId();
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;
  const describedBy = [error ? errorId : null, hint ? hintId : null].filter(Boolean).join(" ") || undefined;
  return (
    <div className="grid gap-x-8 gap-y-1.5 border-b border-rule py-4 sm:grid-cols-[11rem_minmax(0,1fr)]">
      <label htmlFor={id} className="pt-[5px] text-[13px] font-medium text-ink">
        {label}
      </label>
      <div className="min-w-0 max-w-[34rem]">
        {children({ id, "aria-describedby": describedBy, "aria-invalid": error ? true : undefined })}
        {error ? (
          <p id={errorId} className="mt-1.5 text-[12.5px] text-bad">
            {error}
          </p>
        ) : null}
        {hint ? (
          <p id={hintId} className="mt-1.5 text-[12.5px] leading-relaxed text-muted">
            {hint}
          </p>
        ) : null}
      </div>
    </div>
  );
}

/** What happens after Launch, as a short numbered list beside the form. */
export function NextSteps({ steps }: { steps: ReactNode[] }) {
  return (
    <aside className="text-[12.5px] leading-relaxed text-muted">
      <div className="mb-2 border-b border-rule pb-1.5 text-[13.5px] font-semibold text-ink">
        What happens next
      </div>
      <ol className="list-decimal space-y-1.5 pl-4 marker:text-faint">
        {steps.map((s, i) => (
          <li key={i}>{s}</li>
        ))}
      </ol>
    </aside>
  );
}

/** A fresh Idempotency-Key. randomUUID needs a secure context, so fall back to getRandomValues. */
export function newIdempotencyKey(): string {
  const c = globalThis.crypto;
  if (c?.randomUUID) return c.randomUUID();
  const bytes = new Uint8Array(16);
  if (c?.getRandomValues) c.getRandomValues(bytes);
  else for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
  return `web-${Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("")}`;
}
