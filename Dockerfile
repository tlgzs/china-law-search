FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
# 默认只装最小依赖；需要向量检索时取消注释下一行
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" pydantic
# RUN pip install --no-cache-dir -r requirements.txt

COPY server/ ./server/
COPY web/ ./web/

EXPOSE 8001
CMD ["uvicorn", "server.law_search_service:app", "--host", "0.0.0.0", "--port", "8001"]
