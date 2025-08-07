# app/tasks.py
import requests
import time
import json
from datetime import datetime, timezone
from . import celery, db, create_app # Import create_app
from .models import Presentation, Slide, PresentationStatus, User # Import User
from .openai_helpers import build_image_prompt, generate_slide_image, get_style_description
from flask import url_for
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
def generate_single_slide_visual_task(self, slide_id, user_id, presentation_topic, presenter_name, total_slides, text_style_for_image, creativity_score, font_choice, presentation_style_prompt):
    """
    Celery task to generate image for a SINGLE slide.
    FIXED: Creates its own app context to ensure reliability.
    """
    # Create an app instance within the task for a reliable context
    app = create_app()
    with app.app_context():
        app.logger.info(f"Task Started: Generating visual for Slide ID: {slide_id} by User ID: {user_id} (Attempt {self.request.retries + 1})")
        app.logger.debug(f"Task Args: slide_id={slide_id}, user_id={user_id}, topic='{presentation_topic}', presenter='{presenter_name}', total={total_slides}, text_style='{text_style_for_image}', creativity={creativity_score}, font='{font_choice}'")

        slide = db.session.get(Slide, slide_id)
        if not slide:
            app.logger.error(f"Task Error: Slide ID {slide_id} not found.")
            return False # Non-retryable

        if slide.image_url:
            app.logger.info(f"Task Skip: Slide {slide.id} - image already exists.")
            return True # Considered success

        presentation = db.session.get(Presentation, slide.presentation_id)
        if not presentation:
            app.logger.error(f"Task Error: Presentation ID {slide.presentation_id} for Slide {slide_id} not found.")
            return False # Non-retryable

        # Check if presentation was cancelled or failed already
        if presentation.status == PresentationStatus.GENERATION_FAILED:
            app.logger.warning(f"Task Skip: Slide {slide.id} - Presentation {presentation.id} status is GENERATION_FAILED.")
            return False # Don't proceed if already marked as failed

        try:
            style_description_to_use = presentation_style_prompt
            slide_content_parsed = slide.text_content
            try:
                # Attempt to parse JSON only if it looks like JSON
                if slide.text_content and (slide.text_content.strip().startswith('[') or slide.text_content.strip().startswith('{')):
                    slide_content_parsed = json.loads(slide.text_content)
            except json.JSONDecodeError:
                app.logger.warning(f"Could not parse slide content as JSON for slide {slide.id}, passing as string.")
                pass

            image_gen_prompt_text = build_image_prompt(
                slide_title=slide.title, slide_content=slide_content_parsed, style_description=style_description_to_use,
                text_style=text_style_for_image, slide_number=slide.slide_number, total_slides=total_slides,
                creativity_score=creativity_score, presentation_topic=presentation_topic, font_choice=font_choice,
                presenter_name=presenter_name
            )

            relative_image_path, actual_prompt_used = generate_slide_image(
                image_prompt=image_gen_prompt_text, presentation_id=presentation.id, slide_number=slide.slide_number
            )

            if relative_image_path:
                slide.image_url = relative_image_path
                slide.image_gen_prompt = actual_prompt_used
                slide.applied_style_info = style_description_to_use
                db.session.add(slide)
                db.session.commit()
                app.logger.info(f"Task Success: Generated image for slide {slide.id}")
                return True
            else:
                app.logger.warning(f"Task Failure: Image generation helper returned None for slide {slide.id}.")
                return False

        # --- Specific Error Handling ---
        except RateLimitError as e:
            retry_delay = 60 * (2 ** self.request.retries)
            app.logger.warning(f"Task Rate Limited: Slide ID {slide_id}. Retrying in {retry_delay}s... (Attempt {self.request.retries + 1}/{TASK_RETRY_KWARGS['max_retries']}) Error: {e}")
            try:
                self.retry(countdown=retry_delay, exc=e, max_retries=TASK_RETRY_KWARGS['max_retries'])
            except MaxRetriesExceededError:
                app.logger.error(f"Task Failed: Max retries exceeded for Slide ID {slide_id} after RateLimitError. Error: {e}")
                db.session.rollback()
                presentation_fail = db.session.get(Presentation, slide.presentation_id)
                if presentation_fail and presentation_fail.status != PresentationStatus.GENERATION_FAILED:
                    presentation_fail.status = PresentationStatus.GENERATION_FAILED
                    db.session.add(presentation_fail)
                    db.session.commit()
                return False
        except SQLAlchemyError as e:
            db.session.rollback()
            app.logger.error(f"Task DB Error: Slide ID {slide_id}. Error: {e}", exc_info=True)
            try:
                self.retry(exc=e, countdown=30, max_retries=TASK_RETRY_KWARGS['max_retries'])
            except MaxRetriesExceededError:
                app.logger.error(f"Task Failed: Max retries exceeded for Slide ID {slide_id} after SQLAlchemyError. Error: {e}")
                return False
        except OpenAIError as e:
            db.session.rollback()
            app.logger.error(f"Task OpenAI Error: Slide ID {slide_id}. Error: {e}", exc_info=True)
            try:
                self.retry(exc=e, countdown=30, max_retries=TASK_RETRY_KWARGS['max_retries'])
            except MaxRetriesExceededError:
                app.logger.error(f"Task Failed: Max retries exceeded for Slide ID {slide_id} after OpenAIError. Error: {e}")
                presentation_fail = db.session.get(Presentation, slide.presentation_id)
                if presentation_fail and presentation_fail.status != PresentationStatus.GENERATION_FAILED:
                    presentation_fail.status = PresentationStatus.GENERATION_FAILED
                    db.session.add(presentation_fail)
                    db.session.commit()
                return False
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Task Unexpected Failure: Slide ID {slide_id}. Error: {e}", exc_info=True)
            presentation_fail = db.session.get(Presentation, slide.presentation_id)
            if presentation_fail and presentation_fail.status != PresentationStatus.GENERATION_FAILED:
                presentation_fail.status = PresentationStatus.GENERATION_FAILED
                db.session.add(presentation_fail)
                db.session.commit()
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
