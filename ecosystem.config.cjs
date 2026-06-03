// =============================================================================
// AI-PRIORI Backend — PM2 process manifest for the deployed server.
//
// The FastAPI API does NOT run the campaign pipeline — it only enqueues Celery
// tasks. The pipeline runs on the Celery worker + beat defined below.
//
// CAPACITY-OPTIMIZED LAYOUT (2 processes):
//   1. crm-worker — ONE worker consuming ALL four queues. A single worker can
//      serve multiple queues via `-Q a,b,c`; the per-queue routing in
//      celery_app.py is unchanged. `--concurrency=2` = at most 2 tasks at once
//      (tune up only if the box has spare RAM/CPU — heavy_research is memory-heavy).
//   2. crm-beat — the scheduler. Lightweight, but MUST be exactly one instance.
//
// Run from the `backend/` directory:
//     pm2 start ecosystem.config.cjs
//     pm2 save        # persist across reboots
//     pm2 logs        # tail all processes
//
// NOTE: `celery` must be resolvable on PATH. If you use a virtualenv and PATH
// is not inherited, set `script` to the absolute binary, e.g.
//     script: "/srv/app/backend/venv/bin/celery"
//
// EVEN LEANER (1 process): delete the crm-beat app below and append `-B` to the
// worker args to embed the scheduler in the worker. Acceptable for a single
// small instance; never run more than one such worker.
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
      // Heavy AI pipeline (Stages 3/4/5). Mostly I/O-bound (Tavily + OpenAI),
      // so a small prefork pool + per-child recycling keeps memory flat and
      // prevents the langchain memory creep that was triggering the OOM-killer.
      //   --max-tasks-per-child=8 : restart each child after 8 tasks to reclaim RAM
      //   --max-memory-per-child   : hard ceiling (KB) — child restarts if exceeded (~600MB)
      ...common,
      name: "crm-worker-heavy",
      args: `-A ${CELERY_APP} worker --loglevel=info --concurrency=2 -Q ${HEAVY_QUEUE} ` +
            `--max-tasks-per-child=8 --max-memory-per-child=600000 ${QUIET} --hostname=heavy@%h`,
    },
    {
      // Light/latency-sensitive queues (email dispatch, inbox polling,
      // orchestration sweeps). Cheap tasks; modest concurrency is fine.
      ...common,
      name: "crm-worker-light",
      args: `-A ${CELERY_APP} worker --loglevel=info --concurrency=2 -Q ${LIGHT_QUEUES} ` +
            `--max-tasks-per-child=100 ${QUIET} --hostname=light@%h`,
    },
    {
      // CRITICAL: exactly ONE beat instance, ever. Never scale this.
      ...common,
      name: "crm-beat",
      args: `-A ${CELERY_APP} beat --loglevel=info --scheduler celery.beat.PersistentScheduler`,
    },
  ],
};
