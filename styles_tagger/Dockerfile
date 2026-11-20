# TideSync style tagger: Essentia Discogs-EffNet styles + embeddings for every music file.
# Model weights by MTG-UPF, CC BY-NC-SA 4.0 (non-commercial use only).
FROM python:3.14-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg cifs-utils curl \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir --pre essentia-tensorflow numpy soundfile onnxruntime
RUN mkdir /models && cd /models \
    && curl -sSfLO https://essentia.upf.edu/models/feature-extractors/discogs-effnet/discogs-effnet-bsdynamic-1.onnx \
    && curl -sSfLO https://essentia.upf.edu/models/feature-extractors/discogs-effnet/discogs-effnet-bs64-1.json
COPY tagger.py /app/tagger.py
COPY entrypoint.sh /entrypoint.sh
ENV PYTHONUNBUFFERED=1 TF_CPP_MIN_LOG_LEVEL=2 OMP_NUM_THREADS=1
ENTRYPOINT ["/entrypoint.sh"]
