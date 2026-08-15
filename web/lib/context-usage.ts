import type { ContextUsage } from "./types";

const DEFAULT_CONTEXT_WINDOW_TOKENS = 1_000_000;

export function unavailableContextUsage(
  contextWindowTokens?: number,
): ContextUsage {
  return {
    input_tokens: null,
    output_tokens: null,
    total_tokens: null,
    context_window_tokens: Math.max(1, contextWindowTokens ?? DEFAULT_CONTEXT_WINDOW_TOKENS),
    source: "unavailable",
  };
}
