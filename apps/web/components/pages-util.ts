import type { InfiniteData } from "@tanstack/react-query";

/** Flatten an infinite query's pages into one list, given the resource key of the page object. */
export function flatten<K extends string, T>(data: InfiniteData<{ [P in K]: T[] }> | undefined, key: K): T[] {
  return data ? data.pages.flatMap((p) => p[key]) : [];
}
