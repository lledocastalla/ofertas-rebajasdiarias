#!/usr/bin/env python3
"""
Catch-up manual de multi-tienda (24 ago 2026, ver MULTITIENDA_DISENO.md).

Uso puntual: la rotación normal de update_offers.py (1 de los 7 feeds de Leroy Merlin por
ciclo) tarda ~18-24h en cubrir todas las categorías. Este script descarga los 7 feeds de
golpe (fetch_leroy_merlin_offers_all()) + Stylevana, y los fusiona en offers.json con el
MISMO criterio de merge/poda que usa main() en update_offers.py — para no dejar el catálogo
"cojo" (solo Jardín/Bricolaje) mientras la rotación hace su camino sola.

No toca el estado de rotación (~/.rebajas_leroy_feed_state.json): el cron normal de la Pi
sigue rotando a su ritmo de siempre después de este catch-up puntual, sin duplicar trabajo.

Uso: python3 catchup_multitienda.py [--dry-run]
"""
import json
import subprocess
import sys
from datetime import datetime, timedelta

from multitienda_feeds import fetch_leroy_merlin_offers_all, fetch_stylevana_offers

REPO_DIR = "."
OFFERS_PATH = f"{REPO_DIR}/offers.json"
STALE_AFTER_DAYS = 2  # mismo valor que STALE_AFTER_DAYS en update_offers.py


def log(msg):
    print(f"[catchup_multitienda] {msg}", flush=True)


def main():
    dry_run = "--dry-run" in sys.argv

    subprocess.run(["git", "-C", REPO_DIR, "pull", "--quiet"], check=False)

    with open(OFFERS_PATH, encoding="utf-8") as f:
        existing = json.load(f)
    existing_offers = {o["id"]: o for o in existing.get("offers", []) if o.get("id")}
    log(f"Catálogo actual: {len(existing_offers)} ofertas")

    new_or_updated = {}
    new_or_updated.update(fetch_leroy_merlin_offers_all(log))
    new_or_updated.update(fetch_stylevana_offers(log))
    log(f"Multi-tienda esta pasada: {len(new_or_updated)} ofertas (Leroy Merlin + Stylevana)")

    now_iso = datetime.now().astimezone().isoformat(timespec="seconds")
    merged = dict(existing_offers)
    for oid, offer in new_or_updated.items():
        prior = existing_offers.get(oid)
        offer["first_seen"] = prior["first_seen"] if prior else now_iso
        offer["last_seen"] = now_iso
        merged[oid] = offer

    # Poda: mismo criterio que update_offers.py (STALE_AFTER_DAYS), pero aquí solo pueden
    # caducar ofertas de tienda que ya no aparecen en el feed — Amazon no se toca en este
    # catch-up (no se re-visita), así que sus last_seen quedan como estaban.
    cutoff = datetime.now().astimezone() - timedelta(days=STALE_AFTER_DAYS)

    def _is_fresh(o):
        try:
            return datetime.fromisoformat(o["last_seen"]) >= cutoff
        except Exception:
            return True

    pruned = {k: v for k, v in merged.items() if _is_fresh(v)}
    removed = len(merged) - len(pruned)
    if removed:
        log(f"Se retiran {removed} ofertas no vistas en los últimos {STALE_AFTER_DAYS} días.")
    merged = pruned

    log(f"Catálogo final: {len(merged)} ofertas.")

    if dry_run:
        log("--dry-run: no se escribe offers.json ni se hace commit.")
        return

    output = {
        "updated_at": now_iso,
        "affiliate_tag": existing.get("affiliate_tag", ""),
        "offers": list(merged.values()),
    }
    with open(OFFERS_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    subprocess.run(["git", "-C", REPO_DIR, "add", "offers.json"], check=False)
    commit = subprocess.run(
        ["git", "-C", REPO_DIR, "commit", "-m",
         f"Catch-up multi-tienda: {len(new_or_updated)} ofertas de los 7 feeds Leroy Merlin + Stylevana ({now_iso})"],
        check=False,
    )
    if commit.returncode == 0:
        subprocess.run(["git", "-C", REPO_DIR, "push", "--quiet"], check=False)
        log("Commit y push realizados.")
    else:
        log("Nada que comitear (sin cambios reales).")


if __name__ == "__main__":
    main()
