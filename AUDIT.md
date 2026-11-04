# MoodleUpdater audit

Audit of commit `869d2a1`. Line numbers refer to that commit. Every finding has a
**Status** line that records whether it was fixed (and in which commit) or skipped (and why).

**Result:** all 5 critical, 12 high, 11 medium and 10 low findings were addressed. H11 is opt-in only,
and L2 is partly done. See the status lines for details.

Severity scale:

- **Critical**: can take a production site down, lose data, or make unattended runs unusable.
- **High**: wrong or unsafe behavior in common setups, or a security issue.
- **Medium**: wrong behavior in less common setups, or robustness and hardening gaps.
- **Low**: code quality, cosmetics, docs.

---

## Critical

### C1. A failed step does not stop the steps that depend on it
- **Where:** `modules/moodle_backup.py:113-124` (`dir_backup` only logs), `:155-171` (`db_dump` logs and returns), `:183-205` (`git_clone` goes on after a failed `rmtree`, `git clone` or `git checkout`), `:277-281`, `moodle_updater.py:367-425`
- **Problem:** No step reports success or failure. `dir_backup_and_git_clone` deletes the live Moodle code directory even when the backup that was meant to protect it failed. The CLI upgrade, which changes the database schema and cannot be undone, runs even after the DB dump or the clone failed.
- **Impact:** A failed backup followed by a failed clone leaves the site without code and without a backup. Upgrading without a valid dump removes the only way back.
- **Fix:** Have every step return `True` or `False`. Skip the clone when the backup in the same lane failed, and skip plugin restore and the CLI upgrade when any earlier step failed.
- **Status:** Fixed in `222386f`.

### C2. `git_clone` deletes the live code before it knows the new clone works
- **Where:** `modules/moodle_backup.py:183-205`, `:266`
- **Problem:** The existing directory is removed with `rmtree` first, then the repository is cloned into its place. If the clone fails (network, auth, disk), `config.php` is then written into a directory that does not exist. That raises an exception, and the `config.php` content that was only held in memory is lost.
- **Impact:** A network blip means the site is down with no code and no `config.php`.
- **Fix:** Clone, check out, sync submodules and write `config.php` into a staging directory next to the live one. Swap the two directories with `rename` only after every step has succeeded.
- **Status:** Fixed in `b89b09a`.

### C3. Exceptions in worker threads are swallowed
- **Where:** `moodle_updater.py:370-402`
- **Problem:** An exception inside a `threading.Thread` target goes to stderr as a traceback, but it is not logged and the main thread never finds out. Two real examples are `ValueError` from `max([])` in `git_clone` and `FileNotFoundError` when writing `config.php`. The main thread then starts the webserver and runs the CLI upgrade.
- **Impact:** Failures are invisible in the log file, and later steps run against a broken tree.
- **Fix:** Run threads through a wrapper that catches exceptions, logs them with `logging.exception`, and records the failure.
- **Status:** Fixed in `222386f` (`modules/steps.py`).

### C4. The webserver and maintenance mode are not restored after a failure
- **Where:** `moodle_updater.py:359-420`, `modules/moodle_backup.py:444-524`
- **Problem:** Nothing between stopping and starting the webserver is wrapped in `try/finally`. An exception, a `sys.exit` or Ctrl-C leaves the webserver stopped. In `moodle_cli_upgrade`, maintenance mode is turned off inside the same `try` block that turned it on, so an exception after enabling it (for example `Popen` failing) leaves the site in maintenance mode.
- **Impact:** The site stays down after a failure that could otherwise be recovered from.
- **Fix:** Start the webserver and disable maintenance mode in `finally` blocks.
- **Status:** Fixed in `29757b2`.

### C5. Non-interactive mode still prompts and crashes under cron
- **Where:** `modules/application_setup.py:46-54` (self-update prompt), `:109-127` (`confirm` does not handle `EOFError`), `modules/moodle_backup.py:588-594` (system-check prompt), `moodle_updater.py:199-200, 230-231` (DB password prompts)
- **Problem:** With `auto_update_script = False`, the "Pull MoodleUpdater from GitHub?" prompt runs before command-line arguments are looked at, so `--non-interactive` (and even `--help`) still prompts. Under cron, stdin is `/dev/null`, `input()` raises `EOFError`, and the script exits with a traceback. Failed system checks without `--force-continue` also prompt, and so does `read_db_from_config = False`. Reproduced with `python3 moodle_updater.py --help </dev/null`, which gives `EOFError`.
- **Impact:** The automated mode that the README advertises for cron does not work with the default config.
- **Fix:** Parse arguments before setup. Never call `input()` in non-interactive mode: skip the self-update prompt, use the timeout defaults for system checks (stop before the upgrade, continue after it), and read the DB password from `MYSQL_PWD` or fail with a clear message. Make `confirm()` handle `EOFError`.
- **Status:** Fixed in `b89f389` (no self-update prompt in non-interactive mode) and `1a9fce0` (system checks, DB password, `confirm()` on closed stdin).

---

## High

### H1. `sys.argv` is parsed by hand, so `--help` and `--dry-run` are broken and typos are ignored
- **Where:** `moodle_updater.py:45-70` and every `"--x" in sys.argv` check; `modules/application_setup.py:41-44`
- **Problem:**
  - `--help` only runs after the config has loaded, the self-update prompt and the root check. A non-root user cannot see the help at all.
  - `--dry-run` is applied after the root check, so a non-root dry run is impossible: the script exits with "must be run as root".
  - Unknown or misspelled flags are ignored silently. `--dryrun` or `--dry_run` gives a **real** run.
- **Impact:** A typo in the safety flag turns a simulation into a real update.
- **Fix:** Use `argparse` (standard library) before anything else runs. Unknown flags become an error, and `--help` and `--dry-run` take effect first.
- **Status:** Fixed in `b89f389`.

### H2. Boolean config values only count when they are exactly `True`
- **Where:** `modules/application_setup.py:48-49, 70`; `moodle_updater.py:193`
- **Problem:** `dry_run = true`, `yes` or `1` all count as **False**. The same applies to `auto_update_script` and `read_db_from_config`.
- **Impact:** A user who writes `dry_run = true` gets a real run.
- **Fix:** Use `ConfigParser.getboolean`, which accepts true/false, yes/no, on/off and 1/0, and stop with an error on any other value.
- **Status:** Fixed in `f5ec4d6`.

### H3. The DB password is passed on the command line for the connection check
- **Where:** `moodle_updater.py:221, 236` (`-p{dbpass}`)
- **Problem:** The dump already uses `MYSQL_PWD`, but the `mariadb-show`/`mysqlshow` check still puts the password in argv.
- **Impact:** Any local user can read it with `ps` or from `/proc/<pid>/cmdline`.
- **Fix:** Pass the password through `MYSQL_PWD`, the same way the dump does.
- **Status:** Fixed in `062a210`.

### H4. The DB connection retry never actually retries
- **Where:** `moodle_updater.py:228-242`
- **Problem:** `while not dbpass.strip()` never runs, because the password that just failed is not empty. The check is repeated with the same password and fails again. If `config.php` could not be parsed, `dbpass` is `None`, which gives `-pNone` (or a `TypeError` once the password is in the environment).
- **Impact:** A mistyped password cannot be corrected, and a parse failure produces a confusing error.
- **Fix:** Use a real loop. Interactive mode prompts for a new password up to 3 times; non-interactive mode fails right away. Check for a missing name, user or password before connecting.
- **Status:** Fixed in `062a210`. Password prompts now also use `getpass`, so the input is not echoed.

### H5. `--restore-submodules` crashes or restores from the wrong backup
- **Where:** `modules/moodle_backup.py:238-241`, `moodle_updater.py:414`
- **Problem:**
  - With a full backup, `max(glob(...), "moodle", key=os.path.getmtime)` passes `"moodle"` as a second argument to `max`, so it always raises `TypeError`.
  - When the dump is not selected, the sequential code path never passes `full_backup` to `git_clone`. It then looks for a *partial* backup, which raises `ValueError` from `max([])` or quietly picks an older, stale partial backup.
  - Both happen *after* the old code has been deleted. Reproduced: `--dry-run --directory-backup --git-clone --full-backup --restore-submodules` gives `ValueError: max() iterable argument is empty`.
  - Plugin restore (`:310-322`) also picks "the newest backup on disk" instead of the one made in this run.
- **Impact:** The site is left without submodules and without `config.php`, or is restored from old data.
- **Fix:** `dir_backup` records the folder it created. Submodule and plugin restore use that folder instead of searching with `glob`.
- **Status:** Fixed in `10e06bd`. `222386f` already made the backup+clone lane pass `full_backup` consistently.

### H6. Backups and dumps are readable by every local user
- **Where:** `modules/moodle_backup.py:95-114, 160`; `moodle_updater.py:136-138`
- **Problem:** The dump is created with the default umask, and the backup folder is created by rsync with the source's modes. On the audited server the 13 GB dump is `0644` and the backup directory is `0755`. The backup contains `config.php` with the DB password, and the dump contains every user record and password hash.
- **Impact:** Any local user can read the full database and the DB credentials.
- **Fix:** Create the dump with `0600` and the backup root with `0700`, and set the lock file to `0600`.
- **Status:** Fixed in `6d6c2d3`. Existing dumps and backups on disk keep their old modes; tighten them once by hand (see summary).

### H7. The directory backup with `rsync -r` is not a faithful copy
- **Where:** `modules/moodle_backup.py:114`
- **Problem:** `-r` without `-l -p -t -o -g` skips symlinks ("skipping non-regular file") and does not keep permissions, ownership or timestamps.
- **Impact:** A restore from the backup gives a different tree: links are missing and modes and owners are wrong, which matters most for `moodledata`.
- **Fix:** Use `rsync -a`.
- **Status:** Fixed in `fee9404`.

### H8. A Moodle path entered interactively is ignored
- **Where:** `moodle_updater.py:125-130` vs `:173-177`
- **Problem:** `MoodleBackupManager` is created with the configured `path` *before* the "Is this the correct Moodle directory?" prompt. Answering "n" and entering a different path changes only local variables, and `configphppath` is never updated.
- **Impact:** After the user rejects the configured path, the script still backs it up, deletes it and re-clones it.
- **Fix:** Ask for the path first, then build the manager and the derived paths from the answer.
- **Status:** Fixed in `af34e61`.

### H9. `configphp` is unbound when the user skips the `config.php` restore
- **Where:** `moodle_updater.py:268-277`
- **Problem:** Answering "n" and pressing Enter to skip leaves `configphp` undefined. A `NameError` follows when `git_clone` is called, after the webserver has been stopped and the backup has run.
- **Impact:** The script crashes with the site down.
- **Fix:** Default to `None` and skip writing `config.php` when it is `None`.
- **Status:** Fixed in `b89b09a`.

### H10. Self-update has unbound variables, locale-dependent output parsing, and supply-chain risk
- **Where:** `modules/git_manager.py:278-325`; `modules/application_setup.py:46-54`
- **Problem:**
  - After a failed git command, `branch_result`, `current_commit_result`, `status_result` and `pull_result` are still used. The blanket `except` catches the resulting `UnboundLocalError` and logs a misleading message.
  - "Up to date" is detected by matching the English string `Already up to date.`. Under a non-English locale every run counts as an update, so `os.execv` restarts the script, which pulls again and restarts again, forever.
  - Security: as root, the script runs `git pull` and immediately executes whatever comes back, with no verification. On the audited server the checkout is owned by a non-root user (uid 1001) but run with `sudo`, which is a local privilege escalation path.
- **Fix:** Return on every failed git step, and detect updates by comparing `HEAD` before and after the pull. Warn when running as root from a checkout that non-root users can write to. Document the risk. Signed-commit verification goes under future work.
- **Status:** Fixed in `84e57e9` (unbound variables, locale-independent detection, warning for writable installs, README security note). Verifying signed commits is listed under future work, because the brief excludes changing how self-update works.

### H11. Moodle PHP CLI scripts run as root
- **Where:** `modules/moodle_backup.py:448, 541, 561`
- **Problem:** `upgrade.php`, `checks.php` and `maintenance.php`, together with every third-party plugin's upgrade code, run as root. Moodle's documentation recommends running the CLI as the web server user.
- **Impact:** A malicious or buggy plugin runs with root privileges, and root-owned files can end up in `moodledata`.
- **Fix:** Add an optional `php_cli_user` setting. When it is set and the script runs as root, the PHP commands run through `runuser -u <user> --`. It stays empty by default so existing installs keep working.
- **Status:** Fixed in `a0563bc` as an opt-in setting. **Not enabled by default:** switching existing installs could break them if `moodledata` contains root-owned files, so the default (run as root) is under future work.

### H12. The exit code is 0 even when steps failed
- **Where:** `moodle_updater.py:485-491`
- **Problem:** The script exits 0 after a failed backup, dump, clone or upgrade.
- **Impact:** Cron, CI and wrapper scripts cannot detect a failed run.
- **Fix:** Exit with 1 when any selected step failed.
- **Status:** Fixed in `222386f`.

---

## Medium

### M1. `--restart-database` restarts every database-like service
- **Where:** `modules/service_manager.py:97-118`
- **Problem:** It restarts PostgreSQL, Redis, MongoDB and MSSQL when they are installed, although the dump only supports MySQL/MariaDB. Restarting Redis can wipe Moodle sessions and caches.
- **Fix:** Restart only MySQL/MariaDB.
- **Status:** Fixed in `be933bc`.

### M2. Maintenance mode is toggled through `shell=True`
- **Where:** `modules/moodle_backup.py:535-541`
- **Problem:** A command string built from config values is run through the shell. Spaces or shell metacharacters in `path` break it or get executed.
- **Fix:** Pass an argument list.
- **Status:** Fixed in `a0563bc`.

### M3. Backup and dump locations are not checked against the backup source
- **Where:** `modules/moodle_backup.py:93-114`, `moodle_updater.py:119-123, 190-192`
- **Problem:** The README's own example config uses `path = /var/www/moodle` and `folder_backup_path = /var/www/moodle`. A full backup then copies earlier backups and the dump in progress into the new backup, so every run grows. A backup or dump directory inside the Moodle code directory would also be removed by `git_clone`.
- **Fix:** Exclude backup and dump locations that lie inside the full-backup source, and refuse to run when they lie inside the Moodle code directory.
- **Status:** Fixed in `2f00417`. `7d1f977` adds the same check for the lock folder on clone-only runs.

### M4. A failed dump leaves a partial `.sql` file
- **Where:** `modules/moodle_backup.py:160-171`
- **Problem:** A truncated dump stays on disk under a normal-looking name, and a missing `db_dump_path` is not created.
- **Fix:** Delete the partial file on failure and create the directory.
- **Status:** Fixed in `24d0d2e`.

### M5. The `config.php` parser only matches non-empty single-quoted values and ignores `dbhost`
- **Where:** `modules/config_manager.py:56-60`
- **Problem:** `$CFG->dbpass = "secret";` or an empty password gives `None`. `$CFG->dbhost` is never read, so the dump always connects to the local server. That fails safely at the connection check, but it means remote databases are not supported.
- **Fix:** Accept both quote styles and empty values, read `dbhost`, and pass `-h` to the client tools.
- **Status:** Fixed in `2255127`. Adds the optional `db_host` setting for `read_db_from_config = False`.

### M6. `SystemMonitor` hardcodes `mysql` and crashes silently
- **Where:** `modules/system_monitor.py:40, 54-55, 77, 97`
- **Problem:** It ignores `resolve_db_tool`, so on a MariaDB-only Debian 13 system `FileNotFoundError` kills the progress thread without a word. A failed size query returns `1`, which produces nonsense percentages, and a size of 0 raises `ZeroDivisionError` inside the thread.
- **Fix:** Use `resolve_db_tool`. When the size is unknown, skip the percentage and estimate, and catch errors inside the thread.
- **Status:** Fixed in `d39f264`. The client helpers moved to `modules/db_tools.py`.

### M7. The CLI upgrade output parsing is duplicated
- **Where:** `modules/moodle_backup.py:458-506`
- **Problem:** The same line-classification block is copied for the read loop and the drain loop, and the copies have already started to drift.
- **Fix:** Move the classification into one helper and unit-test it.
- **Status:** Fixed in `5d0e1da`.

### M8. Building the remote version URL mangles some repository URLs
- **Where:** `modules/moodle_version.py:46, 53`
- **Problem:** `.replace('.git', '')` removes `.git` anywhere in the URL, so `https://code.gitlab.example/x/moodle.git` becomes `https://codelab.example/x/moodle`.
- **Fix:** Strip only a trailing `.git` (or `/`).
- **Status:** Fixed in `79feded`.

### M9. Opening the lock file truncates it before the lock is taken
- **Where:** `moodle_updater.py:137-150`
- **Problem:** `open(lock_path, "w")` erases the PID of the running instance before `flock` is even tried.
- **Fix:** Open with `O_RDWR|O_CREAT` and mode `0600`, and truncate only after the lock has been taken.
- **Status:** Fixed in `6d6c2d3`.

### M10. The backup size calculation can crash and is slow
- **Where:** `modules/moodle_backup.py:115-119`
- **Problem:** `os.path.getsize` follows symlinks, so a dangling link raises an exception. With `rsync -a` (H7), links are now kept. It also walks millions of `moodledata` files in Python.
- **Fix:** Use `lstat` and ignore `OSError`.
- **Status:** Fixed in `fee9404`.

### M11. The README is out of sync with the code
- **Where:** `README.md`
- **Problem:**
  - `python3-requests` and `python3-apt` are missing from the install command.
  - `mysql-client` cannot be installed on Debian 12/13.
  - The branch is still `MOODLE_500_STABLE`; the template uses `502`.
  - "Blank means current working directory" is wrong: it means the script directory.
  - `--restart-webserver` actually stops the webserver for the whole run.
  - Exit codes are not documented.
  - The "Highlights" section at the end is leftover generated text.
  - The README refers to the user as "he".
- **Fix:** Rewrite the affected sections.
- **Status:** Fixed in `60b4d35`. Each behavior change also updated the README in its own commit.

---

## Low

### L1. Dead code, unused imports, duplicate constants, small slips
- **Where:**
  - unused `RotatingFileHandler` import: `moodle_updater.py:12`
  - dead `result = "returncode=0"`: `:216`
  - repeated condition: `not non_interactive ... not non_interactive`: `:296`
  - builtin `exit()` instead of `sys.exit()`: `:295, 306, 327, 352`
  - f-strings without placeholders
  - `SEPARATOR` defined in four files
- **Fix:** Clean up, and enforce with ruff.
- **Status:** Fixed in `a0a10b6`. Parts were already removed in `b89f389`, `062a210` and `222386f`.

### L2. `main()` is 470 lines and repeats the flag/prompt pattern 12 times
- **Fix:** Add one `resolve_option()` helper and split `main()` into phases.
- **Status:** Partly fixed: `resolve_option` (`b89f389`), `acquire_instance_lock` (`6d6c2d3`), `ensure_db_connection` (`062a210`), `read_db_settings` and `log_run_summary` (`53a86a7`). `main()` went from ~470 to ~300 lines. **Skipped for the rest:** the interactive git-clone preparation is still inline. Splitting it means rewriting the prompt flow, and `main()` has no tests yet, so the risk outweighs the benefit for now.

### L3. The root-check error message names the wrong file
- **Where:** `modules/application_setup.py:43`
- **Problem:** `__file__` points to `modules/application_setup.py`, not `moodle_updater.py`.
- **Status:** Fixed in `b89f389`.

### L4. `ServiceManager` loads the apt cache eagerly and can raise `KeyError`
- **Where:** `modules/service_manager.py:1, 79, 85-88`
- **Problem:** `apt.Cache()` loads on every run, even when no service is touched. `cache["apache2"]` raises `KeyError` for a package apt does not know. `import apt` at module level prevents importing on non-Debian systems and in CI. "stop" + "ed" produces "stoped".
- **Status:** Fixed in `be933bc`.

### L5. Memory monitor polling interval is overwritten
- **Where:** `modules/system_monitor.py:197-199`
- **Problem:** The final `else: sleep_time = 5` overrides the shorter intervals set by the critical and warning branches. `time.sleep` also delays stopping by up to 5 s.
- **Fix:** Pick the shortest interval that applies, and wait on `stop_event.wait()`.
- **Status:** Fixed in `d39f264`.

### L6. "Time saved with multithreading" is wrong
- **Where:** `moodle_updater.py:462-463`
- **Problem:** The audited server's log shows `-121 seconds`. The calculation adds the CLI upgrade, which never runs in parallel, ignores plugin restore, and treats the backup and clone as if they ran in parallel when they run one after the other.
- **Fix:** Compare the wall time of the parallel phase with the sum of the steps run in parallel.
- **Status:** Fixed in `3f0cd75`.

### L7. `e.stderr` is logged but never captured
- **Where:** `modules/moodle_backup.py:122, 201, 205, 214, 229, 260, 271`, `modules/service_manager.py:129`
- **Problem:** The log shows `None` instead of the error.
- **Fix:** Log the exit code. The command's own stderr already goes to the console.
- **Status:** Fixed in `ad21b18`.

### L8. The config difference check reports every customized value
- **Where:** `modules/config_manager.py:87-93`
- **Problem:** It compares (key, value) pairs, so every setting the user customized shows up as both "Added" and "Removed" on every run.
- **Fix:** Compare keys only.
- **Status:** Fixed in `b42d064`.

### L9. A missing `config.ini` is only noticed after logging setup and the self-update prompt
- **Where:** `modules/application_setup.py:25-36`
- **Fix:** Check that the file exists first.
- **Status:** Fixed in `b42d064`.

### L10. No tests and no CI
- **Fix:** Add pytest tests for the logic that is touched, and a GitHub Actions workflow running ruff and pytest.
- **Status:** Fixed in `aab201a` (CI). The pytest tests (104) were added together with each fix.

---

## Found while fixing

### X1. Submodule restore put submodules at the wrong path when their parent was missing
- **Where:** `modules/moodle_backup.py`, the `cp -r <backup>/<sub> <clone>/<parent>` call
- **Problem:** When `<parent>` did not exist in the new clone, `cp` created `<parent>` itself as a copy of the submodule. A new test found this.
- **Status:** Fixed in `10e06bd`.

### X2. DB password prompts echoed the password
- **Where:** `moodle_updater.py`
- **Problem:** `input()` showed the password on screen and in terminal scrollback.
- **Status:** Fixed in `062a210` (`getpass`).

### X3. `--restore-submodules` / `--restore-plugins` without `--git-clone` were silently ignored
- **Status:** Fixed in `7d1f977` (they now log a warning).

### X4. `config_template.ini` in the audited checkout was owned by root
- **Problem:** It was probably written by a self-update run as root. That is a symptom of H10: the checkout is updated by root but owned by a regular user.
- **Status:** Not a code issue. While editing, the file was replaced with a new copy, so it is now owned by the checkout's user like the other files.

---

## Future work (out of scope for this audit)

- **Rollback:** automatically restore the directory backup and the DB dump when the CLI upgrade fails.
- **Retention:** prune old backups and dumps; compress dumps.
- **PostgreSQL support** for the dump and the connection check.
- **Self-update hardening:** verify signed commits or tags, pin to release tags, use `--ff-only`, or replace self-update with a package or a separately managed checkout.
- **Credentials via `--defaults-extra-file`** with a `0600` temporary file instead of `MYSQL_PWD`, which MySQL 8 has deprecated.
- **Read `dbport`/`dbsocket`** from `$CFG->dboptions`.
- **Run the PHP CLI as the web server user by default** once existing installs have been migrated (`moodledata` ownership).
- **Policy decision:** keep the site in maintenance mode, or the webserver stopped, after a failed upgrade, and start the webserver only *after* the CLI upgrade.
- Replace `python3-apt` with `systemctl`-based service detection, and replace `requests` with `urllib`, to drop both runtime dependencies.
- **Clone swap and mount points:** the staging-directory swap (C2) renames the Moodle code folder, so that folder cannot be a mount point. Supporting that would need a copy-based fallback.
- **Clean up stale `.<moodle>_new_*` / `.<moodle>_old_*` directories** left behind by a run that was killed with SIGKILL.
- **Test `main()` end to end**, for example by factoring the git-clone preparation into a function (see L2).
- **`checks.php` exit codes:** only the output text is inspected. Treating exit code ≥ 2 (critical) as an error needs confirming against Moodle's checks API first.
