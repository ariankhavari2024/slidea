# minimal_webhook_test.py
import os
from flask import Flask, request, jsonify
from flask_wtf.csrf import CSRFProtect
import logging

# Basic Flask app setup
app = Flask(__name__)
# Use a dummy secret key for this test
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY') or 'a-dummy-secret-key-for-testing'
# IMPORTANT: Enable CSRF protection globally for the test
app.config['WTF_CSRF_ENABLED'] = True
csrf = CSRFProtect(app)

# Set up basic logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@app.route('/stripe-webhook', methods=['POST'])
@csrf.exempt # Apply exemption directly to this route
def minimal_stripe_webhook():
    # --- Log immediately upon entering the function ---
    print("--- !!! MINIMAL WEBHOOK HANDLER HIT !!! ---", flush=True)
    logger.info("--- MINIMAL WEBHOOK HANDLER HIT ---")

    # You can optionally log headers/body here if needed for deep debugging
    # logger.debug(f"Minimal Headers: {dict(request.headers)}")
    # logger.debug(f"Minimal Raw Body Size: {len(request.get_data(as_text=False))} bytes")

    # Just acknowledge receipt successfully
    return jsonify(status='success', message='Minimal handler received.'), 200

if __name__ == '__main__':
    # Run directly on port 5000 with debug mode ON
    print("--- Starting Minimal Flask App for Webhook Test ---")
    print("--- CSRF Protection is ENABLED globally ---")
    print("--- /stripe-webhook route is EXEMPTED ---")
    app.run(port=5000, debug=True)
