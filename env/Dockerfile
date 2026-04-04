FROM nvidia/cuda:11.8.0-cudnn8-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

WORKDIR /workspace

RUN apt-get update && apt-get install -y \
    python3.10 \
    python3-pip \
    python3-dev \
    gdal-bin \
    libgdal-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --upgrade pip setuptools wheel

# GPU hostではCUDA版torch、CPU環境でも実行可能
RUN pip3 install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118

COPY env/requirements.txt /tmp/requirements.txt
RUN pip3 install -r /tmp/requirements.txt

CMD ["python3", "run.py", "--config", "config/config.yaml"]
