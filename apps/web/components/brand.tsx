/**
 * The AgentEval mark: one trace across a baseline, the way a flight recorder draws a
 * signal. Drawn with currentColor for the frame so it follows the theme; the trace is
 * the accent.
 */
export function Mark({ size = 22, className = "" }: { size?: number; className?: string }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      aria-hidden="true"
      className={className}
      fill="none"
    >
      <rect x="0.75" y="0.75" width="22.5" height="22.5" rx="3" stroke="currentColor" strokeWidth="1.5" />
      <path d="M4 17.5h16" stroke="currentColor" strokeOpacity="0.35" strokeWidth="1" />
      <path
        d="M4 13h3.5l2-5.5 3 10 2-4.5H20"
        stroke="var(--accent)"
        strokeWidth="1.8"
        strokeLinecap="square"
      />
    </svg>
  );
}

export function Wordmark({ className = "" }: { className?: string }) {
  return (
    <span className={`inline-flex items-center gap-2 text-ink ${className}`}>
      <Mark />
      <span className="text-[16px] font-semibold leading-none tracking-[-0.015em]">
        Agent<span className="text-muted">Eval</span>
      </span>
    </span>
  );
}
