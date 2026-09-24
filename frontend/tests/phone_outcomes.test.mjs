import assert from "node:assert/strict";
import test from "node:test";

import { latestSheetOutcomes } from "../src/lib/phoneOutcomes.ts";

function event(id, leadId, action) {
  return { id, lead_id: leadId, action };
}

test("latest saved outcome highlights each lead after reload", () => {
  const events = [
    event(6, 10, "dialed"),
    event(5, 10, "no_answer"),
    event(4, 20, "not_interested"),
    event(3, 10, "not_interested"),
  ];
  const outcomes = latestSheetOutcomes(events);
  assert.equal(outcomes.get(10), "no_answer");
  assert.equal(outcomes.get(20), "not_interested");
  assert.equal(outcomes.size, 2);
});

test("newer follow-up replaces an older outcome, but dial/copy alone do not highlight", () => {
  const outcomes = latestSheetOutcomes([
    event(6, 10, "follow_up"),
    event(5, 10, "no_answer"),
    event(4, 20, "copied"),
    event(3, 30, "dialed"),
  ]);
  assert.equal(outcomes.get(10), "follow_up");
  assert.equal(outcomes.has(20), false);
  assert.equal(outcomes.has(30), false);
});
