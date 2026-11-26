import { useState } from "react";
import { Icon } from "./Icons";
import { recall, remember } from "./chatState";
import type { Book, Library } from "./types";

export type Section = "books" | "cards" | "threads";
type Item = { id: string; title: string };
type Props = {
  book: Book;
  books: Library["books"];
  thread: string;
  card: string | null;
  openBook: (id: string) => void;
  openCard: (id: string) => void;
  openThread: (id: string) => void;
  newChat: () => void;
  demo: boolean;
  remove: (kind: Section, id: string) => Promise<void>;
};
const sections = [
  {
    id: "books",
    label: "我的书架",
    icon: "shelf",
    item: "书籍",
    row: "book",
    empty: "书架还是空的",
  },
  {
    id: "cards",
    label: "本书追踪卡",
    icon: "bookmark",
    item: "追踪卡",
    row: "bookmark",
    empty: "还没有追踪的问题",
  },
  {
    id: "threads",
    label: "本书对话",
    icon: "history",
    item: "对话",
    row: "chat",
    empty: "还没有对话",
  },
] as const;
const effects = {
  books: "这本书的书签、对话和追踪卡会一起删除。",
  cards: "所有同题提问将停止追踪，原对话保留。",
  threads: "已保存的追踪卡会保留。",
};
export function LibrarySections(p: Props) {
  const [fold, setFold] = useState<Partial<Record<Section, boolean>>>(
    () => recall("sidebar-fold") ?? {},
  );
  const [pending, setPending] = useState<{ kind: Section; item: Item } | null>(
    null,
  );
  const [removing, setRemoving] = useState(false);
  const [failure, setFailure] = useState("");
  async function confirm() {
    if (!pending || removing) return;
    setRemoving(true);
    try {
      await p.remove(pending.kind, pending.item.id);
      setPending(null);
    } catch (e) {
      setFailure((e as Error).message);
    } finally {
      setRemoving(false);
    }
  }
  const lists = {
    books: p.books,
    cards: p.book.card_topics,
    threads: p.book.threads,
  };
  const select = {
    books: p.openBook,
    cards: p.openCard,
    threads: p.openThread,
  };
  const current = { books: p.book.id, cards: p.card, threads: p.thread };
  return (
    <nav className="libraryNav" aria-label="阅读导航">
      {sections.map((s) => (
        <section key={s.id} className="sideSection">
          <div className="sectionTitle">
            <button
              className="sectionToggle"
              aria-expanded={!fold[s.id]}
              aria-controls={"nav-" + s.id}
              onClick={() => {
                const next = { ...fold, [s.id]: !fold[s.id] };
                setFold(next);
                remember("sidebar-fold", next);
              }}
            >
              <Icon name={s.icon} size={15} />
              <span>{s.label}</span>
              <small>{lists[s.id].length}</small>
              <span className={fold[s.id] ? "foldArrow" : "foldArrow opened"}>
                <Icon name="chevron" size={12} />
              </span>
            </button>
            {s.id === "threads" && (
              <button
                className="icon"
                aria-label="新建对话"
                onClick={() => {
                  const next = { ...fold, threads: false };
                  setFold(next);
                  remember("sidebar-fold", next);
                  p.newChat();
                }}
              >
                <Icon name="plus" size={15} />
              </button>
            )}
          </div>
          <div id={"nav-" + s.id} hidden={fold[s.id]}>
            {lists[s.id].length === 0 && <p className="navEmpty">{s.empty}</p>}
            {lists[s.id].map((item) => (
              <div key={item.id}>
                <div
                  className={
                    "sideItem" + (current[s.id] === item.id ? " isCurrent" : "")
                  }
                >
                  <button
                    className="navrow"
                    title={item.title}
                    onClick={() => select[s.id](item.id)}
                  >
                    <Icon name={s.row} size={15} />
                    <span>{item.title}</span>
                  </button>
                  {(!p.demo || s.id !== "books") && <button
                    className="removeItem"
                    title={"删除" + s.item}
                    aria-label={"删除" + s.item + "「" + item.title + "」"}
                    onClick={() => {
                      setPending({ kind: s.id, item });
                      setFailure("");
                    }}
                  >
                    <Icon name="trash" size={14} />
                  </button>}
                </div>
                {pending?.kind === s.id && pending.item.id === item.id && (
                  <div
                    className="deletePrompt"
                    role="group"
                    aria-label={"确认删除" + s.item}
                  >
                    <strong>删除「{item.title}」？</strong>
                    <p>{effects[s.id]}</p>
                    {failure && <p role="alert">{failure}</p>}
                    <div>
                      <button
                        className="outline"
                        disabled={removing}
                        onClick={() => setPending(null)}
                      >
                        取消
                      </button>
                      <button
                        className="danger"
                        disabled={removing}
                        onClick={() => void confirm()}
                      >
                        {removing ? "删除中…" : "确认删除"}
                      </button>
                    </div>
                  </div>
                )}
              </div>
            ))}
          </div>
        </section>
      ))}
    </nav>
  );
}
