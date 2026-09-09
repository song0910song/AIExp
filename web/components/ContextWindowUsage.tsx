import type { CSSProperties } from "react";
import type { ContextUsage } from "@/lib/types";

function formatTokens(value: number | null | undefined) {
  if (value === null || value === undefined) return "--";
  return new Intl.NumberFormat("zh-CN", { notation: value >= 10_000 ? "compact" : "standard", maximumFractionDigits: 1 }).format(value);
}

export function ContextWindowUsage({ usage }: { usage: ContextUsage }) {
  const occupied = usage.total_tokens ?? usage.input_tokens;
  if (usage.source !== "reported" || occupied === null) return null;

  const percentage = Math.min(100, Math.round((occupied / usage.context_window_tokens) * 100));
  const sourceLabel = "模型实际返回";
  const cacheSummary = usage.cache_hit_ratio === undefined
    ? usage.cached_input_tokens === undefined
      ? ""
      : ` · 缓存读取 ${formatTokens(usage.cached_input_tokens)}`
    : ` · 缓存命中 ${Math.round(usage.cache_hit_ratio * 100)}%`;
  const cacheTitle = usage.cached_input_tokens === undefined
    ? ""
    : `，缓存读取 ${formatTokens(usage.cached_input_tokens)}${usage.cache_creation_input_tokens === undefined ? "" : `，缓存写入 ${formatTokens(usage.cache_creation_input_tokens)}`}`;
  const ringStyle = { "--context-progress": `${percentage}%` } as CSSProperties;

  return (
    <div
      className="context-window-usage"
      title={`${sourceLabel}：输入 ${formatTokens(usage.input_tokens)}，输出 ${formatTokens(usage.output_tokens)}${cacheTitle}，窗口上限 ${formatTokens(usage.context_window_tokens)} tokens`}
    >
      <span className="context-window-ring" style={ringStyle} aria-hidden="true"><i /></span>
      <div className="context-window-copy">
        <span>CTX</span>
        <strong>{formatTokens(occupied)} / {formatTokens(usage.context_window_tokens)}</strong>
        <em>{percentage}% · {sourceLabel}{cacheSummary}</em>
      </div>
    </div>
  );
}
