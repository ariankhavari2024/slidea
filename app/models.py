# app/models.py
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
# Import necessary SQLAlchemy types
from sqlalchemy import Integer, String, Text, ForeignKey, DateTime, JSON, Enum, Boolean, Float
from sqlalchemy.orm import relationship, Mapped, mapped_column # Mapped and mapped_column for modern SQLAlchemy
from datetime import datetime, timezone # For setting default timestamps with timezone
import enum # For creating Enum types

from . import db, login_manager # Import db and login_manager from the app package (__init__.py)

# Enum for Presentation Status
# This defines the possible states a presentation can be in.
class PresentationStatus(enum.Enum):
    PENDING_TEXT = 'pending_text'           # Initial state, text content generation is pending
    PENDING_VISUALS = 'pending_visuals'     # Text content generated, visuals are pending
    VISUALS_COMPLETE = 'visuals_complete'   # All visuals generated, presentation is ready
    GENERATION_FAILED = 'generation_failed' # If any part of the generation fails or is cancelled

# User Model
# Represents a user in the application. Inherits from UserMixin for Flask-Login integration.
class User(UserMixin, db.Model):
    __tablename__ = "users" # Specifies the database table name

    # Define table columns using Mapped and mapped_column for type hinting and ORM mapping.
    id: Mapped[int] = mapped_column(Integer, primary_key=True) # Primary key for the user
    email: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True) # User's email, must be unique
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False) # Hashed password
    name: Mapped[str] = mapped_column(String(100), nullable=False) # User's name
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc)) # Timestamp of account creation
    
    # Relationship to Presentations: A user can have many presentations.
    # 'lazy="dynamic"' allows querying presentations related to a user.
    # 'cascade="all, delete-orphan"' means if a user is deleted, their presentations are also deleted.
    presentations = relationship("Presentation", back_populates="author", lazy='dynamic', cascade="all, delete-orphan")

    # Stripe and Subscription Fields
    stripe_customer_id: Mapped[str] = mapped_column(String(255), nullable=True, index=True) # Stripe's unique ID for the customer
    stripe_subscription_id: Mapped[str] = mapped_column(String(255), nullable=True, index=True) # Stripe's unique ID for the subscription
    stripe_price_id: Mapped[str] = mapped_column(String(255), nullable=True) # Stripe Price ID of the active plan
    
    # User's current subscription plan name (e.g., 'free', 'pro', 'creator')
    # `server_default` ensures the database itself has a default if a direct insert happens bypassing SQLAlchemy defaults.
    subscription_plan_name: Mapped[str] = mapped_column(String(50), nullable=True, default='free', server_default='free')
    # Status of the subscription (e.g., 'active', 'trialing', 'past_due', 'canceled')
    subscription_status: Mapped[str] = mapped_column(String(50), nullable=True)
    # Timestamp for when the current subscription period ends (relevant for renewals)
    subscription_current_period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    # Number of credits the user has remaining
    credits_remaining: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default='0')

    @property
    def is_subscribed(self) -> bool:
        """Helper property to quickly check if the user has an active or trialing subscription."""
        return self.subscription_status in ['active', 'trialing']

    def set_password(self, password):
        """Hashes the provided password and stores it."""
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        """Checks if the provided password matches the stored hash."""
        return check_password_hash(self.password_hash, password)

    def __repr__(self):
        """String representation of the User object, useful for debugging."""
        return f'<User {self.name} ({self.email}) - Plan: {self.subscription_plan_name}, Credits: {self.credits_remaining}>'


# Presentation Model
# Represents a presentation created by a user.
class Presentation(db.Model):
    __tablename__ = "presentations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True) # Foreign key to link to the User
    title: Mapped[str] = mapped_column(String(250), default="Untitled Presentation") # Title of the presentation
    style_prompt: Mapped[str] = mapped_column(Text, nullable=True) # Custom style prompt if used
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_edited_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)) # Updates on modification
    
    # Status of the presentation, using the PresentationStatus Enum
    status: Mapped[PresentationStatus] = mapped_column(
        Enum( PresentationStatus, values_callable=lambda obj: [e.value for e in obj], # For non-native enum support
              native_enum=False, create_constraint=True, name="presentationstatus" ),
        default=PresentationStatus.PENDING_TEXT,
        server_default=PresentationStatus.PENDING_TEXT.value, nullable=False )
    
    # Celery Chord ID for tracking grouped visual generation tasks
    celery_chord_id: Mapped[str] = mapped_column(String(36), nullable=True, index=True)
    
    # Fields to store choices made during presentation creation
    font_choice: Mapped[str] = mapped_column(String(100), nullable=True)
    creativity_score: Mapped[int] = mapped_column(Integer, nullable=True)

    # Relationship back to the User model
    author = relationship("User", back_populates="presentations")
    # Relationship to Slides: A presentation has many slides.
    # Ordered by slide_number.
    slides = relationship("Slide", back_populates="presentation", lazy='dynamic', cascade="all, delete-orphan", order_by="Slide.slide_number")

    def __repr__(self):
        return f'<Presentation {self.id}: {self.title} (Status: {self.status.name if self.status else "None"})>'

# Slide Model
# Represents a single slide within a presentation.
class Slide(db.Model):
    __tablename__ = "slides"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    presentation_id: Mapped[int] = mapped_column(Integer, ForeignKey("presentations.id"), nullable=False, index=True) # Links to Presentation
    slide_number: Mapped[int] = mapped_column(Integer, nullable=False) # Order of the slide
    title: Mapped[str] = mapped_column(String(250), nullable=True) # Title of the slide
    text_content: Mapped[str] = mapped_column(Text, nullable=True) # Can store JSON string (for bullets) or plain text (for paragraphs)
    image_url: Mapped[str] = mapped_column(String(500), nullable=True) # Path to the generated image
    image_gen_prompt: Mapped[str] = mapped_column(Text, nullable=True) # The prompt used to generate the image
    notes: Mapped[str] = mapped_column(Text, nullable=True) # Speaker notes (future use)
    applied_style_info: Mapped[str] = mapped_column(Text, nullable=True) # Stores the style description/prompt actually used for this slide's visual

    # Relationship back to the Presentation model
    presentation = relationship("Presentation", back_populates="slides")

    def __repr__(self):
        return f'<Slide {self.slide_number} for Pres {self.presentation_id}>'

# User loader function for Flask-Login
# This function is used by Flask-Login to retrieve a user object from the database
# given their ID, which is stored in the session.
@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id)) # Use db.session.get for primary key lookups
