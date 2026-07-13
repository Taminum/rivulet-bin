FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /srv/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN addgroup --system appuser && adduser --system --ingroup appuser appuser

COPY --chown=appuser:appuser app ./app

USER appuser

EXPOSE 15212

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "15212"]
