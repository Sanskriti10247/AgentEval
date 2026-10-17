// The dashboard's API key (ADR-012 D). It lives in sessionStorage, so it is gone when
// the tab closes and is never shared with other tabs, written to a cookie or sent
// anywhere but the API's Authorization header. If sessionStorage is unavailable (a
// locked-down browser), the key is kept in memory for the page's lifetime instead.

const STORAGE_KEY = "agenteval.apiKey";

export interface AuthState {
  /** The stored key, or null when none is set. */
  key: string | null;
  /** True after the API answered 401 for the stored key (it was cleared). */
  rejected: boolean;
}

let memoryKey: string | null = null;
let state: AuthState | null = null;
const listeners = new Set<() => void>();

function readStored(): string | null {
  try {
    return sessionStorage.getItem(STORAGE_KEY) ?? memoryKey;
  } catch {
    return memoryKey;
  }
}

function writeStored(key: string | null): void {
  memoryKey = key;
  try {
    if (key === null) sessionStorage.removeItem(STORAGE_KEY);
    else sessionStorage.setItem(STORAGE_KEY, key);
  } catch {
    // memoryKey already holds it
  }
}

function publish(next: AuthState): void {
  state = next;
  listeners.forEach((l) => l());
}

export function getApiKey(): string | null {
  return readStored();
}

/** A stable snapshot object (same reference until the state changes), for useSyncExternalStore. */
export function getAuthState(): AuthState {
  const key = readStored();
  if (state === null || state.key !== key) state = { key, rejected: state?.rejected ?? false };
  return state;
}

export function subscribeAuth(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function setApiKey(key: string): void {
  writeStored(key.trim());
  publish({ key: readStored(), rejected: false });
}

/** Sign out: forget the key. */
export function clearApiKey(): void {
  writeStored(null);
  publish({ key: null, rejected: false });
}

/** The API answered 401: the key is invalid, revoked or expired. Forget it and say why. */
export function rejectApiKey(): void {
  const had = readStored() !== null;
  writeStored(null);
  publish({ key: null, rejected: had });
}
