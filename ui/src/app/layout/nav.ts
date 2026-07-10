import {
  LayoutDashboard,
  Boxes,
  Route,
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
  /** Implemented in Phase 0? Others render a "planned" placeholder. */
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
      { to: "/models", label: "models", icon: Boxes },
      { to: "/routing", label: "routing", icon: Route },
      { to: "/credentials", label: "credentials", icon: ShieldCheck },
      { to: "/api-keys", label: "api-keys", icon: KeyRound },
    ],
  },
  {
    label: "governance",
    items: [
      { to: "/organizations", label: "organizations", icon: Building2, ready: true },
      { to: "/teams", label: "teams", icon: Users, ready: true },
      { to: "/users", label: "users", icon: UserRound },
      { to: "/service-principals", label: "service-principals", icon: Bot },
    ],
  },
  {
    label: "observability",
    items: [
      { to: "/usage", label: "usage", icon: BarChart3 },
      { to: "/budgets", label: "budgets", icon: Wallet },
      { to: "/audit", label: "audit", icon: ScrollText },
    ],
  },
];
