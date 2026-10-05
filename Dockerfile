FROM python:3.12-slim

# ffmpeg: merging video+audio and MP3/M4A conversion. Deno: yt-dlp needs a JS runtime for YouTube.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl unzip \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py index.html ./

ENV STYPZY_PUBLIC=1 PYTHONUNBUFFERED=1
CMD ["python", "server.py"]