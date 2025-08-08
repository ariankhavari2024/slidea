# app/tasks.py
import requests
import time
import json
from datetime import datetime, timezone
from . import celery, db, create_app # Import create_app
from .models import Presentation, Slide, PresentationStatus, User # Import User
from .openai_helpers import build_image_prompt, generate_slide_image, get_style_description
from flask import url_for, current_app
from openai import OpenAIError, RateLimitError
from requests.exceptions import RequestException
from sqlalchemy.exc import SQLAlchemyError
from celery.exceptions import MaxRetriesExceededError, Ignore
import base64
from sqlalchemy import func

RETRYABLE_ERRORS = (ConnectionError, OpenAIError, RequestException, SQLAlchemyError)
TASK_RETRY_KWARGS = {'max_retries': 3, 'countdown': 60}

# --- generate_single_slide_visual_task (FIXED: Creates its own app context) ---
@celery.task(bind=True, name='app.tasks.generate_single_slide_visual_task',
             autoretry_for=RETRYABLE_ERRORS,
             retry_kwargs=TASK_RETRY_KWARGS,
             rate_limit='4/m')
def generate_single_slide_visual_task(
    self,
    slide_id: int,
    user_id: int,
    presentation_topic: str,
    presenter_name: str,
    total_slides: int,
    text_style_for_image: str,
    creativity_score: int,
    font_choice: str,
    presentation_style_prompt: str,
):
    """
    Generate the image for ONE slide and persist success markers.

    Returns:
        True  -> success (image uploaded + DB updated)
        False -> hard failure (finalizer may mark presentation failed)
    """
    # We rely on Celery's ContextTask to give us an app context via current_app
    app = current_app._get_current_object()

    app.logger.info(
        f"[IMG] start slide={slide_id} user={user_id} try={self.request.retries + 1}"
    )
    app.logger.debug(
        f"[IMG] args: topic='{presentation_topic}' presenter='{presenter_name}' "
        f"total={total_slides} text_style='{text_style_for_image}' "
        f"creativity={creativity_score} font='{font_choice}'"
    )

    # --- Load DB objects
    slide = db.session.get(Slide, slide_id)
    if not slide:
        app.logger.error(f"[IMG] abort: slide {slide_id} not found")
        return False

    pres = db.session.get(Presentation, slide.presentation_id)
    if not pres:
        app.logger.error(f"[IMG] abort: presentation {slide.presentation_id} not found")
        return False

    if pres.status == PresentationStatus.GENERATION_FAILED:
        app.logger.warning(f"[IMG] skip: presentation {pres.id} already failed")
        return False

    # Idempotency: if image already there, we succeed
    if getattr(slide, "image_generated", False) and (slide.image_key or slide.image_url):
        app.logger.info(f"[IMG] skip: slide {slide.id} already has image")
        return True

    # --- Build prompt
    style_desc = presentation_style_prompt or ""
    slide_content_parsed = slide.text_content
    try:
        if isinstance(slide.text_content, str) and slide.text_content.strip()[:1] in "[{":
            import json
            slide_content_parsed = json.loads(slide.text_content)
    except Exception:
        app.logger.warning(f"[IMG] non-json slide content for slide={slide.id}; using raw string")

    image_prompt = build_image_prompt(
        slide_title=slide.title,
        slide_content=slide_content_parsed,
        style_description=style_desc,
        text_style=text_style_for_image,
        slide_number=slide.slide_number,
        total_slides=total_slides,
        creativity_score=creativity_score,
        presentation_topic=presentation_topic,
        font_choice=font_choice,
        presenter_name=presenter_name,
    )

    # --- Call OpenAI Images
    try:
        app.logger.info("[OAI] images.generate start")
        image_url_or_key, revised_prompt = generate_slide_image(
            image_prompt=image_prompt,
            presentation_id=pres.id,
            slide_number=slide.slide_number,
        )
        app.logger.info("[OAI] images.generate done")
    except RateLimitError as e:
        # exponential backoff
        delay = min(60 * (2 ** self.request.retries), 600)
        app.logger.warning(f"[OAI] rate limited; retry in {delay}s (attempt {self.request.retries+1}/{TASK_RETRY_KWARGS['max_retries']})")
        raise self.retry(exc=e, countdown=delay, max_retries=TASK_RETRY_KWARGS["max_retries"])
    except OpenAIError as e:
        app.logger.error(f"[OAI] error: {e}", exc_info=True)
        # retry a few times; if exhausted, return False
        raise self.retry(exc=e, countdown=30, max_retries=TASK_RETRY_KWARGS["max_retries"])
    except Exception as e:
        app.logger.exception(f"[OAI] unexpected exception: {e}")
        return False

    if not image_url_or_key:
        app.logger.error(f"[IMG] failed: no image returned for slide={slide.id}")
        return False

    # Our generate_slide_image returns a URL we can serve via /files/<key>
    # If yours returns a raw key instead, build the URL here.
    if image_url_or_key.startswith("/files/") or image_url_or_key.startswith("http"):
        final_url = image_url_or_key
        final_key = image_url_or_key.split("/files/", 1)[-1] if "/files/" in image_url_or_key else None
    else:
        # treat as key
        final_key = image_url_or_key
        final_url = url_for("main.serve_s3_file", key=final_key, _external=True)

    # --- Persist success
    try:
        slide.image_key = final_key
        slide.image_url = final_url
        slide.image_gen_prompt = revised_prompt
        slide.applied_style_info = style_desc
        slide.image_generated = True
        db.session.add(slide)
        db.session.commit()
        app.logger.info(f"[IMG] ok slide={slide.id} key={final_key} url={final_url}")
        return True
    except SQLAlchemyError as e:
        db.session.rollback()
        app.logger.error(f"[IMG] DB error on slide={slide.id}: {e}", exc_info=True)
        # small retry; DB hiccups happen
        raise self.retry(exc=e, countdown=15, max_retries=TASK_RETRY_KWARGS["max_retries"])
    except Exception as e:
        db.session.rollback()
        app.logger.exception(f"[IMG] unexpected DB exception slide={slide.id}: {e}")
        return False

# --- finalize_presentation_status_task (This task is already correct) ---
@celery.task(name='app.tasks.finalize_presentation_status_task')
def finalize_presentation_status_task(results, presentation_id, user_id, expected_slide_count, credits_deducted):
    """
    Celery task called *after* all generate_single_slide_visual_tasks in a group finish.
    """
    app = create_app()
    with app.app_context():
        app.logger.info(f"Task Started: Finalizing status for Presentation ID: {presentation_id} (User: {user_id}, Expected: {expected_slide_count}, Credits Deducted: {credits_deducted})")
        app.logger.debug(f"Chord results received for Pres {presentation_id}: {results}")

        presentation = db.session.get(Presentation, presentation_id)
        user = db.session.get(User, user_id)

        if not presentation:
            app.logger.error(f"Finalize Task Error: Presentation ID {presentation_id} not found.")
            raise Ignore()

        if not user:
            app.logger.error(f"Finalize Task Error: User ID {user_id} not found for Presentation {presentation_id}.")
            if presentation.status == PresentationStatus.PENDING_VISUALS:
                presentation.status = PresentationStatus.GENERATION_FAILED
                presentation.celery_chord_id = None
                db.session.commit()
            raise Ignore()

        if presentation.status == PresentationStatus.GENERATION_FAILED:
            app.logger.warning(f"Finalize Task: Presentation {presentation_id} was already marked as FAILED. Skipping status update, but attempting refund check.")
            if credits_deducted > 0:
                try:
                    user = db.session.merge(user) if not db.session.object_session(user) else user
                    user.credits_remaining += credits_deducted
                    db.session.add(user)
                    db.session.commit()
                    app.logger.info(f"Refunded {credits_deducted} credits to User {user_id} for cancelled/failed Pres {presentation_id}.")
                except Exception as e:
                    db.session.rollback()
                    app.logger.error(f"Error refunding credits to User {user_id} for cancelled/failed Pres {presentation_id}: {e}", exc_info=True)
            if presentation.celery_chord_id:
                presentation.celery_chord_id = None
                db.session.commit()
            return

        try:
            successful_slides = 0
            if isinstance(results, list):
                valid_results = [res for res in results if res is not None]
                successful_slides = sum(1 for result in valid_results if result is True)
                if len(valid_results) != len(results):
                    app.logger.warning(f"Finalize Task: Some tasks in chord for Pres {presentation_id} did not return a boolean result. Valid results: {len(valid_results)}/{len(results)}")
                if successful_slides != len(valid_results):
                    app.logger.warning(f"Finalize Task: Not all completed tasks reported success for Pres {presentation_id}. Success Count: {successful_slides}/{len(valid_results)}")
            else:
                app.logger.error(f"Finalize Task: Unexpected format for 'results' for Pres {presentation_id}: {type(results)}. Assuming failure.")

            actual_images_generated = db.session.query(func.count(Slide.id)).filter(
                Slide.presentation_id == presentation_id,
                Slide.image_url.isnot(None)
            ).scalar() or 0

            generation_successful = (successful_slides > 0 and actual_images_generated >= successful_slides)

            if not generation_successful:
                app.logger.warning(f"Finalize Task: Mismatch or no success for Pres {presentation_id}. Task Success: {successful_slides}, Found Images: {actual_images_generated}")

            if generation_successful:
                final_status = PresentationStatus.VISUALS_COMPLETE
                app.logger.info(f"Finalizing Presentation {presentation_id} status to VISUALS_COMPLETE.")
            else:
                final_status = PresentationStatus.GENERATION_FAILED
                app.logger.warning(f"Finalizing Presentation {presentation_id} status to GENERATION_FAILED (Task Success: {successful_slides}, Images Found: {actual_images_generated}).")
                if credits_deducted > 0:
                    user = db.session.merge(user) if not db.session.object_session(user) else user
                    user.credits_remaining += credits_deducted
                    db.session.add(user)
                    app.logger.info(f"Refunding {credits_deducted} credits to User {user_id} due to failed generation for Pres {presentation_id}.")
                else:
                    app.logger.info(f"No credits to refund for failed Pres {presentation_id} (Amount was {credits_deducted}).")

            presentation.status = final_status
            presentation.last_edited_at = datetime.now(timezone.utc)
            presentation.celery_chord_id = None
            db.session.commit()
            app.logger.info(f"Finalize Task: Status update committed for Presentation {presentation_id} to {final_status.name}.")

        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Finalize Task Error: Unexpected error during finalization for Presentation ID {presentation_id}: {e}", exc_info=True)
            try:
                presentation_fallback = db.session.get(Presentation, presentation_id)
                user_fallback = db.session.get(User, user_id)
                if presentation_fallback and presentation_fallback.status == PresentationStatus.PENDING_VISUALS:
                    presentation_fallback.status = PresentationStatus.GENERATION_FAILED
                    presentation_fallback.last_edited_at = datetime.now(timezone.utc)
                    presentation_fallback.celery_chord_id = None
                    if user_fallback and credits_deducted > 0:
                        user_fallback = db.session.merge(user_fallback) if not db.session.object_session(user_fallback) else user_fallback
                        user_fallback.credits_remaining += credits_deducted
                        db.session.add(user_fallback)
                        app.logger.info(f"Refunding {credits_deducted} credits to User {user_id} due to finalize task exception for Pres {presentation_id}.")
                    elif not user_fallback:
                        app.logger.error(f"Could not find User {user_id} to refund credits after finalize task exception for Pres {presentation_id}.")
                    db.session.commit()
                    app.logger.warning(f"Finalize Task: Set Presentation {presentation_id} status to GENERATION_FAILED due to finalize task exception.")
            except Exception as inner_e:
                app.logger.error(f"Finalize Task: Failed to set status/refund after error in finalize task for {presentation_id}: {inner_e}")
