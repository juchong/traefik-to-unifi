"""Module for synchronizing Traefik hostnames with Unifi static DNS entries."""

import json
import logging
import os
import re
from datetime import UTC, datetime

import docker
import requests
import urllib3

# Reasons an entry lives in the ledger's "unmanaged" bucket (visibility only,
# never auto-deleted).
REASON_NOT_OURS = "not-created-by-us"
REASON_VALUE_DRIFT = "value-drift"
REASON_OPTED_OUT = "opted-out-orphan"


def _empty_ledger():
    return {"managed": {}, "unmanaged": {}}


def load_ledger(path):
    """Load the two-bucket ownership ledger; missing/corrupt -> empty (fail-safe)."""
    if not path:
        return _empty_ledger()
    try:
        with open(path) as f:
            data = json.load(f)
        managed = data.get("managed") or {}
        unmanaged = data.get("unmanaged") or {}
        if not isinstance(managed, dict) or not isinstance(unmanaged, dict):
            raise ValueError("bad ledger shape")
        return {"managed": managed, "unmanaged": unmanaged}
    except (OSError, ValueError, json.JSONDecodeError) as e:
        logging.warning(f"DNS state ledger unreadable ({path}); starting empty: {e}")
        return _empty_ledger()


def save_ledger(path, ledger):
    """Atomically persist the ledger. Best-effort; logs on failure."""
    if not path:
        return
    try:
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            json.dump(ledger, f, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        logging.error(f"Failed to write DNS state ledger {path}: {e}")


def append_history(path, record, max_records):
    """Append one JSON-line sync record, trimmed to the last max_records.

    ponytail: rewrites the whole file each sync (O(n)); n is capped at
    SYNC_HISTORY_MAX (default 200) so this is trivial. Upgrade to a real
    append + periodic-trim only if that cap grows large.
    """
    if not path:
        return
    try:
        lines = []
        if os.path.exists(path):
            with open(path) as f:
                lines = f.read().splitlines()
        lines.append(json.dumps(record))
        lines = lines[-max_records:]
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
    except OSError as e:
        logging.error(f"Failed to append sync history {path}: {e}")


def compute_prune_actions(
    ledger,
    desired_hosts,
    prune_prefs,
    unifi_entries,
    created_now,
    traefik_ip,
    record_type,
    grace,
    now_iso,
):
    """Pure decision function for prune mode (no I/O -> unit-testable).

    Args:
        ledger: current {"managed": {...}, "unmanaged": {...}}.
        desired_hosts: set of hostnames currently backed by a live filtered
            container + Traefik route.
        prune_prefs: {host: bool} eligibility for currently-present hosts
            (opt-out wins; any container with prune=false -> False).
        unifi_entries: iterable of dicts {"key","value","record_type","_id"}
            (full current UniFi snapshot).
        created_now: set of hosts this tool POSTed this cycle (enter "managed").
        traefik_ip / record_type: what an entry "owned by us" looks like.
        grace: consecutive-absent syncs required before a delete.
        now_iso: timestamp string for first_seen stamping.

    Returns (to_delete, released, ledger_next):
        to_delete: [(host, _id)] still-ours entries past grace -> DELETE now.
        released: [(host, reason)] moved managed -> unmanaged this cycle.
        ledger_next: next ledger. to_delete hosts remain in "managed"
            (retriable) until the caller confirms deletion and drops them.
    """
    by_key = {e["key"]: e for e in unifi_entries}
    old_managed = ledger.get("managed", {})
    old_unmanaged = ledger.get("unmanaged", {})

    managed = {}
    unmanaged = {}
    to_delete = []
    released = []

    # Hosts we (may) manage: previously managed + freshly created this cycle.
    for host in set(old_managed) | set(created_now):
        prev = old_managed.get(host, {})
        # Eligibility: refresh from live prefs when the host is present now,
        # else fall back to what we recorded at creation.
        if host in prune_prefs:
            eligible = bool(prune_prefs[host])
        else:
            eligible = bool(prev.get("prune_eligible", True))

        if host in desired_hosts or host in created_now:
            managed[host] = {
                "value": traefik_ip,
                "record_type": record_type,
                "prune_eligible": eligible,
                "missing_count": 0,
            }
            continue

        # Container gone.
        if not eligible:
            released.append((host, REASON_OPTED_OUT))
            unmanaged[host] = {
                "value": prev.get("value", traefik_ip),
                "record_type": prev.get("record_type", record_type),
                "reason": REASON_OPTED_OUT,
                "first_seen": now_iso,
            }
            continue

        entry = by_key.get(host)
        if entry is None:
            # Already gone from UniFi -> drop silently (no delete, no ledger).
            continue
        if entry.get("value") != traefik_ip or entry.get("record_type") != record_type:
            # Value drifted / adopted manually -> stop managing, keep visible.
            released.append((host, REASON_VALUE_DRIFT))
            unmanaged[host] = {
                "value": entry.get("value"),
                "record_type": entry.get("record_type"),
                "reason": REASON_VALUE_DRIFT,
                "first_seen": now_iso,
            }
            continue

        missing = int(prev.get("missing_count", 0)) + 1
        if missing >= grace:
            to_delete.append((host, entry["_id"]))
        # Keep in managed (retriable) regardless; caller drops on confirmed delete.
        managed[host] = {
            "value": traefik_ip,
            "record_type": record_type,
            "prune_eligible": eligible,
            "missing_count": missing,
        }

    # Classify every remaining UniFi entry pointing at our IP that we do not
    # manage -> "unmanaged" (visibility only; never deleted).
    for entry in unifi_entries:
        host = entry["key"]
        if host in managed or host in unmanaged:
            continue
        if entry.get("value") != traefik_ip:
            continue  # not pointing at us; ignore entirely
        prev_un = old_unmanaged.get(host, {})
        unmanaged[host] = {
            "value": entry.get("value"),
            "record_type": entry.get("record_type"),
            "reason": prev_un.get("reason", REASON_NOT_OURS),
            "first_seen": prev_un.get("first_seen", now_iso),
        }

    return to_delete, released, {"managed": managed, "unmanaged": unmanaged}


class TraefikToUnifi:
    """Synchronizes Traefik hostnames with Unifi static DNS entries."""

    def __init__(self):
        """Initializes the TraefikToUnifi instance."""

        # class variables to track state between syncs
        self.traefik_domains_json_last_run = "[]"
        self.number_of_syncs_without_change = 0
        self.is_first_run = True

        # Load environment variables
        self.traefik_ip = os.environ.get("TRAEFIK_IP")
        self.traefik_api_url = os.environ.get("TRAEFIK_API_URL")
        self.unifi_url = os.environ.get("UNIFI_URL")
        self.unifi_username = os.environ.get("UNIFI_USERNAME")
        self.unifi_password = os.environ.get("UNIFI_PASSWORD")
        self.unifi_api_key = os.environ.get("UNIFI_API_KEY")

        # Load optional environment variables with defaults
        self.ignore_ssl_warnings = os.environ.get("IGNORE_SSL_WARNINGS", "false") in (
            "1",
            "true",
            "True",
            "TRUE",
        )
        self.dns_record_type = os.environ.get("DNS_RECORD_TYPE", "A")
        self.full_sync_interval = int(os.environ.get("FULL_SYNC_INTERVAL", "5"))

        # Docker label filtering (similar to cloudflare-companion)
        # When set, only containers with matching labels will have DNS entries created
        self.docker_filter_label = os.environ.get("DOCKER_FILTER_LABEL")
        self.docker_filter_value = os.environ.get("DOCKER_FILTER_VALUE")
        self.docker_client = None

        # JSON output file for tracking synced DNS entries
        self.output_file = os.environ.get("DNS_OUTPUT_FILE")

        # --- Prune (sync/delete) mode ---
        self.prune_enabled = os.environ.get("UNIFI_DNS_PRUNE", "false") in (
            "1",
            "true",
            "True",
            "TRUE",
        )
        self.dry_run = os.environ.get("DRY_RUN", "false") in (
            "1",
            "true",
            "True",
            "TRUE",
        )
        self.prune_grace_cycles = int(os.environ.get("PRUNE_GRACE_CYCLES", "3"))
        # Per-container opt-out label, e.g. "traefik.unifi-dns.prune".
        self.prune_label = os.environ.get("PRUNE_LABEL") or (
            f"{self.docker_filter_label}.prune" if self.docker_filter_label else None
        )
        self.dns_state_file = os.environ.get("DNS_STATE_FILE", "/data/dns-state.json")

        # --- Sync history (for the web UI + liveness) ---
        self.sync_history_file = os.environ.get(
            "SYNC_HISTORY_FILE", "/data/sync-history.jsonl"
        )
        self.sync_history_max = int(os.environ.get("SYNC_HISTORY_MAX", "200"))

        if self.docker_filter_label:
            logging.info(
                f"Docker label filtering enabled: {self.docker_filter_label}={self.docker_filter_value or '*'}"
            )
            try:
                self.docker_client = docker.from_env()
                logging.info("Docker client initialized successfully.")
            except docker.errors.DockerException as e:
                logging.error(f"Failed to initialize Docker client: {e}")
                logging.warning(
                    "Docker label filtering will be disabled. "
                    "Ensure Docker socket is mounted at /var/run/docker.sock"
                )
                self.docker_filter_label = None

        if self.ignore_ssl_warnings:
            # we show our own warning on startup, no warning on each request required
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            logging.warning(
                "Ignoring SSL warnings as per configuration. This is insecure and should only be used for testing purposes. Adding certificate verification is strongly advised. See: https://urllib3.readthedocs.io/en/latest/advanced-usage.html#tls-warnings"
            )

        # Validate required environment variables
        for key, value in {
            "UNIFI_URL": self.unifi_url,
            "TRAEFIK_IP": self.traefik_ip,
            "TRAEFIK_API_URL": self.traefik_api_url,
        }.items():
            if value is None:
                raise ValueError(f"Required environment variable {key} is not set.")

        if (
            not self.unifi_username or not self.unifi_password
        ) and not self.unifi_api_key:
            raise ValueError(
                "Either UNIFI_USERNAME and UNIFI_PASSWORD or UNIFI_API_KEY should be set."
            )

        # Validate optional environment variables
        if self.dns_record_type not in ("A", "CNAME"):
            raise ValueError(
                f"Invalid DNS_RECORD_TYPE: {self.dns_record_type}. Allowed values are 'A' or 'CNAME'."
            )

        if self.full_sync_interval < 2:
            raise ValueError(
                f"Invalid FULL_SYNC_INTERVAL: {self.full_sync_interval}. Must be 2 or greater."
            )

        if self.prune_grace_cycles < 1:
            raise ValueError(
                f"Invalid PRUNE_GRACE_CYCLES: {self.prune_grace_cycles}. Must be 1 or greater."
            )

        if self.prune_enabled and not self.docker_filter_label:
            # Ownership is only knowable via the Docker label scan, so prune
            # cannot safely run without it. Fail closed rather than risk deletes.
            raise ValueError(
                "UNIFI_DNS_PRUNE requires DOCKER_FILTER_LABEL to be set "
                "(ownership is tracked via the Docker label scan)."
            )

        if self.prune_enabled:
            logging.warning(
                f"Prune mode ENABLED (grace={self.prune_grace_cycles} cycles, "
                f"dry_run={self.dry_run}, opt-out label='{self.prune_label}'). "
                "Only entries this tool created will ever be deleted."
            )

        logging.debug(f"UNIFI_URL={self.unifi_url}")
        logging.debug(f"TRAEFIK_API_URL={self.traefik_api_url}")
        logging.debug(f"FULL_SYNC_INTERVAL={self.full_sync_interval}")

    def get_docker_hostnames_with_label(self):
        """
        Query Docker for containers with the specified label and extract their hostnames.

        Returns a tuple ``(allowed_hostnames, prune_prefs)``:
        - ``allowed_hostnames``: set of hostnames that should be in DNS, or
          ``None`` when filtering is disabled or the Docker query failed. The
          ``None`` contract is preserved: for add/update it means "no filter",
          and for prune it means "ownership unknowable -> skip pruning".
        - ``prune_prefs``: ``{hostname: bool}`` prune eligibility for the hosts
          seen this scan. Opt-out wins: any contributing container with the
          prune label set to a false value marks the host ineligible.
        """
        if not self.docker_client or not self.docker_filter_label:
            return None, {}  # No filtering, include all

        allowed_hostnames = set()
        prune_prefs = {}
        false_values = ("0", "false", "False", "FALSE", "no", "No")

        try:
            containers = self.docker_client.containers.list()
            logging.debug(f"Found {len(containers)} running containers.")

            for container in containers:
                labels = container.labels
                label_value = labels.get(self.docker_filter_label)

                # Check if container has the filter label
                if label_value is None:
                    continue

                # If a specific value is required, check it matches
                if self.docker_filter_value and label_value != self.docker_filter_value:
                    continue

                logging.debug(
                    f"Container {container.name} has matching label "
                    f"{self.docker_filter_label}={label_value}"
                )

                # Per-container prune preference (default eligible; opt-out wins).
                container_eligible = True
                if self.prune_label and labels.get(self.prune_label) in false_values:
                    container_eligible = False

                # Extract hostnames from Traefik router labels
                for label_name, label_val in labels.items():
                    # Match traefik.http.routers.*.rule labels containing Host()
                    if (
                        label_name.startswith("traefik.http.routers.")
                        and label_name.endswith(".rule")
                        and "Host(" in str(label_val)
                    ):
                        # Extract hostname from Host(`hostname`) pattern
                        match = re.search(r"Host\(`([^`]+)`\)", str(label_val))
                        if match:
                            hostname = match.group(1)
                            allowed_hostnames.add(hostname)
                            # Opt-out wins when multiple containers share a host.
                            prune_prefs[hostname] = (
                                prune_prefs.get(hostname, True) and container_eligible
                            )
                            logging.debug(
                                f"Found hostname {hostname} in container {container.name}"
                            )

        except docker.errors.APIError as e:
            logging.error(f"Docker API error: {e}")
            return None, {}  # On error, fall back to no filtering / skip prune

        logging.info(
            f"Docker label filter found {len(allowed_hostnames)} allowed hostnames."
        )
        return allowed_hostnames, prune_prefs

    def sync(self):
        """
        Synchronizes Traefik hostnames with Unifi static DNS entries.
        - Fetches routers from Traefik API
        - Extracts hostnames from router rules
        - Filters by Docker labels if configured
        - Compares them with existing Unifi static DNS entries
        - Adds missing hosts or updates outdated ones
        """

        logging.info("Starting synchronization...")

        counts = {"added": 0, "updated": 0, "deleted": 0, "released": 0, "errors": 0}
        status = "ok"
        try:
            # Get allowed hostnames + per-host prune prefs from Docker labels.
            allowed_hostnames, prune_prefs = self.get_docker_hostnames_with_label()

            # Request routers from Traefik API
            traefik_domains = self.fetch_traefik_domains(allowed_hostnames)

            if not traefik_domains:
                logging.warning("No hostnames found in Traefik routers.")
                return

            # Detect changes compared to previous run
            traefik_domains_json = json.dumps(traefik_domains, indent=4)
            traefik_domains_json_changed = (
                traefik_domains_json != self.traefik_domains_json_last_run
            )

            if traefik_domains_json_changed:
                self.number_of_syncs_without_change = 0
                if self.is_first_run:
                    logging.debug(
                        f"Extracted {len(traefik_domains)} hostnames in Traefik routers in first run."
                    )
                else:
                    logging.debug(
                        f"Extracted {len(traefik_domains)} hostnames in Traefik routers and detected changes since last run."
                    )
            else:
                self.number_of_syncs_without_change += 1
                logging.info(
                    f"No changes since last sync - {self.number_of_syncs_without_change} time(s) since last full sync."
                )

            self.is_first_run = False
            self.traefik_domains_json_last_run = traefik_domains_json

            # Skip the UniFi round-trip when nothing changed, but do a full sync
            # every FULL_SYNC_INTERVAL runs so manually modified records get fixed.
            # Never skip while prune mode is on: grace-cycle counting and stale
            # detection must run every cycle.
            if not traefik_domains_json_changed and not self.prune_enabled:
                if self.number_of_syncs_without_change < self.full_sync_interval:
                    logging.info("Skipping UniFi update due to no changes.")
                    # Still refresh the output file so its last_updated timestamp
                    # reflects that the sync loop is alive and current.
                    self.write_dns_entries_to_file(traefik_domains)
                    return

                # reset counter and do full sync
                logging.info(
                    "Performing full sync with UniFi despite no changes in Traefik hostnames."
                )
                self.number_of_syncs_without_change = 0

            # Login to UniFi
            unifi_session = requests.Session()
            if self.ignore_ssl_warnings:
                unifi_session.verify = False

            if not self.unifi_api_key:
                logging.debug(f"Logging in to UniFi {self.unifi_url} ...")
                unifi_login_response = unifi_session.post(
                    f"{self.unifi_url}api/auth/login",
                    json={
                        "username": self.unifi_username,
                        "password": self.unifi_password,
                    },
                )

                if unifi_login_response.status_code != 200:
                    raise ValueError(
                        f"Failed to login to UniFi API. Status code: {unifi_login_response.status_code}"
                    )

                logging.debug("Login successful, updating CSRF token.")
                unifi_session.headers.update(
                    {"X-Csrf-Token": unifi_login_response.headers["X-Csrf-Token"]}
                )
            else:
                logging.debug("Using UniFi API Key for authentication.")
                unifi_session.headers.update({"X-API-KEY": self.unifi_api_key})

            # Fetch existing static DNS entries from UniFi
            logging.debug("Fetching existing static DNS entries from UniFi...")
            get_static_dns_entries_response = unifi_session.get(
                f"{self.unifi_url}proxy/network/v2/api/site/default/static-dns"
            )

            if get_static_dns_entries_response.status_code != 200:
                raise ValueError(
                    f"Failed to get static DNS entries from UniFi API. Status code: {get_static_dns_entries_response.status_code}"
                )

            # Full snapshot (used by prune classification + ownership double-check).
            unifi_entries_full = [
                {
                    "key": entry["key"],
                    "value": entry["value"],
                    "record_type": entry.get("record_type"),
                    "_id": entry["_id"],
                }
                for entry in get_static_dns_entries_response.json()
            ]
            unifi_static_dns_entries = [
                (e["key"], e["value"], e["_id"]) for e in unifi_entries_full
            ]

            entries_to_update = []
            hosts_to_add = []

            # Compare Traefik hostnames with UniFi static DNS entries
            for dns_name in traefik_domains:
                already_exists = False
                for entry in unifi_static_dns_entries:
                    if entry[0] == dns_name:
                        already_exists = True
                        if entry[1] != self.traefik_ip:
                            logging.info(
                                f"DNS name {dns_name} already exists but with different value {entry[1]}. Scheduling update to {self.traefik_ip}."
                            )
                            entries_to_update.append((entry[0], entry[2]))
                        break

                if not already_exists:
                    logging.info(
                        f"Scheduling addition of DNS name {dns_name} to UniFi static DNS entries."
                    )
                    hosts_to_add.append(dns_name)

            logging.info(
                f"DNS entries to update: {len(entries_to_update)}, "
                f"new DNS entries to add: {len(hosts_to_add)}"
            )

            if not entries_to_update and not hosts_to_add:
                logging.debug("No changes required for UniFi static DNS entries.")
            else:
                logging.info(
                    f"Updating DNS entries using DNS record type: {self.dns_record_type}"
                )

            # Update existing entries
            for key, entry_id in entries_to_update:
                update_static_dns_entry_response = unifi_session.put(
                    f"{self.unifi_url}proxy/network/v2/api/site/default/static-dns/{entry_id}",
                    json={
                        "enabled": True,
                        "key": key,
                        "record_type": self.dns_record_type,
                        "value": self.traefik_ip,
                        "_id": entry_id,
                    },
                )

                if update_static_dns_entry_response.status_code == 200:
                    counts["updated"] += 1
                    logging.info(f"Successfully updated DNS entry {key} in Unifi API.")
                else:
                    counts["errors"] += 1
                    logging.error(
                        f"Failed to update static DNS entry {key} in UniFi API. Status code: {update_static_dns_entry_response.status_code}"
                    )

            # Add new entries
            created_now = set()
            for host in hosts_to_add:
                add_static_dns_entry_response = unifi_session.post(
                    f"{self.unifi_url}proxy/network/v2/api/site/default/static-dns",
                    json={
                        "enabled": True,
                        "key": host,
                        "record_type": self.dns_record_type,
                        "value": self.traefik_ip,
                    },
                )

                if add_static_dns_entry_response.status_code == 200:
                    counts["added"] += 1
                    created_now.add(host)
                    # Reflect the new entry in our local snapshot so the prune
                    # classifier treats it as ours this same cycle.
                    created = add_static_dns_entry_response.json()
                    unifi_entries_full.append(
                        {
                            "key": host,
                            "value": self.traefik_ip,
                            "record_type": self.dns_record_type,
                            "_id": created.get("_id"),
                        }
                    )
                    logging.info(f"Successfully added DNS entry {host} in UniFi API.")
                else:
                    counts["errors"] += 1
                    logging.error(
                        f"Failed to add static DNS entry {host} in UniFi API. Status code: {add_static_dns_entry_response.status_code}"
                    )

            # --- Prune phase (opt-in; only ever deletes entries we created) ---
            if self.prune_enabled and allowed_hostnames is not None:
                deleted, released, prune_errors = self.prune(
                    unifi_session,
                    set(traefik_domains),
                    prune_prefs,
                    unifi_entries_full,
                    created_now,
                )
                counts["deleted"] += deleted
                counts["released"] += released
                counts["errors"] += prune_errors
            elif self.prune_enabled:
                logging.warning(
                    "Prune skipped: Docker container set is unknown this cycle "
                    "(ownership unknowable). Add/update ran normally."
                )

            logging.info("Synchronization completed.")

            # Write current DNS entries to JSON file if configured
            self.write_dns_entries_to_file(traefik_domains)
        except Exception:
            status = "error"
            raise
        finally:
            if counts["errors"]:
                status = "error"
            self._record_history(counts, status)

    def prune(
        self, unifi_session, desired_hosts, prune_prefs, unifi_entries_full, created_now
    ):
        """Delete stale entries this tool created.

        Returns (deleted, released, errors). Only entries in the ledger's
        "managed" bucket that are past the grace period and still verifiably
        ours are deleted; deletion is confirmed with a re-fetch before the
        ledger drops the entry.
        """
        now_iso = datetime.now(UTC).isoformat()
        ledger = load_ledger(self.dns_state_file)
        to_delete, released, ledger_next = compute_prune_actions(
            ledger,
            desired_hosts,
            prune_prefs,
            unifi_entries_full,
            created_now,
            self.traefik_ip,
            self.dns_record_type,
            self.prune_grace_cycles,
            now_iso,
        )

        for host, reason in released:
            logging.info(
                f"Releasing '{host}' from managed set (reason={reason}); not deleting."
            )

        errors = 0
        deleted = 0
        base = f"{self.unifi_url}proxy/network/v2/api/site/default/static-dns"

        attempted = []
        for host, entry_id in to_delete:
            if self.dry_run:
                logging.warning(
                    f"[DRY_RUN] Would delete stale DNS entry '{host}' ({entry_id})."
                )
                continue
            resp = unifi_session.delete(f"{base}/{entry_id}")
            # 404 == already gone (stale _id) -> treat as success for re-verify.
            if resp.status_code in (200, 204, 404):
                logging.info(
                    f"DELETE '{host}' returned {resp.status_code}; will re-verify."
                )
                attempted.append(host)
            else:
                errors += 1
                logging.error(
                    f"Failed to delete DNS entry '{host}'. Status: {resp.status_code}; "
                    "keeping in ledger for retry."
                )

        # Post-delete re-verify (the required double-check): only drop from the
        # ledger once a fresh GET confirms the entry is actually gone.
        if attempted:
            verify = unifi_session.get(base)
            present_keys = (
                {e["key"] for e in verify.json()} if verify.status_code == 200 else None
            )
            for host in attempted:
                if present_keys is None:
                    errors += 1
                    logging.error(
                        f"Could not re-verify deletion of '{host}'; keeping in ledger."
                    )
                elif host in present_keys:
                    errors += 1
                    logging.error(
                        f"'{host}' still present after DELETE; keeping in ledger for retry."
                    )
                else:
                    ledger_next["managed"].pop(host, None)
                    deleted += 1
                    logging.info(f"Confirmed deletion of stale DNS entry '{host}'.")

        save_ledger(self.dns_state_file, ledger_next)
        return deleted, len(released), errors

    def _record_history(self, counts, status):
        """Append one structured record of this sync for the web UI + liveness."""
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "added": counts["added"],
            "updated": counts["updated"],
            "deleted": counts["deleted"],
            "released": counts["released"],
            "errors": counts["errors"],
            "dry_run": self.dry_run,
            "status": status,
        }
        append_history(self.sync_history_file, record, self.sync_history_max)

    def write_dns_entries_to_file(self, hostnames):
        """
        Writes the current DNS entries to a JSON file for tracking.

        Args:
            hostnames: List of hostnames that are synced to UniFi.
        """
        if not self.output_file:
            return

        try:
            # Deduplicate hostnames (multiple routers can have same hostname)
            unique_hostnames = list(dict.fromkeys(hostnames))

            output_data = {
                "last_updated": datetime.now(UTC).isoformat(),
                "traefik_ip": self.traefik_ip,
                "dns_record_type": self.dns_record_type,
                "total_entries": len(unique_hostnames),
                "entries": sorted(
                    [
                        {
                            "hostname": hostname,
                            "target": self.traefik_ip,
                            "type": self.dns_record_type,
                        }
                        for hostname in unique_hostnames
                    ],
                    key=lambda x: x["hostname"],
                ),
            }

            with open(self.output_file, "w") as f:
                json.dump(output_data, f, indent=2)

            logging.info(
                f"Wrote {len(unique_hostnames)} DNS entries to {self.output_file}"
            )
        except Exception as e:
            logging.error(f"Failed to write DNS entries to file: {e}")

    def fetch_traefik_domains(self, allowed_hostnames=None):
        """
        Fetches and returns hostnames from Traefik routers.

        Args:
            allowed_hostnames: Optional set of hostnames to filter by.
                              If None, all hostnames are returned.
                              If a set, only hostnames in the set are returned.
        """

        logging.debug("Extracting hostnames from Traefik...")

        traefik_session = requests.Session()

        if self.ignore_ssl_warnings:
            traefik_session.verify = False

        traefik_routers_response = traefik_session.get(
            f"{self.traefik_api_url}http/routers"
        )

        if traefik_routers_response.status_code != 200:
            raise ValueError(
                f"Failed to query Traefik API. Status code: {traefik_routers_response.status_code}"
            )

        traefik_domains = []

        for router in traefik_routers_response.json():
            if "rule" in router and "Host(" in router["rule"]:
                logging.debug(f"Router: {router['name']} with rule: {router['rule']}")
                match = re.search(r"Host\(`([^`]+)`\)", router["rule"])

                if not match:
                    logging.debug(f"No DNS name found in the rule {router['rule']}.")
                    continue

                dns_name = match.group(1)

                # Apply Docker label filter if configured
                if allowed_hostnames is not None:
                    if dns_name not in allowed_hostnames:
                        logging.debug(
                            f"Skipping hostname {dns_name} - not in allowed list from Docker labels."
                        )
                        continue
                    logging.debug(
                        f"Including hostname {dns_name} - matches Docker label filter."
                    )

                logging.debug(f"Extracted hostname from Traefik: {dns_name}")
                traefik_domains.append(dns_name)

        return traefik_domains
