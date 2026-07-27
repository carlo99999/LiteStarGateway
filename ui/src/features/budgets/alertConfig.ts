/** Parse a comma-separated thresholds string (e.g. "50, 80, 100") into a
 * sorted, de-duplicated list of percentages. Throws with a user-facing message
 * on any non-integer or out-of-1..100 value, mirroring the backend's
 * `validate_thresholds` boundary so the console rejects bad input before the
 * PUT. A blank string yields an empty list (no thresholds). */
export function parseThresholds(input: string): number[] {
  const parts = input
    .split(",")
    .map((part) => part.trim())
    .filter((part) => part.length > 0);
  const values = parts.map((part) => {
    if (!/^\d+$/.test(part)) {
      throw new Error(`"${part}" is not a whole number`);
    }
    const value = Number(part);
    if (value < 1 || value > 100) {
      throw new Error(`threshold ${value} must be between 1 and 100`);
    }
    return value;
  });
  return [...new Set(values)].sort((a, b) => a - b);
}

/** Render a threshold list back to the comma-separated form the input uses. */
export function formatThresholds(thresholds: number[]): string {
  return thresholds.join(", ");
}

/** Normalize an optional text input into the `string | null` the API expects:
 * a blank/whitespace-only value clears the field. */
export function normalizeOptional(value: string): string | null {
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : null;
}
