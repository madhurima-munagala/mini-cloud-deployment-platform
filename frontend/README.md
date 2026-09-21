# CloudForge Frontend

React + Vite frontend for the Mini Cloud Deployment Platform backend in this repository.

## Run locally

1. Start the FastAPI backend on `http://localhost:8000`.
2. From this folder run:

```bash
npm install
npm run dev
```

3. Open `http://localhost:5173`.

The Vite development proxy forwards `/api/*` requests to the FastAPI server, so browser cookies work without changing the backend for local development.

## Production

Set `VITE_API_BASE_URL` to the public FastAPI origin before building. The backend should have:

- `FRONTEND_BASE_URL` set to the deployed frontend URL.
- `CORS_ALLOWED_ORIGINS` containing the frontend origin.
- Production HTTPS/session settings configured as required by the backend.

## Included screens

- GitHub OAuth login
- Overview dashboard
- GitHub repository browser and search
- Repository details
- Repository environment variable management
- Deployment creation with branch and environment overrides
- Deployment history with status filters
- Deployment detail page with live polling, build logs, live URL and runtime CPU/memory metrics
- Responsive mobile navigation
