# PrescriptAI — one production image: the FastAPI backend serves the built
# React app, so the UI and the API share an origin (README §6a: the session
# cookie and CSRF header then need no CORS).
#
#   docker compose up --build        -> http://localhost:8000

# ── stage 1: build the web app ───────────────────────────────────────────────
FROM node:20-alpine AS web
# Extra root CAs, for build machines whose antivirus or proxy intercepts HTTPS
# (certs/README.md). Empty folder = nothing added.
COPY certs/ /certs/
RUN cat /certs/*.crt > /certs/extra.pem 2>/dev/null || true
ENV NODE_EXTRA_CA_CERTS=/certs/extra.pem
WORKDIR /web
COPY web_app/package.json web_app/package-lock.json ./
RUN npm ci
COPY web_app/ ./
# Empty, not unset: the API is the page's own origin.
ENV VITE_API_URL=""
RUN npm run build

# ── stage 2: the API ─────────────────────────────────────────────────────────
# 3.12+ is required: config/settings.py nests quotes inside an f-string.
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

# A Unicode font for the prescription PDF (backend/rx_pdf.py); the PDF core
# fonts are Latin-1 only. apt verifies packages by signature, not TLS.
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Same extra CAs, added to the system store; pip and the Anthropic client
# (httpx) are pointed at it instead of their bundled certifi copy.
COPY certs/ /usr/local/share/ca-certificates/extra/
RUN find /usr/local/share/ca-certificates/extra -type f ! -name '*.crt' -delete \
    && update-ca-certificates
ENV PIP_CERT=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# main.py puts ../ml_pipeline on sys.path, so keep the repo layout.
COPY ml_pipeline/ ml_pipeline/
COPY backend/ backend/
COPY --from=web /web/dist /app/web_dist

ENV WEB_DIST=/app/web_dist \
    DATABASE_URL=sqlite:////app/data/prescriptai.db
RUN mkdir -p /app/data /app/backend/uploads /app/ml_pipeline/.cache

WORKDIR /app/backend
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
