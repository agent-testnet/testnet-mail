FROM python:3.11-slim

WORKDIR /app

# Install build dependencies and Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY app.py .
COPY templates/ templates/
COPY static/ static/

# Expose port
EXPOSE 5000

# Hit /healthz (unauthenticated). The previous probe targeted / which now
# 302-redirects to /login -- still a 200, but noisy and dependent on the
# auth wiring.
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:5000/healthz').status == 200 else 1)" || exit 1

# Production WSGI server. gunicorn imports `app:app` directly, which means
# the fail-closed env validation in dashboard/app.py runs at worker boot --
# a missing DASHBOARD_PASSWORD aborts the worker rather than silently
# serving an unauthenticated dashboard. Sizing rationale:
#   workers=2 + threads=4 (gthread): modest concurrency for the blocking
#     imaplib.fetch loop in app.py without thrashing a t3a.micro.
#   timeout=60: the multi-account IMAP scrape can outlast gunicorn's 30s
#     default; we'd rather complete a slow request than kill the worker.
#   access/error logs to stdout so they flow into `docker compose logs`.
CMD ["gunicorn", \
     "--bind", "0.0.0.0:5000", \
     "--workers", "2", \
     "--threads", "4", \
     "--worker-class", "gthread", \
     "--timeout", "60", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "app:app"]
