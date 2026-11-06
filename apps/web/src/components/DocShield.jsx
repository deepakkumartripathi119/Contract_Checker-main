import "./DocShield.css";

export default function DocShield({ className = "" }) {
  return (
    <svg className={"doc-shield " + className} viewBox="0 0 280 150" aria-hidden="true">
      <ellipse cx="140" cy="82" rx="108" ry="36" fill="#ececfe" />
      <circle cx="36" cy="36" r="5" fill="#ddd9fb" />
      <circle cx="244" cy="30" r="6" fill="#e4e0fb" />
      <circle cx="250" cy="112" r="4" fill="#ddd9fb" />
      <g transform="rotate(-12 96 78)">
        <rect x="42" y="22" width="92" height="108" rx="16" fill="#f7f5ff" stroke="#ddd6fb" />
        <path d="M88 46v18M70 64h36" stroke="#b7b0ea" strokeWidth="2.4" fill="none" strokeLinecap="round" />
        <path d="M70 64l-9 14h18zM106 64l-9 14h18z" fill="#d9d4f6" />
      </g>
      <g transform="rotate(11 186 76)">
        <rect x="148" y="18" width="92" height="108" rx="16" fill="#ffffff" stroke="#e4dff8" />
        <path d="M166 46h56M166 62h46M166 78h36" stroke="#e0daf6" strokeWidth="3.4" strokeLinecap="round" />
      </g>
      <path d="M140 44l28 11v22c0 18-13 28-28 35-15-7-28-17-28-35V55z" fill="#5653fa" />
      <path d="M127 76l10 10 20-20" fill="none" stroke="#fff" strokeWidth="3.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
