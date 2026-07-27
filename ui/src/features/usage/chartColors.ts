/** Fixed categorical color order (never cycled/re-sorted) — CSS vars defined
 * per-theme in `styles/globals.css`, validated per the dataviz skill. Series
 * past the 8th slot fold into a shared "other" swatch rather than repeating
 * a hue, per the skill's non-negotiable on categorical color assignment. */
const CHART_COLOR_VARS = [
  "--chart-1",
  "--chart-2",
  "--chart-3",
  "--chart-4",
  "--chart-5",
  "--chart-6",
  "--chart-7",
  "--chart-8",
] as const;

export const MAX_DIRECT_SERIES = CHART_COLOR_VARS.length;

/** CSS `var(...)` reference for the Nth series in a fixed order — beyond the
 * 8 available slots, every further series shares the muted-foreground token
 * (grouped visually as "other" in the legend). */
export function seriesColorVar(index: number): string {
  const slot = CHART_COLOR_VARS[index];
  return slot ? `var(${slot})` : "var(--muted-foreground)";
}
