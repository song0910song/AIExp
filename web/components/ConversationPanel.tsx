import type { Health, Project } from "@/lib/types";
import { SmartConversation } from "./SmartConversation";

export function ConversationPanel({
  project,
  health,
  onProject,
}: {
  project: Project;
  health: Health | null;
  onProject: (project: Project) => void;
}) {
  return <SmartConversation project={project} health={health} onProject={onProject} />;
}
