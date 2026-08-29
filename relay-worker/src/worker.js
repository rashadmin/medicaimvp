/**
 * MedicAI — WhatsApp cold-start relay
 * =====================================
 * Sits in front of the Render backend as the webhook URL registered
 * with Meta. Cloudflare Workers never sleep, so this always answers
 * Meta's POST instantly (avoiding webhook timeout/retry storms) and:
 *
 *   - On a COLD Render instance: immediately sends the sender a single
 *     WhatsApp text introducing MedicAI + the ~90s wake-up delay, then
 *     hands the message off to a Durable Object (RenderWaker) which
 *     polls Render's /health via a chain of Alarms and AUTO-FORWARDS
 *     the original message the moment Render wakes — no resend needed.
 *
 *     WHY A DURABLE OBJECT: a plain `ctx.waitUntil()` background task
 *     is bounded by a hard per-invocation execution-time ceiling that
 *     sits well under Render's worst-case ~90–110s free-tier boot
 *     time. In practice that meant the poll loop got silently killed
 *     mid-wait — the intro message went out, but neither the forward
 *     nor the "please resend" fallback ever fired. Durable Object
 *     Alarms have no such ceiling per tick: each Alarm firing is its
 *     own fresh, short-lived invocation, chained by rescheduling
 *     rather than by sleeping inside one long-lived call. That's the
 *     platform-supported way to do a multi-minute wait reliably.
 *
 *   - On a WARM Render instance (woken recently, tracked in KV):
 *     forwards the raw webhook payload straight through to Render's
 *     own /webhook/whatsapp, unchanged, exactly as before — the
 *     Durable Object is only involved on the cold path.
 *
 * The GET verification handshake (Meta's one-time webhook setup call)
 * is answered directly here too, so it never depends on Render being
 * awake.
 *
 * Two separate Render services back this system — the main API
 * (api.py, handles /chat and /webhook/whatsapp) and the subagent
 * coordinator (async_coordinator.py). Both can independently fall
 * asleep on Render's free tier; the coordinator has no /health route
 * of its own, so it's just pinged (fire-and-forget) rather than
 * polled — by the time a turn reaches Render and starts launching
 * subagent tasks, it's had the same wake window as everything else.
 *
 * NOTE on text-vs-location pairing: that logic lives entirely in
 * api.py (whatsapp_pending_message / whatsapp_pending_location + a
 * per-sender lock), which combines whichever of {text, location}
 * arrives first with whichever arrives second, regardless of order.
 * This Worker doesn't need to know anything about that — its only
 * job is making sure the raw webhook payload actually reaches Render,
 * even across a multi-minute cold boot, instead of being dropped.
 *
 * Required secrets (set via `wrangler secret put <NAME>`):
 *   WHATSAPP_ACCESS_TOKEN     same token Render uses to send messages
 *   WHATSAPP_PHONE_NUMBER_ID  same phone number id Render uses
 *   WHATSAPP_VERIFY_TOKEN     same value as Render's WHATSAPP_VERIFY_TOKEN
 *   RENDER_URL                e.g. https://your-main-api.onrender.com
 *   SUBAGENT_URL              e.g. https://your-coordinator.onrender.com
 *
 * Required bindings (in wrangler.toml):
 *   WAKE_KV   KV namespace — debounces the wake-up notice, and marks
 *             Render "warm" for WARM_WINDOW_MS after a successful
 *             forward, so most messages skip the Durable Object
 *             entirely.
 *   WAKER     Durable Object binding, class_name "RenderWaker" (see
 *             wrangler.toml snippet at the bottom of this file).
 */

const WARM_WINDOW_MS     = 4 * 60 * 1000;  // treat Render as "warm" if forwarded successfully within this window
const NOTICE_DEBOUNCE_MS = 90 * 1000;      // don't re-send the wake-up notice more than once per this window

const HEALTH_POLL_INTERVAL_MS = 5 * 1000;   // how often the Durable Object's Alarm re-checks /health
const HEALTH_POLL_MAX_ATTEMPTS = 24;        // ~24 * 5s = 120s total wait budget before giving up

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
  // may need to hand it to the Durable Object to hold across a cold boot.
  const rawBody = await request.text();
  const sig     = request.headers.get("X-Hub-Signature-256") || "";

  // Always ack Meta immediately with the standard 200. Everything else
  // happens in the background so this response isn't held up on Render.
  ctx.waitUntil(process(rawBody, sig, env));

  return new Response("EVENT_RECEIVED", { status: 200 });
}

async function process(rawBody, sig, env) {
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
    await forwardToRender(rawBody, sig, env);
    return;
  }

  // Cold (or unknown). Ping the subagent coordinator right away — cheap,
  // safe to repeat, no /health route to poll on that one so it's just a
  // fire-and-forget wake.
  wakeSubagent(env);

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
      "to *90 seconds*, occasionally a bit longer). Hang tight — *no need " +
      "to resend*, I'll message you the moment I'm ready and pick up your " +
      "message automatically.",
      env
    );
  }

  // Hand off to the Durable Object: it queues this message and polls
  // /health via a chain of Alarms (no execution-time ceiling per tick,
  // unlike a plain waitUntil loop), then auto-forwards once Render is
  // warm, or sends a "please resend" fallback if the wait budget runs out.
  const id   = env.WAKER.idFromName("render-waker");
  const stub = env.WAKER.get(id);
  await stub.fetch("https://render-waker.internal/enqueue", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ rawBody, sig, sender }),
  });
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

// Same idea as waking the main API, for the separate subagent coordinator
// service. Not polled/awaited: the coordinator only needs to be awake by
// the time a turn actually reaches Render and starts launching subagent
// tasks, by which point it's had the same wake window as everything else.
function wakeSubagent(env) {
  if (!env.SUBAGENT_URL) return; // optional — skip if not configured
  fetch(`${env.SUBAGENT_URL}/`, { method: "GET" }).catch(() => {});
}

// Forward the raw, unmodified webhook body + relevant headers straight
// through to Render's own /webhook/whatsapp. Render performs its own
// HMAC signature verification against this exact body, so nothing here
// should re-serialize or alter it. Shared by both the fast warm path
// (above) and the Durable Object's post-wake forward.
async function forwardToRender(rawBody, sig, env) {
  const headers = new Headers();
  if (sig) headers.set("X-Hub-Signature-256", sig);
  headers.set("Content-Type", "application/json");

  const resp = await fetch(`${env.RENDER_URL}/webhook/whatsapp`, {
    method: "POST",
    headers,
    body: rawBody,
  });
  if (resp.ok) {
    await env.WAKE_KV.put("last_warm_at", String(Date.now()));
  }
  return resp;
}

// Direct WhatsApp Cloud API send, used for messages the Worker/Durable
// Object send on their own (cold-start intro, timeout fallback) — for
// cases where Render isn't awake yet to send them itself.
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

// ════════════════════════════════════════════════════════════════════════
//  DURABLE OBJECT — survives Render's full cold-boot window via Alarms
// ════════════════════════════════════════════════════════════════════════
//
// One singleton instance ("render-waker") queues every message that
// arrives while Render is cold, and polls /health on a ~5s cadence using
// setAlarm()/alarm() — each tick is a fresh, short-lived invocation, so
// there's no long-running loop to be killed mid-wait. The moment /health
// responds ok, every queued message is forwarded in arrival order and the
// queue is cleared. If the attempt budget runs out first, every waiting
// sender gets a "please resend" fallback instead of silence.
export class RenderWaker {
  constructor(state, env) {
    this.state = state;
    this.env   = env;
  }

  async fetch(request) {
    const url = new URL(request.url);

    if (url.pathname === "/enqueue" && request.method === "POST") {
      const item = await request.json(); // { rawBody, sig, sender }

      const pending = (await this.state.storage.get("pending")) || [];
      pending.push(item);
      await this.state.storage.put("pending", pending);

      // Only (re)start the attempt counter + alarm chain if one isn't
      // already running — later enqueues during the same boot window just
      // join the existing queue and ride along with the alarm already in
      // flight, rather than each spawning its own polling cadence.
      const attempts = await this.state.storage.get("attempts");
      if (attempts === undefined) {
        await this.state.storage.put("attempts", 0);
        await this.state.storage.setAlarm(Date.now() + HEALTH_POLL_INTERVAL_MS);
      }

      return new Response("queued", { status: 200 });
    }

    return new Response("Not found", { status: 404 });
  }

  async alarm() {
    const pending = (await this.state.storage.get("pending")) || [];
    if (pending.length === 0) {
      // Nothing left to do (e.g. queue was already drained) — reset and stop.
      await this.state.storage.delete("attempts");
      return;
    }

    let healthy = false;
    try {
      const resp = await fetch(`${this.env.RENDER_URL}/health`, { method: "GET" });
      healthy = resp.ok;
    } catch (_) {
      healthy = false;
    }

    if (healthy) {
      await this.env.WAKE_KV.put("last_warm_at", String(Date.now()));

      // Forward every queued message, in the order it arrived, so nothing
      // sent during the boot window is lost or reordered.
      for (const item of pending) {
        try {
          await forwardToRender(item.rawBody, item.sig, this.env);
        } catch (err) {
          console.error("forwardToRender failed during drain", err.message || String(err));
        }
      }

      await this.state.storage.delete("pending");
      await this.state.storage.delete("attempts");
      return;
    }

    const attempts = ((await this.state.storage.get("attempts")) || 0) + 1;

    if (attempts >= HEALTH_POLL_MAX_ATTEMPTS) {
      // Wait budget exhausted — tell every waiting sender to resend rather
      // than leaving them with silence, then give up on this queue. A
      // fresh /enqueue later (their resend) starts a brand-new attempt
      // cycle from zero.
      const seen = new Set();
      for (const item of pending) {
        if (item.sender && !seen.has(item.sender)) {
          seen.add(item.sender);
          await sendWhatsAppText(
            item.sender,
            "⏳ Still starting up — sorry for the wait. Please resend your " +
            "message now and it should go through.",
            this.env
          );
        }
      }
      await this.state.storage.delete("pending");
      await this.state.storage.delete("attempts");
      return;
    }

    await this.state.storage.put("attempts", attempts);
    await this.state.storage.setAlarm(Date.now() + HEALTH_POLL_INTERVAL_MS);
  }
}

/**
 * wrangler.toml additions required for the Durable Object:
 *
 *   [[durable_objects.bindings]]
 *   name = "WAKER"
 *   class_name = "RenderWaker"
 *
 *   [[migrations]]
 *   tag = "v1"
 *   new_classes = ["RenderWaker"]
 *
 * (WAKE_KV's existing kv_namespaces binding stays as-is — the Durable
 * Object reuses it via env, no separate KV binding needed for the DO.)
 */
