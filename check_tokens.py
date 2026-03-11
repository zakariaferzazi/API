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
    code, out, err = run_git("pull", "--rebase", "origin", "HEAD")
    if code != 0:
        # Log but don't abort — could simply be no remote configured locally
        log.warning(f"git pull --rebase exited {code}: {err or out}")
    else:
        log.info(f"Pull: {out or 'already up to date'}")


def git_commit_and_push(message: str) -> None:
    """
    Stage voice_changer.json, commit, then push.
    On push conflict (non-fast-forward), rebases on the remote and retries.
    Aborts the whole push gracefully after MAX_PUSH_RETRIES failures.
    """
    rel_path = VOICE_CHANGER_PATH.relative_to(BASE_DIR).as_posix()

    code, _, err = run_git("add", rel_path)
    if code != 0:
        log.error(f"git add failed: {err}")
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
    log.info("Running ElevenLabs balance check...")

    # Always pull latest before reading to avoid acting on stale data
    git_pull_rebase()

    try:
        voice_data = load_json(VOICE_CHANGER_PATH)
        api_keys_data = load_json(API_KEYS_PATH)
    except FileNotFoundError as e:
        log.error(f"Config file not found: {e}")
        sys.exit(1)

    current_key: str = voice_data.get("elevenlabs_api_key", "")
    all_keys: list = api_keys_data.get("api_keys", [])

    if not all_keys:
        log.error("No API keys found in api_keys.json.")
        sys.exit(1)

    if not current_key:
        log.error("No elevenlabs_api_key set in voice_changer.json.")
        sys.exit(1)

    # Check current key balance
    remaining = get_remaining_characters(current_key)
    log.info(f"Active key (...{current_key[-6:]}): {remaining:,} characters remaining.")

    if remaining >= LOW_BALANCE_THRESHOLD:
        log.info(f"Balance is sufficient (≥ {LOW_BALANCE_THRESHOLD:,}). No switch needed.")
        return

    log.warning(
        f"Balance {remaining:,} is below threshold {LOW_BALANCE_THRESHOLD:,}. "
        "Scanning all keys for a better one..."
    )

    # Find the key with the highest remaining balance
    best_key: str | None = None
    best_remaining: int = remaining

    for key in all_keys:
        if key == current_key:
            continue
        key_remaining = get_remaining_characters(key)
        log.info(f"  Key ...{key[-6:]}: {key_remaining:,} characters remaining.")
        if key_remaining > best_remaining:
            best_remaining = key_remaining
            best_key = key

    if best_key is None:
        log.warning("No key with a higher balance found. Keeping current key.")
        return

    log.info(
        f"Switching from key ...{current_key[-6:]} "
        f"to key ...{best_key[-6:]} ({best_remaining:,} chars remaining)."
    )
    voice_data["elevenlabs_api_key"] = best_key
    save_json(VOICE_CHANGER_PATH, voice_data)
    log.info("voice_changer.json updated.")

    git_commit_and_push(
        f"chore: rotate ElevenLabs key → ...{best_key[-6:]} "
        f"({best_remaining:,} chars) [skip ci]"
    )


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    check_and_switch()
