import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type FormEvent } from "react";
import { PageHeader } from "@/components/common/PageHeader";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useAuth } from "@/features/auth/use-auth";
import {
  createGuardrailRule,
  deleteGuardrailRule,
  listGuardrailRules,
  updateGuardrailRule,
  type GuardrailRule,
} from "@/features/guardrails/api";
import {
  describeRule,
  EMPTY_RULE_FORM,
  FAIL_POLICIES,
  fromRule,
  GUARDRAIL_DIRECTIONS,
  GUARDRAIL_KINDS,
  JUDGE_CATEGORIES,
  requiresSecret,
  scopeValue,
  toPayload,
  type GuardrailDirection,
  type GuardrailKind,
  type FailPolicy,
  type RuleFormState,
} from "@/features/guardrails/ruleForm";
import { listCallableModels } from "@/features/models/api";
import { listCallableRouters } from "@/features/routing/api";
import { canManageGuardrails } from "@/features/teams/access";
import { useAccessibleTeams } from "@/features/teams/useAccessibleTeams";
import { toError } from "@/lib/toError";

const SELECT_CLASS =
  "flex h-9 rounded-md border border-input bg-background px-3 py-1 font-mono text-sm " +
  "text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-2 " +
  "focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-background";

function RuleRow({
  rule,
  onEdit,
  onToggle,
  onRemove,
  busy,
}: {
  rule: GuardrailRule;
  onEdit: () => void;
  onToggle: () => void;
  onRemove: () => void;
  busy: boolean;
}) {
  return (
    <div className="flex flex-wrap items-center gap-3 rounded-lg border border-border bg-card p-4">
      <span className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
        {rule.position}
      </span>
      <div className="min-w-64 flex-1">
        <p className="text-foreground">
          {rule.name}{" "}
          <span className="font-mono text-xs text-muted-foreground">[{rule.kind}]</span>
          {rule.enabled ? null : (
            <span className="ml-2 font-mono text-xs text-muted-foreground">// disabled</span>
          )}
        </p>
        <p className="font-mono text-xs text-muted-foreground">{describeRule(rule)}</p>
      </div>
      {rule.kind === "webhook" ? (
        <span className="font-mono text-xs text-muted-foreground">
          {/* The value is never returned by any endpoint — only whether one exists. */}
          {rule.has_secret ? "signed" : "! unsigned"}
        </span>
      ) : null}
      <Button variant="outline" size="sm" onClick={onEdit} disabled={busy}>
        edit
      </Button>
      <Button variant="outline" size="sm" onClick={onToggle} disabled={busy}>
        {rule.enabled ? "disable" : "enable"}
      </Button>
      <Button variant="outline" size="sm" onClick={onRemove} disabled={busy}>
        remove
      </Button>
    </div>
  );
}

/** Per-team guardrail chains: the ordered providers that inspect a prompt
 * before it is sent and an answer before it is returned. Redactions compose in
 * position order; a model-scoped rule replaces the team-wide ones for that
 * model. Signing secrets are write-only — the console can see that one exists,
 * never what it is. */
export function GuardrailsPage() {
  const queryClient = useQueryClient();
  const { user } = useAuth();
  const teams = useAccessibleTeams(canManageGuardrails);
  const [teamId, setTeamId] = useState("");
  const [editing, setEditing] = useState<GuardrailRule | null>(null);
  const [form, setForm] = useState<RuleFormState>(EMPTY_RULE_FORM);
  const [formError, setFormError] = useState<string | null>(null);

  const rules = useQuery({
    queryKey: ["teams", teamId, "guardrails"],
    queryFn: () => listGuardrailRules(teamId),
    enabled: teamId.length > 0,
  });
  // Everything the team can call, for the two scope/judge selects. A rule used
  // to be scoped by typing a raw model UUID.
  const callableModels = useQuery({
    queryKey: ["teams", teamId, "models", "callable"],
    queryFn: () => listCallableModels(teamId),
    enabled: teamId.length > 0,
  });
  const callableRouters = useQuery({
    queryKey: ["teams", teamId, "routers", "callable"],
    queryFn: () => listCallableRouters(teamId),
    enabled: teamId.length > 0,
  });

  useEffect(() => {
    if (!teamId && teams.data?.length) setTeamId(teams.data[0].id);
  }, [teamId, teams.data]);
  useEffect(() => {
    setEditing(null);
    setForm(EMPTY_RULE_FORM);
    setFormError(null);
  }, [teamId]);

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["teams", teamId, "guardrails"] });

  const save = useMutation({
    mutationFn: (state: RuleFormState) => {
      const payload = toPayload(state, { forUpdate: Boolean(editing) });
      return editing
        ? updateGuardrailRule(teamId, editing.id, payload)
        : createGuardrailRule(teamId, payload);
    },
    onSuccess: () => {
      setEditing(null);
      setForm(EMPTY_RULE_FORM);
    },
    onSettled: invalidate,
  });
  const toggle = useMutation({
    mutationFn: (rule: GuardrailRule) =>
      updateGuardrailRule(teamId, rule.id, { enabled: !rule.enabled }),
    onSettled: invalidate,
  });
  const remove = useMutation({
    mutationFn: (rule: GuardrailRule) => deleteGuardrailRule(teamId, rule.id),
    onSettled: invalidate,
  });

  const busy = save.isPending || toggle.isPending || remove.isPending;
  const mutationError = toError(save.error ?? toggle.error ?? remove.error)?.message ?? null;

  function set<K extends keyof RuleFormState>(key: K, value: RuleFormState[K]) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  function startEdit(rule: GuardrailRule) {
    setEditing(rule);
    setForm(fromRule(rule));
    setFormError(null);
  }

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (requiresSecret(form, Boolean(editing?.has_secret))) {
      setFormError("a webhook guardrail needs a signing secret — the payload is the user's prompt");
      return;
    }
    try {
      toPayload(form);
    } catch (err) {
      setFormError(toError(err)?.message ?? "Invalid guardrail");
      return;
    }
    setFormError(null);
    save.mutate(form);
  }

  function toggleCategory(category: string) {
    setForm((current) => ({
      ...current,
      blockCategories: current.blockCategories.includes(category)
        ? current.blockCategories.filter((c) => c !== category)
        : [...current.blockCategories, category],
    }));
  }

  return (
    <>
      <PageHeader
        command="guardrails list"
        title="Guardrails"
        description="Ordered content checks per team. A request-side rule runs before the provider is called and before any budget is reserved; a response-side rule runs after the call is billed. Redactions compose in position order."
      />
      <div className="mb-4 flex items-center gap-3">
        <Label htmlFor="guardrail-team">team</Label>
        <select
          id="guardrail-team"
          className={SELECT_CLASS + " min-w-64"}
          value={teamId}
          onChange={(event) => setTeamId(event.target.value)}
          disabled={teams.isLoading || teams.isError}
        >
          <option value="" disabled>
            {teams.isLoading
              ? "loading…"
              : teams.isError
                ? "failed to load teams"
                : "select a team"}
          </option>
          {(teams.data ?? []).map((team) => (
            <option key={team.id} value={team.id}>
              {team.name}
            </option>
          ))}
        </select>
      </div>

      {rules.isError ? (
        <p className="mb-6 font-mono text-xs text-destructive">
          ! couldn&apos;t load the guardrails — {toError(rules.error)?.message}
        </p>
      ) : (rules.data ?? []).length === 0 && teamId && !rules.isLoading ? (
        <p className="mb-6 font-mono text-xs text-muted-foreground">
          // no guardrails configured — every prompt and answer passes through unchecked.
        </p>
      ) : (
        <div className="mb-6 flex flex-col gap-2">
          {(rules.data ?? []).map((rule) => (
            <RuleRow
              key={rule.id}
              rule={rule}
              busy={busy}
              onEdit={() => startEdit(rule)}
              onToggle={() => toggle.mutate(rule)}
              onRemove={() => remove.mutate(rule)}
            />
          ))}
        </div>
      )}

      {mutationError ? (
        <p className="mb-4 font-mono text-xs text-destructive">! {mutationError}</p>
      ) : null}

      <form onSubmit={handleSubmit} className="flex flex-col gap-4">
        <p className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
          // {editing ? `edit ${editing.name}` : "add a guardrail"}
        </p>
        <div className="flex flex-wrap items-end gap-3">
          <div className="grid gap-2">
            <Label htmlFor="guardrail-name">name</Label>
            <Input
              id="guardrail-name"
              value={form.name}
              onChange={(event) => set("name", event.target.value)}
              placeholder="pii-scan"
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="guardrail-kind">kind</Label>
            <select
              id="guardrail-kind"
              className={SELECT_CLASS}
              value={form.kind}
              onChange={(event) => set("kind", event.target.value as GuardrailKind)}
              // A rule's kind is fixed once created: the update endpoint has no
              // field for it, so offering the choice here only produced a
              // confusing 400 about the other kind's config keys.
              disabled={Boolean(editing)}
            >
              {GUARDRAIL_KINDS.map((kind) => (
                <option key={kind} value={kind}>
                  {kind}
                </option>
              ))}
            </select>
          </div>
          <div className="grid gap-2">
            <Label htmlFor="guardrail-direction">direction</Label>
            <select
              id="guardrail-direction"
              className={SELECT_CLASS}
              value={form.direction}
              onChange={(event) => set("direction", event.target.value as GuardrailDirection)}
            >
              {GUARDRAIL_DIRECTIONS.map((direction) => (
                <option key={direction} value={direction}>
                  {direction}
                </option>
              ))}
            </select>
          </div>
          <div className="grid gap-2">
            <Label htmlFor="guardrail-fail">on provider failure</Label>
            <select
              id="guardrail-fail"
              className={SELECT_CLASS}
              value={form.failPolicy}
              onChange={(event) => set("failPolicy", event.target.value as FailPolicy)}
            >
              {FAIL_POLICIES.map((policy) => (
                <option key={policy} value={policy}>
                  {policy === "closed" ? "closed — refuse the request" : "open — let it through"}
                </option>
              ))}
            </select>
          </div>
          <div className="grid gap-2">
            <Label htmlFor="guardrail-position">position</Label>
            <Input
              id="guardrail-position"
              className="w-24"
              value={form.position}
              onChange={(event) => set("position", event.target.value)}
            />
          </div>
        </div>

        {form.kind === "webhook" ? (
          <div className="flex flex-wrap items-end gap-3">
            <div className="grid gap-2">
              <Label htmlFor="guardrail-url">url (https)</Label>
              <Input
                id="guardrail-url"
                className="min-w-80"
                value={form.url}
                onChange={(event) => set("url", event.target.value)}
                placeholder="https://scanner.internal/check"
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="guardrail-timeout">timeout (ms)</Label>
              <Input
                id="guardrail-timeout"
                className="w-28"
                value={form.timeoutMs}
                onChange={(event) => set("timeoutMs", event.target.value)}
                placeholder="2000"
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="guardrail-secret">
                signing secret {editing?.has_secret ? "(leave blank to keep)" : ""}
              </Label>
              <Input
                id="guardrail-secret"
                type="password"
                className="min-w-64"
                value={form.signingSecret}
                onChange={(event) => set("signingSecret", event.target.value)}
                autoComplete="new-password"
              />
            </div>
          </div>
        ) : (
          <div className="flex flex-col gap-3">
            <div className="flex flex-wrap items-end gap-3">
              <div className="grid gap-2">
                <Label htmlFor="guardrail-judge">judge model</Label>
                {/* Chat models only, and no routers: the judge resolves through
                    the model repository and rejects a non-chat model, so any
                    other option here would be a guaranteed runtime failure. A
                    safety control also wants a fixed model, not a routed one. */}
                <select
                  id="guardrail-judge"
                  className={SELECT_CLASS + " min-w-64"}
                  value={form.judgeModel}
                  onChange={(event) => set("judgeModel", event.target.value)}
                >
                  <option value="">select a chat model…</option>
                  {(callableModels.data ?? [])
                    .filter((entry) => entry.model.type === "chat")
                    .map((entry) => (
                      <option key={entry.alias} value={entry.alias}>
                        {entry.alias}
                      </option>
                    ))}
                </select>
              </div>
              <div className="grid gap-2">
                <Label htmlFor="guardrail-budget">char budget</Label>
                <Input
                  id="guardrail-budget"
                  className="w-28"
                  value={form.charBudget}
                  onChange={(event) => set("charBudget", event.target.value)}
                  placeholder="4000"
                />
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-3">
              <span className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
                // block on
              </span>
              {JUDGE_CATEGORIES.map((category) => (
                <label key={category} className="flex items-center gap-1 font-mono text-xs">
                  <input
                    type="checkbox"
                    checked={form.blockCategories.includes(category)}
                    onChange={() => toggleCategory(category)}
                  />
                  {category}
                </label>
              ))}
              <span className="font-mono text-xs text-muted-foreground">
                // none selected = every category blocks
              </span>
            </div>
          </div>
        )}

        <div className="flex flex-wrap items-end gap-3">
          <div className="grid gap-2">
            <Label htmlFor="guardrail-scope">scope</Label>
            {/* A router scope outranks a rule on the resolved model: the caller
                asked for the alias, and attaching the policy to candidates
                instead leaves a hole that opens the day one is added. */}
            <select
              id="guardrail-scope"
              className={SELECT_CLASS + " min-w-80"}
              value={form.scope}
              onChange={(event) => set("scope", event.target.value)}
            >
              <option value="">all models (team-wide)</option>
              {(callableRouters.data ?? []).map((entry) => (
                <option key={`router-${entry.router.id}`} value={scopeValue("router", entry.router.id)}>
                  router · {entry.alias}
                </option>
              ))}
              {(callableModels.data ?? []).map((entry) => (
                <option key={`model-${entry.model.id}`} value={scopeValue("model", entry.model.id)}>
                  model · {entry.alias}
                </option>
              ))}
            </select>
          </div>
          <label className="flex items-center gap-2 font-mono text-xs">
            <input
              type="checkbox"
              checked={form.enabled}
              onChange={(event) => set("enabled", event.target.checked)}
            />
            enabled
          </label>
        </div>

        {formError ? <p className="font-mono text-xs text-destructive">! {formError}</p> : null}

        <div className="flex gap-3">
          <Button type="submit" disabled={busy || teamId === ""}>
            {editing ? "save" : "add"}
          </Button>
          {editing ? (
            <Button
              type="button"
              variant="outline"
              onClick={() => {
                setEditing(null);
                setForm(EMPTY_RULE_FORM);
                setFormError(null);
              }}
              disabled={busy}
            >
              cancel
            </Button>
          ) : null}
        </div>
      </form>

      {user?.is_admin ? null : (
        <p className="mt-6 font-mono text-xs text-muted-foreground">
          // guardrails are managed by team admins; changes are recorded in the audit trail.
        </p>
      )}
    </>
  );
}
