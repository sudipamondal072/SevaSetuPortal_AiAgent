FROM python:3.11-slim

WORKDIR /app
COPY requirements-mcp.txt .
RUN pip install --no-cache-dir -r requirements-mcp.txt

COPY mcp_servers/ /app/mcp_servers/
COPY evals/ /app/evals/

EXPOSE 8001 8002
