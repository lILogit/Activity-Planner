cd "/Users/jirikouba/Documents/Activity Planner"
source .venv/bin/activate
uvicorn app.main:app --reload --port 8000

Or without activating the venv:

cd "/Users/jirikouba/Documents/Activity Planner"
.venv/bin/uvicorn app.main:app --reload --port 8000

Optional, for the Telegram webhook to work locally:

ngrok http --domain=<your-domain> 8000

Then visit:
- http://localhost:8000/dashboard — main dashboard
- http://localhost:8000/tables — admin CRUD editor
- http://localhost:8000/health — health check