import "./Dialogs.css";

function Close({ onClick }) {
  return (
    <button className="dialog-x" type="button" aria-label="Close" onClick={onClick}>
      <svg viewBox="0 0 16 16" aria-hidden="true">
        <path d="M4 4l8 8M12 4L4 12" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
      </svg>
    </button>
  );
}

export function VendorDialog({ open, onClose, onCreate }) {
  if (!open) return null;
  return (
    <div className="modal" role="presentation" onMouseDown={onClose}>
      <form className="sheet" onMouseDown={(event) => event.stopPropagation()} onSubmit={(event) => { event.preventDefault(); onCreate(new FormData(event.target).get("name")); }}>
        <div className="dialog-top"><h2>Add Vendor</h2><Close onClick={onClose} /></div>
        <label>Vendor name <input name="name" required autoComplete="organization" /></label>
        <button className="primary" type="submit">Create vendor</button>
      </form>
    </div>
  );
}

export function ClauseDialog({ open, onClose, onSend }) {
  if (!open) return null;
  return (
    <div className="modal" role="presentation" onMouseDown={onClose}>
      <form className="sheet" onMouseDown={(event) => event.stopPropagation()} onSubmit={(event) => {
        event.preventDefault();
        const data = new FormData(event.target);
        onSend(String(data.get("clause_type") || ""), String(data.get("clause_text") || ""));
      }}>
        <div className="dialog-top"><h2>Add Single Clause</h2><Close onClick={onClose} /></div>
        <p className="quiet">Write a heading and the clause text, then send it to be checked.</p>
        <label>Heading <input name="clause_type" required maxLength={255} placeholder="Heading" /></label>
        <label>Clause text <textarea name="clause_text" required maxLength={10000} rows={6} placeholder="Clause text" /></label>
        <button className="primary" type="submit">Send clause</button>
      </form>
    </div>
  );
}

export function ConfirmDialog({ open, title, text, onClose, onYes }) {
  if (!open) return null;
  return (
    <div className="modal" role="presentation" onMouseDown={onClose}>
      <form className="sheet" onMouseDown={(event) => event.stopPropagation()} onSubmit={(event) => { event.preventDefault(); onYes(); }}>
        <div className="dialog-top"><h2>{title}</h2><Close onClick={onClose} /></div>
        <p className="quiet">{text}</p>
        <button className="primary" type="submit">Delete</button>
      </form>
    </div>
  );
}
