# 🔹 Módulos estándar de Python
import os
env = os.getenv('FLASK_ENV', 'development')
dotenv_file = f'.env.{env}'
from dotenv import load_dotenv
load_dotenv(dotenv_file)
import json
import mimetypes
import logging
import traceback
import base64
import pickle
from datetime import datetime
from functools import wraps
from email.mime.text import MIMEText

# 🔹 Librerías externas
import requests
import mysql.connector
from google.oauth2 import service_account, credentials as google_credentials
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import Flow
from google.api_core.exceptions import InvalidArgument
from flask import Flask, render_template, request, redirect, url_for, jsonify, session

# 🔹 Google Cloud
from google.cloud import secretmanager
from google.cloud import documentai_v1 as documentai
from google.cloud import storage

# 🔹 Módulos internos
from auth import auth_bp

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'clave-secreta-default')
app.register_blueprint(auth_bp)

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]



# Configuración de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuración de la base de datos
db_config = {
    'host': os.getenv('DB_HOST'),
    'user': os.getenv('DB_USER'),
    'password': os.getenv('DB_PASSWORD'),
    'database': os.getenv('DB_NAME')
}

def get_db_connection():
    try:
        conn = mysql.connector.connect(**db_config)
        return conn
    except mysql.connector.Error as err:
        logger.error(f"Error al conectar a la base de datos: {err}")
        return None

# Validación de sesión externa
def validar_sesion(token):
    token_secreto = obtener_token_secreto()
    if not token_secreto:
        logger.error("Token secreto no disponible")
        return False
    return token == token_secreto

def requiere_sesion(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get('Authorization', '')
        token = auth_header.replace('Bearer ', '').strip()

        if not token:
            # Intentar obtener token desde query param
            token = request.args.get('token', '')

        if not token or not validar_sesion(token):
            return jsonify({"error": "Sesión inválida o expirada."}), 401
        return f(*args, **kwargs)
    return decorated

UPLOAD_FOLDER = 'uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Configuración de Document AI
PROJECT_ID = os.getenv("GCP_PROJECT_ID", "co-impocali-cld-01")
LOCATION = os.getenv("GCP_REGION", "us")
PROCESSORS = {
    "cedulas": os.getenv("PROCESSOR_CEDULAS"),
    "RUT": os.getenv("PROCESSOR_RUT"),
    "camara_comercio": os.getenv("PROCESSOR_CAMARA")
}


def procesar_documento_con_ai(file_path, processor_id):
    mime_type, _ = mimetypes.guess_type(file_path)
    mime_type = mime_type or "application/pdf"
    try:
        client = documentai.DocumentProcessorServiceClient()
        name = f"projects/{PROJECT_ID}/locations/{LOCATION}/processors/{processor_id}"

        with open(file_path, "rb") as file:
            document = {"content": file.read(), "mime_type": mime_type}

        request = {"name": name, "raw_document": document}
        result = client.process_document(request=request)
        doc = result.document

        entidades = {}
        for entity in doc.entities:
            tipo = entity.type_.lower().replace(" ", "_")
            valor = entity.mention_text
            confianza = round(entity.confidence * 100, 2)
            entidades[tipo] = {"valor": valor, "confianza": f"{confianza}%"}

        return entidades

    except InvalidArgument as e:
        logger.error(f"Error en Document AI: {e}")
        return {"error": f"Formato no soportado ({mime_type}): {str(e)}"}

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/admin')
@requiere_sesion
def admin():
    conn = get_db_connection()
    if not conn:
        return "Error al conectar a la base de datos", 500

    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT s.id, s.fecha, s.estado, s.usuario_id, u.correo
        FROM solicitudes s
        LEFT JOIN usuarios u ON s.usuario_id = u.id
        ORDER BY s.fecha DESC
    """)

    solicitudes = cursor.fetchall()
    conn.close()
    return render_template('admin.html', solicitudes=solicitudes)


@app.route('/detalle/<int:id>')
@requiere_sesion  # Valida token en header Authorization
def detalle(id):
    conn = get_db_connection()
    if not conn:
        return "Error al conectar a la base de datos", 500

    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM solicitudes WHERE id = %s", (id,))
    solicitud_base = cursor.fetchone()

    if not solicitud_base:
        conn.close()
        return "Solicitud no encontrada", 404

    cursor.execute("SELECT * FROM archivos WHERE solicitud_id = %s", (id,))
    archivos = cursor.fetchall()

    cursor.execute("SELECT * FROM datos_extraidos WHERE solicitud_id = %s", (id,))
    datos_extraidos_raw = cursor.fetchall()

    conn.close()

    datos_extraidos = {}
    for dato in datos_extraidos_raw:
        tipo = dato['tipo_documento']
        if tipo not in datos_extraidos:
            datos_extraidos[tipo] = {}
        datos_extraidos[tipo][dato['campo']] = {
            "valor": dato['valor'],
            "confianza": dato['confianza']
        }

    solicitud = {
        "id": solicitud_base['id'],
        "usuario_id": solicitud_base['usuario_id'],
        "fecha": solicitud_base['fecha'],
        "estado": solicitud_base['estado'],
        "motivo": solicitud_base['motivo_rechazo'],
        "archivos": [
            {
                "nombre": archivo["nombre_archivo"],
                "ruta": archivo["ruta_archivo"]
            }
            for archivo in archivos
        ],
        "info": datos_extraidos
    }

    return render_template('detalle.html', solicitud=solicitud)

def subir_a_gcs(ruta_local, carpeta, nombre_archivo):
    client = storage.Client.from_service_account_json(os.getenv("GCS_CREDENTIALS_PATH"))
    bucket = client.bucket(os.getenv("GCS_BUCKET_NAME"))
    blob = bucket.blob(f"{carpeta}/{nombre_archivo}")
    blob.upload_from_filename(ruta_local)
    return blob.public_url

@app.route('/subir', methods=['POST'])
def subir_documentos():
    try:
        logger.info("📥 [INICIO] Subida de documentos")

        auth_header = request.headers.get('Authorization', '')
        token = auth_header.replace('Bearer ', '').strip()
        logger.info(f"🔑 Token recibido: {token[:10]}...")

        token_sistema = obtener_token_secreto()
        if not token_sistema:
            logger.error("❌ Token desde Secret Manager no disponible")
            return jsonify({"error": "No se pudo validar el token"}), 500

        if token != token_sistema:
            logger.warning("❌ Token inválido.")
            return jsonify({"error": "Token inválido"}), 401

        usuario_id = int(request.form.get("usuario_id", "0"))
        correo = request.form.get("correo")
        logger.info(f"👤 Usuario ID: {usuario_id}, Correo: {correo}")

        if not usuario_id or not correo:
            logger.warning("❌ Falta ID o correo.")
            return jsonify({"error": "Falta el ID o el correo del usuario"}), 400

        if 'docIdentidad' not in request.files or 'rut' not in request.files or 'camara' not in request.files:
            logger.warning(f"📁 Archivos recibidos incompletos: {list(request.files.keys())}")
            return jsonify({"error": "Faltan uno o más archivos obligatorios"}), 400

        logger.info("📁 Archivos recibidos correctamente")

        doc_identidad = request.files['docIdentidad']
        rut = request.files['rut']
        camara = request.files['camara']

        now = datetime.now()
        fecha_actual = now.strftime("%Y-%m-%d %H:%M:%S")
        fecha_para_archivo = now.strftime("%Y-%m-%d")

        conn = get_db_connection()
        if not conn:
            return "Error al conectar a la base de datos", 500
        cursor = conn.cursor(dictionary=True)

        cursor.execute("SELECT carpeta_gcs FROM usuarios WHERE id = %s", (usuario_id,))
        row = cursor.fetchone()

        if row and row.get('carpeta_gcs'):
            carpeta_gcs = row['carpeta_gcs']
        else:
            carpeta_gcs = f"{fecha_para_archivo}-{correo}"
            cursor.execute("UPDATE usuarios SET carpeta_gcs = %s WHERE id = %s", (carpeta_gcs, usuario_id))
            conn.commit()

        os.makedirs(UPLOAD_FOLDER, exist_ok=True)

        archivos_guardados = [
            ('doc_identidad', doc_identidad),
            ('rut', rut),
            ('camara_comercio', camara)
        ]

        rutas_temporales = {}

        for tipo, archivo in archivos_guardados:
            path_local = os.path.join(UPLOAD_FOLDER, archivo.filename)
            archivo.save(path_local)
            nombre_archivo_final = f"{fecha_para_archivo}-{archivo.filename}"
            url = subir_a_gcs(path_local, carpeta_gcs, nombre_archivo_final)
            rutas_temporales[tipo] = {"local": path_local, "final_name": nombre_archivo_final, "url": url}

        texto_identidad = procesar_documento_con_ai(rutas_temporales['doc_identidad']['local'], PROCESSORS["cedulas"])
        texto_rut = procesar_documento_con_ai(rutas_temporales['rut']['local'], PROCESSORS["RUT"])
        texto_camara = procesar_documento_con_ai(rutas_temporales['camara_comercio']['local'], PROCESSORS["camara_comercio"])

        cursor.execute(
            "INSERT INTO solicitudes (usuario_id, fecha, estado, correo) VALUES (%s, %s, %s, %s)",
            (usuario_id, fecha_actual, 'sin revisar', correo)
        )
        solicitud_id = cursor.lastrowid

        for tipo, datos in rutas_temporales.items():
            cursor.execute(
                "INSERT INTO archivos (solicitud_id, tipo, nombre_archivo, ruta_archivo) VALUES (%s, %s, %s, %s)",
                (solicitud_id, tipo, datos['final_name'], datos['url'])
            )

        for tipo_doc, datos in {'cedulas': texto_identidad, 'RUT': texto_rut, 'camara_comercio': texto_camara}.items():
            for campo, detalle in datos.items():
                cursor.execute(
                    "INSERT INTO datos_extraidos (solicitud_id, tipo_documento, campo, valor, confianza) VALUES (%s, %s, %s, %s, %s)",
                    (solicitud_id, tipo_doc, campo, detalle.get("valor", ""), detalle.get("confianza", ""))
                )

        conn.commit()
        conn.close()

        for datos in rutas_temporales.values():
            if os.path.exists(datos["local"]):
                os.remove(datos["local"])

        logger.info(f"✅ Solicitud {solicitud_id} creada exitosamente.")
        return jsonify({"status": "ok", "mensaje": "Documentos cargados y procesados correctamente."})

    except Exception as e:
        logger.error("❌ Error en /subir:")
        logger.error(traceback.format_exc())
        return jsonify({"error": "Error interno del servidor", "detalle": str(e)}), 500

def obtener_token_secreto(nombre_secreto="token"):
    try:
        client = secretmanager.SecretManagerServiceClient()
        project_id = os.getenv('GCP_PROJECT_ID')
        if not project_id:
            logger.error("❌ GCP_PROJECT_ID está vacío o no definido")
            return None

        name = f"projects/{project_id}/secrets/{nombre_secreto}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        secret_string = response.payload.data.decode("UTF-8")
        
        token_cargado = json.loads(secret_string).get("token")

        # 🔍 Log para depuración
        logger.info(f"🔐 Token cargado desde Secret Manager: {token_cargado}")

        return token_cargado

    except Exception as e:
        logger.error(f"❌ Error accediendo a Secret Manager: {e}")
        return None

@app.route('/rechazar/<int:id>', methods=['POST'])
@requiere_sesion
def rechazar(id):
    motivo = request.form.get("motivo", "").strip()
    if not motivo:
        return jsonify({"error": "El motivo es obligatorio"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Error de conexión a la base de datos"}), 500

    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT correo FROM solicitudes WHERE id = %s", (id,))
        fila = cursor.fetchone()
        correo_destino = fila['correo'] if fila else None

        cursor = conn.cursor()
        cursor.execute(
            "UPDATE solicitudes SET estado = %s, motivo_rechazo = %s WHERE id = %s",
            ('rechazado', motivo, id)
        )
        conn.commit()
        logger.info(f"📄 Solicitud {id} rechazada con motivo: {motivo}")

        if correo_destino:
            asunto = "📄 Tu solicitud ha sido rechazada - Impocali"
            mensaje_html = f"""
            <div style="font-family: Arial, sans-serif; color: #333;">
                <h2 style="color: #d9534f;">Solicitud Rechazada</h2>
                <p>Hola,</p>
                <p>Lamentamos informarte que tu solicitud fue <strong>rechazada</strong> por el siguiente motivo:</p>
                <blockquote style="border-left: 4px solid #d9534f; padding-left: 10px; color: #a94442;">
                    {motivo}
                </blockquote>
                <p>Por favor revisa la documentación y vuelve a subirla en el portal de autogestión.</p>
                <p style="margin-top: 20px;">Gracias,<br><strong>Equipo Impocali</strong></p>
            </div>
            """
            enviar_correo_rechazo(correo_destino, asunto, mensaje_html)

        return jsonify({"status": "ok"})

    except Exception as e:
        logger.error(f"❌ Error en rechazo de solicitud: {e}")
        return jsonify({"error": "Error interno"}), 500

    finally:
        conn.close()

def enviar_correo_aprobacion(destinatario):
    remitente = os.getenv('GMAIL_SENDER')

    try:
        from auth import TOKEN_FILE
        from google.auth.transport.requests import Request

        with open(TOKEN_FILE, "rb") as token_file:
            creds = pickle.load(token_file)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                logger.info("🔁 Token de Gmail refrescado exitosamente.")
            else:
                logger.error("❌ Token inválido y sin refresh_token. Requiere reautenticación.")
                return False

        service = build('gmail', 'v1', credentials=creds)

        mensaje_html = """
        <div style="font-family: Arial, sans-serif; color: #333;">
            <h2 style="color: #5cb85c;">¡Documentación Aprobada!</h2>
            <p>Hola,</p>
            <p>Nos complace informarte que tu documentación ha sido <strong>aprobada correctamente</strong>.</p>
            <p>Ya puedes continuar con los siguientes pasos desde la plataforma de autogestión.</p>
            <p style="margin-top: 20px;">Gracias,<br><strong>Equipo Impocali</strong></p>
        </div>
        """

        mensaje = MIMEText(mensaje_html, 'html')
        mensaje['to'] = destinatario
        mensaje['from'] = remitente
        mensaje['subject'] = "✅ Documentación aprobada - Impocali"

        mensaje_base64 = base64.urlsafe_b64encode(mensaje.as_bytes()).decode()
        cuerpo = {'raw': mensaje_base64}

        service.users().messages().send(userId="me", body=cuerpo).execute()
        logger.info(f"📧 Correo de aprobación enviado a {destinatario}")
        return True  # Éxito

    except Exception as e:
        logger.error(f"❌ Error al enviar correo de aprobación a {destinatario}: {e}")
        return False  # Falla
@app.route('/aceptar/<int:id>', methods=['POST'])
@requiere_sesion
def aceptar(id):
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Error de conexión"}), 500

    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT correo FROM solicitudes WHERE id = %s", (id,))
        fila = cursor.fetchone()
        correo_destino = fila['correo'] if fila else None

        cursor = conn.cursor()
        cursor.execute("UPDATE solicitudes SET estado = %s WHERE id = %s", ('aprobado', id))
        conn.commit()
        logger.info(f"Solicitud {id} aprobada.")

        if correo_destino:
            exito = enviar_correo_aprobacion(correo_destino)
            if not exito:
                return jsonify({
                    "status": "error",
                    "mensaje": "La solicitud fue aprobada, pero el correo no pudo enviarse."
                }), 202  # Aprobado, pero con advertencia

        return jsonify({"status": "ok"})

    except Exception as e:
        logger.error(f"❌ Error en aprobación: {e}")
        return jsonify({"error": "Error interno"}), 500

    finally:
        conn.close()

def enviar_correo_rechazo(destinatario, asunto, mensaje_html):
    remitente = os.getenv('GMAIL_SENDER')

    try:
        from auth import TOKEN_FILE
        from google.auth.transport.requests import Request

        with open(TOKEN_FILE, "rb") as token_file:
            creds = pickle.load(token_file)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                logger.info("🔁 Token de Gmail refrescado exitosamente.")
            else:
                logger.error("❌ Token inválido y sin refresh_token. Requiere reautenticación.")
                return False

        service = build('gmail', 'v1', credentials=creds)

        mensaje = MIMEText(mensaje_html, 'html')
        mensaje['to'] = destinatario
        mensaje['from'] = remitente
        mensaje['subject'] = asunto

        mensaje_base64 = base64.urlsafe_b64encode(mensaje.as_bytes()).decode()
        cuerpo = {'raw': mensaje_base64}

        service.users().messages().send(userId="me", body=cuerpo).execute()
        logger.info(f"📧 Correo de rechazo enviado a {destinatario}")
        return True  # éxito

    except Exception as e:
        logger.error(f"❌ Error al enviar correo de rechazo a {destinatario}: {e}")
        return False  # falla

@app.route('/validar-token', methods=['GET'])
def validar_token_simple():
    auth_header = request.headers.get('Authorization', '')
    token = auth_header.replace('Bearer ', '').strip()
    if token == obtener_token_secreto():
        return jsonify({"status": "ok"})
    return jsonify({"error": "Token inválido"}), 401
@app.route('/probar-envio-correo', methods=['POST'])

def probar_envio_correo():
    from google.auth.exceptions import GoogleAuthError
    from googleapiclient.errors import HttpError
    from google.auth.transport.requests import Request

    data = request.json
    destinatario = data.get('destinatario')
    remitente = data.get('remitente', os.getenv("GMAIL_SENDER"))

    if not destinatario or not remitente:
        return jsonify({"error": "Falta el destinatario o el remitente"}), 400

    mensaje_html = f"""
    <div style="font-family: Arial, sans-serif; color: #333;">
        <h2 style="color: #0b57d0;">Prueba desde: {remitente}</h2>
        <p>Este es un <strong>correo de prueba</strong> desde Impocali.</p>
        <p><strong>Remitente:</strong> {remitente}</p>
        <p><strong>Fecha:</strong> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
    </div>
    """

    try:
        from auth import TOKEN_FILE
        with open(TOKEN_FILE, "rb") as token_file:
            creds = pickle.load(token_file)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                logger.info("🔁 Token de Gmail refrescado exitosamente.")
            else:
                logger.error("❌ Token inválido y sin refresh_token.")
                return jsonify({"error": "Token inválido o requiere reautenticación"}), 401

        service = build('gmail', 'v1', credentials=creds)

        mensaje = MIMEText(mensaje_html, 'html')
        mensaje['to'] = destinatario
        mensaje['from'] = remitente
        mensaje['subject'] = f"Prueba desde {remitente} - Impocali"

        mensaje_base64 = base64.urlsafe_b64encode(mensaje.as_bytes()).decode()
        cuerpo = {'raw': mensaje_base64}

        service.users().messages().send(userId="me", body=cuerpo).execute()

        return jsonify({
            "status": "ok",
            "remitente": remitente,
            "destinatario": destinatario,
            "mensaje": f"Correo enviado desde {remitente} a {destinatario}"
        })

    except HttpError as http_err:
        return jsonify({
            "status": "error",
            "tipo": "HttpError",
            "remitente": remitente,
            "detalle": str(http_err),
            "response": http_err.resp.__dict__ if hasattr(http_err, 'resp') else "sin detalle"
        }), 500

    except GoogleAuthError as auth_err:
        return jsonify({
            "status": "error",
            "tipo": "AuthError",
            "remitente": remitente,
            "detalle": str(auth_err)
        }), 500

    except Exception as e:
        return jsonify({
            "status": "error",
            "tipo": "Otro",
            "remitente": remitente,
            "detalle": str(e)
        }), 500

@app.errorhandler(Exception)
def handle_exception(e):
    logger.error("Error inesperado:", exc_info=e)
    return jsonify({"error": "Error interno del servidor"}), 500
@app.route('/iframe')
def iframe():
    return render_template('iframe.html')



if __name__ == "__main__":
    if os.getenv("FLASK_ENV") == "production":
        from waitress import serve
        serve(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
    else:
        app.run(debug=True, port=8000)
