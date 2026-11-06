import "./Logo.css";

export default function Logo({ size = 40, tone = "light" }) {
  return (
    <div className={"brand tone-" + tone} style={{ "--logo": size + "px" }}>
      <svg className="logo" aria-hidden="true">
        <use href="#clauseintel-logo" />
      </svg>
      <span className="brand-name">Clause<span>Intel</span></span>
    </div>
  );
}
