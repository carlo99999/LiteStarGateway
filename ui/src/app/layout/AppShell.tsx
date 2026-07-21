import { Suspense } from "react";
import { Outlet } from "@tanstack/react-router";
import { Sidebar } from "@/app/layout/Sidebar";
import { Topbar } from "@/app/layout/Topbar";

/** Authenticated app frame: topbar + sidebar + routed content. */
export function AppShell() {
  return (
    <div className="flex min-h-screen flex-col bg-background">
      <Topbar />
      <div className="flex flex-1">
        <Sidebar />
        <main className="min-w-0 flex-1 bg-grid px-4 py-6 sm:px-6 lg:px-8">
          <div className="mx-auto max-w-6xl">
            {/* Pages are code-split (see router.tsx); this catches the brief
                chunk load on first navigation to each route. */}
            <Suspense
              fallback={
                <div className="flex items-center justify-center py-16 text-sm text-muted-foreground">
                  <span className="animate-pulse">loading…</span>
                </div>
              }
            >
              <Outlet />
            </Suspense>
          </div>
        </main>
      </div>
    </div>
  );
}
