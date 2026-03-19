FROM python:3.12-slim

WORKDIR /app

# Install OS-level deps needed by Pillow and lxml
RUN apt-get update && apt-get install -y --no-install-recommends \
        libjpeg-dev \
        zlib1g-dev \
        libxml2-dev \
        libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY substack_to_epub.py app.py ./
COPY templates/ templates/

EXPOSE 5000

CMD ["python3", "app.py"]
