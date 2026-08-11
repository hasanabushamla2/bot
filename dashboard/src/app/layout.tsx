import type { Metadata } from "next";
import "./globals.css";
import { Nav } from "@/components/Nav";
import { StatusBar } from "@/components/StatusBar";

export const metadata: Metadata = {
  title: "Quant Engine — Monitoring",
  description: "Paper trading engine monitoring dashboard",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="dark">
      <body className="min-h-screen bg-background">
        <div className="flex h-screen overflow-hidden">
          <Nav />
          <main className="flex-1 flex flex-col overflow-hidden">
            <StatusBar />
            <div className="flex-1 overflow-y-auto p-6">{children}</div>
          </main>
        </div>
      </body>
    </html>
  );
}
