import assert from "node:assert/strict";
import test from "node:test";
import {
  DESTRUCTIVE_TOGGLE_LABEL,
  describeEffect,
  describeInventory,
  describeLastDiscovery,
  describeOrigin,
  describePolicy,
  describeRemoval,
  needsExplicitKeyGrant,
  removalOutcome,
  resolveInventory,
} from "./inventory.ts";

const DISCOVERED = "2026-07-30T12:00:00Z";

test("a failed inventory query is an error, never an empty inventory", () => {
  const view = resolveInventory({
    isLoading: false,
    isError: true,
    errorMessage: "502 the tool server is unreachable",
    tools: [],
  });

  assert.deepEqual(view, { kind: "error", message: "502 the tool server is unreachable" });
  assert.match(describeInventory(view), /unreachable/);
});

test("an error wins over a stale successful result", () => {
  // react-query keeps the last good data while a refetch fails. Rendering that
  // as a live inventory would show tools the gateway can no longer confirm.
  const view = resolveInventory({
    isLoading: false,
    isError: true,
    errorMessage: null,
    tools: [{ name: "search" }, { name: "fetch" }],
    lastDiscoveredAt: DISCOVERED,
  });

  assert.equal(view.kind, "error");
});

test("an error with no message still says something actionable", () => {
  const view = resolveInventory({ isLoading: false, isError: true, errorMessage: "   " });

  assert.deepEqual(view, { kind: "error", message: "the inventory could not be loaded" });
});

test("never discovered and offers nothing are different states", () => {
  const never = resolveInventory({ isLoading: false, isError: false, tools: [] });
  const nothing = resolveInventory({
    isLoading: false,
    isError: false,
    tools: [],
    lastDiscoveredAt: DISCOVERED,
  });

  assert.deepEqual(never, { kind: "never-discovered" });
  assert.deepEqual(nothing, { kind: "offers-nothing", discoveredAt: DISCOVERED });
  // ...and they must not read the same, or a working server looks unconfigured.
  assert.notEqual(describeInventory(never), describeInventory(nothing));
  assert.match(describeInventory(never), /no discovery has run/);
  assert.match(describeInventory(nothing), /advertises no tools/);
});

test("nothing loaded yet is loading, not empty", () => {
  assert.deepEqual(resolveInventory({ isLoading: true, isError: false }), { kind: "loading" });
  // `tools === undefined` with isLoading already false (a disabled query) is
  // still not evidence that the server has no tools.
  assert.deepEqual(resolveInventory({ isLoading: false, isError: false }), { kind: "loading" });
});

test("a populated inventory reports its count", () => {
  const one = resolveInventory({ isLoading: false, isError: false, tools: [{ name: "a" }] });
  const two = resolveInventory({
    isLoading: false,
    isError: false,
    tools: [{ name: "a" }, { name: "b" }],
  });

  assert.equal(describeInventory(one), "1 tool");
  assert.equal(describeInventory(two), "2 tools");
});

test("remove is described as delete or detach before the click", () => {
  assert.equal(describeRemoval("own").verb, "delete");
  assert.equal(describeRemoval("global").verb, "detach");
  assert.equal(describeRemoval("extended").verb, "detach");
  // The consequence names the other tenants, because that is the part a team
  // admin cannot see from their own page.
  assert.match(describeRemoval("global").consequence, /every other team keeps it/);
  assert.match(describeRemoval("own").consequence, /deletes this server/);
});

test("the removal outcome comes from the gateway, and a surprise is reported", () => {
  assert.equal(removalOutcome("deleted"), "server deleted");
  assert.match(removalOutcome("detached"), /still live for the others/);
  assert.match(removalOutcome("vanished"), /unexpected outcome/);
});

test("the destructive toggle says what it permits, not what it is called", () => {
  assert.match(DESTRUCTIVE_TOGGLE_LABEL, /delete or irreversibly change/);
  // The word "destructive" alone would just restate the field name.
  assert.notEqual(DESTRUCTIVE_TOGGLE_LABEL.toLowerCase(), "enable destructive tools");
});

test("only destructive tools need an explicit per-key grant", () => {
  assert.equal(needsExplicitKeyGrant("destructive"), true);
  assert.equal(needsExplicitKeyGrant("read"), false);
  assert.equal(needsExplicitKeyGrant("write"), false);
});

test("each effect says what the gateway will allow", () => {
  assert.match(describeEffect("read"), /reads only/);
  assert.match(describeEffect("write"), /changes something/);
  assert.match(describeEffect("destructive"), /irreversibly/);
});

test("each origin says where the server came from", () => {
  assert.match(describeOrigin("own"), /this team registered/);
  assert.match(describeOrigin("extended"), /shared with this team/);
  assert.match(describeOrigin("global"), /every team/);
});

test("an absent policy reads as unrestricted, which is the default and not an error", () => {
  const summary = describePolicy({
    restricted: false,
    destructive_enabled: false,
    allowed_tools: [],
  });

  assert.match(summary, /unrestricted/);
  assert.match(summary, /except destructive/);
});

test("a policy summary distinguishes an empty allowlist from a named one", () => {
  assert.match(
    describePolicy({ restricted: true, destructive_enabled: false, allowed_tools: [] }),
    /every tool, destructive excluded/,
  );
  assert.match(
    describePolicy({ restricted: true, destructive_enabled: true, allowed_tools: ["search"] }),
    /1 named tool, destructive included/,
  );
  assert.match(
    describePolicy({
      restricted: true,
      destructive_enabled: false,
      allowed_tools: ["search", "fetch"],
    }),
    /2 named tools/,
  );
});

test("a missing discovery timestamp reads as never, not as a blank", () => {
  assert.equal(describeLastDiscovery(null), "never");
  assert.equal(describeLastDiscovery(undefined), "never");
  assert.equal(describeLastDiscovery("not-a-date"), "unreadable timestamp");
  assert.notEqual(describeLastDiscovery(DISCOVERED), "never");
});
