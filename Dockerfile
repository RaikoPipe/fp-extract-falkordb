FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml .
COPY src/ src/
COPY docprep.yaml .

RUN pip install --no-cache-dir -e ".[chainlit]"

COPY .chainlit/ .chainlit/
COPY chainlit*.md .

EXPOSE 8000

CMD ["chainlit", "run", "src/falkordb_harness/chainlit_app.py", "--host", "0.0.0.0", "--port", "8000"]
