// =============================================================================
// AI-PRIORI Backend — PM2 process manifest for the Celery layer (t2.medium).
//
// The FastAPI API does NOT run the campaign pipeline — it only enqueues Celery
// tasks. The pipeline + the durable email-dispatch poller run on the Celery
// workers + beat defined below.
//
// WHY TWO WORKERS (not one):
//   The outbound email dispatcher is a Celery Beat task (dispatch_due_drafts_task,
//   every 60s on the `outbound_dispatch` queue) that actually SENDS scheduled
//   drafts. If a SINGLE worker serves both `heavy_research` and
//   `outbound_dispatch`, the multi-minute, memory-heavy research tasks occupy all
//   concurrency slots and the dispatch poller never runs. Drafts go overdue, and
//   when the poller finally runs it can only send 1/user/180s and BUMPS the rest
//   forward — which looks like "scheduled emails keep getting rescheduled and
//   never send". Isolating outbound_dispatch on its own lightweight worker fixes
//   this: the poller fires every 60s regardless of what heavy_research is doing.
//
// FITS A t2.medium (2 vCPU / 4 GiB):
//   heavy: concurrency=1, hard-capped at ~600 MB/child  -> ≤ ~0.6 GB
//   light: concurrency=2, cheap tasks (dispatch/inbox)  -> ~0.3–0.4 GB
//   beat : negligible
//   Plenty of headroom alongside crm-api + crm-frontend.
//
// DEPLOY (from the `backend/` directory). This REPLACES the old single
// `crm-celery-worker` and the existing `crm-celery-beat`; it does NOT touch
// crm-api / crm-frontend:
//     pm2 delete crm-celery-worker crm-celery-beat   # remove the old combined worker + beat
//     pm2 start ecosystem.config.cjs                 # start heavy + light + beat
//     pm2 save                                        # persist across reboots
//     pm2 logs crm-celery-worker-light               # watch the dispatch poller
//
// NOTE: `celery` must be resolvable on PATH (your current setup already runs it
// via pm2, so it is). If you use a virtualenv whose PATH is not inherited, set
// `script` to the absolute binary, e.g. script: "/srv/app/backend/venv/bin/celery".
// CRITICAL: never run more than ONE beat instance.
// =============================================================================

const CELERY_APP = "app.workers.config.celery_app";
const HEAVY_QUEUE = "heavy_research";
const LIGHT_QUEUES = "outbound_dispatch,inbox_polling,orchestrator";

// Upstash is billed per command + caps connections, so suppress Celery's
// mingle/gossip/heartbeat chatter (pure overhead for a single small box).
const QUIET = "--without-mingle --without-gossip --without-heartbeat";

const common = {
  cwd: __dirname,          // run from backend/
  interpreter: "none",     // celery is a native binary, not a node script
  script: "celery",
  autorestart: true,
  max_restarts: 10,
  // env: { REDIS_URL: "rediss://...", } // usually inherited from the shell / .env
};

module.exports = {
  apps: [
    {
      // Heavy AI pipeline (Stages 3/4/5). I/O-bound (Tavily + OpenAI) but the
      // langchain object graphs are RAM-heavy, so keep concurrency low and recycle
      // children aggressively to keep memory flat and avoid the OOM-killer.
      //   --concurrency=1          : one heavy task at a time on a small box
      //   --max-tasks-per-child=8  : restart each child after 8 tasks to reclaim RAM
      //   --max-memory-per-child   : hard ceiling (KB) — child restarts if exceeded (~600MB)
      ...common,
      name: "crm-celery-worker-heavy",
      args: `-A ${CELERY_APP} worker --loglevel=info --concurrency=1 -Q ${HEAVY_QUEUE} ` +
            `--max-tasks-per-child=8 --max-memory-per-child=600000 ${QUIET} --hostname=heavy@%h`,
    },
    {
      // Latency-sensitive queues: email dispatch poller + send executor, inbox
      // polling, orchestration sweeps. Cheap tasks; this worker MUST stay
      // unblocked so dispatch_due_drafts_task fires on time. Never share it with
      // heavy_research.
      ...common,
      name: "crm-celery-worker-light",
      args: `-A ${CELERY_APP} worker --loglevel=info --concurrency=2 -Q ${LIGHT_QUEUES} ` +
            `--max-tasks-per-child=100 ${QUIET} --hostname=light@%h`,
    },
    {
      // CRITICAL: exactly ONE beat instance, ever. Never scale this.
      ...common,
      name: "crm-celery-beat",
      args: `-A ${CELERY_APP} beat --loglevel=info --scheduler celery.beat.PersistentScheduler`,
    },
  ],
};
