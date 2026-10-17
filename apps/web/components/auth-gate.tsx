"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useState, useSyncExternalStore, type FormEvent, type ReactNode } from "react";
import { getAuthState, setApiKey, subscribeAuth, type AuthState } from "@/lib/auth";
import { Button } from "./ui";

const UNKNOWN: AuthState | null = null; // server render and first client render: don't flash the form

export function useAuth(): AuthState | null {
  return useSyncExternalStore(subscribeAuth, getAuthState, () => UNKNOWN);
}

/** Shows the page only once an API key is stored; asks for one first, or again after a 401. */
export function AuthGate({ children }: { children: ReactNode }) {
  const auth = useAuth();
  if (auth === null) return null;
  if (auth.key === null) return <KeyForm rejected={auth.rejected} />;
  return <>{children}</>;
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
    <form onSubmit={submit} aria-label="API key" className="mx-auto mt-16 max-w-md space-y-3">
      <h1 className="text-lg font-semibold">API key</h1>
      {rejected ? (
        <div role="alert" className="rounded border border-bad/40 bg-bad/10 px-3 py-2 text-sm text-bad">
          The API rejected the key (it may be invalid, revoked or expired). Enter a valid one.
        </div>
      ) : (
        <p className="text-sm text-muted">The dashboard needs an AgentEval API key to read from the API.</p>
      )}
      <label className="flex flex-col gap-1 text-xs text-muted">
        API key
        <input
          type="password"
          autoComplete="off"
          spellCheck={false}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="ae_…"
          className="rounded border border-line bg-panel px-2 py-1.5 font-mono text-sm text-fg"
        />
      </label>
      <Button type="submit" disabled={!value.trim()}>
        Use key
      </Button>
      <p className="text-xs text-muted">
        The key stays in this tab&apos;s session storage and is sent only to the API. Closing the tab, or
        Sign out, forgets it. Create one with{" "}
        <code className="font-mono">
          docker compose exec api python -m agenteval_api.keys create --name dashboard --scopes read,write
        </code>
        . A read-only key can browse; cancel and replay need the write scope.
      </p>
    </form>
  );
}
