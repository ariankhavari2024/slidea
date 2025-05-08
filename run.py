# run.py
from app import create_app # Import only the factory
from config import Config

# Create the Flask app instance using the factory
app = create_app(Config)

# Optional: Shell context processor (keep if you use 'flask shell')
from app import db, models
@app.shell_context_processor
def make_shell_context():
    return {
        'db': db,
        'User': models.User,
        'Presentation': models.Presentation,
        'Slide': models.Slide,
        'PresentationStatus': models.PresentationStatus
     }

if __name__ == '__main__':
    # Debug mode is controlled by Config.DEBUG
    # Use host='0.0.0.0' if you need external access
    app.run(port=5001, host='127.0.0.1')

