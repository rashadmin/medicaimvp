/**
 * MedicAI — WhatsApp cold-start relay
 * =====================================
 * Sits in front of the Render backend as the webhook URL registered
 * with Meta. Cloudflare Workers never sleep, so this always answers
 * Meta's POST instantly (avoiding webhook timeout/retry storms) and:
 *
 *   - On a COLD Render instance: immediately sends the sender a
 *     WhatsApp text explaining the ~90s free-tier wake-up delay, and
 *     fires a background request to wake Render up. The reviewer's
 *     original message is NOT forwarded in this case — they're asked
 *     to resend once the delay has passed.
 *
 *   - On a WARM Render instance (woken recently): forwards the raw
 *     webhook payload straight through to Render's own
 *     /webhook/whatsapp, unchanged, so Render's existing signature
 *     verification / session logic runs exactly as before.
 *
 * The GET verification handshake (Meta's one-time webhook setup call)
 * is answered directly here too, so it never depends on Render being
 * awake.
 *
 * Two separate Render services back this system — the main API
 * (api.py, handles /chat and /webhook/whatsapp) and the subagent
 * coordinator (async_coordinator.py, runs web_searcher /
 * hospital_notifier / youtube_subagent as background tasks, polled
 * by the main API rather than called directly by WhatsApp). Both can
 * independently fall asleep on Render's free tier, and there's no
 * natural wake trigger for the coordinator on its own — nothing ever
 * sends it a direct inbound request the way WhatsApp hits the main
 * API. So on a cold start this Worker wakes BOTH in parallel.
 *
 * Required secrets (set via `wrangler secret put <NAME>`):
 *   WHATSAPP_ACCESS_TOKEN     same token Render uses to send messages
 *   WHATSAPP_PHONE_NUMBER_ID  same phone number id Render uses
 *   WHATSAPP_VERIFY_TOKEN     same value as Render's WHATSAPP_VERIFY_TOKEN
 *   RENDER_URL                e.g. https://your-main-api.onrender.com
 *   SUBAGENT_URL              e.g. https://your-coordinator.onrender.com
 *
 * Required binding (in wrangler.toml):
 *   WAKE_KV   a KV namespace used to debounce the wake-up notice so
 *             it isn't re-sent on every message during the ~90s
 *             window while Render is still booting.
 */

const WARM_WINDOW_MS   = 4 * 60 * 1000;   // treat Render as "warm" if forwarded successfully within this window
const NOTICE_DEBOUNCE_MS = 90 * 1000;     // don't re-send the wake-up notice more than once per this window

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    if (url.pathname !== "/webhook/whatsapp") {
      return new Response("Not found", { status: 404 });
    }

    if (request.method === "GET") {
      return handleVerify(request, env);
    }

    if (request.method === "POST") {
      return handleIncoming(request, env, ctx);
    }

    return new Response("Method not allowed", { status: 405 });
  },
};

// ── GET: Meta's webhook verification handshake ─────────────────────────────
function handleVerify(request, env) {
  const url = new URL(request.url);
  const mode      = url.searchParams.get("hub.mode");
  const token     = url.searchParams.get("hub.verify_token");
  const challenge = url.searchParams.get("hub.challenge");

  if (mode === "subscribe" && token === env.WHATSAPP_VERIFY_TOKEN) {
    return new Response(challenge, { status: 200 });
  }
  return new Response("Forbidden", { status: 403 });
}

// ── POST: inbound WhatsApp message/event ───────────────────────────────────
async function handleIncoming(request, env, ctx) {
  // Read the raw body once — we may need to forward it byte-for-byte to
  // Render (which re-verifies the X-Hub-Signature-256 HMAC itself).
  const rawBody = await request.text();

  // Always ack Meta immediately with the standard 200. Everything else
  // happens in the background via ctx.waitUntil so this response isn't
  // held up waiting on Render or on the WhatsApp send call.
  ctx.waitUntil(process(rawBody, request, env));

  return new Response("EVENT_RECEIVED", { status: 200 });
}

async function process(rawBody, originalRequest, env) {
  let sender = null;
  try {
    const payload = JSON.parse(rawBody);
    sender = extractSender(payload);
  } catch (err) {
    console.error("Failed to parse/extract sender", err.message || String(err));
  }
  console.log("process() sender=", sender);

  const lastWarm = await env.WAKE_KV.get("last_warm_at");
  const isWarm   = lastWarm && (Date.now() - Number(lastWarm) < WARM_WINDOW_MS);

  if (isWarm) {
    await forwardToRender(rawBody, originalRequest, env);
    return;
  }

  // Cold (or unknown) — debounce the wake-up notice so a burst of
  // messages during the boot window doesn't spam the sender.
  const lastNotice = await env.WAKE_KV.get("last_notice_at");
  const noticeRecently = lastNotice && (Date.now() - Number(lastNotice) < NOTICE_DEBOUNCE_MS);

  // Fire wake-up pings for BOTH backend services regardless — cheap,
  // safe to repeat, and run in parallel so waking two services doesn't
  // take twice as long as waking one.
  wakeRender(env);   // not awaited on purpose; don't block on either boot
  wakeSubagent(env);

  if (sender && !noticeRecently) {
    await env.WAKE_KV.put("last_notice_at", String(Date.now()));
    await sendWhatsAppText(
      sender,
      "⏳ This is a prototype running on free hosting tiers, so the " +
      "backend services are waking up from sleep. This can take up to " +
      "*90 seconds* — occasionally a bit longer for features like " +
      "hospital alerts or video lookup, which run on a second service. " +
      "Please wait a moment, then resend your message and it'll work normally.",
      env
    );
  }
}

// Pull the sender's phone number out of a standard WhatsApp webhook payload.
function extractSender(payload) {
  try {
    const value = payload.entry?.[0]?.changes?.[0]?.value;
    const msg   = value?.messages?.[0];
    return msg?.from || null;
  } catch (_) {
    return null;
  }
}

// Fire-and-forget request to wake the sleeping main-API Render instance.
// Hitting /health is enough — Render just needs any HTTP request to
// spin the dyno back up. We don't wait for or care about the response.
function wakeRender(env) {
  fetch(`${env.RENDER_URL}/health`, { method: "GET" })
    .then((resp) => {
      if (resp.ok) {
        // This closes the deadlock described above: previously
        // `last_warm_at` was only ever written inside forwardToRender()
        // after a successful forward, but forwardToRender() was only
        // ever called when already considered warm. That meant no
        // request could ever be the one to flip isWarm to true, so
        // every message — forever — fell into the cold path. Marking
        // warm as soon as /health responds ok gives the state a way in.
        return env.WAKE_KV.put("last_warm_at", String(Date.now()));
      }
    })
    .catch(() => {});
}

// Same idea, for the separate subagent coordinator service. It has no
// /health route of its own in the code we've seen, so hit its base URL
// instead — any response (even a 404) is enough to prove the dyno is
// awake; we don't need it to succeed semantically, just to wake up.
function wakeSubagent(env) {
  if (!env.SUBAGENT_URL) return; // optional — skip if not configured
  fetch(`${env.SUBAGENT_URL}/`, { method: "GET" }).catch(() => {});
}

// Forward the raw, unmodified webhook body + relevant headers straight
// through to Render's own /webhook/whatsapp. Render performs its own
// HMAC signature verification against this exact body, so nothing here
// should re-serialize or alter it.
async function forwardToRender(rawBody, originalRequest, env) {
  const headers = new Headers();
  const sig = originalRequest.headers.get("X-Hub-Signature-256");
  if (sig) headers.set("X-Hub-Signature-256", sig);
  headers.set("Content-Type", "application/json");

  try {
    const resp = await fetch(`${env.RENDER_URL}/webhook/whatsapp`, {
      method: "POST",
      headers,
      body: rawBody,
    });
    if (resp.ok) {
      await env.WAKE_KV.put("last_warm_at", String(Date.now()));
    }
  } catch (_) {
    // Render dropped the request (e.g. it went back to sleep mid-flight,
    // or a Render restart raced this call). Nothing more to do — the
    // sender will simply not get a reply and can resend.
  }
}

// Direct WhatsApp Cloud API send, used only for the cold-start notice
// itself (this is the one message the Worker sends on its own, since
// Render isn't awake yet to send it).
async function sendWhatsAppText(to, body, env) {
  const graphUrl =
    `https://graph.facebook.com/v21.0/${env.WHATSAPP_PHONE_NUMBER_ID}/messages`;

  try {
    const resp = await fetch(graphUrl, {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${env.WHATSAPP_ACCESS_TOKEN}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        messaging_product: "whatsapp",
        to,
        type: "text",
        text: { body },
      }),
    });
    const text = await resp.text();
    if (!resp.ok) {
      console.error("WhatsApp send failed", resp.status, text);
    } else {
      console.log("WhatsApp send ok", resp.status, text);
    }
  } catch (err) {
    console.error("WhatsApp send threw", err.message || String(err));
  }
}
