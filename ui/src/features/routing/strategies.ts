/** Strategy metadata for the router form — mirrors the backend registry
 * (`application/routing/`): each strategy's id, label, and what its
 * `strategy_config` requires. */

export type StrategyId =
  | "complexity"
  | "weighted"
  | "judge"
  | "webhook"
  | "hybrid"
  | "embeddings";

export const STRATEGY_IDS: StrategyId[] = [
  "complexity",
  "weighted",
  "judge",
  "webhook",
  "hybrid",
  "embeddings",
];

export const STRATEGY_LABELS: Record<StrategyId, string> = {
  complexity: "complexity — rule-based tiering",
  weighted: "weighted — traffic split by candidate weight",
  judge: "judge — an LLM picks the tier",
  webhook: "webhook — your endpoint decides",
  hybrid: "hybrid — rules first, escalate the gray zone",
  embeddings: "embeddings — semantic routes",
};

export const STRATEGY_HELP: Record<StrategyId, string> = {
  complexity:
    "Scores each prompt (keywords, length, structure) into a tier and picks the cheapest candidate serving it. No extra config.",
  weighted:
    "Splits traffic across candidates proportionally to their weight — set a weight on each candidate below.",
  judge:
    "A designated team chat model classifies the prompt's tier. Adds one small LLM call per request.",
  webhook: "POSTs the prompt to your HTTP endpoint, which returns the model to use.",
  hybrid:
    "Rule-based scoring first; only ambiguous prompts escalate to the judge or webhook — near-judge quality at a fraction of the cost.",
  embeddings:
    "Embeds the prompt and matches it against example utterances per route; the best route above its threshold wins.",
};

/** Quality tiers a candidate can serve (domain `QualityTier`). */
export const QUALITY_TIERS = ["SIMPLE", "MEDIUM", "COMPLEX", "REASONING"] as const;
export type QualityTier = (typeof QUALITY_TIERS)[number];

/** Strategies whose empty config is valid — safe to run as a shadow (a shadow
 * runs with `strategy_config.shadow`, which this form leaves empty). */
export const SHADOWABLE_STRATEGIES: StrategyId[] = ["complexity", "weighted"];
