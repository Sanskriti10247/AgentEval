"use client";

import { useSyncExternalStore } from "react";
import { THEME_STORAGE_KEY } from "./theme-boot";

// The reader's theme choice. "system" follows prefers-color-scheme; the other two set
// data-theme on <html>, which globals.css reads. Stored in localStorage because it is a
// display preference, not a secret.

export type ThemeChoice = "system" | "light" | "dark";

const STORAGE_KEY = THEME_STORAGE_KEY;

const listeners = new Set<() => void>();

function read(): ThemeChoice {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    return v === "light" || v === "dark" ? v : "system";
  } catch {
    return "system";
  }
}

export function setTheme(choice: ThemeChoice): void {
  try {
    if (choice === "system") localStorage.removeItem(STORAGE_KEY);
    else localStorage.setItem(STORAGE_KEY, choice);
  } catch {
    // preference simply does not persist
  }
  if (choice === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = choice;
  listeners.forEach((l) => l());
}

function subscribe(l: () => void) {
  listeners.add(l);
  return () => {
    listeners.delete(l);
  };
}

export function useTheme(): ThemeChoice {
  return useSyncExternalStore(subscribe, read, () => "system");
}
