# ---------- build stage ----------
FROM python:3.11-slim AS base

WORKDIR /app
COPY requirements.txt .
RUN apt update
RUN apt install -y git
RUN pip install --no-cache-dir -r requirements.txt

# ---------- runtime image ----------
FROM base AS runtime
WORKDIR /app

# bring in code and env
COPY healthcheck.py .
COPY bot.py .
COPY .env .


# default command
CMD ["python", "bot.py"]
