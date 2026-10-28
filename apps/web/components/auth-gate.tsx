"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useState, useSyncExternalStore, type FormEvent, type ReactNode } from "react";
import { getAuthState, setApiKey, subscribeAuth, type AuthState } from "@/lib/auth";
import { apiBaseUrl } from "@/lib/api/client";
import { isUnreachable, useApiHealth } from "@/lib/hooks";
import { Button, Loading, Notice } from "./ui";

const UNKNOWN: AuthState | null = null; // server render and first client render: don't flash the form

export function useAuth(): AuthState | null {
  return useSyncExternalStore(subscribeAuth, getAuthState, () => UNKNOWN);
}

/**
 * Shows the page only once an API key is stored; asks for one first, or again after a 401.
 * A read-only API or a public demo (GET /health says so) needs no key, so its pages show
 * straight away.
 */
export function AuthGate({ children }: { children: ReactNode }) {
  const auth = useAuth();
  const health = useApiHealth();
  if (auth === null) return null;
  if (auth.key !== null || health.data?.read_only || health.data?.public_demo) return <>{children}</>;
  if (health.isPending) {
    return (
      <div className="mt-10 max-w-[34rem] md:mt-20">
        <Loading label={health.failureCount > 0 ? "Waking the API" : "Connecting to the API"} />
        {health.failureCount > 0 ? (
          <p className="text-[13px] leading-relaxed text-muted">
            No answer from <span className="font-mono text-ink-2">{apiBaseUrl()}</span> yet. A free instance that has
            been idle takes about a minute to start; this page carries on by itself once it does.
          </p>
        ) : null}
      </div>
    );
  }
  // A key cannot help while the API is unreachable, so say what is wrong instead of asking for one.
  if (isUnreachable(health.error) && auth.key === null) return <Unreachable onRetry={() => void health.refetch()} />;
  return <KeyForm rejected={auth.rejected} />;
}

function Unreachable({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="mt-10 max-w-[34rem] space-y-4 md:mt-20">
      <h1 className="font-display text-[26px] font-semibold leading-tight">The API is not answering</h1>
      <Notice title={`No response from ${apiBaseUrl()}/health`}>
        Either it is down, the address is wrong, or it does not allow requests from this site. The address comes
        from NEXT_PUBLIC_API_URL at build time, and the API must list this site in AGENTEVAL_CORS_ORIGINS.
      </Notice>
      <Button onClick={onRetry}>Try again</Button>
    </div>
  );
}

export function KeyForm({ rejected }: { rejected: boolean }) {
  const qc = useQueryClient();
  const [value, setValue] = useState("");

  function submit(e: FormEvent) {
    e.preventDefault();
    const key = value.trim();
    if (!key) return;
    qc.clear(); // nothing cached under another key (or an error) survives
    setApiKey(key);
    setValue("");
  }

  return (
    <form onSubmit={submit} aria-label="API key" className="mt-10 max-w-[30rem] space-y-5 md:mt-20">
      <div>
        <div className="mb-1.5 font-mono text-[11px] uppercase tracking-[0.12em] text-faint">Sign in</div>
        <h1 className="font-display text-[26px] font-semibold leading-tight">API key</h1>
      </div>
      {rejected ? (
        <Notice title="The API rejected the key">
          It may be invalid, revoked or expired. Enter a valid one.
        </Notice>
      ) : (
        <p className="text-[13.5px] leading-relaxed text-muted">
          This dashboard reads everything through the AgentEval API, and this API wants a key.
        </p>
      )}
      <label className="flex flex-col gap-1.5 text-[12.5px] text-ink-2">
        API key
        <input
          type="password"
          autoComplete="off"
          spellCheck={false}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="ae_…"
          className="h-9 rounded-[3px] border border-rule-strong bg-surface px-2.5 font-mono text-[13px] text-ink"
        />
      </label>
      <Button type="submit" variant="primary" disabled={!value.trim()}>
        Use key
      </Button>
      <div className="space-y-2 border-t border-rule pt-4 text-[12.5px] leading-relaxed text-muted">
        <p>
          The key stays in this tab&apos;s session storage and is only ever sent to the API. Closing the tab, or Sign
          out, forgets it.
        </p>
        <p>Create one on the API host:</p>
        <pre className="overflow-x-auto rounded-[3px] bg-sunken px-3 py-2 font-mono text-[11.5px] text-ink-2">
          docker compose exec api python -m agenteval_api.keys create --name dashboard --scopes read,write
        </pre>
        <p>A read-scoped key can browse. Launching, cancelling and replaying need the write scope.</p>
      </div>
    </form>
  );
}
