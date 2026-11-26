import { useEffect, useRef, useState, type FormEvent } from "react";
import { api } from "./types";

export function AccountDialog({
  username, invitationRequired, close, refresh,
}: {
  username: string | null;
  invitationRequired: boolean;
  close: () => void;
  refresh: () => Promise<void>;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [register, setRegister] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => { dialog.current?.showModal(); }, []);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    const fields = new FormData(event.currentTarget);
    setBusy(true);
    setError("");
    try {
      const method = username ? "logout" : register ? "register" : "login";
      await api("/api/account/" + method, "POST", username ? undefined : {
        username: fields.get("username"), password: fields.get("password"),
        invitation: fields.get("invitation") || "",
      });
      await refresh();
      close();
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <dialog className="modal accountDialog" ref={dialog} onCancel={event => {
      event.preventDefault();
      if (!busy) close();
    }} aria-labelledby="account-heading">
      <form onSubmit={submit}>
        <div className="drawerHead">
          <h2 id="account-heading">{username || (register ? "创建账号" : "登录续页")}</h2>
          <button type="button" className="plain" disabled={busy} onClick={close}>关闭</button>
        </div>
        {!username && <>
          {register && <p>创建账号后，这个浏览器的访客书架会转入你的账号。</p>}
          <label>用户名<input required name="username" autoComplete="username" maxLength={40} /></label>
          <label>密码<input required name="password" type="password" minLength={register ? 10 : 1}
            maxLength={128} autoComplete={register ? "new-password" : "current-password"} /></label>
          {register && invitationRequired && <label>邀请码<input required name="invitation" type="password" maxLength={160} /></label>}
        </>}
        {error && <p className="error" role="alert">{error}</p>}
        <div className="accountActions">
          <button className="primary" disabled={busy}>{busy ? "正在处理…" : username ? "退出登录" : register ? "创建账号" : "登录"}</button>
          {!username && <button className="plain" type="button" disabled={busy} onClick={() => { setRegister(!register); setError(""); }}>
            {register ? "已有账号，去登录" : "创建新账号"}
          </button>}
        </div>
      </form>
    </dialog>
  );
}
