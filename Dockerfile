FROM python:3.10

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Copiamos explícitamente el archivo de entorno
COPY .env.production .env.production

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "app:app"]
