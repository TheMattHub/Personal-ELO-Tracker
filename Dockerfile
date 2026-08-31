FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV FLASK_APP=app
ENV DATABASE_PATH=/app/instance/elo-club.sqlite3
ENV SECRET_KEY=change-me
ENV HOST=0.0.0.0
ENV PORT=5000

EXPOSE 5000

CMD ["gunicorn", "-b", "0.0.0.0:5000", "app:app"]
