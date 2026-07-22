import {
  LayoutDashboard,
  Boxes,
  Route,
  FlaskConical,
  ShieldCheck,
  KeyRound,
  Building2,
  Users,
  UserRound,
  Bot,
  BarChart3,
  Wallet,
  ScrollText,
  type LucideIcon,
} from "lucide-react";
import type { ConsoleSurface } from "@/features/teams/access";

export interface NavItem {
  /** Router path (relative to the `/ui` basepath). */
  to: string;
  /** Mono label, shown prefixed with `/`. */
  label: string;
  icon: LucideIcon;
  surface: ConsoleSurface;
  /** Implemented? Others render a "planned" placeholder and show a "soon" tag. */
  ready?: boolean;
}

export interface NavGroup {
  /** Section header, rendered as a `// label` comment. */
  label: string;
  items: NavItem[];
}

// Grouped by backend domain so the console scales past a flat list:
// Gateway = the LLM traffic plane, Governance = identity/tenancy, Observability
// = spend and audit. `organizations` is the Phase 0 read-only view; the rest are
// scaffolded placeholders until Phase 1.
export const NAV_GROUPS: NavGroup[] = [
  {
    label: "overview",
    items: [{ to: "/", label: "dashboard", icon: LayoutDashboard, surface: "dashboard" }],
  },
  {
    label: "gateway",
    items: [
      { to: "/models", label: "models", icon: Boxes, surface: "models", ready: true },
      { to: "/routing", label: "routing", icon: Route, surface: "routing", ready: true },
      {
        to: "/playground",
        label: "playground",
        icon: FlaskConical,
        surface: "playground",
        ready: true,
      },
      {
        to: "/credentials",
        label: "credentials",
        icon: ShieldCheck,
        surface: "credentials",
        ready: true,
      },
      { to: "/api-keys", label: "api-keys", icon: KeyRound, surface: "api-keys", ready: true },
    ],
  },
  {
    label: "governance",
    items: [
      {
        to: "/organizations",
        label: "organizations",
        icon: Building2,
        surface: "organizations",
        ready: true,
      },
      { to: "/teams", label: "teams", icon: Users, surface: "teams", ready: true },
      { to: "/users", label: "users", icon: UserRound, surface: "users", ready: true },
      {
        to: "/service-principals",
        label: "service-principals",
        icon: Bot,
        surface: "service-principals",
        ready: true,
      },
    ],
  },
  {
    label: "observability",
    items: [
      { to: "/usage", label: "usage", icon: BarChart3, surface: "usage", ready: true },
      { to: "/budgets", label: "budgets", icon: Wallet, surface: "budgets", ready: true },
      { to: "/audit", label: "audit", icon: ScrollText, surface: "audit", ready: true },
    ],
  },
];
