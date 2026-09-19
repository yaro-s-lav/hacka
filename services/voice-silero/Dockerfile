FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir fastapi==0.115.8 uvicorn==0.34.0 numpy==2.2.3 && \
    pip install --no-cache-dir torch==2.6.0 --index-url https://download.pytorch.org/whl/cpu
RUN mkdir -p /models && curl -fL --retry 3 -o /models/v5_5_ru.pt https://models.silero.ai/models/tts/ru/v5_5_ru.pt
WORKDIR /service
COPY services/voice-silero/server.py services/voice-silero/selection.py services/voice-silero/verified_phrases.py services/voice-silero/russian_numbers.py ./
CMD ["uvicorn","server:app","--host","0.0.0.0","--port","8092"]
