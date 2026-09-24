import assert from "node:assert/strict";
import test from "node:test";

import { canSeeEmails, canSeePhones, isPhoneOnly } from "../src/lib/verticals.ts";

test("phone-only accounts see phones but not email workspace", () => {
  assert.equal(canSeePhones("phones", false, true), true);
  assert.equal(canSeeEmails("phones", false, true), false);
  assert.equal(isPhoneOnly("phones", false, true), true);
});

test("email-only accounts see email workspace but not phones", () => {
  assert.equal(canSeePhones("emails", false, true), false);
  assert.equal(canSeeEmails("emails", false, true), true);
  assert.equal(isPhoneOnly("emails", false, true), false);
});

test("both, admin, and open-mode accounts retain both workspaces", () => {
  for (const [category, admin, exists] of [
    ["both", false, true], ["phones", true, true], [null, false, false],
  ]) {
    assert.equal(canSeePhones(category, admin, exists), true);
    assert.equal(canSeeEmails(category, admin, exists), true);
    assert.equal(isPhoneOnly(category, admin, exists), false);
  }
});
