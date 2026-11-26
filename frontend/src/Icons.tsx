type Name =
  | "trash"
  | "book"
  | "shelf"
  | "plus"
  | "chat"
  | "arrow"
  | "send"
  | "close"
  | "bookmark"
  | "menu"
  | "quote"
  | "back"
  | "chevron"
  | "shield"
  | "upload"
  | "file"
  | "history"
  | "check";
const shapes: Record<Name, React.ReactNode> = {
  trash: (
    <>
      <path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 10v7m4-7v7" />
    </>
  ),
  book: (
    <>
      <path d="M3 4.5c3.2-.8 6-.2 9 1.8 3-2 5.8-2.6 9-1.8v14c-3.2-.8-6-.2-9 1.6-3-1.8-5.8-2.4-9-1.6z" />
      <path d="M12 6.3v13" />
    </>
  ),
  shelf: (
    <>
      <path d="M4 4v15M8 4v15M12 4v15M15 5l4 14M3 20h18" />
    </>
  ),
  plus: <path d="M12 5v14M5 12h14" />,
  chat: (
    <path d="M20 14a3 3 0 0 1-3 3H9l-5 4V6a3 3 0 0 1 3-3h10a3 3 0 0 1 3 3z" />
  ),
  arrow: <path d="M4 12h15m-6-6 6 6-6 6" />,
  send: <path d="M12 19V5m-6 6 6-6 6 6" />,
  close: <path d="m6 6 12 12M18 6 6 18" />,
  bookmark: <path d="M6 3h12v18l-6-4-6 4z" />,
  menu: <path d="M3 6h18M3 12h18M3 18h18" />,
  quote: (
    <>
      <path d="M5 16c0-5 1-7 5-9M13 16c0-5 1-7 5-9" />
      <path d="M5 14h5v5H5zm8 0h5v5h-5z" />
    </>
  ),
  back: <path d="m14 6-6 6 6 6" />,
  chevron: <path d="m9 6 6 6-6 6" />,
  shield: (
    <>
      <path d="m12 3 8 3v5c0 4-3 7-8 10-5-3-8-6-8-10V6z" />
      <path d="m8.5 11 2.5 2.5 4.5-5" />
    </>
  ),
  upload: (
    <>
      <path d="M12 16V4m-4 4 4-4 4 4M4 15v5h16v-5" />
    </>
  ),
  file: <path d="M5 3h9l5 5v13H5zm9 0v6h5" />,
  history: (
    <>
      <path d="M3 11a9 9 0 1 1 2 7M3 5v6h6M12 7v6l4 2" />
    </>
  ),
  check: <path d="m4 12 5 5L20 6" />,
};
export function Icon({ name, size = 18 }: { name: Name; size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.65"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {shapes[name]}
    </svg>
  );
}
