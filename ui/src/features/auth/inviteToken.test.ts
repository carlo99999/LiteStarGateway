import assert from "node:assert/strict";
import test from "node:test";
import {
  buildInviteSignupLink,
  createInviteTokenStore,
  inviteTokenFromFragment,
  redactedSignupAddress,
} from "./inviteToken.ts";

test("invite links carry the bearer only in the URL fragment", () => {
  const link = buildInviteSignupLink("https://gateway.example", "secret+/= token");
  const url = new URL(link);

  assert.equal(url.origin, "https://gateway.example");
  assert.equal(url.pathname, "/ui/signup");
  assert.equal(url.search, "");
  assert.equal(url.hash, "#token=secret%2B%2F%3D%20token");
});

test("the signup token is read only from the fragment", () => {
  assert.equal(inviteTokenFromFragment("#token=secret%2Bvalue"), "secret+value");
  assert.equal(inviteTokenFromFragment(""), "");
  assert.equal(inviteTokenFromFragment("#unrelated=value"), "");
});

test("redaction clears fragments and removes legacy query tokens", () => {
  assert.equal(
    redactedSignupAddress("/ui/signup", "?locale=en&token=legacy-secret"),
    "/ui/signup?locale=en",
  );
  assert.equal(redactedSignupAddress("/ui/signup", ""), "/ui/signup");
});

test("capture scrubs history once and survives repeated initialization", () => {
  const store = createInviteTokenStore();
  const state = { routerKey: "preserved" };
  const replacements: Array<{ state: unknown; url: string }> = [];
  const history = {
    state,
    replaceState(nextState: unknown, _unused: string, url: string | URL | null) {
      replacements.push({ state: nextState, url: String(url) });
    },
  };
  const location = {
    pathname: "/ui/signup",
    search: "?locale=en",
    hash: "#token=single-use-secret",
  };

  store.capture(location, history);
  store.capture(location, history);

  assert.equal(store.get(), "single-use-secret");
  assert.deepEqual(replacements, [{ state, url: "/ui/signup?locale=en" }]);
  store.clear();
  assert.equal(store.get(), "");
});

test("legacy query tokens are scrubbed but never accepted", () => {
  const store = createInviteTokenStore();
  const replacements: string[] = [];
  store.capture(
    { pathname: "/ui/signup", search: "?token=legacy-secret", hash: "" },
    {
      state: null,
      replaceState(_state: unknown, _unused: string, url: string | URL | null) {
        replacements.push(String(url));
      },
    },
  );

  assert.equal(store.get(), "");
  assert.deepEqual(replacements, ["/ui/signup"]);
});

test("capture ignores other routes without disabling a later signup capture", () => {
  const store = createInviteTokenStore();
  let replaced = false;
  const history = {
    state: null,
    replaceState() {
      replaced = true;
    },
  };
  store.capture(
    { pathname: "/ui/login", search: "", hash: "#token=do-not-consume" },
    history,
  );

  assert.equal(store.get(), "");
  assert.equal(replaced, false);

  store.capture(
    { pathname: "/ui/signup", search: "", hash: "#token=consume-this" },
    history,
  );
  assert.equal(store.get(), "consume-this");
  assert.equal(replaced, true);
});
