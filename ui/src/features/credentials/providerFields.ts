import type { Provider } from "@/features/credentials/api";

export interface CredentialField {
  /** The `values` key sent to the API. */
  key: string;
  label: string;
  required: boolean;
  /** Render as a password input and never echo it back. */
  secret: boolean;
  placeholder?: string;
}

/** Provider display labels for selects and table cells. */
export const PROVIDER_LABELS: Record<Provider, string> = {
  openai: "OpenAI",
  anthropic: "Anthropic",
  azure_openai: "Azure OpenAI",
  vertex_ai: "Vertex AI",
  bedrock: "Bedrock",
  databricks: "Databricks",
};

export const PROVIDERS: Provider[] = [
  "openai",
  "anthropic",
  "azure_openai",
  "vertex_ai",
  "bedrock",
  "databricks",
];

// Mirrors the /credentials endpoint contract (expected `values` keys per
// provider). Kept in sync with the backend's credential validation.
export const PROVIDER_FIELDS: Record<Provider, CredentialField[]> = {
  openai: [
    { key: "api_key", label: "API key", required: true, secret: true, placeholder: "sk-…" },
    { key: "api_base", label: "API base URL", required: false, secret: false },
    { key: "organization", label: "Organization", required: false, secret: false },
  ],
  anthropic: [
    { key: "api_key", label: "API key", required: true, secret: true, placeholder: "sk-ant-…" },
    { key: "api_base", label: "API base URL", required: false, secret: false },
  ],
  azure_openai: [
    { key: "api_key", label: "API key", required: true, secret: true },
    { key: "api_base", label: "API base URL", required: true, secret: false },
    { key: "api_version", label: "API version", required: true, secret: false },
    { key: "deployment", label: "Deployment", required: false, secret: false },
  ],
  vertex_ai: [
    { key: "vertex_project", label: "Project", required: true, secret: false },
    { key: "vertex_location", label: "Location", required: true, secret: false },
    {
      key: "vertex_credentials",
      label: "Credentials JSON",
      required: false,
      secret: true,
      placeholder: "(omit to use Application Default Credentials)",
    },
  ],
  bedrock: [
    { key: "region", label: "Region", required: true, secret: false, placeholder: "us-east-1" },
    { key: "aws_access_key_id", label: "Access key id", required: true, secret: false },
    { key: "aws_secret_access_key", label: "Secret access key", required: true, secret: true },
    { key: "aws_session_token", label: "Session token", required: false, secret: true },
  ],
  databricks: [
    { key: "api_key", label: "API key", required: true, secret: true },
    { key: "api_base", label: "API base URL", required: true, secret: false },
  ],
};
