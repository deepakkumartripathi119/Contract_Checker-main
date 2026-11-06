import "./ReportView.css";

function repeatsReview(reason, reviewReason) {
  if (!reviewReason || !reason) return false;
  return reason.startsWith("The checked internal policy contains no applicable rule for this clause.")
    || reason.startsWith("Internal policy evidence could not be retrieved or inspected.")
    || reason.startsWith("Reranker confidence");
}

function tone(label) {
  if (label === "Matches the policy") return ["ok", "✓"];
  if (label === "Not in the contract" || label === "Only partly in the contract") return ["missing", "–"];
  if (label === "Needs a person to review") return ["review", "!"];
  return ["bad", "✕"];
}

function contractText(item) {
  const raw = (item.clause_text || "").trim();
  if (!raw) return "";
  const title = (item.clause_title || "").trim();
  const lines = raw.split(/\r?\n/);
  const first = lines[0].trim().replace(/^\d+(?:\.\d+)*\.?\s+/, "");
  if (title && first.toLowerCase() === title.toLowerCase()) return lines.slice(1).join("\n").trim();
  return raw;
}

function Finding({ item }) {
  const label = item.reader_label || "Needs a person to review";
  const [name, symbol] = tone(label);
  const sentence = contractText(item);
  return (
    <article className={"finding " + name}>
      <span className="symbol" aria-hidden="true">{symbol}</span>
      <div>
        <h3>{item.clause_title || item.clause_type || "Clause"}</h3>
        <p className="label">{label}</p>
        {sentence ? <p>Contract text: {sentence}</p> : null}
        {item.reason && !repeatsReview(item.reason, item.review_reason) ? <p>{item.reason}</p> : null}
        {item.needs_review && item.review_reason ? <p>{item.review_reason}</p> : null}
        {item.exact_policy_quote ? <p className="quote">Policy quote: {item.exact_policy_quote}</p> : null}
        {(item.clauses_internal || []).map((place) => <p className="place" key={place}>Policy: {place}</p>)}
        {item.suggested_revision ? <p className="suggestion">Suggested revision: {item.suggested_revision}</p> : null}
      </div>
    </article>
  );
}

export default function ReportView({ report, job }) {
  const summary = report.summary || {};
  const nonCompliant = (summary.does_not_match_the_policy || 0) + (summary.only_partly_in_the_contract || 0) + (summary.conflicts_with_the_policy || 0);
  const inactive = report.missing_clause_check === "not_run";
  const cards = [
    ["Total", summary.total || 0, "total", "#"],
    ["Compliant", summary.matches_the_policy || 0, "compliant", "✓"],
    ["Not in the contract", summary.not_in_the_contract || 0, "missing", "–"],
    ["Review", summary.needs_a_person_to_review || 0, "review", "!"],
    ["Non-compliant", nonCompliant, "bad", "✕"],
  ];
  const seconds = job && job.created_at && job.updated_at
    ? Math.max(0, Math.round((Date.parse(job.updated_at) - Date.parse(job.created_at)) / 1000))
    : null;
  const meta = [
    report.document_name ? "File: " + report.document_name : "",
    Number.isFinite(seconds) ? "Time: " + seconds + " seconds" : "",
  ].filter(Boolean).join(" · ");
  return (
    <section className="report">
      <div className="counts">
        {cards.map(([label, value, name, symbol]) => (
          <article key={label} className={"count " + name + (label === "Not in the contract" && inactive ? " inactive" : "")}>
            <i className="mark" aria-hidden="true">{symbol}</i>
            <strong>{value}</strong>
            <span>{label}</span>
          </article>
        ))}
      </div>
      {meta ? <p className="report-meta">{meta}</p> : null}
      {(report.present_clauses || []).map((item, index) => <Finding key={"p" + index} item={item} />)}
      {(report.missing_clauses || []).length ? <h2 className="section-label">Missing from the contract</h2> : null}
      {(report.missing_clauses || []).map((item, index) => <Finding key={"m" + index} item={item} />)}
    </section>
  );
}
