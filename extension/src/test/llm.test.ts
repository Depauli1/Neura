import { strict as assert } from "node:assert";
import { createServer, Server } from "node:http";
import { AddressInfo } from "node:net";
import { test } from "node:test";
import {
  createClient,
  LlmError,
  LlmSettingsSource,
  OpenAiCompatibleClient,
  SecretStore,
} from "../llm";

function memorySecrets(initial = ""): SecretStore & { readonly value: () => string } {
  let value = initial;
  return {
    get: async () => (value ? value : undefined),
    store: async (_key: string, next: string) => {
      value = next;
    },
    delete: async () => {
      value = "";
    },
    value: () => value,
  };
}
function settings(overrides: Record<string, unknown> = {}): LlmSettingsSource {
  return {
    get: <T>(section: string, key: string) =>
      (overrides[`${section}.${key}`] ?? undefined) as T | undefined,
  };
}
interface Stub {
  url: string;
  requests: { auth?: string; body: Record<string, unknown> }[];
  close(): Promise<void>;
}
type StubReply = { status?: number; payload?: unknown; raw?: string };
async function stub(
  handler: (
    body: Record<string, unknown>,
    auth: string | undefined,
  ) => StubReply | Promise<StubReply>,
): Promise<Stub> {
  const requests: Stub["requests"] = [];
  const server: Server = createServer((req, res) => {
    let body = "";
    req.on("data", (chunk) => (body += chunk));
    req.on("end", async () => {
      const parsed = body ? JSON.parse(body) : {};
      requests.push({
        auth: req.headers.authorization,
        body: parsed,
      });
      const result = await handler(parsed, req.headers.authorization);
      res.writeHead(result.status ?? 200, {
        "Content-Type": "application/json",
      });
      res.end(result.raw ?? JSON.stringify(result.payload ?? {}));
    });
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address() as AddressInfo;
  return {
    url: `http://127.0.0.1:${port}/v1`,
    requests,
    close: () =>
      new Promise<void>((resolve, reject) =>
        server.close((error) => (error ? reject(error) : resolve())),
      ),
  };
}

test("chat completions post an OpenAI-compatible request and parse the reply", async () => {
  const server = await stub(() => ({
    payload: {
      id: "cmpl-1",
      model: "gpt-4o-mini",
      choices: [
        {
          message: { role: "assistant", content: "42" },
          finish_reason: "stop",
        },
      ],
      usage: { prompt_tokens: 11, completion_tokens: 2, total_tokens: 13 },
    },
  }));
  try {
    const client = new OpenAiCompatibleClient({
      baseUrl: server.url,
      model: "gpt-4o-mini",
      apiKey: "sk-test-key",
      timeoutMs: 5000,
    });
    const result = await client.chat(
      [
        { role: "system", content: "Be terse." },
        { role: "user", content: "6 * 7" },
      ],
      { temperature: 0, maxTokens: 32, stop: ["\n"] },
    );
    assert.equal(result.content, "42");
    assert.equal(result.finishReason, "stop");
    assert.equal(result.usage?.totalTokens, 13);
    assert.equal(result.model, "gpt-4o-mini");
    const request = server.requests[0];
    assert.equal(request.auth, "Bearer sk-test-key");
    assert.equal(request.body.model, "gpt-4o-mini");
    assert.equal(request.body.stream, false);
    assert.equal(request.body.temperature, 0);
    assert.equal(request.body.max_tokens, 32);
    assert.deepEqual(request.body.stop, ["\n"]);
    assert.equal((request.body.messages as unknown[]).length, 2);
  } finally {
    await server.close();
  }
});

test("settings resolve endpoint, model and timeout with safe defaults", async () => {
  const secrets = memorySecrets("sk-from-secret");
  const client = await createClient(secrets, settings(), fetch);
  assert.equal(client.baseUrl, "https://api.openai.com/v1");
  assert.equal(client.model, "gpt-4o-mini");
  const custom = await createClient(
    secrets,
    settings({
      "neura.llm.baseUrl": "http://127.0.0.1:9/v1/",
      "neura.llm.model": "local-model",
      "neura.llm.timeoutSeconds": 120,
    }),
    fetch,
  );
  assert.equal(custom.baseUrl, "http://127.0.0.1:9/v1");
  assert.equal(custom.model, "local-model");
});

test("a missing API key fails with a configuration error before any request", async () => {
  const secrets = memorySecrets();
  await assert.rejects(
    () => createClient(secrets, settings(), fetch),
    /No LLM API key is set/,
  );
  assert.equal(secrets.value(), "");
  assert.throws(
    () =>
      new OpenAiCompatibleClient({
        baseUrl: "https://api.openai.com/v1",
        model: "m",
        apiKey: "",
        timeoutMs: 1000,
      }),
    (error: unknown) =>
      error instanceof LlmError && error.kind === "configuration",
  );
});

test("authentication, rate-limit and server errors map to their kinds", async () => {
  for (const [status, kind] of [
    [401, "authentication"],
    [403, "authentication"],
    [429, "rateLimit"],
    [500, "server"],
    [503, "server"],
  ] as const) {
    const server = await stub(() => ({
      status,
      payload: { error: { message: "nope" } },
    }));
    try {
      const client = new OpenAiCompatibleClient({
        baseUrl: server.url,
        model: "m",
        apiKey: "sk-secret-value",
        timeoutMs: 5000,
      });
      await assert.rejects(
        () => client.chat([{ role: "user", content: "hi" }]),
        (error: unknown) => {
          assert(error instanceof LlmError, `status ${status}`);
          assert.equal(error.kind, kind);
          assert.equal(error.status, status);
          // The key must never appear in a surfaced message.
          assert(!error.message.includes("sk-secret-value"));
          return true;
        },
      );
    } finally {
      await server.close();
    }
  }
});

test("network failures, invalid JSON and empty choices are protocol-safe", async () => {
  const server = await stub(() => ({ raw: "not json at all" }));
  try {
    const client = new OpenAiCompatibleClient({
      baseUrl: server.url,
      model: "m",
      apiKey: "k",
      timeoutMs: 5000,
    });
    await assert.rejects(
      () => client.chat([{ role: "user", content: "hi" }]),
      (error: unknown) =>
        error instanceof LlmError && error.kind === "protocol",
    );
  } finally {
    await server.close();
  }
  const empty = await stub(() => ({ payload: { choices: [] } }));
  try {
    const client = new OpenAiCompatibleClient({
      baseUrl: empty.url,
      model: "m",
      apiKey: "k",
      timeoutMs: 5000,
    });
    await assert.rejects(
      () => client.chat([{ role: "user", content: "hi" }]),
      /no choices/,
    );
  } finally {
    await empty.close();
  }
  const unreachable = new OpenAiCompatibleClient({
    baseUrl: "http://127.0.0.1:1/v1",
    model: "m",
    apiKey: "k",
    timeoutMs: 3000,
  });
  await assert.rejects(
    () => unreachable.chat([{ role: "user", content: "hi" }]),
    (error: unknown) =>
      error instanceof LlmError &&
      (error.kind === "network" || error.kind === "aborted"),
  );
});

test("timeouts abort the in-flight request", async () => {
  const server = await stub(
    () =>
      new Promise<{ payload: unknown }>(() => {
        /* never responds */
      }),
  );
  try {
    const client = new OpenAiCompatibleClient({
      baseUrl: server.url,
      model: "m",
      apiKey: "k",
      timeoutMs: 120,
    });
    await assert.rejects(
      () => client.chat([{ role: "user", content: "hi" }]),
      (error: unknown) =>
        error instanceof LlmError && error.kind === "aborted",
    );
  } finally {
    await server.close();
  }
});

test("secrets are stored, read back and cleared without touching settings", async () => {
  const secrets = memorySecrets();
  const { storeApiKey, readApiKey, clearApiKey, hasApiKey } = await import(
    "../llm"
  );
  assert.equal(await hasApiKey(secrets), false);
  await storeApiKey(secrets, "  sk-rotated  ");
  assert.equal(await readApiKey(secrets), "sk-rotated");
  assert.equal(await hasApiKey(secrets), true);
  await clearApiKey(secrets);
  assert.equal(await readApiKey(secrets), "");
  assert.equal(secrets.value(), "");
});
