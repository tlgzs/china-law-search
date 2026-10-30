FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
# requirements.txt 只含 Web 服务需要的最小依赖。
# 要语义检索就改用 requirements-vector.txt（体积会大很多）。
RUN pip install --no-cache-dir -r requirements.txt

COPY server/ ./server/
COPY web/ ./web/

EXPOSE 8001
CMD ["uvicorn", "server.law_search_service:app", "--host", "0.0.0.0", "--port", "8001"]
