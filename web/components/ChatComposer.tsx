"use client";

import { ArrowUp, FileText, Paperclip, X } from "lucide-react";
import { useId, useRef } from "react";
import type { ContextUsage } from "@/lib/types";
import { BusyButton } from "./ui";
import { ContextWindowUsage } from "./ContextWindowUsage";

const ACCEPTED_DOCUMENTS = ".pdf,.docx,.md,.txt,.dxf,.dwg";

export function ChatComposer({
  draft,
  attachments,
  busy,
  disabled,
  usage,
  model,
  placeholder,
  onDraftChange,
  onAttachmentsChange,
  onRemoveAttachment,
  onSubmit,
}: {
  draft: string;
  attachments: File[];
  busy: boolean;
  disabled: boolean;
  usage: ContextUsage;
  model?: string;
  placeholder: string;
  onDraftChange: (value: string) => void;
  onAttachmentsChange: (files: File[]) => void;
  onRemoveAttachment: (file: File) => void;
  onSubmit: () => void;
}) {
  const inputId = useId();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const canSubmit = Boolean(draft.trim() || attachments.length) && !disabled;
  const actionLabel = "发送消息";

  return (
    <form
      className="chat-composer"
      onSubmit={(event) => {
        event.preventDefault();
        if (canSubmit) onSubmit();
      }}
    >
      <div className="chat-composer-shell">
        {attachments.length ? <div className="composer-file-row">
          {attachments.map((file, index) => (
            <span className="composer-file-chip" key={`${file.name}-${file.lastModified}-${index}`} title={file.name}>
              <FileText size={13} aria-hidden="true" />
              <b>{file.name}</b>
              <button type="button" onClick={() => onRemoveAttachment(file)} disabled={disabled} aria-label={`移除 ${file.name}`}><X size={12} /></button>
            </span>
          ))}
        </div> : null}
        <textarea value={draft} onChange={(event) => onDraftChange(event.target.value)} rows={2} disabled={disabled} placeholder={placeholder} />
        <div className="chat-composer-footer">
          <div className="composer-attachments">
            <input
              ref={fileInputRef}
              id={inputId}
              className="composer-file-input"
              type="file"
              accept={ACCEPTED_DOCUMENTS}
              multiple
              onChange={(event) => {
                const files = Array.from(event.target.files ?? []);
                if (files.length) onAttachmentsChange([...attachments, ...files]);
                event.target.value = "";
              }}
              disabled={disabled}
            />
            <button className="composer-attach-button" type="button" disabled={disabled} onClick={() => fileInputRef.current?.click()} title="添加资料或 CAD 平面图">
              <Paperclip size={16} /><span>添加文件</span>
            </button>
          </div>
          <div className="composer-actions">
            <ContextWindowUsage usage={usage} />
            <span className="composer-model" title={`当前模型：${model ?? "未配置"}`}><i aria-hidden="true" />{model ?? "未配置模型"}</span>
            <BusyButton className="composer-submit" busy={busy} disabled={!canSubmit} type="submit" title={actionLabel} aria-label={actionLabel}>{!busy ? <ArrowUp size={17} /> : null}</BusyButton>
          </div>
        </div>
      </div>
    </form>
  );
}
