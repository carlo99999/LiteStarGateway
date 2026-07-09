import { Link, useNavigate } from "@tanstack/react-router";
import { LogOut, Terminal, UserRound } from "lucide-react";
import { ThemeToggle } from "@/app/theme-toggle";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useAuth } from "@/features/auth/use-auth";

export function Topbar() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();

  async function onLogout() {
    await logout();
    await navigate({ to: "/login" });
  }

  return (
    <header className="flex h-14 shrink-0 items-center justify-between border-b border-border bg-card/60 px-4">
      <Link to="/" className="flex items-center gap-2">
        <span className="flex h-8 w-8 items-center justify-center rounded-md bg-primary/10 text-primary">
          <Terminal className="h-4 w-4" />
        </span>
        <span className="font-mono text-sm font-semibold">
          <span className="text-gradient">gateway</span>
          <span className="text-muted-foreground"> :: admin</span>
        </span>
      </Link>

      <div className="flex items-center gap-1">
        <ThemeToggle />
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" size="sm" className="gap-2">
              <UserRound className="h-4 w-4" />
              <span className="hidden max-w-[16ch] truncate sm:inline">
                {user?.email ?? "…"}
              </span>
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuLabel>{user?.email}</DropdownMenuLabel>
            <DropdownMenuSeparator />
            <DropdownMenuItem onSelect={onLogout} className="text-destructive">
              <LogOut className="h-4 w-4" />$ logout
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </header>
  );
}
