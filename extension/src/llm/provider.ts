/**
 * LLM provider layer (V1): one OpenAI-compatible chat-completions client with
 * bring-your-own-key credentials.
 *
 * Design constraints:
 * - The user supplies the API key; it lives in VS Code SecretStorage and is
 *   never written to settings, logs, or the runtime/bridge HTTP surface.
 * - Any OpenAI-compatible endpoint works (OpenAI, Azure-compatible gateways,
 *   Ollama, vLLM, LM Studio, …) because only `/chat/completions` is used.
 * - The layer is transport only. It has no Neura-specific prompt policy; the
 *   agent layer composes prompts and decides what runtime context to send.
 */

export type Role = "system" | "user" | "assistant" | "tool";

export interface ChatMessage {
  role: Role;
  content: string;
  /** Optional name for `tool`/`user` messages. */
  name?: string;
}

export interface ChatOptions {
  model?: string;
  temperature?: number;
  maxTokens?: number;
  /** Requested stop sequences. */
  stop?: string[];
  signal?: AbortSignal;
}

export interface ChatResult {
  id: string;
  model: string;
  content: string;
  finishReason: string | null;
  usage?: {
    promptTokens: number;
    completionTokens: number;
    totalTokens: number;
  };
}

export type LlmErrorKind =
  | "configuration"
  | "authentication"
  | "rateLimit"
  | "server"
  | "network"
  | "aborted"
  | "protocol";

export class LlmError extends Error {
  constructor(
    public readonly kind: LlmErrorKind,
    message: string,
    public readonly status?: number,
  ) {
    super(message);
    this.name = "LlmError";
  }
}

export interface LlmProviderConfig {
  /** Base URL without a trailing slash, e.g. https://api.openai.com/v1. */
  baseUrl: string;
  /** Default model when a call does not override it. */
  model: string;
  /** Bearer token. Empty means "not configured". */
  apiKey: string;
  /** Per-request timeout in milliseconds. */
  timeoutMs: number;
}

export const DEFAULT_LLM_CONFIG = {
  baseUrl: "https://api.openai.com/v1",
  model: "gpt-4o-mini",
  timeoutMs: 60000,
} as const;

const MAX_RESPONSE_BYTES = 8 * 1024 * 1024;

function redact(text: string, apiKey: string): string {
  if (!apiKey) {
    return text;
  }
  return text.split(apiKey).join("[redacted]");
}

function endpoint(baseUrl: string): string {
  const trimmed = baseUrl.trim().replace(/\/+$/, "");
  if (!/^https?:\/\//i.test(trimmed)) {
    throw new LlmError(
      "configuration",
      "The LLM base URL must start with http:// or https://.",
    );
  }
  return `${trimmed}/chat/completions`;
}

/**
 * One OpenAI-compatible chat-completions client.
 *
 * Kept free of VS Code imports so it can be unit-tested against a stub HTTP
 * server, and so a future non-extension host can reuse it.
 */
export class OpenAiCompatibleClient {
  constructor(
    private readonly config: LlmProviderConfig,
    private readonly fetchImpl: typeof fetch = fetch,
  ) {
    if (!config.apiKey) {
      throw new LlmError(
        "configuration",
        "No LLM API key is set. Run Neura: Set LLM API Key first.",
      );
    }
    // Canonicalize once so callers can compare/log the endpoint safely.
    this.config = { ...config, baseUrl: config.baseUrl.trim().replace(/\/+$/, "") };
  }

  get model(): string {
    return this.config.model;
  }

  get baseUrl(): string {
    return this.config.baseUrl;
  }

  async chat(
    messages: readonly ChatMessage[],
    options: ChatOptions = {},
  ): Promise<ChatResult> {
    if (!messages.length) {
      throw new LlmError("configuration", "Send at least one message.");
    }
    const model = options.model ?? this.config.model;
    if (!model) {
      throw new LlmError("configuration", "No LLM model is configured.");
    }
    const body: Record<string, unknown> = {
      model,
      messages: messages.map((message) => ({
        role: message.role,
        content: message.content,
        ...(message.name ? { name: message.name } : {}),
      })),
      stream: false,
    };
    if (typeof options.temperature === "number") {
      body.temperature = options.temperature;
    }
    if (typeof options.maxTokens === "number") {
      body.max_tokens = options.maxTokens;
    }
    if (options.stop?.length) {
      body.stop = options.stop;
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.config.timeoutMs);
    const onExternalAbort = () => controller.abort();
    options.signal?.addEventListener("abort", onExternalAbort);
    let response: Response;
    try {
      response = await this.fetchImpl(endpoint(this.config.baseUrl), {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${this.config.apiKey}`,
        },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
    } catch (error) {
      if (controller.signal.aborted) {
        throw new LlmError(
          "aborted",
          options.signal?.aborted
            ? "The LLM request was cancelled."
            : "The LLM request timed out.",
        );
      }
      throw new LlmError(
        "network",
        redact(error instanceof Error ? error.message : String(error), this.config.apiKey),
      );
    } finally {
      clearTimeout(timer);
      options.signal?.removeEventListener("abort", onExternalAbort);
    }
    const text = await response.text();
    if (!response.ok) {
      throw this.httpError(response.status, text);
    }
    if (text.length > MAX_RESPONSE_BYTES) {
      throw new LlmError("protocol", "The LLM response is too large.");
    }
    let payload: unknown;
    try {
      payload = JSON.parse(text);
    } catch {
      throw new LlmError("protocol", "The LLM returned invalid JSON.");
    }
    return parseCompletion(payload, model);
  }

  private httpError(status: number, text: string): LlmError {
    const detail = redact(text.slice(0, 2000), this.config.apiKey);
    const message = (() => {
      try {
        const parsed = JSON.parse(text) as { error?: { message?: string } };
        return parsed.error?.message ?? detail;
      } catch {
        return detail || `HTTP ${status}`;
      }
    })();
    if (status === 401 || status === 403) {
      return new LlmError(
        "authentication",
        `The LLM provider rejected the API key (HTTP ${status}): ${message}`,
        status,
      );
    }
    if (status === 429) {
      return new LlmError(
        "rateLimit",
        `The LLM provider is rate limiting requests (HTTP 429): ${message}`,
        status,
      );
    }
    if (status >= 500) {
      return new LlmError(
        "server",
        `The LLM provider failed (HTTP ${status}): ${message}`,
        status,
      );
    }
    return new LlmError("protocol", `LLM request failed (HTTP ${status}): ${message}`, status);
  }
}

function parseCompletion(payload: unknown, model: string): ChatResult {
  if (!payload || typeof payload !== "object") {
    throw new LlmError("protocol", "The LLM response was not a JSON object.");
  }
  const data = payload as Record<string, unknown>;
  const choices = data.choices;
  if (!Array.isArray(choices) || !choices.length) {
    throw new LlmError("protocol", "The LLM response contained no choices.");
  }
  const first = choices[0] as Record<string, unknown>;
  const message = (first.message ?? {}) as Record<string, unknown>;
  const content = message.content;
  const usage = data.usage as Record<string, unknown> | undefined;
  return {
    id: String(data.id ?? ""),
    model: String(data.model ?? model),
    content:
      typeof content === "string"
        ? content
        : Array.isArray(content)
          ? content
              .map((part) =>
                part && typeof part === "object" && "text" in part
                  ? String((part as { text: unknown }).text)
                  : "",
              )
              .join("")
          : "",
    finishReason:
      typeof first.finish_reason === "string" ? first.finish_reason : null,
    usage: usage
      ? {
          promptTokens: Number(usage.prompt_tokens ?? 0),
          completionTokens: Number(usage.completion_tokens ?? 0),
          totalTokens: Number(usage.total_tokens ?? 0),
        }
      : undefined,
  };
}
