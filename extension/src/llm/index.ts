/** Public surface of the LLM provider layer. */
export {
  ChatMessage,
  ChatOptions,
  ChatResult,
  DEFAULT_LLM_CONFIG,
  LlmError,
  LlmErrorKind,
  LlmProviderConfig,
  OpenAiCompatibleClient,
  Role,
} from "./provider";
export {
  API_KEY_SECRET,
  clearApiKey,
  createClient,
  hasApiKey,
  LlmSettings,
  LlmSettingsSource,
  readApiKey,
  readSettings,
  SecretStore,
  storeApiKey,
} from "./settings";
