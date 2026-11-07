import { json, request } from "./client";
import type {
  AnalysisPayload,
  Citation,
  RunResult,
  EpisodeOutcome,
  ProfileOutcome,
} from "./workflows";
import type { SafeNodeTrace } from "../workflow/runTrace";

export type Message = {
  role: "user" | "assistant";
  content: string;
  citations?: Citation[];
  analysis?: AnalysisPayload;
  abstained?: boolean;
  /** 拒答原因码，决定界面徽标写“证据不足”还是“生成异常/输出截断”。 */
  abstainReason?: string;
  /** 服务端权威回答类型；旧记录缺失时按普通回答解释。 */
  answerType?: "answer" | "clarification";
  runId?: string;
  trace?: SafeNodeTrace[];
  pending?: boolean;
  kind?: RunResult["kind"];
  profile?: ProfileOutcome;
  episode?: EpisodeOutcome;
  memory?: { status: string };
  error?: RunResult["error"];
};

export type Conversation = {
  id: string;
  title: string;
  messages: Message[];
  failedRuns?: { runId: string; trace: SafeNodeTrace[] }[];
};

export const getConversations = () => request<Conversation[]>("/conversations");
export const getConversation = (id: string) =>
  request<Conversation>(`/conversations/${encodeURIComponent(id)}`);
export const importConversations = (conversations: Conversation[]) =>
  request<Conversation[]>("/conversations/import", json({ conversations }));
