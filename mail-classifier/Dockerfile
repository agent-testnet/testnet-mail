FROM python:3.11-slim

WORKDIR /app

ENV PYTHONPATH=/app/src

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/

# Run as UID 33 (www-data on the Roundcube image) so that any files this
# service creates inside the shared roundcube-db volume -- sqlite.db,
# sqlite.db-wal, sqlite.db-shm -- are owned by the same UID Roundcube uses
# for its session/cache writes. If this container runs as root, it wins the
# race to lazy-create the SQLite file at startup and ends up owning it
# root:root mode 0644, locking Roundcube (www-data) out of session writes.
# The first symptom is "Invalid request! No data was saved." on every login
# attempt, because $_SESSION['temp'] can never be persisted and
# check_request() then fails the request-token guard in index.php. Numeric
# UID (no matching /etc/passwd entry needed) -- Docker accepts that fine.
USER 33:33

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -m mail_classifier.healthcheck

CMD ["python", "-m", "mail_classifier.main"]
