import os
import json
import mimetypes
import logging
import traceback
from datetime import datetime
from functools import wraps

import requests
import mysql.connector
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, jsonify
from google.api_core.exceptions import InvalidArgument
from google.cloud import secretmanager
from google.cloud import documentai_v1 as documentai
from google.cloud import storage

# Configuración dinámica de entorno
env = os.getenv('FLASK_ENV', 'development')
dotenv_file = f'.env.{env}'
load_dotenv(dotenv_file)

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
    url = os.getenv('SESION_VALIDACION_URL')
    if not url:
        logger.error("URL para validar sesión no configurada.")
        return False

    headers = {'Authorization': f'Bearer {token}'}
    try:
        respuesta = requests.get(url, headers=headers, timeout=5)
        return respuesta.status_code == 200
    except requests.RequestException as e:
        logger.error(f"Error validando sesión: {e}")
        return False

def requiere_sesion(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get('Authorization', '')
        token = auth_header.replace('Bearer ', '').strip()
        if not token or not validar_sesion(token):
            return jsonify({"error": "Sesión inválida o expirada."}), 401
        return f(*args, **kwargs)
    return decorated

# Configuración de Flask
app = Flask(__name__)
UPLOAD_FOLDER = 'uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Configuración de Document AI
PROJECT_ID = "co-impocali-cld-01"
LOCATION = "us"
PROCESSORS = {
    "cedulas": "4ea25bcf738a49d8",
    "RUT": "a2602ab41b5478fb",
    "camara_comercio": "c643a041b96f7c47"
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
        "archivos": [archivo['nombre_archivo'] for archivo in archivos],
        "info": datos_extraidos
    }

    return render_template('detalle.html', solicitud=solicitud)

@app.route('/aceptar/<int:id>', methods=['POST'])
@requiere_sesion
def aceptar(id):
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Error de conexión"}), 500

    cursor = conn.cursor()
    cursor.execute("UPDATE solicitudes SET estado = %s WHERE id = %s", ('aprobado', id))
    conn.commit()
    conn.close()

    logger.info(f"Solicitud {id} aprobada.")
    return jsonify({"status": "ok"})

@app.route('/rechazar/<int:id>', methods=['POST'])
@requiere_sesion
def rechazar(id):
    motivo = request.form.get("motivo", "")
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Error de conexión"}), 500

    cursor = conn.cursor()
    cursor.execute("UPDATE solicitudes SET estado = %s, motivo_rechazo = %s WHERE id = %s", ('rechazado', motivo, id))
    conn.commit()
    conn.close()

    logger.info(f"Solicitud {id} rechazada. Motivo: {motivo}")
    return jsonify({"status": "ok"})

def subir_a_gcs(ruta_local, carpeta, nombre_archivo):
    client = storage.Client.from_service_account_json(os.getenv("GCS_CREDENTIALS_PATH"))
    bucket = client.bucket(os.getenv("GCS_BUCKET_NAME"))
    blob = bucket.blob(f"{carpeta}/{nombre_archivo}")
    blob.upload_from_filename(ruta_local)
    return blob.public_url

@app.route('/subir', methods=['POST'])
def subir_documentos():
    try:
        auth_header = request.headers.get('Authorization', '')
        token = auth_header.replace('Bearer ', '').strip()

        if token != obtener_token_secreto():
            return jsonify({"error": "Token inválido"}), 401

        usuario_id = int(request.form.get("usuario_id", "0"))
        correo = request.form.get("correo")

        if not usuario_id or not correo:
            return jsonify({"error": "Falta el ID o el correo del usuario"}), 400

        if 'docIdentidad' not in request.files or 'rut' not in request.files or 'camara' not in request.files:
            return jsonify({"error": "Faltan uno o más archivos obligatorios"}), 400

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

        logger.info(f"Solicitud {solicitud_id} creada exitosamente.")
        return redirect(url_for('admin'))

    except Exception as e:
        logger.error("Error en /subir:")
        logger.error(traceback.format_exc())
        return jsonify({"error": "Error interno del servidor", "detalle": str(e)}), 500

def obtener_token_secreto(nombre_secreto="token"):
    try:
        client = secretmanager.SecretManagerServiceClient()
        project_id = os.getenv('GCP_PROJECT_ID')
        name = f"projects/{project_id}/secrets/{nombre_secreto}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        secret_string = response.payload.data.decode("UTF-8")
        return json.loads(secret_string).get("token")
    except Exception as e:
        logger.error(f"Error accediendo a Secret Manager: {e}")
        return None

@app.route('/validar-token', methods=['GET'])
def validar_token_simple():
    auth_header = request.headers.get('Authorization', '')
    token = auth_header.replace('Bearer ', '').strip()
    if token == obtener_token_secreto():
        return jsonify({"status": "ok"})
    return jsonify({"error": "Token inválido"}), 401

if __name__ == '__main__':
    app.run(debug=True)
