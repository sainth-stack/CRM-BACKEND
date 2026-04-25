from app.core.logging_config import logger
from app.services.draft_dispatch import execute_draft_send
from app.workers.config.celery_app import celery_app


@celery_app.task(bind=True, name="app.workers.tasks.outbound_worker.send_draft_worker")
def send_draft_worker(self, draft_id: str):
    """
    Background draft dispatcher.
    Keeps the HTTP API fast while preserving the exact same send semantics in worker space.
    """
    result = execute_draft_send(draft_id)
    if result["status"] == "failed":
        logger.error(f"[OUTBOUND] Draft {draft_id} failed in background dispatch: {result['message']}")
    return result
