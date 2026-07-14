interface BudgetRequestResult<T> {
  data?: T;
  error?: unknown;
  response: { status: number };
}

type BudgetRequest<T> = () => Promise<BudgetRequestResult<T>>;

function failure(error: unknown): Error {
  if (error && typeof error === "object") {
    const envelope = error as { error?: { message?: string }; detail?: string };
    if (envelope.error?.message) return new Error(envelope.error.message);
    if (envelope.detail) return new Error(envelope.detail);
  }
  return new Error("Failed to load budget");
}

/** Resolve the optional budget without conflating absence with auth/server errors. */
export async function loadOptionalBudget<T>(request: BudgetRequest<T>): Promise<T | null> {
  let result: BudgetRequestResult<T>;
  try {
    result = await request();
  } catch (cause) {
    throw new Error("Unable to reach the gateway", { cause });
  }

  if (result.response.status === 404) return null;
  if (result.error || !result.data) throw failure(result.error);
  return result.data;
}
