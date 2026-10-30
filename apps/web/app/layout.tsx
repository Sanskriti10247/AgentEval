import type { Metadata, Viewport } from "next";
import { IBM_Plex_Mono, IBM_Plex_Sans, IBM_Plex_Sans_Condensed } from "next/font/google";
import type { ReactNode } from "react";
import { AuthGate } from "@/components/auth-gate";
import { Nav } from "@/components/nav";
import { THEME_BOOT_SCRIPT } from "@/lib/theme-boot";
import { Providers } from "./providers";
import "./globals.css";

// Self-hosted by next/font at build time: no request to Google from the reader's browser.
const sans = IBM_Plex_Sans({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-plex-sans",
  display: "swap",
});
const mono = IBM_Plex_Mono({
  subsets: ["latin"],
  weight: ["400", "500"],
  variable: "--font-plex-mono",
  display: "swap",
});
const condensed = IBM_Plex_Sans_Condensed({
  subsets: ["latin"],
  weight: ["500", "600"],
  variable: "--font-plex-condensed",
  display: "swap",
});

export const metadata: Metadata = {
  title: { default: "AgentEval", template: "%s · AgentEval" },
  description: "Run, observe, evaluate and benchmark AI agents.",
};

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f3f4f5" },
    { media: "(prefers-color-scheme: dark)", color: "#161718" },
  ],
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html
      lang="en"
      className={`${sans.variable} ${mono.variable} ${condensed.variable}`}
      suppressHydrationWarning
    >
      <head>
        {/* Applies a theme the reader chose before first paint, so there is no flash. */}
        <script dangerouslySetInnerHTML={{ __html: THEME_BOOT_SCRIPT }} />
      </head>
      <body>
        <Providers>
          <div className="min-h-screen md:flex">
            <Nav />
            <main className="min-w-0 flex-1 px-4 pb-16 pt-5 sm:px-6 md:px-8 md:pt-7 lg:px-10">
              <div className="mx-auto max-w-[1180px]">
                <AuthGate>{children}</AuthGate>
              </div>
            </main>
          </div>
        </Providers>
      </body>
    </html>
  );
}
