FROM mcr.microsoft.com/playwright/python:v1.64.1-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/renderer
RUN pip install --no-cache-dir fastapi==0.121.3 uvicorn==0.38.0 playwright==1.64.1
COPY --chown=pwuser:pwuser app.py ./app.py

USER pwuser
EXPOSE 8090
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8090", "--workers", "1"]
