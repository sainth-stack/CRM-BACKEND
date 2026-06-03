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
const ALL_QUEUES = "heavy_research,outbound_dispatch,inbox_polling,orchestrator";

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
      ...common,
      name: "crm-worker",
      args: `-A ${CELERY_APP} worker --loglevel=info --concurrency=2 -Q ${ALL_QUEUES} --hostname=worker@%h`,
    },
    {
      // CRITICAL: exactly ONE beat instance, ever. Never scale this.
      ...common,
      name: "crm-beat",
      args: `-A ${CELERY_APP} beat --loglevel=info --scheduler celery.beat.PersistentScheduler`,
    },
  ],
};
