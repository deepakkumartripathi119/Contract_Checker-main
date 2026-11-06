import { useEffect, useState } from "react";
import DocShield from "./DocShield";
import ReportView from "./ReportView";
import { authed, authHeader, readError } from "../api";
import "./VendorPage.css";

const STEPS = ["extracting", "analysing", "matching", "completed"];

export default function VendorPage({ vendor, onExpired, onReload, onClause }) {
  const [replacing, setReplacing] = useState(false);
  const [job, setJob] = useState(null);
  const [step, setStep] = useState("");
  const [seen, setSeen] = useState(-1);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [handoff, setHandoff] = useState(false);
  const hasReport = vendor.status === "succeeded" && vendor.job_id;
  const showChoices = !hasReport || replacing;

  async function poll(jobId) {
    let current = { status: "queued", job_id: jobId, step: "" };
    for (let attempt = 0; attempt < 150 && current.status !== "succeeded" && current.status !== "failed"; attempt += 1) {
      const response = await authed("/v1/jobs/" + encodeURIComponent(jobId), { headers: authHeader() }, onExpired);
      if (!response.ok) await readError(response);
      current = await response.json();
      setStep(current.step || "");
      const index = STEPS.indexOf(current.step || "");
      if (index >= 0 && current.step !== "completed") setSeen((value) => Math.max(value, index));
      if (current.status === "queued" || current.status === "running") await new Promise((resolve) => setTimeout(resolve, 2000));
    }
    if (current.status === "failed") throw new Error(current.error || "The check failed");
    setStep("");
    setHandoff(true);
    await new Promise((resolve) => setTimeout(resolve, 1700));
    setJob(current);
    setHandoff("leave");
    await new Promise((resolve) => setTimeout(resolve, 480));
    setHandoff(false);
    setReplacing(false);
    await onReload();
  }

  async function upload(file) {
    setError("");
    setSeen(-1);
    setStep("");
    setBusy(true);
    setJob(null);
    const data = new FormData();
    data.append("contract", file);
    const response = await authed("/v1/vendors/" + encodeURIComponent(vendor.vendor_id) + "/contract", {
      method: "POST", headers: authHeader(), body: data,
    }, onExpired);
    if (!response.ok) await readError(response);
    const body = await response.json();
    try {
      await poll(body.job_id);
    } finally {
      setBusy(false);
      setHandoff(false);
    }
  }

  useEffect(() => {
    setJob(null);
    setReplacing(false);
    setStep("");
    setSeen(-1);
    if (vendor.status === "succeeded" && vendor.job_id) {
      openSaved().catch((item) => setError(item.message));
    }
  }, [vendor.vendor_id]);

  async function removeReport() {
    const response = await authed("/v1/vendors/" + encodeURIComponent(vendor.vendor_id) + "/report", {
      method: "DELETE", headers: authHeader(),
    }, onExpired);
    if (!response.ok) await readError(response);
    setJob(null);
    setReplacing(false);
    await onReload();
  }

  async function openSaved() {
    const response = await authed("/v1/jobs/" + encodeURIComponent(vendor.job_id), { headers: authHeader() }, onExpired);
    if (!response.ok) await readError(response);
    setJob(await response.json());
    setReplacing(false);
  }

  async function download() {
    const response = await authed("/v1/jobs/" + encodeURIComponent(job.job_id) + "/report.pdf", { headers: authHeader() }, onExpired);
    if (!response.ok) await readError(response);
    const blob = await response.blob();
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = "report.pdf";
    link.click();
  }

  const running = busy || handoff || Boolean(step && (!job || job.status !== "succeeded"));

  return (
    <section className="vendor-page">
      {running ? (
        <div className={"analyzing" + (handoff === true ? " complete" : "") + (handoff === "leave" ? " complete leaving" : "")}>
          {handoff ? (
            <div className="complete-mark" aria-hidden="true">
              <span className="halo" />
              <svg className="orbit" viewBox="0 0 72 72"><circle cx="36" cy="36" r="30" fill="none" stroke="#178a4a" strokeWidth="3" strokeDasharray="12 8" strokeLinecap="round" /></svg>
              <svg className="tick" viewBox="0 0 72 72"><circle cx="36" cy="36" r="22" fill="#e8f8ee" /><path d="M26 37.5l7 7 14-15" fill="none" stroke="#178a4a" strokeWidth="3.5" strokeLinecap="round" strokeLinejoin="round" /></svg>
            </div>
          ) : <div className="spinner" aria-hidden="true" />}
          <h2>{handoff ? "Completed" : "Analyzing your contract"}</h2>
          {handoff ? null : <p>This can take a few minutes. Keep this tab open.</p>}
          <ol className={handoff ? "steps hidden-steps" : "steps"}>
            {STEPS.map((name, index) => (
              <li key={name} className={step === "completed" ? (index <= seen ? "done" : index === STEPS.indexOf(step) ? "now" : "") : (index < STEPS.indexOf(step) ? "done" : index === STEPS.indexOf(step) ? "now" : "")}>
                {name[0].toUpperCase() + name.slice(1)}
              </li>
            ))}
          </ol>
        </div>
      ) : null}
      {showChoices && !running ? (
        <div>
          <DocShield />
          <h1>Welcome to <span className="brand-name">Clause<span>Intel</span></span>!</h1>
          <p className="quiet">Your AI-powered contract and policy compliance checker</p>
          <div className="choice-grid">
            <article className="choice">
              <h2>Upload Contract</h2>
              <p className="quiet">PDF, DOCX files are supported</p>
              <label className="choice-btn solid">Upload Contract
                <input type="file" accept="application/pdf,.pdf,.docx" hidden onChange={(event) => { const file = event.target.files && event.target.files[0]; if (file) upload(file).catch((item) => setError(item.message)); }} />
              </label>
            </article>
            <article className="choice">
              <h2>Add Single Clause</h2>
              <p className="quiet">Ask about a specific clause</p>
              <button className="choice-btn soft" type="button" onClick={onClause}>Type Clause →</button>
            </article>
          </div>
          {hasReport ? <p className="note">A new contract or clause removes this report after the new one is ready. Download the report first.</p> : null}
          {hasReport ? (
            <div className="report-file">
              <button type="button" onClick={() => openSaved().catch((item) => setError(item.message))}>{vendor.filename || vendor.name}</button>
              {vendor.updated_at ? <time dateTime={vendor.updated_at}>{new Date(vendor.updated_at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}</time> : null}
              <button type="button" aria-label="Delete report" onClick={() => removeReport().catch((item) => setError(item.message))}>Delete</button>
            </div>
          ) : null}
        </div>
      ) : null}
      {error ? <p className="error">{error}</p> : null}
      {job && handoff !== true && job.result && (job.result.present_clauses || job.result.missing_clauses) ? (
        <div className="report-tools">
          <button className="tool tip" type="button" data-tip="Download the report as a PDF" aria-label="Download the report as a PDF" onClick={() => download().catch((item) => setError(item.message))}>
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v10M8 10l4 4 4-4M5 19h14" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" /></svg>
          </button>
          <button className="delete-report tip" type="button" data-tip="Delete report" aria-label="Delete report" onClick={() => removeReport().catch((item) => setError(item.message))}>
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 7h14M9 7V5h6v2M8 7l.7 12h6.6L16 7" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" /></svg>
          </button>
          <button type="button" onClick={() => setReplacing(true)}>Check another</button>
        </div>
      ) : null}
      {job && handoff !== true && job.result && (job.result.present_clauses || job.result.missing_clauses) ? (
        <ReportView report={job.result} job={job} />
      ) : null}
    </section>
  );
}
