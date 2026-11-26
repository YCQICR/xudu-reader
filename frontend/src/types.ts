export type Row = {
  pos: number;
  text: string;
  page: number;
  boxes?: number[][];
};
export type Chapter = {
  page: number;
  start: number;
  end: number;
  label: string;
  title: string;
};
export type Topic = { id: string; title: string; members: string[] };
export type ImportReport = { readable_pages: number; missing_pages: number[] };
export type Book = {
  card_topics: Topic[];
  topics_pending: boolean;
  id: string;
  title: string;
  pages: number;
  progress: number;
  tracked: string[];
  previous: number;
  rev: number;
  last: number;
  location: Row | null;
  chapters: Chapter[];
  threads: { id: string; title: string }[];
  sample: boolean;
  sample_preview_page?: number | null;
  sample_author?: string | null;
  import_report?: ImportReport;
};
export type Answer = {
  status: string;
  reader_note?: string;
  conversational?: boolean;
  prose?: boolean;
  cast_start?: number;
  tally?: { unit: string; checked: number };
  claims: { text: string; name?: string; refs: Row[]; paragraph_start?: boolean; replaces?: string[] }[];
  background: string;
};
export type Turn = {
  id: string;
  pos?: number;
  hidden?: boolean;
  question?: string;
  answer?: Answer;
  pending?: boolean;
  spoiler?: boolean;
};
export type Library = {
  username: string | null;
  invitation_required: boolean;
  demo: boolean;
  book: Book | null;
  books: { id: string; title: string }[];
};
export class ApiError extends Error {
  constructor(message: string, readonly status: number) { super(message); }
}
export async function api<T>(
  path: string,
  method = "GET",
  body?: unknown,
): Promise<T> {
  const form = body instanceof FormData;
  const res = await fetch(path, {
    method,
    headers: body && !form ? { "Content-Type": "application/json" } : undefined,
    body: body ? (form ? body : JSON.stringify(body)) : undefined,
  });
  if (!res.ok) {
    const json = await res.json().catch(() => ({}));
    throw new ApiError(
      typeof json.detail === "string" ? json.detail : "请求未完成，请重试。", res.status,
    );
  }
  return res.json();
}
export function chapterAt(b: Book, pos: number) {
  return b.chapters.filter((ch) => ch.start <= pos).at(-1)?.label || "";
}
export function location(b: Book) {
  return b.location
    ? [chapterAt(b, b.progress), "PDF 第 " + b.location.page + " 页"]
        .filter(Boolean)
        .join(" · ")
    : "尚未标记";
}

// Stream work status while keeping all answer text behind the backend citation check.
export async function generate<T>(
  path: string,
  body: object,
  signal: AbortSignal,
  onStatus: (message: string) => void,
): Promise<T> {
  const res = await fetch(path, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/x-ndjson",
    },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok || !res.body) {
    const error = await res.json().catch(() => ({}));
    throw new Error(
      typeof error.detail === "string" ? error.detail : "请求未完成，请重试。",
    );
  }
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) throw new Error("连接中断，问题已保留。");
      buffer += value;
      let end = buffer.indexOf("\n");
      while (end >= 0) {
        const line = buffer.slice(0, end).trim();
        buffer = buffer.slice(end + 1);
        if (line) {
          const event = JSON.parse(line);
          if (event.error) throw new Error(event.error);
          if ("result" in event) return event.result as T;
          if (event.status) onStatus(event.status);
        }
        end = buffer.indexOf("\n");
      }
    }
  } finally {
    reader.releaseLock();
  }
}
