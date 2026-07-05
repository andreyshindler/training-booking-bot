FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ bot/

# Keep the SQLite database on a mounted volume so bookings survive
# container rebuilds (see docker-compose.yml).
ENV DB_PATH=/data/bookings.db
RUN mkdir /data
VOLUME /data

CMD ["python", "-m", "bot.main"]
