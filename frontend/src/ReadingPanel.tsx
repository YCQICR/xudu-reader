import { useEffect, useRef, useState, type ReactNode } from "react";

export function ReadingPanel({
  title,
  children,
  close,
  kind = "",
}: {
  title: string;
  children: ReactNode;
  close: () => void;
  kind?: string;
}) {
  const panel = useRef<HTMLElement>(null);
  const [mobile, setMobile] = useState(
    () => matchMedia("(max-width: 900px)").matches,
  );
  useEffect(() => {
    const m = matchMedia("(max-width: 900px)");
    const change = () => setMobile(m.matches);
    m.addEventListener("change", change);
    return () => m.removeEventListener("change", change);
  }, []);
  useEffect(() => {
    panel.current?.focus({ preventScroll: true });
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        close();
        return;
      }
      if (!mobile || e.key !== "Tab" || !panel.current) return;
      const els = Array.from(
        panel.current.querySelectorAll<HTMLElement>(
          'button:not(:disabled),input,select,summary,[tabindex="0"]',
        ),
      ).filter((x) => x.offsetParent !== null);
      if (
        els.length &&
        e.shiftKey &&
        (document.activeElement === els[0] ||
          document.activeElement === panel.current)
      ) {
        e.preventDefault();
        els.at(-1)?.focus();
      } else if (
        els.length &&
        !e.shiftKey &&
        document.activeElement === els.at(-1)
      ) {
        e.preventDefault();
        els[0].focus();
      }
    };
    document.addEventListener("keydown", key);
    return () => document.removeEventListener("keydown", key);
  }, [mobile, close, title]);
  return (
    <div
      className="readingPanel"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && mobile) close();
      }}
    >
      <section
        ref={panel}
        tabIndex={-1}
        role={mobile ? "dialog" : "complementary"}
        aria-modal={mobile || undefined}
        aria-label={title}
        className={"drawer " + kind}
      >
        {children}
      </section>
    </div>
  );
}
