// Cloudflare Email Worker for contact@crontal.ai.
//
// 1. Forward every message to the Gmail destination first (unchanged
//    behaviour: mail is never lost, even if handler is down). The raw bytes
//    are buffered in memory just before, see deliver().
// 2. Then POST the raw RFC 822 bytes to handler's /ingest/email, signed with
//    HMAC-SHA256 over "<timestamp>." + body (secret HANDLER_EMAIL_SECRET).
//
// The message is never rejected or bounced from here: a failed POST is
// logged (Workers Logs) and the Gmail copy remains the record.

const encoder = new TextEncoder();

export function toHex(buffer) {
  return [...new Uint8Array(buffer)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export async function sign(secret, timestamp, body) {
  const key = await crypto.subtle.importKey(
    "raw", encoder.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const prefix = encoder.encode(`${timestamp}.`);
  const data = new Uint8Array(prefix.length + body.length);
  data.set(prefix, 0);
  data.set(body, prefix.length);
  return "v1=" + toHex(await crypto.subtle.sign("HMAC", key, data));
}

// Auth headers as the Worker sees them. Informational only: Cloudflare does
// not reliably expose its own verdict here, and a sender can forge these
// headers, so handler verifies DKIM on the raw bytes itself.
function authHeaders(headers) {
  const pick = (name) => headers.get(name);
  const json = JSON.stringify({
    authentication_results: pick("authentication-results"),
    arc_authentication_results: pick("arc-authentication-results"),
    received_spf: pick("received-spf"),
  });
  return btoa(String.fromCharCode(...encoder.encode(json)));
}

export async function deliver(message, env, fetchImpl = fetch) {
  // Buffer the raw bytes before forwarding: whether message.raw can still be
  // read after forward() isn't documented, while reading first is the
  // pattern Cloudflare's own examples use. Reading can't lose the message.
  let body = null;
  try {
    body = new Uint8Array(await new Response(message.raw).arrayBuffer());
  } catch (err) {
    console.error("could not read message.raw", { error: String(err && err.message || err) });
  }

  let forwarded = "skipped: FORWARD_TO not set";
  if (env.FORWARD_TO) {
    try {
      await message.forward(env.FORWARD_TO);
      forwarded = "ok";
    } catch (err) {
      forwarded = `failed: ${String(err && err.message || err).slice(0, 200)}`;
      console.error("forward to Gmail failed", { to: env.FORWARD_TO, error: forwarded });
    }
  } else {
    console.error("FORWARD_TO is not set; not forwarding to Gmail");
  }

  if (!env.HANDLER_INGEST_URL || !env.HANDLER_EMAIL_SECRET) {
    console.error("HANDLER_INGEST_URL or HANDLER_EMAIL_SECRET missing; not posting to handler");
    return { forwarded, posted: false };
  }
  if (body === null) return { forwarded, posted: false };
  try {
    const timestamp = Math.floor(Date.now() / 1000).toString();
    const response = await fetchImpl(env.HANDLER_INGEST_URL, {
      method: "POST",
      body,
      headers: {
        "content-type": "message/rfc822",
        "x-handler-timestamp": timestamp,
        "x-handler-signature": await sign(env.HANDLER_EMAIL_SECRET, timestamp, body),
        "x-handler-envelope-from": encodeURIComponent(message.from || ""),
        "x-handler-envelope-to": encodeURIComponent(message.to || ""),
        "x-handler-auth-results": authHeaders(message.headers),
        "x-handler-forwarded": forwarded,
      },
      signal: AbortSignal.timeout(Number(env.HANDLER_TIMEOUT_MS || 60000)),
    });
    const text = (await response.text()).slice(0, 500);
    if (!response.ok) {
      console.error("handler rejected the email", { status: response.status, body: text });
      return { forwarded, posted: false, status: response.status };
    }
    console.log("handler accepted the email", { status: response.status, body: text });
    return { forwarded, posted: true, status: response.status };
  } catch (err) {
    console.error("posting to handler failed", { error: String(err && err.message || err) });
    return { forwarded, posted: false };
  }
}

export default {
  async email(message, env, ctx) {
    await deliver(message, env);
  },
};
