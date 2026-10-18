// npm test (node --test test/worker.test.mjs)
// Checks the Worker's flow with a fake message and fetch (Node 20+).
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { test } from "node:test";

import { deliver, sign } from "../src/index.js";

const raw = new TextEncoder().encode("From: a@b.example\r\nSubject: hi\r\n\r\nbody\r\n");

function fakeMessage({ forwardFails = false } = {}) {
  return {
    from: "a@b.example", to: "contact@crontal.ai",
    headers: new Headers({ "authentication-results": "mx.cloudflare.net; dkim=pass" }),
    raw: new Blob([raw]).stream(),
    forwarded: [],
    async forward(to) {
      if (forwardFails) throw new Error("destination not verified");
      this.forwarded.push(to);
    },
  };
}

const env = { FORWARD_TO: "crontal.contacts@gmail.com", HANDLER_EMAIL_SECRET: "s3",
              HANDLER_INGEST_URL: "https://handler.example/ingest/email" };

test("signature matches the handler's construction", async () => {
  const expected = createHmac("sha256", "s3").update(Buffer.concat([
    Buffer.from("1700000000."), Buffer.from(raw)])).digest("hex");
  assert.equal(await sign("s3", "1700000000", raw), "v1=" + expected);
});

test("forwards first, then posts the raw bytes", async () => {
  const message = fakeMessage();
  let request;
  const result = await deliver(message, env, async (url, init) => {
    request = { url, init };
    return new Response("{}", { status: 202 });
  });
  assert.deepEqual(message.forwarded, ["crontal.contacts@gmail.com"]);
  assert.equal(result.posted, true);
  assert.equal(request.url, env.HANDLER_INGEST_URL);
  assert.deepEqual(new Uint8Array(request.init.body), raw);
  const h = request.init.headers;
  assert.equal(h["x-handler-envelope-from"], "a%40b.example");
  assert.equal(h["x-handler-forwarded"], "ok");
  assert.equal(h["x-handler-signature"], await sign("s3", h["x-handler-timestamp"], raw));
  const auth = JSON.parse(Buffer.from(h["x-handler-auth-results"], "base64").toString());
  assert.equal(auth.authentication_results, "mx.cloudflare.net; dkim=pass");
});

test("a failed forward or POST never throws", async () => {
  const result = await deliver(fakeMessage({ forwardFails: true }), env, async () => {
    throw new Error("connection refused");
  });
  assert.equal(result.posted, false);
  assert.match(result.forwarded, /^failed: destination not verified/);
  const rejected = await deliver(fakeMessage(), env,
    async () => new Response("bad", { status: 401 }));
  assert.deepEqual([rejected.posted, rejected.status], [false, 401]);
});
