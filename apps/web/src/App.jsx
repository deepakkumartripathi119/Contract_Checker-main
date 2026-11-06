import { useEffect, useState } from "react";
import { SignIn, Register } from "./components/AuthScreens";
import Sidebar from "./components/Sidebar";
import PolicyPage from "./components/PolicyPage";
import VendorPage from "./components/VendorPage";
import { ClauseDialog, ConfirmDialog, VendorDialog } from "./components/Dialogs";
import { authed, authHeader, readError, tokenExpiry } from "./api";
import "./styles/app.css";

export default function App() {
  const [token, setToken] = useState(() => sessionStorage.getItem("token") || "");
  const [screen, setScreen] = useState("login");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [vendors, setVendors] = useState([]);
  const [selected, setSelected] = useState(null);
  const [view, setView] = useState("policy");
  const [sidebarOpen, setSidebarOpen] = useState(() => window.innerWidth > 1024);
  const [addOpen, setAddOpen] = useState(false);
  const [clauseOpen, setClauseOpen] = useState(false);
  const [confirm, setConfirm] = useState(null);
  const name = sessionStorage.getItem("company_name") || "";

  function expire() {
    sessionStorage.clear();
    setToken("");
    setSelected(null);
    setVendors([]);
    setView("policy");
    setScreen("login");
    setNotice("Your session has ended. Sign in again.");
  }

  async function loadVendors() {
    const response = await authed("/v1/vendors", { headers: authHeader() }, expire);
    if (!response.ok) return [];
    const body = await response.json();
    const rows = body.vendors || [];
    setVendors(rows);
    return rows;
  }

  useEffect(() => {
    if (token && tokenExpiry(token) <= Date.now()) expire();
  }, [token]);

  useEffect(() => {
    const timer = setInterval(() => {
      const current = sessionStorage.getItem("token");
      if (current && tokenExpiry(current) <= Date.now()) expire();
    }, 15000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    if (token) loadVendors().catch(() => {});
  }, [token]);

  async function signIn(event) {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(event.target));
    const response = await fetch("/v1/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
    if (!response.ok) { await readError(response); }
    const body = await response.json();
    sessionStorage.setItem("token", body.token);
    sessionStorage.setItem("company_name", body.name || body.org_id || "");
    setNotice("");
    setToken(body.token);
    setView("policy");
  }

  async function register(event, size) {
    const data = Object.fromEntries(new FormData(event.target));
    data.company_size = size;
    const response = await fetch("/v1/register", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
    if (!response.ok) { setError((await response.json().catch(() => ({}))).detail || "Register failed"); return; }
    const body = await response.json();
    sessionStorage.setItem("token", body.token);
    sessionStorage.setItem("company_name", body.name || body.org_id || "");
    setToken(body.token);
    setView("policy");
  }

  async function createVendor(vendorName) {
    const response = await authed("/v1/vendors", {
      method: "POST", headers: { ...authHeader(), "Content-Type": "application/json" }, body: JSON.stringify({ name: vendorName }),
    }, expire);
    if (!response.ok) await readError(response);
    const created = await response.json();
    setAddOpen(false);
    const rows = await loadVendors();
    setSelected(rows.find((item) => item.vendor_id === created.vendor_id) || created);
    setView("vendor");
  }

  async function sendClause(heading, text) {
    const response = await authed("/v1/vendors/" + encodeURIComponent(selected.vendor_id) + "/clause", {
      method: "POST", headers: { ...authHeader(), "Content-Type": "application/json" },
      body: JSON.stringify({ clause_type: heading, clause_text: text }),
    }, expire);
    if (!response.ok) await readError(response);
    setClauseOpen(false);
    await loadVendors();
  }

  if (!token) {
    return (
      <main className="auth">
        {screen === "login"
          ? <SignIn notice={notice} onSubmit={(event) => signIn(event).catch((item) => setNotice(item.message))} onRegister={() => setScreen("register")} />
          : <Register error={error} onSubmit={register} onSignIn={() => setScreen("login")} />}
      </main>
    );
  }

  return (
    <div className={sidebarOpen ? "shell" : "shell rail"}>
      <Sidebar
        open={sidebarOpen}
        onToggle={() => setSidebarOpen((value) => !value)}
        vendors={vendors}
        selectedId={selected && selected.vendor_id}
        onOpenVendor={(vendor) => { setSelected(vendor); setView("vendor"); }}
        onDeleteVendor={(vendor) => setConfirm({
          title: "Delete vendor",
          text: "Delete " + vendor.name + "?",
          run: async () => {
            const response = await authed("/v1/vendors/" + encodeURIComponent(vendor.vendor_id), { method: "DELETE", headers: authHeader() }, expire);
            if (!response.ok) await readError(response);
            if (selected && selected.vendor_id === vendor.vendor_id) { setSelected(null); setView("policy"); }
            setConfirm(null);
            await loadVendors();
          },
        })}
        onPolicy={() => { setView("policy"); setSelected(null); }}
        policyCurrent={view === "policy"}
        onAdd={() => setAddOpen(true)}
      />
      <main>
        <header className="top"><strong>{name}</strong><button type="button" onClick={() => { sessionStorage.clear(); setToken(""); }}>Log out</button></header>
        {view === "policy" || !selected ? <PolicyPage onExpired={expire} /> : (
          <VendorPage vendor={selected} onExpired={expire} onReload={loadVendors} onClause={() => setClauseOpen(true)} />
        )}
      </main>
      <VendorDialog open={addOpen} onClose={() => setAddOpen(false)} onCreate={(value) => createVendor(value).catch(() => {})} />
      <ClauseDialog open={clauseOpen} onClose={() => setClauseOpen(false)} onSend={(heading, text) => sendClause(heading, text).catch(() => {})} />
      <ConfirmDialog open={Boolean(confirm)} title={confirm && confirm.title} text={confirm && confirm.text} onClose={() => setConfirm(null)} onYes={() => confirm && confirm.run()} />
    </div>
  );
}
