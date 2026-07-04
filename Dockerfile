FROM python:3.12-slim

# No pip installs: the monitor uses only the Python standard library.
WORKDIR /app
COPY monitor.py .

# State + logs live on mounted volumes so they survive restarts/crashes.
RUN mkdir -p /data /logs && \
    useradd --create-home --uid 10001 appuser && \
    chown -R appuser:appuser /data /logs /app
VOLUME /data /logs
USER appuser

# Unbuffered stdout so `docker logs` shows checks live.
ENV PYTHONUNBUFFERED=1

CMD ["python3", "monitor.py", "run"]
