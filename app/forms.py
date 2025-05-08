# app/forms.py
from flask import request, flash
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SubmitField, BooleanField, RadioField, TextAreaField, IntegerField, URLField, SelectField
# Import NumberRange for the creativity score and Email validator
from wtforms.validators import DataRequired, Length, Email, EqualTo, ValidationError, Optional, NumberRange, URL

try:
    from .models import User
except ImportError:
    User = None # Allow app to run if models haven't been created yet

class RegistrationForm(FlaskForm):
    """Form for user registration."""
    name = StringField('Name',
                       validators=[DataRequired(), Length(min=2, max=100)])
    email = StringField('Email',
                        validators=[DataRequired(), Email()])
    password = PasswordField('Password',
                             validators=[DataRequired(), Length(min=6)])
    confirm_password = PasswordField('Confirm Password',
                                     validators=[DataRequired(), EqualTo('password', message='Passwords must match.')])
    submit = SubmitField('Register')

    def validate_email(self, email):
        """Check if email already exists in the database."""
        # Check if User model is available (might not be during initial setup/migrations)
        if User:
            user = User.query.filter_by(email=email.data).first()
            if user:
                raise ValidationError('That email is already taken. Please choose a different one or login.')

class LoginForm(FlaskForm):
    """Form for user login."""
    email = StringField('Email',
                        validators=[DataRequired(), Email()])
    password = PasswordField('Password', validators=[DataRequired()])
    remember = BooleanField('Remember Me')
    submit = SubmitField('Login')


class CreatePresentationForm(FlaskForm):
    """Form for creating a new presentation."""
    topic = StringField('Presentation Topic',
                        validators=[Optional(), Length(min=3, max=200)])

    presenter_name = StringField('Presenter Name (Optional)',
                                 validators=[Optional(), Length(max=100)],
                                 render_kw={"placeholder": "e.g., Your Name / Company Name"})

    text_style = RadioField('Text Content Style', # Label simplified, applies to auto-gen or manual blank content
                            choices=[('bullet', 'Bullet Points'), ('paragraph', 'Paragraphs')],
                            default='bullet',
                            validators=[DataRequired("Please select a text style.")]) # Now required as it's always used

    slide_count = IntegerField('Number of Slides',
                               validators=[DataRequired("Please enter the number of slides."), NumberRange(min=1, max=50)],
                               default=3)

    # --- Visual Style Fields (UPDATED LIST) ---
    PREDEFINED_STYLES = [
        # Kept Styles
        ('keynote_modern', 'Keynote Modern'),
        ('abstract_gradient', 'Abstract Gradient'),
        ('minimalist_sketch', 'Minimalist Sketch'),
        ('cyberpunk_glow', 'Cyberpunk Glow'),
        # New Styles
        ('corporate_charts', 'Corporate w/ Charts'),
        ('ghibli_inspired', 'Ghibli Inspired'),
        ('pencil_paper', 'Pencil & Paper'),
        ('claymorphism_3d', 'Claymorphism 3D'),
        # ('wood_texture', 'Wood Texture'), # Example if you wanted 9 + custom
        # Custom Option
        ('custom', 'Custom Prompt Below...')
    ]
    style_choice = SelectField('Visual Theme',
                               choices=PREDEFINED_STYLES,
                               default='keynote_modern', # Default to a new style
                               validators=[DataRequired()])

    custom_style_prompt = StringField('Custom Theme Prompt',
                                     validators=[Optional(), Length(max=300)],
                                     render_kw={"placeholder": "e.g., 'synthwave art style, neon grids, 80s aesthetic'"})

    # --- Font Choice ---
    FONT_CHOICES = [
        # Sans-serif
        ('Inter', 'Inter (Modern Sans)'), ('Lato', 'Lato (Friendly Sans)'),
        ('Montserrat', 'Montserrat (Geometric Sans)'), ('Open Sans', 'Open Sans (Humanist Sans)'),
        ('Poppins', 'Poppins (Geometric Sans)'), ('Roboto', 'Roboto (Neo-grotesque Sans)'),
        ('Nunito', 'Nunito (Rounded Sans)'), ('Helvetica', 'Helvetica (Classic Sans)'),
        ('Arial', 'Arial (Classic Sans)'),
        # Serif
        ('Merriweather', 'Merriweather (Readable Serif)'), ('Lora', 'Lora (Contemporary Serif)'),
        ('Playfair Display', 'Playfair Display (Elegant Serif)'), ('Roboto Slab', 'Roboto Slab (Slab Serif)'),
        ('Times New Roman', 'Times New Roman (Classic Serif)'),
        # Monospace
        ('Roboto Mono', 'Roboto Mono (Monospace)'), ('Source Code Pro', 'Source Code Pro (Monospace)'),
    ]
    font_choice = SelectField('Choose Font',
                              choices=FONT_CHOICES,
                              default='Inter',
                              validators=[DataRequired()])

    # --- Creativity Score (Rendered as Range Slider) ---
    creativity_score = IntegerField(
        'Visual Creativity Level', # Updated label for clarity with slider
        validators=[DataRequired(), NumberRange(min=1, max=10)],
        default=5,
        # *** ADD render_kw to render as range input ***
        render_kw={
            "type": "range",
            "min": "1",
            "max": "10",
            "step": "1",
            "class": "form-range w-input", # Add w-input for base styling + form-range for slider
            "x-model.number": "creativityValue" # Alpine binding
        }
    )

    submit = SubmitField('✨ Generate Presentation')

    def validate(self, extra_validators=None):
        """Perform complex validation dependent on multiple fields."""
        initial_validation = super(CreatePresentationForm, self).validate(extra_validators)
        if not initial_validation:
            return False

        input_method = request.form.get('input_method_choice', 'auto') # Get input method choice

        # If auto mode, topic is required
        if input_method == 'auto' and not self.topic.data:
             self.topic.errors.append('Presentation Topic is required for Automatic Generation.')
             return False

        # Text style is now always required (for auto-gen or manual blank)
        # The DataRequired validator on the field handles this

        # Custom style validation
        if self.style_choice.data == 'custom' and not self.custom_style_prompt.data:
             self.custom_style_prompt.errors.append('Please provide a custom theme prompt when selecting "Custom...".')
             return False

        # Manual input validation (Titles required) - Handled in route now
        # You could add checks here too if needed, accessing request.form['manual_title_X']

        return True

# *** ADDED ContactForm Definition ***
class ContactForm(FlaskForm):
    """Form for sending contact messages."""
    name = StringField('Name', validators=[DataRequired(), Length(min=2, max=100)])
    email = StringField('Email', validators=[DataRequired(), Email()])
    subject = StringField('Subject', validators=[DataRequired(), Length(min=3, max=150)])
    message = TextAreaField('Message', validators=[DataRequired(), Length(min=10, max=2000)])
    submit = SubmitField('Send Message')
# *** END ADDED ContactForm Definition ***

