"""
Copyright (c) 2024, FatalMerlin <https://github.com/FatalMerlin>
All rights reserved.

This source code is licensed under the BSD-style license found in the
LICENSE file in the root directory of this source tree.
"""

import logging
import os
import re
from time import sleep
from typing import List, TypedDict
import requests

# from pprint import pprint
import subprocess
import sqlite3

DB_PATH = os.getenv("DB_PATH", "dns.db")

TRAEFIK_HOST = os.getenv("TRAEFIK_HOST", "localhost")
TRAEFIK_PORT = os.getenv("TRAEFIK_PORT", "8080")
# The entrypoints to monitor for routes which need DNS updates, comma separated
TRAEFIK_ENTRYPOINTS = os.getenv("TRAEFIK_ENTRYPOINTS", "web,websecure").split(",")

DNS_SERVER = os.getenv("DNS_SERVER", "192.168.178.1")
DNS_DOMAIN = os.getenv("DNS_DOMAIN", "fritz.box")

TARGET_IP = os.getenv("TARGET_IP", "192.168.178.2")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

UPDATE_INTERVAL = int(os.getenv("UPDATE_INTERVAL", 60))

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS dns (
    hostname TEXT PRIMARY KEY NOT NULL UNIQUE,
    router TEXT NOT NULL
);
"""

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s - %(levelname)s - %(message)s")

log = logging.getLogger("dns-updater")
log.info("Starting DNS updater")

dirname = os.path.dirname(__file__)
dbfile = os.path.join(dirname, DB_PATH)

try:
    log.info(f"Connecting to DB at {dbfile}")
    if not os.path.exists(dbfile):
        os.makedirs(os.path.dirname(dbfile), exist_ok=True)
    conn = sqlite3.connect(dbfile)
    cursor = conn.cursor()
    log.info("Creating DB schema")
    cursor.execute(DB_SCHEMA)
except Exception as e:
    log.exception("Error connecting to DB", e)
    raise e


class Router(TypedDict):
    entryPoints: List[str]
    service: str
    rule: str
    ruleSyntax: str
    priority: int
    status: str
    using: List[str]
    name: str
    provider: str


class Hostname(TypedDict):
    hostname: str
    router: Router


def fetch_routers() -> List[Router]:
    log.info(f"Fetching routers from Traefik at {TRAEFIK_HOST}:{TRAEFIK_PORT}")
    routers: List[Router] = []

    response = requests.get(f"http://{TRAEFIK_HOST}:{TRAEFIK_PORT}/api/http/routers")
    parsed = response.json()

    log.debug(f"Received {len(parsed)} routers from Traefik")

    for entry in parsed:
        try:
            router = Router(**entry)
            routers.append(router)
        except Exception as e:
            print(e)
            continue

    return routers


def filter_routers(routers: List[Router]) -> List[Router]:
    return [
        router
        for router in routers
        if any(
            entryPoint in router["entryPoints"] for entryPoint in TRAEFIK_ENTRYPOINTS
        )
    ]


hostname_regex = re.compile(r"Host\(`(.+)`\)")


def extract_hostnames(routers: List[Router]) -> List[Hostname]:
    hostnames: List[Hostname] = []

    for router in routers:
        for match in re.finditer(hostname_regex, router["rule"]):
            hostnames.append(Hostname(hostname=match.group(1), router=router))

    return hostnames


def update_dns_entry(hostname: str, delete=False):
    log.info(
        f"Updating DNS entry for {hostname}, mode={'add' if not delete else 'delete'}"
    )

    proc = subprocess.Popen(
        ["/usr/bin/nsupdate", "-4"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )

    input = f"""\
server {DNS_SERVER}
zone {DNS_DOMAIN}
update delete {hostname} A
"""

    if not delete:
        input += f"update add {hostname} 3600 A {TARGET_IP}\n"

    input += "send\nquit\n"

    stdout, stderr = proc.communicate(input=input)

    proc.wait()

    if proc.returncode != 0:
        raise Exception(stderr)

    log.debug("Updated DNS entry for", hostname, stdout)


def update_db(hostname: Hostname):
    log.info(f"Updating DB for {hostname}")

    try:
        cursor.execute(
            "INSERT OR REPLACE INTO dns (hostname, router) VALUES (?, ?)",
            (hostname["hostname"], hostname["router"]["name"]),
        )
        conn.commit()
    except Exception as e:
        log.exception(f"Error updating DB for {hostname}", e)


def cleanup_old_dns_entries(hostnames: List[Hostname]):
    log.info("Cleaning up old DNS entries")
    log.debug("Current hostnames:", hostnames)

    try:
        cursor.execute(
            "SELECT hostname FROM dns WHERE hostname NOT IN (?)",
            (",".join([hostname["hostname"] for hostname in hostnames]),),
        )

        old_hostnames = cursor.fetchall()
        old_hostnames = [hostname[0] for hostname in old_hostnames]
        log.debug("Old hostnames:", old_hostnames)

        for hostname in old_hostnames:
            try:
                update_dns_entry(hostname, delete=True)
                cursor.execute("DELETE FROM dns WHERE hostname = ?", (hostname,))
                log.info(f"Cleaned up old DNS entry for {hostname}")
            except Exception as e:
                log.exception(f"Error cleaning up old DNS entry for {hostname}", e)

        conn.commit()
    except Exception as e:
        log.exception("Error cleaning up old DNS entries", e)


def update_loop():
    routers = fetch_routers()
    filtered_routers = filter_routers(routers)
    hostnames = extract_hostnames(filtered_routers)

    for hostname in hostnames:
        try:
            update_dns_entry(hostname["hostname"])
            update_db(hostname)
        except Exception as e:
            log.exception(f"Error updating DNS entry for {hostname}", e)

    cleanup_old_dns_entries(hostnames)


while True:
    try:
        log.info("Starting update loop")
        update_loop()
    except Exception as e:
        log.exception("Error in update loop", e)

    log.info(f"Waiting for {UPDATE_INTERVAL} seconds")
    sleep(UPDATE_INTERVAL)

conn.close()
