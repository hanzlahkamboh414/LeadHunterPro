import type { PhoneCallAction, PhoneCallEvent } from "../types";

export type SheetOutcome = Extract<PhoneCallAction, "not_interested" | "follow_up" | "no_answer">;

/** The activity API returns newest events first; dial/copy must not clear an outcome. */
export function latestSheetOutcomes(events: readonly PhoneCallEvent[]): ReadonlyMap<number, SheetOutcome> {
  const outcomes = new Map<number, SheetOutcome>();
  for (const event of events) {
    if (outcomes.has(event.lead_id)) continue;
    if (event.action === "not_interested" || event.action === "follow_up" || event.action === "no_answer") {
      outcomes.set(event.lead_id, event.action);
    }
  }
  return outcomes;
}
