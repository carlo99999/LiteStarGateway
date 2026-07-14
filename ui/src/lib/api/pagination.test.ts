import assert from "node:assert/strict";
import test from "node:test";
import {
  fetchAllPages,
  pageRequest,
  pageResult,
  previousPageOffset,
} from "./pagination.ts";

test("pageRequest asks for one sentinel row", () => {
  assert.deepEqual(pageRequest(100, 50), { limit: 51, offset: 100 });
});

test("pageResult trims the sentinel without mutating the response", () => {
  const rows = Object.freeze([1, 2, 3, 4]);

  assert.deepEqual(pageResult(rows, 10, 3), {
    items: [1, 2, 3],
    offset: 10,
    pageSize: 3,
    hasNext: true,
  });
  assert.deepEqual(rows, [1, 2, 3, 4]);
});

test("pageResult marks a short page as final and rejects oversized responses", () => {
  assert.deepEqual(pageResult([1, 2], 0, 3), {
    items: [1, 2],
    offset: 0,
    pageSize: 3,
    hasNext: false,
  });
  assert.throws(() => pageResult([1, 2, 3, 4, 5], 0, 3), /more rows than requested/i);
});

test("sentinel pages reach record 101 without a gap or duplicate", () => {
  const source = Array.from({ length: 101 }, (_, index) => index + 1);
  const firstRequest = pageRequest(0, 50);
  const first = pageResult(
    source.slice(firstRequest.offset, firstRequest.offset + firstRequest.limit),
    firstRequest.offset,
    50,
  );
  const secondRequest = pageRequest(first.offset + first.pageSize, 50);
  const second = pageResult(
    source.slice(secondRequest.offset, secondRequest.offset + secondRequest.limit),
    secondRequest.offset,
    50,
  );
  const thirdRequest = pageRequest(second.offset + second.pageSize, 50);
  const third = pageResult(
    source.slice(thirdRequest.offset, thirdRequest.offset + thirdRequest.limit),
    thirdRequest.offset,
    50,
  );

  assert.equal(first.hasNext, true);
  assert.equal(second.hasNext, true);
  assert.deepEqual([...first.items, ...second.items], source.slice(0, 100));
  assert.deepEqual(third.items, [101]);
  assert.equal(third.hasNext, false);
});

test("pagination inputs must be safe non-negative integers", () => {
  assert.throws(() => pageRequest(-1, 50), /offset/i);
  assert.throws(() => pageRequest(0, 0), /page size/i);
  assert.throws(() => pageRequest(0.5, 50), /offset/i);
  assert.equal(previousPageOffset(25, 50), 0);
  assert.equal(previousPageOffset(100, 50), 50);
});

test("fetchAllPages returns empty and short collections in one request", async () => {
  const emptyRequests: unknown[] = [];
  const empty = await fetchAllPages<number>(async (request) => {
    emptyRequests.push(request);
    return [];
  }, { pageSize: 3 });
  assert.deepEqual(empty, []);
  assert.deepEqual(emptyRequests, [{ limit: 3, offset: 0 }]);

  const short = await fetchAllPages(async () => [1, 2], { pageSize: 3 });
  assert.deepEqual(short, [1, 2]);
});

test("fetchAllPages advances by full pages until the short final page", async () => {
  const source = [1, 2, 3, 4, 5, 6, 7];
  const requests: unknown[] = [];
  const result = await fetchAllPages(async ({ limit, offset }) => {
    requests.push({ limit, offset });
    return source.slice(offset, offset + limit);
  }, { pageSize: 3 });

  assert.deepEqual(result, source);
  assert.deepEqual(requests, [
    { limit: 3, offset: 0 },
    { limit: 3, offset: 3 },
    { limit: 3, offset: 6 },
  ]);
});

test("fetchAllPages probes once after an exact multiple", async () => {
  const source = [1, 2, 3, 4, 5, 6];
  const offsets: number[] = [];
  const result = await fetchAllPages(async ({ limit, offset }) => {
    offsets.push(offset);
    return source.slice(offset, offset + limit);
  }, { pageSize: 3 });

  assert.deepEqual(result, source);
  assert.deepEqual(offsets, [0, 3, 6]);
});

test("fetchAllPages propagates failures and guards against endless pagination", async () => {
  await assert.rejects(
    fetchAllPages(async ({ offset }) => {
      if (offset > 0) throw new Error("second page failed");
      return [1, 2];
    }, { pageSize: 2 }),
    /second page failed/,
  );

  await assert.rejects(
    fetchAllPages(async () => [1], { pageSize: 1, maxPages: 2 }),
    /maximum page count/i,
  );
});

test("fetchAllPages can de-duplicate stable identities without mutating pages", async () => {
  const pages = [
    Object.freeze([{ id: "a" }, { id: "b" }]),
    Object.freeze([{ id: "b" }, { id: "c" }]),
    Object.freeze([]),
  ];
  let index = 0;
  const result = await fetchAllPages(async () => pages[index++] ?? [], {
    pageSize: 2,
    keyOf: (item) => item.id,
  });

  assert.deepEqual(result, [{ id: "a" }, { id: "b" }, { id: "c" }]);
  assert.deepEqual(pages[1], [{ id: "b" }, { id: "c" }]);
});

test("fetchAllPages rejects a repeated full page and excessive item counts", async () => {
  await assert.rejects(
    fetchAllPages(async () => [{ id: "same" }], {
      pageSize: 1,
      keyOf: (item) => item.id,
    }),
    /no progress/i,
  );

  await assert.rejects(
    fetchAllPages(async ({ offset }) => [offset], { pageSize: 1, maxItems: 2 }),
    /maximum item count/i,
  );
});
