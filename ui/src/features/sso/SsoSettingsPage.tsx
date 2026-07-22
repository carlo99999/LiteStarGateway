import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState, type FormEvent } from "react";
import { PageHeader } from "@/components/common/PageHeader";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { getSsoSettings, upsertSsoSettings } from "@/features/sso/api";
import { toError } from "@/lib/toError";

const TEXTAREA_CLASS =
  "flex w-full rounded-md border border-input bg-background px-3 py-2 font-mono text-xs " +
  "text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-2 " +
  "focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-background";

function parseAdminGroups(text: string): string[] {
  return text
    .split(",")
    .map((g) => g.trim())
    .filter((g) => g.length > 0);
}

/** The single OIDC identity provider for this deployment (self-hosted, one
 * IdP per instance) — DB-backed and hot-reloadable, replacing OIDC_* env
 * vars. Takes effect on the next login attempt, no restart needed. */
export function SsoSettingsPage() {
  const queryClient = useQueryClient();
  const settings = useQuery({
    queryKey: ["platform", "sso-settings"],
    queryFn: () => getSsoSettings(),
    retry: false,
  });

  const [enabled, setEnabled] = useState(false);
  const [discoveryUrl, setDiscoveryUrl] = useState("");
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [scopes, setScopes] = useState("openid email profile groups");
  const [adminGroupsText, setAdminGroupsText] = useState("");
  const [defaultAdmin, setDefaultAdmin] = useState(false);
  const [teamMappingText, setTeamMappingText] = useState("{}");
  const [redirectUri, setRedirectUri] = useState("");
  const [hasClientSecret, setHasClientSecret] = useState(false);

  useEffect(() => {
    if (!settings.data) return;
    const s = settings.data;
    setEnabled(s.enabled);
    setDiscoveryUrl(s.discovery_url ?? "");
    setClientId(s.client_id ?? "");
    setClientSecret("");
    setScopes(s.scopes);
    setAdminGroupsText(s.admin_groups.join(", "));
    setDefaultAdmin(s.default_admin);
    setTeamMappingText(JSON.stringify(s.team_mapping, null, 2));
    setRedirectUri(s.redirect_uri ?? "");
    setHasClientSecret(s.has_client_secret);
  }, [settings.data]);

  const teamMapping = useMemo(() => {
    try {
      const parsed: unknown = JSON.parse(teamMappingText || "{}");
      return typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)
        ? { value: parsed as Record<string, unknown>, error: null }
        : { value: null, error: "must be a JSON object" };
    } catch {
      return { value: null, error: "invalid JSON" };
    }
  }, [teamMappingText]);

  const mutation = useMutation({
    mutationFn: () => {
      if (!teamMapping.value) throw new Error(`team_mapping ${teamMapping.error}`);
      return upsertSsoSettings({
        enabled,
        discovery_url: discoveryUrl.trim() || null,
        client_id: clientId.trim() || null,
        client_secret: clientSecret.trim() || null,
        scopes: scopes.trim() || "openid email profile groups",
        admin_groups: parseAdminGroups(adminGroupsText),
        default_admin: defaultAdmin,
        team_mapping: teamMapping.value as Record<string, { team: string; role: string }[]>,
        redirect_uri: redirectUri.trim() || null,
      });
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["platform", "sso-settings"] });
    },
  });

  const canSubmit =
    !mutation.isPending &&
    teamMapping.value !== null &&
    (!enabled ||
      (discoveryUrl.trim().length > 0 &&
        clientId.trim().length > 0 &&
        (clientSecret.trim().length > 0 || hasClientSecret)));

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (canSubmit) mutation.mutate();
  }

  const error = toError(settings.error);
  const loadFailed = settings.isError && !(error?.message ?? "").toLowerCase().includes("404");

  return (
    <>
      <PageHeader
        command="sso settings"
        title="Single Sign-On"
        description="The OIDC identity provider for this deployment — one IdP per instance, configured here instead of environment variables. Changes take effect on the next login, no restart needed."
      />
      {loadFailed ? (
        <p className="mb-4 font-mono text-xs text-destructive">
          ! failed to load current settings — {error?.message}
        </p>
      ) : null}
      <form onSubmit={handleSubmit} className="grid max-w-2xl gap-4">
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
          />
          <span className="text-sm">enabled</span>
        </label>

        <div className="grid gap-2">
          <Label htmlFor="sso-discovery-url">discovery url</Label>
          <Input
            id="sso-discovery-url"
            value={discoveryUrl}
            onChange={(e) => setDiscoveryUrl(e.target.value)}
            placeholder="https://idp.example.com/.well-known/openid-configuration"
            autoComplete="off"
          />
        </div>

        <div className="grid gap-2">
          <Label htmlFor="sso-client-id">client id</Label>
          <Input
            id="sso-client-id"
            value={clientId}
            onChange={(e) => setClientId(e.target.value)}
            autoComplete="off"
          />
        </div>

        <div className="grid gap-2">
          <Label htmlFor="sso-client-secret">
            client secret
            <span className="ml-1 text-muted-foreground">
              {hasClientSecret ? "(blank = keep)" : ""}
            </span>
          </Label>
          <Input
            id="sso-client-secret"
            type="password"
            value={clientSecret}
            onChange={(e) => setClientSecret(e.target.value)}
            placeholder={hasClientSecret ? "•••••• (unchanged)" : ""}
            autoComplete="off"
          />
        </div>

        <div className="grid gap-2">
          <Label htmlFor="sso-scopes">scopes</Label>
          <Input
            id="sso-scopes"
            value={scopes}
            onChange={(e) => setScopes(e.target.value)}
            autoComplete="off"
          />
        </div>

        <div className="grid gap-2">
          <Label htmlFor="sso-admin-groups">admin groups</Label>
          <Input
            id="sso-admin-groups"
            value={adminGroupsText}
            onChange={(e) => setAdminGroupsText(e.target.value)}
            placeholder="comma-separated IdP groups, e.g. gw-admins"
            autoComplete="off"
          />
        </div>

        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={defaultAdmin}
            onChange={(e) => setDefaultAdmin(e.target.checked)}
          />
          <span className="text-sm">
            new SSO users default to platform admin (when not matched by an admin group)
          </span>
        </label>

        <div className="grid gap-2">
          <Label htmlFor="sso-redirect-uri">
            redirect uri <span className="text-muted-foreground">(optional)</span>
          </Label>
          <Input
            id="sso-redirect-uri"
            value={redirectUri}
            onChange={(e) => setRedirectUri(e.target.value)}
            placeholder="derived from the request when unset"
            autoComplete="off"
          />
        </div>

        <div className="grid gap-2">
          <Label htmlFor="sso-team-mapping">
            team mapping <span className="text-muted-foreground">(JSON)</span>
          </Label>
          <textarea
            id="sso-team-mapping"
            className={TEXTAREA_CLASS}
            rows={6}
            value={teamMappingText}
            onChange={(e) => setTeamMappingText(e.target.value)}
            placeholder={'{"idp-group": [{"team": "<uuid>", "role": "admin"}]}'}
            spellCheck={false}
          />
          {teamMapping.error ? (
            <p className="font-mono text-xs text-destructive">! {teamMapping.error}</p>
          ) : null}
        </div>

        {mutation.isError ? (
          <p className="font-mono text-xs text-destructive">{mutation.error.message}</p>
        ) : null}

        <div>
          <Button type="submit" disabled={!canSubmit}>
            {mutation.isPending ? "saving…" : "save"}
          </Button>
        </div>
      </form>
    </>
  );
}
