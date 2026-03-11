import json
import logging
import subprocess
import sys
import requests
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
VOICE_CHANGER_PATH = BASE_DIR / "voice_changer.json"
API_KEYS_PATH = BASE_DIR / "api_keys.json"
ELEVENLABS_USER_URL = "https://api.elevenlabs.io/v1/user"
LOW_BALANCE_THRESHOLD = 500   # switch key when remaining chars fall below this
MAX_PUSH_RETRIES = 3

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Git helpers ───────────────────────────────────────────────────────────────
def run_git(*args: str) -> tuple[int, str, str]:
    """Run a git command; returns (returncode, stdout, stderr)."""
    result = subprocess.run(
        ["git", *args],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def git_pull_rebase() -> None:
    """Pull latest remote changes with rebase to avoid stale reads."""
    log.info("Pulling latest remote changes (--rebase)...")
    # Stash any tracked-file changes (e.g. files written by CI) so rebase can proceed cleanly
    _, stash_out, _ = run_git("stash")
    stashed = "No local changes to save" not in stash_out

    code, out, err = run_git("pull", "--rebase", "origin", "HEAD")
    if code != 0:
        log.warning(f"git pull --rebase exited {code}: {err or out}")
    else:
        log.info(f"Pull: {out or 'already up to date'}")

    if stashed:
        pop_code, _, pop_err = run_git("stash", "pop")
        if pop_code != 0:
            log.warning(f"git stash pop failed: {pop_err}")


def git_commit_and_push(message: str, *rel_paths: str) -> None:
    """
    Stage one or more files, commit, then push.
    On push conflict (non-fast-forward), rebases on the remote and retries.
    Aborts the whole push gracefully after MAX_PUSH_RETRIES failures.
    """
    if not rel_paths:
        rel_paths = (VOICE_CHANGER_PATH.relative_to(BASE_DIR).as_posix(),)

    for rel_path in rel_paths:
        code, _, err = run_git("add", rel_path)
        if code != 0:
            log.error(f"git add {rel_path} failed: {err}")
            return

    # Nothing staged? — another run already committed the same change
    code, _, _ = run_git("diff", "--cached", "--quiet")
    if code == 0:
        log.info("No staged changes — nothing to commit.")
        return

    code, out, err = run_git("commit", "-m", message)
    if code != 0:
        log.error(f"git commit failed: {err}")
        return
    log.info(f"Committed: {out}")

    for attempt in range(1, MAX_PUSH_RETRIES + 1):
        code, out, err = run_git("push")
        if code == 0:
            log.info(f"Pushed successfully (attempt {attempt}/{MAX_PUSH_RETRIES}).")
            return

        log.warning(f"Push attempt {attempt}/{MAX_PUSH_RETRIES} failed: {err or out}")

        if attempt < MAX_PUSH_RETRIES:
            log.info("Remote has new commits — rebasing before retry...")
            rb_code, _, rb_err = run_git("pull", "--rebase", "origin", "HEAD")
            if rb_code != 0:
                log.error(f"Rebase failed: {rb_err}. Aborting rebase.")
                run_git("rebase", "--abort")
                return

    log.error(f"Push failed after {MAX_PUSH_RETRIES} attempts. Manual intervention required.")


# ── JSON helpers ──────────────────────────────────────────────────────────────
def load_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


# ── ElevenLabs API ────────────────────────────────────────────────────────────
def get_remaining_characters(api_key: str) -> int:
    """
    Returns remaining characters for the given API key.
    Returns -1 if the key is invalid or an error occurred.
    """
    try:
        response = requests.get(
            ELEVENLABS_USER_URL,
            headers={"xi-api-key": api_key},
            timeout=15,
        )
        response.raise_for_status()
        sub = response.json().get("subscription", {})
        return sub.get("character_limit", 0) - sub.get("character_count", 0)
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code
        if status == 401:
            log.warning(f"Key ...{api_key[-6:]} is invalid (401 Unauthorized).")
        else:
            log.warning(f"HTTP {status} for key ...{api_key[-6:]}: {e.response.text}")
        return -1
    except Exception as e:
        log.warning(f"Error checking key ...{api_key[-6:]}: {e}")
        return -1


# ── Core logic ────────────────────────────────────────────────────────────────
def check_and_switch() -> None:
    log.info("─" * 60)
    log.info("Running ElevenLabs balance check..")

    # Always pull latest before reading to avoid acting on stale data test
    git_pull_rebase()

    try:
        voice_data = load_json(VOICE_CHANGER_PATH)
        api_keys_data = load_json(API_KEYS_PATH)
    except FileNotFoundError as e:
        log.error(f"Config file not found: {e}")
        sys.exit(1)

    current_key: str = voice_data.get("elevenlabs_api_key", "")
    raw_entries: list = api_keys_data.get("api_keys", [])

    if not raw_entries:
        log.error("No API keys found in api_keys.json.")
        sys.exit(1)

    if not current_key:
        log.error("No elevenlabs_api_key set in voice_changer.json.")
        sys.exit(1)

    # Normalise entries: accept both plain strings and {"key": ..., "value": ...} objects
    entries: list[dict] = [
        e if isinstance(e, dict) else {"key": e, "value": -1}
        for e in raw_entries
    ]

    # ── Check ALL keys and update their value ────────────────────────────────
    new_key: str | None = None
    current_remaining: int = -1

    for entry in entries:
        k = entry["key"]
        bal = get_remaining_characters(k)
        entry["value"] = bal
        label = "(active) " if k == current_key else ""
        log.info(f"  {label}Key ...{k[-6:]}: {bal:,} characters remaining.")
        if k == current_key:
            current_remaining = bal

    # Save updated balances to api_keys.json every run
    api_keys_data["api_keys"] = entries
    save_json(API_KEYS_PATH, api_keys_data)
    log.info("api_keys.json balances updated.")

    api_keys_rel = API_KEYS_PATH.relative_to(BASE_DIR).as_posix()
    voice_rel    = VOICE_CHANGER_PATH.relative_to(BASE_DIR).as_posix()

    if current_remaining >= LOW_BALANCE_THRESHOLD:
        log.info(f"Active key balance {current_remaining:,} is sufficient (≥ {LOW_BALANCE_THRESHOLD:,}). No switch needed.")
        git_commit_and_push("chore: update API key balances [skip ci]", api_keys_rel)
        return

    log.warning(
        f"Active key balance {current_remaining:,} is below threshold {LOW_BALANCE_THRESHOLD:,}. "
        "Switching to first sufficient key..."
    )

    for entry in entries:
        if entry["key"] == current_key:
            continue
        if entry["value"] >= LOW_BALANCE_THRESHOLD:
            new_key = entry["key"]
            log.info(
                f"Switching from ...{current_key[-6:]} "
                f"to ...{new_key[-6:]} ({entry['value']:,} chars remaining)."
            )
            voice_data["elevenlabs_api_key"] = new_key
            save_json(VOICE_CHANGER_PATH, voice_data)
            log.info("voice_changer.json updated.")
            git_commit_and_push(
                f"chore: rotate ElevenLabs key → ...{new_key[-6:]} "
                f"({entry['value']:,} chars), update balances [skip ci]",
                voice_rel,
                api_keys_rel,
            )
            return

    log.warning("No key with sufficient balance found. Keeping current key.")
    git_commit_and_push("chore: update API key balances [skip ci]", api_keys_rel)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    check_and_switch()
