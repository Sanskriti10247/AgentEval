import { describe, expect, it } from "vitest";
import config, { securityHeaders } from "../next.config";

describe("dashboard response headers (security review F-P5)", () => {
  it("applies the security headers to every path", async () => {
    const rules = await config.headers!();
    expect(rules).toHaveLength(1);
    expect(rules[0]?.source).toBe("/:path*");
    expect(rules[0]?.headers).toEqual(securityHeaders);
  });

  it("forbids framing and sniffing, and does not advertise the framework", () => {
    const byKey = Object.fromEntries(securityHeaders.map((h) => [h.key, h.value]));
    expect(byKey["X-Frame-Options"]).toBe("DENY");
    expect(byKey["X-Content-Type-Options"]).toBe("nosniff");
    expect(byKey["Content-Security-Policy"]).toContain("frame-ancestors 'none'");
    expect(config.poweredByHeader).toBe(false);
  });
});
