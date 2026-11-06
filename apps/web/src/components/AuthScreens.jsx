import { useState } from "react";
import Logo from "./Logo";
import "./AuthScreens.css";

const SIZES = ["1–10", "11–50", "51–200", "201–500", "501–1,000", "1,000+"];

export function SignIn({ notice, onSubmit, onRegister }) {
  return (
    <section className="auth-card">
      <Logo size={40} />
      <h1>Welcome Back</h1>
      <p className="quiet">Sign in to continue to your dashboard</p>
      {notice ? <p className="error">{notice}</p> : null}
      <form onSubmit={onSubmit}>
        <input name="login_name" required autoComplete="username" placeholder="Username or Email Address" />
        <input name="password" type="password" required autoComplete="current-password" placeholder="Password" />
        <button className="primary" type="submit">Sign In</button>
      </form>
      <p className="switch">Don't have an account? <button type="button" onClick={onRegister}>Register Now</button></p>
    </section>
  );
}

export function Register({ onSubmit, onSignIn, error }) {
  const [size, setSize] = useState("");
  return (
    <section className="auth-card">
      <Logo size={40} />
      <h1>Register Your Company</h1>
      <p className="quiet">Tell us about your organization to get started</p>
      <form onSubmit={(event) => { event.preventDefault(); if (!size) return; onSubmit(event, size); }}>
        <input name="name" required autoComplete="organization" placeholder="Company Name" />
        <input name="business_email" type="email" required autoComplete="email" placeholder="Business Email" />
        <input name="industry" required placeholder="Industry" />
        <select value={size} onChange={(event) => setSize(event.target.value)} required>
          <option value="">Company Size</option>
          {SIZES.map((item) => <option key={item} value={item}>{item}</option>)}
        </select>
        <input name="headquarters" required placeholder="Headquarters Location" />
        <input name="website" autoComplete="url" placeholder="Website (Optional)" />
        <input name="password" type="password" required minLength={8} autoComplete="new-password" placeholder="Password" />
        {error ? <p className="error">{error}</p> : null}
        <button className="primary" type="submit">Register Company</button>
      </form>
      <p className="switch">Already have an account? <button type="button" onClick={onSignIn}>Sign In</button></p>
    </section>
  );
}
