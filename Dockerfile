FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py .
COPY .claude/ .claude/ 2>/dev/null || true

RUN mkdir -p /root/.workday-mcp

ENV MCP_TRANSPORT=streamable-http
ENV MCP_PORT=8000
ENV WORKDAY_BASE_URL=https://wd115.myworkday.com
ENV WORKDAY_TENANT=lilly

EXPOSE 8000

CMD ["python", "server.py"]
