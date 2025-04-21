from flask import Flask, render_template, request, redirect, url_for, jsonify, Response
import os
from datetime import datetime
from google.cloud import documentai_v1 as documentai
import mimetypes
from google.api_core.exceptions import InvalidArgument
from dotenv import load_dotenv
import mysql.connector
from functools import wraps
import logging
import requests

# ========================
# 🚀 Configuración .env dinámico
# ========================
env = os.getenv('FLASK_ENV', 'development')
dotenv_file = f'.env.{env}'
load_dotenv(dotenv_file)

# ========================
# 🚀 Logging
# ========================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ========================
# 🚀 Conexión a la base de datos
# ========================
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

# ========================
# 🚀 Validación de sesión externa (para admin)
# ========================

def validar_sesion(token):
    url = os.getenv('SESION_VALIDACION_URL')
    if not url:
        logger.error("URL para validar sesión no configurada.")
        return False

    headers = {'Authorization': f'Bearer {token}'}
    try:
        respuesta = requests.get(url, headers=headers, timeout=5)
        if respuesta.status_code == 200:
            logger.info("Sesión válida confirmada.")
            return True
        else:
            logger.warning(f"Sesión inválida o expirada. Status: {respuesta.status_code}")
            return False
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

# ========================
# 🚀 Flask app y configuración
# ========================
app = Flask(__name__)
UPLOAD_FOLDER = 'uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# ========================
# 🚀 Configuración de Document AI
# ========================
PROJECT_ID = "co-impocali-cld-01"
LOCATION = "us"
PROCESSORS = {
    "cedulas": "4ea25bcf738a49d8",
    "RUT": "a2602ab41b5478fb",
    "camara_comercio": "c643a041b96f7c47"
}

# ========================
# 🚀 Mapeo de tokens a usuario_id (Preparado para JWT futuro)
# ========================
TOKENS_MAP = {
    "abcabc": 1,  # user_id = 1
    "xyzxyz": 2,  # user_id = 2
    # Agrega más tokens y usuarios aquí
}

def obtener_usuario_id_desde_token(token):
    return TOKENS_MAP.get(token)

# ========================
# 🚀 Función de procesamiento con Document AI
# ========================
def procesar_documento_con_ai(file_path, processor_id):
    mime_type, _ = mimetypes.guess_type(file_path)
    if mime_type is None:
        mime_type = "application/pdf"
    try:
        client = documentai.DocumentProcessorServiceClient()
        name = f"projects/{PROJECT_ID}/locations/{LOCATION}/processors/{processor_id}"

        with open(file_path, "rb") as file:
            document = {
                "content": file.read(),
                "mime_type": mime_type
            }

        request = {"name": name, "raw_document": document}
        result = client.process_document(request=request)
        doc = result.document

        entidades = {}
        for entity in doc.entities:
            tipo = entity.type_.lower().replace(" ", "_")
            valor = entity.mention_text
            confianza = round(entity.confidence * 100, 2)
            entidades[tipo] = {
                "valor": valor,
                "confianza": f"{confianza}%"
            }

        return entidades

    except InvalidArgument as e:
        logger.error(f"Error en Document AI: {e}")
        return {"error": f"Formato no soportado ({mime_type}): {str(e)}"}

# ========================
# 🚀 Rutas Flask
# ========================

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/admin')
def admin():
    conn = get_db_connection()
    if not conn:
        return "Error al conectar a la base de datos", 500

    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM solicitudes ORDER BY fecha DESC")
    solicitudes = cursor.fetchall()

    conn.close()
    return render_template('admin.html', solicitudes=solicitudes)

@app.route('/detalle/<int:id>')
def detalle(id):
    conn = get_db_connection()
    if not conn:
        return "Error al conectar a la base de datos", 500

    cursor = conn.cursor(dictionary=True)

    # Obtener la solicitud base
    cursor.execute("SELECT * FROM solicitudes WHERE id = %s", (id,))
    solicitud_base = cursor.fetchone()

    if not solicitud_base:
        conn.close()
        return "Solicitud no encontrada", 404

    # Obtener los archivos asociados
    cursor.execute("SELECT * FROM archivos WHERE solicitud_id = %s", (id,))
    archivos = cursor.fetchall()

    # Obtener los datos extraídos
    cursor.execute("SELECT * FROM datos_extraidos WHERE solicitud_id = %s", (id,))
    datos_extraidos_raw = cursor.fetchall()

    conn.close()

    # Procesar datos extraídos para que se agrupen por tipo_documento
    datos_extraidos = {}
    for dato in datos_extraidos_raw:
        tipo = dato['tipo_documento']
        if tipo not in datos_extraidos:
            datos_extraidos[tipo] = {}
        datos_extraidos[tipo][dato['campo']] = {
            "valor": dato['valor'],
            "confianza": dato['confianza']
        }

    # Preparar la estructura esperada por la plantilla detalle.html
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

@app.route('/subir', methods=['POST'])
def subir_documentos():
    # Obtener token desde headers
    auth_header = request.headers.get('Authorization', '')
    token = auth_header.replace('Bearer ', '').strip()

    # Obtener user_id desde token
    usuario_id = obtener_usuario_id_desde_token(token)

    if not usuario_id:
        return jsonify({"error": "Token inválido o usuario no encontrado"}), 401

    doc_identidad = request.files['docIdentidad']
    rut = request.files['rut']
    camara = request.files['camara']

    fecha = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    path_identidad = os.path.join(UPLOAD_FOLDER, doc_identidad.filename)
    path_rut = os.path.join(UPLOAD_FOLDER, rut.filename)
    path_camara = os.path.join(UPLOAD_FOLDER, camara.filename)

    doc_identidad.save(path_identidad)
    rut.save(path_rut)
    camara.save(path_camara)

    texto_identidad = procesar_documento_con_ai(path_identidad, PROCESSORS["cedulas"])
    texto_rut = procesar_documento_con_ai(path_rut, PROCESSORS["RUT"])
    texto_camara = procesar_documento_con_ai(path_camara, PROCESSORS["camara_comercio"])

    conn = get_db_connection()
    if not conn:
        return "Error al conectar a la base de datos", 500

    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO solicitudes (usuario_id, fecha, estado) VALUES (%s, %s, %s)",
        (usuario_id, fecha, 'sin revisar')
    )
    solicitud_id = cursor.lastrowid

    archivos = [
        ('doc_identidad', doc_identidad.filename, path_identidad),
        ('rut', rut.filename, path_rut),
        ('camara_comercio', camara.filename, path_camara),
    ]
    for tipo, nombre_archivo, ruta in archivos:
        cursor.execute(
            "INSERT INTO archivos (solicitud_id, tipo, nombre_archivo, ruta_archivo) VALUES (%s, %s, %s, %s)",
            (solicitud_id, tipo, nombre_archivo, ruta)
        )

    for tipo_doc, datos in {'cedulas': texto_identidad, 'RUT': texto_rut, 'camara_comercio': texto_camara}.items():
        for campo, detalle in datos.items():
            cursor.execute(
                "INSERT INTO datos_extraidos (solicitud_id, tipo_documento, campo, valor, confianza) VALUES (%s, %s, %s, %s, %s)",
                (solicitud_id, tipo_doc, campo, detalle.get("valor", ""), detalle.get("confianza", ""))
            )

    conn.commit()
    conn.close()

    logger.info(f"Solicitud {solicitud_id} creada exitosamente para usuario_id {usuario_id}.")
    return redirect(url_for('admin'))

# ========================
# 🚀 Main
# ========================
if __name__ == '__main__':
    app.run(debug=True)
