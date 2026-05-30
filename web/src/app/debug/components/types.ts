export type SearchResult = {
  conversation_id: string;
  status: string;
  answer: string;
  sources: Array<{ title?: string; url?: string; snippet?: string; source_type?: string }>;
};

export type ChatMessage = {
  role: "system" | "user" | "assistant";
  content: string;
};

export type ChatCompletionResponse = {
  choices?: Array<{ message?: { role?: string; content?: string } }>;
};

export const pretty = (value: unknown) => JSON.stringify(value, null, 2);
