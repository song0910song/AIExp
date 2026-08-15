"use client";

import type { ReactNode } from "react";
import { AlertCircle, Check, LoaderCircle, X } from "lucide-react";

export function Panel({
  title,
  eyebrow,
  action,
  children,
  className = "",
}: {
  title: string;
  eyebrow?: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel ${className}`}>
      <header className="panel-header">
        <div>
          {eyebrow ? <p className="eyebrow">{eyebrow}</p> : null}
          <h2>{title}</h2>
        </div>
        {action ? <div className="panel-action">{action}</div> : null}
      </header>
      <div className="panel-body">{children}</div>
    </section>
  );
}

export function Field({
  label,
  hint,
  children,
  wide = false,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
  wide?: boolean;
}) {
  return (
    <label className={`field ${wide ? "field-wide" : ""}`}>
      <span className="field-label">{label}</span>
      {children}
      {hint ? <span className="field-hint">{hint}</span> : null}
    </label>
  );
}

export function EmptyState({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="empty-state">
      <div className="empty-state-mark" aria-hidden="true">
        <span />
        <span />
        <span />
      </div>
      <h3>{title}</h3>
      <p>{children}</p>
    </div>
  );
}

export function BusyButton({
  busy,
  children,
  className = "button button-primary",
  disabled,
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { busy?: boolean }) {
  return (
    <button className={className} {...props} disabled={busy || disabled}>
      {busy ? <LoaderCircle size={16} className="spin" aria-hidden="true" /> : null}
      {children}
    </button>
  );
}

export function StatusPill({
  status,
  children,
}: {
  status: "success" | "warning" | "neutral" | "danger";
  children: ReactNode;
}) {
  return <span className={`status-pill status-${status}`}>{children}</span>;
}

export function Notice({ tone = "info", children }: { tone?: "info" | "danger" | "success"; children: ReactNode }) {
  return (
    <div className={`notice notice-${tone}`}>
      {tone === "success" ? <Check size={16} /> : <AlertCircle size={16} />}
      <div>{children}</div>
    </div>
  );
}

export function Modal({ title, eyebrow = "NEW PROJECT", children, onClose }: { title: string; eyebrow?: string; children: ReactNode; onClose: () => void }) {
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={onClose}>
      <section className="modal" role="dialog" aria-modal="true" aria-label={title} onMouseDown={(event) => event.stopPropagation()}>
        <header className="modal-header">
          <div>
            <p className="eyebrow">{eyebrow}</p>
            <h2>{title}</h2>
          </div>
          <button className="icon-button" onClick={onClose} aria-label="关闭">
            <X size={18} />
          </button>
        </header>
        <div className="modal-body">{children}</div>
      </section>
    </div>
  );
}

export function formatNumber(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined) return "—";
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: digits }).format(value);
}

export function toNullableNumber(value: string): number | null {
  return value.trim() === "" ? null : Number(value);
}
