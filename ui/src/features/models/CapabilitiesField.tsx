import type { Provider } from "@/features/credentials/api";

/** Gateway operations a model may be declared to serve (Plan 18). Mirrors the
 * backend's `DECLARABLE_CAPABILITIES`. */
export const DECLARABLE_CAPABILITIES = [
  { value: "chat.completions", label: "chat completions" },
  { value: "embeddings", label: "embeddings" },
  { value: "image_generation", label: "image generation" },
] as const;

export const CHAT_CAPABILITY = "chat.completions";

interface CapabilitiesFieldProps {
  provider: Provider;
  value: string[];
  onChange: (next: string[]) => void;
}

/**
 * Only `openai_compatible` models declare capabilities: every other provider
 * advertises a fixed operation set, and the backend refuses a declaration on
 * one. Renders nothing elsewhere, so the field cannot be filled in where it
 * would only produce a 400.
 */
export function CapabilitiesField({ provider, value, onChange }: CapabilitiesFieldProps) {
  if (provider !== "openai_compatible") return null;

  function toggle(capability: string, checked: boolean) {
    // Immutable update, and chat stays declared: clearing everything means
    // "chat only" on the backend anyway, so an empty box would misrepresent it.
    const next = checked
      ? [...value, capability]
      : value.filter((entry) => entry !== capability);
    onChange(next.length > 0 ? next : [CHAT_CAPABILITY]);
  }

  return (
    <div className="grid gap-1.5">
      <span className="text-sm">capabilities</span>
      <p className="text-xs text-muted-foreground">
        What this backend serves. Anything left unchecked returns 501 — the gateway never
        probes the endpoint to find out.
      </p>
      {DECLARABLE_CAPABILITIES.map((capability) => (
        <label
          key={capability.value}
          className="flex cursor-pointer items-center gap-1.5 text-sm"
        >
          <input
            type="checkbox"
            checked={value.includes(capability.value)}
            disabled={capability.value === CHAT_CAPABILITY && value.length === 1}
            onChange={(e) => toggle(capability.value, e.target.checked)}
          />
          {capability.label}
        </label>
      ))}
    </div>
  );
}
