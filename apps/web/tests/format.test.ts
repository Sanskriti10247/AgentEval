import { describe, expect, it } from "vitest";
import {
  ageSeconds,
  compactJson,
  formatAge,
  formatCost,
  formatDuration,
  formatInt,
  formatPercent,
  formatScore,
  formatTimestamp,
  humanize,
  shortId,
} from "@/lib/format";

describe("formatDuration", () => {
  it("formats ms, seconds, minutes and hours", () => {
    expect(formatDuration(842)).toBe("842 ms");
    expect(formatDuration(7200)).toBe("7.2 s");
    expect(formatDuration(125_000)).toBe("2m 05s");
    expect(formatDuration(3_900_000)).toBe("1h 05m");
  });
  it("shows a dash for missing values", () => {
    expect(formatDuration(null)).toBe("–");
    expect(formatDuration(undefined)).toBe("–");
  });
});

describe("formatCost", () => {
  it("keeps four decimals under a dollar and two above", () => {
    expect(formatCost("0.070000")).toBe("$0.0700");
    expect(formatCost("0.000600")).toBe("$0.0006");
    expect(formatCost(12.5)).toBe("$12.50");
  });
  it("handles zero and missing", () => {
    expect(formatCost("0")).toBe("$0.00");
    expect(formatCost(null)).toBe("–");
    expect(formatCost("abc")).toBe("–");
  });
});

describe("formatPercent", () => {
  it("drops the decimal for whole percentages", () => {
    expect(formatPercent(0.82)).toBe("82%");
    expect(formatPercent(1)).toBe("100%");
    expect(formatPercent(0)).toBe("0%");
  });
  it("keeps one decimal otherwise", () => {
    expect(formatPercent(0.3333)).toBe("33.3%");
  });
  it("shows a dash for null", () => {
    expect(formatPercent(null)).toBe("–");
  });
});

describe("misc helpers", () => {
  it("formatInt groups thousands", () => expect(formatInt(1234567)).toBe("1,234,567"));
  it("formatScore uses three decimals", () => expect(formatScore("0.98497")).toBe("0.985"));
  it("shortId takes 8 characters", () => expect(shortId("abcdef0123456789")).toBe("abcdef01"));
  it("formatTimestamp is UTC", () => expect(formatTimestamp("2026-10-17T12:00:00.123Z")).toBe("2026-10-17 12:00:00"));
  it("formatAge is coarse", () => {
    expect(formatAge(12)).toBe("12s");
    expect(formatAge(190)).toBe("3m");
    expect(formatAge(7300)).toBe("2h");
    expect(formatAge(200_000)).toBe("2d");
  });
  it("ageSeconds measures against now", () => {
    expect(ageSeconds("2026-10-17T12:00:00Z", Date.parse("2026-10-17T12:00:30Z"))).toBe(30);
    expect(ageSeconds(null)).toBeNull();
  });
  it("humanize replaces underscores", () => expect(humanize("retries_exhausted")).toBe("retries exhausted"));
  it("compactJson truncates long output", () => {
    expect(compactJson({ a: 1 })).toContain('"a": 1');
    expect(compactJson({ a: "x".repeat(100) }, 20)).toContain("more characters");
  });
});
