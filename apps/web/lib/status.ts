import type { EvaluationStatus, RunStatus } from "./api/types";

export type Tone = "neutral" | "info" | "active" | "warn" | "good" | "bad" | "muted";

export const RUN_STATUS_TONE: Record<RunStatus, Tone> = {
  QUEUED: "neutral",
  CLAIMED: "info",
  RUNNING: "active",
  RETRYING: "warn",
  COMPLETED: "good",
  FAILED: "bad",
  CANCELLED: "muted",
};

export const EVALUATION_STATUS_TONE: Record<EvaluationStatus, Tone> = {
  PENDING: "neutral",
  RUNNING: "active",
  SUCCEEDED: "good",
  ERROR: "bad",
  SKIPPED: "muted",
};

// Chart fills must match the badge tones; read from CSS variables so themes apply.
export const RUN_STATUS_COLOR: Record<RunStatus, string> = {
  QUEUED: "var(--tone-neutral)",
  CLAIMED: "var(--tone-info)",
  RUNNING: "var(--tone-active)",
  RETRYING: "var(--tone-warn)",
  COMPLETED: "var(--tone-good)",
  FAILED: "var(--tone-bad)",
  CANCELLED: "var(--tone-muted)",
};

export function categoryTone(category: string | null | undefined): Tone {
  if (!category) return "muted";
  if (category === "passed") return "good";
  if (category.startsWith("platform_")) return "muted";
  if (category === "evaluation_error") return "warn";
  return "bad";
}
