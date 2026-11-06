FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/home/app

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 --shell /usr/sbin/nologin app \
    && mkdir -p /data /var/log/contract-checker \
    && chown app:app /data /var/log/contract-checker

COPY requirements.txt .
RUN pip install --timeout 120 --retries 10 -r requirements.txt \
    && pip uninstall -y opencv-python \
    && pip install --timeout 120 --retries 10 opencv-python-headless==5.0.0.93
RUN pip install --timeout 300 --retries 5 redis==8.1.0 boto3==1.43.123

COPY --chown=app:app app.py .
COPY --chown=app:app core ./core
COPY --chown=app:app pipeline ./pipeline
COPY --chown=app:app prompts ./prompts
COPY --chown=app:app services ./services
COPY --chown=app:app apps ./apps
COPY --chown=app:app scripts ./scripts
COPY --chown=app:app alembic ./alembic
COPY --chown=app:app alembic.ini .
COPY --chown=app:app .streamlit ./.streamlit

USER app

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health')"

ENTRYPOINT ["python", "-m", "scripts.entrypoint"]
CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501"]
