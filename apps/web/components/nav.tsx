"use client";

import { useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { clearApiKey } from "@/lib/auth";
import { useReadOnly } from "@/lib/hooks";
import { useAuth } from "./auth-gate";

const ITEMS = [
  { href: "/", label: "Dashboard" },
  { href: "/agents", label: "Agents" },
  { href: "/tasks", label: "Tasks" },
  { href: "/evaluations", label: "Evaluations" },
  { href: "/runs", label: "Runs" },
  { href: "/workers", label: "Workers" },
];

export function Nav() {
  const pathname = usePathname();
  const auth = useAuth();
  const readOnly = useReadOnly();
  const qc = useQueryClient();
  return (
    <nav aria-label="Primary" className="w-48 shrink-0 border-r border-line bg-panel px-3 py-4">
      <div className="mb-5 px-2 font-mono text-sm font-semibold tracking-tight">AgentEval</div>
      <ul className="space-y-0.5">
        {ITEMS.map((item) => {
          const active = item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
          return (
            <li key={item.href}>
              <Link
                href={item.href}
                aria-current={active ? "page" : undefined}
                className={`block rounded px-2 py-1.5 text-sm hover:bg-panel2 hover:no-underline ${
                  active ? "bg-panel2 font-medium text-fg" : "text-muted"
                }`}
              >
                {item.label}
              </Link>
            </li>
          );
        })}
      </ul>
      {readOnly ? (
        <p className="mt-5 px-2 text-xs text-muted">Read-only demo: browsing only, no new runs.</p>
      ) : auth?.key ? (
        <button
          type="button"
          onClick={() => {
            qc.clear();
            clearApiKey();
          }}
          className="mt-5 w-full rounded px-2 py-1.5 text-left text-sm text-muted hover:bg-panel2"
        >
          Sign out
        </button>
      ) : null}
    </nav>
  );
}
