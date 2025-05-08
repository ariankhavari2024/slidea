# wsgi.py
# This file acts as the entry point for the Gunicorn server.
# It imports the Flask app instance created by your app factory.

import os
from app import create_app # Import the create_app function from your __init__.py

# Create the Flask app instance using the factory function
# It will automatically load the configuration based on your environment setup (e.g., Config class)
app = create_app()

# This block is typically used for running the Flask development server directly.
# Gunicorn will import the 'app' variable directly, so this part isn't strictly
# necessary for Gunicorn deployment but is standard practice for Flask apps.
if __name__ == "__main__":
    # Use Gunicorn to run the app in production, not the Flask dev server.
    # The entrypoint.sh script and render.yaml handle running Gunicorn.
    # For local testing *without* Gunicorn, you could uncomment the next line:
    # app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
    pass # Gunicorn runs the 'app' object defined above
