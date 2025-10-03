FROM python:3.12-slim
WORKDIR /app
COPY init_db.py .
ENV PYTHONUNBUFFERED=1
CMD ["python", "init_db.py"]
