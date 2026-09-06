import type { Health, ReasoningEffort, ReasoningEffortOption } from "./types";

export const DEFAULT_REASONING_EFFORT_OPTIONS: ReasoningEffortOption[] = [
  { value: "none", label: "关闭", description: "不启用额外推理，响应最快" },
  { value: "low", label: "低", description: "快速分析，适合简单问题" },
  { value: "medium", label: "中", description: "速度与准确性平衡，推荐" },
  { value: "high", label: "高", description: "更深分析，通常更慢且消耗更多 token" },
];

const knownEfforts = new Set<ReasoningEffort>(DEFAULT_REASONING_EFFORT_OPTIONS.map((item) => item.value));

export function reasoningOptionsForHealth(health: Health | null): ReasoningEffortOption[] {
  const options = health?.llm_reasoning_effort_options?.filter((item) => knownEfforts.has(item.value));
  if (options?.length) return options;

  const values = health?.llm_reasoning_efforts?.filter((value) => knownEfforts.has(value));
  if (values?.length) return values.map((value) => DEFAULT_REASONING_EFFORT_OPTIONS.find((item) => item.value === value)!);

  return DEFAULT_REASONING_EFFORT_OPTIONS;
}

export function defaultReasoningEffort(
  health: Health | null,
  options: ReasoningEffortOption[],
): ReasoningEffort {
  const requested = health?.llm_reasoning_effort_default;
  return requested && options.some((option) => option.value === requested)
    ? requested
    : options[0]?.value ?? "medium";
}
