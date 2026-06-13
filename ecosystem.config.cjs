// =============================================================================
// AI-PRIORI — Full PM2 manifest (API + frontend + 2 Celery workers + beat).
//
// THREE-WORKER layout (5 PM2 apps total):
//   crm-api                         FastAPI on 127.0.0.1:9001 (nginx proxies /api/)
//   crm-frontend                    React static bundle on 127.0.0.1:9000
//   crm-celery-worker-heavy         heavy_research   (AI / Tavily — memory-heavy)
//   crm-celery-worker-light         outbound_dispatch, inbox_polling, orchestrator (latency-sensitive/orchestration)
//   crm-celery-beat                 scheduler — exactly ONE instance, never scale
//
// WHY THREE WORKERS:
//   Conserves system memory while keeping the heavy research worker completely isolated.
//
// ⚠️ MEMORY ON t2.medium (4 GiB): 2 workers + beat = 3 Python processes.
//   This layout runs with a lower memory footprint than the 5-worker layout.
//
// DEPLOY (from CRM-BACKEND/):
//     cd /home/ubuntu/CRM-BACKEND
//     pm2 delete all
//     pm2 start ecosystem.config.cjs
//     pm2 save
//     pm2 status
//
// PM2 does NOT inherit shell venv PATH — all binaries use absolute paths below.
// Frontend lives as a sibling: ../CRM-FRONTEND (run `npm run build` before start).
// =============================================================================

const path = require("path");

const BACKEND_DIR = __dirname;
const FRONTEND_DIR = path.join(BACKEND_DIR, "..", "CRM-FRONTEND");
const CELERY_BIN = path.join(BACKEND_DIR, "venv", "bin", "celery");
const UVICORN_BIN = path.join(BACKEND_DIR, "venv", "bin", "uvicorn");
const SERVE_BIN = process.env.SERVE_BIN || "/usr/local/bin/serve";

const CELERY_APP = "app.workers.config.celery_app";

// Per-queue concurrency — all env-overridable so you can right-size the box
// without editing this file. Defaults are conservative for a t2.medium.
const HEAVY_CONCURRENCY = process.env.HEAVY_CONCURRENCY || "1"; // "keep the research as 1 worker"
const LIGHT_CONCURRENCY = process.env.LIGHT_CONCURRENCY || "6"; // Combined concurrency for outbound, inbox, orchestrator

// Upstash is billed per command + caps connections, so suppress Celery's
// mingle/gossip/heartbeat chatter (pure overhead for a single small box).
const QUIET = "--without-mingle --without-gossip --without-heartbeat";

const celeryCommon = {
  cwd: BACKEND_DIR,
  interpreter: "none",
  script: CELERY_BIN,
  autorestart: true,
  max_restarts: 10,
};

module.exports = {
  apps: [
    {
      name: "crm-api",
      cwd: BACKEND_DIR,
      script: UVICORN_BIN,
      args: "main:app --host 127.0.0.1 --port 9001 --workers 2",
      interpreter: "none",
      autorestart: true,
      max_restarts: 10,
    },
    {
      name: "crm-frontend",
      script: SERVE_BIN,
      args: `-s ${path.join(FRONTEND_DIR, "dist")} -l 9000`,
      interpreter: "none",
      autorestart: true,
      max_restarts: 10,
    },
    {
      // Heavy AI pipeline (Stages 3/4/5/6). Per-child memory cap + recycling keeps
      // langchain memory flat and defeats the OOM-killer between tasks.
      ...celeryCommon,
      name: "crm-celery-worker-heavy",
      args: `-A ${CELERY_APP} worker --loglevel=info --concurrency=${HEAVY_CONCURRENCY} -Q heavy_research ` +
            `--max-tasks-per-child=8 --max-memory-per-child=600000 ${QUIET} --hostname=heavy@%h`,
    },
    {
      // Light / latency-sensitive queues (email dispatch, inbox polling, orchestration) combined.
      ...celeryCommon,
      name: "crm-celery-worker-light",
      args: `-A ${CELERY_APP} worker --loglevel=info --concurrency=${LIGHT_CONCURRENCY} -Q outbound_dispatch,inbox_polling,orchestrator ` +
            `--max-tasks-per-child=200 ${QUIET} --hostname=light@%h`,
    },
    {
      // CRITICAL: exactly ONE beat instance, ever. Never scale this.
      ...celeryCommon,
      name: "crm-celery-beat",
      args: `-A ${CELERY_APP} beat --loglevel=info --scheduler celery.beat.PersistentScheduler`,
    },
  ],
};
