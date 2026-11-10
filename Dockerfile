FROM python:3.12-slim
WORKDIR /app
COPY main.py config.example.json ./
COPY tracker ./tracker
COPY web ./web
ENV PYTHONUNBUFFERED=1
EXPOSE 8090
VOLUME ["/app/data"]
CMD ["python", "main.py", "run"]
