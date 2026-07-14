export const TABLE_PAGE_SIZE = 50;
export const FETCH_ALL_PAGE_SIZE = 500;

export interface PageRequest {
  limit: number;
  offset: number;
}

export interface PageResult<T> {
  items: T[];
  offset: number;
  pageSize: number;
  hasNext: boolean;
}

interface FetchAllOptions<T> {
  pageSize?: number;
  maxPages?: number;
  maxItems?: number;
  keyOf?: (item: T) => string;
}

function positiveInteger(value: number, label: string): void {
  if (!Number.isSafeInteger(value) || value <= 0) {
    throw new RangeError(`${label} must be a positive safe integer`);
  }
}

function validOffset(offset: number): void {
  if (!Number.isSafeInteger(offset) || offset < 0) {
    throw new RangeError("offset must be a non-negative safe integer");
  }
}

/** Request one extra row so a bare-array endpoint can expose whether a next
 * page exists without changing its response schema. */
export function pageRequest(offset: number, pageSize = TABLE_PAGE_SIZE): PageRequest {
  validOffset(offset);
  positiveInteger(pageSize, "page size");
  return { limit: pageSize + 1, offset };
}

/** Split a sentinel response into the visible rows and next-page state. */
export function pageResult<T>(
  rows: readonly T[],
  offset: number,
  pageSize = TABLE_PAGE_SIZE,
): PageResult<T> {
  validOffset(offset);
  positiveInteger(pageSize, "page size");
  if (rows.length > pageSize + 1) {
    throw new Error("Pagination response returned more rows than requested");
  }
  return {
    items: rows.slice(0, pageSize),
    offset,
    pageSize,
    hasNext: rows.length > pageSize,
  };
}

export function previousPageOffset(offset: number, pageSize = TABLE_PAGE_SIZE): number {
  validOffset(offset);
  positiveInteger(pageSize, "page size");
  return Math.max(0, offset - pageSize);
}

/** Fetch every page for bounded lookup/aggregate consumers. A short response
 * terminates the sequence; an exact multiple is confirmed with one empty page. */
export async function fetchAllPages<T>(
  fetchPage: (request: PageRequest) => Promise<readonly T[]>,
  {
    pageSize = FETCH_ALL_PAGE_SIZE,
    // 20 full 500-row pages plus the empty/short terminator probe. Native
    // selectors are not usable beyond this bound, and callers surface a hard
    // error instead of silently truncating the collection.
    maxPages = 21,
    maxItems = 10_000,
    keyOf,
  }: FetchAllOptions<T> = {},
): Promise<T[]> {
  positiveInteger(pageSize, "page size");
  positiveInteger(maxPages, "maximum page count");
  positiveInteger(maxItems, "maximum item count");

  const result: T[] = [];
  const seen = keyOf ? new Set<string>() : null;
  let offset = 0;
  for (let page = 0; page < maxPages; page += 1) {
    const rows = await fetchPage({ limit: pageSize, offset });
    if (rows.length > pageSize) {
      throw new Error("Pagination response returned more rows than requested");
    }
    const before = result.length;
    for (const row of rows) {
      const key = keyOf?.(row);
      if (key !== undefined) {
        if (seen?.has(key)) continue;
        seen?.add(key);
      }
      result.push(row);
      if (result.length > maxItems) {
        throw new Error("Pagination exceeded the maximum item count");
      }
    }
    if (rows.length < pageSize) return result;
    if (seen && result.length === before) {
      throw new Error("Pagination made no progress across a full page");
    }
    offset += rows.length;
  }
  throw new Error("Pagination exceeded the maximum page count");
}
