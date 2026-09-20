"""
Pydantic request/response models (schemas) live here.

Empty in Stage 2 — no endpoint in this stage has a request/response body
beyond the shared error shape (app/core/exceptions.py) and the health
check's inline dict. Later stages (auth, repositories, deployments,
environment variables, monitoring) will each add a module here, e.g.
app/schemas/deployment.py, matching the JSON shapes locked in
API_CONTRACT.md.
"""
