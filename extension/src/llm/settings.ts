/**
 * Credential and configuration plumbing for the LLM layer.
 *
 * The API key is a secret: it is stored in VS Code SecretStorage (OS keychain)
 * and only ever held in memory while a request is in flight. Settings carry
 * the non-secret parts (endpoint, model, timeouts) so they can be synced with
 * workspace settings files without leaking credentials.
 */

import {
  DEFAULT_LLM_CONFIG,
  LlmProviderConfig,
  OpenAiCompatibleClient,
} from "./provider";

export const API_KEY_SECRET = "neura.llm.apiKey";

/** Minimal SecretStorage surface, so this module is testable without VS Code. */
export interface SecretStore {
  get(key: string): Thenable<string | undefined>;
  store(key: string, value: string): Thenable<void>;
  delete(key: string): Thenable<void>;
}

export interface LlmSettings {
  baseUrl: string;
  model: string;
  timeoutMs: number;
}

export interface LlmSettingsSource {
  get<T>(section: string, key: string): T | undefined;
}

export function readSettings(config: LlmSettingsSource): LlmSettings {
  const baseUrl = String(config.get("neura", "llm.baseUrl") ?? "").trim();
  const model = String(config.get("neura", "llm.model") ?? "").trim();
  const timeoutSeconds = Number(config.get("neura", "llm.timeoutSeconds") ?? 60);
  return {
    baseUrl: baseUrl || DEFAULT_LLM_CONFIG.baseUrl,
    model: model || DEFAULT_LLM_CONFIG.model,
    timeoutMs:
      Number.isFinite(timeoutSeconds) && timeoutSeconds > 0
        ? Math.min(timeoutSeconds, 600) * 1000
        : DEFAULT_LLM_CONFIG.timeoutMs,
  };
}

/** Reads the key from the secret store. Never logs or persists it elsewhere. */
export async function readApiKey(secrets: SecretStore): Promise<string> {
  const value = await secrets.get(API_KEY_SECRET);
  return typeof value === "string" ? value.trim() : "";
}

export async function storeApiKey(
  secrets: SecretStore,
  apiKey: string,
): Promise<void> {
  const trimmed = apiKey.trim();
  if (!trimmed) {
    await secrets.delete(API_KEY_SECRET);
    return;
  }
  await secrets.store(API_KEY_SECRET, trimmed);
}

export async function clearApiKey(secrets: SecretStore): Promise<void> {
  await secrets.delete(API_KEY_SECRET);
}

/** True when a key is configured (without revealing it). */
export async function hasApiKey(secrets: SecretStore): Promise<boolean> {
  return (await readApiKey(secrets)).length > 0;
}

/**
 * Builds a ready-to-use client, or throws a configuration error naming the
 * missing piece. Callers surface the message through the normal error path.
 */
export async function createClient(
  secrets: SecretStore,
  settings: LlmSettingsSource,
  fetchImpl?: typeof fetch,
): Promise<OpenAiCompatibleClient> {
  const [apiKey, config] = await Promise.all([
    readApiKey(secrets),
    Promise.resolve(readSettings(settings)),
  ]);
  if (!apiKey) {
    throw new Error(
      "No LLM API key is set. Run Neura: Set LLM API Key to add one.",
    );
  }
  const provider: LlmProviderConfig = { ...config, apiKey };
  return new OpenAiCompatibleClient(provider, fetchImpl);
}
