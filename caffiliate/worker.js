// Cloudflare Worker: chạy điểm danh caffiliate tự động lúc 00:00 ICT
// Deploy: paste vào Cloudflare Dashboard → Workers → Quick Edit
// Cron Trigger: 55 16 * * *  (16:55 UTC = 23:55 ICT)
// Secrets cần set: COOKIES, CSRF_TOKEN, XENG_SECRET, USER_ID, USER_AGENT,
//                  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

const API_HOST = "app.caffiliate.vn";
const CHECKIN_URL = `https://${API_HOST}/api/v2/xeng/check-in-secure`;
const STATUS_URL = `https://${API_HOST}/api/v2/xeng/check-in/status`;

const BURST_COUNT = 12;
const BURST_SPACING_MS = 25;

async function hmacSha256Hex(secret, message) {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    enc.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(message));
  return Array.from(new Uint8Array(sig))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

function generateNonce() {
  const alphabet = "0123456789abcdefghijklmnopqrstuvwxyz";
  let s = "";
  for (let i = 0; i < 12; i++) s += alphabet[Math.floor(Math.random() * 36)];
  return s;
}

async function buildHeaders(env) {
  const ts = Date.now().toString();
  const nonce = generateNonce();
  const sig = await hmacSha256Hex(env.XENG_SECRET, `${ts}.${nonce}.${env.USER_ID}`);
  return {
    "Content-Type": "application/json",
    "Accept": "*/*",
    "Origin": "https://app.caffiliate.vn",
    "Referer": "https://app.caffiliate.vn/rewards",
    "User-Agent": env.USER_AGENT,
    "Cookie": env.COOKIES,
    "x-csrf-token": env.CSRF_TOKEN,
    "x-signature": sig,
    "x-timestamp": ts,
    "x-nonce": nonce,
  };
}

async function postCheckin(env) {
  const headers = await buildHeaders(env);
  const sent = Date.now();
  try {
    const res = await fetch(CHECKIN_URL, { method: "POST", headers, body: "{}" });
    const body = await res.text();
    let payload;
    try { payload = JSON.parse(body); }
    catch { payload = { success: false, body: body.slice(0, 200), status: res.status }; }
    return { sent, recv: Date.now(), payload };
  } catch (e) {
    return { sent, recv: Date.now(), payload: { success: false, error: String(e).slice(0, 200) } };
  }
}

async function measureLatency(env) {
  const start = Date.now();
  try {
    const res = await fetch(STATUS_URL, {
      headers: { Cookie: env.COOKIES, "User-Agent": env.USER_AGENT, Accept: "*/*" },
    });
    await res.text();
    return Date.now() - start;
  } catch { return null; }
}

async function getStatus(env) {
  try {
    const res = await fetch(STATUS_URL, {
      headers: { Cookie: env.COOKIES, "User-Agent": env.USER_AGENT, Accept: "*/*" },
    });
    const d = await res.json();
    return d.success ? d.data : null;
  } catch { return null; }
}

async function sendTelegram(env, msg) {
  if (!env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID) {
    console.log("[no-tg]", msg);
    return;
  }
  try {
    await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: env.TELEGRAM_CHAT_ID,
        text: `[Caffiliate-CF] ${msg}`.slice(0, 3900),
      }),
    });
  } catch (e) { console.log("TG fail:", e); }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitUntilTimestamp(targetMs) {
  while (Date.now() < targetMs) {
    const rem = targetMs - Date.now();
    if (rem > 1000) await sleep(Math.min(rem - 500, 10000));
    else if (rem > 50) await sleep(10);
    else if (rem > 5) await sleep(1);
    else { /* tight spin */ }
  }
}

async function runCheckin(env) {
  // Compute next midnight ICT = 17:00 UTC.
  const now = new Date();
  const target = new Date(now);
  target.setUTCHours(17, 0, 0, 0);
  if (now.getTime() >= target.getTime()) {
    target.setUTCDate(target.getUTCDate() + 1);
  }

  const dateStr = new Date(target.getTime() - 7 * 3600 * 1000).toISOString().slice(0, 10);
  await sendTelegram(env, `Bắt đầu phiên ngày ${dateStr}`);

  // Đo RTT 3 mẫu
  const samples = [];
  for (let i = 0; i < 3; i++) {
    const ms = await measureLatency(env);
    if (ms !== null) samples.push(ms);
    await sleep(300);
  }

  let offsetMs;
  if (samples.length) {
    const maxRtt = Math.max(...samples);
    offsetMs = -460 - Math.round(maxRtt * 2.5);
    offsetMs = Math.max(-2500, Math.min(-50, offsetMs));
    await sendTelegram(env, `📡 RTT [${samples.map((s) => Math.round(s)).join(", ")}]ms → offset ${offsetMs}ms`);
  } else {
    offsetMs = -700;
    await sendTelegram(env, `⚠️ RTT đo fail, dùng offset ${offsetMs}ms`);
  }

  await waitUntilTimestamp(target.getTime() + offsetMs);

  // Parallel burst
  const promises = [];
  for (let i = 0; i < BURST_COUNT; i++) {
    promises.push((async () => {
      await sleep(i * BURST_SPACING_MS);
      return await postCheckin(env);
    })());
  }
  const results = await Promise.all(promises);

  // Tìm success sớm nhất
  const successes = results.filter((r) => r.payload && r.payload.success);
  successes.sort((a, b) => a.sent - b.sent);

  if (successes.length) {
    const winner = successes[0];
    const idx = results.indexOf(winner);
    const latency = winner.recv - winner.sent;
    const data = winner.payload.data || {};
    const status = await getStatus(env);
    const pos = status && status.todayCheckInPosition;
    const posStr = pos ? ` | 🏆 top ${pos}` : "";
    const fireUTC = new Date(winner.sent).toISOString().slice(11, 23);
    await sendTelegram(env, `✅ streak=${data.streakDay} +${data.rewardValue}${posStr} | burst #${idx + 1} | fire ${fireUTC} UTC | ${latency}ms`);
  } else {
    const first = results[0];
    const p = (first && first.payload) || {};
    const err = p.message || p.error || (p.status ? `HTTP${p.status}: ${(p.body || "").slice(0, 100)}` : `raw: ${JSON.stringify(p).slice(0, 150)}`);
    await sendTelegram(env, `❌ Burst x${BURST_COUNT} fail: ${err}`);
  }
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(runCheckin(env));
  },

  // HTTP endpoint cho manual test:
  //  /?key=<DEBUG_KEY>             → full flow (chờ midnight rồi fire) — KHÔNG dùng ban ngày!
  //  /?key=<DEBUG_KEY>&test=1      → fire ngay (test connectivity/secrets, KHÔNG chờ midnight)
  //  /?key=<DEBUG_KEY>&status=1    → query status hiện tại (xem hôm nay đã điểm chưa, rank, streak)
  async fetch(req, env, ctx) {
    const url = new URL(req.url);
    const key = url.searchParams.get("key");
    if (!env.DEBUG_KEY || key !== env.DEBUG_KEY) {
      return new Response("Caffiliate Worker. Use scheduled trigger or /?key=<DEBUG_KEY>[&test=1|&status=1]", { status: 200 });
    }

    if (url.searchParams.get("status") === "1") {
      const status = await getStatus(env);
      return Response.json(status || { error: "status fail" });
    }

    if (url.searchParams.get("test") === "1") {
      // Fire 1 POST ngay, return kết quả thô (đã điểm rồi → 400 "Hôm nay bạn đã điểm danh rồi")
      const r = await postCheckin(env);
      const latency = r.recv - r.sent;
      return Response.json({ latency_ms: latency, sent: new Date(r.sent).toISOString(), payload: r.payload });
    }

    ctx.waitUntil(runCheckin(env));
    return new Response("Triggered runCheckin (chạy nền, check Telegram)", { status: 200 });
  },
};
