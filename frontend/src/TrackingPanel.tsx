import { useEffect, useRef, useState } from "react";
import { Icon } from "./Icons";
import { ReadingPanel } from "./ReadingPanel";
import { api, type Book, type Answer, type Row, location } from "./types";

type Card = {
  id: string;
  question: string;
  checked: number;
  state: "ready" | "updating" | "failed";
  versions: { pos: number; page: number | null; claims: Answer["claims"] }[];
  current_claims: Answer["claims"];
};

function groupUpdates(versions: Card["versions"], initial: number, chapters: Book["chapters"]) {
  const groups = new Map<string, { title: string; versions: Card["versions"] }>();
  for (const version of versions) {
    const chapter = chapters.find((ch) => ch.start <= version.pos && version.pos <= ch.end);
    const key = version.pos === initial ? "initial" : chapter ? `chapter-${chapter.start}` : `page-${version.page}`;
    const title = key === "initial" ? "开始追踪" : chapter?.label ?? (version.page ? `PDF 第 ${version.page} 页` : "原文未标页码");
    if (!groups.has(key)) groups.set(key, { title, versions: [] });
    groups.get(key)!.versions.push(version);
  }
  return groups;
}

export type CardHistory = { cardId: string; position: number | null; pageFilter: string };

export function TrackingPanel({
  book,
  active,
  select,
  close,
  source,
  historyView,
  setHistoryView,
}: {
  book: Book;
  active: string | null;
  select: (id: string | null) => void;
  close: () => void;
  source: (row: Row) => void;
  historyView: CardHistory | null;
  setHistoryView: (view: CardHistory | null) => void;
}) {
  const [cards, setCards] = useState<Card[]>([]);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [openGroups, setOpenGroups] = useState<Set<string>>(new Set());
  const body = useRef<HTMLDivElement>(null);
  const openEntry = useRef<HTMLDivElement>(null);
  const path = "/api/books/" + book.id + "/cards";
  useEffect(() => {
    let current = true;
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      try {
        const result = await api<Card[]>(path + "?rev=" + book.rev);
        if (!current) return;
        setCards(result);
        setError("");
        setReady(true);
        if (result.some((c) => c.state === "updating"))
          timer = setTimeout(refresh, 1500);
      } catch (e) {
        if (current) {
          setError((e as Error).message);
          setReady(true);
        }
      }
    };
    void refresh();
    return () => {
      current = false;
      clearTimeout(timer);
    };
  }, [path, book.rev, book.card_topics, attempt, active]);
  const card = cards.find((c) => c.id === active);
  const history = historyView?.cardId === card?.id ? historyView : null;
  const pageFilter = history?.pageFilter ?? "";
  const openedPosition = history?.position;
  const versions = history && card
    ? card.versions.filter((v) => !pageFilter || v.page === Number(pageFilter)).reverse()
    : [];
  const groups = groupUpdates(versions, card?.versions[0]?.pos ?? -1, book.chapters);
  const openedVisible = versions.some((v) => v.pos === openedPosition);
  useEffect(() => {
    if (openedVisible) openEntry.current?.scrollIntoView({ block: "nearest" });
  }, [openedVisible, openedPosition]);
  const linked =
    book.card_topics.find((t) => t.id === active)?.members.length ?? 0;
  const status = (c: Card) =>
    c.state === "ready"
      ? (c.checked === book.progress ? "已检查至" + location(book) + " · " : "已检查至") +
        "第 " + c.checked + " 句"
      : c.state === "updating"
        ? "正在检查新增已读原文…"
        : "更新未完成，已保留上次结果";
  async function retry(id: string) {
    try {
      await api(path + "/" + id + "/retry", "POST", { rev: book.rev });
      setAttempt((n) => n + 1);
    } catch (e) {
      setError((e as Error).message);
    }
  }
  function evidence(claims: Answer["claims"]) {
    return claims.map((claim, i) => (
      <div key={i}>
        <p>{claim.text}</p>
        {claim.refs.map((r) => (
          <button key={r.pos} className="cardSource" onClick={() => source(r)}>
            <Icon name="quote" size={14} />
            <span>{r.text}</span>
            <span>第 {r.page} 页 ›</span>
          </button>
        ))}
      </div>
    ));
  }
  return (
    <ReadingPanel title="追踪卡" close={close} kind="trackingPanel">
      <div className="drawerHead">
        <div>
          <span className="eyebrow">{book.title}</span>
          <h2>追踪卡</h2>
        </div>
        <button className="icon" aria-label="关闭追踪卡" onClick={close}>
          <Icon name="close" />
        </button>
      </div>
      <div className="trackingBody" ref={body}>
        {error && (
          <p className="error" role="alert">
            {error}{" "}
            <button className="plain" onClick={() => setAttempt((n) => n + 1)}>
              重新加载
            </button>
          </p>
        )}
        {!ready && <p role="status">正在打开追踪卡…</p>}
        {ready && !error && !cards.length && (
          <p>
            在对话的回答下点「持续追踪」。读到新线索后，可以在这里看到变化和原文。
          </p>
        )}
        {card ? (
          <>
            <button className="plain" onClick={() => select(null)}>
              <Icon name="back" size={14} />
              本书的追踪卡
            </button>
            <h3>{card.question}</h3>
            <p className="trackStatus" role="status">
              {linked > 1 && <>已归并 {linked} 次提问 · </>}
              {status(card)}
            </p>
            {card.state === "failed" && (
              <button className="outline" onClick={() => void retry(card.id)}>
                重试更新
              </button>
            )}
            <nav className="cardViews" aria-label="追踪卡内容">
              <button type="button" aria-pressed={!history} onClick={() => {
                setHistoryView(null);
                body.current?.scrollTo({ top: 0 });
              }}>当前结论</button>
              <button type="button" aria-pressed={Boolean(history)} onClick={() => {
                if (history) return;
                setHistoryView({ cardId: card.id, position: null, pageFilter: "" });
                body.current?.scrollTo({ top: 0 });
              }}>
                更新记录 <span>{Math.max(0, card.versions.length - 1)}</span>
              </button>
            </nav>
            {history ? (
              <section aria-label="更新记录" className="cardHistory">
                {card.versions.length > 1 ? (
                  <div className="cardHistoryFilter">
                    <input type="search" inputMode="numeric" placeholder="按 PDF 页码查找"
                      aria-label="按 PDF 页码查找更新记录" value={pageFilter}
                      onChange={(e) => setHistoryView({
                        ...history, position: null, pageFilter: e.target.value.replace(/\D/g, ""),
                      })} />
                    {pageFilter && (
                      <button type="button" className="plain" onClick={() => setHistoryView({
                        ...history, position: null, pageFilter: "",
                      })}>清除</button>
                    )}
                  </div>
                ) : <p className="trackStatus">开始追踪后暂无新增线索。</p>}
                {pageFilter && (
                  <p className="trackStatus" role="status">
                    {versions.length ? `第 ${Number(pageFilter)} 页有 ${versions.length} 条记录` : "这一页没有更新记录。"}
                  </p>
                )}
                {[...groups].map(([key, group]) => {
                  const all = group.versions.flatMap((version) => version.claims);
                  const revised = all.find((claim) => claim.replaces?.length);
                  const added = all.find((claim) => !claim.replaces?.length);
                  const containsSelected = group.versions.some((v) => v.pos === openedPosition);
                  const expandedGroup = openGroups.has(key) || containsSelected || Boolean(pageFilter);
                  const groupId = `card-${card.id}-${key}`;
                  const pages = group.versions.flatMap((v) => v.page === null ? [] : [v.page]);
                  const firstPage = Math.min(...pages);
                  const lastPage = Math.max(...pages);
                  return (
                    <section className="cardUpdateGroup" key={key}>
                      <header>
                        <div>
                          <strong>{group.title}</strong>
                          <small>{pages.length > 0 && `PDF 第 ${firstPage}${firstPage !== lastPage ? `–${lastPage}` : ""} 页 · `}{group.versions.length} 条记录</small>
                        </div>
                        {!pageFilter && <button type="button" className="cardHistoryLocate" aria-expanded={expandedGroup}
                          aria-controls={groupId} onClick={() => {
                            if (containsSelected) setHistoryView({ ...history, position: null });
                            setOpenGroups((previous) => {
                              const next = new Set(previous);
                              if (expandedGroup) next.delete(key);
                              else next.add(key);
                              return next;
                            });
                          }}>{expandedGroup ? "收起记录" : "查看记录"}</button>}
                      </header>
                      <div className="cardUpdateSummary">
                        {revised && <p><strong>修订判断</strong> {revised.text}</p>}
                        {added && <p><strong>{key === "initial" ? "当时的结论" : "新增线索"}</strong> {added.text}</p>}
                      </div>
                      <ol id={groupId} className="cardHistoryList" hidden={!expandedGroup}>
                        {expandedGroup && group.versions.map((v) => {
                          const initial = v === card.versions[0];
                          const latest = v === card.versions.at(-1) && !initial;
                          const expanded = v.pos === openedPosition;
                          const positionLabel = v.page
                            ? `PDF 第 ${v.page} 页 · 第 ${v.pos} 句`
                            : v.pos ? `第 ${v.pos} 句` : "尚未标记";
                          const reference = v.claims.flatMap((claim) => claim.refs).find((ref) => ref.pos === v.pos);
                          const detailsId = `card-${card.id}-version-${v.pos}`;
                          return (
                            <li className="cardHistoryEntry" data-latest={latest} key={v.pos}>
                              <div className="cardHistoryHead" ref={expanded ? openEntry : null}>
                                <button type="button" className="cardHistoryToggle" aria-expanded={expanded}
                                  aria-controls={detailsId} onClick={() => {
                                    setHistoryView({ ...history, position: expanded ? null : v.pos });
                                  }}>
                                  <Icon name="chevron" size={16} />
                                  <span>
                                    <strong>{initial ? "开始追踪" : latest ? "最近一次更新" : "更新"}</strong>
                                    <span className="cardHistoryPosition">{positionLabel}</span>
                                  </span>
                                </button>
                                {reference && (
                                  <button type="button" className="cardHistoryLocate" aria-label={`定位原文 · ${positionLabel}`}
                                    onClick={() => {
                                      setHistoryView({ ...history, position: v.pos });
                                      source(reference);
                                    }}>定位原文</button>
                                )}
                              </div>
                              <div id={detailsId} className="cardHistoryDetails" hidden={!expanded}>
                                {expanded && (v.claims.length
                                  ? evidence(v.claims)
                                  : <p>当时没有足够的已读证据。</p>)}
                              </div>
                            </li>
                          );
                        })}
                      </ol>
                    </section>
                  );
                })}
              </section>
            ) : (
              <section aria-label="当前结论" className="cardVersion">
                {card.current_claims.length ? evidence(card.current_claims) : (
                  <p>已读原文还不足以回答这个问题。</p>
                )}
              </section>
            )}
          </>
        ) : (
          cards.map((c) => (
            <button
              key={c.id}
              className="trackListItem"
              onClick={() => select(c.id)}
            >
              <strong>{c.question}</strong>
              <small>{status(c)}</small>
              <Icon name="chevron" size={15} />
            </button>
          ))
        )}
      </div>
    </ReadingPanel>
  );
}
