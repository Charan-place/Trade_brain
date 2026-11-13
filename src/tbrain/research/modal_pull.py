"""Read-only export of one Mongo collection from the production database, run through Modal so no credential touches this machine.

    uv run modal run src/tbrain/research/modal_pull.py --coll features --out data/research/features.json
The connection string comes from the Modal secret `tbrain-secrets`; it is never printed or written.
"""
import modal

app = modal.App("tbrain-research-pull")
image = modal.Image.debian_slim().pip_install("pymongo")
secrets = [modal.Secret.from_name("tbrain-secrets")]
ALLOWED = ("features", "calls", "shadows")


@app.function(image=image, secrets=secrets, timeout=600)
def pull(coll: str) -> str:
    import json
    import os
    from urllib.parse import quote, unquote

    from pymongo import MongoClient

    if coll not in ALLOWED:
        raise ValueError(f"coll must be one of {ALLOWED}")
    uri = os.environ["MONGODB_URI"]
    scheme, rest = uri.split("://", 1)
    userinfo, host = rest.rsplit("@", 1)
    user, _, pwd = userinfo.partition(":")
    uri = f"{scheme}://{quote(unquote(user), safe='')}:{quote(unquote(pwd), safe='')}@{host}"   # percent-encode the userinfo
    db = MongoClient(uri)[os.environ.get("MONGODB_DB", "tbrain")]
    rows = []
    for d in db[coll].find({}, {"owner": 0}):
        d["_id"] = str(d.get("_id"))
        d["_coll"] = coll
        rows.append(d)
    return json.dumps(rows, default=str)


@app.local_entrypoint()
def main(coll: str = "features", out: str = "data/research/export.json"):
    import pathlib
    pathlib.Path(out).parent.mkdir(parents=True, exist_ok=True)
    text = pull.remote(coll)
    pathlib.Path(out).write_text(text)
    print(f"wrote {out} ({len(text):,} bytes)")
