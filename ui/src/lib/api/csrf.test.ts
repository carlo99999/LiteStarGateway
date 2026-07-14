import assert from "node:assert/strict";
import test from "node:test";
import { csrfHeaderValue } from "./csrf.ts";

test("CSRF is attached to every unsafe method", () => {
  for (const method of ["POST", "PUT", "PATCH", "DELETE"]) {
    assert.equal(csrfHeaderValue(method, "secret"), "secret");
  }
});

test("CSRF is omitted from safe methods and when no session is loaded", () => {
  for (const method of ["GET", "HEAD", "OPTIONS"]) {
    assert.equal(csrfHeaderValue(method, "secret"), null);
  }
  assert.equal(csrfHeaderValue("POST", null), null);
});
