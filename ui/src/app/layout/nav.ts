import {
  Building2,
  Users,
  Boxes,
  KeyRound,
  ShieldCheck,
  Wallet,
  BarChart3,
  Route,
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

// Mirrors the backend domains. `organizations` is the Phase 0 read-only view.
export const NAV_ITEMS: NavItem[] = [
  { to: "/organizations", label: "organizations", icon: Building2, ready: true },
  { to: "/teams", label: "teams", icon: Users },
  { to: "/models", label: "models", icon: Boxes },
  { to: "/credentials", label: "credentials", icon: ShieldCheck },
  { to: "/api-keys", label: "api-keys", icon: KeyRound },
  { to: "/budgets", label: "budgets", icon: Wallet },
  { to: "/usage", label: "usage", icon: BarChart3 },
  { to: "/routing", label: "routing", icon: Route },
];
