# Deploy (pick one)

## Render (simplest, free tier)
1. Push this folder to a **public** GitHub repo.
2. render.com > New > Web Service > connect the repo.
3. Runtime: Python 3. Build command: `pip install -r requirements.txt`
   Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
4. Deploy, then open the URL in an incognito window.

Notes: the free tier sleeps after inactivity (first load can take about 50 seconds), and the SQLite file resets on redeploy. That is fine: the sample project is re-created at startup.
Open the link yourself shortly before the deadline so it is warm for judges.

## Hugging Face Spaces (Docker)
1. New Space > SDK: Docker > blank.
2. Upload the repo contents. Add this block at the very top of the Space's README.md:
   ```
   ---
   title: RelayTrace
   sdk: docker
   app_port: 7860
   ---
   ```
3. The included Dockerfile listens on 7860.

## Before submitting
- Open the live link in an incognito window (no sign-in prompt).
- Click: Load demo > resolve conflict > lock > switch to Successor.
- Repo must be public.
