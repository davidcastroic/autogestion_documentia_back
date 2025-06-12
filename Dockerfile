FROM python:3.10-slim

# Variables de entorno básicas
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Instalar dependencias
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar código fuente del backend
COPY . .

# Copiar archivos sensibles necesarios para Gmail API
# ⚠️ IMPORTANTE: asegúrate de que estos archivos estén en tu proyecto local
COPY client_secret.json ./client_secret.json
COPY creds/gmail_token.pickle ./creds/gmail_token.pickle

# Crear carpeta /uploads para archivos temporales
RUN mkdir -p /app/uploads

# Exponer el puerto
EXPOSE 8000

# Comando para arrancar con Gunicorn
CMD ["gunicorn", "-b", "0.0.0.0:8000", "app:app", "--timeout", "120"]
