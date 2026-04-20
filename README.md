# Rivulet Bin

Self-hosted pastebin and link shortener inspired by projects like dogbin and its forks.

## Features

- Create code pastes with syntax highlighting
- Render Markdown pages
- Turn a URL into a short link
- Edit published items with a private edit URL
- Run locally on SQLite or in Docker with Postgres

## Local run

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 15212
```

Then open [http://localhost:15212](http://localhost:15212).

## Docker Compose

```bash
docker compose up --build
```

Then open [http://localhost:15212](http://localhost:15212).

## Branding

You can rebrand the service with environment variables:

- `SITE_NAME`
- `SITE_TAGLINE`
- `SECRET_SALT`
- `MAX_CONTENT_SIZE`
