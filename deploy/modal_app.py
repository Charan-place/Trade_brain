"""Trade Brain on Modal (https://modal.com) - signal-only, never places orders.

Deploy:   modal deploy deploy/modal_app.py
Run now:  modal run deploy/modal_app.py::record_day        (manual start, e.g. after a crash)
Logs:     Modal dashboard -> Apps -> trade-brain

Pieces
- web          FastAPI app: Google sign-in, "Connect Upstox" (daily), live dashboard. Stable *.modal.run URL.
- record_day   the brain for one trading day (09:00-15:35 IST). Started right after you connect Upstox, and by
               a 09:00 IST weekday schedule as a fallback. Exits at once if Upstox isn't connected today or if
               another recorder is already running.
- Storage      Modal Volume "tbrain-data" -> raw ticks/chain (Parquet), calls/shadows (jsonl), sessions.
               MongoDB Atlas -> calls, shadows, minute features, daily summaries, users, encrypted Upstox tokens.
               Modal Dict "tbrain-state" -> the latest brain snapshot for the dashboard.
- Secrets      Modal secret "tbrain-secrets" (see README "Deploy on Modal").
"""
from pathlib import Path

import modal

ROOT = Path(__file__).resolve().parents[1]
REGION = "ap-south"          # Mumbai: close to Upstox. Region pricing multiplier applies (see Modal pricing).

app = modal.App("trade-brain")

image = (
    modal.Image.debian_slim(python_version="3.12")
    .uv_pip_install(requirements=[str(ROOT / "deploy" / "requirements.txt")])   # pinned: uv export of uv.lock
    .env({"TBRAIN_DATA_DIR": "/data", "TBRAIN_SECRETS_DIR": "/data/.secrets", "TBRAIN_FLUSH_S": "60",
          "PYTHONPATH": "/root/src", "TZ": "Asia/Kolkata"})
    .add_local_dir(ROOT / "src", remote_path="/root/src", ignore=["**/__pycache__/**"])
)

secrets = [modal.Secret.from_name("tbrain-secrets")]
data = modal.Volume.from_name("tbrain-data", create_if_missing=True, version=2)
state = modal.Dict.from_name("tbrain-state", create_if_missing=True)


@app.function(image=image, secrets=secrets, volumes={"/data": data}, region=REGION,
              cpu=1.0, memory=2048, timeout=8 * 3600,
              schedule=modal.Cron("0 9 * * 1-5", timezone="Asia/Kolkata"))
def record_day() -> str:
    from tbrain.config import Settings
    from tbrain.web.hosted import run_hosted_day
    why = run_hosted_day(Settings.load(), state, log=lambda *a: print(*a, flush=True))
    print(f"[hosted] record_day finished: {why}", flush=True)
    data.commit()
    return why


@app.function(image=image, secrets=secrets, region=REGION, scaledown_window=300)
@modal.concurrent(max_inputs=50)
@modal.asgi_app()
def web():
    from tbrain.config import Settings
    from tbrain.web.app import create_app
    from tbrain.web.state import DictStateSource

    s = Settings.load()

    def start_today(email: str) -> None:
        if email == s.owner_email:
            record_day.spawn()

    return create_app(s, state_source=DictStateSource(state), on_upstox_connected=start_today)
