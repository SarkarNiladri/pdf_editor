FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    libreoffice \
    libreoffice-writer \
    libmupdf-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy application
COPY . .

# Install Python packages
RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 10000

CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:10000", "--timeout", "120", "app:app"]