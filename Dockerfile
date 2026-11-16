FROM python:3.12-slim-bookworm

# Uses Iran apt mirror when building in restricted networks; harmless if the original hosts already work.
RUN sed -i 's|deb.debian.org|mirror.iranserver.com|g; s|security.debian.org|mirror.iranserver.com|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
    sed -i 's|deb.debian.org|mirror.iranserver.com|g; s|security.debian.org|mirror.iranserver.com|g' /etc/apt/sources.list 2>/dev/null || true \
 && apt-get update \
 && apt-get install -y --no-install-recommends openssh-client \
 && ln -sf /usr/local/bin/python3 /usr/bin/python3 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/app
COPY app.py ./
COPY templates ./templates
RUN mkdir -p /opt/app/data
# .env and known_hosts are mounted at runtime — do not bake secrets into the image

EXPOSE 8080
CMD ["python3", "-u", "app.py"]
