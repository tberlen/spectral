FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    sshpass openssh-client gcc-arm-linux-gnueabi libc-dev-armel-cross \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "-u", "manager.py"]
