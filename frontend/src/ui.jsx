// Shared primitives, lifted verbatim out of App.jsx so that the IT chain view
// and the Phase 6 showcase can both use them without one importing the other.
// Behaviour is unchanged; only the location is new.

export function Card({ className = "", children }) {
  return <section className={`card ${className}`}>{children}</section>;
}

export function PanelHeader({ icon: Icon, title, kicker }) {
  return (
    <div className="panel-header">
      <div className="panel-title">
        <Icon size={18} />
        <h2>{title}</h2>
      </div>
      {kicker ? <span className="kicker">{kicker}</span> : null}
    </div>
  );
}

export function IconButton({ icon: Icon, label, onClick, disabled = false, variant = "primary" }) {
  return (
    <button className={`action-button ${variant}`} type="button" onClick={onClick} disabled={disabled}>
      <Icon size={16} />
      {label}
    </button>
  );
}

export function Status({ value }) {
  return <span className={`status ${value || ""}`}>{value || "unknown"}</span>;
}

export function short(value, limit = 160) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  return text.length > limit ? `${text.slice(0, limit - 3)}...` : text;
}
