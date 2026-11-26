import type { Row } from "./types";

export type Place = { top: number; turn?: string; offset?: number };
type Draft = { text: string; quote: Row | null; at: number };

export function remember(key: string, value: unknown) {
  try {
    sessionStorage.setItem("xudu:" + key, JSON.stringify(value));
  } catch {
    /* The live session still works without browser storage. */
  }
}
export function recall<T>(key: string): T | null {
  try {
    return JSON.parse(sessionStorage.getItem("xudu:" + key) || "null");
  } catch {
    return null;
  }
}
export function loadDraft(key: string, progress: number) {
  const d = recall<Draft>(key + ":draft");
  return d && d.at <= progress ? d : { text: "", quote: null };
}
export function position(flow: HTMLDivElement | null): Place {
  if (!flow) return { top: 0 };
  const top = flow.getBoundingClientRect().top;
  const el = Array.from(flow.querySelectorAll<HTMLElement>("[data-turn]")).find(
    (item) => item.getBoundingClientRect().bottom > top,
  );
  return {
    top: flow.scrollTop,
    turn: el?.dataset.turn,
    offset: el ? el.getBoundingClientRect().top - top : undefined,
  };
}
export function restore(flow: HTMLDivElement | null, place: Place | null) {
  if (!flow) return;
  if (!place) {
    flow.scrollTop = flow.scrollHeight;
    return;
  }
  const el = place.turn
    ? flow.querySelector<HTMLElement>(
        '[data-turn="' + CSS.escape(place.turn) + '"]',
      )
    : null;
  if (el && place.offset !== undefined)
    flow.scrollTop +=
      el.getBoundingClientRect().top -
      flow.getBoundingClientRect().top -
      place.offset;
  else flow.scrollTop = place.top;
}
