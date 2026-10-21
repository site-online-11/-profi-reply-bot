FROM python:3.12-slim
WORKDIR /app
COPY *.py *.txt ./
ENV PYTHONUNBUFFERED=1 DB_PATH=/data/bot.sqlite3 PORT=8080
EXPOSE 8080
CMD ["python", "app.py"]
