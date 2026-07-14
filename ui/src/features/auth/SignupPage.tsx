import { Link, useNavigate } from "@tanstack/react-router";
import { Terminal } from "lucide-react";
import { useState, type FormEvent } from "react";
import { signup } from "@/features/auth/api";
import { CodeRain } from "@/features/auth/CodeRain";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

/** The invite token, carried in the `?token=` query param of the signup link
 * the admin hands out. Read from the URL directly so the page works whether or
 * not the router validated the search. */
function tokenFromUrl(): string {
  if (typeof window === "undefined") return "";
  return new URLSearchParams(window.location.search).get("token") ?? "";
}

/** Public page where an invited user redeems their invite token to set their
 * own password. Reached via the link from the "Invite user" dialog. */
export function SignupPage() {
  const navigate = useNavigate();
  const [token] = useState(tokenFromUrl);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [done, setDone] = useState(false);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await signup(token, email, password);
      setDone(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Signup failed");
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
            <div className="text-xs text-muted-foreground">// accept invite</div>
          </div>
        </div>

        <Card>
          <CardContent className="pt-5">
            {done ? (
              <div className="space-y-4">
                <p className="font-mono text-xs text-primary">
                  ✓ account created — you can sign in now.
                </p>
                <Button className="w-full" onClick={() => navigate({ to: "/login" })}>
                  $ go to sign in
                </Button>
              </div>
            ) : (
              <>
                <p className="mb-5 font-mono text-xs text-muted-foreground">
                  <span className="text-primary">$</span> gateway signup --invite --email --password
                </p>
                {token ? null : (
                  <p role="alert" className="mb-4 font-mono text-xs text-destructive">
                    ! no invite token in the link — ask your admin for a fresh invite.
                  </p>
                )}
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
                      placeholder="you@example.com"
                    />
                  </div>
                  <div className="space-y-1.5">
                    <Label htmlFor="password">password</Label>
                    <Input
                      id="password"
                      type="password"
                      autoComplete="new-password"
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
                  <Button type="submit" className="w-full" disabled={submitting || !token}>
                    {submitting ? "> creating account…" : "$ create account"}
                  </Button>
                </form>
              </>
            )}
          </CardContent>
        </Card>

        <p className="mt-4 text-center font-mono text-[11px] text-muted-foreground">
          already have an account?{" "}
          <Link to="/login" className="text-primary hover:underline">
            sign in
          </Link>
        </p>
      </div>
    </div>
  );
}
