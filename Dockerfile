FROM node:22-bookworm-slim AS web
WORKDIR /app/frontend
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN npx --yes pnpm@10.22.0 install --frozen-lockfile
COPY frontend/ .
RUN npx --yes pnpm@10.22.0 run build

FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 XUDU_DATA=/var/lib/xudu
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 xudu \
    && useradd --uid 10001 --gid 10001 --no-create-home xudu \
    && install -d -o xudu -g xudu /var/lib/xudu
COPY backend/ backend/
COPY samples/ samples/
COPY --from=web /app/frontend/dist/ frontend/dist/
USER xudu
CMD ["python","-m","uvicorn","backend.app:app","--host","0.0.0.0","--port","8767","--forwarded-allow-ips","172.28.21.2"]
