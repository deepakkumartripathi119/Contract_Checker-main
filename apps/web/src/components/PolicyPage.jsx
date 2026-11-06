import { useEffect, useState } from "react";
import DocShield from "./DocShield";
import { authed, authHeader, readError } from "../api";
import "./PolicyPage.css";

export default function PolicyPage({ onExpired }) {
  const [file, setFile] = useState(null);
  const [pending, setPending] = useState("");
  const [chosen, setChosen] = useState(null);
  const [preview, setPreview] = useState(false);
  const [note, setNote] = useState("");

  async function load() {
    const response = await authed("/v1/policies/current", { headers: authHeader() }, onExpired);
    if (!response.ok) return;
    const body = await response.json();
    setFile(body.file || null);
    setPending(body.pending_status || "");
  }

  useEffect(() => { load().catch(() => {}); }, []);

  async function upload(event) {
    event.preventDefault();
    if (!chosen) return;
    const data = new FormData();
    data.append("files", chosen);
    setNote("Storing the policy. Keep this tab open.");
    const response = await authed("/v1/policies", { method: "POST", headers: authHeader(), body: data }, onExpired);
    if (!response.ok) { await readError(response); }
    const body = await response.json();
    let job = { status: "queued", job_id: body.job_id };
    for (let attempt = 0; attempt < 150 && job.status !== "succeeded" && job.status !== "failed"; attempt += 1) {
      const read = await authed("/v1/jobs/" + encodeURIComponent(job.job_id), { headers: authHeader() }, onExpired);
      if (!read.ok) await readError(read);
      job = await read.json();
      if (job.status === "queued" || job.status === "running") await new Promise((resolve) => setTimeout(resolve, 2000));
    }
    if (job.status === "failed") throw new Error(job.error || "The check failed");
    setChosen(null);
    setNote("The company policy is ready.");
    await load();
  }

  async function download() {
    const response = await authed("/v1/policies/file", { headers: authHeader() }, onExpired);
    if (!response.ok) await readError(response);
    const blob = await response.blob();
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = (file && file.filename) || "policy";
    link.click();
  }

  return (
    <section className="policy-page">
      <header className="policy-intro">
        <DocShield />
        <h1>Company Policy</h1>
        <p className="quiet">Every contract and every clause is checked against this file. The company keeps one current policy.</p>
      </header>
      {file ? (
        <article className="policy-card">
          <strong>Company Policy</strong>
          <div>{file.filename}</div>
          <div className="uploaded">Uploaded</div>
          <button type="button" onClick={() => setPreview((value) => !value)}>Preview</button>
          <button type="button" onClick={() => download().catch(() => {})}>Download Policy</button>
          {preview ? <p>This file cannot be previewed here. Download it.</p> : null}
          {pending ? <p>A new policy is {pending}. This file stays until that finishes.</p> : null}
        </article>
      ) : null}
      <form className="policy-card" onSubmit={(event) => upload(event).catch((error) => setNote(error.message))}>
        <h2>{file ? "Replace the current policy" : "Upload a company policy"}</h2>
        <p className="quiet">{file ? "PDF or DOCX." : "PDF or DOCX. Nothing is stored until you upload it."}</p>
        {file ? <p className="note">Uploading a new policy deletes the current one.</p> : null}
        <input type="file" accept="application/pdf,.pdf,.docx" onChange={(event) => setChosen(event.target.files && event.target.files[0])} />
        <button className="primary" type="submit" disabled={!chosen}>Upload policy</button>
        {note ? <p className="quiet">{note}</p> : null}
      </form>
    </section>
  );
}
