import { useState } from "react";
import Logo from "./Logo";
import "./Sidebar.css";

function Building() {
  return (
    <span className="bldg" aria-hidden="true">
      <svg viewBox="0 0 24 24">
        <rect x="4.5" y="3" width="15" height="18" rx="1.2" fill="none" stroke="currentColor" strokeWidth="1.8" />
        <rect x="7.4" y="6" width="2.3" height="2.3" fill="currentColor" />
        <rect x="14.3" y="6" width="2.3" height="2.3" fill="currentColor" />
        <rect x="7.4" y="10.2" width="2.3" height="2.3" fill="currentColor" />
        <rect x="14.3" y="10.2" width="2.3" height="2.3" fill="currentColor" />
        <rect x="7.4" y="14.4" width="2.3" height="2.3" fill="currentColor" />
        <rect x="14.3" y="14.4" width="2.3" height="2.3" fill="currentColor" />
      </svg>
    </span>
  );
}

export default function Sidebar({
  open, onToggle, vendors, selectedId, onOpenVendor, onDeleteVendor, onPolicy, policyCurrent, onAdd,
}) {
  const [query, setQuery] = useState("");
  const [finding, setFinding] = useState(false);
  const shown = vendors.filter((vendor) => vendor.name.toLowerCase().includes(query.trim().toLowerCase()));
  return (
    <aside className={open ? "side" : "side rail"}>
      <div className="side-top">
        {open ? <Logo size={38} tone="navy" /> : null}
        <button className="slide" type="button" aria-label={open ? "Close sidebar" : "Open sidebar"} onClick={onToggle}>
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <rect x="3" y="4" width="7" height="16" rx="1.5" fill="none" stroke="currentColor" strokeWidth="2" />
            <path d={open ? "M19 8l-4 4 4 4" : "M13 8l4 4-4 4"} fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </button>
      </div>
      {open ? (
        <div className="side-rest">
          <button className="primary" type="button" onClick={onAdd}>+ Add Vendor</button>
          <div className="vendor-head">
            <span>Vendors</span>
            <button type="button" aria-label="Find a vendor" onClick={() => setFinding((value) => !value)}>
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h16l-6.2 7.2V19l-3.6-1.8v-5L4 5z" fill="none" stroke="currentColor" strokeWidth="2" strokeLinejoin="round" /></svg>
            </button>
          </div>
          {finding ? <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Find a vendor" /> : null}
          <div className="vendors">
            {shown.map((vendor) => (
              <div className="vendor-line" key={vendor.vendor_id}>
                <button type="button" className={vendor.vendor_id === selectedId ? "vendor-btn current" : "vendor-btn"} onClick={() => onOpenVendor(vendor)}>
                  <Building /> {vendor.name}
                </button>
                <button type="button" className="vendor-remove" aria-label={"Delete " + vendor.name} onClick={() => onDeleteVendor(vendor)}>×</button>
              </div>
            ))}
            {finding && query && !shown.length ? <p className="vendor-empty">No matching vendor</p> : null}
          </div>
          <div className="side-foot">
            <button type="button" className={policyCurrent ? "nav-btn current" : "nav-btn"} onClick={onPolicy}>Company policy</button>
            <button type="button" className="nav-btn">Settings</button>
            <button type="button" className="nav-btn">Help & Support</button>
          </div>
        </div>
      ) : null}
    </aside>
  );
}
