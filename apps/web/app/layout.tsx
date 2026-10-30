import type { Metadata, Viewport } from "next";
import { Hanken_Grotesk, JetBrains_Mono } from "next/font/google";
import type { ReactNode } from "react";
import { AuthGate } from "@/components/auth-gate";
import { Nav } from "@/components/nav";
import { THEME_BOOT_SCRIPT } from "@/lib/theme-boot";
import { Providers } from "./providers";
import "./globals.css";

// Self-hosted by next/font at build time: no request to Google from the reader's browser.
// Hanken Grotesk for reading, JetBrains Mono for ids, numbers in tables and trajectories.
const sans = Hanken_Grotesk({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-ui",
  display: "swap",
});
const mono = JetBrains_Mono({
  subsets: ["latin"],
  weight: ["400", "500"],
  variable: "--font-data",
  display: "swap",
});

export const metadata: Metadata = {
  title: { default: "AgentEval", template: "%s · AgentEval" },
  description: "Run, observe, evaluate and benchmark AI agents.",
};

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f6f5f1" },
    { media: "(prefers-color-scheme: dark)", color: "#151413" },
  ],
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html
      lang="en"
      className={`${sans.variable} ${mono.variable}`}
      suppressHydrationWarning
    >
      <head>
        {/* Applies a theme the reader chose before first paint, so there is no flash. */}
        <script dangerouslySetInnerHTML={{ __html: THEME_BOOT_SCRIPT }} />
      </head>
      <body>
        <Providers>
          <a href="#main" className="sr-only focus:not-sr-only focus:fixed focus:left-3 focus:top-3 focus:z-50 focus:bg-surface focus:px-3 focus:py-2">
            Skip to content
          </a>
          <div className="min-h-screen md:flex">
            <Nav />
            <main id="main" className="min-w-0 flex-1 px-4 pb-16 pt-5 sm:px-6 md:px-8 md:pt-6 lg:px-10">
              <div className="mx-auto max-w-[1240px]">
                <AuthGate>{children}</AuthGate>
              </div>
            </main>
          </div>
        </Providers>
      </body>
    </html>
  );
}
