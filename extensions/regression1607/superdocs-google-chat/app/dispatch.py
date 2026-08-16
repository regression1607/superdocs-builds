"""Google Chat webhook dispatch.

The dispatch logic is a set of pure functions that take a Chat event dict + the
injected SuperDocs client + store, and return the JSON response Chat renders.
Keeping them pure means the whole flow is testable with a fake client and no key.

Flow:
  @mention a request         → MESSAGE event    → draft/edit via SuperDocs, poll to
                                                   the gate, post a summary card + an
                                                   item-by-item approval card.
  press Approve/Reject (item) → CARD_CLICKED     → record the decision, update the
                                                   card in place (nothing applied yet).
  press Apply / Approve all   → CARD_CLICKED     → approve/deny per the recorded
                                                   decisions, export, post a result card.
  @mention "digest"          → MESSAGE event    → post the space's digest.

"Using last year's" (and similar) reuses the space's most recent SuperDocs
session, so the new instruction genuinely builds on the prior document instead of
starting from a blank seed.
"""

from __future__ import annotations

import re
import uuid

from app import cards
from app.store import Store
from app.superdocs import SuperDocsClient

_MENTION = re.compile(r"<users/\d+>|@\S+")
# References to prior work → reuse the space's last session for genuine continuity.
_PRIOR = re.compile(r"\b(last year'?s|last time|previous|prior|earlier|the same|again|update it|revise|based on)\b",
                    re.IGNORECASE)


def _clean(text: str) -> str:
    return _MENTION.sub("", text or "").strip()


def _space(event: dict) -> str:
    return (event.get("space") or {}).get("name", "unknown-space")


def handle_event(event: dict, client: SuperDocsClient, store: Store, *,
                 seed_template: str, export_format: str = "docx") -> dict:
    etype = event.get("type")
    if etype == "MESSAGE":
        return _on_message(event, client, store, seed_template=seed_template,
                           export_format=export_format)
    if etype == "CARD_CLICKED":
        return _on_card_click(event, client, store, export_format=export_format)
    if etype == "ADDED_TO_SPACE":
        return cards.text("Hi! Mention me with a request like "
                          "*draft the renewal letter for Acme* — I'll draft it and post an "
                          "approval card you can approve item by item. Say *digest* for a summary.")
    return cards.text("")


def _on_message(event: dict, client: SuperDocsClient, store: Store, *,
                seed_template: str, export_format: str) -> dict:
    space = _space(event)
    request = _clean((event.get("message") or {}).get("text", ""))
    if not request:
        return cards.text("Tell me what to draft, e.g. *draft the renewal letter for Acme*.")
    if request.lower() in ("digest", "daily digest", "status"):
        produced, pending = store.digest(space)
        return cards.digest_card(produced=produced, pending=pending)

    # Continuity: reuse the space's most recent session when the ask references prior work.
    prior = store.recent_session(space) if _PRIOR.search(request) else None
    if prior:
        session_id, doc_title, document_html = prior.session_id, prior.doc_title, None
    else:
        session_id, doc_title, document_html = f"chat-{uuid.uuid4().hex[:12]}", _title_from(request), seed_template
        client.upload_document(session_id, f"{doc_title}.html", seed_template)

    job = _run_edit(client, session_id, request, document_html)
    if job is None or job.get("status") == "failed":
        err = (job or {}).get("error") or "the model could not complete that request"
        return cards.text(f"Sorry — SuperDocs could not complete that: {err}")

    changes = client.pending_changes(job)
    job_id = job.get("job_id") or job.get("id") or ""
    review = store.open_review(space, session_id, job_id, doc_title, request, changes)

    if not changes:
        store.mark_applied(space, session_id, 0, 0, state="no changes")
        return cards.summary_card(doc_title=doc_title, doc_link=None, request=request,
                                  produced="No changes were needed — the document already satisfies it.")

    draft_link, _ = _download(client, session_id, export_format)
    summary = cards.summary_card(
        doc_title=doc_title, doc_link=draft_link, request=request,
        produced=f"{len(changes)} proposed change(s) awaiting review.")
    approval = cards.approval_card(session_id=session_id, job_id=job_id,
                                   doc_title=doc_title, changes=review.changes)
    return {"cardsV2": [*summary["cardsV2"], *approval["cardsV2"]]}


def _on_card_click(event: dict, client: SuperDocsClient, store: Store, *, export_format: str) -> dict:
    action = event.get("action") or {}
    fn = action.get("function") or action.get("actionMethodName")
    params = {p["key"]: p["value"] for p in action.get("parameters", [])}
    space = _space(event)
    session_id = params.get("session_id", "")

    review = store.review(space, session_id)
    if not review:
        return cards.updated(cards.text("This review has expired — please send the request again."))

    if fn == "approve_one":
        store.set_decision(space, session_id, params.get("change_id", ""), True)
        return _refresh(review)
    if fn == "reject_one":
        store.set_decision(space, session_id, params.get("change_id", ""), False)
        return _refresh(review)
    if fn == "approve_all":
        store.decide_all(space, session_id, True)
    elif fn == "reject_all":
        store.decide_all(space, session_id, False)
    # apply_decisions / approve_all / reject_all all fall through to applying now.
    return _apply(client, store, space, review, export_format)


def _apply(client: SuperDocsClient, store: Store, space: str, review, export_format: str) -> dict:
    # Undecided items default to rejected — never apply a change nobody reviewed.
    decisions = [{"change_id": c.change_id, "approved": c.decision == "approved"}
                 for c in review.changes]
    client.approve_changes(review.session_id, review.job_id, decisions)
    applied = sum(1 for d in decisions if d["approved"])
    rejected = len(decisions) - applied

    download, filename = (None, None)
    if applied:
        client.poll_job(review.job_id)  # let the job resume past approval to completion
        download, filename = _download(client, review.session_id, export_format)
    store.mark_applied(space, review.session_id, applied, rejected)
    return cards.updated(cards.result_card(doc_title=review.doc_title, applied=applied,
                                           rejected=rejected, download_url=download, filename=filename))


def _refresh(review) -> dict:
    return cards.updated(cards.approval_card(session_id=review.session_id, job_id=review.job_id,
                                             doc_title=review.doc_title, changes=review.changes))


def _run_edit(client: SuperDocsClient, session_id: str, request: str, document_html):
    """Start the edit and poll to the review gate, retrying once past the
    documented fresh-session warmup failure."""
    for _ in range(2):
        job_id = client.chat_edit(session_id, request, document_html=document_html)
        job = client.poll_job(job_id)
        job.setdefault("job_id", job_id)
        if job.get("status") != "failed":
            return job
        document_html = None  # session already holds the doc on the retry
    return job


def _download(client: SuperDocsClient, session_id: str, fmt: str):
    """Return a real short-lived signed (url, filename), or (None, None) if the
    download endpoint is unavailable — never fabricate a link."""
    try:
        dl = client.download_url(session_id, fmt)
        return dl.get("download_url"), dl.get("filename")
    except Exception:
        return None, None


def _title_from(text: str) -> str:
    words = _clean(text).split()
    return " ".join(words[:6]).title() or "Document"
