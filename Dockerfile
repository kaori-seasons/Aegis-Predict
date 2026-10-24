FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY data ./data

RUN pip install --no-cache-dir ".[ml]"

ENV PYTHONPATH=/app/src
EXPOSE 8000

CMD ["python", "-m", "overseas_inventory.competition_web", "--host", "0.0.0.0", "--port", "8000"]
