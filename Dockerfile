FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY track-1/starter/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY track-1/starter/ /app/
CMD ["python", "run.py", "--help"]
