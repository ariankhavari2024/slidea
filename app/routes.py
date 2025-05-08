# app/routes.py
from flask import (render_template, Blueprint, flash, redirect, url_for,
                   request, current_app, jsonify, abort, send_file, session)
from flask_login import login_user, current_user, logout_user, login_required
from celery import group, chord # For Celery task grouping
from celery.result import AsyncResult # To check Celery task status
from . import db, csrf, celery as celery_app # Import db, csrf, and Celery instance
from .models import User, Presentation, Slide, PresentationStatus
from .forms import RegistrationForm, LoginForm, CreatePresentationForm, ContactForm # Added ContactForm
from .openai_helpers import (generate_text_content, parse_manual_content,
                             get_style_description, build_image_prompt, generate_slide_image,
                             generate_missing_slide_content)
import json
import os
import shutil # For deleting presentation folders
import io # For PPTX export
import traceback # For detailed error logging
from .tasks import (generate_single_slide_visual_task,
                    finalize_presentation_status_task) # Celery tasks

# Import for PPTX generation
try:
    from pptx import Presentation as PptxPresentation
    from pptx.util import Inches, Pt
    from pptx.enum.text import PP_ALIGN # For text alignment if needed
    PPTX_INSTALLED = True
except ImportError:
    PPTX_INSTALLED = False
    # Consider logging this warning at app startup if not already done
    # current_app.logger.warning("python-pptx library not found. PPTX export will be disabled.")

# SQLAlchemy specific imports
from sqlalchemy import and_, func # For database queries
from sqlalchemy.orm import aliased # For database queries
from datetime import datetime, timezone # For setting default timestamps with timezone

# Stripe Python library
import stripe

# Define the main blueprint for application routes
main = Blueprint('main', __name__)

# --- Standard Page Routes (Home, About, Features, Contact) ---
@main.route('/')
def index():
    """Renders the home page."""
    return render_template('index.html', title='Welcome', current_user=current_user)

@main.route('/about')
def about():
    """Renders the about page."""
    return render_template('about.html', title='About Us', current_user=current_user)

@main.route('/features')
def features():
    """Renders the features page."""
    return render_template('features.html', title='Features', current_user=current_user)

@main.route('/contact', methods=['GET', 'POST'])
def contact():
    """Renders the contact page and handles form submission."""
    form = ContactForm()
    if form.validate_on_submit():
        # Here you would typically process the contact form data,
        # e.g., send an email to your support address.
        # For this example, we'll just flash a success message.
        name = form.name.data
        email = form.email.data
        subject = form.subject.data
        message_body = form.message.data
        current_app.logger.info(f"Contact form submission: Name='{name}', Email='{email}', Subject='{subject}'")
        # Example: send_contact_email(name, email, subject, message_body)
        flash('Thank you for your message! We will get back to you soon.', 'success')
        return redirect(url_for('main.contact')) # Redirect to clear form
    elif request.method == 'POST': # If form validation failed on POST
        flash('Please correct the errors in the form.', 'warning')
    return render_template('contact.html', title='Contact Us', form=form, current_user=current_user)


# --- User Authentication & Account Routes ---
@main.route('/register', methods=['GET', 'POST'])
def register():
    """Handles user registration."""
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard'))
    form = RegistrationForm()
    if form.validate_on_submit():
        new_user = User(name=form.name.data, email=form.email.data)
        new_user.set_password(form.password.data)
        try:
            # Assign initial credits and default plan upon registration
            new_user.credits_remaining = current_app.config['CREDITS_PER_PLAN'].get('free', 0)
            new_user.subscription_plan_name = 'free' # Default to free plan
            new_user.subscription_status = 'active' # Free plan is active by default

            db.session.add(new_user)
            db.session.commit()
            flash(f'Account created! You get {new_user.credits_remaining} free credits. You can now log in.', 'success')
            return redirect(url_for('main.login'))
        except Exception as e:
            db.session.rollback() # Rollback in case of DB error
            current_app.logger.error(f'Registration Error: {e}', exc_info=True)
            flash('An error occurred during registration. Please try again.', 'danger')
    elif request.method == 'POST': # If form validation failed on POST
        current_app.logger.warning(f"Registration validation failed: {form.errors}")
        flash('Please correct the errors below to register.', 'warning')
    return render_template('register.html', title='Register', form=form, current_user=current_user)

@main.route('/login', methods=['GET', 'POST'])
def login():
    """Handles user login."""
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard'))
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data).first()
        if user and user.check_password(form.password.data):
            login_user(user, remember=form.remember.data)
            flash('Login successful!', 'success')
            next_page = request.args.get('next') # For redirecting after login
            return redirect(next_page or url_for('main.dashboard'))
        else:
            flash('Login unsuccessful. Please check email and password.', 'danger')
    return render_template('login.html', title='Login', form=form, current_user=current_user)

@main.route('/logout')
@login_required # Ensures only logged-in users can access this
def logout():
    """Logs out the current user."""
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('main.index'))

@main.route('/account')
@login_required
def account():
    """Renders the account overview page for the logged-in user."""
    return render_template('account.html', title='Account Overview', current_user=current_user)

# --- Presentation Management Routes ---
@main.route('/dashboard')
@login_required
def dashboard():
    """Renders the user's dashboard, showing their presentations."""
    # Handle flash messages from Stripe redirects
    if request.args.get('success'):
        flash('Subscription successful or updated!', 'success')
    if request.args.get('canceled'):
        flash('Subscription checkout canceled.', 'info')
    if request.args.get('upgrade_success'):
        flash('Subscription successfully updated!', 'success')
    if request.args.get('upgrade_failed'):
        flash('Subscription update failed. Please try again or contact support.', 'danger')
    if request.args.get('plan_change_canceled'):
        flash('Plan change canceled.', 'info')

    # Query presentations and the image_url of their first slide
    results = db.session.query(
            Presentation,
            Slide.image_url # Get image_url from the Slide table
        ).outerjoin(
            Slide,
            # Join condition: presentation ID matches AND it's the first slide
            and_(Presentation.id == Slide.presentation_id, Slide.slide_number == 1)
        ).filter(
            Presentation.user_id == current_user.id # Only for the current user
        ).order_by(
            Presentation.last_edited_at.desc() # Show most recently edited first
        ).all()

    return render_template(
        'dashboard.html',
        title='Dashboard',
        current_user=current_user,
        presentation_results=results, # Pass the combined results
        PresentationStatus=PresentationStatus # Pass the enum for status checking in template
    )

@main.route('/create', methods=['GET', 'POST'])
@login_required
def create_presentation():
    """Handles the creation of new presentations."""
    form = CreatePresentationForm()
    required_credits = 0 # Initialize

    # Pre-calculate required credits on POST to check before form validation
    if request.method == 'POST':
        try:
            # Use a default if 'slide_count' is missing, ensure it's an int
            desired_slide_count_str = request.form.get('slide_count')
            desired_slide_count = int(desired_slide_count_str) if desired_slide_count_str and desired_slide_count_str.isdigit() else current_app.config.get('DEFAULT_SLIDE_COUNT', 3)
            
            required_credits = desired_slide_count * current_app.config.get('CREDITS_PER_SLIDE', 25)
        except (ValueError, TypeError) as e:
             current_app.logger.error(f"Error parsing slide_count: {e}. Form data: {request.form}", exc_info=True)
             flash('Invalid number of slides submitted. Please enter a whole number.', 'warning')
             return render_template('create_presentation.html', title='Create Presentation', form=form, current_user=current_user)
        
        if current_user.credits_remaining < required_credits:
            flash(f'Insufficient credits ({current_user.credits_remaining}) to generate {desired_slide_count} slides ({required_credits} needed). Please purchase more credits or upgrade your plan.', 'warning')
            return redirect(url_for('main.pricing')) # Redirect to pricing if not enough credits

    if form.validate_on_submit():
        # Form is valid, proceed with generation logic
        topic = form.topic.data
        presenter_name = form.presenter_name.data
        input_method = request.form.get('input_method_choice', 'auto') # Get from radio buttons
        
        if input_method == 'manual':
            text_style = request.form.get('manual_text_style', 'bullet')
        else: 
            text_style = form.text_style.data
        
        if not text_style: 
            text_style = 'bullet'
            current_app.logger.warning("Text style was not set, defaulted to 'bullet'.")

        desired_slide_count = form.slide_count.data # Use validated count
        style_choice = form.style_choice.data
        custom_style_prompt = form.custom_style_prompt.data
        font_choice = form.font_choice.data
        creativity_score = form.creativity_score.data

        required_credits = desired_slide_count * current_app.config.get('CREDITS_PER_SLIDE', 25)
        credits_deducted_for_this_request = required_credits

        style_prompt_text = custom_style_prompt if style_choice == 'custom' else get_style_description(style_choice)

        slides_content_raw = []
        presentation_title = "Untitled Presentation"
        saved_slides_orm = []
        new_presentation = None

        try:
            user = db.session.get(User, current_user.id)
            if user.credits_remaining < credits_deducted_for_this_request:
                 flash(f'Credit check failed. Needed: {credits_deducted_for_this_request}, Available: {user.credits_remaining}.', 'danger')
                 return redirect(url_for('main.pricing'))

            user.credits_remaining = max(0, user.credits_remaining - credits_deducted_for_this_request)
            db.session.add(user)
            current_app.logger.info(f"CREDIT DEDUCTION: User {user.id} charged {credits_deducted_for_this_request}. Remaining: {user.credits_remaining}")
            flash("Processing input...", 'info')

            if input_method == 'manual':
                manual_topic = request.form.get(f'manual_title_1', '').strip()
                presentation_title = manual_topic or "Manually Created Presentation"
                for i in range(1, desired_slide_count + 1):
                    title = request.form.get(f'manual_title_{i}')
                    content = request.form.get(f'manual_content_{i}', '').strip()
                    if not title:
                        flash(f"Missing title for manually entered Slide {i}.", 'warning')
                        db.session.rollback()
                        user_refund = db.session.get(User, current_user.id)
                        user_refund.credits_remaining += credits_deducted_for_this_request
                        db.session.add(user_refund); db.session.commit()
                        current_app.logger.warning(f"CREDIT REFUND: User {user.id} due to missing manual title.")
                        return render_template('create_presentation.html', title='Create', form=form, current_user=current_user)
                    if i == 1: content = f"By: {presenter_name.strip()}" if presenter_name and presenter_name.strip() else ""
                    elif not content:
                        content = generate_missing_slide_content(title.strip(), text_style, manual_topic)
                        flash(f"Generated content for slide {i} ('{title}') as it was left blank.", "info")
                    slides_content_raw.append({"slide_title": title.strip(), "slide_content": content})
                if not slides_content_raw:
                     flash("Manual input failed.", 'danger'); db.session.rollback()
                     user_refund = db.session.get(User, current_user.id)
                     user_refund.credits_remaining += credits_deducted_for_this_request
                     db.session.add(user_refund); db.session.commit()
                     current_app.logger.warning(f"CREDIT REFUND: User {user.id} due to no manual slides parsed.")
                     return render_template('create_presentation.html', title='Create', form=form, current_user=current_user)
            elif input_method == 'auto' and topic:
                presentation_title = f"Presentation on: {topic}"
                slides_content_raw = generate_text_content(topic, text_style, desired_slide_count, presenter_name)
            else:
                 flash("Invalid input method or missing topic.", 'warning'); db.session.rollback()
                 user_refund = db.session.get(User, current_user.id)
                 user_refund.credits_remaining += credits_deducted_for_this_request
                 db.session.add(user_refund); db.session.commit()
                 current_app.logger.warning(f"CREDIT REFUND: User {user.id} due to invalid input method/topic.")
                 return render_template('create_presentation.html', title='Create', form=form, current_user=current_user)

            if not slides_content_raw:
                flash("Could not generate slide content.", 'danger'); db.session.rollback()
                user_refund = db.session.get(User, current_user.id)
                user_refund.credits_remaining += credits_deducted_for_this_request
                db.session.add(user_refund); db.session.commit()
                current_app.logger.warning(f"CREDIT REFUND: User {user.id} due to content generation failure.")
                return render_template('create_presentation.html', title='Create', form=form, current_user=current_user)

            actual_content_count = len(slides_content_raw)
            if input_method == 'auto' and actual_content_count != desired_slide_count:
                 flash(f"Note: AI generated {actual_content_count} slides (requested {desired_slide_count}).", 'info')
            total_slides = len(slides_content_raw)
            new_presentation = Presentation(user_id=current_user.id, title=presentation_title, style_prompt=style_prompt_text, status=PresentationStatus.PENDING_VISUALS, font_choice=font_choice, creativity_score=creativity_score)
            db.session.add(new_presentation); db.session.flush()
            for i, slide_data in enumerate(slides_content_raw):
                raw_content = slide_data.get('slide_content', '')
                processed_content_db = json.dumps(raw_content) if isinstance(raw_content, list) else str(raw_content)
                new_slide = Slide(presentation_id=new_presentation.id, slide_number=i + 1, title=slide_data.get('slide_title', f'Slide {i+1}'), text_content=processed_content_db)
                db.session.add(new_slide); saved_slides_orm.append(new_slide)
            db.session.commit()
            current_app.logger.info(f"Committed Pres {new_presentation.id} with {total_slides} slides. User {user.id} credits updated.")
            if saved_slides_orm:
                callback_task = finalize_presentation_status_task.s(new_presentation.id, current_user.id, total_slides, credits_deducted_for_this_request)
                slide_task_signatures = []
                for i_task, slide_orm_object_task in enumerate(saved_slides_orm):
                     raw_content_for_style_check_task = slides_content_raw[i_task].get('slide_content', '')
                     image_gen_text_style_hint_task = 'paragraph'
                     if i_task == 0: image_gen_text_style_hint_task = 'paragraph'
                     elif isinstance(raw_content_for_style_check_task, list): image_gen_text_style_hint_task = 'bullet'
                     elif isinstance(raw_content_for_style_check_task, str) and '\n' in raw_content_for_style_check_task: image_gen_text_style_hint_task = 'bullet'
                     task_args_for_slide_task = {"presentation_topic": presentation_title, "presenter_name": presenter_name, "total_slides": total_slides, "text_style_for_image": image_gen_text_style_hint_task, "creativity_score": creativity_score, "font_choice": font_choice, "presentation_style_prompt": style_prompt_text}
                     slide_task_signatures.append(generate_single_slide_visual_task.s(slide_orm_object_task.id, current_user.id, **task_args_for_slide_task))
                chord_result = chord(group(slide_task_signatures))(callback_task)
                new_presentation.celery_chord_id = chord_result.id; db.session.commit()
                flash(f"Presentation '{new_presentation.title}' created! Generating {total_slides} visuals. {credits_deducted_for_this_request} credits deducted.", 'success')
            else:
                if new_presentation: new_presentation.status = PresentationStatus.VISUALS_COMPLETE; db.session.commit()
                flash("Presentation created, but no slide content found.", 'warning')
            return redirect(url_for('main.dashboard'))
        except Exception as e:
             db.session.rollback()
             current_app.logger.error(f"Create Presentation Error: {e}", exc_info=True)
             try:
                 user_refund = db.session.get(User, current_user.id)
                 if user_refund: user_refund.credits_remaining += credits_deducted_for_this_request; db.session.add(user_refund)
                 current_app.logger.info(f"CREDIT REFUND: User {user_refund.id if user_refund else 'UNKNOWN'} due to exception.")
             except Exception as refund_err: current_app.logger.error(f"CREDIT REFUND FAILED: User {current_user.id}: {refund_err}")
             if new_presentation and new_presentation.id:
                 try:
                     pres_to_fail = db.session.get(Presentation, new_presentation.id)
                     if pres_to_fail: pres_to_fail.status = PresentationStatus.GENERATION_FAILED
                 except Exception as db_err: current_app.logger.error(f"Failed to mark pres {new_presentation.id} as failed: {db_err}")
             db.session.commit()
             flash(f"Error creating presentation: {str(e)}", 'danger')
    elif request.method == 'POST' and not form.is_submitted():
        flash('Please correct the errors below.', 'warning')
    return render_template('create_presentation.html', title='Create Presentation', form=form, current_user=current_user)


@main.route('/presentation/<int:presentation_id>')
@login_required
def view_presentation(presentation_id):
    """Displays a single presentation with its slides."""
    presentation = Presentation.query.get_or_404(presentation_id)
    if presentation.user_id != current_user.id:
        abort(403) 

    if presentation.status == PresentationStatus.PENDING_VISUALS:
        flash("Visuals are still generating for this presentation.", "info")
    elif presentation.status == PresentationStatus.GENERATION_FAILED:
        flash("Visual generation failed for this presentation. You can try editing and regenerating slides.", "warning")

    slides = presentation.slides.order_by(Slide.slide_number).all()
    return render_template('view_presentation.html', title=presentation.title, presentation=presentation, slides=slides, current_user=current_user, PresentationStatus=PresentationStatus)

@main.route('/editor/<int:presentation_id>')
@login_required
def editor(presentation_id):
    """Renders the slide editor page for a given presentation."""
    presentation = Presentation.query.get_or_404(presentation_id)
    if presentation.user_id != current_user.id:
        abort(403)

    credits_needed_for_regen = current_app.config.get('CREDITS_PER_REGENERATE', 25)
    can_regenerate = current_user.credits_remaining >= credits_needed_for_regen
    if not can_regenerate:
        flash(f'Insufficient credits ({current_user.credits_remaining}) for visual regeneration ({credits_needed_for_regen} needed). Please top up.', 'warning')

    if presentation.status == PresentationStatus.PENDING_VISUALS:
        flash("Visuals are still generating for some slides. Editing is enabled, but regeneration might be delayed for those slides.", "info")
    elif presentation.status == PresentationStatus.GENERATION_FAILED:
        flash("Initial visual generation failed for some slides. You can edit text and try regenerating visuals.", "warning")

    slides = presentation.slides.order_by(Slide.slide_number).all()
    slides_data = []
    for s in slides:
        content = s.text_content or ""
        try:
            parsed_content = json.loads(content) if content.strip().startswith(('[', '{')) else content
        except json.JSONDecodeError:
            parsed_content = content 
        
        slides_data.append({
            "id": s.id,
            "slide_number": s.slide_number,
            "title": s.title or "",
            "text_content": parsed_content,
            "image_url": url_for('static', filename=s.image_url) if s.image_url else None
        })
    return render_template('editor.html',
                           title=f"Editing: {presentation.title}",
                           presentation=presentation,
                           slides_json=json.dumps(slides_data),
                           current_user=current_user,
                           PresentationStatus=PresentationStatus,
                           can_regenerate=can_regenerate)


@main.route('/api/slide/<int:slide_id>/regenerate_image', methods=['POST'])
@login_required
def regenerate_slide_image_api(slide_id):
    """API endpoint to regenerate an image for a specific slide and save text changes."""
    slide = db.session.get(Slide, slide_id)
    if not slide:
        return jsonify({'status': 'error', 'message': 'Slide not found'}), 404
    
    presentation = db.session.get(Presentation, slide.presentation_id)
    if not presentation or presentation.user_id != current_user.id:
        return jsonify({'status': 'error', 'message': 'Permission denied'}), 403

    credits_needed = current_app.config.get('CREDITS_PER_REGENERATE', 25)
    user = db.session.get(User, current_user.id)

    if user.credits_remaining < credits_needed:
        return jsonify({'status': 'error', 'message': f'Insufficient credits ({user.credits_remaining}) for regeneration ({credits_needed} needed).'}), 402

    data = request.get_json()
    if not data or 'title' not in data or 'text_content' not in data:
        return jsonify({'status': 'error', 'message': 'Missing title or text_content in request'}), 400

    edit_prompt_text = data.get('edit_prompt', '').strip()

    try:
        user.credits_remaining = max(0, user.credits_remaining - credits_needed)
        db.session.add(user)
        current_app.logger.info(f"CREDIT DEDUCTION: User {user.id} charged {credits_needed} for regen (Slide {slide_id}). Remaining: {user.credits_remaining}")

        slide.title = data['title']
        new_content = data['text_content']
        try:
            original_parsed_content = json.loads(slide.text_content) if slide.text_content and slide.text_content.strip().startswith('[') else slide.text_content
            if isinstance(original_parsed_content, list):
                slide.text_content = json.dumps([line.strip() for line in new_content.split('\n') if line.strip()] or [" "])
            else:
                slide.text_content = str(new_content)
        except (json.JSONDecodeError, TypeError):
            slide.text_content = str(new_content)

        presentation.last_edited_at = datetime.now(timezone.utc)
        db.session.add(slide); db.session.add(presentation)
        current_app.logger.info(f"API: Regen for slide {slide.id} by user {user.id}.")
        presenter_name = presentation.author.name if presentation.author else None
        total_slides = db.session.query(func.count(Slide.id)).filter(Slide.presentation_id == presentation.id).scalar() or 1
        try:
            content_for_style_check = json.loads(slide.text_content) if (slide.text_content and (slide.text_content.strip().startswith('[') or slide.text_content.strip().startswith('{'))) else slide.text_content
            text_style_for_image = 'bullet' if isinstance(content_for_style_check, list) or (slide.slide_number != 1 and '\n' in str(content_for_style_check)) else 'paragraph'
            if slide.slide_number == 1: text_style_for_image = 'paragraph'
        except (json.JSONDecodeError, TypeError):
            text_style_for_image = 'paragraph'
        creativity_score = getattr(presentation, 'creativity_score', 5)
        font_choice = getattr(presentation, 'font_choice', 'Inter')
        style_description_to_use = edit_prompt_text or slide.applied_style_info or presentation.style_prompt or get_style_description('keynote_modern')
        
        if edit_prompt_text: current_app.logger.info(f"API: Using NEW edit prompt for regen: '{edit_prompt_text}'")
        elif slide.applied_style_info: current_app.logger.info(f"API: Using PREVIOUS slide style for regen: '{slide.applied_style_info}'")
        else: current_app.logger.info(f"API: Using PRESENTATION style for regen: '{presentation.style_prompt}'")

        image_gen_prompt_text = build_image_prompt(slide_title=slide.title, slide_content=json.loads(slide.text_content) if text_style_for_image == 'bullet' and isinstance(content_for_style_check, list) else slide.text_content, style_description=style_description_to_use, text_style=text_style_for_image, slide_number=slide.slide_number, total_slides=total_slides, creativity_score=creativity_score, presentation_topic=presentation.title, font_choice=font_choice, presenter_name=presenter_name)
        relative_image_path, actual_prompt_used = generate_slide_image(image_prompt=image_gen_prompt_text, presentation_id=presentation.id, slide_number=slide.slide_number)

        if relative_image_path:
            slide.image_url = relative_image_path; slide.image_gen_prompt = actual_prompt_used; slide.applied_style_info = style_description_to_use
            db.session.commit()
            new_image_static_url = url_for('static', filename=relative_image_path)
            current_app.logger.info(f"API: Regen success for slide {slide.id}. User {user.id} credits: {user.credits_remaining}")
            return jsonify({'status': 'success', 'message': 'Slide visual regenerated!', 'new_image_url': new_image_static_url, 'credits_remaining': user.credits_remaining})
        else:
            db.session.rollback()
            user_after_rollback = db.session.get(User, current_user.id)
            credits_after_rollback = user_after_rollback.credits_remaining if user_after_rollback else "unknown"
            current_app.logger.error(f"API: Image gen failed for slide {slide.id}. Credits unchanged ({credits_after_rollback}). Text changes rolled back.")
            return jsonify({'status': 'error', 'message': 'Image regeneration failed. Text changes not saved. Credits not deducted.'}), 500
    except Exception as e:
        db.session.rollback()
        user_after_rollback = db.session.get(User, current_user.id)
        credits_after_rollback = user_after_rollback.credits_remaining if user_after_rollback else "unknown"
        current_app.logger.error(f"API: Regen Error for slide {slide.id}: {e}", exc_info=True)
        current_app.logger.info(f"CREDIT STATUS: User {user.id} credits unchanged ({credits_after_rollback}) due to regen exception.")
        return jsonify({'status': 'error', 'message': f'Error: {str(e)}'}), 500

@main.route('/presentation/<int:presentation_id>/delete', methods=['POST'])
@login_required
def delete_presentation(presentation_id):
    """Deletes a presentation and its associated files."""
    presentation = db.session.get(Presentation, presentation_id)
    if not presentation or presentation.user_id != current_user.id:
        abort(403)
    try:
        presentation_folder_path = os.path.join(current_app.static_folder, 'uploads', str(presentation.id))
        if os.path.isdir(presentation_folder_path):
            shutil.rmtree(presentation_folder_path)
            current_app.logger.info(f"Deleted folder: {presentation_folder_path}")

        db.session.delete(presentation)
        db.session.commit()
        flash(f'Presentation "{presentation.title}" and its files have been deleted.', 'success')
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error deleting presentation {presentation_id}: {e}", exc_info=True)
        flash('Error deleting presentation. Please try again.', 'danger')
    return redirect(url_for('main.dashboard'))

@main.route('/presentation/<int:presentation_id>/export/pptx')
@login_required
def export_pptx(presentation_id):
    """Exports a presentation to a PPTX file."""
    if not PPTX_INSTALLED:
        flash("PPTX export functionality is currently unavailable.", "warning")
        return redirect(url_for('main.dashboard'))

    presentation = db.session.get(Presentation, presentation_id)
    if not presentation or presentation.user_id != current_user.id:
        abort(403)

    if presentation.status != PresentationStatus.VISUALS_COMPLETE:
        flash("Presentation visuals are not yet complete. Please wait or check status.", 'warning')
        return redirect(url_for('main.view_presentation', presentation_id=presentation_id))

    slides = presentation.slides.order_by(Slide.slide_number).all()
    if not slides:
        flash("No slides found in this presentation to export.", 'warning')
        return redirect(url_for('main.view_presentation', presentation_id=presentation_id))

    try:
        prs = PptxPresentation()
        prs.slide_width = Inches(13.333) 
        prs.slide_height = Inches(7.5)
        blank_slide_layout = prs.slide_layouts[6] 

        for slide_data in slides:
            ppt_slide = prs.slides.add_slide(blank_slide_layout)
            if slide_data.image_url:
                image_path_full = os.path.join(current_app.static_folder, slide_data.image_url)
                if os.path.exists(image_path_full):
                    try:
                        ppt_slide.shapes.add_picture(image_path_full, Inches(0), Inches(0), width=prs.slide_width, height=prs.slide_height)
                    except Exception as img_err:
                        current_app.logger.warning(f"PPTX Export: Error adding image {slide_data.image_url} for slide {slide_data.slide_number}: {img_err}")
                        txBox = ppt_slide.shapes.add_textbox(Inches(1), Inches(1), Inches(11.333), Inches(1))
                        tf = txBox.text_frame
                        p_graph = tf.add_paragraph()
                        p_graph.text = f"[Image for slide {slide_data.slide_number} could not be added]"
                        p_graph.font.size = Pt(18)
                else:
                    current_app.logger.warning(f"PPTX Export: Image file not found at {image_path_full} for slide {slide_data.slide_number}")
        
        file_stream = io.BytesIO()
        prs.save(file_stream)
        file_stream.seek(0)
        safe_title = "".join(c if c.isalnum() or c in (' ', '-') else '_' for c in presentation.title)
        filename = f"{safe_title}.pptx"
        return send_file(file_stream, as_attachment=True, download_name=filename, mimetype='application/vnd.openxmlformats-officedocument.presentationml.presentation')
    except Exception as e:
        current_app.logger.error(f"Error exporting PPTX for presentation {presentation_id}: {e}", exc_info=True)
        flash('An error occurred while exporting the presentation to PPTX.', 'danger')
        return redirect(url_for('main.view_presentation', presentation_id=presentation_id))


@main.route('/api/presentation/<int:presentation_id>/status')
@login_required
def presentation_status_api(presentation_id):
    """API endpoint to get the status of a presentation and slide generation progress."""
    presentation = db.session.get(Presentation, presentation_id)
    if not presentation or presentation.user_id != current_user.id:
        return jsonify({'status': 'error', 'message': 'Presentation not found or unauthorized'}), 404

    response_data = {'status': presentation.status.value}
    if presentation.status == PresentationStatus.PENDING_VISUALS:
        try:
            total_slides = db.session.query(func.count(Slide.id)).filter_by(presentation_id=presentation_id).scalar() or 0
            completed_slides = db.session.query(func.count(Slide.id)).filter(
                Slide.presentation_id == presentation_id,
                Slide.image_url.isnot(None)
            ).scalar() or 0
            response_data['total_slides'] = total_slides
            response_data['completed_slides'] = completed_slides
        except Exception as e:
            current_app.logger.error(f"Error querying slide progress for Presentation {presentation_id}: {e}", exc_info=True)
            response_data['total_slides'] = None
            response_data['completed_slides'] = None
            
    return jsonify(response_data)

@main.route('/presentation/<int:presentation_id>/cancel', methods=['POST'])
@login_required
def cancel_presentation(presentation_id):
    """Cancels an in-progress visual generation for a presentation."""
    presentation = db.session.get(Presentation, presentation_id)
    if not presentation or presentation.user_id != current_user.id:
        abort(403)

    if presentation.status != PresentationStatus.PENDING_VISUALS:
        flash("This presentation is not currently generating visuals.", "warning")
        return redirect(url_for('main.dashboard'))

    if not presentation.celery_chord_id:
        flash("No active generation task found to cancel for this presentation.", "warning")
        presentation.status = PresentationStatus.GENERATION_FAILED
        db.session.commit()
        return redirect(url_for('main.dashboard'))

    try:
        current_app.logger.info(f"Attempting to cancel Celery chord: {presentation.celery_chord_id} for Presentation {presentation.id}")
        chord_res = AsyncResult(presentation.celery_chord_id, app=celery_app.celery)
        if chord_res and chord_res.parent:
            chord_res.parent.revoke(terminate=True, signal='SIGTERM')
            current_app.logger.info(f"Revoked parent group task {chord_res.parent.id} for Presentation {presentation.id}")
        if chord_res:
            chord_res.revoke(terminate=True, signal='SIGTERM')
            current_app.logger.info(f"Revoked callback task {chord_res.id} for Presentation {presentation.id}")

        presentation.status = PresentationStatus.GENERATION_FAILED
        presentation.last_edited_at = datetime.now(timezone.utc)
        
        num_slides_intended = presentation.slides.count()
        credits_to_refund = num_slides_intended * current_app.config.get('CREDITS_PER_SLIDE', 25)
        
        user = db.session.get(User, current_user.id)
        if user and credits_to_refund > 0:
            user.credits_remaining += credits_to_refund
            db.session.add(user)
            current_app.logger.info(f"CREDIT REFUND: User {user.id} refunded {credits_to_refund} credits for cancelled Presentation {presentation.id}.")
        
        db.session.commit()
        flash(f'Visual generation for "{presentation.title}" has been cancelled. Credits have been refunded.', 'info')
    except Exception as e:
        current_app.logger.error(f"Error cancelling Celery chord {presentation.celery_chord_id} for Presentation {presentation.id}: {e}", exc_info=True)
        flash('An error occurred while trying to cancel the generation.', 'danger')
        if presentation.status == PresentationStatus.PENDING_VISUALS:
            presentation.status = PresentationStatus.GENERATION_FAILED
            db.session.commit()
            
    return redirect(url_for('main.dashboard'))


# --- Subscription Management Routes ---
@main.route('/pricing')
def pricing():
    """Displays the pricing page with subscription options."""
    return render_template('pricing.html', title='Pricing Plans', current_user=current_user, config=current_app.config)

@main.route('/confirm-plan-change')
@login_required
def confirm_plan_change():
    """Page to confirm a plan change before modifying the subscription."""
    new_price_id = request.args.get('new_price_id')
    current_plan_name_display = current_app.config['PLAN_NAME_MAP'].get(current_user.stripe_price_id, current_user.subscription_plan_name or 'your current plan')
    new_plan_name_display = current_app.config['PLAN_NAME_MAP'].get(new_price_id, 'the selected plan')

    if not new_price_id:
        flash('No plan selected for change.', 'warning')
        return redirect(url_for('main.pricing'))

    session['plan_change_new_price_id'] = new_price_id
    
    return render_template('confirm_plan_change.html',
                           title='Confirm Plan Change',
                           current_plan_name=current_plan_name_display,
                           new_plan_name=new_plan_name_display,
                           current_user=current_user)

@main.route('/process-plan-change', methods=['POST'])
@login_required
# @csrf.exempt # Exemption handled in __init__.py
def process_plan_change():
    """Processes a confirmed plan change by modifying the Stripe subscription."""
    new_price_id = session.pop('plan_change_new_price_id', None)
    if not new_price_id:
        flash('Plan change information missing or expired. Please try again.', 'danger')
        return redirect(url_for('main.pricing'))

    if not current_user.stripe_subscription_id or current_user.subscription_status != 'active':
        flash('No active subscription found to update, or subscription is not active.', 'warning')
        return redirect(url_for('main.pricing'))

    current_app.logger.info(f"User {current_user.id} CONFIRMED plan change from {current_user.stripe_price_id} to {new_price_id} using Subscription.modify.")
    stripe.api_key = current_app.config['STRIPE_SECRET_KEY']
    try:
        subscription = stripe.Subscription.retrieve(current_user.stripe_subscription_id)
        current_app.logger.info(f"Retrieved active subscription {subscription.id} for user {current_user.id}. Current item ID: {subscription['items']['data'][0]['id']}")

        current_plan_credits = current_app.config['CREDITS_PER_PLAN'].get(current_user.subscription_plan_name, 0)
        new_plan_name_temp = current_app.config['PLAN_NAME_MAP'].get(new_price_id, 'unknown')
        new_plan_credits = current_app.config['CREDITS_PER_PLAN'].get(new_plan_name_temp, 0)

        proration_behavior_to_use = 'create_prorations' 
        if new_plan_credits < current_plan_credits: 
            proration_behavior_to_use = 'none' 
            current_app.logger.info(f"Downgrade detected for user {current_user.id}. Proration behavior set to 'none'.")
        else:
            current_app.logger.info(f"Upgrade or lateral move detected for user {current_user.id}. Proration behavior set to 'create_prorations'.")

        updated_subscription = stripe.Subscription.modify(
            current_user.stripe_subscription_id,
            items=[{
                'id': subscription['items']['data'][0]['id'],
                'price': new_price_id,
            }],
            proration_behavior=proration_behavior_to_use,
            cancel_at_period_end=False, 
        )
        current_app.logger.info(f"Successfully MODIFIED subscription {updated_subscription.id} for user {current_user.id} to new price {new_price_id} with proration: {proration_behavior_to_use}.")
        flash('Your subscription plan is being updated! Changes will reflect shortly.', 'info')
        return redirect(url_for('main.dashboard', upgrade_success=True))

    except stripe.error.StripeError as e:
        current_app.logger.error(f"Stripe API error modifying subscription for user {current_user.id}: {e}", exc_info=True)
        flash(f"Could not update your subscription: {str(e)}", 'danger')
        return redirect(url_for('main.pricing', upgrade_failed=True))
    except (KeyError, IndexError) as e: 
        current_app.logger.error(f"Error accessing subscription item data for user {current_user.id} during modify: {e}", exc_info=True)
        flash("Could not update your subscription due to an internal data error.", 'danger')
        return redirect(url_for('main.pricing', upgrade_failed=True))
    except Exception as e:
        current_app.logger.error(f"Unexpected error modifying subscription for user {current_user.id}: {e}", exc_info=True)
        flash("An unexpected error occurred while updating your subscription.", 'danger')
        return redirect(url_for('main.pricing', upgrade_failed=True))


@main.route('/create-checkout-session', methods=['POST'])
@login_required
def create_checkout_session():
    """Creates a Stripe Checkout session for a new subscription or plan change."""
    price_id = request.form.get('price_id')
    if not price_id:
        flash('Invalid plan selected.', 'danger')
        return redirect(url_for('main.pricing'))

    if not current_app.config.get('STRIPE_SECRET_KEY'):
        flash('Stripe payments are not configured on the server.', 'danger')
        current_app.logger.error("STRIPE_SECRET_KEY not found in config for checkout.")
        return redirect(url_for('main.pricing'))

    stripe.api_key = current_app.config['STRIPE_SECRET_KEY']

    if not current_user.stripe_customer_id:
        try:
            customer = stripe.Customer.create(
                email=current_user.email, name=current_user.name,
                metadata={'user_id': current_user.id} )
            current_user.stripe_customer_id = customer.id
            db.session.add(current_user); db.session.commit()
            current_app.logger.info(f"Created Stripe Customer {customer.id} for User {current_user.id}")
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Error creating/saving Stripe customer for user {current_user.id}: {e}", exc_info=True)
            flash("An error occurred setting up your billing account.", 'danger')
            return redirect(url_for('main.pricing'))

    if current_user.stripe_subscription_id and \
       current_user.subscription_status == 'active' and \
       price_id != current_user.stripe_price_id:
        current_plan_name_display = current_app.config['PLAN_NAME_MAP'].get(current_user.stripe_price_id, current_user.subscription_plan_name or 'Your Current Plan')
        new_plan_name_display = current_app.config['PLAN_NAME_MAP'].get(price_id, 'The Selected Plan')
        current_app.logger.info(f"User {current_user.id} attempting to change plan from {current_user.stripe_price_id} ({current_plan_name_display}) to {price_id} ({new_plan_name_display}). Redirecting to confirmation.")
        return redirect(url_for('main.confirm_plan_change',
                                new_price_id=price_id,
                                current_plan_name=current_plan_name_display,
                                new_plan_name=new_plan_name_display))
    else:
        checkout_params = {
            'customer': current_user.stripe_customer_id,
            'payment_method_types': ['card'],
            'line_items': [{'price': price_id, 'quantity': 1}],
            'mode': 'subscription',
            'success_url': url_for('main.dashboard', _external=True) + '?success=true&session_id={CHECKOUT_SESSION_ID}',
            'cancel_url': url_for('main.pricing', _external=True) + '?canceled=true',
            'metadata': {'user_id': current_user.id}
        }
        if current_user.stripe_subscription_id and price_id == current_user.stripe_price_id:
            current_app.logger.info(f"User {current_user.id} is re-selecting current plan {price_id}. Proceeding to Stripe Checkout.")
        else:
            current_app.logger.info(f"User {current_user.id} is creating a NEW subscription with price {price_id} via Checkout.")
        try:
            checkout_session = stripe.checkout.Session.create(**checkout_params)
            return redirect(checkout_session.url, code=303)
        except stripe.error.StripeError as e:
            current_app.logger.error(f"Stripe error creating checkout session for user {current_user.id}, params: {checkout_params}: {e}", exc_info=True)
            flash(f"Could not initiate subscription checkout: {str(e)}", 'danger')
            return redirect(url_for('main.pricing'))
        except Exception as e:
            current_app.logger.error(f"Error creating checkout session: {e}", exc_info=True)
            flash("An unexpected error occurred during checkout setup.", 'danger')
            return redirect(url_for('main.pricing'))


@main.route('/create-portal-session', methods=['POST'])
@login_required
def create_portal_session():
    """Creates a Stripe Billing Portal session for the current user."""
    if not current_app.config.get('STRIPE_SECRET_KEY'):
        flash('Payments are not configured on the server.', 'danger')
        current_app.logger.error("STRIPE_SECRET_KEY not found in config for portal session.")
        return redirect(url_for('main.account'))

    if not current_user.stripe_customer_id:
        flash('Billing portal unavailable. No billing information found for your account.', 'warning')
        current_app.logger.warning(f"User {current_user.id} attempted to access portal without stripe_customer_id.")
        return redirect(url_for('main.account'))

    stripe.api_key = current_app.config['STRIPE_SECRET_KEY']
    try:
        portal_session = stripe.billing_portal.Session.create(
            customer=current_user.stripe_customer_id,
            return_url=url_for('main.account', _external=True), # URL user returns to after portal
        )
        current_app.logger.info(f"Created Stripe Portal Session for User {current_user.id}, Customer {current_user.stripe_customer_id}. Redirecting.")
        return redirect(portal_session.url, code=303)
    except stripe.error.StripeError as e:
        current_app.logger.error(f"Stripe error creating portal session for user {current_user.id}: {e}", exc_info=True)
        flash(f"Could not open billing portal: {str(e)}", 'danger')
        return redirect(url_for('main.account'))
    except Exception as e:
        current_app.logger.error(f"Error creating portal session for user {current_user.id}: {e}", exc_info=True)
        flash("An unexpected error occurred. Please try again.", 'danger')
        return redirect(url_for('main.account'))


@main.route('/stripe-webhook', methods=['POST'], strict_slashes=False)
# @csrf.exempt # Exemption handled in __init__.py
def stripe_webhook():
    """
    Handles incoming webhook events from Stripe to keep subscription
    and payment data in sync with the application's database.
    CSRF exemption is applied in __init__.py.
    """
    print("--- !!! STRIPE WEBHOOK ROUTE FUNCTION ENTERED !!! ---", flush=True)
    current_app.logger.info("--- WEBHOOK RECEIVED ---")
    payload = request.data
    sig_header = request.headers.get('Stripe-Signature')
    endpoint_secret = current_app.config.get('STRIPE_ENDPOINT_SECRET')
    event = None

    current_app.logger.info(f"Using Endpoint Secret: {'********' + endpoint_secret[-4:] if endpoint_secret else 'Not Set!'}")
    if not endpoint_secret:
        current_app.logger.error("Webhook Error: Endpoint secret not configured.")
        return jsonify(error="Webhook secret not configured"), 500
    try:
        current_app.logger.info(f"Attempting stripe.Webhook.construct_event with payload (first 100 chars): {payload[:100]}")
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
        current_app.logger.info(f"✅ Webhook Event Verified: ID={event.id}, Type={event['type']}")
    except ValueError as e:
        current_app.logger.error(f"Webhook ValueError: {e}", exc_info=True)
        return jsonify(error=str(e)), 400
    except stripe.error.SignatureVerificationError as e:
        current_app.logger.error(f"Webhook SignatureVerificationError: {e}", exc_info=True)
        return jsonify(error=str(e)), 400
    except Exception as e:
        current_app.logger.error(f"Webhook Generic Error during construction: {e}", exc_info=True)
        return jsonify(error=str(e)), 500

    session_data = event['data']['object']
    event_type = event['type']
    user = None
    customer_id = session_data.get('customer')
    metadata = session_data.get('metadata', {})
    user_id_metadata = metadata.get('user_id')
    current_app.logger.info(f"Webhook {event_type}: Extracted customer_id='{customer_id}', user_id_metadata='{user_id_metadata}'")

    if customer_id:
        user = User.query.filter_by(stripe_customer_id=customer_id).first()
        log_prefix = f"Webhook {event_type} (User via Customer {customer_id})"
        if user: current_app.logger.info(f"{log_prefix}: Found user by customer_id: User ID {user.id}")
        else: current_app.logger.warning(f"{log_prefix}: No matching user for customer_id '{customer_id}'.")
    if not user and user_id_metadata:
        log_prefix = f"Webhook {event_type} (User via Metadata {user_id_metadata})"
        try:
            user = db.session.get(User, int(user_id_metadata))
            if user: current_app.logger.info(f"{log_prefix}: Found user by metadata: User ID {user.id}")
            else: current_app.logger.warning(f"{log_prefix}: No matching user for metadata_id '{user_id_metadata}'.")
        except (ValueError, TypeError) as e: current_app.logger.warning(f"{log_prefix}: Invalid metadata_id '{user_id_metadata}': {e}")
    if not user:
        current_app.logger.warning(f"Webhook {event_type}: FINAL User lookup failed. Skipping.")
        return jsonify(success=True, message="User not found, skipping.")

    log_prefix = f"Webhook {event_type} (User ID {user.id})"
    current_app.logger.info(f"{log_prefix}: Processing event...")
    try:
        if event_type == 'checkout.session.completed':
            current_app.logger.info(f"{log_prefix}: Handling checkout.session.completed...")
            if session_data.get('payment_status') == 'paid' and session_data.get('mode') == 'subscription':
                subscription_id = session_data.get('subscription') 
                if not subscription_id:
                     current_app.logger.warning(f"{log_prefix}: No subscription ID in checkout.session.completed."); return jsonify(success=True)
                
                if user.stripe_subscription_id != subscription_id: 
                    try:
                        subscription = stripe.Subscription.retrieve(subscription_id)
                        items = subscription['items']['data']
                        price_id = items[0]['price']['id'] if items else None
                        current_period_end_ts = subscription.get('current_period_end')
                        current_period_end_dt = datetime.fromtimestamp(current_period_end_ts, tz=timezone.utc) if current_period_end_ts else None
                    except Exception as e:
                        current_app.logger.error(f"{log_prefix}: Error retrieving/processing subscription {subscription_id}: {e}", exc_info=True)
                        return jsonify(error=f"Error processing subscription details: {e}"), 500

                    plan_name = current_app.config['PLAN_NAME_MAP'].get(price_id, 'unknown')
                    credits_to_grant = current_app.config['CREDITS_PER_PLAN'].get(plan_name, 0)
                    
                    current_app.logger.info(f"{log_prefix}: Mapped Plan: {plan_name}, Credits for this plan: {credits_to_grant}")

                    user.credits_remaining = credits_to_grant 
                    current_app.logger.info(f"{log_prefix}: New subscription via Checkout. Credits SET to {credits_to_grant}.")

                    user.stripe_customer_id = customer_id
                    user.stripe_subscription_id = subscription_id 
                    user.stripe_price_id = price_id
                    user.subscription_plan_name = plan_name
                    user.subscription_status = subscription.status 
                    user.subscription_current_period_end = current_period_end_dt
                    
                    db.session.add(user)
                    current_app.logger.info(f"{log_prefix}: PRE-COMMIT User credits: {user.credits_remaining}, plan: {user.subscription_plan_name}, sub_id: {user.stripe_subscription_id}")
                    db.session.commit()
                    current_app.logger.info(f"{log_prefix}: COMMIT successful. Credits SET for new subscription.")
                else:
                    current_app.logger.info(f"{log_prefix}: checkout.session.completed for an already known active subscription ({subscription_id}). Credit logic in customer.subscription.updated.")
        
        elif event_type == 'customer.subscription.updated':
            current_app.logger.info(f"{log_prefix}: Handling customer.subscription.updated...")
            subscription_event_data = session_data 
            
            new_status = subscription_event_data.get('status')
            sub_items = subscription_event_data.get('items', {}).get('data', [])
            new_price_id = sub_items[0].get('price', {}).get('id') if sub_items and sub_items[0].get('price') else None
            current_period_end_ts = subscription_event_data.get('current_period_end')
            new_period_end_dt = datetime.fromtimestamp(current_period_end_ts, tz=timezone.utc) if current_period_end_ts else None
            updated_subscription_id = subscription_event_data.get('id')

            old_user_stripe_price_id = user.stripe_price_id 
            old_user_credits = user.credits_remaining if user.credits_remaining is not None else 0
            
            user.stripe_subscription_id = updated_subscription_id 
            user.subscription_status = new_status
            user.subscription_current_period_end = new_period_end_dt
            user.stripe_price_id = new_price_id 
            new_plan_name = current_app.config['PLAN_NAME_MAP'].get(new_price_id, 'unknown')
            user.subscription_plan_name = new_plan_name
            
            plan_actually_changed = new_price_id and (new_price_id != old_user_stripe_price_id)
            
            current_app.logger.info(f"{log_prefix}: Sub updated. Event Sub ID: {updated_subscription_id}. New Status: {new_status}, New Price ID: {new_price_id}. Plan Changed This Event: {plan_actually_changed}")
            
            if plan_actually_changed: 
                if new_status in ['active', 'trialing']:
                    credits_for_new_plan = current_app.config['CREDITS_PER_PLAN'].get(new_plan_name, 0)
                    user.credits_remaining = old_user_credits + credits_for_new_plan # Stack credits
                    current_app.logger.info(f"{log_prefix}: Plan changed to '{new_plan_name}'. Added {credits_for_new_plan} credits. Old: {old_user_credits}, New Total: {user.credits_remaining}.")
                else: 
                    user.credits_remaining = 0 
                    current_app.logger.info(f"{log_prefix}: Plan changed to {new_plan_name}, but status is {new_status}. Credits set to 0.")
            
            elif new_status not in ['active', 'trialing'] and user.subscription_status != new_status : 
                current_app.logger.info(f"{log_prefix}: Subscription status changed to {new_status} (plan unchanged). Setting credits to 0 and plan to 'free'.")
                user.credits_remaining = 0
                user.subscription_plan_name = 'free'
                user.stripe_price_id = None
            
            db.session.add(user)
            current_app.logger.info(f"{log_prefix}: PRE-COMMIT User credits: {user.credits_remaining}, plan: {user.subscription_plan_name}, status: {user.subscription_status}")
            db.session.commit()
            current_app.logger.info(f"{log_prefix}: COMMIT successful for subscription update.")

        elif event_type == 'customer.subscription.deleted':
            current_app.logger.info(f"{log_prefix}: Handling customer.subscription.deleted...")
            if user.stripe_subscription_id == session_data.get('id'): 
                user.stripe_subscription_id = None; user.stripe_price_id = None; user.subscription_plan_name = 'free'
                user.subscription_status = 'canceled'; user.subscription_current_period_end = None; user.credits_remaining = 0
                db.session.add(user); db.session.commit()
                current_app.logger.info(f"{log_prefix}: COMMIT successful for sub deletion. User set to free plan.")
            else:
                current_app.logger.info(f"{log_prefix}: Received delete for sub {session_data.get('id')}, but user's current sub is {user.stripe_subscription_id}. No change.")
        
        elif event_type == 'invoice.paid':
            current_app.logger.info(f"{log_prefix}: Handling invoice.paid...")
            invoice = session_data
            subscription_id_on_invoice = invoice.get('subscription')
            billing_reason = invoice.get('billing_reason')
            current_app.logger.info(f"{log_prefix}: Invoice billing_reason: {billing_reason}")

            if not subscription_id_on_invoice:
                current_app.logger.warning(f"{log_prefix}: Invoice missing subscription ID."); return jsonify(success=True)

            if user.stripe_subscription_id == subscription_id_on_invoice and \
               user.subscription_status == 'active' and \
               billing_reason == 'subscription_cycle': 

                invoice_lines = invoice.get('lines', {}).get('data', [])
                price_id = invoice_lines[0].get('price', {}).get('id') if invoice_lines and invoice_lines[0].get('price') else None
                plan_name = current_app.config['PLAN_NAME_MAP'].get(price_id, 'unknown')
                credits_to_grant = current_app.config['CREDITS_PER_PLAN'].get(plan_name, 0)
                
                current_app.logger.info(f"{log_prefix}: Invoice for RENEWAL. Price ID: {price_id}, Plan: {plan_name}, Credits to grant: {credits_to_grant}")

                if credits_to_grant > 0:
                    user.credits_remaining = credits_to_grant 
                    user.subscription_current_period_end = datetime.fromtimestamp(invoice_lines[0].get('period', {}).get('end'), tz=timezone.utc) if invoice_lines and invoice_lines[0].get('period') and invoice_lines[0].get('period', {}).get('end') else None
                    db.session.add(user); db.session.commit()
                    current_app.logger.info(f"{log_prefix}: COMMIT successful for invoice.paid (renewal). Granted/Reset {credits_to_grant} credits.")
                else:
                    current_app.logger.warning(f"{log_prefix}: No credits to grant for invoice.paid renewal, price_id: {price_id}")
            elif billing_reason in ['subscription_create', 'subscription_update']:
                current_app.logger.info(f"{log_prefix}: Invoice for {billing_reason}. Credits handled by other events. No credit change here based on invoice.paid.")
            else:
                current_app.logger.info(f"{log_prefix}: Skipped credit grant for invoice.paid. User sub ID: {user.stripe_subscription_id}, Invoice sub ID: {subscription_id_on_invoice}, User status: {user.subscription_status}, Billing Reason: {billing_reason}")
        else:
            current_app.logger.info(f"{log_prefix}: Unhandled event type: {event_type}")
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Webhook {event_type}: Error processing for User {user.id}: {e}", exc_info=True)
        return jsonify(error="Internal server error"), 500
    current_app.logger.info(f"{log_prefix}: Event processed successfully.")
    return jsonify(success=True), 200
