FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/app
COPY app.py ./
COPY templates ./templates
RUN mkdir -p /opt/app/data
# .env and known_hosts are mounted at runtime — do not bake secrets into the image

EXPOSE 8080
CMD ["python3", "-u", "app.py"]
