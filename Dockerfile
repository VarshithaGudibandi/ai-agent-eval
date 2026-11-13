FROM python:3.12-slim
RUN pip install --no-cache-dir uv==0.11.32
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
WORKDIR /srv

COPY pyproject.toml uv.lock .python-version ./
# The SDK and CLI are workspace members, so the lock needs their metadata. The image does not
# install them.
COPY sdk/pyproject.toml ./sdk/
COPY cli/pyproject.toml ./cli/
RUN uv sync --locked --no-dev --no-install-project

COPY app ./app
COPY migrations ./migrations
RUN uv sync --locked --no-dev

RUN useradd --system --uid 10001 app
USER app
EXPOSE 8000
CMD ["uvicorn", "app.api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
