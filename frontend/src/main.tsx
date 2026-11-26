import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { createRoot } from "react-dom/client";
import { AccountDialog } from "./AccountDialog";
import { Icon } from "./Icons";
import { ReadingPanel } from "./ReadingPanel";
import {
  loadDraft,
  position,
  remember,
  recall,
  restore,
  type Place,
} from "./chatState";
import { Reader } from "./Reader";
import { LibrarySections, type Section } from "./LibrarySections";
import { TrackingPanel, type CardHistory } from "./TrackingPanel";
import {
  api,
  ApiError,
  generate,
  Book,
  Row,
  Answer,
  Turn,
  Library,
  type ImportReport,
  location,
} from "./types";
import "./style.css";

type Evidence = { refs: Row[]; title: string; spoiler: boolean; turn: string };
type Panel = "bookmark" | "adjust" | "quote" | "evidence" | "cards";
type Intent =
  | { kind: "ask"; question: string; selected: number; id: string; continueFrom?: string }
  | { kind: "allow" | "decline" | "reveal"; turn: string; id: string };
type ActiveRequest = {
  intent: Intent;
  controller: AbortController;
  stopping: boolean;
};
const prompts = [
  "帮我回顾已读的情节",
  "梳理已出场的人物",
  "解释我选中的一句话",
];

function pageRanges(pages: number[]) {
  const groups: { first: number; last: number }[] = [];
  for (const page of pages) {
    const group = groups.at(-1);
    if (group && page === group.last + 1) group.last = page;
    else groups.push({ first: page, last: page });
  }
  return groups.map(({ first, last }) =>
    first === last ? String(first) : first + "–" + last
  ).join("、");
}

function AnswerView({
  a,
  inspect,
  turn,
  spoiler = false,
}: {
  a: Answer;
  turn: string;
  inspect: (e: Evidence) => void;
  spoiler?: boolean;
}) {
  const [directoryOpen, setDirectoryOpen] = useState(a.cast_start === 0);
  const [nameQuery, setNameQuery] = useState("");
  const [visibleNames, setVisibleNames] = useState(30);
  const query = nameQuery.trim().toLocaleLowerCase();
  const matches = directoryOpen && a.cast_start !== undefined
    ? a.claims.slice(a.cast_start).map((claim, index) => {
        const [label, ...detail] = claim.text.split(" · ");
        return { claim, number: index + 1, label, detail: detail.join(" · ") };
      }).filter(entry => entry.label.toLocaleLowerCase().includes(query))
    : [];
  const fallback: Record<string, string> = {
    off_topic: "我可以帮你讨论这本书的内容。",
    clarify: "你指的是哪位人物或哪句话？可以选中一段已读原文再问我。",
    unknown: "在本次可用的原文里，没有找到足够依据。",
    limited: "目前读到的内容不足以完整回答这个问题。",
  };
  function citation(cl: Answer["claims"][number], label: string) {
    return <button className="cite" aria-label={"查看原文依据 · " + (cl.name || label)}
      onClick={() => inspect({ refs: cl.refs, title: cl.name || "原文依据", spoiler, turn })}>
      <Icon name="quote" size={11} />{label}
    </button>;
  }
  const narrative = a.prose && a.claims.length > 0 && a.cast_start === undefined;
  const sharedCitations = narrative || (a.conversational && a.status === "answer"
    && a.cast_start === undefined && !a.tally && !spoiler);
  const answerRefs = sharedCitations
    ? [...new Map(a.claims.flatMap(cl => cl.refs).map(ref => [ref.pos, ref])).values()]
    : [];
  const paragraphs: string[] = [];
  if (narrative) {
    for (const claim of a.claims) {
      if (claim.paragraph_start || paragraphs.length === 0) paragraphs.push(claim.text);
      else paragraphs[paragraphs.length - 1] += claim.text;
    }
  }
  return (
    <div className="answer">
      {a.reader_note && a.status !== "answer" && <p>{a.reader_note}</p>}
      {narrative ? paragraphs.map((paragraph, index) => <p key={index}>{paragraph}</p>)
        : a.claims.slice(0, a.cast_start ?? a.claims.length).map((cl, i) => (
        <p key={i}>{cl.text}{!sharedCitations && <> {citation(cl, String(i + 1))}</>}</p>
      ))}
      {a.reader_note && a.status === "answer" && <p>{a.reader_note}</p>}
      {answerRefs.length > 0 && (
        <div>{citation({ text: "", refs: answerRefs }, "查看原文依据")}</div>
      )}
      {a.cast_start !== undefined && (
        <details className="castDirectory" open={directoryOpen}
          onToggle={event => setDirectoryOpen(event.currentTarget.open)}>
          <summary>查看人物名单（{a.claims.length - a.cast_start} 条）</summary>
          {directoryOpen && <>
            <input aria-label="查找人物或别名" placeholder="查找人物或别名" value={nameQuery}
              onChange={event => { setNameQuery(event.target.value); setVisibleNames(30); }} type="search" />
            <ol>{matches.slice(0, visibleNames).map(entry => (
              <li value={entry.number} key={entry.number}>
                <strong>{entry.label}</strong>　{entry.detail} {citation(entry.claim, "原文")}
              </li>
            ))}</ol>
            {matches.length > visibleNames && (
              <button className="plain" onClick={() => setVisibleNames(count => count + 30)}>再显示 {Math.min(30, matches.length - visibleNames)} 条</button>
            )}
            {!matches.length && <p>这份名单里没有找到这个称呼。</p>}
          </>}
        </details>
      )}
      {a.background && (
        <p>
          <strong>补充背景</strong>　{a.background}
        </p>
      )}
      {!a.reader_note && a.status === "unknown" && a.claims.length > 0 && (
        <p className="hint">其余部分，目前没有足够的原文依据。</p>
      )}
      {!a.reader_note && !a.claims.length && !a.background && (
        <p>{fallback[a.status] || "暂未找到足够的原文依据。"}</p>
      )}
    </div>
  );
}

function Workspace() {
  const [accountOpen, openAccount] = useState(false);
  const [username, setUsername] = useState<string | null>(null);
  const [demo, setDemo] = useState(false);
  const [invitationRequired, setInvitationRequired] = useState(false);
  const [emptyShelf, setEmptyShelf] = useState(false);
  const [trackingTurn, setTrackingTurn] = useState("");
  const [phrasing, setPhrasing] = useState("");
  const [books, setBooks] = useState<Library["books"]>([]),
    [b, setBook] = useState<Book | null>(null),
    [tid, thread] = useState(""),
    [turns, setTurns] = useState<Turn[]>([]);
  const [draft, write] = useState(""),
    [selected, select] = useState<Row | null>(null),
    [panel, setPanel] = useState<Panel | null>(null),
    [evidence, inspect] = useState<Evidence | null>(null);
  const [menu, sidebar] = useState(false),
    [upload, importing] = useState(false),
    [busy, working] = useState(""),
    [err, error] = useState(""),
    [show, setShow] = useState<Record<string, Answer>>({});
  const [uploadError, setUploadError] = useState(""),
    [importingFile, setImportingFile] = useState(false),
    [pendingQ, setPendingQ] = useState("");
  const [importReport, setImportReport] = useState<ImportReport | null>(null);
  const live = useRef(0),
    bottom = useRef<HTMLDivElement>(null),
    editor = useRef<HTMLTextAreaElement>(null),
    channel = useRef<BroadcastChannel | null>(null),
    file = useRef<HTMLInputElement>(null);
  const [activeCard, setActiveCard] = useState<string | null>(null);
  const [cardHistory, setCardHistory] = useState<CardHistory | null>(null);
  const [cardQuote, setCardQuote] = useState<Row | null>(null);
  const flow = useRef<HTMLDivElement>(null);
  const currentKey = useRef("");
  const fetchRequest = useRef<ActiveRequest | null>(null);
  const [retry, setRetry] = useState<Intent | null>(null);
  const [notice, setNotice] = useState("");
  const panelPlace = useRef<Place | null>(null),
    opener = useRef<HTMLElement | null>(null);
  const scrollAfter = useRef<{ place: Place | null } | null>(null);
  const [loadingThread, setLoadingThread] = useState(true);
  const shouldFollow = useRef(false);
  const key = b && tid ? b.id + ":" + tid : "";
  const [mobile, setMobile] = useState(
    () => matchMedia("(max-width: 900px)").matches,
  );
  useEffect(() => {
    const m = matchMedia("(max-width: 900px)");
    const update = () => setMobile(m.matches);
    m.addEventListener("change", update);
    return () => m.removeEventListener("change", update);
  }, []);
  useEffect(() => {
    if (key && b)
      remember(key + ":draft", {
        text: draft,
        quote: selected,
        at: b.progress,
      });
  }, [key, b?.progress, draft, selected]);
  function keepPlace() {
    if (currentKey.current && !loadingThread)
      remember(
        currentKey.current + ":scroll",
        panelPlace.current ?? position(flow.current),
      );
  }
  function openPanel(next: Panel) {
    if (!panel) {
      panelPlace.current = position(flow.current);
      opener.current =
        document.activeElement instanceof HTMLElement
          ? document.activeElement
          : null;
    }
    setPanel(next);
  }
  const closePanel = useCallback(() => {
    setCardQuote(null);
    setPanel(null);
  }, []);
  const backToCards = useCallback(() => {
    setCardQuote(null);
    setPanel("cards");
  }, []);
  async function trackTurn(turn: string) {
    if (!b) return;
    const generation = live.current;
    try {
      let id = b.card_topics.find((c) => c.members.includes(turn))?.id;
      if (!id) {
        setTrackingTurn(turn);
        const result = await api<
          Pick<Book, "tracked" | "card_topics" | "topics_pending"> & {
            id: string;
          }
        >("/api/books/" + b.id + "/turns/" + turn + "/track", "POST", {
          rev: b.rev,
        });
        if (generation !== live.current) return;
        const { id: matched, ...topics } = result;
        id = matched;
        setBook((prev) => prev && { ...prev, ...topics });
      }
      setActiveCard(id);
      openPanel("cards");
    } catch (e) {
      if (generation === live.current) error((e as Error).message);
    } finally {
      if (generation === live.current) setTrackingTurn("");
    }
  }
  async function rephrase(turn: string) {
    if (!b || phrasing) return;
    const generation = live.current;
    setPhrasing(turn);
    try {
      const updated = await api<Turn>(
        "/api/books/" + b.id + "/turns/" + turn + "/rephrase",
        "POST",
        { rev: b.rev },
      );
      if (generation === live.current)
        setTurns((items) => items.map((t) => (t.id === turn ? updated : t)));
    } catch (e) {
      if (generation === live.current) error((e as Error).message);
    } finally {
      if (generation === live.current) setPhrasing("");
    }
  }
  function openEvidence(e: Evidence) {
    inspect(e);
    openPanel("evidence");
  }
  useLayoutEffect(() => {
    if (!panel && panelPlace.current && !loadingThread) {
      restore(flow.current, panelPlace.current);
      panelPlace.current = null;
      opener.current?.focus({ preventScroll: true });
      opener.current = null;
    }
  }, [panel, loadingThread]);
  useLayoutEffect(() => {
    if (!loadingThread && scrollAfter.current) {
      restore(flow.current, scrollAfter.current.place);
      scrollAfter.current = null;
    } else if (shouldFollow.current && !panel && !loadingThread) {
      bottom.current?.scrollIntoView({ block: "nearest" });
    }
    shouldFollow.current = false;
  }, [turns, pendingQ, loadingThread]);
  function cancelOnLeave() {
    const op = fetchRequest.current;
    if (op) {
      fetchRequest.current = null;
      op.controller.abort();
      void api("/api/requests/" + op.intent.id + "/stop", "POST").catch(
        () => {},
      );
    }
  }
  function reset() {
    keepPlace();
    cancelOnLeave();
    live.current++;
    panelPlace.current = null;
    opener.current = null;
    inspect(null);
    setActiveCard(null);
    setPhrasing("");
    setTrackingTurn("");
    setCardQuote(null);
    setPanel(null);
    setShow({});
    working("");
    setPendingQ("");
    error("");
    setRetry(null);
    setNotice("");
  }
  async function view(book: Book, threadId = book.threads[0]?.id ?? "") {
    reset();
    const v = live.current,
      next = book.id + ":" + threadId;
    currentKey.current = next;
    const saved = loadDraft(next, book.progress);
    write(saved.text);
    select(saved.quote);
    scrollAfter.current = { place: recall<Place>(next + ":scroll") };
    setLoadingThread(true);
    setTurns([]);
    setBook(book);
    setEmptyShelf(false);
    thread(threadId);
    sidebar(false);
    if (!threadId) {
      setLoadingThread(false);
      return;
    }
    try {
      const ts = await api<Turn[]>(
        "/api/books/" + book.id + "/threads/" + threadId,
      );
      if (v === live.current) setTurns(ts);
    } catch (e) {
      if (v === live.current) error((e as Error).message);
    } finally {
      if (v === live.current) setLoadingThread(false);
    }
  }
  async function refresh(currentThread?: string, currentBook?: string) {
    try {
      const lib = await api<Library>("/api/library");
      setBooks(lib.books);
      setUsername(lib.username);
      setInvitationRequired(lib.invitation_required);
      setDemo(lib.demo);
      if (lib.book) await view(lib.book, lib.book.id === currentBook && lib.book.threads.some(t => t.id === currentThread) ? currentThread : undefined);
      else {
        reset();
        currentKey.current = "";
        setBook(null);
        setTurns([]);
        thread("");
        write("");
        select(null);
        setEmptyShelf(true);
        setLoadingThread(false);
      }
    } catch (e) {
      error((e as Error).message);
    }
  }
  useEffect(() => {
    void refresh();
    const bc = new BroadcastChannel("xudu-position");
    channel.current = bc;
    bc.onmessage = () => {
      reset();
      setTurns([]);
      setLoadingThread(true);
      void refresh();
    };
    return () => bc.close();
  }, []);
  useEffect(() => {
    if (!username) return;
    let disposed = false;
    async function sync() {
      if (disposed || document.hidden) return;
      try {
        const remote = await api<{ username: string | null; book: string | null; rev: number | null }>("/api/session");
        if (!disposed && (remote.username !== username || remote.book !== (b?.id ?? null) || remote.rev !== (b?.rev ?? null))) {
          await refresh(tid, b?.id);
          if (!disposed && remote.username === username) setNotice("已同步最新阅读位置。");
        }
      } catch (cause) {
        if (!disposed && cause instanceof ApiError && cause.status === 401) await refresh();
      }
    }
    const timer = window.setInterval(() => void sync(), 7000);
    const focus = () => void sync();
    window.addEventListener("focus", focus);
    return () => {
      disposed = true;
      window.clearInterval(timer);
      window.removeEventListener("focus", focus);
    };
  }, [username, b?.id, b?.rev, tid]);
  useEffect(() => {
    const esc = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !e.defaultPrevented) {
        setPanel(null);
        sidebar(false);
        if (!importingFile) importing(false);
      }
    };
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [importingFile]);
  async function switchBook(id: string) {
    try {
      reset();
      const next = await api<Book>("/api/books/" + id + "/open", "POST");
      await view(next);
      channel.current?.postMessage("book");
    } catch (e) {
      error((e as Error).message);
    }
  }
  async function saveMark(pos: number) {
    if (!b) return;
    try {
      const next = await api<Book>("/api/books/" + b.id + "/progress", "PUT", {
        pos,
        rev: b.rev,
      });
      await view(next, tid);
      channel.current?.postMessage("progress");
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 409) {
        await refresh(tid, b.id);
        setNotice("另一处更新了书签。已显示最新位置，请重新选择并确认要标记的位置。");
      }
      throw cause;
    }
  }
  async function removeItem(kind: Section, id: string) {
    if (!b) return;
    if (kind === "books") {
      await api("/api/books/" + id, "DELETE");
      await refresh();
    } else {
      const next = await api<Book>(
        "/api/books/" + b.id + "/" + kind + "/" + id + "?rev=" + b.rev,
        "DELETE",
      );
      if (kind === "threads" && tid === id) await view(next);
      else {
        setBook(next);
        if (kind === "cards" && activeCard === id) {
          closePanel();
          setActiveCard(null);
        }
      }
    }
    channel.current?.postMessage("library");
  }
  useEffect(() => {
    if (!b?.topics_pending) return;
    let valid = true;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      if (!b) return;
      try {
        const topics = await api<
          Pick<Book, "card_topics" | "tracked" | "topics_pending">
        >("/api/books/" + b.id + "/topics?rev=" + b.rev);
        if (!valid) return;
        setBook((prev) =>
          prev && prev.id === b.id && prev.rev === b.rev
            ? { ...prev, ...topics }
            : prev,
        );
        if (!topics.topics_pending) return;
      } catch {
        if (!valid) return;
      }
      timer = setTimeout(poll, 2000);
    }
    timer = setTimeout(poll, 1200);
    return () => {
      valid = false;
      clearTimeout(timer);
    };
  }, [b?.id, b?.rev, b?.topics_pending]);
  async function newChat() {
    if (!b) return;
    try {
      const book = await api<Book>("/api/books/" + b.id + "/threads", "POST");
      await view(book);
    } catch (e) {
      error((e as Error).message);
    }
  }
  async function importFile(f: File) {
    if (importingFile) return;
    const form = new FormData();
    form.append("file", f);
    setImportingFile(true);
    setUploadError("");
    try {
      const book = await api<Book>("/api/books", "POST", form);
      const lib = await api<Library>("/api/library");
      setBooks(lib.books);
      await view(book);
      setImportReport(book.import_report ?? null);
      importing(!!book.import_report);
      if (!book.import_report) openPanel("adjust");
      channel.current?.postMessage("book");
    } catch (e) {
      setUploadError((e as Error).message);
    } finally {
      setImportingFile(false);
      if (file.current) file.current.value = "";
    }
  }
  async function perform(intent: Intent) {
    if (!b || fetchRequest.current || loadingThread) return;
    const op: ActiveRequest = {
      intent,
      controller: new AbortController(),
      stopping: false,
    };
    fetchRequest.current = op;
    const v = live.current;
    error("");
    setNotice("");
    setRetry(null);
    working(
      intent.kind === "ask" || intent.kind === "allow"
        ? "正在提交问题…"
        : "正在读取…",
    );
    const receive = (status: string) => {
      if (fetchRequest.current === op && !op.stopping) working(status);
    };
    try {
      if (intent.kind === "ask") {
        shouldFollow.current = true;
        setPendingQ(intent.question);
        let targetThread = tid;
        if (!targetThread) {
          const updated = await api<Book>(
            "/api/books/" + b.id + "/threads",
            "POST",
          );
          if (v !== live.current) return;
          targetThread = updated.threads[0].id;
          setBook(updated);
          thread(targetThread);
          currentKey.current = b.id + ":" + targetThread;
        }
        const t = await generate<Turn>(
          "/api/books/" + b.id + "/threads/" + targetThread + "/ask",
          {
            question: intent.question,
            rev: b.rev,
            selected: intent.selected,
            request_id: intent.id,
            continue_from: intent.continueFrom,
          },
          op.controller.signal,
          receive,
        );
        if (v !== live.current) return;
        shouldFollow.current =
          !!flow.current &&
          flow.current.scrollHeight -
            flow.current.scrollTop -
            flow.current.clientHeight <
            100;
        setTurns((old) => (old.some((x) => x.id === t.id) ? old : [...old, t]));
        write((old) => (old.trim() === intent.question ? "" : old));
        select((old) => (old?.pos === intent.selected ? null : old));
        setBook(
          (prev) =>
            prev && {
              ...prev,
              threads: prev.threads.map((th) =>
                th.id === targetThread && turns.length === 0
                  ? { id: th.id, title: intent.question }
                  : th,
              ),
            },
        );
      } else {
        const path = "/api/books/" + b.id + "/turns/" + intent.turn + "/choice";
        const body = { action: intent.kind, rev: b.rev, request_id: intent.id };
        const a =
          intent.kind === "allow"
            ? await generate<Answer>(path, body, op.controller.signal, receive)
            : await api<Answer>(path, "POST", body);
        if (v !== live.current) return;
        if (intent.kind !== "decline")
          setShow((old) => ({ ...old, [intent.turn]: a }));
        setTurns((old) =>
          old.map((t) =>
            t.id === intent.turn
              ? { ...t, pending: false, spoiler: intent.kind !== "decline" }
              : t,
          ),
        );
      }
    } catch (e) {
      if (v === live.current) {
        if (op.stopping) setNotice("已停止，本次请求可以重试。");
        else error((e as Error).message);
        setRetry(intent);
      }
    } finally {
      if (fetchRequest.current === op) {
        fetchRequest.current = null;
        working("");
        setPendingQ("");
      }
    }
  }
  function ask() {
    const question = draft.trim(),
      pos = selected?.pos ?? 0;
    if (!question) return;
    const intent =
      retry?.kind === "ask" &&
      !retry.continueFrom &&
      retry.question === question &&
      retry.selected === pos
        ? retry
        : {
            kind: "ask" as const,
            question,
            selected: pos,
            id: crypto.randomUUID(),
          };
    void perform(intent);
  }
  function decide(t: Turn, action: "allow" | "decline" | "reveal") {
    void perform({ kind: action, turn: t.id, id: crypto.randomUUID() });
  }
  async function stop() {
    const op = fetchRequest.current;
    if (!op || op.stopping) return;
    op.stopping = true;
    working("正在停止…");
    try {
      await api("/api/requests/" + op.intent.id + "/stop", "POST");
    } finally {
      op.controller.abort();
    }
  }
  function quote(row: Row) {
    select(row);
    write((old) => old || "这句话是什么意思？");
    setRetry(null);
    setNotice("");
    error("");
    opener.current = editor.current;
    closePanel();
  }
  function toggleImport() {
    sidebar(false);
    setUploadError("");
    setImportReport(null);
    importing(true);
  }
  const accountButton = !demo && (
    <button className="plain accountButton" onClick={() => {sidebar(false); openAccount(true);}}>
      {username ? "账号设置" : "登录 / 注册"}
    </button>
  );
  const accountDialog = accountOpen && (
    <AccountDialog username={username} invitationRequired={invitationRequired} close={() => openAccount(false)}
      refresh={async () => { await refresh(); channel.current?.postMessage("account"); }} />
  );
  if (!b && emptyShelf)
    return (
      <main className="boot">
        <span className="logo">
          <Icon name="shelf" />
        </span>
        <h1>我的书架</h1>
        <p>书架还是空的，导入一本 PDF 开始阅读。</p>
        <button
          className="primary"
          disabled={importingFile}
          onClick={() => file.current?.click()}
        >
          {importingFile ? "正在导入…" : "导入书籍"}
        </button>
        <input
          hidden
          type="file"
          ref={file}
          accept="application/pdf,.pdf"
          onChange={(e) => {
            if (e.target.files?.[0]) void importFile(e.target.files[0]);
          }}
        />
        <a className="sampleLink" href="/sample.pdf" download>
          下载示范书《西游记》
        </a>
        {accountButton}
        {accountDialog}
        {(uploadError || err) && <p role="alert">{uploadError || err}</p>}
      </main>
    );
  if (!b)
    return (
      <main className="boot">
        <span className="logo">
          <Icon name="book" size={24} />
        </span>
        <h1>续页</h1>
        <p>{err || "正在打开阅读工作区…"}</p>
        {err && <button onClick={() => void refresh()}>重新打开</button>}
      </main>
    );
  return (
    <div className={"layout" + (panel ? " panelOpen" : "")}>
      {menu && (
        <button
          aria-label="关闭侧栏"
          className="mobileShade"
          onClick={() => sidebar(false)}
        />
      )}
      <aside
        className={"side " + (menu ? "onscreen" : "")}
        inert={(mobile && !menu) || (mobile && !!panel)}
      >
        <div className="brand">
          <span className="logo">
            <Icon name="book" size={22} />
          </span>
          <strong>续页</strong>
          <span className="brandsep" />
          阅读助手
        </div>
        {!demo && <button className="import" onClick={toggleImport}>
          <Icon name="plus" size={17} /> 导入书籍
        </button>}
        <LibrarySections
          key={b.id}
          book={b}
          books={books}
          thread={tid}
          card={panel === "cards" ? activeCard : null}
          openBook={(id) => {
            if (id !== b.id) void switchBook(id);
            else sidebar(false);
          }}
          openCard={(id) => {
            sidebar(false);
            setActiveCard(id);
            openPanel("cards");
          }}
          openThread={(id) => void view(b, id)}
          newChat={() => void newChat()}
          demo={demo}
          remove={removeItem}
        />
        <div className="sideFoot">
          <span className="avatar">
            <Icon name="bookmark" size={16} />
          </span>
          <div>
            {username || "访客书架"}<small>{username ? "阅读进度随账号同步" : "进度保存在当前浏览器对应的书架"}</small>
            {accountButton}
          </div>
        </div>
      </aside>
      <main className="workspace" inert={mobile && !!panel}>
        <header>
          <button
            className="icon mobileMenu"
            aria-label="打开书架"
            onClick={() => sidebar(true)}
          >
            <Icon name="menu" />
          </button>
          <div className="crumb">
            <Icon name="book" size={16} />
            <span>书架</span>
            <em>/</em>
            <b>{b.title}</b>
          </div>
          <button
            className="progressBtn"
            aria-label={"阅读书签 · " + location(b)}
            aria-expanded={panel === "bookmark"}
            onClick={() => openPanel("bookmark")}
          >
            <Icon name="bookmark" size={15} />
            <span>{b.progress ? location(b) : "标记阅读位置"}</span>
            <Icon name="chevron" size={13} />
          </button>
        </header>
        <div
          className="flow"
          ref={flow}
          onScroll={() => {
            if (!loadingThread && !scrollAfter.current && !panel && key)
              remember(key + ":scroll", position(flow.current));
          }}
        >
          <div className="column">
            {!loadingThread && !turns.length && !busy && (
              <section className="welcome">
                <div className="bookHero" data-edition={b.sample_author?.startsWith("简·奥斯汀") ? "austen" : undefined}>
                  <div aria-hidden="true" className="cover">
                    {b.sample_author?.startsWith("简·奥斯汀") ? (
                      <>
                        <span className="coverAuthor">Jane Austen</span>
                        <b lang="en">Pride and<br />Prejudice</b>
                      </>
                    ) : <b>{b.title}</b>}
                  </div>
                  <div className="bookHeroInfo">
                    <h1>{b.title}</h1>
                    <p>{b.sample_author || "PDF 电子书"}</p>
                    <div className="bookHeroPages"><Icon name="file" size={13} />{b.pages} 页</div>
                  </div>
                </div>
                <div className="welcomeText">
                  <span className="badge">
                    <Icon name="shield" size={14} />
                    防剧透已开启
                  </span>
                  <h2>
                    {b.progress ? "接着上次读到的地方聊" : "你读到哪里了？"}
                  </h2>
                  <p>
                    {b.progress
                      ? "你已读至" +
                        location(b) +
                        "。可以聊人物、回顾前情，或从一句原文开始。"
                      : "标记已读的最后一句，再聊人物或回顾前情。遇到可能涉及后文的问题，我会先问你。"}
                  </p>
                  <button
                    className="primary"
                    onClick={() =>
                      openPanel(b.progress ? "bookmark" : "adjust")
                    }
                  >
                    {b.progress ? "查看阅读位置" : "标记阅读位置"}{" "}
                    <Icon name="arrow" size={16} />
                  </button>
                </div>
                <div className="promptgrid">
                  {prompts.map((p, i) => (
                    <button
                      key={p}
                      onClick={() => {
                        if (i === 2) {
                          openPanel("quote");
                          return;
                        }
                        write(p);
                        editor.current?.focus();
                      }}
                    >
                      <Icon
                        name={(["history", "chat", "quote"] as const)[i]}
                        size={17}
                      />
                      <span>{p}</span>
                      <Icon name="chevron" size={13} />
                    </button>
                  ))}
                </div>
              </section>
            )}
            {turns.map((t) => (
              <article key={t.id} data-turn={t.id} className="turn">
                {t.hidden ? (
                  <div className="hidden">
                    <Icon name="shield" size={14} />
                    这段对话晚于当前阅读位置，已收起
                  </div>
                ) : (
                  <>
                    <div className="question">{t.question}</div>
                    <div className="assistantLabel">
                      <Icon name="book" size={17} />
                      续页
                      {t.pos !== undefined && t.pos !== b.progress && (
                        <small className="turnBookmark">
                          {t.pos
                            ? "提问时读至第 " + t.pos + " 句"
                            : "提问时尚未标记"}
                        </small>
                      )}
                    </div>
                    {t.answer && (
                      <AnswerView
                        turn={t.id}
                        a={t.answer}
                        inspect={openEvidence}
                      />
                    )}
                    {t.answer?.tally && (
                      <button
                        className="plain trackAction"
                        disabled={!!busy || loadingThread}
                        onClick={() => void perform({
                          kind: "ask", question: "继续核对", selected: 0,
                          id: crypto.randomUUID(), continueFrom: t.id,
                        })}
                      >
                        从此处继续核对
                      </button>
                    )}
                    {t.answer &&
                      !["off_topic", "clarify"].includes(t.answer.status) && (
                        <button
                          className="plain trackAction"
                          disabled={!!trackingTurn}
                          onClick={() => void trackTurn(t.id)}
                        >
                          <Icon
                            name={
                              b.tracked.includes(t.id) ? "check" : "bookmark"
                            }
                            size={14}
                          />
                          {trackingTurn === t.id
                            ? "正在整理追踪卡…"
                            : b.tracked.includes(t.id)
                              ? "查看追踪卡"
                              : t.spoiler
                                ? "持续追踪已读回答"
                                : "持续追踪"}
                        </button>
                      )}
                    {t.answer?.status === "answer" &&
                      !!t.answer.claims.length &&
                      !t.answer.conversational && (
                        <button
                          className="plain trackAction"
                          disabled={!!phrasing}
                          onClick={() => void rephrase(t.id)}
                        >
                          {phrasing === t.id ? "正在整理…" : "换个说法"}
                        </button>
                      )}
                    {t.pending && (
                      <div className="consent">
                        <strong>
                          <Icon name="shield" size={16} />
                          要查看后文吗？
                        </strong>
                        <p>
                          完整回答可能涉及未读内容。允许查看只对这一个问题生效。
                        </p>
                        <div>
                          <button
                            className="primary"
                            disabled={!!busy}
                            onClick={() => decide(t, "decline")}
                          >
                            只看已读内容
                          </button>
                          <button
                            className="outline"
                            disabled={!!busy}
                            onClick={() => decide(t, "allow")}
                          >
                            查看本题剧透
                          </button>
                        </div>
                      </div>
                    )}
                    {t.spoiler &&
                      (show[t.id] ? (
                        <div className="spoiler">
                          <div>
                            <strong>本题剧透回答</strong>
                            <button
                              className="plain"
                              onClick={() => {
                                setShow((old) => {
                                  const next = { ...old };
                                  delete next[t.id];
                                  return next;
                                });
                                if (panel === "evidence") closePanel();
                              }}
                            >
                              收起
                            </button>
                          </div>
                          <AnswerView
                            turn={t.id}
                            a={show[t.id]}
                            spoiler
                            inspect={openEvidence}
                          />
                        </div>
                      ) : (
                        <button
                          className="outline"
                          disabled={!!busy}
                          onClick={() => decide(t, "reveal")}
                        >
                          展开本题剧透回答
                        </button>
                      ))}
                  </>
                )}
              </article>
            ))}
            {pendingQ && <div className="question">{pendingQ}</div>}

            <div ref={bottom} />
          </div>
        </div>
        <div className="composeWrap">
          <div className="column">
            {busy && (
              <div className="workbar">
                <div className="working" role="status">
                  <i />
                  {busy}
                </div>
                {(fetchRequest.current?.intent.kind === "ask" ||
                  fetchRequest.current?.intent.kind === "allow") && (
                  <button
                    className="outline stop"
                    disabled={fetchRequest.current.stopping}
                    onClick={() => void stop().catch(() => {})}
                  >
                    停止回答
                  </button>
                )}
              </div>
            )}
            {(err || notice) && (
              <div
                className={err ? "error" : "statusNotice"}
                role={err ? "alert" : "status"}
              >
                <span>{err || notice}</span>
                {retry && (
                  <button
                    className="outline"
                    onClick={() => void perform(retry)}
                  >
                    重试
                  </button>
                )}
                <button
                  className="icon"
                  aria-label="关闭提示"
                  onClick={() => {
                    error("");
                    setNotice("");
                    setRetry(null);
                  }}
                >
                  <Icon name="close" size={14} />
                </button>
              </div>
            )}
            <form
              className="composer"
              onSubmit={(e) => {
                e.preventDefault();
                void ask();
              }}
            >
              {selected && (
                <button
                  type="button"
                  className="selectedQuote"
                  onClick={() => select(null)}
                >
                  {selected.text} ×
                </button>
              )}
              <textarea
                ref={editor}
                aria-label="输入与本书有关的问题"
                placeholder={"关于《" + b.title + "》，你想问什么？"}
                value={draft}
                maxLength={1800}
                onChange={(e) => {
                  write(e.target.value);
                  if (retry?.kind === "ask") {
                    setRetry(null);
                    error("");
                    setNotice("");
                  }
                }}
                onKeyDown={(e) => {
                  if (
                    e.key === "Enter" &&
                    !e.shiftKey &&
                    !e.nativeEvent.isComposing
                  ) {
                    e.preventDefault();
                    void ask();
                  }
                }}
              />
              <div className="composeFoot">
                <button
                  type="button"
                  className="plain"
                  onClick={() => openPanel("quote")}
                >
                  <Icon name="quote" size={14} /> 引用原文
                </button>
                <button
                  className="submit"
                  disabled={!draft.trim() || !!busy}
                  aria-label="发送问题"
                >
                  <Icon name="send" size={18} />
                </button>
              </div>
            </form>
            <p className="privacy">
              <Icon name="shield" size={12} />
              默认仅使用已读内容<span>·</span>回答可回到原文核对
            </p>
          </div>
        </div>
      </main>
      {panel === "bookmark" && (
        <ReadingPanel title="阅读书签" close={closePanel} kind="bookmarkPanel">
          <div className="drawerHead">
            <div>
              <span className="eyebrow">{b.title}</span>
              <h2>阅读书签</h2>
            </div>
            <button className="icon" aria-label="关闭书签" onClick={closePanel}>
              <Icon name="close" />
            </button>
          </div>
          <div className="bookmarkPaper">
            <span className="bookmarkLabel">
              <Icon name="bookmark" size={15} />
              {location(b)}
            </span>
            <small>{b.location ? "上次读到" : "尚未开始阅读"}</small>
            {b.location ? (
              <blockquote>{b.location.text}</blockquote>
            ) : (
              <p>选择你读完的最后一句，保存这本书的第一个书签。</p>
            )}
          </div>
          <div className="bookmarkActions">
            <button className="primary" onClick={() => openPanel("adjust")}>
              调整进度 <Icon name="arrow" size={15} />
            </button>
            {b.previous !== b.progress && (
              <button
                className="plain"
                onClick={() =>
                  void saveMark(b.previous).catch((e) => {
                    closePanel();
                    error(e.message);
                  })
                }
              >
                撤销上次标记
              </button>
            )}
          </div>
        </ReadingPanel>
      )}
      {panel === "cards" && (
        <TrackingPanel
          key={b.id + ":" + b.rev}
          book={b}
          active={
            b.card_topics.find((c) => c.members.includes(activeCard ?? ""))
              ?.id ?? activeCard
          }
          select={(id) => {
            setActiveCard(id);
            setCardHistory(null);
          }}
          historyView={cardHistory}
          setHistoryView={setCardHistory}
          close={closePanel}
          source={(r) => {
            setCardQuote(r);
            openPanel("quote");
          }}
        />
      )}
      {(panel === "adjust" || panel === "quote") && (
        <Reader
          key={b.id + panel}
          b={b}
          purpose={panel}
          initialQuote={panel === "quote" ? cardQuote : null}
          close={cardQuote ? backToCards : closePanel}
          mark={saveMark}
          quote={quote}
        />
      )}
      {panel === "evidence" && evidence && (
        <ReadingPanel title="原文依据" close={closePanel} kind="evidence">
          <div className="drawerHead">
            <div>
              <span className="eyebrow">{b.title}</span>
              <h2>原文依据</h2>
            </div>
            <button className="icon" aria-label="关闭" onClick={closePanel}>
              <Icon name="close" />
            </button>
          </div>
          <p className="evHint">
            {evidence.spoiler
              ? "以下引文属于本题的剧透回答。"
              : "只展示当前读到的原句。"}
          </p>
          {evidence.refs.map((r) => (
            <div key={r.pos} className="ref">
              <small>PDF 第 {r.page} 页</small>
              <blockquote>
                <mark>{r.text}</mark>
              </blockquote>
              {!evidence.spoiler && (
                <img
                  loading="lazy"
                  className="originalCut"
                  alt="原页中这句文字的裁切"
                  src={
                    "/api/books/" +
                    b.id +
                    "/turns/" +
                    evidence.turn +
                    "/citation/" +
                    r.pos +
                    "?spoiler=" +
                    evidence.spoiler +
                    "&rev=" +
                    b.rev
                  }
                />
              )}
              {r.pos <= b.progress && (
                <button className="plain" onClick={() => quote(r)}>
                  引用这句提问 <Icon name="arrow" size={14} />
                </button>
              )}
            </div>
          ))}
        </ReadingPanel>
      )}
      {accountDialog}
      {upload && (
        <div className="scrim center">
          <section
            role="dialog"
            aria-modal="true"
            aria-label="导入书籍"
            className="modal"
          >
            <div className="drawerHead">
              <div>
                <span className="eyebrow">我的书架</span>
                <h2>{importReport ? "PDF 导入结果" : "导入书籍"}</h2>
              </div>
              <button
                className="icon"
                aria-label="关闭"
                disabled={importingFile}
                onClick={() => importing(false)}
              >
                <Icon name="close" />
              </button>
            </div>
            {importReport ? (
              <div className="importReport">
                <p><strong>{b.title}</strong>　共 {b.pages} 页，{importReport.readable_pages} 页可用于问答。</p>
                {!!importReport.missing_pages.length && (
                  <>
                    <p>第 {pageRanges(importReport.missing_pages)} 页未提取到可用于问答的正文。</p>
                    <p className="hint">这些页可能是封面、目录或扫描页，可以在阅读器里查看原页。</p>
                  </>
                )}
                <button className="primary" onClick={() => {
                  importing(false);
                  openPanel("adjust");
                }}>打开原文并标记进度</button>
              </div>
            ) : <>
            <button
              disabled={importingFile}
              className="drop"
              onDragOver={(e) => e.preventDefault()}
              onDrop={(e) => {
                e.preventDefault();
                if (e.dataTransfer.files[0])
                  void importFile(e.dataTransfer.files[0]);
              }}
              onClick={() => file.current?.click()}
            >
              <Icon name="upload" size={27} />
              <strong>
                {importingFile ? "正在提取原文和位置…" : "点击选择或拖入 PDF"}
              </strong>
              <small>文字版 PDF · 最大 32 MB。问答与追踪会将相关原文发送给 DeepSeek。</small>
            </button>
            <input
              type="file"
              ref={file}
              accept="application/pdf,.pdf"
              hidden
              onChange={(e) => {
                if (e.target.files?.[0]) void importFile(e.target.files[0]);
              }}
            />
            {uploadError && (
              <p className="error" role="alert">
                {uploadError}
              </p>
            )}
            <a className="sampleLink" href="/sample.pdf" download>
              下载示范书《西游记》
              <Icon name="file" size={14} />
            </a>
            </>}
          </section>
        </div>
      )}
    </div>
  );
}
createRoot(document.getElementById("root")!).render(<Workspace />);
