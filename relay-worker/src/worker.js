/**
 * MedicAI — WhatsApp cold-start relay
 * =====================================
 * Sits in front of the Render backend as the webhook URL registered
 * with Meta. Cloudflare Workers never sleep, so this always answers
 * Meta's POST instantly (avoiding webhook timeout/retry storms) and:
 *
 *   - On a COLD Render instance: immediately sends the sender a single
 *     WhatsApp text that introduces MedicAI and explains the ~90s
 *     free-tier wake-up delay, then POLLS Render's /health in the
 *     background until it wakes (or a time budget runs out) and
 *     AUTO-FORWARDS the reviewer's original message the moment it's
 *     ready — no resend needed. Only if the poll times out do we fall
 *     back to asking the sender to resend.
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
 * NOTE on text-vs-location pairing: that logic lives entirely in
 * api.py (whatsapp_pending_message / whatsapp_pending_location + a
 * per-sender lock), which combines whichever of {text, location}
 * arrives first with whichever arrives second, regardless of order.
 * This Worker doesn't need to know anything about that — its only
 * job is to make sure the raw webhook payload actually reaches
 * Render (eventually) instead of being silently dropped on a cold
 * start, which is what this file fixes.
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

const WARM_WINDOW_MS     = 4 * 60 * 1000;  // treat Render as "warm" if forwarded successfully within this window
const NOTICE_DEBOUNCE_MS = 90 * 1000;      // don't re-send the wake-up notice more than once per this window

const HEALTH_POLL_INTERVAL_MS = 5 * 1000;  // how often to check /health while cold
const HEALTH_POLL_BUDGET_MS   = 110 * 1000; // give up polling after this long (stay under Workers' waitUntil ceiling)

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
  // Render (which re-verifies the X-Hub-Signature-256 HMAC itself), and we
  // may need to hold onto it while Render wakes up, then forward that exact
  // same body once it's ready.
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

  // Cold (or unknown). Fire wake-up pings for BOTH backend services right
  // away — cheap, safe to repeat, and run in parallel so waking two
  // services doesn't take twice as long as waking one.
  wakeSubagent(env); // fire-and-forget, no /health route to poll on this one

  // Debounce the intro/wake-up notice so a burst of messages during the
  // boot window doesn't spam the sender with it more than once.
  const lastNotice = await env.WAKE_KV.get("last_notice_at");
  const noticeRecently = lastNotice && (Date.now() - Number(lastNotice) < NOTICE_DEBOUNCE_MS);

  if (sender && !noticeRecently) {
    await env.WAKE_KV.put("last_notice_at", String(Date.now()));
    await sendWhatsAppText(
      sender,
      "👋 Hi, this is *MedicAI* — your emergency first-aid assistant.\n\n" +
      "I'm just starting up (running on free hosting, so this can take up " +
      "to *90 seconds*, occasionally a bit longer for things like hospital " +
      "alerts or video lookup). Hang tight — *no need to resend*, I'll " +
      "pick up your message automatically as soon as I'm ready.",
      env
    );
  }

  // Hold the request here and poll Render until it wakes, then auto-forward
  // the ORIGINAL message that triggered this cold start — this is what
  // replaces the old "tell them to resend" behavior, which silently
  // dropped whatever they'd sent (text or location).
  const becameWarm = await waitForRenderWarm(env);

  if (becameWarm) {
    await forwardToRender(rawBody, originalRequest, env);
    return;
  }

  // Poll budget exhausted without Render responding — fall back to asking
  // the sender to resend, same as the old behavior, so they're never just
  // left hanging with no explanation.
  if (sender) {
    await sendWhatsAppText(
      sender,
      "⏳ Still starting up — sorry for the wait. Please resend your " +
      "message now and it should go through.",
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

// Poll Render's /health every HEALTH_POLL_INTERVAL_MS until it responds ok,
// or until HEALTH_POLL_BUDGET_MS has elapsed — whichever comes first.
// Returns true the moment Render is confirmed warm (and marks it warm in
// KV so subsequent messages take the fast warm path), false on timeout.
async function waitForRenderWarm(env) {
  const deadline = Date.now() + HEALTH_POLL_BUDGET_MS;

  while (Date.now() < deadline) {
    try {
      const resp = await fetch(`${env.RENDER_URL}/health`, { method: "GET" });
      if (resp.ok) {
        await env.WAKE_KV.put("last_warm_at", String(Date.now()));
        return true;
      }
    } catch (_) {
      // Render not answering yet — keep polling.
    }
    await sleep(HEALTH_POLL_INTERVAL_MS);
  }

  return false;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// Same idea as waking the main API, for the separate subagent coordinator
// service. It has no /health route of its own in the code we've seen, so
// hit its base URL instead — any response (even a 404) is enough to prove
// the dyno is awake; we don't need it to succeed semantically, just to
// wake up. Not polled/awaited: the coordinator is only needed once a turn
// actually reaches Render and starts launching subagent tasks, by which
// point it's had the same ~90s+ to boot in the background as everything
// else here.
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

// Direct WhatsApp Cloud API send, used only for messages the Worker sends
// on its own (cold-start intro/notice, timeout fallback) — for cases where
// Render isn't awake yet to send them itself.
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
