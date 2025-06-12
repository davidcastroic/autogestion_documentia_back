from flask import Blueprint, redirect, request, session, url_for
from google_auth_oauthlib.flow import Flow
import os
import pickle
import logging

auth_bp = Blueprint('auth', __name__)
logger = logging.getLogger(__name__)

if os.getenv("FLASK_ENV") != "production":
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

# Archivo de credenciales OAuth
CLIENT_SECRETS_FILE = "client_secret.json"

# Scopes necesarios
SCOPES = [
    'https://www.googleapis.com/auth/userinfo.email',
    'https://www.googleapis.com/auth/gmail.send',
    'openid'
]


# URI de redirección
REDIRECT_URI = os.getenv("GOOGLE_OAUTH_REDIRECT", "http://localhost:8000/auth/callback")

# Ruta del token
TOKEN_DIR = os.getenv("GMAIL_TOKEN_PATH", "creds")
TOKEN_FILE = os.path.join(TOKEN_DIR, "gmail_token.pickle")

@auth_bp.route("/login")
def login():
    if os.path.exists(TOKEN_FILE):
        return redirect("/admin")

    flow = Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE,
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI
    )
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        prompt='consent'
    )
    session['state'] = state
    return redirect(authorization_url)

@auth_bp.route("/auth/callback")
def callback():
    state = session.get('state')
    if not state:
        return "⚠️ Error: No se encontró el estado de sesión. Intenta nuevamente desde /login.", 400

    try:
        flow = Flow.from_client_secrets_file(
            CLIENT_SECRETS_FILE,
            scopes=SCOPES,
            state=state,
            redirect_uri=REDIRECT_URI
        )

        flow.fetch_token(authorization_response=request.url)
        credentials = flow.credentials

        os.makedirs(TOKEN_DIR, exist_ok=True)
        with open(TOKEN_FILE, "wb") as token_file:
            pickle.dump(credentials, token_file)

        return redirect("/admin")

    except Exception as e:
        logger.error("❌ Error durante el callback de OAuth", exc_info=True)
        return "❌ Ocurrió un error inesperado durante la autenticación.", 500
