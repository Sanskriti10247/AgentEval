"use client";

import { useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";
import { apiBaseUrl } from "@/lib/api/client";
import { clearApiKey } from "@/lib/auth";
import { useCapabilities } from "@/lib/hooks";
import { setTheme, useTheme, type ThemeChoice } from "@/lib/theme";
import { useAuth } from "./auth-gate";
import { Wordmark } from "./brand";

const ITEMS = [
  { href: "/", label: "Dashboard" },
  { href: "/agents", label: "Agents" },
  { href: "/tasks", label: "Tasks" },
  { href: "/evaluations", label: "Evaluations" },
  { href: "/runs", label: "Runs" },
  { href: "/workers", label: "Workers" },
];

function isActive(pathname: string, href: string) {
  return href === "/" ? pathname === "/" : pathname.startsWith(href);
}

export function Nav() {
  const pathname = usePathname();
  const [open, setOpen] = useState(false);
  const current = ITEMS.find((i) => isActive(pathname, i.href));

  return (
    <nav aria-label="Primary" className="shrink-0 border-b border-rule bg-paper md:w-[212px] md:border-b-0 md:border-r">
      <div className="md:sticky md:top-0 md:flex md:h-screen md:flex-col">
      {/* Top bar on small screens; the head of the rail on wide ones. */}
      <div className="flex h-12 items-center justify-between px-4 md:h-auto md:px-5 md:pb-6 md:pt-6">
        <Link href="/" className="no-underline hover:no-underline" aria-label="AgentEval home">
          <Wordmark />
        </Link>
        <button
          type="button"
          className="inline-flex h-8 items-center gap-2 rounded-[3px] border border-rule-strong px-2.5 font-mono text-[11px] uppercase tracking-[0.1em] text-ink-2 md:hidden"
          aria-expanded={open}
          aria-controls="primary-nav"
          onClick={() => setOpen((o) => !o)}
        >
          {current?.label ?? "Menu"}
          <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true" className={open ? "rotate-180" : ""}>
            <path d="M1.5 3.5 5 7l3.5-3.5" fill="none" stroke="currentColor" strokeWidth="1.3" />
          </svg>
        </button>
      </div>

      <div id="primary-nav" className={`${open ? "block" : "hidden"} border-t border-rule md:flex md:flex-1 md:flex-col md:border-t-0`}>
        <ul className="py-2 md:py-0">
          {ITEMS.map((item, i) => {
            const active = isActive(pathname, item.href);
            return (
              <li key={item.href}>
                <Link
                  href={item.href}
                  onClick={() => setOpen(false)}
                  aria-current={active ? "page" : undefined}
                  className={`group relative flex items-baseline gap-3 px-4 py-[7px] text-[13.5px] no-underline hover:no-underline md:px-5 ${
                    active ? "font-medium text-ink" : "text-muted hover:text-ink"
                  }`}
                >
                  <span
                    aria-hidden="true"
                    className={`absolute inset-y-[6px] left-0 w-[2px] ${active ? "bg-accent" : "bg-transparent"}`}
                  />
                  <span aria-hidden="true" className={`font-mono text-[10.5px] tabular-nums ${active ? "text-accent" : "text-faint"}`}>
                    {String(i + 1).padStart(2, "0")}
                  </span>
                  {item.label}
                </Link>
              </li>
            );
          })}
        </ul>
        <NavFooter />
      </div>
      </div>
    </nav>
  );
}

function NavFooter() {
  const auth = useAuth();
  const caps = useCapabilities();
  const qc = useQueryClient();
  const host = apiBaseUrl().replace(/^https?:\/\//, "");

  return (
    <div className="space-y-3 border-t border-rule px-4 py-4 text-[12px] md:mt-auto md:px-5">
      {caps.publicDemo ? (
        <p className="leading-snug text-muted">
          <span className="font-medium text-ink-2">Public demo.</span> Launch evaluations and runs on the agents and
          datasets already here
          {caps.demoLimits ? `, up to ${caps.demoLimits.max_repetitions} repetitions each` : ""}.
        </p>
      ) : caps.readOnly ? (
        <p className="leading-snug text-muted">
          <span className="font-medium text-ink-2">Read-only demo:</span> browsing only, no new runs.
        </p>
      ) : null}
      <ThemeSwitch />
      <div className="flex items-center justify-between gap-2">
        <span className="truncate font-mono text-[10.5px] text-faint" title={apiBaseUrl()}>
          {host}
        </span>
        {!caps.readOnly && auth?.key ? (
          <button
            type="button"
            onClick={() => {
              qc.clear();
              clearApiKey();
            }}
            className="shrink-0 text-[12px] text-muted underline decoration-rule-strong underline-offset-[3px] hover:text-ink"
          >
            Sign out
          </button>
        ) : null}
      </div>
    </div>
  );
}

const THEMES: { value: ThemeChoice; label: string }[] = [
  { value: "system", label: "Auto" },
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
];

function ThemeSwitch() {
  const theme = useTheme();
  return (
    <div role="radiogroup" aria-label="Theme" className="inline-flex rounded-[3px] border border-rule">
      {THEMES.map((t) => (
        <button
          key={t.value}
          type="button"
          role="radio"
          aria-checked={theme === t.value}
          onClick={() => setTheme(t.value)}
          className={`px-2 py-[3px] font-mono text-[10.5px] uppercase tracking-[0.08em] first:rounded-l-[2px] last:rounded-r-[2px] ${
            theme === t.value ? "bg-sunken text-ink" : "text-faint hover:text-ink-2"
          }`}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}
