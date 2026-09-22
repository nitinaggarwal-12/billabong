FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN python3 seed_urology.py
ENV PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=3)"
CMD ["python3","app.py"]
