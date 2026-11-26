# syntax=docker/dockerfile:1
FROM python:3.14-slim
WORKDIR /app
COPY pyproject.toml ./
RUN pip install --no-cache-dir "mistralai-workflows[mistralai]>=3.17.1" "pydantic>=2.14.1" "requests>=2.32"
COPY . .
CMD ["python", "worker.py"]
