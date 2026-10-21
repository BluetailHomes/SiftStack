// SiftStack dispatcher: fires pm-text-sender.yml's jobs on time via workflow_dispatch.
// See wrangler.toml for why this exists and how to deploy.

// Central-time slot -> workflow_dispatch "mode" input. Each slot gets a few minutes
// of tolerance so a cron that fires a minute late still matches.
const JOBS = [
  { mode: "am-start", hour: 9, minute: 50 },
  { mode: "pm-texts", hour: 16, minute: 30 },
  { mode: "audit", hour: 19, minute: 15 },
];
const TOLERANCE_MIN = 5;
const WEEKDAYS = new Set(["Mon", "Tue", "Wed", "Thu", "Fri"]);

function centralNow(date = new Date()) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/Chicago", hour12: false,
    weekday: "short", hour: "2-digit", minute: "2-digit",
  }).formatToParts(date);
  const get = (t) => parts.find((p) => p.type === t)?.value;
  return { weekday: get("weekday"), hour: Number(get("hour")) % 24, minute: Number(get("minute")) };
}

function jobForNow(now) {
  if (!WEEKDAYS.has(now.weekday)) return null;
  const mins = now.hour * 60 + now.minute;
  return JOBS.find((j) => {
    const slot = j.hour * 60 + j.minute;
    return mins >= slot && mins < slot + TOLERANCE_MIN;
  }) || null;
}

async function dispatch(env, mode) {
  const url = `https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/${env.WORKFLOW_FILE}/dispatches`;
  const res = await fetch(url, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
      "Accept": "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "siftstack-dispatcher",
      "Content-Type": "application/json",
    },
    // gate=true tells the workflow to apply the same Central time window the
    // schedule path uses, so a queued/late dispatch can never text at dinner time.
    body: JSON.stringify({ ref: env.GITHUB_REF, inputs: { mode, gate: "true" } }),
  });
  return { ok: res.status === 204, status: res.status, body: res.ok ? "" : await res.text() };
}

async function slack(env, text) {
  if (!env.SLACK_WEBHOOK_URL) return;
  try {
    await fetch(env.SLACK_WEBHOOK_URL, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }) });
  } catch (_) { /* alerts are best-effort */ }
}

export default {
  async scheduled(event, env, ctx) {
    const now = centralNow(new Date(event.scheduledTime));
    const job = jobForNow(now);
    const stamp = `${now.weekday} ${String(now.hour).padStart(2, "0")}:${String(now.minute).padStart(2, "0")} Central`;
    if (!job) { console.log(`cron ${event.cron} at ${stamp}: no job in this slot (other DST entry), nothing to do`); return; }
    if (!env.GITHUB_TOKEN) {
      console.error("GITHUB_TOKEN secret is not set");
      ctx.waitUntil(slack(env, `:x: siftstack-dispatcher: GITHUB_TOKEN not set, could not start *${job.mode}* at ${stamp}`));
      return;
    }
    const r = await dispatch(env, job.mode);
    if (r.ok) { console.log(`dispatched ${job.mode} at ${stamp}`); return; }
    console.error(`dispatch ${job.mode} failed: HTTP ${r.status} ${r.body}`);
    ctx.waitUntil(slack(env, `:x: siftstack-dispatcher could not start *${job.mode}* at ${stamp}: GitHub returned HTTP ${r.status} ${r.body.slice(0, 200)}. Fallback: run it by hand (GitHub Actions -> PM Text Sender -> Run workflow).`));
  },

  // GET /            -> status (what Central time it is, which slot would fire now)
  // POST /run/<mode> -> manual dispatch, requires header X-Dispatch-Key matching the DISPATCH_KEY secret
  async fetch(request, env) {
    const url = new URL(request.url);
    const now = centralNow();
    if (request.method === "POST" && url.pathname.startsWith("/run/")) {
      const mode = url.pathname.slice(5);
      if (!env.DISPATCH_KEY || request.headers.get("X-Dispatch-Key") !== env.DISPATCH_KEY) return new Response("forbidden", { status: 403 });
      if (!JOBS.some((j) => j.mode === mode)) return new Response("unknown mode", { status: 400 });
      const r = await dispatch(env, mode);
      return new Response(r.ok ? `dispatched ${mode}` : `failed HTTP ${r.status} ${r.body}`, { status: r.ok ? 200 : 502 });
    }
    const job = jobForNow(now);
    return new Response(JSON.stringify({ central: now, slot_now: job?.mode || null, jobs: JOBS, token_set: Boolean(env.GITHUB_TOKEN) }, null, 2),
      { headers: { "Content-Type": "application/json" } });
  },
};
