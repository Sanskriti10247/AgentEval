const DASH = "–";

export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return DASH;
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  const totalSeconds = Math.round(ms / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) return `${hours}h ${String(minutes).padStart(2, "0")}m`;
  return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
}

/** USD from a Decimal string or number. Sub-cent amounts keep four decimals so they don't read as $0.00. */
export function formatCost(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return DASH;
  const n = typeof value === "number" ? value : Number(value);
  if (Number.isNaN(n)) return DASH;
  if (n === 0) return "$0.00";
  return `$${n.toFixed(n < 1 ? 4 : 2)}`;
}

/** A 0..1 rate as a percentage; whole numbers drop the decimal ("82%", "33.3%"). */
export function formatPercent(rate: number | null | undefined): string {
  if (rate === null || rate === undefined || Number.isNaN(rate)) return DASH;
  const pct = rate * 100;
  const rounded = Math.round(pct * 10) / 10;
  return `${Number.isInteger(rounded) ? rounded.toFixed(0) : rounded.toFixed(1)}%`;
}

export function formatInt(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return DASH;
  return new Intl.NumberFormat("en-US").format(n);
}

export function formatCompact(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return DASH;
  return new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(n);
}

/** A Decimal-string score (0..1) with three decimals. */
export function formatScore(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return DASH;
  const n = typeof value === "number" ? value : Number(value);
  return Number.isNaN(n) ? DASH : n.toFixed(3);
}

export function shortId(id: string | null | undefined): string {
  return id ? id.slice(0, 8) : DASH;
}

/** UTC, deterministic: "2026-10-17 12:00:00". */
export function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return DASH;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return DASH;
  return d.toISOString().slice(0, 19).replace("T", " ");
}

/** Seconds as a coarse age: "12s", "3m", "5h", "2d". */
export function formatAge(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return DASH;
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86_400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86_400)}d`;
}

export function ageSeconds(iso: string | null | undefined, now: number = Date.now()): number | null {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  return Number.isNaN(t) ? null : (now - t) / 1000;
}

export function humanize(value: string | null | undefined): string {
  return value ? value.replace(/_/g, " ").toLowerCase() : DASH;
}

export function compactJson(value: unknown, max = 4000): string {
  const text = JSON.stringify(value, null, 2) ?? "";
  return text.length > max ? `${text.slice(0, max)}\n… (${text.length - max} more characters)` : text;
}
