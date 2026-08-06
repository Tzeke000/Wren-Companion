# SELF_ASSESSMENT: I am the Spotify link — PKCE OAuth (no client secret ever
# exists on disk or in chat) connecting Iris to Zeke's Spotify account, at his
# request 2026-08-03 ("linking you to my actual Spotify account"). Tokens live
# in state/spotify_auth.json (MUST be gitignored — verified at write time).
# Scopes are read-only: top items, library, playlists, recent plays, follows.
"""
Usage (.venv python):
  spotify_link.py auth <client_id>   -> prints the authorize URL to send Zeke
  spotify_link.py code "<pasted redirect URL or raw code>" -> exchanges + saves tokens
  spotify_link.py test               -> prints display name + top 5 artists (proof of life)
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state" / "spotify_auth.json"
REDIRECT = "http://127.0.0.1:8899/callback"
SCOPES = ("user-top-read user-library-read playlist-read-private "
          "user-read-recently-played user-follow-read")


def _load() -> dict:
    return json.loads(STATE.read_text(encoding="utf-8")) if STATE.is_file() else {}


def _save(d: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(d, indent=2), encoding="utf-8")


def _post_token(data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request("https://accounts.spotify.com/api/token", data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def cmd_auth(client_id: str) -> None:
    verifier = secrets.token_urlsafe(64)[:100]
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    _save({"client_id": client_id, "code_verifier": verifier})
    url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
        "client_id": client_id, "response_type": "code", "redirect_uri": REDIRECT,
        "scope": SCOPES, "code_challenge_method": "S256", "code_challenge": challenge})
    print(url)


def cmd_code(raw: str) -> None:
    st = _load()
    code = raw.strip()
    if "code=" in code:
        code = urllib.parse.parse_qs(urllib.parse.urlparse(code).query)["code"][0]
    tok = _post_token({"grant_type": "authorization_code", "code": code,
                       "redirect_uri": REDIRECT, "client_id": st["client_id"],
                       "code_verifier": st["code_verifier"]})
    st.update({"access_token": tok["access_token"],
               "refresh_token": tok.get("refresh_token"),
               "expires_at": time.time() + tok.get("expires_in", 3600)})
    _save(st)
    print("linked ok — refresh token saved")


def _access() -> str:
    st = _load()
    if time.time() > st.get("expires_at", 0) - 60:
        tok = _post_token({"grant_type": "refresh_token",
                           "refresh_token": st["refresh_token"],
                           "client_id": st["client_id"]})
        st["access_token"] = tok["access_token"]
        if tok.get("refresh_token"):
            st["refresh_token"] = tok["refresh_token"]
        st["expires_at"] = time.time() + tok.get("expires_in", 3600)
        _save(st)
    return st["access_token"]


def api(path: str) -> dict:
    req = urllib.request.Request("https://api.spotify.com/v1" + path,
                                 headers={"Authorization": "Bearer " + _access()})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def cmd_test() -> None:
    me = api("/me")
    print("account:", me.get("display_name"), f"({me.get('id')})")
    top = api("/me/top/artists?limit=5&time_range=medium_term")
    for i, a in enumerate(top.get("items", []), 1):
        print(f"top{i}: {a['name']}  [{', '.join(a.get('genres', [])[:3])}]")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "auth":
        cmd_auth(sys.argv[2])
    elif cmd == "code":
        cmd_code(sys.argv[2])
    elif cmd == "test":
        cmd_test()
    else:
        print(__doc__)
