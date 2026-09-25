import test from "node:test";
import assert from "node:assert/strict";

import { chooseTenant, tenantStorageKey } from "../src/lib/tenantSelection.ts";

test("stored tenant is used only while the user still belongs to it", () => {
  const memberships = [{ id: "first" }, { id: "second" }];
  assert.equal(chooseTenant(memberships, "second"), "second");
  assert.equal(chooseTenant(memberships, "revoked"), "first");
  assert.equal(chooseTenant([], "first"), "");
});

test("saved selection is separated by user", () => {
  assert.notEqual(tenantStorageKey("user-a"), tenantStorageKey("user-b"));
});
