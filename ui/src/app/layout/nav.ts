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

export interface NavItem {
  /** Router path (relative to the `/ui` basepath). */
  to: string;
  /** Mono label, shown prefixed with `/`. */
  label: string;
  icon: LucideIcon;
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
    items: [{ to: "/", label: "dashboard", icon: LayoutDashboard }],
  },
  {
    label: "gateway",
    items: [
      { to: "/models", label: "models", icon: Boxes, ready: true },
      { to: "/routing", label: "routing", icon: Route, ready: true },
      { to: "/playground", label: "playground", icon: FlaskConical, ready: true },
      { to: "/credentials", label: "credentials", icon: ShieldCheck, ready: true },
      { to: "/api-keys", label: "api-keys", icon: KeyRound, ready: true },
    ],
  },
  {
    label: "governance",
    items: [
      { to: "/organizations", label: "organizations", icon: Building2, ready: true },
      { to: "/teams", label: "teams", icon: Users, ready: true },
      { to: "/users", label: "users", icon: UserRound, ready: true },
      { to: "/service-principals", label: "service-principals", icon: Bot, ready: true },
    ],
  },
  {
    label: "observability",
    items: [
      { to: "/usage", label: "usage", icon: BarChart3, ready: true },
      { to: "/budgets", label: "budgets", icon: Wallet, ready: true },
      { to: "/audit", label: "audit", icon: ScrollText, ready: true },
    ],
  },
];
