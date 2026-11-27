FROM python:3.13-slim

# Install system dependencies, including full LibreOffice and fonts.
# fonts-noto-core gives Add Text / Edit Existing Text glyphs for Indian scripts,
# the rupee sign, curly quotes, etc.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice \
    libreoffice-writer \
    libreoffice-calc \
    libreoffice-impress \
    fonts-liberation \
    fonts-dejavu-core \
    fonts-noto-core \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/*

WORKDIR /app

# Install Python dependencies first (for better caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application
COPY . .

# Create a non-root user and give it ownership of the app directory
# This ensures the app can create its upload/output directories
RUN useradd -m -u 1000 appuser \
    && mkdir -p converter_uploads converter_outputs \
    && chown -R appuser:appuser /app
USER appuser

# Fly.io uses the PORT environment variable
ENV PORT=8080
EXPOSE 8080

# Use 1 worker with threads for better memory stability with LibreOffice
CMD ["sh", "-c", "gunicorn -w 1 --threads 4 -b 0.0.0.0:${PORT} --timeout 120 app:app"]