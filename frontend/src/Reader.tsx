import { useEffect, useState } from "react";
import { api, Book, Row, Chapter, location } from "./types";
import { ReadingPanel } from "./ReadingPanel";
import { Icon } from "./Icons";

type Props = {
  b: Book;
  purpose: "adjust" | "quote";
  initialQuote?: Row | null;
  close: () => void;
  mark: (pos: number) => Promise<void>;
  quote: (r: Row) => void;
};
export function Reader({
  b,
  close,
  mark,
  quote,
  purpose,
  initialQuote,
}: Props) {
  const quoting = purpose === "quote";
  const heading = quoting ? "引用已读原文" : "调整阅读位置";
  const pageLimit = quoting ? (b.location?.page ?? 1) : b.pages;
  const initial =
    initialQuote?.page || b.location?.page || (!quoting && b.sample_preview_page) || 1;
  const [page, setPage] = useState(initial),
    [entry, setEntry] = useState(String(initial)),
    [rows, setRows] = useState<Row[]>([]);
  const [chosen, choose] = useState<number | null>(
      initialQuote?.pos ?? b.location?.pos ?? null,
    ),
    [chapter, setChapter] = useState<Chapter | null>(null);
  const [mode, setMode] = useState<"text" | "image">("text"),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [loading, load] = useState(false);
  const [finding, find] = useState(false),
    [term, query] = useState(""),
    [hits, results] = useState<Row[] | null>(null);
  const path = "/api/books/" + b.id + "/pages/" + page;
  const source = quoting ? path + "?read_only=true" : path;
  useEffect(() => {
    let valid = true;
    load(true);
    setError("");
    setRows([]);
    api<{ rows: Row[] }>(source)
      .then((v) => {
        if (valid) setRows(v.rows.map((r) => ({ ...r, page })));
      })
      .catch((e) => {
        if (valid) setError(e.message);
      })
      .finally(() => {
        if (valid) load(false);
      });
    return () => {
      valid = false;
    };
  }, [source, page]);
  useEffect(() => {
    if (initialQuote && rows.length)
      document
        .getElementById("sentence-" + initialQuote.pos)
        ?.scrollIntoView({ block: "center" });
  }, [rows, initialQuote]);
  function go(n: number) {
    if (!Number.isInteger(n) || n < 1 || n > pageLimit) {
      setError("请输入 1 至 " + pageLimit + " 的页码。");
      return;
    }
    setPage(n);
    setEntry(String(n));
    choose(null);
    setChapter(null);
    setError("");
  }
  const selected = rows.find((r) => r.pos === chosen);
  async function save(pos: number) {
    setBusy(true);
    setError("");
    try {
      await mark(pos);
      close();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <ReadingPanel title={heading} close={close}>
      <div className="drawerHead">
        <div>
          <span className="eyebrow">{b.title}</span>
          <h2>{heading}</h2>
        </div>
        <button className="icon" onClick={close} aria-label="关闭">
          <Icon name="close" />
        </button>
      </div>
      {initialQuote && (
        <button className="plain cardBack" onClick={close}>
          <Icon name="back" size={14} />
          返回追踪卡
        </button>
      )}
      <div className="current">
        <Icon name="bookmark" /> 当前已读 · {location(b)}
        {!quoting && b.previous !== b.progress && (
          <button onClick={() => save(b.previous)} disabled={busy}>
            撤销上次标记
          </button>
        )}
      </div>
      {!quoting && b.chapters.length > 0 && (
        <label className="chapSelect">
          按章节标记
          <select
            value={chapter?.start ?? ""}
            onChange={(e) => {
              const ch = b.chapters.find(
                (x) => x.start === Number(e.target.value),
              );
              if (ch) {
                go(ch.page);
                setChapter(ch);
                choose(ch.end);
              }
            }}
          >
            <option value="">选择已读完的章节</option>
            {b.chapters.map((ch) => (
              <option key={ch.start} value={ch.start}>
                {ch.title}
              </option>
            ))}
          </select>
        </label>
      )}
      {b.location && (
        <details
          className="readSearch"
          onToggle={(e) => find(e.currentTarget.open)}
        >
          <summary>查找已读原文</summary>
          {finding && (
            <form
              onSubmit={async (e) => {
                e.preventDefault();
                if (!term.trim()) return;
                try {
                  results(
                    await api<Row[]>(
                      "/api/books/" +
                        b.id +
                        "/search?q=" +
                        encodeURIComponent(term.trim()),
                    ),
                  );
                } catch (e) {
                  setError((e as Error).message);
                }
              }}
            >
              <input
                value={term}
                aria-label="在已读内容中搜索"
                placeholder="输入已读原文中的词句"
                maxLength={100}
                onChange={(e) => {
                  query(e.target.value);
                  results(null);
                }}
              />
              <button>查找</button>
            </form>
          )}
          {finding && hits && (
            <div>
              {hits.length
                ? hits.map((r) => (
                    <button
                      type="button"
                      key={r.pos}
                      onClick={() => {
                        go(r.page);
                        choose(r.pos);
                        results(null);
                      }}
                    >
                      第 {r.page} 页 · {r.text}
                    </button>
                  ))
                : "已读内容中未找到匹配。"}
            </div>
          )}
        </details>
      )}
      <div className="pagebar">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            go(Number(entry));
          }}
        >
          <label>
            PDF 第{" "}
            <input
              type="number"
              aria-label="PDF 页码"
              min={1}
              max={pageLimit}
              value={entry}
              onChange={(e) => setEntry(e.target.value)}
            />{" "}
            页
          </label>
          <button>跳转</button>
        </form>
        <div>
          <button
            className="icon"
            disabled={page === 1}
            onClick={() => go(page - 1)}
            aria-label="上一页"
          >
            <Icon name="back" />
          </button>
          <button
            className="icon"
            disabled={page === pageLimit}
            onClick={() => go(page + 1)}
            aria-label="下一页"
          >
            <Icon name="chevron" />
          </button>
        </div>
      </div>
      <div className="modebar">
        <span>{quoting ? "选择想讨论的句子" : "点击你读完的最后一句"}</span>
        {!quoting && (
          <button onClick={() => setMode(mode === "text" ? "image" : "text")}>
            {mode === "text" ? "看 PDF 原页" : "看句子列表"}
          </button>
        )}
      </div>
      <div className="pagescroll" key={page}>
        {loading && <p className="hint">正在读取第 {page} 页…</p>}
        {!loading && mode === "text" && !rows.length && (
          <p className="hint">
            {quoting
              ? b.progress
                ? "本页没有已读正文。"
                : "还没有标记已读内容。请先用顶部书签设置阅读位置。"
              : "本页没有可标记的正文，可能是封面、目录或扫描页。可浏览原页或选择其他页。"}
          </p>
        )}
        {mode === "text" ? (
          rows.map((r) => (
            <button
              key={r.pos}
              id={"sentence-" + r.pos}
              className={"sentence " + (r.pos === chosen ? "pick" : "")}
              aria-pressed={r.pos === chosen}
              onClick={() => {
                choose(r.pos);
                setChapter(null);
              }}
            >
              {r.text}
            </button>
          ))
        ) : (
          <div className="facsimile">
            <img src={path + "/image"} alt={"PDF 第 " + page + " 页"} />
            {rows.map((r) =>
              r.boxes?.map(([x, y, x1, y1], i) => (
                <button
                  aria-label={r.text}
                  title={r.text}
                  key={r.pos + "-" + i}
                  className={"box " + (r.pos === chosen ? "picked" : "")}
                  style={{
                    left: x * 100 + "%",
                    top: y * 100 + "%",
                    width: (x1 - x) * 100 + "%",
                    height: (y1 - y) * 100 + "%",
                  }}
                  onClick={() => {
                    choose(r.pos);
                    setChapter(null);
                  }}
                />
              )),
            )}
          </div>
        )}
      </div>
      {quoting ? (
        <div className="drawerFoot">
          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
          {selected && <p className="excerpt">{selected.text}</p>}
          <button
            className="primary"
            disabled={!selected}
            onClick={() => selected && quote(selected)}
          >
            问这句话 <Icon name="arrow" size={15} />
          </button>
        </div>
      ) : (
        <div className="drawerFoot">
          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
          <small>
            {chapter
              ? "已选整章"
              : selected
                ? "已选末句"
                : "浏览原文不会推进阅读进度"}
          </small>
          {(chapter || selected) && (
            <p className="excerpt">
              {chapter ? "已读完" + chapter.label : selected?.text}
            </p>
          )}
          <div className="footActions">
            <button
              className="primary"
              disabled={chosen === null || busy}
              onClick={() => chosen !== null && save(chosen)}
            >
              {busy ? "保存中…" : chapter ? "标记整章已读" : "已读到这里"}{" "}
              <Icon name="check" size={15} />
            </button>
            {b.progress > 0 && !selected && !chapter && (
              <button className="plain" onClick={() => save(0)}>
                设为尚未开始
              </button>
            )}
          </div>
        </div>
      )}
    </ReadingPanel>
  );
}
