# syntax=docker/dockerfile:1.7
ARG PYTHON_IMAGE=python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
FROM ${PYTHON_IMAGE} AS wheelhouse
WORKDIR /build
COPY pyproject.toml README.md ./
COPY requirements ./requirements
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/pip \
    pip wheel --wheel-dir /wheels --constraint requirements/constraints.txt . \
    && python -c "import hashlib,pathlib; p=pathlib.Path('/wheels'); (p/'SHA256SUMS').write_text(''.join(hashlib.sha256(f.read_bytes()).hexdigest()+'  '+f.name+'\\n' for f in sorted(p.glob('*.whl'))))"

FROM ${PYTHON_IMAGE} AS runtime
WORKDIR /app
COPY --from=wheelhouse /wheels/SHA256SUMS /app/artifacts/runtime-wheel-sha256.txt
RUN --mount=from=wheelhouse,source=/wheels,target=/wheels \
    pip install --no-cache-dir --no-index --find-links /wheels aegis-predict \
    && useradd --uid 10001 --create-home aegis \
    && mkdir -p /app/var/inbox && chown -R aegis:aegis /app/var
COPY pyproject.toml README.md Dockerfile docker-compose.yml ./
COPY requirements ./requirements
COPY src ./src
COPY data/sample_network.json ./data/sample_network.json
USER 10001:10001
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8000
CMD ["aegis", "serve", "--host", "0.0.0.0", "--port", "8000"]
