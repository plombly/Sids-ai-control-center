FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Two worker processes: one slow request never holds up the dashboard's polling.
# umask 0007: files it writes into LAIka's shared folders (app data, uploads)
# stay usable by the laika group on the host (services/laika_user.py).
CMD ["sh", "-c", "umask 0007 && exec uvicorn main:app --host 0.0.0.0 --port 8000 --workers 2"]
