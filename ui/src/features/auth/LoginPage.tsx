import { useNavigate } from "@tanstack/react-router";
import { Terminal } from "lucide-react";
import { useState, type FormEvent } from "react";
import { CodeRain } from "@/features/auth/CodeRain";
import { useAuth } from "@/features/auth/use-auth";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export function LoginPage() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await login(email, password);
      await navigate({ to: "/" });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="relative flex min-h-screen items-center justify-center bg-background bg-grid px-4">
      <CodeRain />
      <div className="relative z-10 w-full max-w-sm">
        <div className="mb-6 flex items-center gap-2">
          <span className="flex h-9 w-9 items-center justify-center rounded-md bg-primary/10 text-primary">
            <Terminal className="h-5 w-5" />
          </span>
          <div className="font-mono text-sm leading-tight">
            <div className="text-gradient font-semibold">litestar-gateway</div>
            <div className="text-xs text-muted-foreground">// admin console</div>
          </div>
        </div>

        <Card>
          <CardContent className="pt-5">
            <p className="mb-5 font-mono text-xs text-muted-foreground">
              <span className="text-primary">$</span> gateway login --email --password
            </p>
            <form className="space-y-4" onSubmit={onSubmit}>
              <div className="space-y-1.5">
                <Label htmlFor="email">email</Label>
                <Input
                  id="email"
                  type="email"
                  autoComplete="username"
                  required
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="admin@example.com"
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="password">password</Label>
                <Input
                  id="password"
                  type="password"
                  autoComplete="current-password"
                  required
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="••••••••••••"
                />
              </div>
              {error ? (
                <p role="alert" className="font-mono text-xs text-destructive">
                  ! {error}
                </p>
              ) : null}
              <Button type="submit" className="w-full" disabled={submitting}>
                {submitting ? "> authenticating…" : "$ sign in"}
              </Button>
            </form>
          </CardContent>
        </Card>

        <p className="mt-4 text-center font-mono text-[11px] text-muted-foreground">
          bearer-token session · phase 0
        </p>
      </div>
    </div>
  );
}
