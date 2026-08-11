"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  LayoutDashboard,
  BarChart3,
  Briefcase,
  ArrowLeftRight,
  Shield,
  Monitor,
  ScrollText,
} from "lucide-react";
import { cn } from "@/lib/utils";

const links = [
  { href: "/", label: "Overview", icon: LayoutDashboard },
  { href: "/metrics", label: "Metrics", icon: BarChart3 },
  { href: "/positions", label: "Positions", icon: Briefcase },
  { href: "/trades", label: "Trades", icon: ArrowLeftRight },
  { href: "/risk", label: "Risk", icon: Shield },
  { href: "/system", label: "System", icon: Monitor },
  { href: "/logs", label: "Logs", icon: ScrollText },
];

export function Nav() {
  const pathname = usePathname();

  return (
    <nav className="w-56 border-r border-border bg-card flex flex-col shrink-0">
      <div className="h-14 flex items-center px-5 border-b border-border">
        <span className="font-semibold text-sm tracking-wide text-foreground">
          QUANT ENGINE
        </span>
      </div>
      <div className="flex-1 py-3 space-y-1 px-2">
        {links.map(({ href, label, icon: Icon }) => {
          const active = pathname === href;
          return (
            <Link
              key={href}
              href={href}
              className={cn(
                "flex items-center gap-3 px-3 py-2 rounded-md text-sm transition-colors",
                active
                  ? "bg-accent text-foreground font-medium"
                  : "text-muted-foreground hover:text-foreground hover:bg-secondary"
              )}
            >
              <Icon className="w-4 h-4" />
              {label}
            </Link>
          );
        })}
      </div>
      <div className="px-4 py-3 border-t border-border text-[10px] text-muted-foreground uppercase tracking-widest">
        PAPER TRADING
      </div>
    </nav>
  );
}
