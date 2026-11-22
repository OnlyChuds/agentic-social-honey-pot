import type { Metadata } from "next";
import { Inter, JetBrains_Mono } from "next/font/google";
import type { ReactNode } from "react";
import { TooltipProvider } from "@/components/ui/tooltip";
import "./globals.css";

const inter = Inter({
  subsets: ["latin"],
  variable: "--font-inter",
  display: "swap",
});

const jetbrainsMono = JetBrains_Mono({
  subsets: ["latin"],
  variable: "--font-jetbrains-mono",
  display: "swap",
});

export const metadata: Metadata = {
  title: "Honeypot Monitor — Target Intelligence & Campaign Dashboard",
  description:
    "Live campaign stage tracker, suspicion telemetry, and personality evolution inspector over SQLite WAL.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className={`dark ${inter.variable} ${jetbrainsMono.variable}`}>
      <body className="min-h-screen bg-background font-sans text-zinc-50 antialiased selection:bg-zinc-700/60 selection:text-zinc-50">
        <TooltipProvider>{children}</TooltipProvider>
      </body>
    </html>
  );
}
