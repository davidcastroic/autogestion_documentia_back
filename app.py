from flask import Flask, render_template, request, redirect, url_for
import os
from datetime import datetime
from google.cloud import documentai_v1 as documentai
from flask import Flask, render_template, request, redirect, url_for, jsonify
import mimetypes
from google.api_core.exceptions import InvalidArgument

app = Flask(__name__)
UPLOAD_FOLDER = 'uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Base de datos temporal en memoria
solicitudes = []

# Configuración del procesador de Document AI
PROJECT_ID = "co-impocali-cld-01"
LOCATION = "us"
PROCESSORS = {
    "cedulas": "4ea25bcf738a49d8",
    "RUT": "a2602ab41b5478fb",
    "camara_comercio": "c643a041b96f7c47"
}



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

        # EXTRAER ENTIDADES
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
        return {"error": f"Formato no soportado ({mime_type}): {str(e)}"}




@app.route('/')
def index():
    return render_template('index.html')

@app.route('/aceptar/<int:id>', methods=['POST'])
def aceptar(id):
    solicitud = next((s for s in solicitudes if s["id"] == id), None)
    if solicitud:
        solicitud["estado"] = "aprobado"
        return jsonify({"status": "ok"})
    return jsonify({"error": "No encontrada"}), 404

@app.route('/rechazar/<int:id>', methods=['POST'])
def rechazar(id):
    motivo = request.form.get("motivo", "")
    solicitud = next((s for s in solicitudes if s["id"] == id), None)
    if solicitud:
        solicitud["estado"] = "rechazado"
        solicitud["motivo"] = motivo
        return jsonify({"status": "ok"})
    return jsonify({"error": "No encontrada"}), 404
@app.route('/subir', methods=['POST'])
def subir_documentos():
    doc_identidad = request.files['docIdentidad']
    rut = request.files['rut']
    camara = request.files['camara']

    usuario = f"usuario_{len(solicitudes)+1:03d}"
    fecha = datetime.now().strftime("%Y-%m-%d")

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

    info_extraida = {
    "cedulas": texto_identidad,
    "RUT": texto_rut,
    "camara_comercio": texto_camara
}


    solicitudes.append({
        "id": len(solicitudes)+1,
        "usuario": usuario,
        "fecha": fecha,
        "estado": "sin revisar",
        "archivos": [doc_identidad.filename, rut.filename, camara.filename],
        "info": info_extraida
    })

    return redirect(url_for('admin'))


@app.route('/admin')
def admin():
    return render_template('admin.html', solicitudes=solicitudes)


@app.route('/detalle/<int:id>')
def detalle(id):
    solicitud = next((s for s in solicitudes if s["id"] == id), None)
    if solicitud:
        return render_template('detalle.html', solicitud=solicitud)
    else:
        return "Solicitud no encontrada", 404


if __name__ == '__main__':
    app.run(debug=True)
