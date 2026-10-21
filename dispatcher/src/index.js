// SiftStack dispatcher: fires pm-text-sender.yml's jobs on time via workflow_dispatch.
// See wrangler.toml for why this exists and how to deploy.

// Each job is either a Central-time SLOT (hour/minute, weekdays, a few minutes of
// tolerance so a cron that fires a minute late still matches) or an INTERVAL (every N
// minutes inside a Central hour range, every day). `workflow` defaults to env.WORKFLOW_FILE;
// `inputs` replace the default {mode, gate:"true"} dispatch inputs.
const JOBS = [
  { mode: "am-start", hour: 9, minute: 50 },
  { mode: "pm-texts", hour: 16, minute: 30 },
  { mode: "audit", hour: 19, minute: 15 },
  // Inbound-text responder: poll every 5 minutes while sellers can be replying (the drip can
  // send until ~11 PM Central). 180-minute lookback so a missed poll never loses a reply.
  { mode: "sms-responder", everyMinutes: 5, fromHour: 7, toHour: 23, daily: true,
    workflow: "sms-auto-responder.yml", inputs: { since_minutes: "180" } },
];
const TOLERANCE_MIN = 5;
const INTERVAL_CRON = "*/5 * * * *";  // must match the interval entry in wrangler.toml
const WEEKDAYS = new Set(["Mon", "Tue", "Wed", "Thu", "Fri"]);

function centralNow(date = new Date()) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/Chicago", hour12: false,
    weekday: "short", hour: "2-digit", minute: "2-digit",
  }).formatToParts(date);
  const get = (t) => parts.find((p) => p.type === t)?.value;
  return { weekday: get("weekday"), hour: Number(get("hour")) % 24, minute: Number(get("minute")) };
}

// All jobs whose rule matches this Central minute (a slot job and an interval job can coincide).
function jobsForNow(now) {
  const mins = now.hour * 60 + now.minute;
  return JOBS.filter((j) => {
    if (!j.daily && !WEEKDAYS.has(now.weekday)) return false;
    if (j.everyMinutes) return now.hour >= j.fromHour && now.hour < j.toHour && now.minute % j.everyMinutes === 0;
    const slot = j.hour * 60 + j.minute;
    return mins >= slot && mins < slot + TOLERANCE_MIN;
  });
}

async function dispatch(env, job) {
  const workflow = job.workflow || env.WORKFLOW_FILE;
  const url = `https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/${workflow}/dispatches`;
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
    body: JSON.stringify({ ref: env.GITHUB_REF, inputs: job.inputs || { mode: job.mode, gate: "true" } }),
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
    // The */5 interval cron also fires at each slot's minute (:50, :30, :15), so each cron
    // only starts its own kind of job; without this every slot job was dispatched twice
    // (two am-start runs 2 seconds apart on 2026-10-21).
    const fromInterval = event.cron === INTERVAL_CRON;
    const jobs = jobsForNow(now).filter((j) => Boolean(j.everyMinutes) === fromInterval);
    const stamp = `${now.weekday} ${String(now.hour).padStart(2, "0")}:${String(now.minute).padStart(2, "0")} Central`;
    if (!jobs.length) { console.log(`cron ${event.cron} at ${stamp}: no job due (other DST entry or outside hours), nothing to do`); return; }
    if (!env.GITHUB_TOKEN) {
      console.error("GITHUB_TOKEN secret is not set");
      ctx.waitUntil(slack(env, `:x: siftstack-dispatcher: GITHUB_TOKEN not set, could not start *${jobs.map((j) => j.mode).join(", ")}* at ${stamp}`));
      return;
    }
    for (const job of jobs) {
      const r = await dispatch(env, job);
      if (r.ok) { console.log(`dispatched ${job.mode} at ${stamp}`); continue; }
      console.error(`dispatch ${job.mode} failed: HTTP ${r.status} ${r.body}`);
      ctx.waitUntil(slack(env, `:x: siftstack-dispatcher could not start *${job.mode}* at ${stamp}: GitHub returned HTTP ${r.status} ${r.body.slice(0, 200)}. Fallback: run it by hand (GitHub Actions -> the workflow -> Run workflow).`));
    }
  },

  // GET /            -> status (what Central time it is, which jobs are due this minute)
  // POST /run/<mode> -> manual dispatch, requires header X-Dispatch-Key matching the DISPATCH_KEY secret
  async fetch(request, env) {
    const url = new URL(request.url);
    const now = centralNow();
    if (request.method === "POST" && url.pathname.startsWith("/run/")) {
      const mode = url.pathname.slice(5);
      if (!env.DISPATCH_KEY || request.headers.get("X-Dispatch-Key") !== env.DISPATCH_KEY) return new Response("forbidden", { status: 403 });
      const job = JOBS.find((j) => j.mode === mode);
      if (!job) return new Response("unknown mode", { status: 400 });
      const r = await dispatch(env, job);
      return new Response(r.ok ? `dispatched ${mode}` : `failed HTTP ${r.status} ${r.body}`, { status: r.ok ? 200 : 502 });
    }
    const due = jobsForNow(now).map((j) => j.mode);
    return new Response(JSON.stringify({ central: now, due_now: due, jobs: JOBS, token_set: Boolean(env.GITHUB_TOKEN) }, null, 2),
      { headers: { "Content-Type": "application/json" } });
  },
};
