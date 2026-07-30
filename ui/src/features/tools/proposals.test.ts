import assert from "node:assert/strict";
import test from "node:test";
import {
  APPROVE_CONSEQUENCE,
  canDecide,
  describeQueue,
  describeReason,
  describeStatus,
  resolveQueue,
} from "./proposals.ts";

test("a failed queue query is an error, never an empty queue", () => {
  // The defect three separate rounds found on other pages: "nobody asked" and "we
  // could not find out" render identically, and only one lets an admin stop
  // looking.
  const view = resolveQueue({
    isLoading: false,
    isError: true,
    errorMessage: "403 you are not a member of this team",
    proposals: [],
  });

  assert.deepEqual(view, { kind: "error", message: "403 you are not a member of this team" });
  assert.match(describeQueue(view), /not a member/);
});

test("an error wins over a stale successful result", () => {
  // react-query keeps the last good data while a refetch fails. Rendering it as a
  // live queue would offer approve buttons for rows nobody can confirm.
  const view = resolveQueue({
    isLoading: false,
    isError: true,
    errorMessage: null,
    proposals: [{ status: "pending" }],
  });

  assert.equal(view.kind, "error");
  assert.match(describeQueue(view), /could not be loaded/);
});

test("an empty queue says nobody has asked, rather than nothing at all", () => {
  const view = resolveQueue({ isLoading: false, isError: false, proposals: [] });

  assert.deepEqual(view, { kind: "empty" });
  assert.match(describeQueue(view), /nobody has asked/);
});

test("the count an admin acts on is the pending one, not the total", () => {
  const view = resolveQueue({
    isLoading: false,
    isError: false,
    proposals: [{ status: "pending" }, { status: "approved" }, { status: "rejected" }],
  });

  assert.deepEqual(view, { kind: "proposals", total: 3, pending: 1 });
  assert.match(describeQueue(view), /1 waiting for a decision, of 3/);
});

test("a queue with nothing pending says so instead of reporting zero", () => {
  const view = resolveQueue({
    isLoading: false,
    isError: false,
    proposals: [{ status: "approved" }, { status: "rejected" }],
  });

  assert.match(describeQueue(view), /2 proposals, none waiting/);
});

test("only a pending proposal can be decided", () => {
  // The gateway answers a second decision with 409, so a live button on a decided
  // row invites a click whose only outcome is an error.
  assert.equal(canDecide("pending"), true);
  assert.equal(canDecide("approved"), false);
  assert.equal(canDecide("rejected"), false);
  // And an unknown status is not treated as decidable.
  assert.equal(canDecide("withdrawn"), false);
});

test("an unrecognised status is surfaced rather than read as pending", () => {
  assert.match(describeStatus("withdrawn"), /unrecognised status/);
  assert.match(describeStatus("pending"), /waiting for a team admin/);
  assert.match(describeStatus("rejected"), /refused/);
});

test("approved does not claim a server still exists", () => {
  // The API leaves `server_id` null on an approved proposal whose server was
  // deleted, so the label must not assert something the row cannot back up.
  assert.equal(describeStatus("approved"), "approved — registered as a tool server");
});

test("the reason is shown for a rejection and only for a rejection", () => {
  assert.equal(describeReason("rejected", "use the global one"), "use the global one");
  assert.equal(describeReason("pending", null), null);
  assert.equal(describeReason("approved", "leftover"), null);
});

test("a rejection with a blank reason is reported, not rendered as an empty cell", () => {
  assert.match(describeReason("rejected", "   ") ?? "", /should not happen/);
  assert.match(describeReason("rejected", null) ?? "", /should not happen/);
});

test("the approve label warns that the allowlist is re-checked now", () => {
  // Otherwise a 400 on a proposal that was legal when filed reads as a bug.
  assert.match(APPROVE_CONSEQUENCE, /MCP_ALLOWED_HOSTS/);
  assert.match(APPROVE_CONSEQUENCE, /contacts it for the first time/);
});
