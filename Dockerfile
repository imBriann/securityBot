# Usa una imagen base ligera con Python
FROM python:3.10-slim

# Establece variables para no tener que confirmar durante la instalación
ENV DEBIAN_FRONTEND=noninteractive

# Instala Tesseract OCR y dependencias básicas
RUN apt-get update && \
    apt-get install -y tesseract-ocr libtesseract-dev libleptonica-dev \
    poppler-utils build-essential libglib2.0-0 libsm6 libxext6 libxrender-dev \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Establece el directorio de trabajo dentro del contenedor
WORKDIR /app

# Copia los archivos del proyecto al contenedor
COPY . .

# Instala las dependencias de Python
RUN pip install --no-cache-dir --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# Expone el puerto en el que correrá FastAPI
EXPOSE 8000

# Ejecuta el servidor Uvicorn
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
