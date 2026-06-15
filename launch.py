"""
CUCM Tools — entry point.
Handles master-password setup/unlock in the terminal before starting the web server.
"""

import getpass
import sys
import threading
import webbrowser

import uvicorn

import database as db

PORT       = 8000
_MIN_PW    = 8
_MAX_TRIES = 3


def _prompt_new_password() -> str:
    """Prompt for a new password with confirmation loop. Returns the password."""
    while True:
        pw = getpass.getpass("  New master password: ")
        if len(pw) < _MIN_PW:
            print(f"  Password must be at least {_MIN_PW} characters. Try again.")
            continue
        confirm = getpass.getpass("  Confirm password:    ")
        if pw != confirm:
            print("  Passwords do not match. Try again.")
            continue
        return pw


def main():
    print()
    print("  CUCM Tools")
    print("  " + "-" * 38)

    if db.is_first_run():
        print()
        print("  First run — set a master password to protect your cluster credentials.")
        print(f"  Minimum {_MIN_PW} characters.")
        print()
        pw = _prompt_new_password()
        db.setup_password(pw)
        print()
        print("  Password set.")

    else:
        print()
        attempts = 0
        while True:
            pw = getpass.getpass("  Master password (or RESET to wipe clusters): ")

            if pw.strip().upper() == "RESET":
                print()
                print("  WARNING: This permanently deletes all saved cluster configurations.")
                confirm = input("  Type CONFIRM to proceed: ").strip()
                if confirm == "CONFIRM":
                    print()
                    new_pw = _prompt_new_password()
                    db.reset_and_setup(new_pw)
                    print()
                    print("  All cluster data cleared. New password set.")
                    break
                else:
                    print("  Cancelled.")
                    print()
                    continue

            if db.unlock(pw):
                print("  Unlocked.")
                break

            attempts += 1
            if attempts >= _MAX_TRIES:
                print("  Incorrect password. Too many failed attempts.")
                print("  Run again and type RESET if you have forgotten your password.")
                sys.exit(1)
            print(f"  Incorrect password. {_MAX_TRIES - attempts} attempt(s) remaining.")

    print()
    print(f"  Server starting at http://localhost:{PORT}")
    print("  Press Ctrl+C to stop.")
    print()

    # Open browser after a short delay so the server is ready
    threading.Thread(
        target=lambda: (
            __import__("time").sleep(1.5),
            webbrowser.open(f"http://localhost:{PORT}"),
        ),
        daemon=True,
    ).start()

    uvicorn.run("main:app", host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
