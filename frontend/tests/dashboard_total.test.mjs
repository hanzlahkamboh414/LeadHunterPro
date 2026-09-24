import assert from "node:assert/strict";
import test from "node:test";

import { api } from "../src/api/client.ts";

test("paged companies exposes the full total beyond the 1,000-row sample", async () => {
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async (url) => {
    assert.match(String(url), /\/leads\?folder=\*&limit=1000$/);
    return new Response(JSON.stringify(Array.from({ length: 1000 }, (_, i) => ({ id: i }))), {
      status: 200,
      headers: { "Content-Type": "application/json", "X-Total-Count": "1100" },
    });
  };
  try {
    const page = await api.pageLeads({ folder: "*", limit: 1000 });
    assert.equal(page.rows.length, 1000);
    assert.equal(page.total, 1100);
  } finally {
    globalThis.fetch = previousFetch;
  }
});
