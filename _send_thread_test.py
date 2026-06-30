"""
One-off live verification: send 10 real emails from bigdog.coc143@gmail.com to
vinaykumarreddy8374@gmail.com through the actual production dispatch path
(draft_dispatch.execute_draft_send), to confirm they land in one Gmail thread.

Creates a throwaway Campaign + DecisionMaker scoped to bigdog's account, then
creates and dispatches 10 EmailDraft rows one at a time (1 INITIAL + 9
FOLLOWUP), exactly the way a real campaign would, so the thread_id /
last_rfc_message_id chaining fix gets exercised for real.
"""
import time
from dotenv import load_dotenv
load_dotenv()

from app.db.database import SessionLocal
from app.db import models
from app.services.draft_dispatch import execute_draft_send
from app.agents.email_drafter import reply_subject

SENDER_EMAIL = "bigdog.coc143@gmail.com"
RECIPIENT_EMAIL = "vinaykumarreddy8374@gmail.com"
N_EMAILS = 10

db = SessionLocal()
try:
    user = db.query(models.User).filter(models.User.email == SENDER_EMAIL).first()
    if not user:
        raise SystemExit(f"No user found for {SENDER_EMAIL}")

    campaign = models.Campaign(
        user_id=user.id,
        name="[TEST] Email threading verification",
    )
    db.add(campaign)
    db.flush()

    dm = models.DecisionMaker(
        campaign_id=campaign.id,
        name="Vinay Kumar Reddy",
        email=RECIPIENT_EMAIL,
        position="Test Recipient",
    )
    db.add(dm)
    db.commit()

    print(f"Campaign: {campaign.id}")
    print(f"DecisionMaker: {dm.id}")
    print("=" * 70)

    root_subject_text = "Email threading verification"

    results = []
    for i in range(N_EMAILS):
        subject = root_subject_text if i == 0 else reply_subject(root_subject_text)
        draft_type = "INITIAL" if i == 0 else "FOLLOWUP"
        body = (
            f"Hi Vinay,\nHow are you?\n\n"
            f"This is test message #{i + 1} of {N_EMAILS}, confirming all {N_EMAILS} emails "
            f"land in the same Gmail thread.\n\n"
            "Best regards,\n\n"
            "BigDog\n"
            "Test Co"
        )

        draft = models.EmailDraft(
            campaign_id=campaign.id,
            decision_maker_id=dm.id,
            subject=subject,
            body=body,
            status="DRAFTED",
            followup_index=i,
            draft_type=draft_type,
            dispatch_state="IDLE",
        )
        db.add(draft)
        db.commit()

        result = execute_draft_send(draft.id)
        db.refresh(dm)
        print(
            f"  [{i + 1}/{N_EMAILS}] {draft_type:8s} status={result['status']:10s} "
            f"subject={subject!r} thread_id={dm.thread_id} last_rfc_message_id={dm.last_rfc_message_id}"
        )
        results.append(result)

        if i < N_EMAILS - 1:
            time.sleep(2)

    print("=" * 70)
    sent_ok = sum(1 for r in results if r["status"] in ("sent", "recovered"))
    print(f"RESULT: {sent_ok}/{N_EMAILS} sent successfully")
    print(f"Final dm.thread_id (should be the same single value for all 10): {dm.thread_id}")
    print(f"Final dm.last_rfc_message_id: {dm.last_rfc_message_id}")
    print(f"Campaign id (for cleanup if desired): {campaign.id}")
finally:
    db.close()
