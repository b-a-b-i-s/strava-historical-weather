FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir 'requests>=2.32,<3' 'python-dotenv>=1.0,<2'
COPY strava_weather.py /app/strava_weather.py
ENTRYPOINT ["python", "/app/strava_weather.py"]