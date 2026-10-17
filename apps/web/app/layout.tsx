import type { Metadata } from "next";
import type { ReactNode } from "react";
import { Nav } from "@/components/nav";
import { Providers } from "./providers";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "AgentEval", template: "%s · AgentEval" },
  description: "Run, observe, evaluate and benchmark AI agents.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <Providers>
          <div className="flex min-h-screen">
            <Nav />
            <main className="min-w-0 flex-1 px-6 py-5">{children}</main>
          </div>
        </Providers>
      </body>
    </html>
  );
}
