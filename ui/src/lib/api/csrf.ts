const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

export function csrfHeaderValue(method: string, token: string | null): string | null {
  if (!token || SAFE_METHODS.has(method.toUpperCase())) return null;
  return token;
}
