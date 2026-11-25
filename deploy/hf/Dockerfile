FROM python:3.12-slim
# libgomp1 is required by LightGBM
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1000 user
WORKDIR /home/user/app
COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt huggingface_hub==2.4.0
COPY --chown=user . .
USER user
ENV HOST=0.0.0.0 PORT=7860 MALLOC_ARENA_MAX=2 DATA_DIR=/home/user/app/app_data \
    HIST_DIR=/nonexistent ARCHIVE_DAYS=7 BYBIT_LIVE_ENABLED=false PYTHONUNBUFFERED=1
EXPOSE 7860
CMD ["python", "-m", "app"]
