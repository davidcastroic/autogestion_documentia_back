# Imagen base con Python 3.10
FROM python:3.10-slim

# Directorio de trabajo dentro del contenedor
WORKDIR /app

# Copiar archivos del proyecto al contenedor
COPY . /app

# Instalar dependencias
RUN pip install --no-cache-dir -r requirements.txt

# Expone el puerto usado por Flask
EXPOSE 5000

# Ejecuta la app usando Gunicorn para producción
CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:5000", "app:app"]
