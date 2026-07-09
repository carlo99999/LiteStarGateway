import { Navigate } from "@tanstack/react-router";
import type { ReactNode } from "react";
import { useAuth } from "@/features/auth/use-auth";

/** Protected-route wrapper: gates children behind an authenticated session. */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { status } = useAuth();

  if (status === "loading") {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background">
        <p className="animate-pulse font-mono text-sm text-muted-foreground">
          <span className="text-primary">$</span> restoring session…
        </p>
      </div>
    );
  }

  if (status === "unauthenticated") {
    return <Navigate to="/login" />;
  }

  return <>{children}</>;
}
