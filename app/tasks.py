# app/tasks.py
import json
from datetime import datetime, timezone

from flask import current_app
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from requests.exceptions import RequestException
from celery.exceptions import MaxRetriesExceededError, Ignore

from app import celery, db
from app.models import Presentation, Slide, PresentationStatus, User
from app.openai_helpers import build_image_prompt, generate_slide_image
from openai import OpenAIError, RateLimitError

RETRYABLE_ERRORS = (ConnectionError, OpenAIError, RequestException, SQLAlchemyError)
TASK_RETRY_KWARGS = {"max_retries": 3, "countdown": 60}

@celery.task(
    bind=True,
    name="app.tasks.generate_single_slide_visual_task",
    autoretry_for=RETRYABLE_ERRORS,
    retry_kwargs=TASK_RETRY_KWARGS,
    rate_limit="4/m",
)
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
    app = current_app._get_current_object()
    app.logger.info(f"[IMG] start slide={slide_id} user={user_id} try={self.request.retries + 1}")

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

    if getattr(slide, "image_generated", False) and (slide.image_key or slide.image_url):
        app.logger.info(f"[IMG] skip: slide {slide.id} already has image")
        return True

    style_desc = presentation_style_prompt or ""
    slide_content_parsed = slide.text_content
    try:
        if isinstance(slide.text_content, str) and slide.text_content.strip()[:1] in "[{":
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

    try:
        app.logger.info("[OAI] images.generate start")
        image_key, revised_prompt = generate_slide_image(
            image_prompt=image_prompt,
            presentation_id=pres.id,
            slide_number=slide.slide_number,
        )
        app.logger.info("[OAI] images.generate done")
    except RateLimitError as e:
        delay = min(60 * (2 ** self.request.retries), 600)
        app.logger.warning(
            f"[OAI] rate limited; retry in {delay}s "
            f"(attempt {self.request.retries + 1}/{TASK_RETRY_KWARGS['max_retries']})"
        )
        raise self.retry(exc=e, countdown=delay, max_retries=TASK_RETRY_KWARGS["max_retries"])
    except OpenAIError as e:
        app.logger.error(f"[OAI] error: {e}", exc_info=True)
        raise self.retry(exc=e, countdown=30, max_retries=TASK_RETRY_KWARGS["max_retries"])
    except Exception as e:
        app.logger.exception(f"[OAI] unexpected exception: {e}")
        return False

    if not image_key:
        app.logger.error(f"[IMG] failed: no image returned for slide={slide.id}")
        return False

    stable_url = f"/files/{image_key}"

    try:
        slide.image_key = image_key
        slide.image_url = stable_url
        slide.image_gen_prompt = revised_prompt
        slide.applied_style_info = style_desc
        slide.image_generated = True
        db.session.add(slide)
        db.session.commit()
        app.logger.info(f"[IMG] ok slide={slide.id} key={image_key} url={stable_url}")
        return True
    except SQLAlchemyError as e:
        db.session.rollback()
        app.logger.error(f"[IMG] DB error on slide={slide.id}: {e}", exc_info=True)
        raise self.retry(exc=e, countdown=15, max_retries=TASK_RETRY_KWARGS["max_retries"])
    except Exception as e:
        db.session.rollback()
        app.logger.exception(f"[IMG] unexpected DB exception slide={slide.id}: {e}")
        return False

@celery.task(name="app.tasks.finalize_presentation_status_task")
def finalize_presentation_status_task(results, presentation_id, user_id, expected_slide_count, credits_deducted):
    app = current_app._get_current_object()
    app.logger.info(
        f"Task Started: Finalizing status for Presentation ID: {presentation_id} "
        f"(User: {user_id}, Expected: {expected_slide_count}, Credits Deducted: {credits_deducted})"
    )
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
        app.logger.warning(
            f"Finalize Task: Presentation {presentation_id} already FAILED. Skipping status update, attempting refund."
        )
        if credits_deducted > 0:
            try:
                user = db.session.merge(user) if not db.session.object_session(user) else user
                user.credits_remaining += credits_deducted
                db.session.add(user)
                db.session.commit()
                app.logger.info(f"Refunded {credits_deducted} credits to User {user_id} for cancelled/failed Pres {presentation_id}.")
            except Exception as e:
                db.session.rollback()
                app.logger.error(f"Refund error for User {user_id} on Pres {presentation_id}: {e}", exc_info=True)
        if presentation.celery_chord_id:
            presentation.celery_chord_id = None
            db.session.commit()
        return

    try:
        successful_slides = 0
        if isinstance(results, list):
            valid_results = [res for res in results if res is not None]
            successful_slides = sum(1 for r in valid_results if r is True)
            if len(valid_results) != len(results):
                app.logger.warning(
                    f"Finalize Task: Some tasks did not return boolean. Valid={len(valid_results)}/{len(results)}"
                )
            if successful_slides != len(valid_results):
                app.logger.warning(
                    f"Finalize Task: Not all tasks reported success. "
                    f"Success Count: {successful_slides}/{len(valid_results)}"
                )
        else:
            app.logger.error(
                f"Finalize Task: Unexpected 'results' type: {type(results)}. Assuming failure."
            )

        actual_images_generated = (
            db.session.query(func.count(Slide.id))
            .filter(Slide.presentation_id == presentation_id, Slide.image_url.isnot(None))
            .scalar()
            or 0
        )

        generation_successful = (successful_slides > 0 and actual_images_generated >= successful_slides)

        if generation_successful:
            final_status = PresentationStatus.VISUALS_COMPLETE
            app.logger.info(f"Finalizing Presentation {presentation_id} status to VISUALS_COMPLETE.")
        else:
            final_status = PresentationStatus.GENERATION_FAILED
            app.logger.warning(
                f"Finalizing Presentation {presentation_id} to GENERATION_FAILED "
                f"(Task Success: {successful_slides}, Images Found: {actual_images_generated})."
            )
            if credits_deducted > 0:
                user = db.session.merge(user) if not db.session.object_session(user) else user
                user.credits_remaining += credits_deducted
                db.session.add(user)

        presentation.status = final_status
        presentation.last_edited_at = datetime.now(timezone.utc)
        presentation.celery_chord_id = None
        db.session.commit()
        app.logger.info(f"Finalize Task: Status update committed for Presentation {presentation_id} to {final_status.name}.")

    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Finalize Task Error: Unexpected error during finalization for Presentation {presentation_id}: {e}", exc_info=True)
        try:
            p = db.session.get(Presentation, presentation_id)
            u = db.session.get(User, user_id)
            if p and p.status == PresentationStatus.PENDING_VISUALS:
                p.status = PresentationStatus.GENERATION_FAILED
                p.last_edited_at = datetime.now(timezone.utc)
                p.celery_chord_id = None
                if u and credits_deducted > 0:
                    u = db.session.merge(u) if not db.session.object_session(u) else u
                    u.credits_remaining += credits_deducted
                    db.session.add(u)
                db.session.commit()
                app.logger.warning(f"Finalize Task: Fallback set Pres {presentation_id} to GENERATION_FAILED.")
        except Exception as inner:
            app.logger.error(f"Finalize Task: Fallback also failed: {inner}")
