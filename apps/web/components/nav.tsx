"use client";

import { useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState, type ReactNode } from "react";
import { apiBaseUrl } from "@/lib/api/client";
import { clearApiKey } from "@/lib/auth";
import { useCapabilities } from "@/lib/hooks";
import { setTheme, useTheme, type ThemeChoice } from "@/lib/theme";
import { useAuth } from "./auth-gate";
import { Wordmark } from "./brand";

type Item = { href: string; label: string; icon: ReactNode };

// 16px line icons on a 16 grid; stroke follows the text colour.
const icon = (d: string) => (
  <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round">
    <path d={d} />
  </svg>
);

const GROUPS: { label?: string; items: Item[] }[] = [
  {
    items: [
      { href: "/", label: "Dashboard", icon: icon("M2.5 13.5h11M4 11V8M7 11V4.5M10 11V7M13 11V2.5") },
      { href: "/evaluations", label: "Evaluations", icon: icon("M2.5 3.5h11v9h-11zM5 6.5l1.5 1.5L9 5.5M5 10h6") },
      { href: "/runs", label: "Runs", icon: icon("M2.5 4h11M2.5 8h11M2.5 12h7") },
    ],
  },
  {
    label: "Registry",
    items: [
      { href: "/agents", label: "Agents", icon: icon("M8 2.5v2M4.5 4.5h7v8h-7zM6.5 8h0M9.5 8h0M6.5 10.5h3M2.5 8v2.5M13.5 8v2.5") },
      { href: "/tasks", label: "Tasks", icon: icon("M3.5 2.5h7l2 2v9h-9zM5.5 7h5M5.5 9.5h5M5.5 12h3") },
      { href: "/workers", label: "Workers", icon: icon("M2.5 3.5h11v4h-11zM2.5 8.5h11v4h-11zM4.5 5.5h0M4.5 10.5h0") },
    ],
  },
];
const ITEMS = GROUPS.flatMap((g) => g.items);

function isActive(pathname: string, href: string) {
  return href === "/" ? pathname === "/" : pathname.startsWith(href);
}

export function Nav() {
  const pathname = usePathname();
  const [open, setOpen] = useState(false);
  const current = ITEMS.find((i) => isActive(pathname, i.href));

  return (
    <nav
      aria-label="Primary"
      className="sticky top-0 z-30 shrink-0 border-b border-rule bg-surface md:static md:w-[208px] md:border-b-0 md:border-r"
    >
      <div className="md:sticky md:top-0 md:flex md:h-screen md:flex-col">
        {/* Top bar on small screens; the head of the rail on wide ones. */}
        <div className="flex h-12 items-center justify-between px-4 md:h-14 md:px-4">
          <Link href="/" className="no-underline hover:no-underline" aria-label="AgentEval home">
            <Wordmark />
          </Link>
          <button
            type="button"
            className="inline-flex h-8 items-center gap-2 rounded-[4px] border border-rule-strong px-2.5 text-[13px] font-medium text-ink-2 md:hidden"
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
          <div className="px-2 py-2 md:pt-1">
            {GROUPS.map((g, gi) => (
              <div key={gi} className={gi ? "mt-4" : ""}>
                {g.label ? <div className="px-2.5 pb-1 text-[11.5px] font-medium text-faint">{g.label}</div> : null}
                <ul className="space-y-px">
                  {g.items.map((item) => {
                    const active = isActive(pathname, item.href);
                    return (
                      <li key={item.href}>
                        <Link
                          href={item.href}
                          onClick={() => setOpen(false)}
                          aria-current={active ? "page" : undefined}
                          className={`flex h-8 items-center gap-2.5 rounded-[4px] px-2.5 text-[13.5px] no-underline hover:no-underline ${
                            active ? "bg-sunken font-medium text-ink" : "text-muted hover:bg-sunken/60 hover:text-ink"
                          }`}
                        >
                          <span className={active ? "text-accent" : "text-faint"}>{item.icon}</span>
                          {item.label}
                        </Link>
                      </li>
                    );
                  })}
                </ul>
              </div>
            ))}
          </div>
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
    <div className="space-y-3 border-t border-rule px-4 py-4 text-[12px] md:mt-auto">
      {caps.publicDemo ? (
        <p className="leading-snug text-muted">
          <span className="font-medium text-ink-2">Public demo.</span> Launches use the agents and datasets already here
          {caps.demoLimits ? `, up to ${caps.demoLimits.max_repetitions} repetitions each` : ""}.
        </p>
      ) : caps.readOnly ? (
        <p className="leading-snug text-muted">
          <span className="font-medium text-ink-2">Read-only demo:</span> browsing only, no new runs.
        </p>
      ) : null}
      <ThemeSwitch />
      <div className="flex items-center justify-between gap-2">
        <span className="truncate font-mono text-[11px] text-faint" title={apiBaseUrl()}>
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
    <div role="radiogroup" aria-label="Theme" className="grid w-full grid-cols-3 rounded-[4px] border border-rule bg-paper p-0.5">
      {THEMES.map((t) => (
        <button
          key={t.value}
          type="button"
          role="radio"
          aria-checked={theme === t.value}
          onClick={() => setTheme(t.value)}
          className={`rounded-[3px] py-[3px] text-[12px] ${
            theme === t.value ? "bg-surface font-medium text-ink shadow-[0_0_0_1px_var(--rule)]" : "text-faint hover:text-ink-2"
          }`}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}
